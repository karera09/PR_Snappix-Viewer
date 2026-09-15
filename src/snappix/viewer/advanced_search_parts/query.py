"""AI 検索のクエリ状態（値型）とその導出 — Qt 非依存の純関数層.

:class:`AiQuery` は AI 検索が「何を引くか」を決める値の**全部**を 1 つの
frozen dataclass に畳んだもの。AIタグ語（include / exclude / OR 群の生表記）・
類似シード・精度・種別・表示単位・年齢区分・投稿日プリセット・3 択モード・
揮発の起動フラグがここに載る。

**なぜ値型なのか**: これらはかつて ``AdvancedSearchMixin`` の平置き属性 10 個
として散り、書き込みが 20 箇所以上（ハンドラ・復元・リセット・降格）に散って
いた。結果、書き込み口の 1 つが**再描画同期の経路**に置かれ（グリッド再構築の
たびに保持モードを検証して書き換える）、「表示を合わせただけのつもり」が
クエリの署名を変える経路になっていた。値型に畳めば書き込みは
「新しい :class:`AiQuery` を作って :meth:`AdvancedSearchController.set_query`
へ渡す」1 本になり、再描画側は読むだけになる。

:class:`ScanRequest` は「いま投げているスキャン 1 件」— 以前は
``_pending_tag_query`` / ``_pending_vector_query`` と
``_pending_tag_want_posted_at`` / ``_pending_vector_want_posted_at`` の 2 対
4 フィールドだった。同時に有効なのは常に片方だけ（:meth:`_kick_tag_scan` が
一方を投げる前にもう一方のスキャナを ``cancel`` する）なので 1 スロットで
足りる。着地ガードの意味は変わらない — 現署名と一致しない着地は捨てる。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..filter_query import _parse_query, _parse_tag_groups
from ..search_dimensions import DEFAULT_TAG_THRESHOLD

#: 年齢区分の帯 → その帯が許す ``images.rating`` の集合（severity 順
#: safe < questionable < explicit）。``None`` = 制限なし。
RATING_BANDS: dict[str, set[str] | None] = {
    "all": None,
    "safe": {"safe"},
    "sfw": {"safe", "questionable"},
    "explicit": {"explicit"},
}

#: 表示単位の中立値（条件チップ × の戻し先と同じ既定）。
NEUTRAL_DISPLAY_UNIT = "folder_coverage"

#: 3 択モードのキー（表示セグメントの並びと同じ順序・同じ値）。
AI_MODE_KEYS = ("and", "rank", "similar")


@dataclass(frozen=True, slots=True)
class AiQuery:
    """AI 検索のクエリ状態（不変値）。

    * ``text`` — AIタグ入力欄の生テキスト（``-語`` 除外 / ``~語`` OR を含む）
    * ``enabled`` — 揮発の起動フラグ。チップが 1 つ入った瞬間に True、
      空（かつ年齢区分が中立）で False。永続化しない。
    * ``threshold`` — AIタグの精度しきい値
    * ``media_type`` — 種別走査の軸（``all`` / ``image`` 以外は拡張子走査）
    * ``folder_mode`` / ``coverage_mode`` — 表示単位の 2 bool（永続化形）
    * ``rating_key`` — 年齢区分の帯（:data:`RATING_BANDS` の鍵）
    * ``date_preset`` — 投稿日プリセット（境界の解決は GUI 側の日付欄が要る
      ので、ここにはプリセット鍵だけを持つ）
    * ``mode`` — 3 択（``and`` / ``rank`` / ``similar``）の**唯一の保持値**
    * ``similar_seed`` — ``similar`` モードのデータ（参照画像のパス文字列）
    """

    text: str = ""
    enabled: bool = False
    threshold: float = DEFAULT_TAG_THRESHOLD
    media_type: str = "all"
    folder_mode: bool = True
    coverage_mode: bool = True
    rating_key: str = "all"
    date_preset: str = "all"
    mode: str = "and"
    similar_seed: str | None = None


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """投げたスキャン 1 件の記録（着地ガードの材料）。

    ``kind`` は ``"tag"``（タグ / 種別走査スキャナ）か ``"vector"``（意味検索
    スキャナ）。``signature`` はそのとき有効だったクエリ署名、
    ``want_posted_at`` は ``posted_at`` のシードを頼んだかどうか。
    """

    kind: str
    signature: tuple
    want_posted_at: bool = False


def rating_signature(rating_key: str) -> tuple[str, ...] | None:
    """年齢区分の帯を署名に載る形（ソート済みタプル / ``None``）へ畳む。"""
    ratings = RATING_BANDS.get(rating_key)
    return tuple(sorted(ratings)) if ratings else None


def normalize_mode(
    key: str, *, has_vectors: bool, rank_available: bool,
) -> str:
    """3 択モードの**可用性検証** — 押せないモードを保持させない。

    倒し先はいずれも ``"and"``（AIタグ検索）: タグ語が実際に効く唯一のモード
    なので、打った条件が無言で捨てられない。

    * 未知のキー → ``"and"``
    * ベクトル索引が無い → 意味検索 2 種は不可 → ``"and"``
    * タグ→ベクトル行列が無い / 壊れている → ``"rank"`` は無言の 0 件に
      なるので ``"and"``
    """
    if key not in AI_MODE_KEYS:
        return "and"
    if key != "and" and not has_vectors:
        return "and"
    if key == "rank" and not rank_available:
        return "and"
    return key


def query_mode(query: AiQuery, *, has_tag_index: bool) -> str | None:
    """いま引く検索源（``None`` = AI 検索は立っていない）。

    * ``"media"`` — 種別が ``all`` / ``image`` 以外（拡張子走査。tags.db 不要）
    * ``"semantic"`` — 意味検索 2 種のうち**引くものがある**形
      （``similar`` にシードがある / ``rank`` に include がある）
    * ``"tags"`` — 起動フラグが立ち、tags.db があり、タグ語か年齢区分がある
    """
    if query.media_type not in ("all", "image"):
        return "media"
    # 意味検索 2 種は保持モードから直に決まる（可用性は書き込み時に検証済み）。
    # 引くものが無いモード（シード未設定の similar / タグ未入力の rank）は
    # 「まだ走らせない」のであって別モードへ化けるのではない — tags 分岐へ落ちる。
    if query.mode == "similar":
        if query.similar_seed:
            return "semantic"
    elif query.mode == "rank":
        includes, _excludes = _parse_query(query.text)
        if includes:
            return "semantic"
    if query.enabled and has_tag_index:
        includes, excludes = _parse_query(query.text)
        if includes or excludes or query.rating_key != "all":
            return "tags"
    return None


def signature(query: AiQuery, mode: str | None) -> tuple | None:
    """ワーカーに効くパラメータだけを畳んだ安定署名。

    ``_tag_results_query`` と突き合わせることで、「操作中に変わったクエリの
    結果」が画面に出るのを防ぐ。絞り込み欄と投稿日は**わざと外す** — どちらも
    着地済みの結果を GUI 側で絞り直すだけで、ワーカーを引き直さない。ルートも
    載せない（ルート変更はスキャナの cancel + 再キックで表現する）。
    """
    if mode == "semantic":
        rating_sig = rating_signature(query.rating_key)
        if query.similar_seed:
            return ("semantic", "similar", query.similar_seed, rating_sig)
        includes, _excludes = _parse_query(query.text)
        return ("semantic", "tags", tuple(includes), rating_sig)
    if mode == "tags":
        # OR 群の構造ごと署名へ（``~`` の付け外しで引き直す）。
        groups, excludes = _parse_tag_groups(query.text)
        return (
            "tags",
            tuple(tuple(g) for g in groups),
            tuple(excludes),
            round(query.threshold, 4),
            rating_signature(query.rating_key),
            query.folder_mode,
            # カバレッジはフォルダモードでしか結果を変えないので、ファイル
            # モードでは None に畳む（切り替えても引き直さない）。
            query.coverage_mode if query.folder_mode else None,
            query.media_type,
        )
    if mode == "media":
        return ("media", query.media_type, query.folder_mode)
    return None


def display_unit_key(query: AiQuery) -> str:
    """表示単位コンボの鍵を 2 bool から導く。"""
    if not query.folder_mode:
        return "file"
    return "folder_coverage" if query.coverage_mode else "folder_strict"


def with_display_unit(query: AiQuery, key: str) -> AiQuery:
    """表示単位の鍵を 2 bool（永続化形）へ写した新しいクエリ。

    ``file`` はフォルダモードを落とすだけ（カバレッジは無関係なので保つ）。
    """
    if key == "file":
        return replace(query, folder_mode=False)
    return replace(query, folder_mode=True, coverage_mode=key == "folder_coverage")


def tag_terms_narrow_query(query: AiQuery, mode: str | None) -> bool:
    """AIタグ欄が *mode* のクエリに**実際に効いているか**。

    「AIタグ条件を解除」を 0 件カードに出してよいかの判定。種別走査
    （``media``）と類似画像形（シードあり）の署名はタグ欄を含まないので、
    押しても署名が変わらず引き直しも起きない = 行き止まりのボタンになる。
    """
    if mode == "tags":
        return True
    return mode == "semantic" and not query.similar_seed
