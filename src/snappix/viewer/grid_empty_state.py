"""左グリッドの空状態を「値から値へ」決める純関数群（Qt 非依存）。

タイルが 0 枚のとき何を描くかは 3 つの問いに分かれる — **分類**（どの状況
なのか）、**文言**（どのカタログキーを並べるか）、**ボタン**（どの一手を
出すか）。この 3 つが別々の分岐として並走すると、片側だけ増えた分類が
「押せるのに何も起きない」/「押せるはずの手が出ない」を作る。ここでは 3 つ
とも同じ :class:`GridEmptyInput` 1 つから導く。

AI 検索の 0 件 / 失敗カード（``advanced_zero`` / ``advanced_error``）も同じ
流儀で持つ: :class:`AdvancedEmptyInput` から :func:`plan_relaxations` /
:func:`plan_error_actions` / :func:`advanced_message_keys` が決める。分類器
（:func:`classify`）から見ると中身は「供給側に訊く」2 種（:data:`SUPPLIED_KINDS`）
のままで、供給側 = ``advanced_search.AdvancedSearchController`` は観測値を束ねて
ここへ渡し、返ってきた動作 id を自分の ``_relax_*`` へ戻すだけの層になる。

**射程**: 決めるのは「分類・文言キー・ボタンの並び」まで。実際の描画は
``ChildrenGrid.refresh_empty_state`` → ``GalleryView.set_empty_state``、
席の裁定（どのペインが主案内を持つか）はウィンドウレベルの
:mod:`.empty_state` が引き続き持つ。

テストは ``tests/test_viewer_grid_empty_state.py``（QApplication 不要）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

__all__ = [
    "ACTION_IDS",
    "ADVANCED_ACTION_IDS",
    "ActionSpec",
    "AdvancedEmptyInput",
    "GridEmptyInput",
    "OverlayStatus",
    "SUPPLIED_KINDS",
    "advanced_message_keys",
    "classify",
    "error_text_keys",
    "icon_for",
    "message_keys",
    "overlay_zero_kind",
    "plan_actions",
    "plan_error_actions",
    "plan_relaxations",
    "results_pending",
]

#: 文言もボタンも AI パネルが供給する分類（ここは席の裁定だけを持つ）。
SUPPLIED_KINDS = frozenset({"advanced_zero", "advanced_error"})

#: :func:`plan_actions` が返し得る動作 id の全体。呼び出し側
#: （``PostGrid._empty_action_callbacks``）はこの集合を**漏れなく**引けること
#: — 片側だけ増えると「押せるのに何も起きないボタン」が戻る。
ACTION_IDS = frozenset({
    "open_folder",
    "help",
    "go_up",
    "close_curation",
    "close_recent",
    "retry_overlay",
    "clear_overlay_narrowing",
    "show_nsfw",
    "widen_subfolders",
    "clear_search",
})


class ActionSpec(NamedTuple):
    """空状態カードのボタン 1 つ分の**指定**（ラベルキー + 動作 id）.

    :class:`~.empty_state.EmptyAction` が「ラベル文字列 + 呼び出し先」の
    実体なのに対し、こちらは Qt 非依存の値。``action`` は呼び出し側
    （``PostGrid._empty_action_callbacks`` /
    ``AdvancedSearchController._relaxation_callbacks``）が引く動作 id で、文字列に
    してあるので「どの分類がどの一手を出すか」を単体テストで表にできる。
    ``label_params`` は ``t(label_key, **dict(label_params))`` に渡す差し込み。
    """

    label_key: str
    action: str
    tooltip_key: str = ""
    label_params: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class OverlayStatus:
    """全面占有一覧（横断キュレーション / 最近追加）の観測値。

    * ``prefix`` — 分類の接頭辞（``"curation"`` / ``"recent"``）。
    * ``pending`` — 母集合の解決 / 走査がまだ走っている。
    * ``failed`` — 1 件も読めなかった。
    * ``has_population`` — 母集合に 1 件でもあるか。
    * ``narrowing_engaged`` — 一覧の上に載せた次元が実際に効いているか。
    """

    prefix: str
    pending: bool = False
    failed: bool = False
    has_population: bool = False
    narrowing_engaged: bool = False


@dataclass(frozen=True)
class GridEmptyInput:
    """0 タイルの分類に要る観測値すべて（ウィジェットを含まない）.

    ``PostGrid`` 側の対応は ``_empty_state_inputs()``。値にしてあるので
    「この状況ならこの分類 / この文言 / このボタン」を表で固定できる。
    """

    #: AI（詳細）検索がグリッドを乗っ取っているか。
    advanced_active: bool = False
    #: ``AdvancedSearchController._advanced_phase()`` の値
    #: （``"inactive"`` / ``"searching"`` / ``"landed"`` / ``"error"``）。
    advanced_phase: str = "inactive"
    #: 全面占有一覧に居るなら、その観測値。
    overlay: OverlayStatus | None = None
    #: 再帰ウォークが走っている。
    recursive_scanning: bool = False
    #: ``body:`` の判定ワーカーが走っている。
    body_pending: bool = False
    #: ルート走査 / post.md メタパスが走っている。
    scan_loading: bool = False
    #: 検索次元が 1 つでも効いている。
    search_engaged: bool = False
    #: マーカーを除いた閲覧母集合に 1 件でもあるか。
    has_browse_population: bool = False
    #: ナビ履歴なしのルート直開き（ウィンドウが教える）。
    first_run: bool = False
    #: 既定ライブラリを自動生成した直後か（初回カードの 3 行目）。
    first_run_default_library: bool = False
    #: 直前の NSFW 抑制が隠したタイル数。
    nsfw_hidden_count: int = 0
    #: 「サブフォルダも検索」が ON。
    recursive_search: bool = False
    #: 効いている制限が「素のテキスト語」だけ（範囲を広げれば救える形）。
    only_plain_text_restriction: bool = False
    #: 素の肯定語（``~`` プールを含む）が 1 つでもある。
    has_plain_includes: bool = False
    #: 「サブフォルダも検索」を ON にすれば母集合が広がる状態か。
    can_widen_to_subfolders: bool = False
    #: 空フォルダカードが「上の階層へ」を名乗ってよいか。
    can_go_up: bool = False
    #: post.md 由来の分野限定トークンが効いている。
    post_md_scoped_terms: bool = False
    #: 走査結果が 1 件でもある（``#thumb#`` 抑制の前）。
    has_entries: bool = False
    #: 並んでいる母集合のどこにも post.md が無い。
    no_post_md_anywhere: bool = False


def overlay_zero_kind(overlay: OverlayStatus, *, nsfw_hidden: bool) -> str:
    """0 タイルのオーバーレイ一覧を分類する（母集合非依存の二分岐）.

    「母集合が空（``*_empty``）」と「母集合はあるのに次元が全部隠した
    （``*_filtered``）」の判定を横断一覧・最近追加一覧で 1 実装にする。
    後者の中の ``nsfw_hidden`` 名指しは、一覧に**実際に効いている**次元の
    有無で切り分ける。横断一覧は NSFW 抑制を通らない（隠蔽数は 0 に
    リセット済み）ので、この分岐は自然に ``curation_filtered`` へ落ちる。
    """
    if overlay.pending:
        return f"{overlay.prefix}_loading"
    if overlay.failed:
        return f"{overlay.prefix}_error"
    if overlay.has_population:
        # 母集合はあるのに 0 タイル → 隠したのは一覧の上に載せた条件の方。
        # 「このフォルダにファイルはありません」と言うと事実と逆になる
        # （走査は N 件見つけている）ので、犯人を名指しする。
        if nsfw_hidden and not overlay.narrowing_engaged:
            return "nsfw_hidden"
        return f"{overlay.prefix}_filtered"
    return f"{overlay.prefix}_empty"


def classify(inp: GridEmptyInput) -> str:
    """0 タイルのグリッドを分類する — 文言 / アイコン / ボタンの唯一の情報源。

    分類:

    * ``"none"`` — 何も描かない（何もエンゲージしていない走査中）。
    * ``"advanced_zero"`` / ``"advanced_error"`` — AI クエリが現署名で着地し
      **画面上 0 タイル**、あるいはスキャナ / provider の失敗として着地した。
      カードの中身（軸ごとの緩和ボタン / ⚠ + 再読み込み）は AI パネルが
      供給し、**出すかどうかの裁定**だけがここにある。
    * ``"searching"`` — 検索がまだ飛んでいる（再帰ウォーク / ``body:``
      判定ワーカー / 検索が効いている最中のルート走査・post.md メタパス）。
      ここで「一致なし」と言い切ると嘘になる。着地するたびに再構築が走り、
      文言が確定へ落ち直す。
    * ``"curation_loading"`` / ``"curation_empty"`` / ``"curation_error"`` /
      ``"curation_filtered"`` — 横断一覧の解決待ち / 母集合 0 / 1 件も読め
      なかった / 母集合はあるのに上に載せた条件が全部隠した。読み取り失敗を
      分けるのは、オフラインの共有と「まだ何も印を付けていない」がどちらも
      0 タイルで着地し、後者の文言が前者では事実と逆になるため。
    * ``"recent_loading"`` / ``"recent_empty"`` / ``"recent_error"`` /
      ``"recent_filtered"`` — 「最近追加されたファイル」一覧の同型。
    * ``"empty_folder"`` — フォルダに子が無い。
    * ``"first_run"`` — 同じ 0 子でも初回起動・ルート直開き（履歴なし）。
      「このフォルダは空です」だと「買ったのに中身が空」と読めるので、
      オンボーディング文言 + [フォルダを開く…] を出す。
    * ``"nsfw_hidden"`` — 何もエンゲージしていないのに NSFW のビュー設定が
      全タイルを隠した。何もしない「絞り込みを解除」ではなく犯人を名指しし、
      隠さずに表示する一手を出す。
    * ``"filtered_shallow"`` — 素の名前フィルタが 0 件かつ「サブフォルダも
      検索」が OFF。真因は needle ではなく**検索範囲**なので、それを名指し
      して範囲を広げる一手を主に置く。ほかの軸が 1 つでも効いていれば範囲は
      真因ではないので ``"filtered"`` へ落とす。
    * ``"filtered"`` — 子はあるが効いている条件が 1 件も通さなかった。
    """
    if inp.advanced_active:
        # 着地していない AI クエリでグリッドを白紙にしない（タグを 1 打鍵
        # 直すたびに無言で空になる）。3 値の判定式は AI パネル側の
        # ``_advanced_phase()`` 1 本。
        phase = inp.advanced_phase
        if phase == "searching":
            return "searching"
        return "advanced_error" if phase == "error" else "advanced_zero"
    if inp.overlay is not None:
        # 一覧の種類（横断キュレーション / 最近追加）は接頭辞だけの違い —
        # 分類そのものは 1 実装。
        return overlay_zero_kind(
            inp.overlay, nsfw_hidden=inp.nsfw_hidden_count > 0
        )
    if inp.recursive_scanning or inp.body_pending:
        return "searching"
    if inp.scan_loading:
        return "searching" if inp.search_engaged else "none"
    if not inp.has_browse_population:
        # マーカーしか無いフォルダは「空」— ``#thumb#`` を隠すのは表示の軸
        # なので、素の走査結果を見ていると ``filtered`` に落ちて「検索条件を
        # すべて解除」の行き止まりになる。
        return "first_run" if inp.first_run else "empty_folder"
    if inp.nsfw_hidden_count > 0 and not inp.search_engaged:
        return "nsfw_hidden"
    # いま見ているフォルダの中で名前フィルタが 0 件、は最も多い 0 件で、
    # 「1 階層しか見ていない」ことが画面のどこにも出ていなかった。
    if (
        not inp.recursive_search
        and inp.only_plain_text_restriction
        and inp.has_plain_includes
    ):
        return "filtered_shallow"
    return "filtered"


def results_pending(inp: GridEmptyInput) -> bool:
    """検索結果がまだ着地していないか（中間状態の再構築で判定を保留する用）.

    :func:`classify` が ``"searching"`` と呼ぶ検索の飛行中 3 種 — AI 検索の
    未着地・``body:`` の判定待ち・再帰ウォーク — と同じ信号を読む。再帰の
    シード（キャッシュ）着地はウォークが続く限り中間結果なので、着地済みとは
    見なさない。いずれも着地時に再構築が走るので、判定はそこへ送られる。
    """
    if inp.advanced_active and inp.advanced_phase == "searching":
        return True
    return inp.recursive_scanning or inp.body_pending


#: 分類 → 空状態カードのアイコン（``common/ui/icons.py`` のグリフ名）。
#: 過渡の分類（``searching`` / ``*_loading`` / ``none``）はアイコン無し —
#: すぐ差し替わる（あるいは文言すら出ない）カードにグリフを置くと点滅する。
_ICONS: dict[str, str] = {
    "empty_folder": "folder",
    # 初回カードは中央ウェルカムカードと同じグリフ（2 面で図像を揃える）。
    "first_run": "folder-open",
    "filtered": "search",
    "filtered_shallow": "search",
    "curation_empty": "search",
    "curation_filtered": "search",
    "recent_empty": "search",
    "recent_filtered": "search",
    "advanced_zero": "search",
    "nsfw_hidden": "filter",
    # 走査 / 索引の失敗は「0 件」ではない — 虫めがねを出すと「条件をゆるめ
    # れば直る」と読める。
    "advanced_error": "alert-triangle",
    # 読み取り失敗は「探し方の問題」ではないので虫めがねを出さない。
    "curation_error": "folder",
    "recent_error": "folder",
}


def icon_for(kind: str) -> str:
    """*kind* のアイコン名（無しなら ``""``）."""
    return _ICONS.get(kind, "")


def message_keys(kind: str, inp: GridEmptyInput) -> list[str]:
    """*kind* の文言を組む i18n カタログキーの列（改行で繋ぐ前提）.

    供給側が文言を持つ分類（:data:`SUPPLIED_KINDS` とオーバーレイ 4 状態）と
    文言を持たない ``"none"`` は ``[]`` を返す — 呼び出し側が供給側へ訊く。
    """
    if kind == "searching":
        return ["viewer.post_grid.empty_searching"]
    if kind == "empty_folder":
        return ["viewer.post_grid.empty_folder"]
    if kind == "first_run":
        # 見出し + 一文 +（既定ライブラリなら）その説明。``GalleryView`` の
        # 空状態は 1 本のテキストなので改行で段を作る（``TextWordWrap`` が
        # 改行をそのまま尊重する）。文言は中央ウェルカムカードと**同じキー**
        # を再利用し、2 面で食い違わせない。
        keys = [
            "viewer.content_view.welcome_heading",
            "viewer.content_view.welcome_body",
        ]
        if inp.first_run_default_library:
            keys.append("viewer.content_view.welcome_default_library")
        return keys
    if kind == "nsfw_hidden":
        return ["viewer.post_grid.empty_nsfw_hidden"]
    if kind == "filtered_shallow":
        return ["viewer.post_grid.empty_filtered_shallow"]
    if kind == "filtered":
        # 分野限定トークン（``body:`` / ``tags:`` …）で 0 件になったときは
        # 理由を 1 行足す。**post.md 由来の**条件は、直下に post.md を持つ
        # フォルダが 1 つも無ければ必ず 0 件になる — その事実が画面のどこに
        # も出ていなかった。どの条件が post.md を要るかは条件次元レジストリ
        # からの導出で、``name:`` / ``star:`` のような post.md 非依存の条件
        # まで巻き込むと誤った断定になる。分類は増やさず文言側だけを厚くする
        # （分類器が 1 本という規約）。
        keys = ["viewer.post_grid.empty_filtered"]
        if (
            inp.post_md_scoped_terms
            and inp.has_entries
            and inp.no_post_md_anywhere
        ):
            keys.append("viewer.post_grid.empty_filtered_no_post_md")
        return keys
    return []


def plan_actions(kind: str, inp: GridEmptyInput) -> list[ActionSpec]:
    """*kind* の空状態カードに載せるボタン列。

    過渡の分類（``searching`` / ``*_loading`` / AI 未着地）はボタンを持た
    ない — 自分で解決する状態を破棄させる誤クリックを誘わないため。
    :data:`SUPPLIED_KINDS` も ``[]`` を返す（中身は AI パネルが供給する）。
    """
    if kind == "first_run":
        # 初回起動に「上の階層へ」は無い（履歴なしが定義）。一手は中央の
        # ウェルカムカードと同じ 2 つ（開く → 操作の基本）— このカードは
        # 分割ビューでグリッドが主案内を取るので、片側にだけ足すと既定
        # レイアウトでは一度も現れない。
        return [
            ActionSpec("viewer.main_window.open_folder", "open_folder"),
            ActionSpec("viewer.content_view.welcome_help", "help"),
        ]
    if kind == "empty_folder":
        # 次の一手をグリッドの上に直接置く。「上の階層へ」は登れる先がある
        # ときだけで、無ければ「フォルダを開く…」（= 空のトップレベル
        # ライブラリ）。
        if inp.can_go_up:
            return [ActionSpec("viewer.main_window.go_up", "go_up")]
        return [ActionSpec("viewer.main_window.open_folder", "open_folder")]
    if kind == "curation_empty":
        return [
            ActionSpec("viewer.post_grid.curation_close", "close_curation")
        ]
    if kind == "recent_empty":
        return [ActionSpec("viewer.post_grid.curation_close", "close_recent")]
    if kind in ("recent_error", "curation_error"):
        # 読み取り失敗は「やり直せば直る」ことがある唯一の空状態（共有が
        # 戻る / フォルダが再びマウントされる）— 主アクションは再試行、
        # 「一覧を閉じる」は第 2 ボタン。2 つの一覧で同じ 2 択にするのは、
        # 片側だけ再試行を持つ非対称を作らないため。
        close = "close_recent" if kind == "recent_error" else "close_curation"
        return [
            ActionSpec("common.action.retry", "retry_overlay"),
            ActionSpec("viewer.post_grid.curation_close", close),
        ]
    if kind in ("recent_filtered", "curation_filtered"):
        # 一覧そのものは生きている（母集合 N 件）— 出口は「閉じる」ではなく
        # 上に載せた条件の解除。走査 / 解決をやり直させない。
        return [
            ActionSpec(
                "viewer.post_grid.empty_clear_filter",
                "clear_overlay_narrowing",
            )
        ]
    if kind == "nsfw_hidden":
        return [
            ActionSpec("viewer.post_grid.empty_nsfw_action", "show_nsfw")
        ]
    if kind == "filtered_shallow":
        # 主アクションは真因（検索範囲）を潰す「サブフォルダも検索して
        # 再試行」、副は「検索条件をすべて解除」（needle を捨てて戻る）。
        # どちらが正解かは利用者にしか分からないので両方その場に置く。
        return [
            ActionSpec(
                "viewer.post_grid.empty_recursive_retry", "widen_subfolders"
            ),
            ActionSpec("viewer.post_grid.empty_clear_filter", "clear_search"),
        ]
    if kind == "filtered":
        # 主ボタンは**あらゆる次元**（絞り込み欄・ポップオーバー各軸・AI
        # クエリ…）を落とすので、ラベルがそれより少ないことを約束しては
        # ならない。範囲を広げれば救える形のときだけ副ボタンを併記する。
        out = [
            ActionSpec("viewer.post_grid.empty_clear_filter", "clear_search")
        ]
        if inp.can_widen_to_subfolders:
            out.append(
                ActionSpec(
                    "viewer.post_grid.empty_recursive_retry",
                    "widen_subfolders",
                )
            )
        return out
    return []


# --------------------------------------------------------- AI 検索の 0 件 / ⚠

#: :func:`plan_relaxations` / :func:`plan_error_actions` が返し得る動作 id。
#: 呼び出し側（``AdvancedSearchController._relaxation_callbacks``）はこの集合を
#: 漏れなく引けること。
ADVANCED_ACTION_IDS = frozenset({
    "relax_locked_only",
    "relax_name_filter",
    "relax_hide_nsfw",
    "relax_ai_tags",
    "relax_threshold",
    "relax_excludes",
    "relax_coverage",
    "relax_rating",
    "relax_date",
    "open_ai_popover",
    "reload_tag_db",
})

#: 緩和ボタンのラベルへ差し込むクエリ文字列の上限（超えたら末尾を省略）。
_LABEL_ELLIPSIS_AT = 24


def _ellipsize(text: str) -> str:
    if len(text) <= _LABEL_ELLIPSIS_AT:
        return text
    return text[:_LABEL_ELLIPSIS_AT - 1] + "…"


@dataclass(frozen=True)
class AdvancedEmptyInput:
    """AI 検索の 0 件 / ⚠ カードを決めるのに要る観測値（ウィジェット非依存）.

    ``AdvancedSearchController._advanced_empty_inputs()` が組む。
    """

    #: 🔒 ロックありのみ。AI の結果は ``locked_count`` を持たないので、これが
    #: 効いていると**必ず全件**が消える = 真因。
    locked_only: bool = False
    #: 絞り込み欄の生テキスト（ラベルへ差し込む用。空なら軸は効いていない）。
    name_filter_text: str = ""
    #: 絞り込み欄が AI 結果の上で実際に絞っているか。
    name_filter_engaged: bool = False
    #: 直前の NSFW 抑制が隠したタイル数。
    nsfw_hidden_count: int = 0
    #: ``_query_mode()`` の値（``"tags"`` / ``"semantic"`` / ``"media"`` / None）。
    mode: str | None = None
    #: AIタグ欄の生テキスト。
    tag_terms: str = ""
    #: そのタグ語が *mode* のクエリに実際に効くか。
    tag_terms_narrow: bool = False
    #: OR グループ数（カバレッジ表示単位の成立条件）。
    tag_group_count: int = 0
    #: 除外タグがあるか。
    has_excludes: bool = False
    #: 精度スライダの現在値と、その中立点。
    threshold: float = 0.0
    precision_neutral: float = 0.0
    #: 表示単位の 2 フラグ。
    folder_mode: bool = False
    coverage_mode: bool = False
    #: 年齢区分 / 投稿日プリセット（``"all"`` = 中立）。
    rating_key: str = "all"
    date_preset: str = "all"
    #: 失敗着地の種別（``"tag_db"`` / ``"vector"`` / ``"engine"``）。
    error_kind: str = "tag_db"
    #: tags.db が「実在するのに開けない」判定か（「無い」と出し分ける）。
    tag_db_broken: bool = False


def plan_relaxations(inp: AdvancedEmptyInput) -> list[ActionSpec]:
    """効いている軸ごとに 1 つ、条件をゆるめる一手を並べる。

    それぞれちょうど 1 次元だけを中立へ戻して再クエリする。**いま実際に
    絞っている軸だけ**を出す — 押しても署名が変わらない一手は「行き止まり」
    そのものなので出さない。

    並びには意味がある: GUI 側で重ねた軸（🔒 / 絞り込み欄 / 年齢制限を隠す）
    が先頭 — それらが効いているときは真犯人である可能性が高く、AI 側の緩和
    だけを出すと戻せない条件を追わせることになる。その直後が「AIタグ条件を
    解除」で、ほかの AI 軸が既定のときはこれが先頭に来る（かつてここが空で
    カードが 1 つも一手を出せなかった）。それでも空なら「AI 検索条件を編集…」
    へ落として、行き止まりのカードを作らない。
    """
    out: list[ActionSpec] = []
    if inp.locked_only:
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_locked_only",
                "relax_locked_only",
            )
        )
    if inp.name_filter_engaged:
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_name_filter",
                "relax_name_filter",
                label_params=(("query", _ellipsize(inp.name_filter_text)),),
            )
        )
    # 「年齢制限を隠す」は永続のビュー設定で AI 結果にも効く — 実際にこの
    # パスで隠していたときだけ候補に出す（隠蔽数は直前の実測値なので
    # 「効かない回復手段」にはならない）。
    if inp.nsfw_hidden_count > 0:
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_hide_nsfw", "relax_hide_nsfw"
            )
        )
    # AIタグ条件そのもの — たいてい一致しなかった当の理由なのに、長く提案
    # されていなかった軸。
    if inp.tag_terms.strip() and inp.tag_terms_narrow:
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_ai_tags",
                "relax_ai_tags",
                label_params=(
                    ("terms", _ellipsize(inp.tag_terms.strip())),
                ),
            )
        )
    # 精度の中立点は**製品既定**であって DB の記録 floor ではない。floor を
    # 基準にすると未操作の既定値でも常に緩和候補が出て、押すと条件チップが
    # 点かない値（= × で戻せない値）が永続保存される。
    if (
        inp.mode == "tags"
        and inp.threshold - inp.precision_neutral > 1e-6
    ):
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_threshold",
                "relax_threshold",
                label_params=(("floor", inp.precision_neutral),),
            )
        )
    # 除外タグが効くのは strict なタグ AND だけ: 意味検索の署名は includes
    # しか運ばず、種別走査はタグ欄を一切見ない。そこで出すとチップだけ消えて
    # クエリが変わらない。
    if inp.mode == "tags" and inp.has_excludes:
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_excludes", "relax_excludes"
            )
        )
    if (
        inp.mode == "tags" and inp.folder_mode
        and not inp.coverage_mode and inp.tag_group_count >= 2
    ):
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_coverage", "relax_coverage"
            )
        )
    # 年齢帯はタグ / 意味検索の署名に入り ``ratings=`` としてワーカーへ渡る。
    # 種別走査の署名は見ないので、そこで広げても結果は戻らない。
    if inp.mode in ("tags", "semantic") and inp.rating_key != "all":
        out.append(
            ActionSpec(
                "viewer.advanced_search.relax_rating", "relax_rating"
            )
        )
    if inp.date_preset != "all":
        out.append(
            ActionSpec("viewer.advanced_search.relax_date", "relax_date")
        )
    if not out:
        # 行き止まり回避: 1 クリックで緩められる軸が無い（例: 種別=動画 だけ
        # で 1 件も無い）ので、せめてエディタを開き直す。
        out.append(
            ActionSpec(
                "viewer.advanced_search.empty_edit_query", "open_ai_popover"
            )
        )
    return out


def error_text_keys(inp: AdvancedEmptyInput) -> tuple[str, str]:
    """⚠ カードの ``(見出しキー, 説明キー)``.

    文言は着地そのものの判定に従うので、カードが別のサブシステムを責めない。
    tags.db のときだけ「索引が無い」と「索引が壊れている」を出し分ける
    （どちらも「スキャンしてください」に潰すと、壊れたファイルには効かない
    助言になる）。
    """
    if inp.error_kind == "engine":
        return (
            "viewer.advanced_search.error_title_engine",
            "viewer.advanced_search.error_hint_engine",
        )
    if inp.error_kind == "vector":
        return (
            "viewer.advanced_search.error_title_vector",
            "viewer.advanced_search.error_hint_vector",
        )
    hint = (
        "viewer.advanced_search.error_hint_broken"
        if inp.tag_db_broken
        else "viewer.advanced_search.error_hint_tag_db"
    )
    return ("viewer.advanced_search.error_title_tag_db", hint)


def plan_error_actions(inp: AdvancedEmptyInput) -> list[ActionSpec]:
    """⚠ カードに載せる操作ボタン。

    ``"engine"`` は provider 不在（プラグイン未活性）で、再読み込みは tags.db
    を開き直すだけ — provider は決して登録されないので押しても状態は変わら
    ない。ボタンを出さず説明文だけにする。``"vector"`` の説明は「Tagger で
    再スキャン」だが、ボタンは tags.db の読み直しで、これは**再スキャン後の
    後半の手順**として正しいので残し、その順序を語る文言をツールチップに
    添える。
    """
    if inp.error_kind == "engine":
        return []
    return [
        ActionSpec(
            "viewer.advanced_search.reload_db_btn",
            "reload_tag_db",
            "viewer.advanced_search.reload_db_tooltip",
        )
    ]


def advanced_message_keys(kind: str, inp: AdvancedEmptyInput) -> list[str]:
    """AI 検索の 0 件 / ⚠ カードの文言キー（改行で繋ぐ前提）.

    ``advanced_zero`` は 0 件見出し +「条件をゆるめて再検索できます:」で、
    絞り込み欄が効いているときだけ注記を**見出しの直後**（導入文の前）へ
    挿す — 導入文はその下に並ぶボタン列に掛かるので、間に注記が挟まると行が
    孤立して読めない。AI 結果に重ねた名前フィルタは名前 / タイトルしか見ない
    ので、タイル上に見える語で 0 件になったとき「ライブラリに無い」と読まれ
    てしまうのを防ぐ注記。
    """
    if kind == "advanced_error":
        title, hint = error_text_keys(inp)
        return [title, hint] if hint else [title]
    lines = ["viewer.advanced_search.empty_title"]
    if inp.name_filter_engaged:
        lines.append("viewer.advanced_search.empty_name_match_note")
    lines.append("viewer.advanced_search.empty_hint")
    return lines
