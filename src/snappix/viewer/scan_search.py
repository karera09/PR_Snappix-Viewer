"""Off-thread recursive search scanner + shared search-source helpers.

:class:`RecursiveSearchScanner` (file-name / structure search over
descendants: search-index seed + authoritative live walk) feeds the
PostGrid's "サブフォルダも検索" mode off the GUI thread, producing
``list[tuple[FolderEntry, rel]]``.  It mirrors
:class:`~.scan_children.ChildrenScanner`'s concurrency model: a monotonic
generation counter, a cooperative :class:`~.cancel_token.CancelToken`, and a
single-thread ``QThreadPool`` so rapid filter edits never stack overlapping
NAS walks.

This module also hosts the **shared infrastructure for non-live-walked
search sources**: the two-phase emit skeleton
(:func:`emit_with_existence_check` — emit every match instantly, then
re-emit the pruned/enriched set once an existence check has run), the
existence check itself (:func:`_existing_paths`), and the posted_at
seeding helpers.  Their primary consumers today are the AI-tag / vector
scanners, which live in the official AI plugin
(``plugins/snappix_ai/engine/scanners.py``) and import these helpers by
name — treat their signatures as a contract.

The shallow children scanner lives in :mod:`scan_children`; :mod:`scan_worker`
re-exports both for backwards-compatible imports.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

if TYPE_CHECKING:  # import-time-free annotations for injected collaborators
    from .folder_preview_cache import FolderPreviewCache
    from .search_index import SearchIndex

from .cancel_token import CancelToken as _CancelToken
from .cancel_token import SessionOwner
from .folder_scan import (
    THUMBNAILABLE_SUFFIXES,
    FolderEntry,
    walk_for_search,
)
from .perf import measure
from .post_md import read_post_meta
from .search_index import NodeRow

#: 再帰走査のヒット上限（``walk_for_search`` の既定と同値）。上限に達したら
#: 走査を打ち切り、``walk_truncated`` で告げる。
_WALK_MAX_HITS = 50000


# --------------------------------------------------------------------------- #
# Recursive file-name / structure search
# --------------------------------------------------------------------------- #

def _node_row_to_entry(row, root: Path) -> tuple[FolderEntry, str]:
    """Convert a search-index ``NodeRow`` into the ``(FolderEntry, rel)``
    shape ``walk_for_search`` produces, so cache-seeded and live-walked hits
    are indistinguishable to the grid.  No filesystem I/O — the index's
    stored mtime / size are trusted (the live walk re-surfaces stale rows)."""
    try:
        rel = row.path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover (defensive)
        rel = row.path.name
    if row.is_dir:
        entry = FolderEntry(
            path=row.path,
            title=row.name,
            has_post_md=False,
            thumbnail_path=None,
            thumbnail_resolved=False,
            posted_at=None,
            tags=[],
            locked_count=0,
            mtime=row.mtime,
            is_dir=True,
            size=0,
            metadata_loaded=False,
        )
    else:
        thumbable = row.path.suffix.lower() in THUMBNAILABLE_SUFFIXES
        entry = FolderEntry(
            path=row.path,
            title=row.name,
            has_post_md=False,
            thumbnail_path=row.path if thumbable else None,
            thumbnail_resolved=True,
            posted_at=None,
            tags=[],
            locked_count=0,
            mtime=row.mtime,
            is_dir=False,
            size=row.size,
            metadata_loaded=True,
        )
    return entry, rel


class _RecursiveSearchSignals(QObject):
    # (generation, list[tuple[FolderEntry, str]], is_seed)
    results_ready = Signal(int, list, bool)
    progress = Signal(int, int)  # (generation, entries_scanned_so_far)
    # (generation, unreadable_dirs) — 列挙できなかったディレクトリがあった
    # （ルート自身が読めず走査が丸ごと成立しなかった場合も 1 件と数える）。
    # ``results_ready`` の**直前**に（同じブリッジから同じ受け手へ）投函するので、
    # 消費側は結果を受け取った時点で穴の有無を知っている。
    walk_incomplete = Signal(int, int)
    # (generation, cap) — 上限に達して走査を打ち切った。``walk_incomplete``
    # と同じ「結果より先に穴を告げる」規約で ``results_ready`` の直前に投函
    # する。既存消費者の契約を壊さないよう、``walk_incomplete`` の引数を
    # 増やすのではなく別のシグナルにする。
    walk_truncated = Signal(int, int)


class _RecursiveSearchTask(QRunnable):
    """Run a recursive descendant search entirely off the GUI thread.

    Two phases, both emitted via ``results_ready`` with an ``is_seed`` flag:

    1. **Seed** (``is_seed=True``) — query the search index for cached
       matches and convert them with zero filesystem I/O.  Fast, so the grid
       paints near-instantly while the authoritative walk runs.
    2. **Live** (``is_seed=False``) — :func:`walk_for_search`, filtered by
       ``includes`` / ``excludes`` / ``or_terms`` on THIS thread so the GUI receives only
       matches (no O(descendants) substring scan on the main thread, which
       was the source of the "results appear" freeze on large hit sets).

    Cancellation follows the same cooperative ``_CancelToken`` pattern as
    :class:`~.scan_children._ScanChildrenTask`; stale emissions are dropped by
    the consumer via generation comparison.
    """

    def __init__(
        self,
        generation: int,
        root: Path,
        signals: _RecursiveSearchSignals,
        cancel: _CancelToken,
        includes: list[str] | None = None,
        excludes: list[str] | None = None,
        or_terms: list[str] | None = None,
        search_index: SearchIndex | None = None,
        max_hits: int = _WALK_MAX_HITS,
    ) -> None:
        super().__init__()
        self.generation = generation
        self.root = root
        self.signals = signals
        self.cancel = cancel
        self.includes = includes
        self.excludes = excludes or []
        self.or_terms = or_terms or []
        self.search_index = search_index
        self.max_hits = max_hits
        self.setAutoDelete(True)

    def run(self) -> None:
        # Already superseded before this task ever got a thread?  The pool is
        # single-threaded, so a task queued behind a running one can be
        # cancelled (root change / newer keystroke) long before it starts.
        # ``query_filenames`` below takes no cancel token and cannot be
        # abandoned mid-flight, so without this guard a stale task runs the
        # whole index query only to drop it at the check further down —
        # delaying the *current* query's seed by that much.
        if self.cancel.is_cancelled():
            return
        # Step 1: cache seed (index query → convert), off-thread.
        if self.search_index is not None and (self.includes or self.or_terms):
            try:
                rows = self.search_index.query_filenames(
                    self.root, self.includes or [], self.excludes,
                    or_terms=self.or_terms,
                )
                seed = [_node_row_to_entry(r, self.root) for r in rows]
            except Exception as exc:  # pragma: no cover (defensive)
                logger.debug("recursive search seed failed: {}", exc)
                seed = []
            if self.cancel.is_cancelled():
                return
            if seed:
                self.signals.results_ready.emit(self.generation, seed, True)

        # Step 2: authoritative live walk, filtered on this worker thread.
        # 列挙できなかったディレクトリを数える — 0 件を「該当なし」と断定
        # させないため（最近追加一覧の
        # ``RecentFilesScan.unreadable_dirs`` と同じ趣旨）。
        unreadable = 0

        def _note_dir_error(_path: Path) -> None:
            nonlocal unreadable
            unreadable += 1

        # 上限で打ち切られた走査も「穴のある走査」— 件数が全件の顔をしない
        # ように、結果より先に告げる。
        truncated = False

        def _note_truncated() -> None:
            nonlocal truncated
            truncated = True

        # ルート自身が読めない（NAS 切断・権限変更・削除）ときは
        # ``walk_for_search`` が本体に入る前に ``root.is_dir()`` で抜ける
        # ＝ ``on_dir_error`` は 1 度も呼ばれない。そのまま 0 件を渡すと
        # 消費側が「該当なし」と断定してしまうので、走査全体が読めなかった
        # 1 件として数える（``_iter_tree`` 内の失敗と同じ告知チャネル）。
        try:
            root_readable = self.root.is_dir()
        except OSError:  # pragma: no cover (is_dir swallows OSError itself)
            root_readable = False
        if not root_readable:
            unreadable += 1
        try:
            with measure("recursive_search", str(self.root)):
                results = walk_for_search(
                    self.root,
                    should_cancel=self.cancel.is_cancelled,
                    on_progress=lambda n: self.signals.progress.emit(
                        self.generation, n
                    ),
                    on_dir_error=_note_dir_error,
                    includes=self.includes,
                    excludes=self.excludes,
                    or_terms=self.or_terms,
                    max_hits=self.max_hits,
                    on_truncated=_note_truncated,
                )
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("walk_for_search failed for {}: {}", self.root, exc)
            results = []
            # 例外で終わった走査も「穴のある走査」— 0 件を完全な答えとして
            # 語らせない（上の早期 return と同じ理由）。
            unreadable += 1
        if self.cancel.is_cancelled():
            return
        if unreadable:
            self.signals.walk_incomplete.emit(self.generation, unreadable)
        if truncated:
            self.signals.walk_truncated.emit(self.generation, self.max_hits)
        # Emit FIRST so the grid updates immediately — the index write-back
        # below runs afterwards on this (now otherwise-idle) worker thread,
        # so it adds no latency to the search and no GUI-thread load.  The
        # index has its own RLock+WAL, so it won't block live readers.
        self.signals.results_ready.emit(self.generation, results, False)
        if self.search_index is not None and results:
            try:
                self.search_index.upsert_nodes(
                    [
                        NodeRow(e.path, e.path.name, e.is_dir, e.mtime, e.size)
                        for e, _rel in results
                    ]
                )
            except Exception as exc:  # pragma: no cover (defensive)
                logger.debug("recursive search index write-back failed: {}", exc)


class RecursiveSearchScanner(QObject):
    """Recursive descendant scanner for the PostGrid "サブフォルダも検索" mode.

    Same generation + ``_CancelToken`` cooperative-cancel pattern as
    :class:`~.scan_children.ChildrenScanner`.  Distinct ``QThreadPool`` (size
    1) so a flurry of filter-text edits doesn't stack overlapping NAS walks —
    the previous walk is signalled to abort before the next one runs.

    Emits :sig:`results_ready(int generation, list, bool is_seed)` — the
    cache seed (``is_seed=True``) then the authoritative live walk — and,
    when the walk could not enumerate some directory,
    :sig:`walk_incomplete(int generation, int unreadable_dirs)` just before
    the live results, so the consumer never presents a holed answer as a
    confident 「N 件」/「該当なし」.  上限に達して走査を打ち切ったときは同じ
    地点で :sig:`walk_truncated(int generation, int cap)` も出る（件数が
    全件の顔をしないように）。
    """

    results_ready = Signal(int, list, bool)
    progress = Signal(int, int)  # (generation, entries_scanned_so_far)
    walk_incomplete = Signal(int, int)  # (generation, unreadable_dirs)
    walk_truncated = Signal(int, int)  # (generation, cap)

    def __init__(
        self, search_index: SearchIndex | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._signals = _RecursiveSearchSignals()
        # 転送先は**メソッド**にする（``self.results_ready.emit`` を直接つながない）
        # — 理由は ``ChildrenScanner.__init__`` の同じ箇所のコメント（self の
        # 破棄後に着地した queued 結果が "Signal source has been deleted" を
        # 送出しない）。
        self._signals.results_ready.connect(self._forward_results_ready)
        self._signals.progress.connect(self._forward_progress)
        self._signals.walk_incomplete.connect(self._forward_walk_incomplete)
        self._signals.walk_truncated.connect(self._forward_walk_truncated)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        # 世代とキャンセルのペアは SessionOwner が 1 箇所で持つ:
        # ``start()`` = 旧セッション cancel + 世代++、``cancel()``
        # = cancel + 世代++（キャンセル済みタスクが emit 済みの結果を消費側の
        # ``generation == latest_generation()`` ガードで確実に落とす）。
        self._sessions = SessionOwner()
        # Optional SearchIndex: seeds the instant cache phase AND receives a
        # write-back of the live walk's descendants so a searched-but-
        # unbrowsed deep sub-tree warms the file-name index.
        self._search_index = search_index

    # ---- ワーカーブリッジ → 公開シグナルの転送 slot（受け手 = self） ----

    def _forward_results_ready(self, *args) -> None:
        self.results_ready.emit(*args)

    def _forward_progress(self, *args) -> None:
        self.progress.emit(*args)

    def _forward_walk_incomplete(self, *args) -> None:
        self.walk_incomplete.emit(*args)

    def _forward_walk_truncated(self, *args) -> None:
        self.walk_truncated.emit(*args)

    def request(
        self,
        root: Path,
        includes: list[str] | None = None,
        excludes: list[str] | None = None,
        or_terms: list[str] | None = None,
    ) -> int:
        session = self._sessions.start()
        task = _RecursiveSearchTask(
            session.generation, root, self._signals, session,
            includes=includes, excludes=excludes, or_terms=or_terms,
            search_index=self._search_index,
        )
        self._pool.start(task)
        return session.generation

    def cancel(self) -> None:
        self._sessions.cancel()

    def latest_generation(self) -> int:
        return self._sessions.latest_generation()


# --------------------------------------------------------------------------- #
# Shared existence-check / posted_at helpers (non-live-walked search sources)
# --------------------------------------------------------------------------- #

def _existing_paths(
    paths, cancel: _CancelToken,
    folder_mtimes: dict[str, float] | None = None,
) -> dict[str, tuple[float, int]]:
    """Return ``{path: (mtime, size)}`` for the subset of *paths* on disk.

    ``tags.db`` is produced by the *separate* tagger process and, unlike the
    recursive search's stale-while-revalidate seeding, the tag query has **no**
    live walk to correct it — so an image deleted or moved after it was tagged
    would otherwise surface as a broken tile.  This filter removes those stale
    rows.

    It does **one ``os.scandir`` per distinct parent folder** rather than one
    ``os.stat`` per image: on a network share a single directory listing is far
    cheaper than N per-file stats when many images share a folder (the common
    gallery-per-post layout), so the number of NAS round-trips collapses from
    *image count* to *folder count* while keeping per-image accuracy (coverage
    AND stays correct).  Only the *queried* members are ``DirEntry.stat``-ed —
    unrelated entries sharing the folder are skipped, so a folder full of
    non-image files doesn't pay per-entry stats (on POSIX/SMB each
    ``DirEntry.stat`` can be a round-trip) — and that stat data is
    surfaced as the ``(mtime, size)`` value so the phase-2 re-emit can replace
    the placeholder zeros on file entries.  Mere *presence* in the listing is
    not enough to prove existence-as-a-file, but every queried member was a
    tagged image file, so treating a stat failure as ``(0.0, 0)`` (kept, with
    placeholder stats) matches the previous behaviour.  Name matching is
    exact, with one fallback: when a member is missing from the listing but a
    sibling differs from it by **case only**, the recorded path is ``os.stat``
    -ed once — on a case-insensitive volume (NTFS) a case-only rename leaves it
    readable and pruning it would drop a live hit, while a case-sensitive
    volume answers ``ENOENT`` and the stale row stays pruned.  A folder
    whose ``os.scandir`` *itself* fails is only pruned wholesale when the failure
    proves it is gone (``FileNotFoundError`` / ``NotADirectoryError`` = ENOENT);
    any other ``OSError`` (NAS disconnect, share reconnect, transient access
    denial) leaves the folder *unknown* — its queried members are kept with
    placeholder stats rather than dropped, so a momentary I/O blip can't wipe
    live seed-pass hits (same rule as the tagdb prune's "un-enumerable =
    unknown, not empty").  Runs on the worker thread (never the GUI thread) and
    polls the ``_CancelToken`` once per folder so a superseded query
    short-circuits.

    When *folder_mtimes* is supplied it is filled in place with each parent
    folder's own ``st_mtime`` (one extra ``os.stat`` per folder, only on the
    folders already being scandir-ed here).  Collapsed folder tiles seed their
    ``FolderEntry.mtime`` from it so the thumbnail loader's ``resolve_in_folder``
    path can hit the FolderPreviewCache on revisit instead of skipping it with a
    ``folder_mtime=0`` live resolve.
    """
    by_parent: dict[str, list[str]] = {}
    for p in paths:
        by_parent.setdefault(str(Path(p).parent), []).append(p)
    existing: dict[str, tuple[float, int]] = {}
    for parent, members in by_parent.items():
        if cancel.is_cancelled():
            return existing
        wanted = {Path(m).name for m in members}
        wanted_cf = {n.casefold() for n in wanted}
        stats: dict[str, tuple[float, int]] = {}
        case_variants: set[str] = set()
        try:
            # ``with``: an iteration that raises midway (the transient-failure
            # branch below is exactly that case) must not leave the directory
            # handle open until GC — on Windows an open handle also blocks
            # renaming / deleting the folder.
            with os.scandir(parent) as it:
                for e in it:
                    if e.name not in wanted:
                        if e.name.casefold() in wanted_cf:
                            # Differs from a queried member by case only —
                            # resolved (with an identity check) in the miss
                            # path below.
                            case_variants.add(e.name.casefold())
                        continue  # unrelated sibling — don't pay its stat
                    try:
                        st = e.stat()
                        stats[e.name] = (st.st_mtime, st.st_size)
                    except OSError:
                        stats[e.name] = (0.0, 0)
        except (FileNotFoundError, NotADirectoryError):
            continue  # folder really gone (ENOENT) → none of its members exist
        except OSError:
            # A *transient* enumeration failure (NAS disconnect / share
            # reconnect / temporary access denial — ERROR_NETNAME_DELETED,
            # ENETDOWN, EACCES), NOT proof the folder vanished.  Pruning here
            # would drop live hits the user is already looking at (phase-1
            # tiles), so treat the folder as *unknown* and keep every queried
            # member — placeholder ``(0.0, 0)`` stats for those the failing
            # scandir never reached, real stats preserved for any it did.  This
            # mirrors the per-file stat-failure branch above (a stat failure is
            # kept, not pruned) and the tagdb prune rule "an un-enumerable
            # folder is unknown, not empty": only ENOENT /
            # NotADirectory above are treated as "gone".
            for name in wanted:
                stats.setdefault(name, (0.0, 0))
        if folder_mtimes is not None:
            try:
                folder_mtimes[parent] = os.stat(parent).st_mtime
            except OSError:
                pass
        for m in members:
            name = Path(m).name
            st_pair = stats.get(name)
            if st_pair is None and name.casefold() in case_variants:
                # Case-only rename on a case-INSENSITIVE volume (NTFS: tagged
                # as ``IMG001.jpg``, renamed to ``img001.jpg``): the recorded
                # path still opens, so pruning it would drop a live hit.
                # Ask the filesystem itself rather than
                # assuming — on a case-sensitive volume those are two
                # different files and the stat fails, keeping the stale row
                # pruned.  Only paid when a case variant is actually sitting
                # in the listing, so ordinary misses (deleted files) still
                # cost zero extra round-trips.
                try:
                    st = os.stat(m)
                    st_pair = (st.st_mtime, st.st_size)
                except OSError:
                    st_pair = None
            if st_pair is not None:
                existing[m] = st_pair
    return existing


def _posted_at_map(
    results: list[tuple[FolderEntry, str]],
    folder_cache: FolderPreviewCache | None,
    cancel: _CancelToken,
    known: dict[str, datetime | None] | None = None,
) -> dict[str, datetime | None]:
    """Resolve ``posted_at`` for each result's owning folder.

    Folder entries read their own ``post.md``; file entries inherit their
    parent folder's.  Cache-warm folders (a :class:`FolderPreviewCache` row
    valid for the folder's current mtime) resolve from the local sqlite via
    ``get_many`` — one ``os.stat`` per folder is the only filesystem touch.
    Cache-cold folders fall back to one **bounded head** ``post.md`` read each
    via :func:`post_md.read_post_meta` (the meta block is always at the top, so
    the body is never transferred).  Only called when the
    active query has posted-date bounds (see ``want_posted_at``), so queries
    without a date filter pay zero extra I/O.  Polls the ``_CancelToken``
    once per folder; a cancelled call returns the partial map (the caller
    bails out right after).

    *known* is a set of folder keys whose ``posted_at`` was already resolved
    (e.g. by the seed pass); those folders are skipped entirely — no ``os.stat``, no
    cache hit, no ``post.md`` re-read.  This is what stops the vector worker's
    top-k expansion from re-reading a cold folder's ``post.md`` that the seed pass
    already read.
    """
    folders: list[Path] = []
    seen: set[str] = set()
    for entry, _rel in results:
        folder = entry.path if entry.is_dir else entry.path.parent
        key = str(folder)
        if known is not None and key in known:
            continue  # already resolved (seed pass) — don't stat / re-read
        if key not in seen:
            seen.add(key)
            folders.append(folder)
    out: dict[str, datetime | None] = {}
    mtimes: dict[str, float] = {}
    for folder in folders:
        if cancel.is_cancelled():
            return out
        try:
            mtimes[str(folder)] = os.stat(folder).st_mtime
        except OSError:
            continue  # folder gone → posted_at stays unknown
    if folder_cache is not None and mtimes:
        try:
            hits = folder_cache.get_many(
                [(Path(k), m) for k, m in mtimes.items()]
            )
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview cache get_many failed: {}", exc)
            hits = {}
        for key, preview in hits.items():
            out[key] = preview.posted_at
    for folder in folders:
        key = str(folder)
        if key in out or key not in mtimes:
            continue
        if cancel.is_cancelled():
            return out
        # Only ``posted_at`` is wanted and the meta block is always at the top
        # of post.md (docs/formats/post-md.md §2), so read a bounded head
        # instead of the whole file — the same reader folder_scan already uses.
        # A multi-MB body would otherwise cross the NAS in full for
        # a few hundred bytes of meta.  ``None`` covers both "unreadable" and
        # "no meta block" → date unknown.
        parsed = read_post_meta(folder / "post.md")
        out[key] = parsed.posted_at if parsed is not None else None
    return out


def _apply_posted_at(
    results: list[tuple[FolderEntry, str]],
    posted: dict[str, datetime | None],
) -> list[tuple[FolderEntry, str]]:
    """Return *results* with ``posted_at`` filled in from the *posted* map."""
    out: list[tuple[FolderEntry, str]] = []
    for entry, rel in results:
        key = str(entry.path if entry.is_dir else entry.path.parent)
        stamp = posted.get(key)
        if stamp is not None and entry.posted_at is None:
            entry = replace(entry, posted_at=stamp)
        out.append((entry, rel))
    return out


def _maybe_seed_posted_at(
    results: list[tuple[FolderEntry, str]],
    posted: dict[str, datetime | None],
    *,
    want: bool,
    folder_cache: FolderPreviewCache | None,
    cancel: _CancelToken,
) -> list[tuple[FolderEntry, str]]:
    """Seed ``posted_at`` on *results* when the query has date bounds.

    Fills *posted* in place (so the phase-2 re-emit can re-apply the same map
    with no additional I/O) and returns the seeded result list.  A no-op when
    *want* is false — search entries then keep ``posted_at=None``, which is
    harmless because no date filter is active.
    """
    if not want:
        return results
    # Pass the already-resolved keys as *known* so a second seeding pass (the
    # vector worker's top-k expansion) skips folders the seed pass already read.
    posted.update(_posted_at_map(results, folder_cache, cancel, known=posted))
    return _apply_posted_at(results, posted)


def emit_with_existence_check(
    *,
    phase1: list[tuple[FolderEntry, str]],
    live_paths: (
        list[str]
        | Callable[[dict[str, tuple[float, int]]], list[str] | None]
    ),
    rebuild: Callable[
        [dict[str, tuple[float, int]], dict[str, float]],
        list[tuple[FolderEntry, str]],
    ],
    posted: dict[str, datetime | None],
    emit: Callable[[list[tuple[FolderEntry, str]]], None],
    cancel: _CancelToken,
    want_posted_at: bool = False,
    folder_cache: FolderPreviewCache | None = None,
) -> None:
    """Run the shared two-phase emit skeleton for a non-live-walked source.

    ``tags.db`` (tag + vector queries) is not revalidated by a live walk, so a
    row whose image was deleted / moved after tagging would otherwise stay a
    broken tile.  Rather than make the user wait for the existence check, the
    caller has already emitted the seed result (every match, zero filesystem I/O unless
    a posted-date filter is active).  This helper then:

    1. collects candidate paths from *live_paths* and runs
       :func:`_existing_paths` over them (one scandir per parent folder,
       capturing folder mtimes for the loader cache).  *live_paths* is either
       a plain list (the single-round sources: tag-coverage / tag-strict) or —
       for sources whose candidate set must **grow** until enough live hits
       are collected (the vector path's top-k widening) — a callable
       receiving the live-so-far ``{path: (mtime, size)}`` map and returning
       the next batch of candidate paths, or ``None`` to stop.  Batches may
       overlap: already-checked paths are skipped, so a widening query can
       simply return its full new candidate list each round.
    2. asks *rebuild* to build the final list from the live set — it receives
       the ``{path: (mtime, size)}`` existence map and the ``{folder: mtime}``
       map so it can fill real stats,
    3. re-seeds ``posted_at`` itself via :func:`_maybe_seed_posted_at` (pass
       ``want_posted_at`` / *folder_cache*): rows that *appeared* after
       the seed pass (a widened candidate set) get their dates resolved,
       while folders the seed pass already resolved are skipped through the shared
       *posted* map — a source that only shrinks pays no extra I/O,
    4. re-emits the rebuilt set **only if it differs** from the seed result — when
       nothing was pruned and no stats were gained the seed result was
       already final and the redundant re-emit / re-paint is skipped.

    Cancellation is polled after each existence-check round and before the
    emit; a superseded query returns without emitting.  This is the single
    implementation of the "emit → existence check → re-collapse → posted
    apply → early-return-if-equal" skeleton that the tag-coverage, tag-strict,
    and vector paths share — new search sources must pass their collapse (and,
    if their candidate set can grow, their widening loop as a *live_paths*
    callable) into this helper rather than re-implementing the skeleton.
    """
    folder_mtimes: dict[str, float] = {}
    live: dict[str, tuple[float, int]] = {}
    if callable(live_paths):
        supply = live_paths
    else:
        batches = iter([list(live_paths)])
        supply = lambda _live: next(batches, None)  # noqa: E731
    checked: set[str] = set()
    while True:
        batch = supply(live)
        if batch is None:
            break
        new = [p for p in batch if p not in checked]
        checked.update(new)
        live.update(_existing_paths(new, cancel, folder_mtimes))
        if cancel.is_cancelled():
            return
    final = rebuild(live, folder_mtimes)
    final = _maybe_seed_posted_at(
        final, posted, want=want_posted_at,
        folder_cache=folder_cache, cancel=cancel,
    )
    if cancel.is_cancelled():
        return
    if final == phase1:
        return  # nothing pruned / no stats gained — phase-1 already final
    emit(final)


__all__ = [
    "RecursiveSearchScanner",
    "_RecursiveSearchSignals",
    "_RecursiveSearchTask",
    "_apply_posted_at",
    "_existing_paths",
    "_maybe_seed_posted_at",
    "_node_row_to_entry",
    "_posted_at_map",
    "emit_with_existence_check",
]
