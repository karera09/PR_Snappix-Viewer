"""SQLite cache of image aspect ratios for the gallery's justified layout.

The justified layout needs every visible tile's aspect ratio *before* it
can pack rows (otherwise rows reflow as thumbnails stream in — the jitter
the legacy ``"fit"`` mode suffered).  Reading image headers over a NAS for
hundreds of tiles on every folder visit would be slow, so we persist
``(width, height)`` keyed by path and validated by ``(mtime, size)``.

**The filesystem is always the source of truth.**  This cache only ever
*answers* aspect queries for paths a live directory scan already produced;
it never enumerates entries.  A row whose file changed (mtime/size
mismatch) is treated as a miss and re-probed; a row whose file was deleted
is simply never queried again (and eventually pruned).

Stale rows are deliberately **not** deleted on a mismatched ``get`` /
``get_many`` (unlike ``ThumbDiskCache.lookup`` / ``FolderPreviewCache.get``):
the query keys here come from scan *entries* that can themselves be outdated,
so the row may be FRESHER than the query — deleting on mismatch would destroy
the newer row (see the stale-row policy in :mod:`._sqlite_cache`).  Stale
rows are tiny and reclaimed by the ``probed_at`` LRU ``prune``.

This module is Qt-free (stdlib ``sqlite3`` only) so it unit-tests without a
``QApplication``.  Almost all access is on the GUI thread (the owning widget
calls ``get_many`` on populate and ``put`` when a probe result lands on the
main thread); probe *workers* never touch sqlite — they read headers and hand
back plain integers (see ``aspect_probe.py``).  The one exception is the
post-build ``prune`` (``cache_build_controller._prune_caches_async``), which
runs on a daemon thread to keep the byte-budget sweep off the GUI thread —
symmetric with :class:`~snappix.viewer.thumb_disk_cache.ThumbDiskCache`.  So,
like that store, the connection is opened ``check_same_thread=False`` and
every sqlite access is guarded by the shared ``RLock`` (WAL allows the
concurrent reads; the lock serialises the prune against live ``get_many`` /
``put``).

The aspect table shares one database file with the other two path-keyed
caches (:mod:`.viewer_cache` — ``data/viewer_cache.db``) but keeps its OWN
size budget, deliberately separate from the decoded-thumbnail disk cache
(``thumb_disk_cache.py``): aspect rows are tiny (~50–100 B) and high-value
(zero-reflow layout), so they must never be evicted under thumbnail-image
cache pressure.  ``prune`` only ever walks the ``aspect`` table, so sharing
the file changes where the bytes live, not which rows a budget may evict.
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from ._sqlite_cache import (
    QUERY_CHUNK,
    SqliteCacheBase,
    SqliteStoreBase,
    iter_chunks,
    mtime_matches,
)


def create_aspect_schema(conn) -> None:
    """Create the ``aspect`` table + its LRU index (idempotent).

    Module-level so the shared :class:`~snappix.viewer.viewer_cache.ViewerCacheStore`
    can materialise every table of ``viewer_cache.db`` up front — a table that
    only appeared when its cache happened to be enabled would read as
    ``no such table`` to the corruption probe and send a healthy file to
    quarantine.  The DDL still lives next to the cache that owns it.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aspect (
            path      TEXT PRIMARY KEY,
            mtime     REAL NOT NULL,
            size      INTEGER NOT NULL,
            width     INTEGER NOT NULL,
            height    INTEGER NOT NULL,
            probed_at REAL NOT NULL
        )
        """
    )
    # Index to make LRU pruning (ORDER BY probed_at) cheap.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_aspect_probed_at ON aspect(probed_at)"
    )


class ThumbMetaCache(SqliteCacheBase):
    """Path-keyed ``(width, height)`` cache validated by ``(mtime, size)``."""

    # 所有テーブル（open_with_recovery の "no such table" 破損分類に使う）。
    SCHEMA_TABLES = ("aspect",)
    # No LRU touch buffer: recency is ``probed_at``, set at put time.
    _TOUCH_SQL = None
    # Generic byte-estimate / prune declarations (see SqliteCacheBase).
    _TABLE = "aspect"
    _KEY_COL = "path"
    _ORDER_COL = "probed_at"
    _SIZE_SQL = "LENGTH(CAST(path AS BLOB))"
    # Path string bytes are measured directly; this covers the four
    # integer/real columns, the primary-key index entry and row overhead.
    _ROW_OVERHEAD = 64

    def __init__(
        self, db_path: Path, *, share: SqliteStoreBase | None = None,
    ) -> None:
        # Cross-thread: almost all access is on the GUI thread, but the
        # post-build ``prune`` runs on a daemon thread (see the module
        # docstring).  Every method guards its sqlite access with ``_lock``,
        # so — like ThumbDiskCache — the connection is opened
        # ``check_same_thread=False``.
        #
        # ``share`` attaches this cache to an already-open store's connection
        # (the viewer passes its :mod:`.viewer_cache` store); ``None`` opens
        # ``db_path`` standalone.
        super().__init__(db_path, check_same_thread=False, share=share)

    # ------------------------------------------------------------- schema

    def _migrate(self) -> None:
        # ``user_version`` gate: a standalone file is created here, while an
        # attached cache finds the shared store's version already stamped
        # (the host created every table before handing out the views) and
        # does nothing.  Keeping the gate is what lets "version says migrated
        # but the table is missing" stay a corruption signal.
        with self._lock:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                create_aspect_schema(self._conn)
                self._conn.execute("PRAGMA user_version=1")
                self._conn.commit()

    # --------------------------------------------------------------- reads

    def get(
        self, path: Path, mtime: float, size: int,
    ) -> tuple[int, int] | None:
        """Return cached ``(w, h)`` iff the row exists and mtime/size match.

        A mismatch is a plain miss — the row is NOT deleted (the caller's
        mtime/size may be the stale side; see the module docstring).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime, size, width, height FROM aspect WHERE path=?",
                (str(path),),
            ).fetchone()
        if row is None:
            return None
        c_mtime, c_size, w, h = row
        if c_size != size or not mtime_matches(c_mtime, mtime):
            return None
        return int(w), int(h)

    def get_many(
        self, specs: list[tuple[Path, float, int]],
    ) -> dict[str, tuple[int, int]]:
        """Batch lookup.

        ``specs`` is ``[(path, mtime, size), ...]``.  Returns a dict mapping
        ``str(path)`` → ``(w, h)`` for the subset whose cached mtime/size
        match.  Paths absent or stale are simply omitted (callers probe
        those).
        """
        if not specs:
            return {}
        want: dict[str, tuple[float, int]] = {
            str(p): (m, s) for p, m, s in specs
        }
        out: dict[str, tuple[int, int]] = {}
        with self._lock:
            for chunk in iter_chunks(list(want.keys()), QUERY_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT path, mtime, size, width, height FROM aspect "
                    f"WHERE path IN ({placeholders})",
                    chunk,
                ).fetchall()
                for path_str, c_mtime, c_size, w, h in rows:
                    m, s = want[path_str]
                    if c_size == s and mtime_matches(c_mtime, m):
                        out[path_str] = (int(w), int(h))
        return out

    # -------------------------------------------------------------- writes

    def put(
        self, path: Path, mtime: float, size: int, w: int, h: int,
        *, probed_at: float | None = None,
    ) -> None:
        if w <= 0 or h <= 0:
            return
        ts = time.time() if probed_at is None else probed_at
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO aspect"
                "(path, mtime, size, width, height, probed_at)"
                " VALUES (?,?,?,?,?,?)",
                (str(path), float(mtime), int(size), int(w), int(h), ts),
            )
            self._maybe_commit_locked()

    def put_many(
        self,
        rows: list[tuple[str, float, int, int, int]],
        *,
        probed_at: float | None = None,
    ) -> None:
        """Insert a batch of ``(path, mtime, size, w, h)`` probe results."""
        if not rows:
            return
        ts = time.time() if probed_at is None else probed_at
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO aspect"
                "(path, mtime, size, width, height, probed_at)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (p, float(m), int(s), int(w), int(h), ts)
                    for (p, m, s, w, h) in rows
                    if w > 0 and h > 0
                ],
            )
            self._maybe_commit_locked()

    # -------------------------------------------------------------- pruning

    def estimated_bytes(self) -> int:
        """Approximate on-disk footprint of the aspect rows."""
        with self._lock:
            return self._estimated_bytes_locked()

    def prune(self, max_bytes: int) -> int:
        """Evict oldest-probed rows until under ``max_bytes``.  Returns count.

        Independent of the thumbnail-image cache — this only ever touches
        the ``aspect`` table.  Runs off the GUI thread (the post-build daemon
        prune); the base-class sweep takes ``_lock`` once per pass and
        releases it in between, so concurrent GUI-side ``get_many`` / ``put``
        calls interleave with a long sweep instead of blocking until it
        finishes — the same per-pass locking ThumbDiskCache.prune uses.
        """
        deleted = self._prune_lru(max_bytes)
        if deleted:
            logger.debug("Pruned {} aspect cache rows (budget {} B)", deleted, max_bytes)
        return deleted

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM aspect").fetchone()[0]
            )

    def clear(self) -> int:
        """Delete every aspect row.  Returns the number removed."""
        with self._lock:
            n = int(
                self._conn.execute("SELECT COUNT(*) FROM aspect").fetchone()[0]
            )
            self._conn.execute("DELETE FROM aspect")
            self._conn.commit()
        return n


__all__ = ["ThumbMetaCache", "create_aspect_schema"]
