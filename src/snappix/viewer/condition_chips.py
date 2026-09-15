"""条件チップの列を「値から値へ」決める純関数群（Qt 非依存）。

条件バーに何が並ぶかは 1 文で言える — 『いま効いている次元を、いま見ている
母集合が実際に適用する順に、1 軸 1 枚ずつ』。その判定材料はすべて左ペインの
**値**（フィルタバーの各軸・AI クエリのモードと語・絞り込み欄の残り）なので、
ウィジェットを 1 つも持たずに決められる。ここは観測値 :class:`ConditionState`
1 つを入口にして、

* :data:`DIM_ROWS` — 1 次元 = 1 行（判定 ``engaged`` / チップ文言 ``label`` /
  編集面 ``kind`` / 参加する母集合 ``scopes`` / 中立判定 ``at_neutral``）
* :func:`rows` — AI パック帰属の行フィルタを掛けた表
* :func:`chips` — 表 → 描くチップ列（:class:`ChipSpec`）
* :func:`visible_chips` / :func:`summary_text` / :func:`chips_that_fit`

だけを持つ。``PostGrid`` 側は観測値を束ねて渡し、返った ``key`` を
:data:`ACTION_IDS` のコールバック表で自分の ``_clear_dim_*`` へ戻すだけの層に
なる（``grid_empty_state`` と同じ流儀）。

**不変**: 行順は条件次元レジストリ :mod:`.search_dimensions` の行順と一致し、
文言・書式は全て :func:`~.search_dimensions.chip_text` を通る。``owner="ai"``
の行は :func:`rows` 末尾のフィルタで**表ごと消える** — 可用性ゲートの適用点は
ここ 1 か所で、軸ごとの gate 見落としは構造的に起きない。

**射程**: 決めるのは「どのチップがどの順で出るか・その文言・何枚まで並ぶか」
まで。ウィジェットの生成と × の配線は :mod:`.condition_bar`、状態を変える
``_clear_dim_*`` は ``PostGrid`` が持つ。

テストは ``tests/test_viewer_condition_chips.py``（QApplication 不要）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from ..common.i18n import t
from .filter_predicates import FilterBarCriteria
from .search_dimensions import chip_text
from .search_dimensions import get as _dim_get

__all__ = [
    "ACTION_IDS",
    "CHIP_KINDS",
    "ChipSpec",
    "ConditionState",
    "DIM_ROWS",
    "DimRow",
    "OVERLAY_ID",
    "chips",
    "chips_that_fit",
    "engaged_keys",
    "rows",
    "summary_text",
    "visible_chips",
]

#: scope 集合の略記（``_ViewDim.scopes`` と同じ語彙）。
_SCOPE_ALL = frozenset({"plain", "advanced", "overlay"})
_SCOPE_PLAIN_ADV = frozenset({"plain", "advanced"})
_SCOPE_ADV = frozenset({"advanced"})
_SCOPE_PLAIN = frozenset({"plain"})

#: 全面占有一覧（横断キュレーション / 最近追加）のチップ id。次元表の行では
#: なく**母集合そのもの**なので台帳にも scopes にも乗らないが、× を持つ以上は
#: コールバック表の鍵が要る。
OVERLAY_ID = "overlay"

#: チップ本体クリックの行き先（``PostGrid._edit_condition`` が受ける集合）。
#: ``"none"`` は編集面を持たないチップ（一覧そのもの）。
CHIP_KINDS = frozenset({"ai", "filter", "search", "none"})

#: AI タグ精度の比較誤差（浮動小数の往復で中立点と 1 ulp ずれるため）。
_EPS = 1e-6


@dataclass(frozen=True)
class ConditionState:
    """条件チップを決めるのに要る観測値すべて（ウィジェットを含まない）.

    ``PostGrid`` 側の対応は ``_condition_state()``。**束ねる時点**が判定の
    時点になるので、束ねてから描くまでの間に状態を動かさないこと（束ねた値で
    描いておきながら実際のグリッドは別の条件、という時点適用を作らない）。

    フィルタバーの 8 軸は PR で先に値型になった :class:`FilterBarCriteria` を
    **内包**する（同じ軸の 2 つ目の束ねを作らない）。ここが足すのは AI クエリ
    側の観測値と、コンボの**表示文字列**である。

    表示文字列を値キーから引き直さずコンボから写すのは、それが利用者の目に
    見えている語そのものだから — 投稿日の「期間指定」のように、キーだけでは
    復元できない文言を持つ軸がある。ウィジェットが 1 つも建っていない構成
    （構築途中・素の値だけのテスト）では空文字で、チップは軸名だけを名乗る。
    """

    #: フィルタバー 8 軸の値（種別 / ★ / あとで見る / ユーザータグ /
    #: 検索範囲 / 年齢区分 / 投稿日 / 🔒）。
    bar: FilterBarCriteria = field(default_factory=FilterBarCriteria)
    #: AI パックが使えるか（偽なら ``owner="ai"`` の行が表ごと消える）。
    ai_available: bool = False
    #: ``AdvancedSearchController._query_mode()``（``"semantic"`` / ``"tags"`` /
    #: ``"media"`` / ``None``）。AI パック無効時は ``None`` で束ねる。
    query_mode: str | None = None
    #: AIタグ検索欄の語（``-`` を剥がした肯定形 / 除外語）。
    ai_includes: tuple[str, ...] = ()
    ai_excludes: tuple[str, ...] = ()
    #: 類似画像検索のシード画像名（空 = シード無し）。
    similar_seed_name: str = ""
    #: AIタグ精度の現在値と中立点（``max(記録 floor, 既定)``）。
    tag_threshold: float = 0.0
    precision_neutral: float = 0.0
    #: 表示単位キー（``"folder_coverage"`` が中立）。
    display_unit: str = "folder_coverage"
    #: AI 対象種別キー（``"all"`` / ``"image"`` が中立）。
    ai_media: str = "all"
    #: 絞り込み欄のうち**次元チップを所有しないトークンだけ**の残り
    #: （``PostGrid._plain_filter_label``）。
    plain_filter: str = ""
    #: コンボの表示文字列（軸ごと。空なら軸名だけのチップになる）。
    rating_label: str = ""
    media_label: str = ""
    date_label: str = ""
    unit_label: str = ""
    ai_media_label: str = ""
    #: いまグリッドを持っている母集合（``"plain"`` / ``"advanced"`` /
    #: ``"overlay"``）。
    view_scope: str = "plain"
    #: 全面占有一覧に居るなら、その見出し（空 = 一覧に居ない）。
    overlay_label: str = ""


class ChipSpec(NamedTuple):
    """条件チップ 1 枚分の**指定**（Qt 非依存の値）.

    * ``key`` — 次元 id（:data:`ACTION_IDS` の 1 つ）。× の行き先をコール
      バック表から引く鍵で、文字列にしてあるので「どの状態でどのチップが
      出るか」を単体テストで表にできる。
    * ``label`` — 表示文言（既に ``t()`` 済み）。
    * ``kind`` — 本体クリックの行き先（:data:`CHIP_KINDS`）。
    """

    key: str
    label: str
    kind: str


class DimRow(NamedTuple):
    """ビュー次元表の 1 行のうち、**値だけで決まる列**.

    ``PostGrid._view_dimensions`` が組む実行時表（``_ViewDim``）はこの行に
    ``apply`` / ``clear``（ホストの状態を触る 2 列）を足したもの。判定と文言が
    値の側に居ることで、チップの表を QApplication 無しで固定できる。
    """

    key: str
    kind: str
    scopes: frozenset[str]
    engaged: Callable[[ConditionState], bool]
    label: Callable[[ConditionState], str]
    at_neutral: Callable[[ConditionState], bool] | None = None
    #: チップを出す母集合。``None`` = ``scopes`` と同じ（適用と表示をあえて
    #: 分けたい将来の軸のために機構だけ残す）。
    chip_scopes: frozenset[str] | None = None


def _ai_terms(state: ConditionState) -> list[str]:
    """AIタグ検索欄の語を、チップに出す表記（除外は ``-`` 付き）で並べる。"""
    return list(state.ai_includes) + [f"-{tok}" for tok in state.ai_excludes]


def _label_semantic(state: ConditionState) -> str:
    """意味検索チップはシードの有無で 2 形態（台帳は ``chip_form="custom"``）。"""
    if state.similar_seed_name:
        return t(
            "viewer.post_grid.dim_similar_image", name=state.similar_seed_name,
        )
    return t(
        "viewer.post_grid.dim_ai_similar",
        terms=", ".join(state.ai_includes),
    )


#: ビュー次元表の値側（行順 = 条件次元レジストリの行順 = チップの並び順）。
#:
#: 1 行 = グリッドを絞る 1 次元。``apply=None`` に相当する軸（AI クエリ・
#: locked・絞り込み欄・検索範囲）もここに居る — 適用は母集合構築側が意味論
#: ごとに行うが、**engaged / チップ / scope の唯一の情報源**は表だから。
DIM_ROWS: tuple[DimRow, ...] = (
    # --- AI クエリ次元（母集合 = AI 検索結果そのもの）。
    DimRow(
        "ai_semantic", "ai", _SCOPE_ADV,
        engaged=lambda s: s.query_mode == "semantic",
        label=_label_semantic,
    ),
    DimRow(
        "ai_tags", "ai", _SCOPE_ADV,
        engaged=lambda s: s.query_mode == "tags" and bool(_ai_terms(s)),
        label=lambda s: chip_text("ai_tags", terms=", ".join(_ai_terms(s))),
    ),
    # 精度 — 中立点（``max(記録 floor, 既定)``）を超えたときだけ点く。
    # スライダ下限との比較にすると、下限が既定を下回る構成で「触っていない
    # 既定値」がチップになる。中立点の式は観測側 1 か所（``_precision_neutral``）。
    DimRow(
        "ai_precision", "ai", _SCOPE_ADV,
        engaged=lambda s: (
            s.query_mode == "tags"
            and s.tag_threshold - s.precision_neutral > _EPS
        ),
        label=lambda s: chip_text("ai_precision", value=s.tag_threshold),
        # チップは AIタグ検索中しか点かないが、値そのものは非既定のまま次の
        # 検索へ持ち越される — リセット対象かの判定はモードを見ない。
        at_neutral=lambda s: s.tag_threshold - s.precision_neutral <= _EPS,
    ),
    # 表示単位 — 既定（フォルダ / カバレッジ）以外のときだけ。
    DimRow(
        "ai_unit", "ai", _SCOPE_ADV,
        engaged=lambda s: (
            s.query_mode == "tags" and s.display_unit != "folder_coverage"
        ),
        label=lambda s: chip_text("ai_unit", value=s.unit_label),
        at_neutral=lambda s: s.display_unit == "folder_coverage",
    ),
    DimRow(
        "ai_media", "ai", _SCOPE_ADV,
        engaged=lambda s: s.query_mode == "media",
        label=lambda s: chip_text("ai_media", value=s.ai_media_label),
        # 種別走査は「AI クエリのモードそのもの」なので engaged はモードを
        # 見るが、コンボ値が非既定なら常にリセット対象。
        at_neutral=lambda s: s.ai_media in ("all", "image"),
    ),
    # 年齢区分は独立した 1 次元（タグクエリの一部だが概念は別）。AI パックの
    # ゲートは台帳の ``owner="ai"`` 側にあり、この判定式には無い — 軸ごとの
    # 「モードを見ない行」の見落としを再現不能にするため。チップの編集面は
    # フィルターポップオーバー（"filter"）。
    DimRow(
        "rating", "filter", _SCOPE_PLAIN_ADV,
        engaged=lambda s: s.bar.rating != "all",
        label=lambda s: chip_text("rating", value=s.rating_label),
    ),
    # --- 適応型フィルタの述語（全母集合に共通適用）。
    DimRow(
        "media", "filter", _SCOPE_ALL,
        engaged=lambda s: s.bar.media != "all",
        label=lambda s: chip_text("media", value=s.media_label),
    ),
    # キュレーション述語 — ★N以上コンボの語と「あとで見る」の動作名を
    # そのままチップ文言に使う（新しい文字列を作らない）。
    DimRow(
        "star", "filter", _SCOPE_ALL,
        engaged=lambda s: s.bar.star_min > 0,
        label=lambda s: chip_text("star", n=s.bar.star_min),
    ),
    DimRow(
        "later", "filter", _SCOPE_ALL,
        engaged=lambda s: bool(s.bar.later),
        label=lambda s: chip_text("later"),
    ),
    # ユーザータグ — ★ / あとで見ると同じ契約（本体クリックで絞り込み
    # ポップオーバー、× でこの次元だけ解除）。
    DimRow(
        "usertag", "filter", _SCOPE_ALL,
        engaged=lambda s: bool(s.bar.user_tag),
        label=lambda s: chip_text("usertag", value=s.bar.user_tag),
    ),
    # 投稿日。overlay は scope 外 — 母集合が posted_at=None で組まれるので
    # 意味を持たない（チップも同じ判定で消える）。advanced では母集合構築が
    # 同じ述語を適用済みで、fold の再適用は冪等。
    DimRow(
        "date", "filter", _SCOPE_PLAIN_ADV,
        engaged=lambda s: s.bar.date_preset != "all",
        label=lambda s: chip_text("date", value=s.date_label),
    ),
    # 「ロックありのみ」— 平常は母集合構築（直下の事前絞り + 子孫抑制）、AI は
    # 結果全滅、と母集合ごとに意味論が違う。overlay は locked_count=0 で組まれる
    # 母集合に対し原理的に無意味なので scope 外。
    DimRow(
        "locked", "filter", _SCOPE_PLAIN_ADV,
        engaged=lambda s: bool(s.bar.locked_only),
        label=lambda s: chip_text("locked"),
    ),
    # 「サブフォルダも検索」。条件バーの契約は「グリッドに出るものへの制限は
    # 全部チップになる」で、検索**範囲**はその中で唯一欠けていた軸。絞り込み
    # ではないので narrowing には数えない（scopes=plain のみ）。AI 検索経路は
    # この軸を一切読まない（常に再帰）ため、チップも plain のみ — 「範囲を
    # 示さないチップ」を出さない。編集面は他の軸と同じフィルターポップオーバー。
    DimRow(
        "recursive", "filter", _SCOPE_PLAIN,
        engaged=lambda s: bool(s.bar.recursive),
        label=lambda s: chip_text("recursive"),
    ),
    # 絞り込み欄はコントロール項を**除いた**残りだけを見せる（``type:`` /
    # ``rating:`` / ``score:`` は上で自分の次元チップになるので二重に数えない）。
    DimRow(
        "filter", "search", _SCOPE_ALL,
        engaged=lambda s: bool(s.plain_filter),
        label=lambda s: chip_text("filter", text=s.plain_filter),
    ),
)

#: × が戻る先の動作 id の全体（次元 13 + 一覧 1）。呼び出し側
#: （``PostGrid._condition_clear_callbacks``）はこの集合を**漏れなく**引ける
#: こと — 片側だけ増えると「押せるのに何も起きない ×」が戻る。AI パック無効時
#: でも縮まない（``_clear_dim_*`` はメソッドとして常に在る）。
ACTION_IDS = frozenset({row.key for row in DIM_ROWS} | {OVERLAY_ID})


def rows(state: ConditionState) -> tuple[DimRow, ...]:
    """*state* で**存在する**次元表（AI 帰属の行フィルタ込み）。

    素の配布 / AI パック無効では、台帳が ``owner="ai"`` と宣言した軸は
    **表ごと存在しない** — engaged 側の個別ゲートに頼らない。
    """
    if state.ai_available:
        return DIM_ROWS
    return tuple(
        row for row in DIM_ROWS
        if (led := _dim_get(row.key)) is None or led.owner != "ai"
    )


def engaged_keys(state: ConditionState) -> frozenset[str]:
    """いま効いている次元の id 集合（scope を問わない）。"""
    return frozenset(row.key for row in rows(state) if row.engaged(state))


def chips(state: ConditionState) -> list[ChipSpec]:
    """*state* が描くべきチップ列（**状態 → チップ列**の純関数）。

    先頭は全面占有一覧（横断キュレーション / 最近追加）— 一覧そのものが最上位
    の次元で、× は絞り込みではなく一覧からの退場。以降は次元表の行順で、
    「いま見ている母集合が実際に適用する」軸だけが並ぶ（``chip_scopes`` ないし
    ``scopes`` に現在の母集合が入っている行）。母集合が適用しない軸のチップを
    出すと「見えているのに効かない条件」になる。
    """
    out: list[ChipSpec] = []
    if state.overlay_label:
        out.append(ChipSpec(OVERLAY_ID, state.overlay_label, "none"))
    for row in rows(state):
        shown_in = row.chip_scopes if row.chip_scopes is not None else row.scopes
        if state.view_scope in shown_in and row.engaged(state):
            out.append(ChipSpec(row.key, row.label(state), row.kind))
    return out


def visible_chips(
    specs: Sequence[ChipSpec], *, field_shows_term: bool,
) -> list[ChipSpec]:
    """描画する列 — 検索欄が同じ語を見せている間は絞り込みチップを落とす。

    ツールバーの検索欄は既に語とその × を出しているので、同じものを 2 枚目の
    チップ（と 2 つ目の ×）として並べるのは純粋な重複。**次元としては**生きて
    いる（要約にも載るし件数にも効く）ので、落とすのは描画列だけ。
    """
    if not field_shows_term:
        return list(specs)
    return [spec for spec in specs if spec.kind != "search"]


def summary_text(specs: Sequence[ChipSpec], shown: int) -> str:
    """適用中の条件を 1 行に畳んだ要約（バーのツールチップ / 集約ラベル）。

    可視面はチップ列なので、ここは**落とさない列**（:func:`chips` の全件）から
    組む — 検索欄と重複するから描かなかった軸も、要約には載る。
    """
    if not specs:
        return ""
    sep = t("common.sep.middot")
    return (
        sep.join(spec.label for spec in specs)
        + sep
        + t("viewer.post_grid.banner_count", n=shown)
    )


def chips_that_fit(
    widths: Sequence[int],
    *,
    budget: int,
    spacing: int,
    overflow_width: int,
) -> int:
    """先頭から何枚まで並べられるか（残りは「他 N 件」へ畳む）。

    省略はチップ**単体**の幅にしか効かないので、行全体がバー幅を超えると Qt は
    各チップを最小幅まで潰し、全部が「… ×」になって「どの × が何を落とすか」
    が判別不能になる（押せる状態は残るので誤クリックで意図しない条件が落ちる）。
    入り切らない分は畳んで、並べたチップは必ず読める幅を保つ。

    *budget* の ``-1`` は「まだレイアウトされていない（幅が未確定）」で全部
    並べる（幅が決まった時点の Resize で組み直す）。``0`` は「レイアウト済み
    だが席が 1 枚ぶんも無い」で意味が違う — 同じ ``0`` で表すと後者が全チップ
    を並べ、Qt の既定圧縮で全部が「… ×」に潰れる。最低 1 枚は必ず並べる
    （何で絞り込んでいるかを 1 つも言わないバーにはしない）。
    """
    if budget < 0 or not widths:
        return len(widths)
    if budget == 0:
        return 1
    if sum(widths) + spacing * (len(widths) - 1) <= budget:
        return len(widths)
    reserve = spacing + overflow_width
    keep, used = 1, widths[0]
    while keep < len(widths):
        nxt = used + spacing + widths[keep]
        if nxt + reserve > budget:
            break
        used, keep = nxt, keep + 1
    return keep
