"""ZIP / CBZ の中央ディレクトリプレビュー（``ZipView``）。"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Literal, assert_never, cast

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...common.archive import decode_zip_member_name
from ...common.i18n import t
from ...common.touch import enable_touch_scroll
from ...common.ui import FONT_SUBTITLE_PT, hint_style, set_icon
from .. import view_prefs
from .._runnable import GuardedStream, StreamJob, StreamOutcome
from ..edge_nav import navigate_at_edge, navigate_on_wheel
from ..view_prefs import (
    _format_bytes,
    _reveal_in_explorer,
    get_zip_preview_size_limit,
    open_with_default,
)
from ._shared import _popup_entry_menu


# Cap on how many ZIP members ZipView renders as tree rows.  The byte-size
# limit (``_ZIP_PREVIEW_SIZE_LIMIT``) bounds *bytes*, not *entries* — an
# asset-pack ZIP of tens of thousands of tiny files stays under it yet would
# freeze the GUI building that many QTreeWidgetItem rows (plus the sort /
# ResizeToContents recompute).  Totals still count every member; rows past
# this cap are omitted with a "…他 N 件" note.  Module-level so it's easy to
# tune (no settings UI — see the ZIP preview notes in docs/claude/viewer/content.md).
_ZIP_PREVIEW_MAX_ENTRIES = 5000


#: :func:`_read_zip_listing` の結末（``StreamOutcome.kind``）。``match`` の
#: ``case _:`` に ``assert_never`` を置くので、結末を足すと受け側の分岐漏れを
#: 型検査が指す。
_ZipReadKind = Literal["ready", "failed", "stat_failed", "too_large"]


def _read_zip_listing(
    path: Path, size_limit: int, job: StreamJob,
) -> StreamOutcome:
    """Stat + read the ZIP central directory off the GUI thread.

    Python's ``zipfile`` seeks to the EOCD record at the tail of the
    file and reads only the central-directory block — no extraction,
    no iteration over compressed data.  Even on NAS this is fast for
    files within ``ZIP_PREVIEW_SIZE_LIMIT``.

    The ``stat()`` size probe lives here too: a cold NAS round-trip on
    every ``.zip`` selection used to run synchronously on the GUI thread
    (FileInfoView / TextView had already moved theirs off-thread).  The
    size cap is captured at dispatch time so a settings change mid-flight
    doesn't re-gate a running task.

    結末は 4 種あるがシグナルは増やさない（:class:`StreamOutcome` の kind へ
    畳む）。stat が通って一覧の読みに入る瞬間だけは**途中報告**なので
    ``job.report(file_size)``＝ ``progress`` で流す — 「サイズは分かった、
    中身はこれから」を出せるのはここだけで、結末ではない。
    """
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return StreamOutcome("stat_failed", str(exc))
    if file_size > size_limit:
        return StreamOutcome("too_large", file_size)
    job.report(file_size)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
    except Exception as exc:
        return StreamOutcome("failed", str(exc))
    return StreamOutcome("ready", infos)


class _SortableItem(QTreeWidgetItem):
    """QTreeWidgetItem that sorts size / ratio / date columns by UserRole.

    Columns that store a raw numeric value under ``Qt.UserRole`` use that
    for comparison; other columns fall back to the display text so the
    filename sorts lexicographically as expected.
    """

    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        me = self.data(col, Qt.UserRole)
        them = other.data(col, Qt.UserRole)
        if me is not None and them is not None:
            try:
                return me < them  # type: ignore[operator]
            except TypeError:
                pass
        return self.text(col) < other.text(col)


class ZipView(QWidget):
    """Central-directory preview of a ZIP archive.

    Reads only the central directory (cheap — seeks to the tail, no
    extraction) in a background thread and displays file name /
    uncompressed size / compression ratio / modification date in a
    sortable tree.  Archives over ``ZIP_PREVIEW_SIZE_LIMIT`` show the
    file size but skip the listing.

    Wheel events on the tree emit ``navigate_requested`` when the tree's
    scroll bar is already at its limit, matching the behaviour of
    :class:`ImageView` and :class:`PdfView`.

    A 「開いて閲覧」 button makes the double-click drill-in reachable in
    one click; it emits ``open_requested`` and is disabled with an
    explanatory tooltip when the archive is over the drill size limit.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)
    open_requested = Signal(Path)           # 「開いて閲覧」 drill-in

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(6)

        self._header = QLabel()
        self._header.setStyleSheet(
            f"font-size: {FONT_SUBTITLE_PT}pt; font-weight: bold;"
        )
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        self._summary = QLabel()
        self._summary.setStyleSheet(hint_style())
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels([
            t("viewer.content_view.col_filename"),
            t("common.label.size"),
            t("viewer.content_view.col_ratio"),
            t("viewer.content_view.col_mtime"),
        ])
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setRootIsDecorated(False)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # The tree viewport swallows contextMenuEvent, so route its
        # right-click through a custom-menu signal to the shared entry menu.
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_tree_menu)
        self._tree.installEventFilter(self)
        enable_touch_scroll(self._tree)
        layout.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        # Primary 「開いて閲覧」 — the 1-click equivalent of the double-click
        # drill-in.  Disabled (with a reason tooltip) when the archive is
        # over the drill size limit; see _on_listing / _on_too_large.
        self._drill_btn = QPushButton(t("viewer.content_view.zip_open_button"))
        set_icon(self._drill_btn, "folder-open")
        self._drill_btn.clicked.connect(self._on_drill_clicked)
        btn_row.addWidget(self._drill_btn)
        self._open_btn = QPushButton(t("common.action.open_with_default"))
        set_icon(self._open_btn, "external-link")
        self._open_btn.clicked.connect(self._on_open_default)
        btn_row.addWidget(self._open_btn)
        self._reveal_btn = QPushButton(t("common.action.open_in_explorer"))
        set_icon(self._reveal_btn, "folder-output")  # OS 起動系の専用図像
        self._reveal_btn.clicked.connect(self._on_reveal)
        btn_row.addWidget(self._reveal_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._path: Path | None = None
        # 中央ディレクトリ読みの専用ストリーム（1 スレッド・世代ガード・
        # 窓じまいの有界ドレインつき）。結末 4 種は :class:`StreamOutcome` の
        # kind に畳み、途中報告（stat 完了）だけが ``progress`` へ乗る。
        self._read_stream = GuardedStream(self)
        self._read_stream.bind(self._on_read)
        self._read_stream.bind_progress(self._on_listing)

    def show_zip(self, path: Path) -> None:
        self._path = path
        self._tree.clear()
        self._tree.show()
        self._header.setText(path.name)
        # Size is unknown until stat lands, so gate the drill button off until
        # _on_listing (fits) or _on_too_large (over limit) resolves it.
        self._drill_btn.setEnabled(False)
        self._drill_btn.setToolTip("")
        # ``stat()`` runs inside the worker (see _read_zip_listing) so a cold
        # NAS round-trip never blocks the GUI thread on selection; the size /
        # too-large / listing states land through the stream's guard.
        self._summary.setText(t("common.status.loading"))
        limit = view_prefs._ZIP_PREVIEW_SIZE_LIMIT
        self._read_stream.submit_job(
            lambda job, p=path, lim=limit: _read_zip_listing(p, lim, job)
        )

    def _on_read(self, payload: object) -> None:
        """中央ディレクトリ読みの結末（4 種）を 1 箇所で捌く。"""
        if not isinstance(payload, StreamOutcome):
            return  # the worker raised
        kind = cast(_ZipReadKind, payload.kind)
        match kind:
            case "ready":
                self._on_ready(payload.value)
            case "failed":
                self._on_failed(cast(str, payload.value))
            case "stat_failed":
                self._on_stat_failed(cast(str, payload.value))
            case "too_large":
                self._on_too_large(cast(int, payload.value))
            case _:
                assert_never(kind)

    def _on_stat_failed(self, message: str) -> None:
        self._summary.setText(
            t("viewer.content_view.file_info_error", exc=message)
        )

    def _on_too_large(self, file_size: object) -> None:
        self._summary.setText(
            t("viewer.content_view.zip_too_large",
              size=_format_bytes(int(file_size)))  # type: ignore[arg-type]
        )
        self._tree.hide()
        # Over the drill limit — the fallback preview is all we can offer, so
        # keep 「開いて閲覧」 disabled and explain why on hover.
        self._drill_btn.setEnabled(False)
        self._drill_btn.setToolTip(
            t("viewer.content_view.zip_open_too_large_tooltip",
              limit_mb=get_zip_preview_size_limit() // (1024 * 1024))
        )

    def _on_listing(self, file_size: object) -> None:
        """stat が通り一覧の読みに入った（``progress`` の途中報告）。"""
        self._summary.setText(
            t("viewer.content_view.zip_loading",
              size=_format_bytes(int(file_size)))  # type: ignore[arg-type]
        )
        # Within the limit — the archive can be drilled into.
        self._drill_btn.setEnabled(True)
        self._drill_btn.setToolTip(t("viewer.content_view.zip_open_tooltip"))

    def clear(self) -> None:
        self._path = None
        self._read_stream.cancel()
        self._tree.clear()
        self._header.clear()
        self._summary.clear()
        self._drill_btn.setEnabled(False)
        self._drill_btn.setToolTip("")

    def _on_ready(self, infos: object) -> None:
        # Suspend painting + sorting while bulk-inserting.  A ZIP with tens
        # of thousands of small members (image asset packs are common) would
        # otherwise trigger a layout / ResizeToContents recompute per
        # addTopLevelItem and freeze the UI for seconds.  The entry cap below
        # bounds the item count so ResizeToContents stays cheap regardless.
        self._tree.setUpdatesEnabled(False)
        self._tree.setSortingEnabled(False)
        self._tree.clear()
        total_orig = 0
        total_comp = 0
        file_count = 0
        shown = 0
        items: list[_SortableItem] = []
        for info in infos:  # type: ignore[union-attr]
            if info.is_dir():
                continue
            total_orig += info.file_size
            total_comp += info.compress_size
            file_count += 1
            if shown >= _ZIP_PREVIEW_MAX_ENTRIES:
                # Keep totals accurate but stop building rows — the note
                # below tells the user how many were omitted.
                continue
            dt = info.date_time  # (year, month, day, h, m, s)
            date_str = (
                f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d}"
                f" {dt[3]:02d}:{dt[4]:02d}"
            )
            # Ratio as a float so the 圧縮率 column sorts numerically
            # (stored-only entries sink below with a -1.0 sentinel).
            ratio = (
                1 - info.compress_size / info.file_size
                if info.file_size > 0 else -1.0
            )
            ratio_str = f"{100 * ratio:.0f}%" if ratio >= 0 else "—"
            item = _SortableItem([
                decode_zip_member_name(info),
                _format_bytes(info.file_size),
                ratio_str,
                date_str,
            ])
            item.setData(1, Qt.ItemDataRole.UserRole, info.file_size)
            item.setData(2, Qt.ItemDataRole.UserRole, ratio)
            item.setData(3, Qt.ItemDataRole.UserRole, info.date_time)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
            shown += 1
        self._tree.addTopLevelItems(items)
        self._tree.setSortingEnabled(True)
        self._tree.setUpdatesEnabled(True)
        ratio_part = (
            t("viewer.content_view.zip_summary_ratio",
              comp=_format_bytes(total_comp))
            if total_comp < total_orig else ""
        )
        omitted = file_count - shown
        omitted_part = (
            t("viewer.content_view.zip_summary_omitted",
              shown=shown, omitted=omitted)
            if omitted > 0 else ""
        )
        self._summary.setText(
            t("viewer.content_view.zip_summary",
              count=file_count, total=_format_bytes(total_orig),
              ratio=ratio_part, omitted=omitted_part)
        )

    def _on_failed(self, message: str) -> None:
        self._summary.setText(
            t("viewer.content_view.zip_load_error", message=message)
        )
        self._tree.hide()

    def _on_open_default(self) -> None:
        # 共通ヘルパ経由 — 失敗時はステータス通知。
        if self._path:
            open_with_default(self._path, self)

    def _on_reveal(self) -> None:
        if self._path:
            _reveal_in_explorer(self._path, self)

    def _on_drill_clicked(self) -> None:
        # Request the same drill-in the double-click path performs.
        if self._path is not None:
            self.open_requested.emit(self._path)

    def _on_tree_menu(self, pos) -> None:
        # Right-click on a row → shared entry menu for the archive file.
        _popup_entry_menu(self, self._path, self._tree.viewport().mapToGlobal(pos))

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Right-click on the surrounding chrome (header / summary / buttons).
        _popup_entry_menu(self, self._path, event.globalPos())

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # type: ignore[override]
        # Wheel inside the tree: navigate only when its scrollbar is at
        # the travel limit; otherwise the tree scrolls normally.
        if (
            obj is self._tree
            and event.type() == QEvent.Type.Wheel
            and navigate_at_edge(
                self._tree.verticalScrollBar(), event,
                self.navigate_requested.emit,
            )
        ):
            return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Wheel on the surrounding chrome (header / summary / buttons):
        # nothing scrollable there, so navigate immediately.
        if navigate_on_wheel(event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)
