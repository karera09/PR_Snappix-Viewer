"""Modal dialog for managing the viewer's folder bookmarks (B-7).

Presents the saved bookmarks in a table (editable display name + read-only
path), with reorder / delete controls.  It's a pure editor over two plain
containers — a ``list[str]`` of paths (order-significant) and a
``dict[str, str]`` of path→display-name overrides — so the owning
:class:`~snappix.viewer.main_window.ViewerWindow` can round-trip them straight
into :class:`~snappix.viewer.state.ViewerState` on OK.

Rows whose path no longer exists on disk are shown in red so the user can spot
stale entries, but they are NOT auto-removed — deleting is left to the user's
judgement (a temporarily-unmounted NAS share shouldn't silently drop its
bookmarks).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
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

#: 「名前」列の開き幅（px）。パス列が ``Stretch`` で残りを取るので、名前列は
#: 内容に合わせて伸ばさない — 長い表示名 / フォルダ名 1 つでパス列が画面外へ
#: 押し出され、どの行が失われているかを見るのに横スクロールが要るため。
#: :class:`~snappix.viewer.library_dialog.LibraryDialog` と共有する（2 枚は
#: 同じ表の姉妹実装で、列幅の作法も同じであるべき）。
NAME_COLUMN_WIDTH = 160


class BookmarkDialog(QDialog):
    """Edit bookmark display names + order; delete stale / unwanted entries."""

    def __init__(
        self,
        bookmarks: list[str],
        names: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.bookmark_dialog.title"))
        self.resize(560, 420)
        self.setModal(True)
        # Work on copies so a Cancel leaves the caller's state untouched.
        self._paths: list[str] = list(bookmarks)
        self._names: dict[str, str] = dict(names)
        # Existence is probed ONCE off-thread (``_runnable.dir_exists_probe``):
        # a dead NAS ``is_dir()`` can block for tens of seconds, and
        # reorder/delete rebuild the rows repeatedly.  Until the probe lands,
        # treat every path as existing (no red) so the dialog opens instantly;
        # the async result then repaints stale rows.  A missing key here means
        # "unknown / not yet probed" and paints as existing.
        self._exists: dict[str, bool] = {}
        # Parent the relay to self so a pooled worker mid-``emit`` can't be
        # GC'd during teardown (exit-139), matching image_view/markdown_view (#8).
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
        # 行は作り直さず色/ツールチップだけを塗り替える（レビュー
        # 2026-07-31 #65）。``_reload_rows`` の ``setItem`` はモデルデータを
        # 差し替えるので、開いているセルエディタの未確定入力（``_commit_name_edits``
        # は確定済みテキストしか拾えない）が無言で消えていた。到達不能な
        # NAS では probe が数十秒ブロックしうる = ユーザーが名前を編集し始めた
        # 後に着地するのは現実的なタイミングなので、着地は非破壊にする。
        self._apply_existence_marks()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

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
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setColumnWidth(0, NAME_COLUMN_WIDTH)
        self._table.itemSelectionChanged.connect(self._sync_controls)
        # 空のときは大きな空テーブルではなく中央寄せの案内に切り替える
        # （UIレビュー #15 — detail_window のタグ表と同じ見せ方に統一）。
        self._stack, self._empty_label = empty_state_stack(
            self._table, icon_name="bookmark"
        )
        outer.addWidget(self._stack, 1)

        btn_row = QHBoxLayout()
        self._up_btn = QPushButton(t("common.action.move_up"))
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(self._up_btn)
        self._down_btn = QPushButton(t("common.action.move_down"))
        self._down_btn.clicked.connect(lambda: self._move_selected(1))
        btn_row.addWidget(self._down_btn)
        # 名前セルはダブルクリックで編集できるが手がかりが無い（UIレビュー #5）
        # — 選択行の編集をボタンからも明示的に開始できるようにする。
        self._rename_btn = QPushButton(t("common.action.rename"))
        self._rename_btn.clicked.connect(self._rename_selected)
        btn_row.addWidget(self._rename_btn)
        self._delete_btn = QPushButton(t("common.action.delete"))
        self._delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # 追加はメニュー側にしかない — その導線をダイアログ内でも案内し、
        # 空のときは空である旨も添える（UIレビュー #15, #28。メニューの
        # 「(ブックマークなし)」表示との整合）。
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
        """Rebuild the table from ``_paths`` / ``_names`` (order-preserving)."""
        # Persist any in-progress name edits before a reorder/delete rebuild so
        # they aren't lost when the rows are recreated.
        self._commit_name_edits()
        self._table.setRowCount(len(self._paths))
        for row, path in enumerate(self._paths):
            name_item = QTableWidgetItem(self._names.get(path, ""))
            name_item.setFlags(
                Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable
            )
            path_item = QTableWidgetItem(path)
            path_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, path_item)
        self._apply_existence_marks()
        self._sync_controls()

    def _apply_existence_marks(self) -> None:
        """既存行の前景色 / ツールチップだけを ``_exists`` に合わせて更新する。

        Flag stale (non-existent) entries in red so the user can prune them —
        but leave the decision to them (see module docstring).  Existence comes
        from the cached async probe (``_exists``); a path not yet probed
        (missing key) is treated as existing so we never block the GUI thread
        with a per-row ``is_dir()`` (see #167).

        行を作り直さないので、非同期プローブの着地が編集中のセルエディタを
        壊さない（レビュー 2026-07-31 #65）。
        """
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            path_item = self._table.item(row, 1)
            if name_item is None or path_item is None:
                continue
            path = path_item.text()
            missing = self._exists.get(path, True) is False
            brush = QBrush(QColor(current_tokens().danger)) if missing else QBrush()
            name_item.setForeground(brush)
            path_item.setForeground(brush)
            path_item.setToolTip(
                t("viewer.bookmark_dialog.folder_not_found", path=path)
                if missing
                else path
            )

    def _sync_controls(self) -> None:
        # 未選択（や先頭/末尾）で押しても無反応なボタンは無効化して
        # フィードバック欠如を防ぐ（UIレビュー #14）。
        row = self._selected_row()
        n = len(self._paths)
        self._up_btn.setEnabled(row > 0)
        self._down_btn.setEnabled(0 <= row < n - 1)
        self._rename_btn.setEnabled(row >= 0)
        self._delete_btn.setEnabled(row >= 0)
        self._hint.setText(
            t("viewer.bookmark_dialog.empty_hint") if n == 0
            else t("viewer.bookmark_dialog.add_hint")
        )
        # 空: 案内は中央のスタックへ昇格し、下端ヒントは隠して二重表示を避ける
        # （UIレビュー #15）。項目あり: 表を出し下端に追加導線ヒントを残す。
        if n == 0:
            self._empty_label.setText(t("viewer.bookmark_dialog.empty_hint"))
            self._stack.setCurrentWidget(self._empty_label)
        else:
            self._stack.setCurrentWidget(self._table)
        self._hint.setVisible(n > 0)

    def _commit_name_edits(self) -> None:
        """Fold the table's edited name cells back into ``_names``."""
        for row in range(self._table.rowCount()):
            path_item = self._table.item(row, 1)
            name_item = self._table.item(row, 0)
            if path_item is None or name_item is None:
                continue
            path = path_item.text()
            name = name_item.text().strip()
            if name:
                self._names[path] = name
            else:
                self._names.pop(path, None)

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
        self._commit_name_edits()
        self._paths[row], self._paths[target] = (
            self._paths[target], self._paths[row],
        )
        self._reload_rows()
        self._table.selectRow(target)

    def _rename_selected(self) -> None:
        """選択行の名前セルの編集を明示的に開始する（UIレビュー #5）。"""
        row = self._selected_row()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item is None:
            return
        self._table.setCurrentItem(item)
        self._table.editItem(item)

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self._commit_name_edits()
        path = self._paths.pop(row)
        self._names.pop(path, None)
        self._reload_rows()
        if self._paths:
            self._table.selectRow(min(row, len(self._paths) - 1))

    # ------------------------------------------------------------- results

    def accept(self) -> None:  # type: ignore[override]
        # Fold the final in-progress name edits before the caller reads results.
        self._commit_name_edits()
        super().accept()

    def result_bookmarks(self) -> list[str]:
        """The edited bookmark path list, in the (possibly reordered) order."""
        return list(self._paths)

    def result_names(self) -> dict[str, str]:
        """Path → display-name overrides, pruned of empty / orphaned entries."""
        return {p: n for p, n in self._names.items() if p in self._paths and n}
