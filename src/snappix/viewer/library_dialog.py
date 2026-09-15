"""Modal dialog for managing the registered library roots (M02).

Lists the shared library roots (``common/shared_prefs.py`` ``library_roots``)
in a table with reorder / delete controls, so the user can prune stale entries
and set the order they appear in the ファイル → ライブラリ submenu.  It's a pure
editor over a ``list[str]`` of paths — the owning
:class:`~snappix.viewer.main_window.ViewerWindow` diffs the result against the
baseline it handed in and applies that diff (with the presented order) through
:func:`~snappix.common.shared_prefs.apply_library_roots` (項目#80/#82).

Existence is probed **once, off-thread** (a dead NAS ``is_dir()`` blocks for
tens of seconds): stale rows paint red as a hint but are never auto-removed —
a temporarily-unmounted share shouldn't silently drop its library entry.  This
mirrors :class:`~snappix.viewer.bookmark_dialog.BookmarkDialog`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import (
    align_header,
    current_tokens,
    empty_state_stack,
    hint_style,
    localize_buttons,
)
from ._runnable import GuardedSignals, dir_exists_probe
from .bookmark_dialog import NAME_COLUMN_WIDTH


class LibraryDialog(QDialog):
    """Reorder / delete the registered library roots (M02)."""

    def __init__(self, roots: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.library_dialog.title"))
        self.resize(560, 400)
        self.setModal(True)
        # Work on a copy so Cancel leaves the caller's list untouched.
        self._paths: list[str] = list(roots)
        # Async existence (see BookmarkDialog): unknown paints as existing.
        self._exists: dict[str, bool] = {}
        self._probe_signals = GuardedSignals(self)
        self._probe_signals.done.connect(self._on_existence_probed)
        self._build_ui()
        self._reload_rows()
        self._start_existence_probe()

    def _start_existence_probe(self) -> None:
        if not self._paths:
            return
        dir_exists_probe(self._paths, self._probe_signals)

    def _on_existence_probed(self, token: int, result: object) -> None:
        del token  # 1 回きりのプローブなので世代は見ない
        if not isinstance(result, dict):
            return  # ``run_detached`` は work の例外を None payload で報せる
        self._exists = dict(result)
        # 行は作り直さず色 / ツールチップだけを塗り替える（BookmarkDialog と
        # 同じ非破壊着地）。``_reload_rows`` の ``setItem`` は選択とスクロール
        # 位置を落とすので、数十秒後に着地しうるプローブで表を作り直さない。
        self._apply_existence_marks()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # UIレビュー 07-25 #85: このダイアログだけ表示名列が無く、ナビレール
        # /メニューのフォルダ名表示（末尾セグメント）と突き合わせにくかった
        # — 読み取り専用の「名前」列を追加する（並べ替え・削除等の対象は
        # 従来どおりフルパス基準・列は表示のみ）。
        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(
            [t("common.label.name"), t("common.label.path")]
        )
        # 見出しの揃え = 内容の揃え（どちらもテキスト = 左）
        # — UIレビュー 07-25 #97。
        align_header(self._table)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        # 名前列は内容に合わせて伸ばさない（BookmarkDialog と同じ作法）—
        # ``ResizeToContents`` だと長いフォルダ名 1 つでパス列と見出しが画面外
        # へ出て、実在マークの赤がどの行かを見るのに横スクロールが要る。
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setColumnWidth(0, NAME_COLUMN_WIDTH)
        self._table.itemSelectionChanged.connect(self._sync_controls)
        # 空のときは大きな空テーブルではなく中央寄せの案内に切り替える
        # （UIレビュー #15 — detail_window のタグ表と同じ見せ方に統一）。
        self._stack, self._empty_label = empty_state_stack(
            self._table, icon_name="folder"
        )
        outer.addWidget(self._stack, 1)

        btn_row = QHBoxLayout()
        self._up_btn = QPushButton(t("common.action.move_up"))
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(self._up_btn)
        self._down_btn = QPushButton(t("common.action.move_down"))
        self._down_btn.clicked.connect(lambda: self._move_selected(1))
        btn_row.addWidget(self._down_btn)
        self._delete_btn = QPushButton(t("common.action.delete"))
        self._delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # 追加はメニュー側にしかない — その導線をダイアログ内でも案内し、
        # 空のときは空である旨も添える（UIレビュー #28）。
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(hint_style())
        outer.addWidget(self._hint)

        buttons = localize_buttons(QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        ))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------- rows

    def _reload_rows(self) -> None:
        self._table.setRowCount(len(self._paths))
        for row, path in enumerate(self._paths):
            # 表示名は ``main_window._compute_library_bases``（ナビレールの
            # ライブラリ節 / パンくずのライブラリ基点）と同じ「末尾セグメント、
            # 無ければ生パス」規則 — 独自の派生ロジックを持つとレール表示と
            # 必ずドリフトする。
            # 注意: ファイル ▸ ライブラリ submenu（``_rebuild_library_menu``）
            # だけは**生パス全文**を項目名にしている（同名フォルダを複数
            # 登録したときに区別できるようにするため）。ここを「メニュー側と
            # 同じ規則」と書くと嘘になる — 参照先は上記 2 面。
            name_item = QTableWidgetItem(Path(path).name or path)
            path_item = QTableWidgetItem(path)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, path_item)
        self._apply_existence_marks()
        self._sync_controls()

    def _apply_existence_marks(self) -> None:
        """既存行の前景色 / ツールチップだけを ``_exists`` に合わせて更新する。

        実在しない項目は赤で目印を付けるが自動削除はしない（モジュール
        docstring）。実在は非同期プローブの結果（``_exists``）だけから引き、
        未判定のパス（キー無し）は実在扱い — GUI スレッドで行ごとに
        ``is_dir()`` を撃たないため。

        行を作り直さないので、着地が選択やスクロール位置を落とさない
        （``BookmarkDialog._apply_existence_marks`` と同じ非破壊着地）。
        """
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            path_item = self._table.item(row, 1)
            if name_item is None or path_item is None:
                continue
            path = path_item.text()
            missing = self._exists.get(path, True) is False
            brush = QBrush(QColor(current_tokens().danger)) if missing else QBrush()
            for item in (name_item, path_item):
                item.setForeground(brush)
            if missing:
                tip = t("viewer.library_dialog.folder_not_found", path=path)
                name_item.setToolTip(tip)
                path_item.setToolTip(tip)
            else:
                name_item.setToolTip("")
                path_item.setToolTip(path)

    def _sync_controls(self) -> None:
        # 未選択（や先頭/末尾）で押しても無反応なボタンは無効化して
        # フィードバック欠如を防ぐ（UIレビュー #14）。
        row = self._selected_row()
        n = len(self._paths)
        self._up_btn.setEnabled(row > 0)
        self._down_btn.setEnabled(0 <= row < n - 1)
        self._delete_btn.setEnabled(row >= 0)
        self._hint.setText(
            t("viewer.library_dialog.empty_hint") if n == 0
            else t("viewer.library_dialog.add_hint")
        )
        # 空: 案内は中央のスタックへ昇格し、下端ヒントは隠して二重表示を避ける
        # （UIレビュー #15）。項目あり: 表を出し下端に追加導線ヒントを残す。
        if n == 0:
            self._empty_label.setText(t("viewer.library_dialog.empty_hint"))
            self._stack.setCurrentWidget(self._empty_label)
        else:
            self._stack.setCurrentWidget(self._table)
        self._hint.setVisible(n > 0)

    def _selected_row(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _move_selected(self, delta: int) -> None:
        row = self._selected_row()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= len(self._paths):
            return
        self._paths[row], self._paths[target] = (
            self._paths[target], self._paths[row],
        )
        self._reload_rows()
        self._table.selectRow(target)

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self._paths.pop(row)
        self._reload_rows()
        if self._paths:
            self._table.selectRow(min(row, len(self._paths) - 1))

    # ------------------------------------------------------------- results

    def result_roots(self) -> list[str]:
        """The edited library-root list, in the (possibly reordered) order."""
        return list(self._paths)
