"""One sqlite file for the viewer's three path-keyed disk caches.

``data/viewer_cache.db`` holds the ``aspect`` / ``thumb`` / ``preview``
tables — the aspect-ratio cache (:mod:`.thumb_meta_cache`), the decoded
thumbnail *index* (:mod:`.thumb_disk_cache`, whose WebP bytes live beside it
under ``data/thumb_cache/``) and the folder-preview resolution cache
(:mod:`.folder_preview_cache`).

**Why one file.**  All three answer the same shape of question — "what do you
know about this exact path?" — validated by the same ``(mtime, size)`` /
``mtime`` keys, written by the same worker pools and torn down at the same
moment.  Three separate files meant three connections, three WAL sets, three
corruption-recovery seams and three entries in the close budget, all to keep
data that is conceptually one accelerator.  This store opens the file once;
each cache *attaches* to that connection (``share=`` in
:class:`~._sqlite_cache.SqliteStoreBase`) and keeps only what is genuinely its
own: its table, its LRU touch buffer and its byte budget.

**The cost, stated plainly: one ``RLock`` instead of three.**  A GUI-thread
``FolderPreviewCache.get_many`` seed can now wait behind a loader worker's
``ThumbDiskCache.lookup``.  Most critical sections are a single indexed
statement (or one ``QUERY_CHUNK`` batch) on a local sqlite file — tens to a
few hundred µs — but ``ThumbDiskCache.lookup`` is *not* one of them.  On a row
hit it holds the lock across one ``Path.exists()`` on the blob, and on a stale
row across a ``DELETE`` + ``unlink`` + ``commit`` instead; even on a local
disk the stat alone outweighs the SELECT.  Put the portable base on a
high-latency volume (NAS / USB) and the GUI seed waits that out.  Those FS
calls inside the lock predate the merge; what the merge changes is *who*
waits on them — and for the aspect seed, whose only cross-thread company used
to be the once-per-build prune sweep, contending with a per-tile loader
lookup is new.  The long sweeps (``prune``) already take the lock once per
pass and release it in between precisely so a GUI read can interleave.
What the merge removes in exchange is three-way write amplification: three
connections to the same directory each fsyncing their own WAL on every
commit, against one.

The search index (``viewer_search_index.db``) deliberately stays separate: it
is a prefix-*enumerable* index with a different stale policy
(stale-while-revalidate, see ``docs/claude/viewer/scanning.md``), not a point
lookup, and it is the one store whose rows are read by a query planner rather
than by primary key.

**Per-table budgets survive the merge.**  ``aspect_cache_max_mib`` /
``thumb_disk_cache_max_mib`` / ``folder_preview_cache_max_mib`` remain three
independent budgets, each swept by its own LRU over its own table, so the
invariant that tiny high-value aspect rows are never evicted under thumbnail
pressure is unchanged.  A single shared budget would have to compare a
~64-byte aspect row against a ~40 KB WebP master, and the cheap rows would
always lose.  What the merge does change is the *file*: a sqlite page freed by
one table can be reused by another instead of sitting in a file only its own
table can grow into.

**Upgrade path: the old files are discarded, not migrated.**  Everything here
is regenerable from the filesystem, so
:func:`discard_legacy_cache_dbs` deletes the three pre-merge databases (and
their WAL / journal sidecars) on the first run that finds them, and the
caller sweeps the now-orphaned WebP tree.  Importing the old rows would mean
carrying three schema histories — including the folder-preview table's
``ALTER TABLE`` chain — into a database that has no history, for a warm-cache
head start the next browse re-earns anyway.  Nothing non-regenerable is
involved: stars, tags and other user data live in ``user_meta.db``, which
this migration never touches.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from ..common.paths import LEGACY_VIEWER_CACHE_DB_NAMES
from ._sqlite_cache import CORRUPT_SUFFIX, SqliteStoreBase
from .folder_preview_cache import FolderPreviewCache, create_preview_schema
from .thumb_disk_cache import ThumbDiskCache, create_thumb_schema
from .thumb_meta_cache import ThumbMetaCache, create_aspect_schema

#: Sidecars sqlite may leave next to a database file, plus the quarantine
#: copies :func:`._sqlite_cache.open_with_recovery` renames to.
_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")


def discard_legacy_cache_dbs(data: Path) -> list[Path]:
    """Delete the pre-merge cache databases under *data*.  Returns what went.

    The three files named by
    :data:`~snappix.common.paths.LEGACY_VIEWER_CACHE_DB_NAMES` are never
    *read* — their rows are regenerable and their schemas are history.  Each
    is removed together with its ``-wal`` / ``-shm`` / ``-journal`` sidecars
    (sqlite treats a database and its journal as one unit; an orphaned hot
    journal next to a fresh file is a hazard) and with any ``.corrupt``
    quarantine copy an earlier session left behind.

    Best-effort: a file another process still holds open simply stays, and the
    next startup tries again.  The caller decides what to do about the WebP
    blob tree, which the removed thumbnail index leaves fully orphaned.
    """
    removed: list[Path] = []
    for name in LEGACY_VIEWER_CACHE_DB_NAMES:
        db_path = data / name
        gone = False
        for suffix in _SIDECAR_SUFFIXES:
            for path in (
                db_path.with_name(db_path.name + suffix),
                db_path.with_name(db_path.name + suffix + CORRUPT_SUFFIX),
            ):
                try:
                    if path.exists():
                        os.remove(path)
                        gone = gone or suffix == ""
                except OSError as exc:
                    logger.warning("旧キャッシュ {} を削除できません: {}", path, exc)
        if gone:
            removed.append(db_path)
    if removed:
        logger.info(
            "統合前のキャッシュ DB {} 件を破棄しました（再生成されます）",
            len(removed),
        )
    return removed


class ViewerCacheStore(SqliteStoreBase):
    """Owner of ``viewer_cache.db`` and of the three caches attached to it.

    Construction materialises every table (so a cache the settings disabled
    never reads as a missing table to the corruption probe) and then hands
    out the attached caches as :attr:`aspect` / :attr:`thumbs` /
    :attr:`previews`.  A disabled cache is ``None``; its table still exists
    and stays empty.

    The public surface of each attached cache is unchanged — callers keep
    ``ThumbMetaCache`` / ``ThumbDiskCache`` / ``FolderPreviewCache`` objects
    and call the same methods.  What moved here is everything that used to be
    per-file: the connection, the WAL set, the corruption-recovery seam
    (:func:`._sqlite_cache.open_with_recovery` takes this store's
    :attr:`SCHEMA_TABLES` and :meth:`prune`) and the single ``close`` in the
    window's shutdown budget.
    """

    #: All three tables of the merged file.  ``open_with_recovery`` uses this
    #: to classify ``no such table: <name>`` as "foreign sqlite file" —
    #: including tables whose cache the settings disabled, which nothing else
    #: would ever touch.
    SCHEMA_TABLES = ("aspect", "thumb", "preview")

    #: File schema version.  When a table gains a column: add it to the
    #: table's ``create_*_schema`` (fresh files get it at CREATE time), bump
    #: this, and add an idempotent ``if ver < N:`` upgrade step in
    #: :meth:`_migrate` for files an older build wrote.  The row-schema
    #: recipe in :mod:`.folder_preview_cache` covers making old rows
    #: re-resolve.
    _SCHEMA_VER = 1

    def __init__(
        self,
        db_path: Path,
        *,
        blob_dir: Path,
        thumb_max_bytes: int,
        preview_max_bytes: int,
        thumbs_enabled: bool = True,
        previews_enabled: bool = True,
    ) -> None:
        # Attribute-first so a failure below can close through ``self``.
        self.aspect: ThumbMetaCache | None = None
        self.thumbs: ThumbDiskCache | None = None
        self.previews: FolderPreviewCache | None = None
        # Cross-thread: the scanner, loader and cache-builder pools all read
        # and write through this one connection, serialised by the one lock.
        super().__init__(db_path, check_same_thread=False)
        try:
            self.aspect = ThumbMetaCache(db_path, share=self)
            if thumbs_enabled:
                self.thumbs = ThumbDiskCache(
                    db_path, blob_dir, max_bytes=thumb_max_bytes, share=self,
                )
            if previews_enabled:
                self.previews = FolderPreviewCache(
                    db_path, max_bytes=preview_max_bytes, share=self,
                )
        except BaseException:
            # Same rule as the base class's migration guard: never leave a
            # handle behind, or the quarantine rename in ``open_with_recovery``
            # fails on Windows and the handle leaks after the ``None``
            # degradation.
            SqliteStoreBase.close(self)
            raise

    # ------------------------------------------------------------- schema

    def _migrate(self) -> None:
        with self._lock:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                # Fresh file.  The ``create_*_schema`` helpers build the
                # *current* column set, so stamp the current version and
                # return WITHOUT falling through to the upgrade steps below:
                # an ``ALTER TABLE ADD COLUMN`` step would re-add a column
                # that was just created, and ``duplicate column name`` is an
                # ``OperationalError`` that ``_is_corrupt_db_error``
                # deliberately classifies as transient — so it would never be
                # quarantined and ``_open_cache`` would degrade all three
                # caches to ``None`` on every single launch.
                create_aspect_schema(self._conn)
                create_thumb_schema(self._conn)
                create_preview_schema(self._conn)
                self._conn.execute(f"PRAGMA user_version={self._SCHEMA_VER}")
                self._conn.commit()
                return
            # Upgrade steps for files written by an older build go here, each
            # gated on ``if ver < N`` and stamping ``PRAGMA user_version=N``.
            # ``ALTER TABLE ADD COLUMN`` auto-commits, so a crash between the
            # ALTER and its version bump re-runs the step on the next open:
            # every step must be idempotent (check ``PRAGMA table_info``
            # before adding the column), for the same reason as above.

    # -------------------------------------------------------------- views

    def caches(self) -> tuple:
        """The attached caches that actually exist, in close order."""
        return tuple(
            c for c in (self.aspect, self.thumbs, self.previews)
            if c is not None
        )

    # ------------------------------------------------------ prune / close

    def prune(self, *, aspect_max_bytes: int) -> None:
        """Bring every table back under its own budget.

        Doubles as the startup corruption probe: ``_open_cache`` hands this
        to :func:`._sqlite_cache.open_with_recovery` as ``verify``, so a
        page-2+ malformed file (or a foreign sqlite file whose
        ``user_version`` claims it is migrated) surfaces here as a
        ``sqlite3.DatabaseError`` and the quarantine + recreate path runs.
        The explicit table probe covers tables whose cache is disabled — their
        prune would never run and their corruption would otherwise wait until
        the user re-enables the cache.

        ``aspect_max_bytes`` is passed in rather than stored because the
        aspect budget lives in ``ViewerState`` and the caller (GUI thread)
        snapshots it; the other two caches carry their own.
        """
        with self._lock:
            self._probe_schema_tables_locked()
        if self.aspect is not None:
            self.aspect.prune(aspect_max_bytes)
        if self.thumbs is not None:
            self.thumbs.prune()
        if self.previews is not None:
            self.previews.prune()

    def flush(self) -> None:
        """Flush every attached cache's batched touches / deferred commits."""
        for cache in self.caches():
            cache.flush()

    def close(self) -> None:
        """Flush the attached caches, then close the one connection.

        The attached caches' own ``close`` only flushes (the handle is not
        theirs), so this is the single close the window's shutdown budget
        schedules — 詳細は ``docs/claude/viewer/scanning.md`` の終了処理節。
        """
        for cache in self.caches():
            cache.close()
        super().close()


__all__ = ["ViewerCacheStore", "discard_legacy_cache_dbs"]
