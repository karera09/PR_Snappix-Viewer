"""On-disk cache of *folder preview resolution* for instant folder revisits.

The thumbnail-image disk cache (``thumb_disk_cache.py``) already persists the
decoded bytes of a folder's representative image — but only keyed by that
*child image's* path.  What it does NOT persist is the step that decides
**which** child image represents a folder, plus the folder's ``post.md``
metadata.  That resolution is a per-folder ``os.scandir`` (+ a bounded BFS
descent + a ``post.md`` read) and it re-runs on every visit, which on a NAS
share is one round-trip per sub-folder — the part that still feels slow even
when the image bytes are warm.

This cache closes that gap: ``read_folder_preview``'s result is stored keyed
by the folder path and validated by the folder's ``mtime``, so a revisit
skips the scandir entirely and the representative thumbnail (and its aspect
ratio, via the existing probe) lands like any image file's.

Design mirrors the sibling caches:

* **The filesystem is the source of truth.**  This only ever *answers*
  preview queries for folders a live directory scan already produced; it
  never enumerates.  A row whose folder ``mtime`` changed is a miss and
  re-resolved; a deleted folder's row is never queried and pruned lazily.
* **Independent byte budget** — preview rows share ``viewer_cache.db`` with
  the aspect / thumbnail-index tables (:mod:`.viewer_cache`) but keep their
  OWN budget and their own LRU sweep, so they never evict (nor are evicted
  by) the thumbnail-image or aspect caches.

**Adding a new ``post.md``-derived column (the recipe):** rows are keyed by
folder path + (effectively immutable) mtime, so a newly added column would
stay at its backfill default *forever* — the folder never re-resolves.  Rows
therefore carry a **row-schema version** (``schema_ver``): to add a column,
add it to :func:`create_preview_schema` (so fresh files get it at CREATE
time), add an **idempotent** ``ALTER TABLE ADD COLUMN`` migration step with
any sentinel default in
:meth:`~snappix.viewer.viewer_cache.ViewerCacheStore._migrate` (for files an
older build wrote — check ``PRAGMA table_info`` first, because the ALTER
auto-commits before its version bump), bump that store's ``_SCHEMA_VER`` (it
is what a fresh file gets stamped with) and :data:`_ROW_SCHEMA_VER`, and stop
— rows written under the older row schema simply read as misses and lazily
re-resolve (one cheap scandir + ``post.md`` read each, only when actually
visited).  No wholesale wipe needed.  **Never interpolate :data:`_ROW_SCHEMA_VER` into migration
DDL** — a migration is history and must stamp the literal version that was
current when it ran, or every old row gets re-labelled as the newest schema
and the columns added *after* that migration silently read as their backfill
defaults forever.

**Invalidation caveat (documented limitation):** the folder ``mtime`` only
reflects changes to the folder's *direct* children (add / remove / rename).
``read_folder_preview`` may descend into sub-folders (gallery-per-subfolder
layouts), and a change deep in that subtree does NOT bump the top folder's
mtime — so a stale representative can survive until the folder itself
changes or the cache is rebuilt.  This is acceptable for post folders
(effectively immutable once written by the external tool) and matches
the "FS is truth, cache is an accelerator" contract of the other caches.

Thread-safe: the scanner's metadata pool and the thumbnail loader's worker
pool both resolve folders concurrently, so every sqlite access is guarded by
a lock (WAL mode allows concurrent reads; the lock serialises writes).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from ._sqlite_cache import (
    QUERY_CHUNK,
    SqliteCacheBase,
    SqliteStoreBase,
    iter_chunks,
    mtime_matches,
)

# Version of the *row payload* (the set of post.md-derived columns a fully
# populated row carries).  Bump when adding a column — see the module
# docstring's recipe.  Rows whose stored ``schema_ver`` differs are treated
# as misses and lazily re-resolved; they are never wiped wholesale.
_ROW_SCHEMA_VER = 2


def create_preview_schema(conn) -> None:
    """Create the ``preview`` table + its LRU index (idempotent).

    Module-level for the same reason as
    :func:`~snappix.viewer.thumb_meta_cache.create_aspect_schema`.  The column
    set is the *current* one: the incremental ``ALTER TABLE`` history that
    grew the old standalone ``viewer_folder_cache.db`` is gone with that file
    (see :func:`~snappix.viewer.viewer_cache.discard_legacy_cache_dbs`) — a
    brand-new database has nothing to migrate from.  Future additions follow
    the row-schema recipe in the module docstring.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS preview (
            path             TEXT PRIMARY KEY,
            mtime            REAL NOT NULL,
            has_post_md      INTEGER NOT NULL,
            title            TEXT NOT NULL,
            posted_at        TEXT NOT NULL,
            tags             TEXT NOT NULL,
            locked_count     INTEGER NOT NULL,
            thumb_marker     TEXT NOT NULL,
            non_thumb_marker TEXT NOT NULL,
            file_names       TEXT NOT NULL,
            favorites        INTEGER NOT NULL DEFAULT -1,
            plan_name        TEXT NOT NULL DEFAULT '',
            plan_price       TEXT NOT NULL DEFAULT '',
            service          TEXT NOT NULL DEFAULT '',
            post_id          TEXT NOT NULL DEFAULT '',
            schema_ver       INTEGER NOT NULL DEFAULT 1,
            used_at          REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_preview_used_at ON preview(used_at)"
    )


@dataclass(frozen=True)
class FolderPreview:
    """The cacheable result of resolving one folder's preview.

    Carries exactly the fields ``read_folder_preview`` surfaces that the UI
    needs to build an enriched :class:`folder_scan.FolderEntry` without
    re-scanning: both representative-image candidates (so the ``#thumb#``
    exclude toggle works without a rescan), plus the ``post.md``-derived
    metadata and the folder's direct file names (for the PostGrid filter).
    """

    has_post_md: bool
    title: str
    posted_at: datetime | None
    tags: list[str]
    locked_count: int
    thumb_marker_path: Path | None
    non_thumb_marker_path: Path | None
    file_names: list[str]
    #: Post favorite / like count (``None`` when unknown).  Stored as -1 in
    #: the integer column (favorites are never negative) so the sentinel
    #: round-trips back to ``None``.
    favorites: int | None = None
    #: Plan / tier name and price (verbatim, with currency symbol) from
    #: ``post.md``.  Empty string when absent.  Searchable via the ``plan:`` /
    #: ``plan_price:`` field prefixes in the PostGrid filter.
    plan_name: str = ""
    plan_price: str = ""
    #: ``post.md``'s ``service`` / ``post_id`` meta pair.  NOT displayed —
    #: they exist so a cache hit can rebuild the ``postref`` row the browse
    #: metadata scan writes (``scan_children`` → ``search_index.postref_row``).
    #: Without them a warm folder-preview cache silently stopped the
    #: downloaded-post link index from ever being populated.  Empty string
    #: when the ``post.md`` carries no such meta (older downloads).
    service: str = ""
    post_id: str = ""


def _path_to_text(p: Path | None) -> str:
    return str(p) if p is not None else ""


def _text_to_path(s: str) -> Path | None:
    return Path(s) if s else None


class FolderPreviewCache(SqliteCacheBase):
    """Folder-path-keyed preview store validated by the folder ``mtime``."""

    # 所有テーブル（open_with_recovery の "no such table" 破損分類に使う）。
    SCHEMA_TABLES = ("preview",)
    _TOUCH_SQL = "UPDATE preview SET used_at=? WHERE path=?"
    # Generic byte-estimate / prune declarations (see SqliteCacheBase).
    _TABLE = "preview"
    _KEY_COL = "path"
    _ORDER_COL = "used_at"
    _SIZE_SQL = (
        "LENGTH(CAST(path AS BLOB))+LENGTH(CAST(title AS BLOB))"
        "+LENGTH(CAST(posted_at AS BLOB))+LENGTH(CAST(tags AS BLOB))"
        "+LENGTH(CAST(thumb_marker AS BLOB))+LENGTH(CAST(non_thumb_marker AS BLOB))"
        "+LENGTH(CAST(file_names AS BLOB))"
        "+LENGTH(CAST(plan_name AS BLOB))+LENGTH(CAST(plan_price AS BLOB))"
        "+LENGTH(CAST(service AS BLOB))+LENGTH(CAST(post_id AS BLOB))"
    )
    _ROW_OVERHEAD = 96

    def __init__(
        self,
        db_path: Path,
        *,
        max_bytes: int,
        share: SqliteStoreBase | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        # Cross-thread: the scanner + loader pools both resolve folders.
        # ``share`` attaches to an already-open store's connection (the
        # viewer's :mod:`.viewer_cache` store); ``None`` opens ``db_path``
        # standalone.
        super().__init__(db_path, check_same_thread=False, share=share)

    # ------------------------------------------------------------- schema

    def _migrate(self) -> None:
        # See ThumbMetaCache._migrate for why the version gate stays.
        with self._lock:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                create_preview_schema(self._conn)
                self._conn.execute("PRAGMA user_version=1")
                self._conn.commit()

    # --------------------------------------------------------------- reads

    @staticmethod
    def _row_to_preview(row) -> FolderPreview:
        (
            has_post_md, title, posted_at, tags, locked_count,
            thumb_marker, non_thumb_marker, file_names, favorites,
            plan_name, plan_price, service, post_id,
        ) = row
        return FolderPreview(
            has_post_md=bool(has_post_md),
            title=title,
            posted_at=_parse_dt(posted_at),
            tags=_loads_list(tags),
            locked_count=int(locked_count),
            thumb_marker_path=_text_to_path(thumb_marker),
            non_thumb_marker_path=_text_to_path(non_thumb_marker),
            file_names=_loads_list(file_names),
            favorites=(None if int(favorites) < 0 else int(favorites)),
            plan_name=plan_name or "",
            plan_price=plan_price or "",
            service=service or "",
            post_id=post_id or "",
        )

    _SELECT_COLS = (
        "has_post_md, title, posted_at, tags, locked_count, "
        "thumb_marker, non_thumb_marker, file_names, favorites, "
        "plan_name, plan_price, service, post_id"
    )

    def get_postref(self, path: Path) -> tuple[str, str] | None:
        """Return the cached ``(service, post_id)`` for *path*, or ``None``.

        Deliberately **mtime-agnostic**, unlike :meth:`get`: the pair is the
        folder's *identity* (which downloaded post it is), not a rendering of
        its contents, so a row whose ``mtime`` has since moved on still names
        the same post.  The caller is the curation write path, which needs the
        postref columns for rename tracking while standing INSIDE the post
        folder — there is no populated entry to read it off, and re-reading
        ``post.md`` on every ★ would put a possibly-unreachable share on the
        GUI thread.  A stale row is therefore better than no answer, and a
        row is never written for a folder without a ``post.md`` pair.

        Returns ``None`` for a missing row or an empty pair.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT service, post_id FROM preview WHERE path=?",
                (str(path),),
            ).fetchone()
        if row is None:
            return None
        service, post_id = (row[0] or ""), (row[1] or "")
        if not service or not post_id:
            return None
        return service, post_id

    def get(self, path: Path, mtime: float) -> FolderPreview | None:
        """Return the cached preview iff the row exists and mtime matches.

        The *mtime* key comes from a stat the caller just performed, so a
        mismatched (or old-row-schema) row is proven outdated and deleted
        eagerly — the next resolve re-``put``\\s it fresh.
        """
        key_path = str(path)
        with self._lock:
            row = self._conn.execute(
                f"SELECT mtime, schema_ver, {self._SELECT_COLS}"
                f" FROM preview WHERE path=?",
                (key_path,),
            ).fetchone()
            if row is None:
                return None
            c_mtime, row_schema = row[0], int(row[1])
            if row_schema != _ROW_SCHEMA_VER or not mtime_matches(c_mtime, mtime):
                # Stale (folder's direct children changed) or written under
                # an older row schema (missing newer columns) — drop + miss.
                self._conn.execute("DELETE FROM preview WHERE path=?", (key_path,))
                self._pending_touch.pop(key_path, None)
                self._conn.commit()
                return None
            self._touch_locked(key_path)
            return self._row_to_preview(row[2:])

    def get_many(
        self, specs: list[tuple[Path, float]],
    ) -> dict[str, FolderPreview]:
        """Batch lookup.  ``specs`` is ``[(path, mtime), ...]``.

        Returns ``{str(path): FolderPreview}`` for the subset whose cached
        mtime matches (and whose row schema is current).  Absent / stale
        paths are simply omitted (callers resolve those live).  Stale rows
        are NOT deleted here (the grid's seed path runs on the GUI thread and
        we keep it allocation-light); they're overwritten on the next live
        resolve + ``put``.

        **Three callers, not one**: the grid's seed
        (``children_grid._seed_folder_resolution``, GUI thread), the scan
        worker's bulk resolve (``scan_children``) and the recursive search
        worker (``scan_search``).  They contend for the one ``RLock`` below,
        so the batch size is also the worst case a *GUI* seed can be made to
        wait: sizing it is a responsiveness decision, not just a throughput
        one.

        The LRU touch is applied **once for the whole batch**
        (:meth:`_touch_many_locked`), so a 900-folder seed costs at most one
        ``executemany`` + ``commit`` inside the lock instead of one per
        ``TOUCH_FLUSH_THRESHOLD`` hits.  The price is LRU granularity: every
        hit in one call shares a single timestamp.
        """
        if not specs:
            return {}
        want: dict[str, float] = {str(p): m for p, m in specs}
        out: dict[str, FolderPreview] = {}
        # Hold the lock for the whole batch: a worker pool may be writing
        # concurrently, and sharing the one connection without serialising
        # would race the write cursor.  The batch is bounded by the caller's
        # own page of folders, and one ``QUERY_CHUNK`` round is a few hundred
        # µs of local sqlite, so the wait a GUI-thread seed can inherit from a
        # worker's batch stays well inside a frame.
        with self._lock:
            for chunk in iter_chunks(list(want.keys()), QUERY_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT path, mtime, schema_ver, {self._SELECT_COLS}"
                    f" FROM preview WHERE path IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    key_path, c_mtime, row_schema = row[0], row[1], int(row[2])
                    if (
                        row_schema == _ROW_SCHEMA_VER
                        and mtime_matches(c_mtime, want[key_path])
                    ):
                        out[key_path] = self._row_to_preview(row[3:])
            # One touch for the whole batch: ``_touch_locked`` per hit makes
            # the flush threshold trip mid-loop, each trip doing an
            # ``executemany`` + ``commit`` while this GUI-thread call still
            # holds the lock.
            self._touch_many_locked(list(out.keys()))
        return out

    # -------------------------------------------------------------- writes

    def put(self, path: Path, mtime: float, preview: FolderPreview) -> None:
        """Upsert the resolved *preview* for *path* keyed by *mtime*.

        Commits through the base class's bulk-aware
        ``_maybe_commit_locked`` (#40): live browse writes still commit
        per row (bulk mode off), while a cache build that streams one row
        per walked directory batches them under ``set_bulk_writes(True)``
        — per-row fsync across a whole library would otherwise dominate
        the walk.
        """
        key_path = str(path)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO preview"
                "(path, mtime, has_post_md, title, posted_at, tags,"
                " locked_count, thumb_marker, non_thumb_marker, file_names,"
                " favorites, plan_name, plan_price, service, post_id,"
                " schema_ver, used_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key_path,
                    float(mtime),
                    1 if preview.has_post_md else 0,
                    preview.title or "",
                    _fmt_dt(preview.posted_at),
                    _dumps_list(preview.tags),
                    int(preview.locked_count),
                    _path_to_text(preview.thumb_marker_path),
                    _path_to_text(preview.non_thumb_marker_path),
                    _dumps_list(preview.file_names),
                    -1 if preview.favorites is None else int(preview.favorites),
                    preview.plan_name or "",
                    preview.plan_price or "",
                    preview.service or "",
                    preview.post_id or "",
                    _ROW_SCHEMA_VER,
                    time.time(),
                ),
            )
            self._maybe_commit_locked()

    # -------------------------------------------------------------- pruning

    def estimated_bytes(self) -> int:
        """Approximate on-disk footprint (monotonic with content, no VACUUM)."""
        with self._lock:
            return self._estimated_bytes_locked()

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM preview").fetchone()[0]
            )

    def prune(self, max_bytes: int | None = None) -> int:
        """Evict least-recently-used rows until under budget.  Returns count.

        The base-class sweep takes ``_lock`` once per pass (releasing it in
        between), so concurrent GUI-side reads are not blocked for the whole
        sweep.
        """
        budget = self._max_bytes if max_bytes is None else max_bytes
        deleted = self._prune_lru(budget)
        if deleted:
            logger.debug(
                "Pruned {} folder preview rows (budget {} B)", deleted, budget
            )
        return deleted

    def set_max_bytes(self, max_bytes: int) -> None:
        self._max_bytes = int(max_bytes)

    def clear(self) -> int:
        """Delete every cached preview row.  Returns the number removed."""
        with self._lock:
            n = self.count()
            self._pending_touch.clear()
            self._conn.execute("DELETE FROM preview")
            self._conn.commit()
            return n

    def invalidate_under(self, root: Path | str) -> int:
        """Drop the row for *root* and every row beneath it.  Returns count.

        Targeted invalidation for external rewrites: another tool can
        overwrite ``post.md`` / image bytes **in place**, which does NOT bump
        the parent folder's mtime — the row would keep answering with stale
        metadata forever (the module docstring's documented mtime
        limitation).  Deleting the subtree forces a live re-resolve on the
        next visit.

        Matching is textual with ``COLLATE NOCASE`` (paths may arrive from
        another tool's config with different ASCII casing than the rows the
        viewer's own scans stored); over-deletion on a case-sensitive volume
        is harmless — this is an accelerator, the FS stays the source of
        truth.  Pending LRU touches for deleted rows degrade to no-op
        UPDATEs, so the touch buffer needs no scrubbing.
        """
        base = str(root).rstrip("\\/")
        if not base:
            return 0
        prefix = base + os.sep
        # Range upper bound: must sort above ANY code point a path can start
        # with.  chr(0xFFFF) only covers the BMP — a folder name starting with
        # a non-BMP character (emoji etc.) sorts above it in UTF-8 memcmp
        # order and would escape the invalidation range.  U+10FFFF is the
        # maximum code point, so ``prefix + chr(0x10FFFF)`` bounds them all.
        hi = prefix + chr(0x10FFFF)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM preview WHERE path COLLATE NOCASE = ? OR ("
                "path COLLATE NOCASE >= ? AND path COLLATE NOCASE < ?)",
                (base, prefix, hi),
            )
            self._conn.commit()
            n = cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0
        if n:
            logger.debug("Invalidated {} folder preview rows under {}", n, base)
        return n

    # ``flush`` / ``close`` and the LRU-touch buffer come from SqliteCacheBase.


def _fmt_dt(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else ""


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:  # pragma: no cover (defensive)
        return None


def _dumps_list(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _loads_list(s: str) -> list[str]:
    if not s:
        return []
    try:
        val = json.loads(s)
    except (ValueError, TypeError):  # pragma: no cover (defensive)
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


__all__ = ["FolderPreview", "FolderPreviewCache", "create_preview_schema"]
