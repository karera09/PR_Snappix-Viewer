"""空状態オーケストレータ — 「どの席が主案内を持つか」をウィンドウで裁定する.

**なぜウィンドウレベルなのか**: 空状態の判定は
3 つのウィジェットに分散していて、互いを見られない —
:meth:`post_grid.PostGrid._empty_state_kind` (11 種) /
:class:`content_view.ContentView` のプレースホルダ 4 種 /
:class:`file_list.FileListView` の 3 種。「**ボタンを持つ案内カードは 1 画面に
1 枚**」をアクションだけで守っても**メッセージ側の重複と文体の不揃い**が残る。
どの席が主案内を持つかを誰かが決めなければならないので、その裁定だけを
**ウィンドウレベルの純関数**として置く。

**射程**: このモジュールは「**どの席がどの声量で喋るか**」だけを決める。
各席が「**何と**喋るか」は従来どおりその席が持つ:

* グリッドの 11 種分類（文言・ボタン・クリック先が同一分岐から出る資産）は
  **そのまま**入力
  (:attr:`EmptyStateInput.grid_kind`) として使い、割当が ``PRIMARY`` の
  ときにその 11 種がそのまま描かれる。``PRIMARY`` でないときは黙る —
  この裁定は ``window_status.WindowStatus.sync_centre_placeholder`` が
  ``ChildrenGrid.refresh_empty_state(allowed=...)`` で渡す（:attr:`EmptyStatePlan.grid`
  を production が読まなければ「``PRIMARY`` は 1 席」は単体テストの中でしか
  成立しない）。
* 従属面（プレビュー列・右情報パネル）は自前で判定するのをやめ、
  :class:`PaneGuidance` の ``message_key`` を描くだけになる。

**3 つの役割**:

``PRIMARY``
    見出し + 本文 + アクションボタンを持つ案内カード。
    「1 画面に 1 枚」を構造で保証する — リゾルバは**必ず 1 席以下**にしか
    ``PRIMARY`` を割り当てない。
``SECONDARY``
    アイコン無し・1 行・控えめの説明。「ここには出す物が無い」
    という事実だけを述べ、主案内と競合しない。命令形（「〜してください」）
    は使わない — 選べる物が無い席で「選べ」と言わないため。
``NONE``
    何も描かない。その席が空状態でない（実プレビュー中・一覧に中身がある）
    か、畳まれていて見えないか、**その席自身が持つ空状態**（右ペインの
    スキャン失敗カード等）を尊重する場合。

**畳まれた席は主になれない**という 1 規則が「最大化中・未選択で、幅 0 の
グリッドを指す案内が出る」ことを構造的に防ぐ: グリッド席が畳まれていれば
``PRIMARY`` は自動でプレビュー列へ移り、ウィンドウは「分割ビューに戻す」導線
付きのカードを出す。

純関数なので**組み合わせを単体テストで固定できる**
（``tests/test_viewer_empty_state_resolver.py`` の網羅表）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "EmptyAction",
    "EmptyState",
    "EmptyStateInput",
    "EmptyStatePlan",
    "PaneGuidance",
    "Role",
    "resolve_empty_state",
]


@dataclass(frozen=True)
class EmptyAction:
    """空状態カードのボタン 1 つ — ラベル + 押下先 (+ 任意のツールチップ).

    「何を出すか」と「押されたら何をするか」を **1 つの値** に束ねるための型。
    ラベルと振る舞いを別々の分岐で持つと、片側だけ増えた分岐が「押せるのに
    何も起きないボタン」を作る。ボタンは 0 個以上の**リスト**で表す — AI 検索の
    0 件カードは engaged な軸ごとに 1 つ、最大 9 個の緩和ボタンを並べる。

    Qt 非依存（このモジュールの他の要素と同じく純データ）。
    """

    label: str
    callback: Callable[[], None]
    tooltip: str = ""


class Role(Enum):
    """1 つの席（グリッド / プレビュー列 / 右情報パネル）への割当。"""

    NONE = "none"
    SECONDARY = "secondary"
    PRIMARY = "primary"


class EmptyState(Enum):
    """ウィンドウ全体としての「今どういう空きか」。"""

    #: 空状態ではない（実プレビューが出ていて、グリッドにも項目がある）。
    NONE = "none"
    #: 空ライブラリ + ナビ履歴なし = 初回起動・ルート直開き。
    FIRST_RUN = "first_run"
    #: ドリルインした先が空フォルダ（履歴あり）。
    EMPTY_FOLDER = "empty_folder"
    #: グリッドに項目はあるが何も選ばれていない（分割ビュー）。
    NO_SELECTION = "no_selection"
    #: 最大化中で何も選ばれていない — グリッドは幅 0 で見えない。
    NO_SELECTION_MAXIMIZED = "no_selection_maximized"
    #: 絞り込み・検索・一覧の条件が全項目を隠した（0 件）。
    NO_MATCH = "no_match"
    #: 走査・検索がまだ着地していない過渡の 0 件。
    SEARCHING = "searching"
    #: ルートの走査に失敗した。
    SCAN_ERROR = "scan_error"
    #: 選択済みだが中央に出せるプレビューが無い（代表画像なし等）。
    NO_PREVIEW = "no_preview"


#: グリッドが「まだ答えが出ていない」ことを言っている過渡の分類。
TRANSIENT_GRID_KINDS = frozenset(
    {"searching", "curation_loading", "recent_loading"}
)

#: グリッドが**何も描かない**分類（AI 検索パネル自身のカードが主案内を持つ /
#: 走査中の歴史的な空白）。この席は主にも副にもなれない。
SILENT_GRID_KIND = "none"

#: 「探し方（条件）が悪くて 0 件」であって「中身が無い」わけではない分類。
#: 従属面の文言を「検索条件に一致する項目が…」側にするかの判定に使う。
NARROWED_GRID_KINDS = frozenset(
    {
        "filtered", "filtered_shallow", "recent_filtered", "curation_filtered",
        # AI 検索が着地して 0 件 — 「条件で消えた」側（空状態の
        # オーナーはグリッドなので、この分類が席の裁定にも届く）。
        # ``advanced_error`` は「探し方が悪い」ではないので入れない。
        "advanced_zero",
    }
)

# ------------------------------------------------------------ i18n キー

#: 従属プレビュー列（SECONDARY）の 1 行。状態に依らず「ここには出す物が無い」
#: という事実のみ — 主案内はグリッド側が持つ。
PREVIEW_SECONDARY_KEY = "viewer.content_view.empty_quiet"

#: 右情報パネル（SECONDARY）の 1 行。3 択:
#: * 選べる物がある（未選択）→ 従来の「選ぶと一覧表示されます」
#: * 条件で 0 件 → 「検索条件に一致する項目がないため…」
#: * そもそも選べる物が無い（空フォルダ・空ライブラリ・走査中・失敗）→
#:   命令形を使わない静音文言（選べる物が無いのに「選べ」と言わない）
INFO_UNSELECTED_KEY = "viewer.file_list.empty_unselected"
INFO_NO_HITS_KEY = "viewer.file_list.empty_search_no_hits"
INFO_NO_ITEMS_KEY = "viewer.file_list.empty_no_items"


@dataclass(frozen=True)
class EmptyStateInput:
    """リゾルバへの入力（= ウィンドウが持つ観測値だけ・Qt 非依存）.

    :param library_empty: :meth:`post_grid.PostGrid.is_library_empty` —
        現ルートが子エントリ 0 で settle した（検索非アクティブ・走査失敗なし）。
    :param grid_kind: :meth:`post_grid.PostGrid._empty_state_kind` の値、
        またはタイルがあるときは ``""``。11 種の分類そのものは移設せず、
        ここでは「過渡か / 条件由来か / 無音か」の 3 点だけを読む。
    :param has_selection: 左グリッドでフォルダ・投稿を選択中か。
    :param preview_is_placeholder: プレビュー列がプレースホルダを表示中か
        （実プレビューを踏み潰さないための必須ガード）。
    :param grid_seat_collapsed: グリッド席が幅 0（プレビュー最大化 / ハンドル
        を手で引き切った）— :meth:`window_status.WindowStatus.is_grid_seat_collapsed`。
    :param preview_seat_collapsed: プレビュー列が幅 0（F6 で畳んだ）。
    :param info_panel_visible: 右情報パネルが表示中（F8）。
    :param has_history: ナビ履歴がある = 初回起動ではない。
    :param scan_error: ルート走査に失敗した（I01 — グリッドが自前の
        ⚠ + [再試行] カードを持つ）。
    """

    library_empty: bool = False
    grid_kind: str = ""
    has_selection: bool = False
    preview_is_placeholder: bool = True
    grid_seat_collapsed: bool = False
    preview_seat_collapsed: bool = False
    info_panel_visible: bool = True
    has_history: bool = False
    scan_error: bool = False


@dataclass(frozen=True)
class PaneGuidance:
    """1 席への割当 — 役割と、その席が描くべき文言の i18n キー。

    ``message_key`` が空なのは ``role is Role.NONE`` のときと、
    ``PRIMARY`` を割り当てられた席が**自前の文言体系**（グリッドの 11 種 /
    ContentView の各カード）を持つとき。
    """

    role: Role = Role.NONE
    message_key: str = ""

    def __bool__(self) -> bool:  # 「この席は何か描くか」
        return self.role is not Role.NONE


@dataclass(frozen=True)
class EmptyStatePlan:
    """リゾルバの出力 — 状態 1 つと 3 席への割当。

    3 席すべてが適用点（``window_status.WindowStatus.sync_centre_placeholder``）で消費される:
    ``grid`` は ``ChildrenGrid.refresh_empty_state(allowed=...)``、``preview``
    は ``ContentView`` の各カード、``info`` は ``InfoPanel``。
    """

    state: EmptyState
    grid: PaneGuidance = PaneGuidance()
    preview: PaneGuidance = PaneGuidance()
    info: PaneGuidance = PaneGuidance()

    def primary_seat(self) -> str:
        """``PRIMARY`` を持つ席名（``""`` = どこにも無い）— テスト・診断用。"""
        for name in ("grid", "preview", "info"):
            if getattr(self, name).role is Role.PRIMARY:
                return name
        return ""


def _classify(inp: EmptyStateInput) -> EmptyState:
    """観測値 → :class:`EmptyState`（席の可視性は見ない = 「何が起きているか」）."""
    if inp.scan_error:
        return EmptyState.SCAN_ERROR
    if inp.library_empty:
        # 履歴が無い = 起動直後にこのルートへ着地した。ここが
        # 「ようこそ」の居場所。履歴があれば閲覧中に
        # 降りてきた空フォルダなので「上の階層へ」側。
        return (
            EmptyState.EMPTY_FOLDER if inp.has_history else EmptyState.FIRST_RUN
        )
    if _grid_zero(inp):
        if inp.grid_kind in TRANSIENT_GRID_KINDS or inp.grid_kind == SILENT_GRID_KIND:
            return EmptyState.SEARCHING
        return EmptyState.NO_MATCH
    # ここから下はグリッドに項目がある。
    if not inp.has_selection:
        return (
            EmptyState.NO_SELECTION_MAXIMIZED
            if inp.grid_seat_collapsed
            else EmptyState.NO_SELECTION
        )
    if inp.preview_is_placeholder:
        return EmptyState.NO_PREVIEW
    return EmptyState.NONE


def _grid_zero(inp: EmptyStateInput) -> bool:
    """グリッドが 0 タイルか（**何が起きているか**の判定用）.

    ``grid_kind`` が空文字 = タイルがある、という約束
    (:attr:`EmptyStateInput.grid_kind`)。``library_empty`` は ``grid_kind`` を
    経由せずに真になり得る（ウィンドウのハーネス経路）ので両方を見る。
    """
    return inp.library_empty or bool(inp.grid_kind)


def _grid_is_empty(inp: EmptyStateInput) -> bool:
    """グリッド席が**自分で何か描ける**か（0 タイル ∧ 黙る分類でない）.

    ``SILENT_GRID_KIND`` は「グリッドは何も描かない」という分類（AI 検索
    パネル自身のカードが主案内を持つ / 走査中の歴史的な空白）なので、0 タイル
    でも席としては描けない — 主案内はその分だけ隣の席へ移る。
    """
    return _grid_zero(inp) and inp.grid_kind != SILENT_GRID_KIND


def _info_message_key(state: EmptyState, inp: EmptyStateInput) -> str:
    if state in (EmptyState.NO_SELECTION, EmptyState.NO_SELECTION_MAXIMIZED):
        return INFO_UNSELECTED_KEY
    if state is EmptyState.NO_MATCH and inp.grid_kind in NARROWED_GRID_KINDS:
        return INFO_NO_HITS_KEY
    # 空フォルダ・空ライブラリ・走査中・走査失敗・NSFW 抑制など
    # 「選べる物がそもそも無い」側は命令形を使わない静音文言へ。
    return INFO_NO_ITEMS_KEY


#: 状態ごとの ``PRIMARY`` 席の優先順。先頭から見て**描ける席**が主になる。
#: 席が描けるかは :func:`_renderable` が決めるので、畳まれた席は自動で飛ばされ、
#: 右情報パネルが主案内を持つことは無い（従属面に固定）。
_PRIMARY_ORDER: dict[EmptyState, tuple[str, ...]] = {
    EmptyState.FIRST_RUN: ("grid", "preview"),
    EmptyState.EMPTY_FOLDER: ("grid", "preview"),
    EmptyState.NO_MATCH: ("grid", "preview"),
    EmptyState.SEARCHING: ("grid", "preview"),
    # 走査失敗の主案内（⚠ + [再試行]）はグリッドが持つが、**畳まれたら
    # プレビュー列へ移す**。ここに退避先が無いと、失敗表示が出たあとに E で
    # 最大化しただけで「失敗した事実」も「再試行」も画面から消え、静音 1 行の
    # 「プレビューする項目がありません」だけが残る（他の状態は全部この退避先を
    # 持っていて、SCAN_ERROR だけが欠けていた）。
    EmptyState.SCAN_ERROR: ("grid", "preview"),
    EmptyState.NO_SELECTION: ("preview",),
    EmptyState.NO_SELECTION_MAXIMIZED: ("preview",),
    # 選択済みでプレビューだけ出せない状態に「主案内」は要らない — 静音 1 行で
    # 足りる（ここで「グリッドから選べ」と言うと
    # 選択済みなのに選べと言う自己矛盾になる）。
    EmptyState.NO_PREVIEW: (),
    EmptyState.NONE: (),
}


def _renderable(seat: str, state: EmptyState, inp: EmptyStateInput) -> bool:
    """その席が今この状態について**何か描ける**か（見えているか + 空か）."""
    if state is EmptyState.NONE:
        return False
    if seat == "grid":
        return _grid_is_empty(inp) and not inp.grid_seat_collapsed
    if seat == "preview":
        return inp.preview_is_placeholder and not inp.preview_seat_collapsed
    # 右情報パネル: 選択中は一覧に中身があるか、ペイン自身が持つ空状態
    # （スキャン失敗の ⚠+[再試行] / 「このフォルダにファイルはありません」）が
    # 正しい。ウィンドウはそれを踏み潰さない。
    return inp.info_panel_visible and not inp.has_selection


def resolve_empty_state(inp: EmptyStateInput) -> EmptyStatePlan:
    """観測値 → 状態 + 3 席への役割割当（純関数）.

    不変条件（テストで固定）:

    * ``PRIMARY`` は多くとも 1 席（「案内カードは 1 画面に 1 枚」）。
    * 畳まれている / 非表示の席には ``PRIMARY`` も ``SECONDARY`` も割り当てない
      （見えない席に案内を置かない）。
    * 右情報パネルは決して ``PRIMARY`` にならない（従属面）。
    * 選択中は右情報パネルへ書き込まない（ペイン自身の空状態を尊重）。
    """
    state = _classify(inp)
    if state is EmptyState.NONE:
        return EmptyStatePlan(state)

    seats = {
        name: _renderable(name, state, inp)
        for name in ("grid", "preview", "info")
    }
    primary = ""
    for name in _PRIMARY_ORDER.get(state, ()):
        if seats.get(name):
            primary = name
            break

    info_key = _info_message_key(state, inp)
    roles: dict[str, PaneGuidance] = {}
    for name, ok in seats.items():
        if not ok:
            roles[name] = PaneGuidance()
            continue
        if name == primary:
            # 主案内の文言はその席自身の体系（グリッドの 11 種 / ContentView の
            # カード）が持つ — ここでキーを渡すと二重定義になる。
            roles[name] = PaneGuidance(Role.PRIMARY)
        elif name == "preview":
            roles[name] = PaneGuidance(Role.SECONDARY, PREVIEW_SECONDARY_KEY)
        elif name == "info":
            roles[name] = PaneGuidance(Role.SECONDARY, info_key)
        else:
            # グリッドは「描ける = 0 タイルで見えている」なら必ず主になる
            # （:data:`_PRIMARY_ORDER` の先頭）ので、ここへは来ない。従属の
            # 1 行に格下げする体系をグリッドは持たない（11 種がそのまま出る）
            # ため、来た場合は黙る方に倒す。
            roles[name] = PaneGuidance()
    return EmptyStatePlan(
        state, grid=roles["grid"], preview=roles["preview"], info=roles["info"]
    )
