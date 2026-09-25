"""全面占有一覧の**組み立て** — Qt 非依存の値と純関数。

左ペインのグリッドを丸ごと奪う一覧は 2 種類ある — 横断キュレーション一覧
（★ / あとで見る / ユーザータグ）と「最近追加されたファイル」一覧。
:mod:`.overlay_list` の :class:`~.overlay_list.OverlayList` が**入場中の生きた
状態**（母集合 / 失敗 / 状態行 / 奪った並び順）を 1 本の値に閉じ込めているのに
対し、こちらはその値を **作る / 書き換える / 読み替える** 側の計算だけを持つ:

* 入場 1 回ぶんの指定 :class:`OverlaySession` — 母集合の組み立て（横断一覧の
  パス集合）と、退出時の復帰情報（入場前の並び順）を値で持つ。``PostGrid`` の
  2 つの入場口は「解決の投げ方」だけが違う薄い殻になる。
* 着地の翻訳 :class:`OverlayLanding` — off-thread の解決 / 走査が返したものを
  「母集合 + ゴースト + 件数 + 状態行」へ畳む。読み取り失敗を「まだありません」
  と言わない分岐（読めなかった行があって 1 件も拾えていない）はここ 1 か所。
* 一覧の上の絞り込み（素のテキスト項）・可視タイル限定の後追い解決の対象選び・
  ゴーストの印外しの軸選び — どれもウィジェットを見ない写像。

``PostGrid`` 側に残るのは「いつ呼ぶか」と「結果をどのウィジェットへ配るか」で、
判定の分岐はこのモジュールの単体テスト（``tests/test_viewer_grid_overlays.py``
— ``QApplication`` 不要）で表にできる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..common.i18n import t
from .curation_list import CurationList
from .curation_recovery import build_ghost_entries, prefix_rebind_group
from .filter_query import _match_filter_terms, _parse_filter_query
from .folder_scan import FolderEntry, RecentFilesScan
from .grid_empty_state import OverlayStatus
from .overlay_list import OverlayList
from .user_meta import CurationResolve, UserMeta, normalize_entry_key

__all__ = [
    "OverlayLanding",
    "OverlaySession",
    "applies_nsfw",
    "apply_landing",
    "curation_landing",
    "curation_pool_paths",
    "curation_status_line",
    "empty_status",
    "ghost_prefix_group",
    "ghost_unmark",
    "meta_targets",
    "narrow_by_fields",
    "narrow_by_text",
    "rebind_merge_check",
    "recent_landing",
    "recent_progress_status",
]


# ---------------------------------------------------------------- 母集合

def curation_pool_paths(
    meta_map: Mapping[str, object] | Iterable[tuple[str, object]],
    kind: str,
) -> list[str] | None:
    """*kind* の母集合パス（メモリ上のキュレーション地図の走査・I/O ゼロ）。

    未知の *kind* は ``None`` — 呼び出し側の入場ガードと件数表示
    (``PostGrid.curation_pool_count``) が同じ 1 実装を共有するため
    「入れないものは数えない」が構造的に揃う。

    *meta_map* は ``(path_key, meta)`` を配る写像で、``meta`` は ``star`` /
    ``later`` / ``tags`` を持てばよい（``user_meta.CurationMap`` そのもの）。
    """
    items = (
        meta_map.items() if hasattr(meta_map, "items")
        else meta_map  # type: ignore[assignment]
    )
    view = CurationList.from_kind(kind)
    if view is None:
        return None
    return [p for p, m in items if view.contains(m)]


@dataclass(frozen=True)
class OverlaySession:
    """占有一覧への入場 1 回ぶんの指定（母集合 + 復帰情報）。

    * ``kind`` — 一覧の同一性（:class:`~.curation_list.CurationList` か、
      最近追加一覧の対象フォルダ ``Path``）。表示名 / 単一クラム / 入場で奪う
      並び順はここからではなく :class:`~.overlay_list.OverlayList` が導く
      （導出点を二重に持たない）。
    * ``saved_sort`` — **退出時の復帰情報**。入場前にユーザーが選んでいた並び
      順で、一覧が奪った軸を返すときにそのまま使う。
    * ``pool`` — 横断一覧の母集合パス（最近追加一覧は走査で作るので ``None``）。

    生成の 2 つの classmethod が「入れるかどうか」も同時に答えるので、ホスト側
    の入場口は ``session is None`` の 1 ガードで済む。
    """

    kind: "CurationList | Path"
    saved_sort: str | None = None
    pool: tuple[str, ...] | None = None

    @classmethod
    def for_curation(
        cls,
        kind: str,
        meta_map: Mapping[str, object] | Iterable[tuple[str, object]],
        *,
        saved_sort: str | None = None,
    ) -> "OverlaySession | None":
        """横断キュレーション一覧の指定（未知の種別は ``None``）。

        母集合はここで組む — 入場ガード（入れるか）と件数（何件あるか）が
        :func:`curation_pool_paths` の 1 実装を共有する。
        """
        view = CurationList.from_kind(kind)
        if view is None:
            return None
        paths = curation_pool_paths(meta_map, view.key)
        if paths is None:
            return None
        return cls(kind=view, saved_sort=saved_sort, pool=tuple(paths))

    @classmethod
    def for_recent(
        cls, folder: Path | None, *, saved_sort: str | None = None,
    ) -> "OverlaySession | None":
        """最近追加一覧の指定（対象フォルダが無ければ ``None``）。"""
        if folder is None:
            return None
        return cls(kind=folder, saved_sort=saved_sort)

    def open_list(
        self, cancel: Callable[[], None] | None = None,
    ) -> OverlayList:
        """指定 → 入場中の生きた状態。*cancel* は所有ストリームの中止口。"""
        return OverlayList(
            kind=self.kind, saved_sort=self.saved_sort, cancel=cancel,
        )


# ---------------------------------------------------------------- 着地

@dataclass(frozen=True)
class OverlayLanding:
    """解決 / 走査の着地を「一覧の状態へ書き写す値」に翻訳したもの。

    ``discard`` が真なら母集合ごと捨ててから書く（読み取り失敗 — 拾えた分を
    中途半端に残さない）。``status`` は着地と同時に出す状態行で、
    :func:`apply_landing` の戻り値としてホストへ返る。
    """

    status: str = ""
    failed: bool = False
    discard: bool = False
    entries: "list[FolderEntry]" = field(default_factory=list)
    rel_paths: dict[str, str] = field(default_factory=dict)
    ghosts: "list[FolderEntry]" = field(default_factory=list)
    ghost_keys: set[str] = field(default_factory=set)
    ghost_missing_keys: set[str] = field(default_factory=set)
    missing: int = 0
    unreadable: int = 0


def curation_status_line(
    *, failed: bool, missing: int, unreadable: int,
) -> str:
    """横断一覧の件数行（消えた件数 / 読めなかった件数を出し分ける）。

    断定してよいのは確実に不在と分かった分（*missing*）だけ — 読めなかった分
    （ボリュームごと落ちている / 権限が外れている）を「移動または削除済み」と
    名乗ると、NAS 未接続が削除として報告される。
    """
    if failed:
        return t("viewer.post_grid.curation_failed")
    parts: list[str] = []
    if missing:
        parts.append(t("viewer.post_grid.curation_missing", n=missing))
    if unreadable:
        parts.append(t("viewer.post_grid.curation_unreadable", n=unreadable))
    return " / ".join(parts)


def curation_landing(
    payload: object,
    library_bases: list[tuple[Path, str]] | None = None,
) -> OverlayLanding:
    """横断一覧の off-thread 解決の着地を翻訳する。

    ワーカーが例外で落ちた（``None``）ときは母集合が空だったのと区別できない
    ので、**読み取り失敗**を名乗る。全滅（1 件も解決できず、読めなかった行が
    ある）も同じ席で、部分失敗はタイルが出るので件数行だけで出し分ける。

    読めずに落ちた行は追加 I/O ゼロのプレースホルダとして一覧末尾へ回る
    （:func:`~.curation_recovery.build_ghost_entries`）。そのキャプションは
    生きている行と同じ ``rel_paths`` へ相乗りさせる。
    """
    if not isinstance(payload, CurationResolve):
        return OverlayLanding(
            status=t("viewer.post_grid.curation_failed"),
            failed=True, discard=True,
        )
    ghosts, ghost_captions, ghost_missing = build_ghost_entries(
        payload.missing_paths, payload.unreadable_paths, library_bases,
    )
    rel_paths = dict(payload.rel_paths)
    rel_paths.update(ghost_captions)
    failed = not payload.entries and payload.unreadable > 0
    return OverlayLanding(
        status=curation_status_line(
            failed=failed,
            missing=payload.missing,
            unreadable=payload.unreadable,
        ),
        failed=failed,
        entries=list(payload.entries),
        rel_paths=rel_paths,
        ghosts=ghosts,
        ghost_keys={str(e.path) for e in ghosts},
        ghost_missing_keys=set(ghost_missing),
        missing=payload.missing,
        unreadable=payload.unreadable,
    )


def recent_progress_status(scanned: int = 0) -> str:
    """走査中の「探しています… / N 件走査」行（スロットルされた刻み）。"""
    parts = [t("viewer.post_grid.recent_loading")]
    if scanned:
        parts.append(t("viewer.post_grid.recursive_scanned", n=scanned))
    return " / ".join(parts)


def recent_landing(payload: object) -> OverlayLanding:
    """最近追加一覧の走査の着地を翻訳する。

    読み取り失敗を名乗るのは 3 通り: ワーカーが例外で落ちた（``None``）/
    ルートが読めない（``failed``）/ **走査に穴が空いた上で 1 件も拾えていない**。
    3 つ目は「読めなかった所以外の答えは立つ」という前提そのものが崩れた形で、
    読めた範囲がたまたま空なのと区別できない。「ファイルはありません」と言うと
    共有の切断やフォルダ削除を「増えていない」と読ませてしまう。

    成功したときは上限で切ったこと（「N 件中 新しい M 件」）も、読み取れなかった
    フォルダがあったこと（「一部読み取れませんでした」）も黙らない。
    """
    if (
        not isinstance(payload, RecentFilesScan)
        or payload.failed
        or (not payload.files and payload.incomplete)
    ):
        return OverlayLanding(
            status=t("viewer.post_grid.recent_failed"),
            failed=True, discard=True,
        )
    if payload.truncated:
        summary = t(
            "viewer.post_grid.recent_truncated",
            total=payload.total, shown=len(payload.files),
        )
    else:
        summary = t("viewer.post_grid.recent_done", n=payload.total)
    if payload.incomplete:
        summary = " / ".join([summary, t("viewer.post_grid.scan_partial")])
    return OverlayLanding(
        status=summary,
        entries=[entry for entry, _rel in payload.files],
        rel_paths={str(entry.path): rel for entry, rel in payload.files},
    )


def apply_landing(overlay: OverlayList, landing: OverlayLanding) -> str:
    """着地を一覧の状態へ書き写し、出すべき状態行を返す。

    in-flight の印（``pending``）はどちらの着地でも必ず降りる。``discard`` の
    ときだけ母集合を捨ててから失敗を立てる — 拾えた分を残すと「0 タイルなのに
    母集合あり」と読まれて空状態が ``*_filtered``（絞り込みのせい）へ落ちる。
    """
    overlay.pending = False
    if landing.discard:
        overlay.reset_population()
        overlay.failed = landing.failed
        return landing.status
    overlay.entries = list(landing.entries)
    overlay.rel_paths = dict(landing.rel_paths)
    overlay.ghosts = list(landing.ghosts)
    overlay.ghost_keys = set(landing.ghost_keys)
    overlay.ghost_missing_keys = set(landing.ghost_missing_keys)
    overlay.missing = landing.missing
    overlay.unreadable = landing.unreadable
    overlay.failed = landing.failed
    return landing.status


# ---------------------------------------------------------------- 読み替え

def empty_status(
    overlay: OverlayList | None, *, narrowing_engaged: bool,
) -> OverlayStatus | None:
    """0 タイル分類が要る観測値だけを抜き出す（``None`` = 一覧に居ない）。

    分類そのものは :func:`~.grid_empty_state.overlay_zero_kind` が持つので、
    ここは値の引き写しだけ — 「母集合があるか」の読み方（ゴーストは母集合の
    一員ではない）がホスト側と空状態側で食い違わないように 1 か所に置く。
    """
    if overlay is None:
        return None
    return OverlayStatus(
        prefix=overlay.prefix,
        pending=overlay.pending,
        failed=overlay.failed,
        has_population=bool(overlay.entries),
        narrowing_engaged=narrowing_engaged,
    )


def narrow_by_text(
    entries: "Sequence[FolderEntry]",
    rel_paths: Mapping[str, str],
    includes: Sequence[str],
    excludes: Sequence[str],
    or_pool: Sequence[str],
) -> "list[FolderEntry]":
    """一覧の母集合を素のテキスト項で絞る（名前 / 投稿タイトル / 相対パス）。

    照合面は 3 つの連結で、*rel_paths* が両一覧の唯一の差（横断一覧は
    ライブラリ基準、最近追加一覧はルート基準の相対パス）。``~`` プールは
    **いずれか 1 つ**が当たれば通す。効いている項が 1 つも無ければ入力を
    そのまま返す（素の一覧でコピーを作らない）。
    """
    if not includes and not excludes and not or_pool:
        return list(entries)

    def _match(e: "FolderEntry") -> bool:
        rel = rel_paths.get(str(e.path), "")
        hay = " ".join([e.path.name, e.title, rel]).casefold()
        if any(exc in hay for exc in excludes):
            return False
        if not all(inc in hay for inc in includes):
            return False
        return not or_pool or any(alt in hay for alt in or_pool)

    return [e for e in entries if _match(e)]


def narrow_by_fields(
    entries: "Sequence[FolderEntry]",
    filter_text: str,
    fields: "Iterable[str]",
    curation: Callable[[Path], tuple] | None = None,
) -> "list[FolderEntry]":
    """一覧の母集合を分野限定項で絞る（*fields* に載る種別だけ）。

    載せてよいのは、この母集合でも**メモリだけで**答えられる項
    （``name:`` / ``title:`` / ``star:`` / ``mytags:`` / ``later:``）。
    ``~`` プール項は対象外 — 効かない代替と効く代替が同じプールに混ざると、
    プール全体の意味が変わる。効く項が無ければ入力をそのまま返す。
    """
    allowed = set(fields)
    terms = [
        term for term in _parse_filter_query(filter_text)
        if term.field in allowed and not term.or_group
    ]
    if not terms:
        return list(entries)
    return [
        e for e in entries
        if _match_filter_terms(
            e, terms, include_file_names=False, curation=curation,
        )
    ]


def applies_nsfw(overlay: OverlayList) -> bool:
    """この一覧に「年齢制限を隠す」を効かせるか。

    効かせるのは最近追加一覧だけ。横断キュレーション一覧の母集合は**利用者
    自身が印を付けた項目の再掲**なので、見せ方の設定で間引かない（AI 検索結果
    は利用者が選んだ集合ではないので効かせる側）。
    """
    return overlay.recent is not None


def ghost_prefix_group(
    overlay: OverlayList | None,
    path: Path,
    library_bases: list[tuple[Path, str]] | None = None,
):
    """*path* と同じ根を共有するゴースト群（無ければ ``None``）。

    材料は ``overlay.ghost_keys`` = 解決が**到達不能と確定させた**行だけなので、
    一括張り替えの対象に「いまそこにある行」が混ざらない（純パス演算・
    追加 I/O ゼロ）。
    """
    if overlay is None or not overlay.ghost_keys:
        return None
    return prefix_rebind_group(overlay.ghost_keys, path, library_bases)


def rebind_merge_check(
    meta_map, old_path: str, picked: str,
) -> "tuple[UserMeta, UserMeta] | None":
    """張り替え先が既にキュレーション済みなら ``(元の印, 先の印)``、他は ``None``。

    無確認の不可逆併合にしないための判定 — 張り替えは行き先の独自の表明
    （★2 等）を黙って上書きし得る。参照はメモリ上の地図だけ（綴り違いは
    正規化して引く・I/O ゼロ）。新旧が同じ鍵へ畳まれる「単なる綴り替え」は
    併合ではないので確認しない。
    """
    dst_meta = meta_map.get(picked)
    if (
        dst_meta is None
        or dst_meta.is_empty()
        or normalize_entry_key(picked) == normalize_entry_key(old_path)
    ):
        return None
    return (meta_map.get(old_path) or UserMeta(), dst_meta)


def meta_targets(
    tiles: "Iterable[tuple[str, bool]]",
    entries: "Sequence[FolderEntry]",
    is_requested: Callable[[str], bool],
) -> "list[tuple[str, FolderEntry]]":
    """可視タイルのうち post.md を後追いで読ませる対象を選ぶ。

    *tiles* はビューポート内の ``(パス文字列, フォルダか)``（表示順）。
    受付済みの鍵・既にメタが載っている行・母集合に居ない行・ファイル行は
    落とす。上限（1 tick の投入量）は解決器の仕事なのでここでは掛けない —
    「可視で未解決なのはどれか」だけを答える。
    """
    by_path = {str(e.path): e for e in entries}
    wanted: "list[tuple[str, FolderEntry]]" = []
    for key, is_dir in tiles:
        if not is_dir or is_requested(key):
            continue
        entry = by_path.get(key)
        if entry is None or entry.metadata_loaded:
            continue
        wanted.append((key, entry))
    return wanted


def ghost_unmark(
    view: CurationList, tags: "Sequence[str] | None" = None,
) -> tuple[str, object]:
    """「この一覧から外す」で書く印 ``(kind, value)``（*view* の軸だけ）。

    外すのは**その一覧が about な軸だけ**: 他の軸の表明は別の一覧にまだ属して
    いるかもしれないので巻き添えにしない。タグ一覧では *tags* からその 1 つ
    だけを大文字小文字無視で抜いた残りを返す。軸の対応は台帳
    (:meth:`~.curation_list.CurationList.unmark`) が持つ。
    """
    return view.unmark(tags)
