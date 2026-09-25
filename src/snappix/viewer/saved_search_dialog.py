"""Modal dialog for managing saved searches / smart folders (M03).

Lists the saved searches (``ViewerState.saved_searches``) in a table with an
editable name column plus reorder / delete controls.  Each row's serialised
query payload rides along untouched by index — only the ``name`` field is
edited here — so the owning
:class:`~snappix.viewer.main_window.ViewerWindow` round-trips the result
straight back into state on OK.

Pure editor over ``list[dict]`` (no filesystem I/O — saved searches are query
payloads, never paths), so it needs none of the async existence probing the
bookmark / library dialogs use.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
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
    empty_state_stack,
    hint_style,
    localize_buttons,
)
from .post_grid import describe_search_payload, saved_search_tooltip


class SavedSearchDialog(QDialog):
    """Rename / reorder / delete saved searches (M03)."""

    def __init__(
        self, searches: list[dict], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.saved_search_dialog.title"))
        self.resize(480, 380)
        self.setModal(True)
        # Deep-ish copy: copy each entry dict so editing a name / reordering
        # never mutates the caller's state on Cancel.
        self._entries: list[dict] = [dict(e) for e in searches]
        self._build_ui()
        self._reload_rows()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        self._table = QTableWidget()
        # 2 列目「条件」: 保存した検索が名前だけだと、中身も適用範囲も
        # 見えない（別フォルダで 0 件になった理由も説明されない）。
        # 条件チップと同じ語彙の要約を常時列に出す。
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels([
            t("common.label.name"),
            t("viewer.saved_search_dialog.col_query"),
        ])
        # 見出しの揃え = 内容の揃え（テキスト = 左）。
        align_header(self._table)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.itemSelectionChanged.connect(self._sync_controls)
        # 空のときは大きな空テーブルではなく中央寄せの案内に切り替える
        # （detail_window のタグ表と同じ見せ方）。
        self._stack, self._empty_label = empty_state_stack(
            self._table, icon_name="search"
        )
        outer.addWidget(self._stack, 1)

        btn_row = QHBoxLayout()
        self._up_btn = QPushButton(t("common.action.move_up"))
        self._up_btn.clicked.connect(lambda: self._move_selected(-1))
        btn_row.addWidget(self._up_btn)
        self._down_btn = QPushButton(t("common.action.move_down"))
        self._down_btn.clicked.connect(lambda: self._move_selected(1))
        btn_row.addWidget(self._down_btn)
        # 名前セルはダブルクリックで編集できるが手がかりが無い
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
        # 空のときは空である旨も添える。
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
        # NB: do NOT commit name edits here.  ``_reload_rows`` runs *after* a
        # caller has already mutated ``_entries`` (a swap in ``_move_selected``
        # or a ``pop`` in ``_delete_selected``), while the table still holds the
        # PRE-mutation rows.  Committing now would fold those stale row texts
        # back into the reordered/shrunk ``_entries`` by index, pairing a name
        # with the wrong query.  Every mutating caller already commits
        # *before* mutating, and ``accept`` commits directly, so no edit is lost.
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            # 行ツールチップは「<条件サマリ>／現在のフォルダを起点に適用」
            # （メニュー・ナビレールと同じ 1 本の文面）。
            tip = saved_search_tooltip(entry)
            item = QTableWidgetItem(str(entry.get("name") or ""))
            item.setFlags(
                Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable
            )
            item.setToolTip(tip)
            self._table.setItem(row, 0, item)
            query = QTableWidgetItem(describe_search_payload(entry))
            # 条件列は表示専用（編集できるのは名前だけ — ペイロードは
            # インデックスでそのまま持ち回る契約）。
            query.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            query.setToolTip(tip)
            self._table.setItem(row, 1, query)
        self._sync_controls()

    def _sync_controls(self) -> None:
        # 未選択（や先頭/末尾）で押しても無反応なボタンは無効化して
        # フィードバック欠如を防ぐ。
        row = self._selected_row()
        n = len(self._entries)
        self._up_btn.setEnabled(row > 0)
        self._down_btn.setEnabled(0 <= row < n - 1)
        self._rename_btn.setEnabled(row >= 0)
        self._delete_btn.setEnabled(row >= 0)
        self._hint.setText(
            t("viewer.saved_search_dialog.empty_hint") if n == 0
            else t("viewer.saved_search_dialog.add_hint")
        )
        # 空: 案内は中央のスタックへ昇格し、下端ヒントは隠して二重表示を避ける
        # 項目あり: 表を出し下端に追加導線ヒントを残す。
        if n == 0:
            self._empty_label.setText(t("viewer.saved_search_dialog.empty_hint"))
            self._stack.setCurrentWidget(self._empty_label)
        else:
            self._stack.setCurrentWidget(self._table)
        self._hint.setVisible(n > 0)

    def _commit_name_edits(self) -> None:
        """Fold the table's edited name cells back into the entry dicts."""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None or row >= len(self._entries):
                continue
            name = item.text().strip()
            if name:
                self._entries[row]["name"] = name

    def _selected_row(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _move_selected(self, delta: int) -> None:
        row = self._selected_row()
        if row < 0:
            return
        target = row + delta
        if target < 0 or target >= len(self._entries):
            return
        self._commit_name_edits()
        self._entries[row], self._entries[target] = (
            self._entries[target], self._entries[row],
        )
        self._reload_rows()
        self._table.selectRow(target)

    def _rename_selected(self) -> None:
        """選択行の名前セルの編集を明示的に開始する。"""
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
        self._entries.pop(row)
        self._reload_rows()
        if self._entries:
            self._table.selectRow(min(row, len(self._entries) - 1))

    # ------------------------------------------------------------- results

    def accept(self) -> None:  # type: ignore[override]
        self._commit_name_edits()
        super().accept()

    def result_searches(self) -> list[dict]:
        """The edited saved-search entries, in the (possibly reordered) order."""
        return [dict(e) for e in self._entries]
