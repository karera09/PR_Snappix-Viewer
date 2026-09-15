"""Left pane — the always-on navigation rail (layout redesign 2026-07, Phase 2-3).

The left splitter seat, previously an empty placeholder, is now a thin
:class:`NavRail`: three stacked sections, each under a
:class:`~snappix.common.ui.PanelHeader`, that surface the viewer's durable
navigation targets as single-click lists instead of only living behind menus /
dialogs:

* **ライブラリ** — the registered library roots (plus the default library,
  labelled 「ライブラリ」 like the breadcrumb).  A click re-roots there; the
  entry matching the current root is drawn as "current" (see below).
* **スター・あとで見る** — the cross-library 「スター付き一覧」 /
  「あとで見る一覧」 overlays (UIレビュー 07-25 #57) plus one row per ユーザータグ
  (UIレビュー 2026-08-28 N-71).  A click enters the matching curation view; the
  active one is drawn as "current" while it lasts.  Each row carries its
  「印を付けた件数」 and the section's header badge is suppressed (N-117).
* **ブックマーク** — the saved bookmarks.  A click navigates.
* **保存した検索** — the saved smart-search payloads.  A click re-applies the
  search against the current root.

Each section's ``⋯`` overflow button opens the corresponding management dialog
(the same one the menus already open — the rail never re-implements it) and
carries a tooltip naming that dialog (UIレビュー 07-25 #79); the existing menu
routes stay in place.

Keyboard parity (UIレビュー 07-25 #7): rows activate on ``itemActivated`` (Enter
*and* double-click) as well as ``itemClicked``, and the "current location" is a
*painted* state (bold + a left accent bar via :class:`_RailItemDelegate`) rather
than the list's selection — so moving the cursor with ↑↓ no longer turns the
current-root indicator into a lie.  A library that merely *contains* the current
root (after drilling down) gets the same bar at a fainter alpha, the second tier
of the two-step emphasis (UIレビュー 07-25 #79).

``NavRail`` is a dumb view: it owns no state and reads nothing off disk.  The
window feeds it already-collected lists via :meth:`set_libraries` /
:meth:`set_curation` / :meth:`set_bookmarks` / :meth:`set_saved_searches`
(called from the same ``_rebuild_*`` points that refresh the menus, so the rail
follows every add / remove / manage without polling) and emits click / manage
signals back for the window to act on.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import PanelHeader, current_tokens, empty_state_stack
from .breadcrumb import pick_library_base

# Same role slot everywhere: each list item stores its click payload here
# (a library-root path string, a bookmark path string, or a saved-search index).
_PAYLOAD_ROLE = Qt.UserRole

# 行の右端に淡色で添える数（現状はキュレーション行の「印を付けた件数」だけ）。
# ラベル文字列に埋め込むと、他 3 節が右寄せ muted で数を見せる書式と割れる
# （UIレビュー 2026-09-11 N-120）。``None`` = 数を持たない行。
_COUNT_ROLE = Qt.UserRole + 1

# 件数と本文 / 右端の間隔（px）。
_COUNT_GAP = 6

#: 1 行ぶんの素材 ``(label, payload, tooltip, count)``。*count* を**タプルに
#: 含める**のが要点: :meth:`_RailSection.set_items` の no-op ガードは行タプル
#: の同値比較なので、件数を外に出すと「件数だけ変わったときに更新されない」
#: 穴が開く（N-120）。
_Row = tuple[str, object, "str | None", "int | None"]

# A section's list/empty-hint area is capped to a *content-sized* height
# (row height x item count, up to this many visible rows) instead of being
# left to stretch — otherwise a rail with few entries per section spreads
# them out with large gaps (the bug this module fixes).  Past the cap the
# list scrolls internally like any other list.  The empty-hint height is a
# fixed, compact allowance (roughly two lines of hint text) so a section
# with nothing in it still reads as a small guidance strip, not a stretched
# empty box.
_MAX_VISIBLE_ROWS = 6
_EMPTY_STATE_HEIGHT = 56

# "Current location" accent bar (UIレビュー 07-25 #7 / #79).  Two tiers: the
# exact current row is opaque + bold, an ancestor of the current root is a
# faint bar with normal weight.  Colour comes from the accent token — never a
# literal (docs/claude/design.md).
_ACCENT_BAR_WIDTH = 3
_ACCENT_BAR_INSET = 3
_ANCESTOR_BAR_ALPHA = 90

# An ``itemActivated`` that trails the ``itemClicked`` of the very same
# double-click must not fire the navigation twice.  Anything slower than this
# is a genuine second activation (and re-navigating is harmless anyway).
_ACTIVATE_DEDUPE_S = 0.3


def bookmark_label(raw: str, names: dict[str, str]) -> str:
    """ブックマーク 1 件の表示名（レールとメニューが共有する 1 実装）.

    優先順は「管理ダイアログで付けた表示名 → フォルダ名 → 生パス」。追加時
    （``ViewerWindow._add_current_bookmark``）は表示名を持たないので、生パスの
    ままだとレールの狭い列に ``D:\\lib\\creator\\2024-05-01_Title`` が省略記号
    付きで並ぶ（保存した検索は無題の既定名を持つのに、ブックマークだけ素通し
    だった — N-126）。同名フォルダが並ぶ場合はツールチップのフルパスで
    区別する。ドライブ直下（``Path("D:/").name == ""``）は生パスへ戻す。
    """
    return names.get(raw) or Path(raw).name or raw


class _RailItemDelegate(QStyledItemDelegate):
    """Paints the rail's "current location" emphasis (UIレビュー 07-25 #7).

    The list's *selection* is now purely a keyboard cursor; where the user
    actually **is** is drawn by this delegate from the owning section's
    ``current_payload`` / ``ancestor_payload``, so ↑↓ cannot desynchronise the
    two.  Tier 1 (current) = bold + opaque accent bar; tier 2 (ancestor of the
    current root, i.e. the library we drilled down from) = faint accent bar
    only (UIレビュー 07-25 #79).
    """

    def __init__(self, section: "_RailSection") -> None:
        super().__init__(section)
        self._section = section

    def initStyleOption(self, option, index) -> None:  # noqa: N802 (Qt API)
        super().initStyleOption(option, index)
        if self._section.emphasis_for(index.data(_PAYLOAD_ROLE)) == "current":
            # ``option.font`` は値プロパティ — 取り出して書き戻す。
            font = option.font
            font.setBold(True)
            option.font = font

    def paint(self, painter, option, index) -> None:
        count = index.data(_COUNT_ROLE)
        if count is None:
            super().paint(painter, option, index)
        else:
            self._paint_with_count(painter, option, index, str(count))
        tier = self._section.emphasis_for(index.data(_PAYLOAD_ROLE))
        if tier is None:
            return
        colour = QColor(current_tokens().accent)
        if tier == "ancestor":
            colour.setAlpha(_ANCESTOR_BAR_ALPHA)
        rect = option.rect
        painter.save()
        painter.fillRect(
            rect.left(),
            rect.top() + _ACCENT_BAR_INSET,
            _ACCENT_BAR_WIDTH,
            max(0, rect.height() - 2 * _ACCENT_BAR_INSET),
            colour,
        )
        painter.restore()

    def _paint_with_count(self, painter, option, index, text: str) -> None:
        """本文 + 右端の淡色な数（N-120）.

        本文は**スタイルに描かせる**（``CE_ItemViewItem``）。自前で
        ``SE_ItemViewItemText`` の矩形へ直接描いていたころは、スタイルが本文を
        描くときにさらに内側へ寄せる textMargin を写していなかったので、件数を
        持つ節の行頭だけが他の節より左へずれ、フォーカス枠も出なかった
        （``QCommonStyle`` の item 描画は最後にフォーカス枠を描く）。

        件数ぶんの幅は**ラベルを先に省略して**空ける — 背景（選択 / ホバー）・
        選択時のペン・フォーカス枠はスタイルの描画のまま残るので、行全体に
        背景が乗り、右端に地の色の帯も残らない。件数の色は選択時だけ
        ``HighlightedText``（淡色のままだとハイライト上でほぼ読めない）、
        通常時は ``common/ui`` のトークン（直書き禁止）。
        """
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()

        metrics = opt.fontMetrics
        count_w = metrics.horizontalAdvance(text) + _COUNT_GAP
        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, opt, widget
        )
        margin = style.pixelMetric(QStyle.PM_FocusFrameHMargin, opt, widget) + 1
        budget = max(0, text_rect.width() - count_w - 2 * margin)
        opt.text = metrics.elidedText(opt.text, Qt.ElideRight, budget)
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.State_Selected)
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(
            opt.palette.color(QPalette.HighlightedText)
            if selected
            else QColor(current_tokens().text_muted)
        )
        painter.drawText(
            text_rect.adjusted(0, 0, -margin, 0),
            Qt.AlignRight | Qt.AlignVCenter,
            text,
        )
        painter.restore()


class _RailList(QListWidget):
    """レール節のリスト — ←→ をウィンドウの ←→ ショートカットに渡さない。

    UIレビュー 2026-08-28 N-90: ウィンドウレベルの ←→ は
    ``main_window._step_or_navigate`` へ入り、「最大化中か / フォーカスが
    中央ペイン配下か」以外はすべて **無条件で左グリッドの選択を動かす**。
    そのためレールにフォーカスがある状態で ←→ を押すと、見てもいないグリッド
    の選択と中央プレビューが動いていた。

    両ペインのグリッド（``GalleryView.event``）は既に ``ShortcutOverride`` を
    accept してこのすり抜けを塞いでおり、レールの ``QListWidget`` にだけ同じ
    防御が無かった（片側欠落）。accept すると Qt はキーを通常の
    ``keyPressEvent`` としてこのリストへ届けるので、リスト自身のカーソル移動
    として自然に消化される。
    """

    def event(self, ev) -> bool:  # type: ignore[override]
        if ev.type() == QEvent.ShortcutOverride and ev.key() in (
            Qt.Key_Left, Qt.Key_Right,
        ):
            ev.accept()
            return True
        return super().event(ev)


class _RailSection(QWidget):
    """One rail section: a :class:`PanelHeader` + a flat single-click list.

    Empty lists swap to a centred hint label (via ``empty_state_stack``) so a
    section with nothing in it reads as guidance, not a broken empty box.  The
    caller wires :attr:`item_clicked` (payload of the clicked *or* keyboard-
    activated row) and the header's ``⋯`` overflow (:attr:`manage_clicked`).

    *manage_tooltip* names the dialog the ``⋯`` opens (UIレビュー 07-25 #79);
    passing ``None`` omits the overflow button entirely (a section with nothing
    to manage).
    """

    item_clicked = Signal(object)
    manage_clicked = Signal()

    def __init__(
        self,
        title: str,
        empty_hint: str,
        manage_tooltip: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Payloads driving the delegate's two-tier "current location" paint
        # (UIレビュー 07-25 #7/#79) — deliberately *not* the list selection.
        self._current_payload: object = None
        self._ancestor_payload: object = None
        self._last_emit: tuple[object, float] = (None, 0.0)
        # 直近に流し込んだ行（``set_items`` の同内容 no-op 判定用 — 追修 #7）。
        # ``None`` = 未投入（初回は空リストでも必ず適用してヒント面へ切り替える）。
        self._rows: list[_Row] | None = None
        #: 直近の ``show_count``（同内容 no-op 判定に含める — バッジだけを
        #: 落とす呼び出しが黙って無視されないように）。
        self._show_count = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = PanelHeader(title)
        if manage_tooltip is not None:
            # (UIレビュー 08-28 N-24) 歯車であって ⋯ ではない — このボタンは
            # メニューですらなく管理ダイアログを開く**単一アクション**なので、
            # 「⋯ = そのペインの表示オプション」という図像の意味を借りない。
            # (UIレビュー 07-25 #79) 右一覧の ⋯ にはあるツールチップが
            # レールだけ無かった — どのダイアログが開くかを名指しする。
            manage = self._header.action_button("settings")
            manage.setToolTip(manage_tooltip)
            manage.clicked.connect(self.manage_clicked)
        layout.addWidget(self._header)

        self._list = _RailList()
        self._list.setObjectName("navRailList")
        self._list.setFrameShape(QListWidget.NoFrame)
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setItemDelegate(_RailItemDelegate(self))
        self._list.itemClicked.connect(self._on_item_clicked)
        # (UIレビュー 07-25 #7) Enter / ダブルクリックの双方が発火する
        # itemActivated をクリックと同じスロットへ — レールは Tab で到達
        # できるのにキーボードでは開けない袋小路だった。
        self._list.itemActivated.connect(self._on_item_clicked)
        self._stack, self._empty_label = empty_state_stack(self._list)
        self._empty_label.setText(empty_hint)
        layout.addWidget(self._stack, 1)

    # ------------------------------------------------------------------ API

    def set_items(
        self,
        rows: list[_Row],
        *,
        show_count: bool = True,
    ) -> None:
        """Populate the list from ``(label, payload, tooltip, count)`` *rows*.

        *count* (``None`` for every section but キュレーション) is painted by
        :class:`_RailItemDelegate` at the row's right edge in ``text_muted`` —
        the same 右寄せ muted 書式 the other sections' header badges use
        (N-120)。件数を行タプルに**含める**こと: 下の no-op ガードはタプルの
        同値比較なので、外に出すと件数だけの変化が黙って捨てられる。

        An empty *rows* shows the section's hint label instead of a bare list.
        The count label mirrors the number of rows.  The "current location"
        emphasis is payload-based (:meth:`set_emphasis`), so unlike the old
        selection-based highlight it survives a repopulate untouched.

        *show_count* ``False`` suppresses the header badge for a section whose
        row count carries no information (UIレビュー 2026-08-28 N-117): the
        キュレーション section's rows are a fixed menu of destinations, so its
        badge was the constant 「2」 sitting in a column of numbers that vary
        everywhere else — read as 「★を 2 件付けている」.  The badge contract
        itself (「行数を映す」) is unchanged for every other section; those rows
        carry their own real counts in the label instead.

        **内容が同じ呼び出しは no-op** (UIレビュー07-25 追修 #7): ホストは
        グリッド再構築のたび（= 背景スキャンが着地するたび / 絞り込み 1 文字
        ごと）に ``set_curation`` 等を呼び直すが、``clear()`` + 再生成は
        キーボードカーソル（current item）を落とす — Tab で入って ↑↓ を
        押している最中にスキャンが着地すると選択が消える袋小路になっていた。
        件数が動けばラベル文字列が変わるので、この no-op ガードは自然に抜ける。
        """
        if rows == self._rows and show_count == self._show_count:
            return
        self._rows = list(rows)
        self._show_count = show_count
        self._list.clear()
        for label, payload, tooltip, count in rows:
            item = QListWidgetItem(label)
            item.setData(_PAYLOAD_ROLE, payload)
            item.setData(_COUNT_ROLE, count)
            if tooltip:
                item.setToolTip(tooltip)
            self._list.addItem(item)
        self._header.set_count_text(
            str(len(rows)) if (rows and show_count) else ""
        )
        if rows:
            self._stack.setCurrentWidget(self._list)
            row_height = self._list.sizeHintForRow(0)
            frame = 2 * self._list.frameWidth()
            visible_rows = min(len(rows), _MAX_VISIBLE_ROWS)
            self._stack.setMaximumHeight(row_height * visible_rows + frame)
        else:
            self._stack.setCurrentWidget(self._empty_label)
            self._stack.setMaximumHeight(_EMPTY_STATE_HEIGHT)

    def set_empty_text(self, text: str) -> None:
        """空状態のヒント文を差し替える（節の構築時の既定を上書きする）。

        ``set_items`` の no-op ガードは**行**に対するものなので空文言には
        効かない。毎回 ``setText`` して構わない（同じ文字列なら Qt 側で
        再描画も起きない）。
        """
        self._empty_label.setText(text)

    def set_emphasis(
        self, current: object = None, ancestor: object = None
    ) -> None:
        """Mark the "current location" row (and optionally its ancestor).

        Purely a *painted* state (see :class:`_RailItemDelegate`) — the list's
        selection stays a free keyboard cursor, so ↑↓ can no longer break the
        current-location indicator (UIレビュー 07-25 #7).  ``None`` for both
        means "nothing here is current".
        """
        if (current, ancestor) == (self._current_payload, self._ancestor_payload):
            return
        self._current_payload = current
        self._ancestor_payload = ancestor
        self._list.viewport().update()

    def emphasis_for(self, payload: object) -> str | None:
        """``"current"`` / ``"ancestor"`` / ``None`` for *payload* (delegate)."""
        if payload is not None and payload == self._current_payload:
            return "current"
        if payload is not None and payload == self._ancestor_payload:
            return "ancestor"
        return None

    def current_payload(self) -> object:
        return self._current_payload

    def clear_selection(self) -> None:
        self._list.clearSelection()
        self._list.setCurrentItem(None)

    def focus_list(self) -> bool:
        """行があればリストへフォーカスを移す（Alt+1 — N-28）。

        空の節はヒントラベルに差し替わっていてキーの行き先が無いので False を
        返し、呼び出し側が次の節を試せるようにする。
        """
        if self._list.count() == 0:
            return False
        self._list.setFocus(Qt.ShortcutFocusReason)
        if self._list.currentItem() is None:
            self._list.setCurrentRow(0)
        return True

    # -------------------------------------------------------------- internals

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        payload = item.data(_PAYLOAD_ROLE)
        # A double-click delivers itemClicked *and* itemActivated; collapse the
        # pair into one navigation (UIレビュー 07-25 #7).
        last_payload, last_at = self._last_emit
        now = time.monotonic()
        if last_payload == payload and now - last_at < _ACTIVATE_DEDUPE_S:
            return
        self._last_emit = (payload, now)
        self.item_clicked.emit(payload)


class NavRail(QWidget):
    """The left navigation rail: library / curation / bookmark / search sections."""

    navigate_root = Signal(Path)
    navigate_bookmark = Signal(str)
    apply_saved_search = Signal(int)
    open_curation = Signal(str)
    manage_libraries = Signal()
    manage_bookmarks = Signal()
    manage_saved_searches = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        # Path strings of the current library rows, kept so ``set_current_root``
        # can re-derive the current / ancestor emphasis without the window
        # re-supplying the whole list.
        self._library_paths: list[str] = []
        # 現在地の材料（ライブラリ行の強調は root と横断ビューの両方に依存する
        # ため、後から届いたほうだけで再計算できるよう両方を持つ）。
        self._current_root: Path | None = None
        self._curation_active = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # Small fixed gap between sections (not the 0-stretch idiom below,
        # which would have every section share leftover space equally and
        # spread out — see the trailing addStretch instead).
        layout.setSpacing(6)

        self._libraries = _RailSection(
            t("viewer.main_window.library_menu"),
            t("viewer.main_window.no_libraries"),
            t("viewer.nav_rail.manage_libraries_tooltip"),
        )
        self._libraries.item_clicked.connect(self._on_library_clicked)
        self._libraries.manage_clicked.connect(self.manage_libraries)
        layout.addWidget(self._libraries)

        # (UIレビュー 07-25 #57) 横断キュレーション一覧はブックマークメニューの
        # 奥にしか無かった — ライブラリと同格の「行き先」としてレールに常設する。
        # 管理ダイアログを持たないセクションなので ⋯ は出さない。
        self._curation = _RailSection(
            t("viewer.nav_rail.section_curation"),
            t("viewer.nav_rail.empty_curation"),
        )
        self._curation.item_clicked.connect(self._on_curation_clicked)
        layout.addWidget(self._curation)

        self._bookmarks = _RailSection(
            t("viewer.nav_rail.section_bookmarks"),
            t("viewer.nav_rail.empty_bookmarks"),
            t("viewer.nav_rail.manage_bookmarks_tooltip"),
        )
        self._bookmarks.item_clicked.connect(self._on_bookmark_clicked)
        self._bookmarks.manage_clicked.connect(self.manage_bookmarks)
        layout.addWidget(self._bookmarks)

        self._searches = _RailSection(
            t("viewer.main_window.saved_searches"),
            t("viewer.nav_rail.empty_saved_searches"),
            t("viewer.nav_rail.manage_saved_searches_tooltip"),
        )
        self._searches.item_clicked.connect(self._on_saved_search_clicked)
        self._searches.manage_clicked.connect(self.manage_saved_searches)
        layout.addWidget(self._searches)

        # Sections stack from the top at their content height; all leftover
        # rail height collects here at the bottom instead of being divided
        # up among the three sections.
        layout.addStretch(1)

    # ------------------------------------------------------------------ API

    def set_libraries(
        self, bases: list[tuple[Path, str]], current_root: Path | None
    ) -> None:
        """Populate the library section from ``(path, label)`` *bases*.

        *bases* is exactly ``ViewerWindow._compute_library_bases()`` — the
        default library first (labelled 「ライブラリ」) followed by each
        registered root.  *current_root* re-applies the emphasis afterwards.
        """
        self._library_paths = [str(p) for p, _label in bases]
        rows: list[_Row] = [
            (label, str(p), str(p), None) for p, label in bases
        ]
        self._libraries.set_items(rows)
        self.set_current_root(current_root)

    def set_current_root(self, root: Path | None) -> None:
        """Mark the library row for *root* — exact match, else its ancestor.

        Two tiers (UIレビュー 07-25 #7 / #79): the row equal to *root* is the
        strong "current" paint, and — when the user has drilled below a
        library — the enclosing library keeps a faint bar instead of the rail
        going blank.  Pure path arithmetic (no disk access).
        """
        self._current_root = root
        self._apply_library_emphasis()

    def _apply_library_emphasis(self) -> None:
        current, ancestor = self._library_emphasis(self._current_root)
        if self._curation_active and current is not None:
            # 横断一覧の表示中は「現在地」はキュレーション行 1 つだけ — 母集合を
            # 提供しているライブラリ行は祖先扱いへ落とす（現在地が 2 箇所同時に
            # 点灯すると「どちらに居るのか」の答えが割れる）。
            current, ancestor = None, current
        self._libraries.set_emphasis(current, ancestor)

    def _library_emphasis(self, root: Path | None) -> tuple[object, object]:
        if root is None:
            return (None, None)
        # 基準ライブラリの選び方はパンくず / 検索と同じ ``pick_library_base``
        # （**最も浅い**基準）に一本化する — レールだけが「最も深い祖先」を
        # 採っていたため、入れ子登録では画面ごとに別のライブラリ名を名乗って
        # いた。照合はケース非区別（``normcase``）で、完全一致と祖先判定が
        # Windows で非対称にならないようにする。ラベル欄には行のペイロード
        # （元の文字列）を載せて、正規化した経路から戻せるようにする。
        bases = [(Path(os.path.normcase(raw)), raw) for raw in self._library_paths]
        picked = pick_library_base(Path(os.path.normcase(str(root))), bases)
        if picked is None:
            return (None, None)
        _base, payload, rel = picked
        return (payload, None) if not rel else (None, payload)

    def set_curation(
        self,
        available: bool,
        counts: dict[str, int] | None = None,
        user_tags: list[str] | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        """Populate the キュレーション section (UIレビュー 07-25 #57).

        *available* mirrors "a user-curation store is wired" (read-only volumes
        have none) — without it there is nothing to list, so the section falls
        back to its hint text exactly like an empty bookmark list.

        *reason* は「店が開けなかった」理由（``user_meta.db`` を開けない）。
        与えられた（かつ *available* が False の）ときだけ空文言を**保存でき
        ない**旨へ差し替える — 既定の「スターや「あとで見る」を付けると一覧
        できます」は保存できる前提の案内なので、開けない環境では誤案内に
        なっていた（N-128）。例外メッセージそのものはここへ出さない（定型文
        のみ。詳細は起動時の警告トースト）。

        *counts* maps each row's payload (``"starred"`` / ``"later"`` /
        ``"tag:<名前>"``) to **how many entries the user has marked**, shown after
        the row label (UIレビュー 2026-08-28 N-117).  The host reads them off its
        in-memory curation map, so this costs no I/O; it is deliberately
        「印を付けた件数」 and not 「一覧に並ぶ件数」, which can be smaller when
        entries have moved, gone, or become unreachable (N-09).

        *user_tags* adds one row per distinct ユーザータグ (N-71) — the third
        curation axis, which until now had no cross-library way out at all.
        """
        rows: list[_Row] = []
        if available:
            counts = counts or {}
            rows = [
                self._curation_row(
                    "starred",
                    t("viewer.main_window.curation_starred_list"),
                    t("viewer.main_window.curation_starred_list_hint"),
                    counts,
                ),
                self._curation_row(
                    "later",
                    t("viewer.main_window.curation_later_list"),
                    t("viewer.main_window.curation_later_list_hint"),
                    counts,
                ),
            ]
            for tag in user_tags or []:
                rows.append(
                    self._curation_row(
                        f"tag:{tag}",
                        t("viewer.main_window.curation_tag_list", tag=tag),
                        t("viewer.main_window.curation_tag_list_hint", tag=tag),
                        counts,
                    )
                )
        self._curation.set_empty_text(
            t("viewer.nav_rail.empty_curation_unavailable")
            if (not available and reason)
            else t("viewer.nav_rail.empty_curation")
        )
        # バッジは出さない (N-117) — この節の行数は「行き先の数」であって印の数
        # ではないので、変動する他 3 節の数字の列に定数が混ざって誤読させていた。
        # 実件数は各行が専用ロールで持つ（N-120）。
        self._curation.set_items(rows, show_count=False)

    @staticmethod
    def _curation_row(
        payload: str, label: str, hint: str, counts: dict[str, int],
    ) -> _Row:
        """One キュレーション row with its 「印を付けた件数」 (N-117 / N-120).

        件数はラベル文字列ではなく専用ロールへ載せる（デリゲートが右端に
        ``text_muted`` で描く = 他 3 節のヘッダー件数と同じ書式）。タプルに
        含めるので ``set_items`` の no-op ガードは件数の変化で自然に抜ける。
        """
        n = counts.get(payload)
        if n is None:
            return (label, payload, hint, None)
        return (
            label,
            payload,
            "\n".join(
                (hint, t("viewer.nav_rail.curation_row_count_hint", n=n))
            ),
            n,
        )

    def set_current_curation(self, kind: str | None) -> None:
        """Mark the active cross-library curation view.

        *kind* is ``"starred"`` / ``"later"`` / ``"tag:<名前>"`` (N-71) or
        ``None``.  Same "current location" paint as the library row (UIレビュー
        07-25 #7) so the rail tells the truth about which横断ビュー is showing.
        現在地はレール全体で 1 つ — 一覧の表示中はライブラリ行の強調を祖先へ
        落とす（:meth:`_apply_library_emphasis`）。
        """
        active = isinstance(kind, str) and bool(kind)
        self._curation.set_emphasis(kind if active else None)
        if active != self._curation_active:
            self._curation_active = active
            self._apply_library_emphasis()

    def set_bookmarks(self, bookmarks: list[str], names: dict[str, str]) -> None:
        """Populate the bookmark section (:func:`bookmark_label` per row)."""
        rows: list[_Row] = [
            (bookmark_label(raw, names), raw, raw, None) for raw in bookmarks
        ]
        self._bookmarks.set_items(rows)

    def set_saved_searches(
        self, searches: list[dict], tooltips: list[str] | None = None,
    ) -> None:
        """Populate the saved-search section; payload = the search's index.

        *tooltips* (same order / length as *searches*) is the hover text the
        window builds from each payload — the条件サマリ + 「現在のフォルダを起点に
        適用」 line (UIレビュー 07-25 #34).  Omitted / short lists fall back to the
        row's name, keeping the rail a dumb view that never inspects a payload.
        """
        rows: list[_Row] = []
        for idx, entry in enumerate(searches):
            name = str(entry.get("name") or "").strip() or t(
                "viewer.main_window.saved_search_unnamed"
            )
            tip = tooltips[idx] if tooltips and idx < len(tooltips) else name
            rows.append((name, idx, tip, None))
        self._searches.set_items(rows)

    def focus_first_row(self) -> None:
        """Alt+1 の着地点 — 中身のある最初の節のリストへフォーカスする (N-28).

        全節が空（登録ライブラリも保存も無い新規環境）なら、レール内で最初に
        フォーカスを受けられる部品（節見出しの ``⋯``）へ落とす: 何も起きない
        より、フォーカス枠（N-26 案B）が左席に出て「ここに居る」ことが伝わる
        ほうがよい。``NavRail`` 自身は ``NoFocus`` のまま（Tab 巡回の席は
        ``main_window._tab_stops`` が子へ展開する規約を崩さない）。
        """
        for section in (
            self._libraries, self._curation, self._bookmarks, self._searches,
        ):
            if section.focus_list():
                return
        for child in self.findChildren(QWidget):
            if child.focusPolicy() != Qt.NoFocus and child.isVisible():
                child.setFocus(Qt.ShortcutFocusReason)
                return

    # -------------------------------------------------------------- internals

    def _on_library_clicked(self, payload: object) -> None:
        # 「現在地」はレール全体で 1 つ（:meth:`_apply_library_emphasis` の
        # 不変条件）。クリック由来の**選択ハイライト**も落とす — 落とさないと、
        # ライブラリ行を踏んだあとに横断一覧へ入ったとき、強調を祖先へ下げた
        # ライブラリ行が選択ハイライトのまま点灯し続け、キュレーション行と
        # 併せて現在地が 2 箇所光る（UIレビュー 2026-08-28 N-90）。実際の
        # 現在地は payload 由来の描画（``set_current_root``）が担う。
        self._libraries.clear_selection()
        if isinstance(payload, str):
            self.navigate_root.emit(Path(payload))

    def _on_curation_clicked(self, payload: object) -> None:
        # 横断ビューは「現在地」なので選択は落とすが、
        # ``set_current_curation`` の描画（ホストが実際に入場した後に呼ぶ）
        # で点灯し続ける (UIレビュー 07-25 #57)。
        self._curation.clear_selection()
        if isinstance(payload, str):
            self.open_curation.emit(payload)

    def _on_bookmark_clicked(self, payload: object) -> None:
        # Bookmarks / saved searches are actions, not a "current location", so
        # drop the transient click selection (only the library / curation rows
        # carry the painted current-location state).
        self._bookmarks.clear_selection()
        if isinstance(payload, str):
            self.navigate_bookmark.emit(payload)

    def _on_saved_search_clicked(self, payload: object) -> None:
        self._searches.clear_selection()
        if isinstance(payload, int):
            self.apply_saved_search.emit(payload)


__all__ = ["NavRail", "bookmark_label"]
