"""Small shared Qt widget helpers for the snappix GUIs.

Things live here:

* :class:`EmptyStateCard` — the unified empty-state visual (icon + heading +
  optional description + optional action button(s)) introduced by the layout
  redesign (docs/claude/design.md, `EmptyStateCard` section): every "nothing here"
  surface (management-dialog empty lists, the content pane's welcome / empty
  cards, ...) reads as the same card grammar instead of ad-hoc per-widget
  hint labels.
* :func:`empty_state_stack` — used by the management dialogs (UIレビュー
  #15) and ``NavRail``: a big empty table reads as "broken / not loaded", so
  when a list has no rows we swap the table out for a centred guidance
  widget — the same ``QStackedWidget`` pattern ``detail_window`` uses for its
  tag table.  Kept as a tiny constructor helper (the caller drives
  ``setCurrentWidget`` itself, since the "is it empty?" condition differs per
  dialog) rather than a full widget subclass.
* :class:`PanelHeader` — the standardised 28px pane-header strip introduced by
  the layout redesign (docs/claude/design.md, `PanelHeader` section): a caption
  title + optional item-count label + optional "⋯" overflow entry point.
* :func:`align_header` — 「見出しの揃え = 内容の揃え」 for the management
  dialogs' tables / trees (UIレビュー 07-25 #97).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import set_icon, set_icon_pixmap
from .qss import hint_style
from .tokens import (
    FONT_BODY_PT,
    FONT_CAPTION_PT,
    FONT_SUBTITLE_PT,
    FONT_TITLE_PT,
    PANEL_HEADER_HEIGHT,
)

#: Icon pixel size range the redesign spec calls for (32-48px); the default
#: sits in the middle so a card reads clearly without dominating a compact
#: pane (self-review note: bigger read as "间延び" — over-padded).
_DEFAULT_ICON_SIZE = 40

#: Heading emphasis levels (see :class:`EmptyStateCard`).
CardEmphasis = Literal["hint", "onboarding", "secondary"]


class EmptyStateCard(QWidget):
    """Unified empty-state visual: icon + heading + description + action(s).

    docs/claude/design.md (`EmptyStateCard` section) asks for one visual grammar for
    every "nothing here" surface: a centred, vertically-stacked
    ``[icon] / heading / [description] / [action buttons]``.  ``emphasis``
    picks how loud the heading reads:

    * ``"hint"`` (default) — a quiet, single-purpose guidance message
      (:data:`FONT_BODY_PT`, ``text_muted``).  This is the regularised form
      of the old plain hint ``QLabel`` used by :func:`empty_state_stack`,
      ``GalleryView``'s settled-empty overlay, and ``FileListView``'s
      unselected/empty placeholder.
    * ``"onboarding"`` — a named call-to-action card (:data:`FONT_TITLE_PT`,
      bold, normal ``text`` colour) for a surface that already earns its own
      heading + body + button: the centre pane's first-run 「ようこそ」 card
      and the drilled-into-empty-folder card (UIレビュー #3/#6) predate this
      component and keep their stronger heading weight — only the icon and
      the shared icon/heading/body/button scaffold are new for them.
    * ``"secondary"`` — the **従属面** form (:data:`FONT_CAPTION_PT`,
      ``text_muted``) introduced by the空状態オーケストレータ
      (UIレビュー 2026-08-28 提案3 / N-101): when another seat already owns the
      window's ``PRIMARY`` guidance card, the remaining seats must not repeat
      it at the same visual weight.  Pass ``icon_name=None`` with it — the
      whole point is that a subordinate placeholder carries **no icon and one
      quiet line**, so three folder glyphs never line up across three panes.

    Both the description and the action row are optional and only take up
    space once used (:meth:`set_body`, :meth:`add_action`).
    """

    def __init__(
        self,
        heading: str = "",
        *,
        icon_name: str | None = None,
        icon_size: int = _DEFAULT_ICON_SIZE,
        body: str = "",
        emphasis: CardEmphasis = "hint",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._emphasis: CardEmphasis = emphasis

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)
        layout.addStretch(1)

        if icon_name is not None:
            icon_label = QLabel()
            icon_label.setAlignment(Qt.AlignCenter)
            # set_icon_pixmap (not a bare ``icon(...).pixmap(...)``) so the
            # glyph is re-tinted by ``apply_theme`` → ``retint_all``: these
            # cards live as long as the main window, and a baked pixmap kept
            # the creation-time ``text_muted`` after a theme switch (#118).
            set_icon_pixmap(icon_label, icon_name, role="muted", size=icon_size)
            layout.addWidget(icon_label)
            layout.addSpacing(4)

        self._heading_label = QLabel(heading)
        self._heading_label.setAlignment(Qt.AlignCenter)
        self._heading_label.setWordWrap(True)
        self._heading_label.setStyleSheet(self._heading_style())
        layout.addWidget(self._heading_label)

        # Optional supplementary note (e.g. the welcome card's 「library は
        # 初期フォルダ」 aside) — inserted between the body and the button row
        # on demand via :meth:`add_note`.  Tracked so repeated notes append in
        # order without disturbing the button row that follows.
        self._pre_button_index = layout.count()

        self._body_label = QLabel(body)
        self._body_label.setAlignment(Qt.AlignCenter)
        self._body_label.setWordWrap(True)
        body_pt = FONT_SUBTITLE_PT if emphasis == "onboarding" else FONT_CAPTION_PT
        self._body_label.setStyleSheet(hint_style(font_pt=body_pt))
        self._body_label.setVisible(bool(body))
        layout.insertWidget(self._pre_button_index, self._body_label)
        self._pre_button_index += 1

        layout.addSpacing(8)
        self._button_row = QHBoxLayout()
        self._button_row.addStretch(1)
        self._button_row.addStretch(1)
        layout.addLayout(self._button_row)

        # 上下同率 = 内容ブロックを垂直中央に置く。以前は下 2 / 上 1 で
        # 「やや上寄り」だったため、3 席（ナビレール / グリッド / 情報パネル）
        # に空状態が同時に出ると基準線が席ごとにズレて見えた。
        layout.addStretch(1)

    def _heading_style(self) -> str:
        if self._emphasis == "onboarding":
            return f"font-size: {FONT_TITLE_PT}pt; font-weight: bold;"
        if self._emphasis == "secondary":
            # 従属面は主カードより 1 段小さく・同じ muted 色（N-101）。
            return hint_style(font_pt=FONT_CAPTION_PT)
        return hint_style(font_pt=FONT_BODY_PT)

    # ------------------------------------------------------------- content

    def set_heading(self, text: str) -> None:
        self._heading_label.setText(text)

    def heading(self) -> str:
        return self._heading_label.text()

    # ``setText``/``text`` alias the heading so an ``EmptyStateCard`` is a
    # drop-in replacement for the plain ``QLabel`` that :func:`empty_state_stack`
    # used to hand back (existing callers only ever call ``setText``/``text``
    # on it, plus ``QStackedWidget.setCurrentWidget``).
    setText = set_heading
    text = heading

    def set_body(self, text: str) -> None:
        """Set the optional description line; empty text hides it."""
        self._body_label.setText(text)
        self._body_label.setVisible(bool(text))

    def add_note(self, text: str) -> QLabel:
        """Insert a supplementary note between the body and the button row.

        Used for the welcome card's first-run-library aside — a one-off
        addendum distinct from the main description.  Returns the label so
        the caller can toggle its visibility.
        """
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        note_pt = FONT_SUBTITLE_PT if self._emphasis == "onboarding" else FONT_CAPTION_PT
        label.setStyleSheet(hint_style(font_pt=note_pt))
        self.layout().insertWidget(self._pre_button_index, label)
        self._pre_button_index += 1
        return label

    def add_action(self, text: str, *, icon_name: str | None = None) -> QPushButton:
        """Append an action button to the (initially empty) button row."""
        btn = QPushButton(text)
        if icon_name is not None:
            set_icon(btn, icon_name)
        self._button_row.insertWidget(self._button_row.count() - 1, btn)
        return btn


def empty_state_stack(
    content: QWidget, *, icon_name: str | None = None,
) -> tuple[QStackedWidget, QLabel | EmptyStateCard]:
    """Wrap *content* in a stack whose second page is a centred empty guidance.

    Returns ``(stack, empty_label)``.  Page 0 is *content* (a table / tree),
    page 1 is the empty-state guidance.  Callers set the label text (via
    ``.setText(...)``, works on either return type — see
    :class:`EmptyStateCard`) and call ``stack.setCurrentWidget(...)`` from
    their own row-sync logic to switch between the populated table and the
    centred empty guidance.

    *icon_name* is ``None`` by default, which keeps the historical plain
    word-wrapped ``QLabel`` (styled with :func:`hint_style`) — used by
    ``NavRail``'s tightly height-capped sections, where the card's 24px
    margins would not fit.  Passing an icon name (e.g. ``"folder"``,
    ``"search"``, ``"bookmark"``) upgrades page 1 to a full
    :class:`EmptyStateCard` instead, for dialogs with room to breathe.
    """
    stack = QStackedWidget()
    stack.addWidget(content)  # index 0: the populated table / tree
    empty_label: QLabel | EmptyStateCard
    if icon_name is None:
        empty_label = QLabel("")
        empty_label.setWordWrap(True)
        empty_label.setAlignment(Qt.AlignCenter)
        empty_label.setStyleSheet(hint_style())
    else:
        empty_label = EmptyStateCard(icon_name=icon_name)
    stack.addWidget(empty_label)  # index 1: centred empty guidance
    return stack, empty_label


def align_header(
    view,
    *,
    right: Iterable[int] = (),
    center: Iterable[int] = (),
) -> None:
    """Align each column heading with the content beneath it (#97).

    Qt's default heading alignment differs per widget family — a
    ``QTableWidget``'s header items come out **centred** while a
    ``QTreeWidget``'s header is **left**-aligned — so the viewer's six
    management tables (タグ一覧 / ライブラリ管理 / 保存した検索 /
    ブックマーク / ショートカット / 健全性) disagreed with each other AND
    with their own right-aligned numeric cells (UIレビュー 07-25 #97).
    This applies one rule everywhere: **the heading takes the alignment of
    its column's content**.

    Columns listed in *right* / *center* get that alignment; every other
    column is left-aligned (the default for text).  Accepts either a
    ``QTableWidget`` (header items) or a ``QTreeWidget`` (a single
    ``headerItem``); pass numeric columns in *right* so the heading sits
    over the digits it labels.  Re-apply after any call that rebuilds the
    header labels (``setHorizontalHeaderLabels`` replaces the items).
    """
    right_cols = set(right)
    center_cols = set(center)
    header_item = getattr(view, "headerItem", None)
    if header_item is not None:  # QTreeWidget: one item carries every column
        item = header_item()
        if item is None:
            return
        columns = item.columnCount()

        def _set(col: int, flags) -> None:
            item.setTextAlignment(col, flags)
    else:  # QTableWidget: one QTableWidgetItem per column
        columns = view.columnCount()

        def _set(col: int, flags) -> None:
            cell = view.horizontalHeaderItem(col)
            if cell is not None:
                cell.setTextAlignment(flags)

    for col in range(columns):
        if col in right_cols:
            flags = Qt.AlignRight | Qt.AlignVCenter
        elif col in center_cols:
            flags = Qt.AlignHCenter | Qt.AlignVCenter
        else:
            flags = Qt.AlignLeft | Qt.AlignVCenter
        _set(col, flags)


class PanelHeader(QWidget):
    """Standardised pane-header strip (redesign 2026-07 Phase 1-2).

    Fixed height :data:`PANEL_HEADER_HEIGHT` so every pane that adopts it
    lines up.  Layout, left → right:

    * **Title** — caption-size (:data:`FONT_CAPTION_PT`), letter-spaced,
      ``text_muted``.  Japanese UI copy, so it is shown as-is (no
      upper-casing).
    * **Stretch** — absorbs the remaining width.
    * **Count label** (optional) — right-aligned, also ``text_muted``; hidden
      whenever its text is empty (:meth:`set_count_text`).
    * **Trailing action button** (optional, revealed by :meth:`action_button` /
      its 「⋯」 alias :meth:`overflow_button`) — a borderless ``QToolButton``.
      ``PanelHeader`` only standardises its look/placement; the caller wires
      ``clicked`` to whatever it should open (a ``QMenu``, a small popover
      widget, …) and picks the glyph by meaning.  The button is **always
      constructed and always occupies its slot** (``retainSizeWhenHidden``),
      merely hidden until a caller claims it: a header without one otherwise
      pulls its count label ~37px further right than its neighbours, so a stack
      of headers (the ナビレール's four sections) shows a ragged column of
      numbers (UIレビュー 2026-08-28 N-117).

    The surrounding hairline (transparent background + 1px bottom border) is
    driven by the ``QWidget#panelHeader`` rule in ``qss.py`` — tokens only,
    no literal colours here (docs/claude/design.md).
    """

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panelHeader")
        # QWidget サブクラスは WA_StyledBackground が無いと QSS の background /
        # border を一切描かない（design.md の「`QWidget` サブクラスの QSS 背景」）。
        # これが無い間、qss の `#panelHeader` の下境界線は生成されていても
        # 一度も描かれていなかった。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(PANEL_HEADER_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)

        self._title_label = QLabel(title)
        title_font = QFont(self._title_label.font())
        # Letter-spacing must go through QFont — there is no QSS equivalent —
        # while colour/size stay on the usual hint_style() stylesheet path.
        title_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 115)
        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
        layout.addWidget(self._title_label)

        layout.addStretch(1)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
        self._count_label.setVisible(False)
        layout.addWidget(self._count_label)

        # ⋯ の席は常に確保する（N-117 — 上の docstring 参照）。実際に使うかは
        # :meth:`overflow_button` を呼んだかどうかで決まる。
        btn = QToolButton(self)
        set_icon(btn, "more-horizontal")
        # One-property override (design.md's allowed exception): flatten
        # an already-transparent QToolButton with no border at all.
        btn.setStyleSheet("QToolButton { border: none; }")
        policy = btn.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        btn.setSizePolicy(policy)
        btn.setVisible(False)
        layout.addWidget(btn)
        self._overflow_btn = btn
        self._overflow_enabled = False

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)

    def set_count_text(self, text: str) -> None:
        """Set the count label; an empty string hides it entirely."""
        self._count_label.setText(text)
        self._count_label.setVisible(bool(text))

    def action_button(self, icon_name: str = "more-horizontal") -> QToolButton:
        """Claim (reveal) + return the header's borderless trailing button.

        Idempotent — repeat calls return the same button (the *first* call
        decides the glyph).  ``PanelHeader`` does not own a menu/popover
        itself: the caller connects ``clicked`` (or calls ``setMenu``) to
        whatever this pane wants to tuck away.

        **Pick the glyph by meaning, not by habit** (UIレビュー 08-28 N-24):
        「⋯」(``more-horizontal``) means *the display options of this pane* and
        nothing else — a header whose button fires a single action (e.g. 「管理
        ダイアログを開く」) must pass its own glyph (``settings``) instead, or the
        same figure ends up standing for three unrelated things.

        The widget itself already exists (and already holds its slot) from
        construction; this only picks the glyph and makes it visible — see the
        class docstring for why the slot is reserved either way (N-117).
        """
        if not self._overflow_enabled:
            self._overflow_enabled = True
            set_icon(self._overflow_btn, icon_name)
            self._overflow_btn.setVisible(True)
        return self._overflow_btn

    def overflow_button(self) -> QToolButton:
        """The 「⋯」 overflow button — this pane's **display options** trigger."""
        return self.action_button("more-horizontal")

    def has_action_button(self) -> bool:
        """Whether a caller has claimed the trailing button (i.e. it is shown)."""
        return self._overflow_enabled


def align_form_labels(*labels: QLabel) -> int:
    """Right-align *labels* to a shared width so their rows' inputs line up.

    「揃っていないラベル列」は 07-18 #16 → 07-25 #24 → 08-28 N-99 と 3 度
    指摘されている。原因は **同じ整列機構を面ごとに手書きしていた**ことで、
    N-99 の検証は「2 面で計 7 通りの左端」を実測した上で、既に正解形を
    持っていた AI 検索ポップオーバー（``advanced_search``）のヘルパを
    ``common/ui`` へ引き上げて 3 面で共有することを改善案に据えた。

    行ごとに ``QHBoxLayout`` を持つ形（行が inline のコントロールを併せ
    持つため単一の ``QFormLayout`` が合わない — フィルターポップオーバーの
    投稿日行が典型）でも、先頭ラベルに**共有の固定幅 + 右揃え**を与える
    だけで入力ウィジェットの x が揃う。幅は最も広いラベル自身のフォント
    メトリクス由来なので、マジックピクセルは入らない（design.md）。
    共有ラベル列幅を返すので、行に属するが独立行に置かれるキャプションを
    :func:`indent_to_form_column` で同じ x へ字下げできる（07-25 #81）。
    """
    width = 0
    for lbl in labels:
        width = max(width, lbl.fontMetrics().horizontalAdvance(lbl.text()))
    for lbl in labels:
        lbl.setFixedWidth(width)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return width


def indent_to_form_column(
    widget: QWidget, label_column_width: int, spacing: int,
) -> None:
    """ラベル列の幅ぶん *widget* を字下げする (UIレビュー 07-25 #81).

    ラベルを持たない独立行（入力欄直下の例示行）は
    :func:`align_form_labels` の整列から外れて左端に取り残される。行の
    ラベル列 +（行レイアウトの）間隔ぶんの左マージンを与えて、同じ縦の
    ラインへ載せる。幅はフォントメトリクス由来なのでマジックピクセルは
    増えない。

    **効くのは自分でテキストを描くウィジェット（``QLabel`` 等）だけ**:
    実装は ``setContentsMargins`` 1 本なので、``QCheckBox`` のように印と
    テキストの配置をスタイルが決めるコントロールは動かない。ラベルを持た
    ないチェック行は**空の ``QLabel`` を行の先頭に置いて**列へ載せること
    （``filter_popover._build_row`` — N-43）。
    """
    widget.setContentsMargins(
        max(0, label_column_width) + max(0, spacing), 0, 0, 0
    )


__all__ = [
    "align_form_labels",
    "align_header",
    "empty_state_stack",
    "EmptyStateCard",
    "indent_to_form_column",
    "PanelHeader",
]
