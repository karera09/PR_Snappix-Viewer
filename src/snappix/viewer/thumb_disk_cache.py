"""On-disk cache of decoded thumbnails for instant folder revisits.

This is what makes the gallery feel like Eagle: a thumbnail decoded once is
stored as a compact WebP on disk (under the portable ``data/`` tree), so
revisiting a folder — or restarting the app — paints from local disk
instead of re-reading (and re-decoding) originals over a NAS share.

Design:

* **One master thumbnail per image**, longest edge ``cache_edge`` (default
  1024, configurable).  The cache is *downscale-only*: the view scales the
  master down to whatever the layout needs.  Tiles that need MORE pixels
  than the master (huge slider + hi-DPI) bypass the cache and decode the
  original at the exact size — so a cached thumb is never upscaled, hence
  never blurry.  A master decoded from an original smaller than
  ``cache_edge`` records ``full=1`` (it is the best resolution that will
  ever exist) and is served regardless of requested size.
* Validated by ``(mtime, size)`` like the aspect cache — **the filesystem
  is the source of truth**; a changed original is re-decoded, a deleted
  original's row is inert and pruned lazily.  ``lookup``'s key comes from a
  fresh ``stat`` the caller just performed, so a mismatched row is proven
  outdated and deleted eagerly (see the stale-row policy in
  :mod:`._sqlite_cache`).
* **Independent byte budget** from the aspect cache: thumbnail-image
  eviction (LRU by ``last_used``) only ever deletes WebP files + ``thumb``
  rows — it never touches the aspect table, even though both tables now live
  in one file (:mod:`.viewer_cache`).  Only the *index* is in that database;
  the bytes this budget accounts for are the WebP blobs beside it.

Thread-safe: the :class:`ThumbnailLoader`'s worker pool calls
``lookup`` / ``store`` concurrently, so every sqlite access is guarded by a
lock (WAL mode allows the concurrent reads; the lock serialises writes).
Blob files are written to a temp name then ``os.replace``-d so a concurrent
reader never sees a partial file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from ._sqlite_cache import SqliteCacheBase, SqliteStoreBase, mtime_matches

# How often the ``clear()`` blob sweep re-reads the set of still-referenced
# blob files (rows written by workers that kept running through the sweep).
_CLEAR_LIVE_REFRESH = 128
# Rows per batched "is this blob referenced again?" lookup in the prune sweep
# (host-parameter cap per statement is ~999).
_UNLINK_CHUNK = 200
# Grace window (s) before a ``*.webp.tmp`` written by ANOTHER process is
# treated as a crash leftover.  A second viewer on the same portable base is
# a supported configuration, and its in-flight ``store()`` temp carries a
# foreign pid — unlinking it breaks that process's ``os.replace`` (Windows
# ``PermissionError`` / a truncated blob on POSIX).  A temp older than this
# cannot belong to a live write: the encode + write of one 1024 px WebP is
# milliseconds, so anything still around a minute later is debris, and a temp
# that slips into the window is reclaimed by the next sweep anyway.
_TMP_GRACE_S = 60.0


def sweep_orphan_blobs(
    blob_dir: Path, *, keep: Callable[[str, int], bool] | None = None,
) -> None:
    """Unlink every ``*.webp`` under *blob_dir* plus stale ``*.webp.tmp``.

    The single implementation of the blob-tree sweep, shared by
    :meth:`ThumbDiskCache.clear` (which passes *keep* so a concurrent
    ``store()`` can re-reference a blob mid-sweep) and by the recovery
    callback that runs after a corrupt index DB is quarantined (where every
    blob is an orphan by construction, so *keep* is ``None``).

    *keep* receives ``(relative blob path, how many blobs were examined)`` and
    returns True to spare that file.  Temps are never handed to it: they are
    skipped when they belong to this process or are younger than
    :data:`_TMP_GRACE_S` (another viewer may be writing them), and unlinked
    otherwise.

    Both production callers run this on a daemon thread (the tree can hold
    tens of thousands of files); every filesystem error is swallowed so a
    sweep can never take the caller down.
    """
    own_tmp_tag = f".{os.getpid()}-"
    # One clock reading for the whole walk — the tree can hold tens of
    # thousands of files, and a temp that enters the grace window while we
    # walk is simply reclaimed next time.
    now = time.time()
    checked = 0
    for pattern in ("*.webp", "*.webp.tmp"):
        for fp in blob_dir.rglob(pattern):
            if fp.name.endswith(".webp.tmp"):
                if own_tmp_tag in fp.name:
                    continue  # in-flight temp of a concurrent store()
                try:
                    if now - fp.stat().st_mtime < _TMP_GRACE_S:
                        continue  # possibly another viewer's live write
                except OSError:  # pragma: no cover (vanished mid-walk)
                    continue
            else:
                checked += 1
                try:
                    rel = str(fp.relative_to(blob_dir))
                except ValueError:  # pragma: no cover (defensive)
                    rel = ""
                if keep is not None and keep(rel, checked):
                    continue
            try:
                fp.unlink()
            except OSError:  # pragma: no cover (defensive)
                pass


def create_thumb_schema(conn) -> None:
    """Create the ``thumb`` index table + its LRU index (idempotent).

    Module-level for the same reason as
    :func:`~snappix.viewer.thumb_meta_cache.create_aspect_schema`: the shared
    :class:`~snappix.viewer.viewer_cache.ViewerCacheStore` materialises every
    table of ``viewer_cache.db`` regardless of which caches the settings
    enable, so a disabled cache never looks like a missing table.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thumb (
            path      TEXT PRIMARY KEY,
            mtime     REAL NOT NULL,
            size      INTEGER NOT NULL,
            edge      INTEGER NOT NULL,
            full      INTEGER NOT NULL,
            file      TEXT NOT NULL,
            bytes     INTEGER NOT NULL,
            last_used REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thumb_last_used ON thumb(last_used)"
    )


class ThumbDiskCache(SqliteCacheBase):
    """Persistent WebP thumbnail store keyed by path + (mtime, size)."""

    # 所有テーブル（open_with_recovery の "no such table" 破損分類に使う）。
    SCHEMA_TABLES = ("thumb",)
    _TOUCH_SQL = "UPDATE thumb SET last_used=? WHERE path=?"

    def __init__(
        self,
        db_path: Path,
        blob_dir: Path,
        *,
        max_bytes: int,
        share: SqliteStoreBase | None = None,
    ) -> None:
        self._blob_dir = blob_dir
        self._max_bytes = max_bytes
        blob_dir.mkdir(parents=True, exist_ok=True)
        # Cross-thread: the loader pools call lookup/store concurrently.
        # ``share`` attaches the index to an already-open store's connection
        # (the viewer's :mod:`.viewer_cache` store); ``None`` opens
        # ``db_path`` standalone.
        super().__init__(db_path, check_same_thread=False, share=share)

    def _migrate(self) -> None:
        # See ThumbMetaCache._migrate for why the version gate stays.
        with self._lock:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                create_thumb_schema(self._conn)
                self._conn.execute("PRAGMA user_version=1")
                self._conn.commit()

    # ------------------------------------------------------------ key/paths

    @staticmethod
    def _key(path: Path, mtime: float, size: int, edge: int) -> str:
        """Blob name for one master.

        ``edge`` is part of the key because the row and the blob must describe
        the same pixels.  Two writers can hold different ``cache_edge``
        settings at the same time (a background cache build keeps the edge it
        started with, while the live loaders take a new one the moment the
        settings dialog applies it), and both may decode the same original.
        Without the edge in the name they race over one blob file while their
        rows race over one primary key, so a row can end up saying ``edge=1024``
        next to a 512 px blob — and ``lookup`` then serves those 512 px for an
        800 px request, upscaled and blurry, until the original's mtime/size
        changes.  With the edge in the name each master owns its own file, so
        whichever row wins, it points at bytes of its own size.

        Existing rows are unaffected: ``lookup`` resolves the blob through the
        row's stored ``file`` column, never by recomputing this key.
        """
        raw = f"{path}|{mtime!r}|{size}|{edge}".encode("utf-8", "surrogatepass")
        return hashlib.sha1(raw).hexdigest()

    def _blob_file(self, key: str) -> Path:
        # Shard by the first two hex chars so no single directory holds tens
        # of thousands of files.
        return self._blob_dir / key[:2] / f"{key}.webp"

    # --------------------------------------------------------------- lookup

    def lookup(
        self, path: Path, mtime: float, size: int, required_edge: int,
    ) -> Path | None:
        """Return the master blob's path iff it satisfies the request.

        Served when the stored master's longest edge is at least
        ``required_edge`` OR the master is the full original resolution
        (``full=1`` — no larger version will ever exist).  A stale row
        (mtime/size mismatch) or a missing blob file is deleted and treated
        as a miss.
        """
        key_path = str(path)
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime, size, edge, full, file FROM thumb WHERE path=?",
                (key_path,),
            ).fetchone()
            if row is None:
                return None
            c_mtime, c_size, edge, full, file = row
            if c_size != size or not mtime_matches(c_mtime, mtime):
                self._delete_locked(key_path, file)
                return None
            fp = self._blob_dir / file
            if not fp.exists():
                self._delete_locked(key_path, file)
                return None
            if not (full or edge >= required_edge):
                # A larger master is possible and the caller needs it; treat
                # as a miss so the worker regenerates at the bigger edge.
                return None
            self._touch_locked(key_path)
            return fp

    # ---------------------------------------------------------------- store

    def store(
        self,
        path: Path,
        mtime: float,
        size: int,
        data: bytes,
        *,
        edge: int,
        full: bool,
    ) -> Path:
        """Write ``data`` (WebP bytes) as the master for *path*; upsert row."""
        key_path = str(path)
        key = self._key(path, mtime, size, edge)
        fp = self._blob_file(key)
        # The blob write is done WITHOUT the lock: the final filename is a
        # content hash of (path, mtime, size, edge), so no two workers write
        # DIFFERENT bytes to the same blob.  Only the sqlite row update is
        # serialised.
        # Writing the blob *before* the row means a committed row is never
        # visible without its backing file — a crash leaves at most an orphan
        # blob (pruned lazily / overwritten next build), never a dangling row.
        # Keeping multi-KB WebP writes out of the lock is what lets the
        # higher-concurrency cache builder actually parallelise.
        #
        # The TEMP name, however, must be unique per writer: the live loader
        # pool and a background CacheBuilder's own loader pool share this one
        # ThumbDiskCache, so browsing a folder mid-build can have TWO pools
        # decode the same image concurrently — a shared ``key.webp.tmp`` would
        # then be written by both and os.replace would race (PermissionError on
        # Windows / a torn blob on POSIX).  Tag the temp with pid + thread id so
        # every writer owns a distinct temp; os.replace stays atomic.
        tmp = fp.with_name(
            f"{key}.{os.getpid()}-{threading.get_ident()}.webp.tmp"
        )
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, fp)
        except OSError as exc:  # pragma: no cover (disk full etc.)
            logger.warning("thumb cache write failed for {}: {}", path, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return fp
        rel = str(fp.relative_to(self._blob_dir))
        with self._lock:
            prev = self._conn.execute(
                "SELECT file FROM thumb WHERE path=?", (key_path,),
            ).fetchone()
            self._conn.execute(
                "INSERT OR REPLACE INTO thumb"
                "(path, mtime, size, edge, full, file, bytes, last_used)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    key_path, float(mtime), int(size), int(edge),
                    1 if full else 0, rel, len(data), time.time(),
                ),
            )
            self._maybe_commit_locked()
            if prev is not None and prev[0] != rel:
                # The replaced row's blob is now unreferenced.  Its bytes left
                # the ``SUM(bytes)`` budget with the row, so leaving the file
                # behind would grow the blob tree past the configured cap with
                # no in-app way to reclaim it short of "clear cache".
                try:
                    (self._blob_dir / prev[0]).unlink(missing_ok=True)
                except OSError:  # pragma: no cover (a reader may hold it open)
                    pass
        return fp

    # -------------------------------------------------------------- pruning

    def total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM thumb"
            ).fetchone()
            return int(row[0])

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM thumb").fetchone()[0]
            )

    def clear(self) -> int:
        """Delete every cached thumbnail (rows + WebP blobs).  Returns count.

        Only ever touches the ``thumb`` table / blob tree — the aspect and
        folder-preview tables sharing the file are untouched.

        The row deletion (a single ``DELETE`` + ``commit``) is done under the
        lock and returns quickly; the potentially tens-of-thousands of blob
        ``unlink`` calls are handed to a **background daemon thread** so the
        GUI thread (which calls this from the settings "clear cache" button)
        never blocks on filesystem churn, and worker ``lookup`` / ``store``
        calls aren't held off behind a long lock.

        Because every row is deleted first, *nothing* under the blob tree is
        still referenced, so the sweep walks the whole ``blob_dir`` and unlinks
        every ``*.webp`` **and** every leftover ``*.webp.tmp``.  This reclaims
        orphans no row ever pointed at — blobs written just before a crash (or
        left by a rolled-back bulk-write transaction) and temp files whose
        pid/tid-tagged name means no later writer reuses them.  Neither is
        counted in the ``SUM(bytes)`` budget, so without this sweep they would
        silently accumulate past ``thumb_disk_cache_max_mib`` with no in-app
        way to reclaim them.  (The returned count is the number of ``thumb``
        rows removed, not the number of blob files swept.)

        "Nothing is referenced" holds only for the instant of the ``DELETE``:
        the loader pool keeps decoding through the sweep, so a worker's
        :meth:`store` can write a fresh blob **and its row** while the sweep
        walks.  Unlinking that blob would leave a dangling row (a lookup
        miss + needless re-decode) or break an in-flight ``os.replace``, so the
        sweep skips (a) blobs a re-read of the ``thumb`` table says are
        referenced again and (b) ``*.webp.tmp`` temps owned by *this* process
        **or younger than** :data:`_TMP_GRACE_S` — a second viewer running on
        the same portable base writes temps under its own pid, so a foreign
        pid alone does not prove the writer is dead.
        """
        with self._lock:
            self._pending_touch.clear()
            count = int(
                self._conn.execute("SELECT COUNT(*) FROM thumb").fetchone()[0]
            )
            self._conn.execute("DELETE FROM thumb")
            self._conn.commit()
        self._start_guarded_sweep("thumb-cache-clear")
        return count

    def _start_guarded_sweep(self, thread_name: str) -> None:
        """Daemon sweep of ``blob_dir`` that spares blobs rows point at.

        Shared by :meth:`clear` and :meth:`sweep_orphans_async` — one guard,
        not two hand-written copies.
        """
        blob_dir = self._blob_dir

        def _referenced() -> set[str]:
            """Blob files rows point at *right now* (empty on a closed DB)."""
            try:
                with self._lock:
                    return {
                        row[0]
                        for row in self._conn.execute("SELECT file FROM thumb")
                    }
            except sqlite3.Error:  # pragma: no cover (shutdown race)
                return set()

        def _sweep_blobs() -> None:
            # Sweep the whole tree, including row-less orphan blobs and stale
            # *.webp.tmp temps that no writer reuses — minus anything a
            # concurrent store() (re-)referenced meanwhile.
            live = {"set": _referenced()}

            def _keep(rel: str, checked: int) -> bool:
                if checked % _CLEAR_LIVE_REFRESH == 0:
                    live["set"] = _referenced()
                return rel in live["set"]

            sweep_orphan_blobs(blob_dir, keep=_keep)

        threading.Thread(
            target=_sweep_blobs, name=thread_name, daemon=True,
        ).start()

    def sweep_orphans_async(self) -> None:
        """Sweep row-less blobs on a daemon thread, keeping what rows reference.

        For the index-quarantine path (:func:`_sqlite_cache.open_with_recovery`
        replaced a corrupt index with an empty one): every blob is an orphan
        at that instant, but this store is already open and the loader pool
        starts :meth:`store` ing fresh blobs while the sweep walks — the same
        race :meth:`clear` guards.  Unlinking a freshly stored blob would
        leave a dangling row (lookup miss + needless re-decode), so the sweep
        re-reads the ``thumb`` table and skips anything referenced again.
        """
        self._start_guarded_sweep("thumb-cache-orphans")

    def prune(self, max_bytes: int | None = None) -> int:
        """Evict oldest-used blobs until under budget.  Returns rows removed.

        Deletes both the ``thumb`` row and its WebP file, so this keeps its
        own loop instead of the base class's row-only prune.  Never touches
        the aspect / preview tables that share the file.

        Like :meth:`clear`, the potentially tens-of-thousands of blob
        ``unlink`` calls are handed to a **background daemon thread** — a
        budget cut (settings ``2GiB → 256MiB``) or the startup budget prune
        would otherwise stall the GUI thread (and, at startup, the whole
        window's first paint) on filesystem churn.  Crucially, the **row scan
        and DELETE stay synchronous in this call**: the startup prune
        doubles as the corruption probe for :func:`._sqlite_cache.open_with_recovery`
        (a ``sqlite3.DatabaseError`` from a page-2+ malformed DB must surface
        from *this* call so the recovery path quarantines + recreates it), and
        offloading the DB work would swallow that signal.  The probe itself is
        run explicitly (``_probe_schema_tables_locked``) so a zero budget —
        which returns before any row is read — still exercises it.  Only the
        ``unlink`` of already-dereferenced blob files is deferred — and because
        a concurrent :meth:`store` of the same ``(path, mtime, size)`` rewrites
        the very blob queued for deletion, the sweep re-checks each queued row
        by PK and keeps whatever became referenced again.

        The lock is taken **once per 64-row pass** and released in between
        (not held across the whole sweep), so concurrent loader-pool
        ``lookup`` / ``store`` calls — and thus the GUI — interleave with a
        long sweep instead of blocking until it finishes.  The running
        ``total`` is decremented instead of re-measured per pass; rows
        written (or eagerly deleted by ``lookup``) between passes merely
        shift the soft budget until the next prune.
        """
        budget = self._max_bytes if max_bytes is None else max_bytes
        removed = 0
        to_unlink: list[tuple[str, str]] = []
        with self._lock:
            # Startup corruption probe, run explicitly:
            # the base class's ``_prune_lru`` probes before its own budget
            # short-circuit, but this override keeps its own loop and would
            # otherwise return without touching a single page when the budget
            # is 0 — leaving a malformed DB to surface at the first lookup,
            # long after ``open_with_recovery``'s quarantine seam has passed.
            self._probe_schema_tables_locked()
            self._flush_touches_locked()
            if budget <= 0:
                return 0
            total = self.total_bytes()
        while total > budget:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT path, file, bytes FROM thumb "
                    "ORDER BY last_used ASC LIMIT 64"
                ).fetchall()
                if not rows:
                    break
                for key_path, file, nbytes in rows:
                    # Drop the ROW synchronously (keeps the DatabaseError
                    # surface for the startup corruption probe); collect the
                    # blob file for the async unlink sweep below.
                    self._conn.execute(
                        "DELETE FROM thumb WHERE path=?", (key_path,)
                    )
                    self._pending_touch.pop(key_path, None)
                    to_unlink.append((key_path, file))
                    total -= int(nbytes)
                    removed += 1
                    if total <= budget:
                        break
                self._conn.commit()
        if to_unlink:
            blob_dir = self._blob_dir

            def _sweep_pruned() -> None:
                # A worker can re-``store`` an evicted (path, mtime, size)
                # while the sweep runs; the blob name is a hash of that key, so
                # it rewrites the very file queued here and the async unlink
                # would delete a *referenced* blob.  Re-check each queued
                # row by PK (batched) and keep anything referenced again.
                for i in range(0, len(to_unlink), _UNLINK_CHUNK):
                    batch = to_unlink[i : i + _UNLINK_CHUNK]
                    keys = [k for k, _f in batch]
                    try:
                        with self._lock:
                            live = set(
                                self._conn.execute(
                                    "SELECT path, file FROM thumb WHERE path IN"
                                    f" ({','.join('?' * len(keys))})",
                                    keys,
                                ).fetchall()
                            )
                    except sqlite3.Error:  # pragma: no cover (shutdown race)
                        live = set()
                    for key_path, file in batch:
                        if (key_path, file) in live:
                            continue  # stored again after the DELETE
                        try:
                            (blob_dir / file).unlink(missing_ok=True)
                        except OSError:  # pragma: no cover (defensive)
                            pass

            threading.Thread(
                target=_sweep_pruned,
                name="thumb-cache-prune",
                daemon=True,
            ).start()
        if removed:
            logger.debug("Pruned {} thumb cache blobs (budget {} B)", removed, budget)
        return removed

    def set_max_bytes(self, max_bytes: int) -> None:
        self._max_bytes = int(max_bytes)

    # ``set_bulk_writes`` / ``flush`` / ``close`` come from SqliteCacheBase.

    # --------------------------------------------------------------- internal

    def _delete_locked(self, key_path: str, file: str, *, commit: bool = True) -> None:
        """Delete a row + its blob file.  Caller holds ``self._lock``."""
        self._conn.execute("DELETE FROM thumb WHERE path=?", (key_path,))
        self._pending_touch.pop(key_path, None)
        try:
            (self._blob_dir / file).unlink(missing_ok=True)
        except OSError:  # pragma: no cover (defensive)
            pass
        if commit:
            self._conn.commit()


def sweep_orphan_blobs_async(blob_dir: Path) -> None:
    """Fire-and-forget :func:`sweep_orphan_blobs` on a daemon thread.

    Called after the index DB was quarantined as corrupt: every blob under
    *blob_dir* is an orphan then (the recreated index is empty, so nothing
    ever looks them up again and their bytes never enter the ``SUM(bytes)``
    budget), and the only other sweep lives in :meth:`ThumbDiskCache.clear`.
    Daemon so it can never hold the window open at shutdown.
    """
    threading.Thread(
        target=sweep_orphan_blobs,
        args=(blob_dir,),
        name="thumb-cache-orphans",
        daemon=True,
    ).start()


__all__ = [
    "ThumbDiskCache",
    "create_thumb_schema",
    "sweep_orphan_blobs",
    "sweep_orphan_blobs_async",
]
