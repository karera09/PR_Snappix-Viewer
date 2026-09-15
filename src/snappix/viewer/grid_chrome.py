"""ツールバー周りのクローム部品（``PostGrid`` から切り出し）。

ウィンドウレベルの統合ツールバーと、そこから開く 2 つのポップオーバーの
**組み立てと見た目だけ**を持つ Qt 層。所有は従来どおり ``PostGrid`` で、
ここは席を作るだけ — 状態（絞り込み文字列・並び順・表示形式・サムネイル
サイズ・年齢区分…）は 1 つも持たない。

部品:

* :class:`SearchField` — 検索欄（モードチップ + ``filter_edit`` + 構文ヘルプ
  の図像）。押下・入力の**意図**をシグナルで出し、表示の反映は
  :meth:`SearchField.apply_modes` が受け取った真偽値だけから決める。
* :class:`GridToolbar` — 統合ツールバー（ナビ群 / パンくず / ルート変更 /
  検索欄 / フィルタ / 並び・表示 / ペイン表示トグル）。
* :class:`ViewPopover` — 「並び・表示」ポップオーバーの枠。3 行の中身は
  ホストの共有ファクトリ（``ChildrenGrid._build_view_settings_rows``）が
  組むので、枠はそれを**コールバックで**受け取る。
* :class:`DisplayOptions` — そのポップオーバー下段の表示オプション
  （``#thumb#`` 除外 / 年齢区分バンド）。
* :class:`FilterHelpPopups` — 検索欄の構文ヘルプ 2 面（図像から開く全文 /
  初回フォーカスの短縮版）の寿命管理。どちらも TOP-LEVEL なので、開き直し
  と畳みで必ず ``deleteLater`` する。

ホストとの境界は**シグナル（ユーザー操作の意図）とコールバック**だけで、
どの部品もホストの属性を読み書きしない。外から観測される名前
（``filter_edit`` / ``mode_chip_*`` / ``nsfw_menu`` …）は ``PostGrid`` 側の
プロパティが**ここで作った同一実体**を返す。

テストは ``tests/test_viewer_grid_chrome.py``（部品単体）と
``tests/test_viewer_toolbar.py``（席と配線）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QIcon,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import TOOLBAR_HEIGHT, popover_position, set_icon
from . import ai_pack
from .breadcrumb import BreadcrumbBar
from .filter_query import _CONTROL_FIELDS, _FILTER_FIELD_GETTERS
from .post_grid_types import FILTER_HELP_POPUP_NAME
from .qimage_decode import enable_clear_button
from .search_dimensions import (
    filter_help_brief_html,
    filter_help_html,
    token_description,
    token_fields,
)

__all__ = [
    "DateRangeEditors",
    "DisplayOptions",
    "FilterHelpPopups",
    "GridToolbar",
    "NAV_BUTTON_WIDTH",
    "SEARCH_FIELD_MAX_W",
    "SEARCH_FIELD_MIN_W",
    "SearchField",
    "ViewPopover",
    "build_date_range_editors",
    "build_prefix_completer",
    "filter_token_kinds",
    "make_mode_chip",
    "rewrite_query_for_mode",
]

#: ツールバーの図像ボタンの固定幅（px）。ナビ群・ルート変更・フィルタ・
#: 並び・表示・ペイン表示トグルが全部この 1 つの値を読む。
NAV_BUTTON_WIDTH = 32

#: 検索欄の下限幅（px）。プレースホルダが読める最小幅。
SEARCH_FIELD_MIN_W = 260

#: Upper bound on the toolbar search field's width.  Raised 340 → 500
#: (UIレビュー 07-25 #3): the three mode chips + magnifier + ⚙ ate ~200px
#: of the old cap, leaving ``filter_edit`` about 133px — too narrow for
#: either the placeholder or the user's own query to be readable.  The
#: breadcrumb (the bar's stretch member) still has spare width at every
#: window size we measured, so the two share it instead of the field
#: being pinned narrow.  Paired with a shortened placeholder (案B従).
SEARCH_FIELD_MAX_W = 500


# --------------------------------------------------------------- mode chips

def make_mode_chip(
    text: str, tooltip: str, *, checkable: bool = True,
) -> QToolButton:
    """One small mode chip (名前 / 本文 / AIタグ) for the search field."""
    chip = QToolButton()
    chip.setObjectName("searchModeChip")
    chip.setToolButtonStyle(Qt.ToolButtonTextOnly)
    chip.setText(text)
    chip.setToolTip(tooltip)
    chip.setCheckable(checkable)
    chip.setCursor(Qt.PointingHandCursor)
    chip.setFocusPolicy(Qt.NoFocus)
    return chip


def filter_token_kinds(text: str) -> tuple[bool, bool]:
    """Classify the filter box's raw tokens → ``(has_body, has_bare)``.

    Raw tokenisation (not the parser) so an in-progress ``body:`` with no
    value yet still counts as a body token — the chips must not flip back
    to 名前 while the user is about to type the needle.
    """
    known = set(_FILTER_FIELD_GETTERS) | set(_CONTROL_FIELDS)
    has_body = has_bare = False
    for raw in text.split():
        head = raw.lstrip("~-")
        if not head:
            continue
        field = head.partition(":")[0].casefold()
        if ":" in head and field in known:
            if field == "body":
                has_body = True
            continue
        has_bare = True
    return has_body, has_bare


def rewrite_query_for_mode(text: str, mode: str) -> str:
    """名前 ⇄ 本文 チップ押下による検索欄テキストの書き換え。

    A pure *syntax aid* over the existing grammar: 本文 prefixes every
    bare term with ``body:`` (preserving ``~`` / ``-`` markers); 名前
    strips the ``body:`` prefix back off.  Other field-scoped terms are
    left untouched.  An empty box in 本文 mode pre-types the ``body:``
    prefix so the user just appends the needle.
    """
    known = set(_FILTER_FIELD_GETTERS) | set(_CONTROL_FIELDS)
    out: list[str] = []
    for raw in text.split():
        head = raw.lstrip("~-")
        if not head:
            continue
        marker = raw[: len(raw) - len(head)]
        field = head.partition(":")[0].casefold()
        if ":" in head and field in known:
            if mode == "name" and field == "body":
                value = head.partition(":")[2]
                if value:
                    out.append(marker + value)
                continue
            out.append(raw)
            continue
        out.append(marker + "body:" + head if mode == "body" else raw)
    new = " ".join(out)
    if mode == "body" and not filter_token_kinds(new)[0]:
        new = (new + " " if new else "") + "body:"
    return new


# -------------------------------------------------------------- search field

class SearchField(QFrame):
    """The toolbar's global search field: 260–500px wide, one bordered
    container hosting the mode chips (left), the ``filter_edit`` line edit
    (centre — the search execution paths are untouched) and the syntax
    help glyph at its right edge (a trailing ``QAction``).

    出す意図はシグナルだけ: どのチップが押されたか / 入力が変わったか /
    確定されたか / ヘルプが要求されたか。表示の反映は
    :meth:`apply_modes` の 4 つの真偽値だけから決まる（ホストの属性を
    1 つも読まない）。
    """

    #: 名前 / 本文 チップが押された（``"name"`` / ``"body"``）。
    mode_clicked = Signal(str)
    #: AIタグ チップが押された（AI パック有効時のみ存在する席）。
    ai_chip_clicked = Signal()
    #: 構文ヘルプの図像（または検索欄フォーカス中の Ctrl+/）が押された。
    help_clicked = Signal()
    #: 検索欄のテキストが変わった（プログラム由来の差し替えを含む）。
    text_changed = Signal(str)
    #: 利用者がキーで編集した（自動チートシートを畳む口）。
    text_edited = Signal(str)
    #: Enter — 「結果へ移る」。
    submitted = Signal()

    def __init__(self, parent: QWidget | None = None, *, ai_available: bool):
        super().__init__(parent)
        self.setObjectName("toolbarSearch")
        self.setMinimumWidth(SEARCH_FIELD_MIN_W)
        self.setMaximumWidth(SEARCH_FIELD_MAX_W)
        field_lay = QHBoxLayout(self)
        field_lay.setContentsMargins(6, 2, 2, 2)
        field_lay.setSpacing(3)

        # Mode chips.  名前 (default) and 本文 are a syntax aid over the
        # EXISTING filter grammar: 本文 rewrites the bare terms to ``body:``
        # terms (and back), never adding a second query path.  The AIタグ
        # chip only exists in the AI-pack build (``ai_pack.available()``
        # gate) and routes to the existing 詳細検索 panel — engine access
        # stays behind the provider registry.
        self.mode_chip_name = make_mode_chip(
            t("common.label.name"), t("viewer.toolbar.mode_name_tooltip"),
        )
        self.mode_chip_name.setChecked(True)
        self.mode_chip_name.clicked.connect(self._on_name_chip_clicked)
        field_lay.addWidget(self.mode_chip_name)
        self.mode_chip_body = make_mode_chip(
            t("viewer.toolbar.mode_body"),
            t("viewer.toolbar.mode_body_tooltip"),
        )
        self.mode_chip_body.clicked.connect(self._on_body_chip_clicked)
        field_lay.addWidget(self.mode_chip_body)
        #: AI パック無効の配布では席そのものが無い（``None``）。
        self.mode_chip_ai: QToolButton | None = None
        if ai_available:
            # Checkable so it can carry the SAME :checked accent styling as
            # 名前/本文 when an AI query is active (UIレビュー #10).  The check
            # state is driven purely by ``apply_modes`` (mirrors
            # ``_advanced_search_active``); a plain click only opens the AI
            # popover, so the host re-syncs right after to undo the auto-toggle.
            chip = make_mode_chip(
                t("viewer.toolbar.mode_ai"),
                t("viewer.toolbar.mode_ai_tooltip"),
                checkable=True,
            )
            # Visual separation of the lit AIタグ chip (UIレビュー 07-25 #14):
            # 名前/本文 are syntax aids over the SAME box, while AIタグ is a
            # different engine whose results the box then only narrows — so its
            # lit state gets the full accent fill instead of the soft tint the
            # other two share.  Palette roles only (no hardcoded colour), so it
            # tracks theme switches like the rest of the chrome.
            chip.setStyleSheet(
                "QToolButton#searchModeChip:checked {"
                " background: palette(highlight);"
                " color: palette(highlighted-text);"
                " font-weight: bold; }"
            )
            chip.clicked.connect(self._on_ai_chip_clicked)
            field_lay.addWidget(chip)
            self.mode_chip_ai = chip

        self.filter_edit = QLineEdit()
        enable_clear_button(self.filter_edit)
        self.filter_edit.setPlaceholderText(t("viewer.post_grid.filter_placeholder"))
        # 検索欄からの唯一のキーボード出口（Enter / ↓）を画面上で予告する
        # （UIレビュー 2026-09-11 N-69）。キーは表から引く（D1）。
        from .shortcuts_dialog import key_hint, with_key_hint

        results_hint = t(
            "viewer.post_grid.filter_results_hint",
            key=key_hint("viewer.shortcuts_dialog.desc_jump_to_results"),
        )
        self.filter_edit.setToolTip(
            (
                t("viewer.post_grid.filter_tooltip") if ai_available
                else t("viewer.post_grid.filter_tooltip_free")
            )
            + "\n" + results_hint
        )
        self.filter_edit.textChanged.connect(self.text_changed.emit)
        # Enter = 「結果へ移る」 (UIレビュー 07-25 #6).  Incremental search has
        # no "commit" concept, so Enter used to be a silent no-op indistinguishable
        # from a mis-press; ↓ is wired next to it in the host's ``eventFilter``
        # (the same address-bar / Explorer convention).
        self.filter_edit.returnPressed.connect(self.submitted.emit)
        # First-focus syntax cheatsheet (session-once) + Esc-to-dismiss are
        # handled in the host's eventFilter; typing hides it via textEdited.
        self.filter_edit.textEdited.connect(self.text_edited.emit)
        # Decorative leading magnifier so the field reads as "search" at a
        # glance (retinted on theme switch via the set_icon registry).
        self.filter_icon_action = self.filter_edit.addAction(
            QIcon(), QLineEdit.LeadingPosition
        )
        set_icon(self.filter_icon_action, "search", role="muted")
        field_lay.addWidget(self.filter_edit, 1)

        # 構文ヘルプは検索欄の右端（UIレビュー 2026-09-11 E3）— 旧「検索
        # オプション」ポップオーバー（sliders 図像・中身 2 項目）は撤去し、
        # 「サブフォルダも検索」はフィルターポップオーバーの先頭行（検索範囲）
        # へ（台帳 ``search_dimensions`` の ``recursive`` 行）。図像は既定色・
        # 16px（装飾の虫めがねと同じ明度に沈まない — N-124）。
        self.help_action = self.filter_edit.addAction(
            QIcon(), QLineEdit.TrailingPosition
        )
        set_icon(self.help_action, "help-circle")
        # 検索欄内の図像は Tab 巡回に乗らない（QLineEdit の内部ボタンは
        # NoFocus）ので、検索欄にフォーカスがある間だけ効くキーを対にする
        # （F1 は窓全体の操作ガイド）。表 ``SHORTCUTS`` の行と同期。
        self.help_action.setShortcut(QKeySequence("Ctrl+/"))
        self.help_action.setShortcutContext(Qt.WidgetShortcut)
        self.help_action.setToolTip(
            with_key_hint(
                t("viewer.post_grid.filter_help_tooltip"),
                "viewer.post_grid.filter_help_tooltip",
            )
        )
        self.help_action.triggered.connect(self._on_help_triggered)

    # -- 意図の送出（``Signal.emit`` を直に繋がない口だけメソッドにする）

    def _on_name_chip_clicked(self, _checked: bool = False) -> None:
        self.mode_clicked.emit("name")

    def _on_body_chip_clicked(self, _checked: bool = False) -> None:
        self.mode_clicked.emit("body")

    def _on_ai_chip_clicked(self, _checked: bool = False) -> None:
        self.ai_chip_clicked.emit()

    def _on_help_triggered(self, _checked: bool = False) -> None:
        self.help_clicked.emit()

    # -- 表示の反映

    def apply_modes(
        self,
        *,
        ai_active: bool,
        overlay_active: bool,
        body_usable: bool,
        body_mode: bool,
    ) -> None:
        """現在の検索次元を 名前 / 本文 / AIタグ チップへ映す。

        本文 lights up only for a pure body-scoped query; any bare term means
        the default name search is (also) engaged.  When an AI query is
        *dominating* the grid (UIレビュー #10) the AIタグ chip is the lit one
        and 名前/本文 are cleared — the plain filter box is then only a name
        overlay on the AI hits, so lighting 名前 would misname the current
        search dimension.
        """
        self.mode_chip_name.setChecked(not body_mode and not ai_active)
        self.mode_chip_body.setChecked(body_mode)
        if self.mode_chip_ai is not None:
            self.mode_chip_ai.setChecked(ai_active)
        # UIレビュー 2026-08-28 N-05: AI 検索中の 名前 / 本文 チップは押しても
        # **絶対に点灯しない**（上の 2 行が強制消灯する）のに活性チップと同じ
        # 見た目で並んでいた＝壊れたボタン。しかも「本文」押下は検索欄を
        # ``body:…`` へ書き換えるが、AI 結果に対して ``body:`` 項は
        # ``_general_filter_terms`` が捨てるので黙って無効化される。既存の不変
        # 条件（AI 中は plain モードに入れない）を**見た目へ一致させるだけ**で、
        # 挙動は変えない。
        self.mode_chip_name.setEnabled(not ai_active)
        self.mode_chip_body.setEnabled(body_usable)
        # UIレビュー #9: while AI results are shown the 名前 chip's match surface
        # narrows to name/title only — say so in its tooltip (restored to the
        # full-surface wording once the AI query clears).  無効時は「なぜ押せ
        # ないか」も同じツールチップで答える（N-05）。
        self.mode_chip_name.setToolTip(
            t("viewer.toolbar.mode_name_tooltip_ai") if ai_active
            else t("viewer.toolbar.mode_name_tooltip")
        )
        if ai_active:
            body_tip = t("viewer.toolbar.mode_body_tooltip_ai")
        elif overlay_active:
            body_tip = t("viewer.toolbar.mode_body_tooltip_overlay")
        else:
            body_tip = t("viewer.toolbar.mode_body_tooltip")
        self.mode_chip_body.setToolTip(body_tip)
        # UIレビュー 07-25 #14: the box next to the chips is NOT an AI-tag input
        # while an AI query owns the grid — it only narrows the AI hits by name.
        # The placeholder is the one always-visible surface, so it has to say so
        # (the tooltip / 0 件カード were already split by #9).  Restored to the
        # normal wording the moment the AI query clears.
        self.filter_edit.setPlaceholderText(
            t("viewer.post_grid.filter_placeholder_ai") if ai_active
            else t("viewer.post_grid.filter_placeholder")
        )


def build_prefix_completer(line_edit: QLineEdit) -> QCompleter:
    """Offer ``field:`` prefixes as the user types the first token (1-5c).

    A ``QCompleter`` over the recognised field prefixes with
    ``MatchStartsWith`` only surfaces while the current text is a prefix of
    a field name — i.e. while typing the leading token — so it doesn't
    intrude once a full term is being written.  The dropdown shows a short
    description (DisplayRole) but inserts only the bare ``field:`` prefix
    (EditRole), so a picked suggestion leaves the value to be typed.

    行・説明・AI ゲート（rating:/score: は tags.db 前提 — AI パック無効
    時は候補に出さない。``filter_query`` は引き続き受理するが inert）は
    条件次元レジストリの接頭辞台帳
    :data:`~.search_dimensions.TOKEN_FIELDS` 由来 — 構文ヘルプの軸一覧と
    同じ行・同じ文言が並ぶ（提案2 第1段。台帳が
    :data:`~.filter_query._FILTER_FIELD_GETTERS` /
    :data:`~.filter_query._CONTROL_FIELDS` を覆うことは
    ``tests/test_viewer_search_dimensions.py`` が機械検証）。
    """
    model = QStandardItemModel(line_edit)
    ai_on = ai_pack.available()
    for tok in token_fields(ai=ai_on):
        prefix = f"{tok.field}:"
        desc = token_description(tok, ai=ai_on)
        item = QStandardItem(f"{prefix}  — {desc}" if desc else prefix)
        # The bare prefix rides in UserRole: it's both the match target and
        # the inserted completion (completionRole below), so a picked
        # suggestion inserts only "body:", never the description text.
        item.setData(prefix, Qt.UserRole)
        model.appendRow(item)
    completer = QCompleter(model, line_edit)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setCompletionRole(Qt.UserRole)
    completer.setFilterMode(Qt.MatchStartsWith)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    line_edit.setCompleter(completer)
    return completer


# ------------------------------------------------------ filter-syntax help

class FilterHelpPopups:
    """検索欄の構文ヘルプ 2 面の寿命管理（Qt ウィジェットではなく所有者）。

    図像から開く**全文**（``Qt.Popup`` — クリックアウェイで閉じる）と、
    初回フォーカスの**短縮版**（``Qt.ToolTip`` — 入ったばかりの検索欄から
    フォーカスを奪わない）の 2 面。**不変**: どちらも TOP-LEVEL なので、
    非可視の旧インスタンスは開き直すときに ``deleteLater`` し、畳むときも
    close + ``deleteLater`` する（``Qt.Popup`` はクリックアウェイで hide
    されるだけなので、参照を差し替えるだけでは開くたびに親の子として
    溜まる）。
    """

    def __init__(self, *, parent: QWidget, anchor: QWidget) -> None:
        self._parent = parent
        self._anchor = anchor
        self.popup: QFrame | None = None
        self.auto: QFrame | None = None

    def _build_frame(
        self, flags: Qt.WindowFlags, *, brief: bool = False,
    ) -> QFrame:
        """Construct the shared filter-syntax cheatsheet frame.

        *brief* は N-116 の減量版（自動表示側だけ）: 実測 382×415px のパネル
        が中央グリッドに自動で被さっていたので、初回向けの 3 行 + ヘルプ
        図像への誘導 + **閉じ方**の 1 行に落とす。図像から開く全文はそのまま。
        """
        popup = QFrame(self._parent, flags)
        # このペインには補完器の ``Qt.Popup`` も居るので、構文ヘルプの 2 面
        # （図像から開く全文 / 初回自動の短縮版）だけを名前で選べるようにする
        # — 「開くたびに溜まらない」の回帰テストが数える対象（項目 #201）。
        popup.setObjectName(FILTER_HELP_POPUP_NAME)
        popup.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(10, 8, 10, 8)
        # 本文は条件次元レジストリが生成（軸の一覧 = 接頭辞台帳、静的説明 =
        # i18n 断片）。AI パック無効時は rating:/score: 行と AIタグへの言及を
        # 含まない（存在しない機能を説明しない — 従来の _free 変種と同じ）。
        # 短縮版は軸の一覧を持たないので AI 可用性に依存しない。
        label = QLabel(
            filter_help_brief_html() if brief
            else filter_help_html(ai=ai_pack.available())
        )
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setMaximumWidth(360)
        layout.addWidget(label)
        return popup

    def toggle_full(self) -> None:
        """Toggle a lightweight, frameless filter-syntax help popup.

        Clicking the help glyph again (or focusing away from the popup)
        closes it.  The popup is a borderless ``QFrame`` with a rich-text
        label — no modal dialog — so it never steals the header layout's
        width.
        """
        existing = self.popup
        if existing is not None:
            if existing.isVisible():
                existing.close()
                return
            # 非可視の旧インスタンスは明示破棄する（レビュー 2026-09-03 項目
            # #201）。``Qt.Popup`` はクリックアウェイで hide されるだけなので、
            # 参照を差し替えるだけでは開くたびにこのペインの子として溜まる
            # — ``advanced_search._show_search_cheatsheet``（項目#155）が
            # 同じ形の対で、こちらが手本を写し損ねていた側。
            existing.deleteLater()
            self.popup = None
        popup = self._build_frame(Qt.Popup)
        self.popup = popup
        popup.adjustSize()
        # 検索欄の右端（ヘルプ図像の真下）に掛ける。
        popup.move(popover_position(self._anchor, popup.size(), align="right"))
        popup.show()

    def show_brief(self) -> None:
        """初回フォーカスの自動表示 — **短縮版**（N-116）.

        表示済みフラグ（``PostGrid._filter_help_autoshown``）の
        ``viewer_state.json`` 永続化は意図的に**しない**: 教示機会を恒久的に
        失う副作用があり、割り込み感の主因は「毎回出ること」ではなく「初回に
        大きすぎること」だという検証結果に従う（減量だけで解消する）。
        """
        popup = self._build_frame(
            Qt.ToolTip | Qt.FramelessWindowHint, brief=True,
        )
        popup.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.auto = popup
        popup.adjustSize()
        popup.move(popover_position(self._anchor, popup.size()))
        popup.show()

    def hide_brief(self) -> None:
        popup = self.auto
        if popup is not None:
            # 自動表示は 1 セッション 1 回だが、``_filter_help_autoshown`` を
            # 跨ぐ再表示（履歴復元・別ルート）でも子が残らないよう、閉じると
            # 同時に破棄まで予約する（項目 #201 — 図像側と同じ作法）。
            popup.close()
            popup.deleteLater()
            self.auto = None

    def close_all(self) -> None:
        """ペインを閉じるときに 2 面とも畳む（TOP-LEVEL は自分では閉じない）."""
        for attr in ("popup", "auto"):
            popup = getattr(self, attr)
            if popup is not None:
                popup.close()
                popup.deleteLater()
                setattr(self, attr, None)


# ------------------------------------------------------------- 表示 popover

class DisplayOptions(QWidget):
    """The pane's display options, seated inside the 「並び・表示」 popover.

    Until UIレビュー 08-28 N-24 these lived behind their own 「⋯」 toolbar
    button right next to 「並び・表示」, whose tooltips both started with
    「表示オプション(」 while their contents did not overlap — and the same
    「⋯」 figure meant something different again in the 情報パネル and in the
    ナビレール.  ⋯ now means exactly one thing (**that pane's display
    options**), so the grid's display options move in with the pane's other
    display controls and the second trigger is gone.

    ``exclude_thumb_check`` is a ``QCheckBox``; ``setChecked`` / ``isChecked``
    / ``toggled`` are the same three members ``main_window`` and the state
    round-trip already used against the older ``QAction``.  The NSFW band
    stays a ``QMenu`` of exclusive checkable ``QAction``s — it is a 3-way
    radio that reads better as a submenu than as a third combo row.

    UIレビュー 07-25 #40: 「ロックありのみ」 is a *query* axis and lives in the
    フィルタ popover, not here.
    """

    #: ``#thumb#`` 除外トグルが変わった。
    exclude_thumb_toggled = Signal(bool)
    #: 年齢区分バンドが選ばれた（``nsfw_filter.LABEL_KEYS`` の鍵）。
    nsfw_band_selected = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        ai_available: bool,
        exclude_thumb_checked: bool,
        nsfw_label_keys: Mapping[str, str],
    ) -> None:
        super().__init__(parent)
        box_lay = QVBoxLayout(self)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(6)

        self.exclude_thumb_check = QCheckBox(t("viewer.post_grid.exclude_thumb"))
        self.exclude_thumb_check.setToolTip(
            t("viewer.post_grid.exclude_thumb_tooltip")
        )
        self.exclude_thumb_check.setChecked(exclude_thumb_checked)
        self.exclude_thumb_check.toggled.connect(self.exclude_thumb_toggled.emit)
        box_lay.addWidget(self.exclude_thumb_check)

        #: AI 機能パック無効（素の配布）では席そのものを作らない。
        self.nsfw_btn: QPushButton | None = None
        self.nsfw_menu: QMenu | None = None
        self.nsfw_actions: dict[str, QAction] = {}
        self.nsfw_action_group: QActionGroup | None = None

        # NSFW view suppression (item 2-1) — a persistent VIEW setting, exclusive
        # radio band.  Disabled with a reason when no tags.db supplies ratings.
        # AI 機能パック無効（素の配布）ではサブメニュー自体を作らない — 代表
        # レーティングの供給源（tags.db）ごと存在しない機能を UI に出さない。
        # 下流は ``getattr(self, "nsfw_menu", None)`` ガード済み（_sync_nsfw_menu）。
        if ai_available:
            self.nsfw_btn = QPushButton(t("viewer.post_grid.nsfw_menu"), self)
            self.nsfw_menu = QMenu(self.nsfw_btn)
            self.nsfw_btn.setMenu(self.nsfw_menu)
            box_lay.addWidget(self.nsfw_btn)
            # Exclusive QActionGroup: without it, re-clicking the already-checked
            # radio unchecks it (checkable QAction toggles) and — because
            # ``set_hide_nsfw`` early-returns on an unchanged value without
            # re-syncing — the whole band renders with no item checked.  The
            # exclusive group makes a click on the checked action a no-op.
            self.nsfw_action_group = QActionGroup(self)
            self.nsfw_action_group.setExclusive(True)
            # ラベルキーの表は ``nsfw_filter.LABEL_KEYS`` 1 つ（席のラベルも
            # 同じ表から引く — 手書きの複製を作らない）。
            for key, label_key in nsfw_label_keys.items():
                act = QAction(t(label_key), self)
                act.setCheckable(True)
                act.triggered.connect(
                    lambda _=False, k=key: self.nsfw_band_selected.emit(k)
                )
                self.nsfw_action_group.addAction(act)
                self.nsfw_menu.addAction(act)
                self.nsfw_actions[key] = act


class ViewPopover(QFrame):
    """「並び・表示」 popover の枠: sort / layout / thumbnail size + 表示オプション。

    3 行（並び順 → 表示形式 → サムネイルサイズ）は右ペインと共通のファクトリ
    （``ChildrenGrid._build_view_settings_rows``）が組むので、枠は *build_rows*
    コールバックへ自分のレイアウトを渡すだけ。下段の表示オプションも
    *build_options* が返したウィジェットを座らせるだけで、中身は知らない。

    NOTE ペイン表示トグル 3 つ（ナビレール / プレビュー列 / 情報パネル）は
    かつてここに ☑ として同居していたが、UIレビュー 2026-08-28 N-134 の
    裁定で**取り除いた**: 同じツールバーの 2cm 右に常設の 3 ボタンが
    並んでいて、同一バー上に同じ 3 機能が二重に見えていた。導線は
    ツールバーボタン・表示メニュー・F6/F7/F8 の 3 系統に残るので失われず、
    checked 同期も窓側の ``_set_*_checks`` に一元化されているため
    削除側の片側欠落も起きない。**ここへ戻さないこと** — 戻すなら
    常設ボタン側を外して 1 面に絞る形で検討する。
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        build_rows: Callable[[QVBoxLayout], None],
        build_options: Callable[[QWidget], QWidget],
    ) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("toolbarPopover")
        pop_lay = QVBoxLayout(self)
        pop_lay.setContentsMargins(10, 8, 10, 8)
        pop_lay.setSpacing(6)
        build_rows(pop_lay)
        # 区切り線は qss の `#popoverSeparator`（border トークン）に描かせる
        # — 素の HLine は WindowText で描かれ、本文と同じ濃さの線になる。
        opt_sep = QFrame()
        opt_sep.setObjectName("popoverSeparator")
        opt_sep.setFrameShape(QFrame.NoFrame)
        opt_sep.setFixedHeight(1)
        pop_lay.addWidget(opt_sep)
        pop_lay.addWidget(build_options(self))

    def popup_at(self, anchor: QWidget) -> None:
        """*anchor* の右揃えで開く。"""
        self.adjustSize()
        self.move(popover_position(anchor, self.size(), align="right"))
        self.show()


def make_view_button(on_click: Callable[[], None]) -> QToolButton:
    """「並び・表示」 popover の引き金ボタン。

    UIレビュー 07-25 #100: the three toolbar popover triggers (フィルタ /
    並び・表示 / ⋯) are the same kind of control, so they now share one
    style — icon-only + tooltip.  This one carried a text label, which
    both broke the row's rhythm and competed for the width #3 gives back
    to the search field.  ``setText`` stays for the accessible name.
    UIレビュー 07-25 #24: renamed 「表示」 → 「並び・表示」 (the menu bar's
    表示(V) is a different menu) and the tooltip now enumerates what is
    inside, so neither entry point is searched in vain.
    """
    btn = QToolButton()
    btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
    set_icon(btn, "sort-display")
    btn.setText(t("viewer.toolbar.view_label"))
    btn.setFixedWidth(NAV_BUTTON_WIDTH)
    btn.setToolTip(t("viewer.toolbar.view_tooltip"))
    btn.clicked.connect(lambda _=False: on_click())
    return btn


# ------------------------------------------------- filter popover: 投稿日の範囲

class DateRangeEditors:
    """投稿日行の「範囲」エディタ 3 点（開始 / 〜 / 終了）の束。"""

    __slots__ = ("date_from", "separator", "date_to")

    def __init__(
        self, date_from: QDateEdit, separator: QLabel, date_to: QDateEdit,
    ) -> None:
        self.date_from = date_from
        self.separator = separator
        self.date_to = date_to


def build_date_range_editors(
    date_row: QHBoxLayout, *, on_changed: Callable[[QDate], None],
) -> DateRangeEditors:
    """投稿日行の「範囲」指定エディタ（開始 〜 終了）を *date_row* へ足す.

    投稿日だけが行内に追加のインラインコントロールを持つ軸なので、
    台帳の生成ループからは外して 1 関数に閉じ込める（この形が
    あるために単一の ``QFormLayout`` では組めない — N-99 の改善案が
    ``QFormLayout`` 化を退けた理由そのもの）。
    """
    date_from = QDateEdit()
    date_from.setCalendarPopup(True)
    date_from.setDisplayFormat("yyyy-MM-dd")
    date_from.setDate(QDate.currentDate().addMonths(-1))
    date_from.dateChanged.connect(on_changed)
    date_from.setVisible(False)
    date_row.addWidget(date_from)
    separator = QLabel("〜")
    separator.setVisible(False)
    date_row.addWidget(separator)
    date_to = QDateEdit()
    date_to.setCalendarPopup(True)
    date_to.setDisplayFormat("yyyy-MM-dd")
    date_to.setDate(QDate.currentDate())
    date_to.dateChanged.connect(on_changed)
    date_to.setVisible(False)
    date_row.addWidget(date_to)
    # 相互クランプ(項目188): 開始 > 終了の逆転入力をウィジェットレベルで
    # 発生させない。片方を動かすともう片方の可動域が追従し、逆転させる
    # 変更は Qt が境界値へ丸める(復元経路で保存済みの逆転値が来ても同様に
    # 正規化される)。保険として preset_range 側にも lo>hi の swap がある。
    date_to.setMinimumDate(date_from.date())
    date_from.setMaximumDate(date_to.date())
    date_from.dateChanged.connect(date_to.setMinimumDate)
    date_to.dateChanged.connect(date_from.setMaximumDate)
    return DateRangeEditors(date_from, separator, date_to)


# -------------------------------------------------------------- the toolbar

class GridToolbar(QWidget):
    """The window-level unified toolbar.

    Built **parentless**; ``main_window._build_ui`` mounts it above the
    splitter, which reparents it into the window.  Height comes from the
    shared ``TOOLBAR_HEIGHT`` token; the bar reads as one strong strip —
    ``bg_surface`` fill + a 1px bottom border — via the ``#unifiedToolbar``
    QSS block (qss.py).

    The old Row 1 (nav + breadcrumb), Row 2 (filter / recursive / sort) and
    Row 3 (⋯ options / 表示 / size slider) all live on this ONE 40px bar.
    ``PostGrid`` still OWNS the widgets and their state — the toolbar is
    only their seat — so every existing signal path, persistence round-trip
    and programmatic restore (nav history / saved searches) updates the
    toolbar display for free (two-way sync by construction).
    """

    back_clicked = Signal()
    forward_clicked = Signal()
    up_clicked = Signal()
    reload_clicked = Signal()
    change_root_clicked = Signal()
    filter_clicked = Signal()

    def __init__(
        self, *, search_field: QWidget, view_button: QToolButton,
    ) -> None:
        super().__init__()
        self.setObjectName("unifiedToolbar")
        # A plain QWidget doesn't paint stylesheet backgrounds without this.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(TOOLBAR_HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(4)

        # Nav cluster.  Back / forward are QToolButtons (not QPushButtons) so
        # a *long press* can drop a history dropdown (B-6, wired by
        # main_window via ``attach_history_menus``) while a normal short
        # click still fires the navigate signal — QToolButton.DelayedPopup
        # gives exactly that split.  Hide the built-in menu arrow so the
        # glyph stays centred.
        self.back_btn = self._nav_btn(
            lay, "arrow-left", "viewer.post_grid.back_tooltip", menu_host=True,
        )
        self.back_btn.clicked.connect(self.back_clicked.emit)
        self.forward_btn = self._nav_btn(
            lay, "arrow-right", "viewer.post_grid.forward_tooltip",
            menu_host=True,
        )
        self.forward_btn.clicked.connect(self.forward_clicked.emit)
        self.up_btn = self._nav_btn(
            lay, "arrow-up", "viewer.post_grid.up_tooltip",
        )
        self.up_btn.clicked.connect(self.up_clicked.emit)
        # ↻ のツールチップは並び順で変わる（ホストの ``_sync_reload_tooltip``
        # が構築直後と並び変更のたびに差し替える）ので、ここでは付けない。
        self.reload_btn = self._nav_btn(lay, "refresh", "")
        self.reload_btn.clicked.connect(self.reload_clicked.emit)

        # Clickable breadcrumb — the stretch member that absorbs spare width.
        # Clicking an ancestor segment re-emits ``breadcrumb_navigate``; the
        # trailing count "(N 件)" rides along as a non-clickable suffix.
        # Reports a tiny minimum width so a long path never widens the bar.
        # PostGrid keeps updating it (set_root / scan handlers / rebuild) —
        # the update paths are unchanged, only the seat moved.
        self.breadcrumb = BreadcrumbBar()
        lay.addWidget(self.breadcrumb, 1)

        # Icon-only, like the nav buttons — the full-text label used to eat
        # the width the breadcrumb needs to show the current folder (UIレビュー
        # #2).  Same wording as the File menu's フォルダを開く… (A04), carried
        # by the tooltip.
        self.change_root_btn = self._nav_btn(
            lay, "folder-open", "viewer.post_grid.open_folder_tooltip",
        )
        self.change_root_btn.clicked.connect(self.change_root_clicked.emit)

        # Global search field (mode chips + filter box + syntax help).
        # Stretch 1, like the breadcrumb: 07-25 #3 raised the cap to 500px on
        # the stated premise that "the two share" the bar's spare width, but
        # the field was added with the default stretch 0 and never grew past
        # ~294px — leaving ``filter_edit`` at 153px, narrower than the 209px
        # its own placeholder needs (UIレビュー 2026-08-28 N-108).  The
        # existing min/max still bound it.
        self.search_field = search_field
        lay.addWidget(search_field, 1)

        # フィルタ popover trigger (種別 / 投稿日 / ★ / あとで見る — the
        # adaptive filter axes).  The popover itself is owned by the host.
        self.filter_btn = self._nav_btn(
            lay, "filter", "viewer.post_grid.filter_bar_tooltip",
        )
        self.filter_btn.clicked.connect(self.filter_clicked.emit)

        # 「並び・表示」 popover — sort / layout / thumbnail size **and** the
        # display options that used to sit behind a second ⋯ button next to it
        # (UIレビュー 08-28 N-24).  Two triggers whose tooltips both began with
        # 「表示オプション(」 stood side by side in one toolbar, so the same
        # figure 「⋯」 pointed at three unrelated things across the window.
        self.view_button = view_button
        lay.addWidget(view_button)

        # Pane visibility toggles (折り畳み導線): an always-visible,
        # mouse-discoverable seat for the F7 / F6 / F8 pane toggles at the
        # bar's right end (VS Code 通念).  PostGrid owns the chrome only —
        # the window wires ``toggled`` to the real show/hide + persistence
        # and keeps ``setChecked`` in sync with every other entry point
        # (メニュー / 表示 popover / shortcuts / splitter drags).
        # 区切り線は QFrame の VLine（= WindowText 描画 = 本文と同じ濃さ）では
        # なく qss の `#toolbarSeparator`（border トークン）に描かせる。
        # NoFrame にしないと Qt の枠描画と QSS の塗りが二重になる。
        pane_sep = QFrame()
        pane_sep.setObjectName("toolbarSeparator")
        pane_sep.setFrameShape(QFrame.NoFrame)
        pane_sep.setFixedWidth(1)
        lay.addWidget(pane_sep)

        # 並びは画面上の配置と同じ 左→右: ナビレール / プレビュー列 / 情報パネル。
        self.nav_rail_btn = self._pane_btn(
            lay, "panel-left", "viewer.main_window.nav_rail_toggle_tooltip",
        )
        self.preview_btn = self._pane_btn(
            lay, "panel-preview", "viewer.main_window.preview_toggle_tooltip",
        )
        self.info_panel_btn = self._pane_btn(
            lay, "panel-right", "viewer.main_window.info_panel_toggle_tooltip",
        )

    @staticmethod
    def _nav_btn(
        lay: QHBoxLayout,
        icon_name: str,
        tooltip_key: str,
        *,
        menu_host: bool = False,
    ) -> QToolButton:
        btn = QToolButton()
        set_icon(btn, icon_name)
        if tooltip_key:
            btn.setToolTip(t(tooltip_key))
        btn.setFixedWidth(NAV_BUTTON_WIDTH)
        if menu_host:
            btn.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        lay.addWidget(btn)
        return btn

    @staticmethod
    def _pane_btn(
        lay: QHBoxLayout, icon_name: str, tooltip_key: str,
    ) -> QToolButton:
        btn = QToolButton()
        btn.setCheckable(True)
        set_icon(btn, icon_name)
        btn.setFixedWidth(NAV_BUTTON_WIDTH)
        btn.setToolTip(t(tooltip_key))
        btn.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(btn)
        return btn
