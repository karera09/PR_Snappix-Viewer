"""Diagnostics dialog showing recorded performance stats.

Refreshes every 500 ms from :mod:`snappix.viewer.perf` so the user can
watch timings accumulate while they navigate.  The top table shows a
per-category summary (count / total / avg / median / min / max); the
bottom table shows the most recent individual events (ordered newest
first) so outliers can be inspected directly.

No measurement happens here — this dialog only *reads* the singleton
recorder.  Toggling the recorder on/off is handled by the menu in
:mod:`snappix.viewer.main_window`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..common.i18n import t
from ..common.ui import (
    align_header,
    demote_close_default,
    hint_style,
    localize_buttons,
    show_toast,
)
from .perf import CategoryStats, EventRecord, recorder


_CATEGORY_ORDER = [
    # Scan path (worker-thread)
    "scan_children",
    "scan_children_scandir",
    "metadata_pass_total",
    "folder_preview_scandir",
    "post_md_read",
    "post_md_parse",
    "list_files",
    "list_files_scandir",
    "recursive_search",
    "walk_for_search",
    "aspect_probe",
    # Thumbnail pipeline (worker-thread)
    "thumb_task_total",
    "thumb_cache_hit",
    "thumb_file_read",
    "thumb_decode",
    "thumb_decode_retry",
    "thumb_decode_pil",
    "thumb_scale",
    "thumb_resolve_folder",
    "thumb_pdf_load",
    "thumb_pdf_render",
    "thumb_video_load",
    "thumb_failed",
    # Main-thread UI
    "folder_selected",
    "folder_selected_show_markdown",
    "folder_selected_show_image",
    "folder_selected_list",
    "scan_finished_apply",
    "metadata_batch_apply",
    "populate_step",
    "populate_step_loop",
    "populate_step_enable_updates",
    "append_item_total",
    "append_ctor",
    "append_initial_icon",
    "append_set_icon",
    "append_add_item",
    "visible_request",
    "icon_flush",
    "build_tiles",
    "gallery_relayout",
    "gallery_paint",
]

_CATEGORY_LABELS = {
    "scan_children": "scan_children (shallow)",
    "scan_children_scandir": "  └ os.scandir(root)",
    "metadata_pass_total": "metadata_pass (all subfolders)",
    "folder_preview_scandir": "  └ os.scandir(subfolder)",
    "post_md_read": "  └ post.md read_text",
    "post_md_parse": "  └ post.md parse",
    # 用語は「情報パネル」に揃える（perf の内部カテゴリ名は i18n を通らないため
    # 文言の一括置換の対象にならない）。
    "list_files": "list_files (info panel)",
    "list_files_scandir": "  └ os.scandir",
    "recursive_search": "recursive name search (worker)",
    "walk_for_search": "  └ folder walk",
    "aspect_probe": "aspect probe / image header (worker)",
    "thumb_task_total": "thumbnail task (worker)",
    "thumb_cache_hit": "  └ disk-cache hit",
    "thumb_file_read": "  └ open()+read() bytes",
    "thumb_decode": "  └ QImageReader.read()",
    "thumb_decode_retry": "  └ retry without setScaledSize",
    "thumb_decode_pil": "  └ PIL decode fallback",
    "thumb_scale": "  └ QImage.scaled()",
    "thumb_resolve_folder": "  └ find_first_image()",
    "thumb_pdf_load": "  └ PDF open",
    "thumb_pdf_render": "  └ PDF page render",
    "thumb_video_load": "  └ video frame grab",
    "thumb_failed": "  └ FAILED (null/error)",
    "folder_selected": "folder selected (main)",
    "folder_selected_show_markdown": "  └ show_markdown",
    "folder_selected_show_image": "  └ show_image/empty",
    "folder_selected_list": "  └ file_list.set_folder",
    "scan_finished_apply": "apply scan result (main)",
    "metadata_batch_apply": "apply metadata batch (main)",
    "populate_step": "grid populate chunk (main)",
    "populate_step_loop": "  └ 80-item append loop",
    "populate_step_enable_updates": "  └ setUpdatesEnabled(True) reflow",
    "append_item_total": "    └ _append_item (per item)",
    "append_ctor": "        └ QListWidgetItem + setData/Tooltip",
    "append_initial_icon": "        └ _initial_icon_for (style lookup)",
    "append_set_icon": "        └ item.setIcon",
    "append_add_item": "        └ grid.addItem",
    "visible_request": "visible thumb enqueue (main)",
    "icon_flush": "icon flush QPixmap (main)",
    "build_tiles": "build tiles (main)",
    "gallery_relayout": "gallery relayout (main)",
    "gallery_paint": "gallery paint (main)",
}


def _format_ms(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    if v >= 1:
        return f"{v:.1f} ms"
    return f"{v:.2f} ms"


class PerfDialog(QDialog):
    """Modeless diagnostics window; stays open while user navigates."""

    #: Emitted when the in-dialog checkbox flips the recorder on/off, so the
    #: window can keep its diagnostics-menu QAction check state in sync (the
    #: ``PerfRecorder`` itself is Qt-free and can't notify).
    enabled_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.perf_dialog.window_title"))
        self.resize(960, 640)
        # Modeless so the user can keep navigating and watch stats update.
        self.setModal(False)

        self._build_ui()

        # Skip rebuilding the (expensive) tables when nothing changed between
        # ticks — otherwise the modeless dialog regenerates ~1200 table items
        # per second even while idle, adding GC/paint noise to the very
        # measurements it exists to show.  Keyed on (total event count,
        # enabled) since new events only ever append.
        self._last_render_key: tuple[int, bool] | None = None

        # 実際に表示されている間だけ回す（``showEvent`` / ``hideEvent``）。
        # このダイアログは main_window が単一
        # インスタンスを使い回す（WA_DeleteOnClose 無し）ので、無条件に
        # start すると閉じた後もセッション終了まで 500ms ごとに
        # ``recorder().snapshot()``（ロック保持下で最大 4000 件の
        # EventRecord + 全カテゴリ統計を複製）を GUI スレッドで実行し続け、
        # ワーカーの ``record()`` とロックを奪い合って計測対象そのものに
        # ノイズを足していた。
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.timeout.connect(self._refresh)
        # Populate immediately so the dialog isn't empty on open.
        self._refresh()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._refresh()
        self._refresh_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._refresh_timer.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        ctrl = QHBoxLayout()
        self._enable_check = QCheckBox(t("viewer.perf_dialog.enable_measurement"))
        self._enable_check.setChecked(recorder().is_enabled())
        self._enable_check.toggled.connect(self._on_toggle)
        ctrl.addWidget(self._enable_check)

        self._status_label = QLabel("")
        # Hint role — ``hint_style`` (palette Mid = the text_muted token; the
        # old ``placeholder-text`` pick predates the design system pinning
        # Mid, and split one semantic role across two colours).
        self._status_label.setStyleSheet(hint_style())
        ctrl.addWidget(self._status_label, 1)

        # メニュー「表示 ▸ 診断 ▸ 計測結果をクリア」
        # (viewer.main_window.perf_clear)
        # と同じ操作なので、専用キーを持たずそのキーを直接再利用して名称の
        # ドリフトを構造的に防ぐ（表記揺れガード test_no_duplicate_values 対応）。
        self._clear_btn = QPushButton(t("viewer.main_window.perf_clear"))
        self._clear_btn.clicked.connect(self._on_clear)
        ctrl.addWidget(self._clear_btn)

        self._copy_btn = QPushButton(t("viewer.perf_dialog.copy_stats"))
        self._copy_btn.clicked.connect(self._on_copy)
        ctrl.addWidget(self._copy_btn)
        # 「計測結果をクリア」は確認の無い破棄
        # なのに、``QDialog`` の中の ``QPushButton`` は既定で
        # ``autoDefault`` — 表を眺めていて Enter を押しただけで消えてしまう。
        # 下の ``demote_close_default`` は ``QDialogButtonBox`` の中しか見ない
        # ので、ボックス外のこの 2 本は自分で降格する（先行例は
        # ``plugin_host/dialog.py``）。
        for btn in (self._clear_btn, self._copy_btn):
            btn.setAutoDefault(False)
            btn.setDefault(False)
        outer.addLayout(ctrl)

        hint = QLabel(t("viewer.perf_dialog.hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(hint_style())
        outer.addWidget(hint)

        outer.addWidget(QLabel(t("viewer.perf_dialog.category_summary")))
        self._stats_table = QTableWidget()
        self._stats_table.setColumnCount(7)
        self._stats_table.setHorizontalHeaderLabels([
            t("viewer.perf_dialog.col_category"),
            t("viewer.perf_dialog.col_count"),
            t("viewer.perf_dialog.col_total"),
            t("viewer.perf_dialog.col_avg"),
            t("viewer.perf_dialog.col_median"),
            t("viewer.perf_dialog.col_min"),
            t("viewer.perf_dialog.col_max"),
        ])
        self._stats_table.verticalHeader().setVisible(False)
        self._stats_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        for col in range(1, 7):
            self._stats_table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeToContents
            )
        self._stats_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._stats_table.setSelectionBehavior(QTableWidget.SelectRows)
        outer.addWidget(self._stats_table, 3)

        outer.addWidget(QLabel(t("viewer.perf_dialog.recent_events")))
        self._events_table = QTableWidget()
        self._events_table.setColumnCount(3)
        self._events_table.setHorizontalHeaderLabels([
            t("viewer.perf_dialog.col_category"),
            t("viewer.perf_dialog.col_time"),
            t("common.label.details"),
        ])
        self._events_table.verticalHeader().setVisible(False)
        self._events_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self._events_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self._events_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        # カテゴリ列（インデント付きの長いラベルを持つ）
        # が ResizeToContents のまま実測 130px 前後まで縮み、詳細列（Stretch）
        # が残り全部（実測 750px 超）を持っていく配分になる。カテゴリ
        # 列が過度に狭くならないよう幅を与える。
        # setMinimumSectionSize はヘッダ全体の下限なので短い「時刻」列まで
        # 膨らませてしまう。列単位で幅を決める。
        self._events_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Interactive
        )
        self._events_table.horizontalHeader().resizeSection(0, 160)
        self._events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        # 見出しの寄せを列の中身に合わせる — 右寄せの
        # 数値・時刻セルの上で見出しだけが中央に残らないように。
        align_header(self._stats_table, right=range(1, 7))
        align_header(self._events_table, right=(1,))
        outer.addWidget(self._events_table, 2)

        buttons = demote_close_default(
            localize_buttons(QDialogButtonBox(QDialogButtonBox.Close))
        )
        # Close は RejectRole なので ``rejected`` だけが発火する
        # （``accepted`` への配線は発火しない）。
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)

    # ----------------------------------------------------------- slots

    def _on_toggle(self, checked: bool) -> None:
        recorder().set_enabled(checked)
        # Let the window mirror the new state onto its diagnostics-menu action.
        self.enabled_toggled.emit(checked)

    def _on_clear(self) -> None:
        recorder().clear()
        self._refresh()

    def _on_copy(self) -> None:
        text = self._format_snapshot_as_text()
        QGuiApplication.clipboard().setText(text)
        # クリップボードは不可視なので、成功トースト
        # （design.md の「成功 = 非モーダル」）が唯一の完了フィードバック。
        show_toast(self, t("viewer.perf_dialog.copy_stats_done_toast"), kind="success")

    # ----------------------------------------------------------- refresh

    def _refresh(self) -> None:
        events, stats, elapsed = recorder().snapshot()
        enabled = recorder().is_enabled()
        # Keep the enable checkbox in sync in case something else toggled it.
        block = self._enable_check.blockSignals(True)
        self._enable_check.setChecked(enabled)
        self._enable_check.blockSignals(block)

        total_events = sum(s.count for s in stats.values())
        if enabled:
            self._status_label.setText(
                t(
                    "viewer.perf_dialog.status_recording",
                    elapsed=elapsed,
                    total=total_events,
                )
            )
        else:
            self._status_label.setText(
                t("viewer.perf_dialog.status_stopped", total=total_events)
            )

        # Nothing changed since the last tick (events only ever append, so the
        # total count + enabled flag fully identify the table contents) — skip
        # regenerating every QTableWidgetItem.  The status label above still
        # updates each tick (elapsed seconds tick even with no new events).
        render_key = (total_events, enabled)
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key

        self._update_stats_table(stats)
        self._update_events_table(events)

    def _update_stats_table(self, stats: dict[str, CategoryStats]) -> None:
        # Show known categories in the curated order first, then anything new.
        ordered: list[str] = [c for c in _CATEGORY_ORDER if c in stats]
        for c in stats:
            if c not in ordered:
                ordered.append(c)
        self._stats_table.setRowCount(len(ordered))
        for row, cat in enumerate(ordered):
            s = stats[cat]
            label = _CATEGORY_LABELS.get(cat, cat)
            cells = [
                label,
                str(s.count),
                _format_ms(s.total_ms),
                _format_ms(s.avg_ms),
                _format_ms(s.median_ms),
                _format_ms(s.min_ms),
                _format_ms(s.max_ms),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col >= 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._stats_table.setItem(row, col, item)

    def _update_events_table(self, events: list[EventRecord]) -> None:
        # Newest first, capped at 200 rows so the dialog stays responsive.
        recent = list(reversed(events))[:200]
        self._events_table.setRowCount(len(recent))
        for row, ev in enumerate(recent):
            label = _CATEGORY_LABELS.get(ev.category, ev.category).strip()
            cells = [label, _format_ms(ev.duration_ms), ev.detail]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._events_table.setItem(row, col, item)

    # ----------------------------------------------------- clipboard export

    def _format_snapshot_as_text(self) -> str:
        events, stats, elapsed = recorder().snapshot()
        lines: list[str] = []
        lines.append(f"# Viewer perf snapshot (elapsed {elapsed:.2f} s)")
        lines.append("")
        lines.append("## Category summary")
        header = (
            f"{'category':40} {'count':>6} {'total':>10} {'avg':>10} "
            f"{'median':>10} {'min':>10} {'max':>10}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        ordered: list[str] = [c for c in _CATEGORY_ORDER if c in stats]
        for c in stats:
            if c not in ordered:
                ordered.append(c)
        for cat in ordered:
            s = stats[cat]
            lines.append(
                f"{cat:40} {s.count:>6d} {_format_ms(s.total_ms):>10} "
                f"{_format_ms(s.avg_ms):>10} {_format_ms(s.median_ms):>10} "
                f"{_format_ms(s.min_ms):>10} {_format_ms(s.max_ms):>10}"
            )
        lines.append("")
        lines.append(f"## Recent events ({min(len(events), 200)} of {len(events)})")
        for ev in list(reversed(events))[:200]:
            lines.append(
                f"{ev.category:40} {_format_ms(ev.duration_ms):>10}  {ev.detail}"
            )
        return "\n".join(lines)
