"""``user_meta`` の解決層 — 改名追従の台帳と横断一覧のパス解決（Qt 非依存）.

``user_meta.py`` に同居していた「ストアの外側」= **パスを解決する側**をここへ
切り出した置き場。ストア :class:`~snappix.viewer.user_meta.UserMetaStore` 本体
と、行の同一性を決めるキー計算（:func:`~snappix.viewer.user_meta.absolute_spelling`
/ :func:`~snappix.viewer.user_meta.normalize_entry_key`）は ``user_meta.py`` に
残る — キー経路があのファイルの中で閉じることを
``tests/test_viewer_user_meta_path_keys.py`` が静的に要求しているため（#133。
呼び先が別モジュールにあると閉包走査が本体を一度も検査しないまま素通りする）。

ここに居るのは 3 組:

* **到達性の判定** — :func:`_is_gone_exc` / :func:`_is_definitely_gone` /
  :func:`_anchor_reachable`。「消えた」と「読めなかった」を取り違えないための
  判定で、ストアの張り替え
  （:meth:`~snappix.viewer.user_meta.UserMetaStore.resolve_moved_entries`）と
  横断一覧の解決（:func:`resolve_curation_paths`）が同じ 1 実装を共有する
  （#151）。
* **改名追従の台帳** — :class:`MovedResolver` と、``post.md`` の頭だけを読んで
  それを組む :func:`build_moved_resolver`（ワーカースレッド専用）。
* **横断一覧のパス解決** — :func:`build_curation_entry` /
  :func:`_curation_display_name` / :class:`CurationResolve` /
  :func:`resolve_curation_paths`（H01）。

依存の向きは **解決層 → ストアは無し**（このモジュールは ``user_meta.py`` を
import しない）。``user_meta.py`` が従来の名前を全て re-export するので、外から
見た import 面（``from .user_meta import resolve_curation_paths`` 等）は分割前と
変わらない。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


def _is_gone_exc(exc: OSError) -> bool:
    """「確実に不在」と読める ``OSError`` か（``ENOENT`` / ``ENOTDIR``）。

    判定そのもの — 既に ``stat`` を撃って例外を握っている呼び出し元は、
    :func:`_is_definitely_gone` を呼び直さずにこちらを使う（同じパスを 2 度
    stat しない。死んだ共有では 1 回が 15〜195 秒ブロックしうる）。
    """
    return isinstance(exc, (FileNotFoundError, NotADirectoryError))


def _is_definitely_gone(path: Path) -> bool:
    """True only when *path* is **provably** absent (``ENOENT`` / ``ENOTDIR``).

    ``Path.exists()`` swallows every ``OSError`` and answers ``False``, so an
    offline share, a denied ACL or a transient I/O error looks exactly like a
    deleted folder.  :meth:`UserMetaStore.resolve_moved_entries` acts on that
    answer by **rewriting** the row's path (the only non-regenerable user data
    the viewer holds, with no undo), so "we couldn't tell" must not be read as
    "it's gone" — an unreachable NAS entry would otherwise have its star moved
    onto a local copy carrying the same postref (#151).
    """
    try:
        path.stat()
    except OSError as exc:
        if _is_gone_exc(exc):
            return True
        # unreachable share / denied / busy — unknown
        logger.debug("user_meta: cannot probe {}: {}", path, exc)
        return False
    return False


def _anchor_reachable(path: Path, cache: dict[str, bool]) -> bool:
    """Whether the drive / UNC share *path* lives on answers at all.

    Windows reports an unresolvable UNC host as a plain "path not found"
    (``FileNotFoundError``), which :func:`_is_definitely_gone` cannot tell from
    a genuinely deleted folder.  Probing the **anchor** (``D:\\`` /
    ``\\\\nas\\lib\\``) separates the two: a reachable anchor means the entry
    really did move, an unreachable one means the whole volume is offline and
    nothing under it may be repointed (#151).  Cached per anchor so a store
    full of NAS rows costs one probe, not one per row.
    """
    anchor = path.anchor
    if not anchor:  # relative path — nothing to probe
        return True
    cached = cache.get(anchor)
    if cached is None:
        try:
            cached = Path(anchor).exists()
        except OSError:  # pragma: no cover (defensive)
            cached = False
        cache[anchor] = cached
    return cached


class MovedResolver:
    """Maps a ``(service, post_id)`` postref → the folder that now carries it.

    Built off the GUI thread by scanning post folders' ``post.md`` heads; a
    simple dict wrapper so :meth:`UserMetaStore.resolve_moved_entries` stays
    storage-only and unit-testable with a hand-built resolver.
    """

    def __init__(self, mapping: dict[tuple[str, str], Path]) -> None:
        self._map = mapping

    def folder_for(self, service: str, post_id: str) -> Path | None:
        return self._map.get((service, post_id))


def build_moved_resolver(
    root: Path,
    wanted: set[tuple[str, str]],
    *,
    max_folders: int = 20000,
    should_cancel: Callable[[], bool] | None = None,
) -> MovedResolver:
    """Walk *root* for post folders and map each wanted postref to its folder.

    Reads only the bounded ``post.md`` head of each folder (via
    :func:`snappix.viewer.post_md.read_post_identity`) and records the folder for
    any ``(service, post_id)`` in *wanted* — so the walk stays cheap and stops
    early once every wanted postref is found.  ``max_folders`` bounds the walk
    on a pathological tree.  **Runs on a worker thread** (does NAS ``os.scandir``
    + reads) — never call it on the GUI thread.

    走査そのものは共有 BFS :func:`snappix.viewer.folder_scan._iter_tree` に
    委ねる（レビュー 2026-09-03 項目#88）。以前はここに 3 本目の手書きウォークが
    あり、共有された性質のうち**サイクルガードだけ**を借りて残りを落として
    いた:

    * **キャンセルが無かった** — 呼び出し元 (``ViewerWindow._kick_rename_follow``)
      の世代トークンは**結果を捨てる**だけでウォークは止まらず、
      窓を閉じてもルートを切り替えても最大 ``max_folders`` 件ぶんの NAS
      ``os.scandir`` が最後まで走り、同居する短命プローブがグローバル
      ``QThreadPool`` の枠をこの長寿命タスクに食われていた。いまは
      *should_cancel* を受け、``_iter_tree`` の 256 件刻みのポーリングで
      協調的に止まる（早期終了 = 「欲しい postref が全部見つかった」も
      同じ述語に畳んである）。
    * **フォルダごとに余分な ``stat``** — ``(folder / "post.md").is_file()`` は
      1 フォルダ 1 往復で、直後の ``os.scandir`` が同じ子一覧を返すので完全な
      重複だった。``_iter_tree`` が返す**ファイルエントリ**の名前を見る形に
      すれば往復はゼロ（20,000 フォルダで 20,000 回の NAS stat 削減）。
    * **読めなかったフォルダを誰にも言わなかった** — ``except OSError:
      continue`` で黙って落としていたので、一時的に読めなかったサブツリー
      配下の★は追従されず、その事実がログにも残らなかった。いまは
      ``on_dir_error`` で数えて 1 行の警告にする。

    Cycles are guarded the same way every other recursive walk in the viewer
    guards them — the shared :func:`snappix.viewer.folder_scan.should_descend`
    predicate (#44), now reached through ``_iter_tree``: junctions / symlinks
    are **followed** and only identity de-duplication stops loops, so stars
    under a junction-mounted subtree keep following renames too (方針決定
    2026-08-28 — the walk is read-only, so descending links carries none of the
    health check's deletion risk).  Without the guard an ancestor-pointing link
    re-walks the same subtree until ``max_folders`` is burnt, so folders outside
    the loop are never reached and the rename following silently loses stars
    (#96).

    Returns a :class:`MovedResolver` (possibly partial) for
    :meth:`UserMetaStore.resolve_moved_entries`.
    """
    from ..folder_scan import POST_MD_NAME, _iter_tree
    from ..post_md import read_post_identity

    mapping: dict[tuple[str, str], Path] = {}
    if not wanted:
        return MovedResolver(mapping)
    remaining = set(wanted)
    folders = 0
    unreadable = 0

    def _stop() -> bool:
        return (
            not remaining
            or folders >= max_folders
            or (should_cancel is not None and should_cancel())
        )

    def _note(_folder: Path) -> None:
        nonlocal unreadable
        unreadable += 1

    for entry, _direct in _iter_tree(
        root, should_cancel=_stop, on_dir_error=_note,
    ):
        try:
            is_dir = entry.is_dir()
        except OSError:  # pragma: no cover — _iter_tree already skipped these
            continue
        if is_dir:
            folders += 1
            if folders >= max_folders:
                break
            continue
        if entry.name != POST_MD_NAME:
            continue
        md = Path(entry.path)
        service, post_id = read_post_identity(md)
        if service and post_id:
            key = (service, post_id)
            if key in remaining:
                mapping[key] = md.parent
                remaining.discard(key)
                if not remaining:
                    break
    if unreadable:
        # 「読めなかった」を「移動していない」として黙らせない（#64 と同じ弁）。
        logger.warning(
            "rename following could not read {} folder(s) under {} —"
            " stars below them may not follow their rename this time",
            unreadable, root,
        )
    return MovedResolver(mapping)


#: 先頭省略で残す末尾成分の数（ライブラリ基準の相対パスが深すぎるとき）。
_CURATION_NAME_MAX_PARTS = 3
_CURATION_ELLIPSIS = "…"


def _curation_display_name(
    path: Path, library_bases: list[tuple[Path, str]] | None = None,
) -> str:
    """A short, context-carrying caption for a cross-library curation tile.

    The 「あとで見る一覧」 / 「スター付き一覧」 (H01) pools entries from all over
    the library into one flat grid, where a bare basename ("image01.jpg") is
    ambiguous.  The caption therefore names **where in the library** the entry
    lives.

    UIレビュー 2026-08-28 N-49: 旧実装は ``path.parts[-3:]`` 固定だったため、
    投稿フォルダ（``library/作家/投稿``）は無意味な先頭「library」が付き、
    ファイル（``library/作家/投稿/img.jpg``）は「作家/投稿/img.jpg」と、
    **種別で基準の深さが変わっていた**。基準はパンくず / ナビレール / 全文検索と
    同じ :func:`~snappix.common.fsutil.pick_library_base`（最も浅い登録
    ライブラリ）へ一本化し、そこからの相対パスを出す。深すぎるときだけ先頭を
    ``…`` に畳むので、末尾（＝実体の名前）は必ず残る。

    *library_bases* が空 / 基準の外なら従来どおり末尾 3 成分にフォールバック
    する（ライブラリ登録が無い起動でもキャプションが消えない）。
    """
    from ...common.fsutil import pick_library_base

    picked = pick_library_base(path, library_bases)
    if picked is not None:
        _base, _label, rel = picked
        parts: tuple[str, ...] = rel
        if not parts:  # 基準ライブラリそのもの
            return path.name or str(path)
        if len(parts) > _CURATION_NAME_MAX_PARTS:
            return "/".join(
                (_CURATION_ELLIPSIS, *parts[-_CURATION_NAME_MAX_PARTS:])
            )
        return "/".join(parts)
    parts = path.parts[-_CURATION_NAME_MAX_PARTS:]
    return "/".join(parts) if parts else path.name


def build_curation_entry(path: Path, *, is_dir: bool, mtime: float, size: int):
    """Build a display :class:`FolderEntry` for a curated *path* (Qt-free).

    Folders get ``thumbnail_resolved=False`` so the grid's loader resolves the
    representative image lazily (``#thumb#`` aware), exactly as a normal
    shallow-scan folder tile does; files get their own path as the thumbnail
    source when thumbnailable.

    **No ``post.md`` is read while the list is built** — the cross-library list
    gathers entries from all over the library (potentially several volumes), so
    a metadata pass over the whole pool would be a NAS round-trip per entry
    before a single tile appears.  Folders therefore land with
    ``metadata_loaded=False`` and the grid resolves post.md **for the visible
    tiles only**, after the fact (``PostGrid._schedule_curation_meta``, UIレビュー
    2026-08-28 N-49 後半) — the same "viewport-limited resolution" discipline the
    thumbnail loader and the aspect prober follow.
    """
    from ..folder_scan import THUMBNAILABLE_SUFFIXES, FolderEntry

    if is_dir:
        return FolderEntry(
            path=path, title=path.name, has_post_md=False,
            thumbnail_path=None, thumbnail_resolved=False,
            mtime=mtime, is_dir=True, size=0, metadata_loaded=False,
        )
    thumbable = path.suffix.lower() in THUMBNAILABLE_SUFFIXES
    return FolderEntry(
        path=path, title=path.name, has_post_md=False,
        thumbnail_path=path if thumbable else None, thumbnail_resolved=True,
        mtime=mtime, is_dir=False, size=size, metadata_loaded=True,
    )


@dataclass(frozen=True)
class CurationResolve:
    """Outcome of :func:`resolve_curation_paths` (H01).

    ``missing`` and ``unreadable`` are deliberately **separate** buckets
    (UIレビュー 2026-08-28 N-09 / 旧 N-68).  The historical single ``missing``
    folded every ``OSError`` together and the UI then asserted 「移動または
    削除済み」 — so an unplugged NAS was reported as deleted curation, and a
    list whose entries all lived on that share showed 「まだありません」, i.e.
    「印を付けていない」.  Both readings are the opposite of the truth.

    * ``missing_paths`` — **provably** gone (``ENOENT`` / ``ENOTDIR`` on a
      drive / share that still answers), the only case the assertive wording
      fits.
    * ``unreadable_paths`` — the probe failed for any other reason, or the
      entry's whole volume is offline: we simply could not tell.

    落ちた行は**件数ではなくパスそのもの**で持つ (#133 項目 3):
    ``viewer/curation_recovery.py`` がこれをプレースホルダタイルとして一覧
    末尾に出し、「現在の場所を指定…」→ :meth:`UserMetaStore.rebind_path` の
    張り替え起点にする。件数だけだと「どの行が失われたか」が UI に一切
    現れず、数字のズレでしか気付けなかった。``missing`` / ``unreadable`` は
    ステータス行互換の導出値。
    """

    entries: list
    rel_paths: dict[str, str]
    missing_paths: tuple[str, ...] = ()
    unreadable_paths: tuple[str, ...] = ()

    @property
    def missing(self) -> int:
        return len(self.missing_paths)

    @property
    def unreadable(self) -> int:
        return len(self.unreadable_paths)


def resolve_curation_paths(
    paths: list[str],
    library_bases: list[tuple[Path, str]] | None = None,
    *,
    should_cancel: Callable[[], bool] | None = None,
):
    """Resolve curated path strings to on-disk entries (H01, off the GUI thread).

    Returns a :class:`CurationResolve`: the resolved :class:`FolderEntry` list
    (folders and files interleaved in input order), a
    ``{str(path): short_caption}`` map for the grid caption, and the two
    failure counts described there.  Each path is an ``os.stat`` that can block
    for seconds on a half-mounted NAS share, so this MUST run on a worker
    thread — never call it while painting.

    *library_bases* is ``ViewerWindow._compute_library_bases()`` — the same
    ``(path, label)`` list the breadcrumb gets — used only to make the caption
    library-relative (N-49).  Pure path arithmetic, no extra I/O.

    *should_cancel* is polled before every ``os.stat`` (レビュー 2026-09-03
    項目 #215 追補): the owning ``GuardedStream`` passes its job's
    ``cancel.is_cancelled`` so that an exit / re-enter / window close stops
    the per-path loop cooperatively instead of grinding through the rest of a
    dead share (one stat there can block 15–195 s).  A cancelled call returns
    what it resolved so far; the landing slot drops it by generation anyway.
    """
    import stat as _stat

    entries = []
    rel: dict[str, str] = {}
    missing: list[str] = []
    unreadable: list[str] = []
    anchor_ok: dict[str, bool] = {}
    for p in paths:
        if should_cancel is not None and should_cancel():
            break
        path = Path(p)
        try:
            info = path.stat()
        except OSError as exc:
            # 「消えた」と「読めない」を取り違えない (#151 と同じ判定を再利用):
            # 確実に不在 かつ そのボリュームが応答している ときだけ missing。
            # パスは捨てずに持ち帰る — プレースホルダ表示と張り替え導線の
            # 母集合になる (#133 項目 3)。判定は**今握っている例外**から行う
            # （`_is_definitely_gone` を呼ぶと同じパスを 2 度 stat することに
            # なり、死んだ共有ではその 1 回が 15〜195 秒ブロックする）。
            if _is_gone_exc(exc) and _anchor_reachable(path, anchor_ok):
                missing.append(str(path))
            else:
                logger.debug("user_meta: cannot probe {}: {}", path, exc)
                unreadable.append(str(path))
            continue
        is_dir = _stat.S_ISDIR(info.st_mode)
        entries.append(
            build_curation_entry(
                path, is_dir=is_dir, mtime=info.st_mtime,
                size=0 if is_dir else info.st_size,
            )
        )
        rel[str(path)] = _curation_display_name(path, library_bases)
    return CurationResolve(entries, rel, tuple(missing), tuple(unreadable))


__all__ = [
    "CurationResolve",
    "MovedResolver",
    "build_curation_entry",
    "build_moved_resolver",
    "resolve_curation_paths",
]
