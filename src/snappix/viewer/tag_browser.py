"""Modeless "AI tag browser" dialog — lets the user explore/pick tags from
``tags.db`` without knowing them in advance.

This is a thin UI over ``TagIndex.top_tags`` (the AI plugin's tags.db reader,
``plugins/snappix_ai/engine/tag_db.py`` — the same materialised ``tag_stats``
table the advanced-search panel's autocomplete already reads). It never queries ``image_tags`` directly and never does its
own ``GROUP BY`` — :meth:`TagIndex.top_tags` already encapsulates the "only
read the materialised table, an unbuilt ``tag_stats`` means an empty list"
contract (see ``tag_db.py`` docstring for why the live fallback must not run
on an interactive path), so this dialog just renders whatever it returns.

The dialog doesn't touch the search panel itself — it only emits
:data:`tag_selected` / :data:`tag_excluded` signals with the chosen tag name.
Wiring those into the advanced-search include/exclude fields is a follow-up
phase's job (see the main_window / post_grid wiring), so this module has no
dependency on ``post_grid.py``.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui.timers import DebounceMode, Debouncer
from ..common.ui import (
    RADIUS_SM,
    align_header,
    current_tokens,
    demote_close_default,
    hint_style,
    localize_buttons,
    rgba,
)
# 条件チップ列の折返しは検索欄のチップ入力と同じ実装を再利用する
# （Qt にフローレイアウトが無いため — UIレビュー 07-25 #28）。
from .qimage_decode import enable_clear_button
from .tag_chips import FlowLayout

# How many tags to fetch/display at once. Mirrors the ``limit=500`` the task
# calls for — big enough to be useful, small enough to stay instant even
# though ``tag_stats`` is indexed and the query is already fast regardless.
_TOP_TAGS_LIMIT = 500

# Debounce delay for the filter box so fast typing doesn't re-query on every
# keystroke (same idea as the advanced-search tag completer's throttling).
_FILTER_DEBOUNCE_MS = 250


class _CountItem(QTableWidgetItem):
    """Table item that sorts numerically by an attached int, not by text.

    Mirrors ``detail_window.py``'s ``_ScoreItem`` pattern so the image-count
    column sorts as a number (500 > 42) instead of lexicographically.
    """

    def __init__(self, value: int) -> None:
        super().__init__(str(value))
        self._value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # noqa: D401
        if isinstance(other, _CountItem):
            return self._value < other._value
        return super().__lt__(other)


class _ReadOnlyChip(QLabel):
    """A non-interactive pill showing one accumulated search term.

    Mirrors ``tag_chips._TagChip``'s token-derived tinting (accent = include,
    danger = exclude) without any of its interaction (no ×, no drag) — this
    strip only *reports* the conditions the dialog has pushed into the search
    (UIレビュー 07-25 #28).
    """

    def __init__(self, term: str, *, exclude: bool) -> None:
        super().__init__(f"-{term}" if exclude else term)
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self._exclude = bool(exclude)
        self._apply_style()

    def _apply_style(self) -> None:
        tok = current_tokens()
        bg = rgba(tok.danger, 0.28) if self._exclude else rgba(tok.accent, 0.22)
        css = (
            f"QLabel {{ background: {bg}; border-radius: {RADIUS_SM}px;"
            " padding: 1px 6px; }"
        )
        # 同一文字列の再設定は StyleChange を無駄撃ちするだけなので避ける。
        if css != self.styleSheet():
            self.setStyleSheet(css)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        # UIレビュー07-25 追修: トークン由来の色を作成時に焼き込んでいたため、
        # このダイアログ（セッション中使い回される）はテーマを切り替えても
        # 古いパレットのチップを出し続けていた。``_TagChip`` と同じ再着色を
        # パレット変更イベントで行う（``StyleChange`` は setStyleSheet 自身が
        # 発火させるので拾わない — 無限再帰になる）。
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.ThemeChange,
        ):
            self._apply_style()
        super().changeEvent(event)


class TagBrowserDialog(QDialog):
    """Modeless browser over ``tag_stats`` for picking search tags.

    ``tag_index`` must be a live ``TagIndex`` (the AI plugin's tags.db reader,
    ``plugins/snappix_ai/engine/tag_db.py`` — injected duck-typed, never
    imported here) — callers are expected to only construct this dialog when
    ``tags.db`` is present (same convention as other tag-search UI in the
    viewer); passing ``None`` isn't supported.
    """

    #: Emitted when the user wants *tag* added to the include (search) side.
    tag_selected = Signal(str)
    #: Emitted when the user wants *tag* added to the exclude side.
    tag_excluded = Signal(str)

    def __init__(self, tag_index, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.tag_browser.window_title"))
        self.resize(480, 640)
        self.setModal(False)
        self._tag_index = tag_index
        self._rows: list[tuple[str, int]] = []
        # 追加先のタグ入力欄（ポップオーバー）が閉じていて反映が見えないので、
        # 積算した条件をダイアログ内で読み取れるようにする
        # (UIレビュー 07-25 #28)。順序を保った重複なしの積算リスト。
        self._include_terms: list[str] = []
        self._exclude_terms: list[str] = []
        # Whether the default (count-descending) sort has been applied.  After
        # the first population, reloads keep the user's chosen header sort
        # instead of snapping back — part of the scroll-stability fix.
        self._sorted_once = False

        self._debounce = Debouncer(
            self, _FILTER_DEBOUNCE_MS, self._reload, mode=DebounceMode.TRAILING
        )

        self._build_ui()
        self._reload()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(t("viewer.common.filter_tags_placeholder"))
        enable_clear_button(self._filter_edit)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        outer.addWidget(self._filter_edit)

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(
            [t("common.label.tag"), t("viewer.tag_browser.col_image_count")]
        )
        # 見出しの揃えは内容の揃えに合わせる (UIレビュー 07-25 #97) —
        # 画像数（列1）は _CountItem が右揃えなので見出しも右。
        align_header(self._table, right=(1,))
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSortingEnabled(True)
        self._table.itemDoubleClicked.connect(self._on_row_double_clicked)
        # 未選択時に押しても無反応な 2 ボタンを無効化してフィードバック欠如を
        # 防ぐ（UIレビュー #4）。
        self._table.itemSelectionChanged.connect(self._sync_controls)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        # Qt 既定の並び順インジケータは昇順=▼ / 降順=▲ と描くため、既定の
        # 「画像数 降順」が ▲ に見えていた (UIレビュー 07-25 #120)。既定描画は
        # 止め、見出し文字列側に自前の矢印を出して明示制御する。
        hdr.setSortIndicatorShown(False)
        hdr.sortIndicatorChanged.connect(self._sync_sort_indicator)
        outer.addWidget(self._table, 1)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(hint_style())
        outer.addWidget(self._status_label)

        # 現在の検索条件（読み取り専用チップ列）— このダイアログから追加した
        # 積算状態がここで完結する (UIレビュー 07-25 #28)。
        terms_row = QHBoxLayout()
        terms_row.setSpacing(6)
        terms_caption = QLabel(t("viewer.tag_browser.current_terms_label"))
        terms_caption.setStyleSheet(hint_style())
        terms_row.addWidget(terms_caption, 0, Qt.AlignTop)
        self._terms_host = QWidget()
        self._terms_layout = FlowLayout(self._terms_host, spacing=4)
        terms_row.addWidget(self._terms_host, 1)
        outer.addLayout(terms_row)

        button_row = QHBoxLayout()
        self._add_btn = QPushButton(t("viewer.tag_browser.add_to_search"))
        self._add_btn.clicked.connect(self._emit_selected)
        button_row.addWidget(self._add_btn)
        self._exclude_btn = QPushButton(t("viewer.tag_browser.add_to_exclude"))
        self._exclude_btn.clicked.connect(self._emit_excluded)
        button_row.addWidget(self._exclude_btn)
        button_row.addStretch(1)
        buttons = demote_close_default(
            localize_buttons(QDialogButtonBox(QDialogButtonBox.Close))
        )
        # The Close button carries RejectRole, so clicking it fires ``rejected``
        # — connecting only that avoids calling ``close()`` twice per click
        # (#181).
        buttons.rejected.connect(self.close)
        button_row.addWidget(buttons)
        outer.addLayout(button_row)

        # 初期状態は無選択なので 2 ボタンは無効から始める（UIレビュー #4）。
        self._sync_controls()
        self._rebuild_term_chips()

    # --------------------------------------------------------------- helpers

    def _on_filter_changed(self, _text: str) -> None:
        self._debounce.trigger()

    def _reload(self) -> None:
        prefix = self._filter_edit.text().strip()
        rows = self._tag_index.top_tags(limit=_TOP_TAGS_LIMIT, prefix=prefix)
        self._rows = rows
        self._populate(rows)

    def _populate(self, rows: list[tuple[str, int]]) -> None:
        # Preserve the scroll position across a filter-debounced reload.  The
        # old code re-ran ``sortItems`` on every populate, which snaps the
        # scrollbar back to the top — with ``ResizeToContents`` on the count
        # column re-measuring and the sort indicator re-applying, the viewport
        # ended up thrashing on every keystroke ("scroll runs away").  We now
        # sort ONCE (the first population sets the default count-descending
        # order; later reloads keep whatever column/order the user clicked) and
        # restore the prior scroll offset after rebuilding the rows.
        scroll = self._table.verticalScrollBar().value()
        sorting_was_on = self._table.isSortingEnabled()
        # Disable sorting during the bulk fill so Qt doesn't re-sort after every
        # setItem (that per-insert churn is another source of visual jumping).
        self._table.setSortingEnabled(False)
        self._table.setRowCount(0)
        self._table.setRowCount(len(rows))
        for r, (tag, count) in enumerate(rows):
            self._table.setItem(r, 0, QTableWidgetItem(tag))
            self._table.setItem(r, 1, _CountItem(count))
        self._table.setSortingEnabled(sorting_was_on or not self._sorted_once)
        # ``setSortingEnabled`` は内部で ``setSortIndicatorShown(True)`` を呼ぶ
        # ので、明示制御 (UIレビュー 07-25 #120) をここで掛け直す。
        self._table.horizontalHeader().setSortIndicatorShown(False)
        if not self._sorted_once:
            # First population only: establish the default (image count desc)
            # and remember the user's header choice from here on.
            self._table.sortItems(1, Qt.DescendingOrder)
            self._sorted_once = True
        # Restore the scroll offset (clamped to the new row count by Qt).
        self._table.verticalScrollBar().setValue(scroll)
        self._update_status(len(rows))
        self._sync_controls()

    def _update_status(self, shown: int) -> None:
        if not shown:
            # 0 件には 2 つの理由がある（UIレビュー 09-11 N-102）: 統計そのもの
            # が無いのか、絞り込み語に一致しなかったのか。``_reload`` は
            # ``_filter_edit`` の prefix を ``top_tags`` に渡すので、後者は
            # 統計が正常でも日常的に起こる — 一律に「タグ統計がありません。」と
            # 出すのは誤案内だった。出し分けは ``PostGrid._empty_state_kind`` の
            # curation_empty / curation_filtered と同型。
            prefix = self._filter_edit.text().strip()
            self._status_label.setText(
                t("viewer.tag_browser.no_filter_match", query=prefix) if prefix
                else t("viewer.tag_browser.no_tag_stats")
            )
            return
        if shown >= _TOP_TAGS_LIMIT:
            count_text = t("viewer.tag_browser.showing_n_top", n=shown)
        else:
            count_text = t("viewer.tag_browser.showing_n", n=shown)
        # 追加導線の常時ヒントを件数の隣に添える（UIレビュー #4 — 初見ユーザーが
        # 行き止まりに入るのを防ぐ）。
        self._status_label.setText(
            f"{count_text} ・ {t('viewer.tag_browser.select_hint')}"
        )

    def _sync_controls(self) -> None:
        # 未選択で「検索に追加 / 除外に追加」を押しても無反応なのを防ぐ
        # （UIレビュー #4）。
        has_selection = bool(self._table.selectionModel().selectedRows())
        self._add_btn.setEnabled(has_selection)
        self._exclude_btn.setEnabled(has_selection)

    def _selected_tags(self) -> list[str]:
        rows = sorted(
            idx.row() for idx in self._table.selectionModel().selectedRows()
        )
        tags = []
        for r in rows:
            item = self._table.item(r, 0)
            if item is not None:
                tags.append(item.text())
        return tags

    # ---------------------------------------------- current-condition chips

    def set_current_terms(
        self, include: list[str] | None, exclude: list[str] | None,
    ) -> None:
        """Seed / replace the read-only condition strip (UIレビュー 07-25 #28).

        Optional: the dialog keeps its own accumulation of everything it has
        pushed out, so the strip is meaningful even when nothing calls this.
        An owner that knows the live search state can push it in here.
        """
        self._include_terms = list(dict.fromkeys(include or []))
        self._exclude_terms = list(dict.fromkeys(exclude or []))
        self._rebuild_term_chips()

    def _remember_term(self, tag: str, *, exclude: bool) -> None:
        bucket = self._exclude_terms if exclude else self._include_terms
        other = self._include_terms if exclude else self._exclude_terms
        if tag in other:
            other.remove(tag)  # 反対側へ移した扱い（AND 条件の取り違えを防ぐ）
        if tag not in bucket:
            bucket.append(tag)
        self._rebuild_term_chips()

    def _rebuild_term_chips(self) -> None:
        while self._terms_layout.count():
            item = self._terms_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if not self._include_terms and not self._exclude_terms:
            empty = QLabel(t("viewer.tag_browser.current_terms_empty"))
            empty.setStyleSheet(hint_style())
            self._terms_layout.addWidget(empty)
        else:
            for tag in self._include_terms:
                self._terms_layout.addWidget(_ReadOnlyChip(tag, exclude=False))
            for tag in self._exclude_terms:
                self._terms_layout.addWidget(_ReadOnlyChip(tag, exclude=True))
        self._terms_host.updateGeometry()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # 再オープン時にもチップを貼り直す（UIレビュー07-25 追修 — このダイアログは
        # セッション中使い回されるので、閉じている間のテーマ変更に対する保険）。
        for i in range(self._terms_layout.count()):
            item = self._terms_layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if isinstance(widget, _ReadOnlyChip):
                widget._apply_style()

    # ------------------------------------------------------ sort indicator

    def _sync_sort_indicator(self, section: int, order) -> None:
        """並び順の矢印を見出し文字列で明示する (UIレビュー 07-25 #120).

        Qt 既定描画は昇順=▼ / 降順=▲ と逆に見えるため ``setSortIndicatorShown``
        を切り、降順は ▼・昇順は ▲ を見出しに添える。
        """
        base = [t("common.label.tag"), t("viewer.tag_browser.col_image_count")]
        arrow = "▼" if order == Qt.DescendingOrder else "▲"
        labels = [
            f"{text} {arrow}" if i == section else text
            for i, text in enumerate(base)
        ]
        self._table.setHorizontalHeaderLabels(labels)
        # setHorizontalHeaderLabels は見出しアイテムごと作り直すため、
        # 揃えの指定もここで貼り直す (UIレビュー 07-25 #97)。
        align_header(self._table, right=(1,))

    # ------------------------------------------------------------- signals

    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        # Double-click means "add this row's tag", regardless of which column
        # was clicked — read the tag name straight from column 0 of that row
        # rather than relying on the (already-updated, but let's be explicit)
        # selection state.
        name_item = self._table.item(item.row(), 0)
        if name_item is not None:
            self._remember_term(name_item.text(), exclude=False)
            self.tag_selected.emit(name_item.text())

    def _emit_selected(self) -> None:
        for tag in self._selected_tags():
            self._remember_term(tag, exclude=False)
            self.tag_selected.emit(tag)

    def _emit_excluded(self) -> None:
        for tag in self._selected_tags():
            self._remember_term(tag, exclude=True)
            self.tag_excluded.emit(tag)


__all__ = ["TagBrowserDialog"]
