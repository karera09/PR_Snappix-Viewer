"""フィルタバー各軸の述語 — Qt 非依存の純関数群。

左ペインのフィルタバー（フィルタポップオーバー）が持つ軸 — 種別 / ★ 下限 /
「あとで見る」/ ユーザータグ / 投稿日 — は、どれも「``FolderEntry`` の列を
**順序を保ったまま**絞る」だけの写像で、ウィジェットにも ``PostGrid`` の
インスタンス状態にも依存しない。ここでは軸の値を :class:`FilterBarCriteria`
1 つに束ね、``(entries, criteria) -> entries`` の純関数として持つ。

**不変**: どの関数も*絞る*だけで並べ替えない（並び順は
``filter_query._sort_spec`` の担当）。効いていない軸は入力リストを
**同一オブジェクトのまま**返す（素の閲覧でコピーを作らない）。

キュレーション値（★ / ユーザータグ / あとで見る）はメモリ上の辞書引きを
コールバック *curation* で受け取る — ``filter_query._match_filter_terms`` の
``curation=`` 引数と同じ契約で、``(path) -> (star, tags, later)`` を同期に
返すこと（sqlite / NAS を触らない。描画経路から呼ばれる）。

テストは ``tests/test_viewer_filter_predicates.py``（QApplication 不要）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .folder_scan import FolderEntry, media_suffixes_for
from .tag_filter import date_matches

__all__ = [
    "CurationLookup",
    "FilterBarCriteria",
    "apply_all",
    "apply_curation",
    "apply_date",
    "apply_later",
    "apply_media",
    "apply_star",
    "apply_usertag",
    "bar_engaged",
    "row_engaged",
]

#: ``(path) -> (star, user_tags, later)`` のメモリ内引き。
CurationLookup = Callable[[Path], tuple[int, Sequence[object], bool]]


@dataclass(frozen=True)
class FilterBarCriteria:
    """フィルタバー各軸の**値**（ウィジェットではなく素の状態）.

    ``PostGrid`` は軸ごとにインスタンス属性（``_filterbar_media`` 等）で
    保持しているので、述語を呼ぶ直前にこの 1 つへ束ねて渡す。こうすると
    述語側はホスト属性を直読みせずに済み、単体テストも値を組むだけで書ける。

    * ``media`` — 種別キー（``"all"`` = 中立）。
    * ``star_min`` — ★ 下限（``0`` = 中立）。
    * ``later`` — 「あとで見る」だけに絞るか。
    * ``user_tag`` — ユーザータグ 1 つ（``""`` = 「すべて」）。
    * ``recursive`` / ``rating`` / ``date_preset`` / ``locked_only`` — 述語は
      持たないがフィルタポップオーバーのアクセント同期（:func:`row_engaged`）
      が数える軸。``recursive`` は「絞り込み」ではなく**検索範囲**なので
      :func:`bar_engaged` からは既定で外れる。

    投稿日の半開窓は**ここに載せない**: 具体値 ``(lo, hi)`` は
    ``tag_filter.preset_range`` とポップオーバーの期間エディタから導く派生値
    なので、:func:`apply_date` へ直接渡す（この束ねはウィジェットが 1 つも
    建っていない構築途中でも組めることを契約にしている）。
    """

    media: str = "all"
    star_min: int = 0
    later: bool = False
    user_tag: str = ""
    recursive: bool = False
    rating: str = "all"
    date_preset: str = "all"
    locked_only: bool = False


def apply_media(
    entries: list[FolderEntry], kind: str,
) -> list[FolderEntry]:
    """種別（拡張子）で絞る。

    種別が選ばれているときは、その種別に属する**ファイル**行だけを残す —
    拡張子を持たないフォルダは落ちるので「画像」がちょうど画像だけを出す
    （``"image"`` が何もしない、という非対称を作らない）。``"all"`` は
    *entries* をそのまま返す。
    """
    suffixes = media_suffixes_for(kind) if kind != "all" else None
    if suffixes is None:
        return entries
    return [
        e for e in entries
        if not e.is_dir and e.path.suffix.lower() in suffixes
    ]


def apply_star(
    entries: list[FolderEntry], star_min: int, curation: CurationLookup,
) -> list[FolderEntry]:
    """★ 下限（*star_min* > 0 のときだけ絞る）."""
    if star_min <= 0:
        return entries
    return [e for e in entries if curation(e.path)[0] >= star_min]


def apply_later(
    entries: list[FolderEntry], engaged: bool, curation: CurationLookup,
) -> list[FolderEntry]:
    """「あとで見る」フラグ（*engaged* のときだけ絞る）."""
    if not engaged:
        return entries
    return [e for e in entries if curation(e.path)[2]]


def apply_usertag(
    entries: list[FolderEntry], want_tag: str, curation: CurationLookup,
) -> list[FolderEntry]:
    """ユーザータグ（*want_tag* が空でないときだけ絞る）.

    大小同一視で照合する（編集ダイアログの補完も CaseInsensitive、
    ``mytags:`` トークンも casefold 比較）。
    """
    if not want_tag:
        return entries
    needle = want_tag.casefold()
    return [
        e for e in entries
        if needle in {str(x).casefold() for x in curation(e.path)[1]}
    ]


def apply_curation(
    entries: list[FolderEntry],
    criteria: FilterBarCriteria,
    curation: CurationLookup,
) -> list[FolderEntry]:
    """キュレーション 3 軸（★ / あとで見る / ユーザータグ）の合成。

    ビュー次元表は 3 軸を別々の行として持つので通常はそちらが個別に適用する。
    この合成は一括適用の互換 API として残す。
    """
    return apply_usertag(
        apply_later(
            apply_star(entries, criteria.star_min, curation),
            criteria.later,
            curation,
        ),
        criteria.user_tag,
        curation,
    )


def apply_date(
    entries: list[FolderEntry],
    lo: datetime | None,
    hi: datetime | None,
) -> list[FolderEntry]:
    """投稿日の半開窓 ``[lo, hi)`` で絞る。

    ``posted_at`` は左ペイン自身のメタデータパス（post.md を読む）がシード
    するので、ここに追加のワーカーは要らない。日付がまだ分からないエントリは
    残す（``date_matches`` の keep_unknown）— post.md をまだ読めていないだけで
    フォルダが消える、という畳み方をしないため。
    """
    if lo is None and hi is None:
        return entries
    return [e for e in entries if date_matches(e.posted_at, lo, hi)]


def apply_all(
    entries: list[FolderEntry],
    criteria: FilterBarCriteria,
    curation: CurationLookup,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[FolderEntry]:
    """種別 → キュレーション 3 軸 → 投稿日 を順に適用する。

    ``PostGrid`` はビュー次元表から軸ごとに呼ぶので production では使わない
    が、「フィルタバーが全部効いた形」を 1 呼び出しで得たいテスト / 将来の
    呼び出し側のために置く。
    """
    items = apply_media(entries, criteria.media)
    items = apply_curation(items, criteria, curation)
    return apply_date(items, date_from, date_to)


#: :func:`bar_engaged` が数えない行 — 「サブフォルダも検索」は絞り込みでは
#: なく**検索範囲**の軸（条件チップも別扱い）。
BAR_EXCLUDED_ROWS = frozenset({"recursive"})


def row_engaged(criteria: FilterBarCriteria) -> dict[str, bool]:
    """フィルターポップオーバーの各軸が効いているか ``{軸 id: bool}``.

    値は素の状態フィールドから読む（ウィジェット不在の構成でも同じ式が
    成立する）。``_view_dimensions`` の ``engaged`` 閉包と同じ判定だが、
    そちらは AI クエリのモード判定に AI ポップオーバーのウィジェットを
    使うため、**フィルターポップオーバー構築中には呼べない**（AI 側は
    まだ建っていない）。この 1 関数がアクセント同期の情報源。
    """
    return {
        "recursive": bool(criteria.recursive),
        "media": criteria.media != "all",
        "rating": criteria.rating != "all",
        "date": criteria.date_preset != "all",
        "star": criteria.star_min > 0,
        "later": bool(criteria.later),
        "usertag": bool(criteria.user_tag),
        "locked": bool(criteria.locked_only),
    }


def bar_engaged(
    rows: dict[str, bool], *, excluded: frozenset[str] = BAR_EXCLUDED_ROWS,
) -> bool:
    """絞り込みの軸が 1 つでも非既定値か（軸集合は :func:`row_engaged` から導出）.

    軸集合を呼び出し側が自分の式で数え直すと、軸が増えたときに片方だけ追随
    する形が残る（かつて 5 軸版 / 8 軸版の 2 実装があり、欠けた軸を呼び出し側
    が手当てしていた）。
    """
    return any(
        engaged for key, engaged in rows.items() if key not in excluded
    )
