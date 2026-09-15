"""SQLite *search index* for the viewer's file-name search and post links.

Unlike the sibling accelerator caches (``thumb_meta_cache`` / ``thumb_disk_cache``
/ ``folder_preview_cache``), which are strictly *point-lookup* stores that
**never enumerate** (they only answer for a path a live scan already produced),
this is a genuine **index**: it is *queried by prefix* to enumerate matches.
That different role is deliberate and kept honest by the same "filesystem is
the source of truth" contract the caches use:

* Every row carries the key it was indexed with (file ``mtime`` / ``size``),
  but that key rides along for **display**, not validation: the one consumer
  (``scan_search._node_row_to_entry``) does no filesystem I/O at all, so a row
  whose file changed or vanished is surfaced as-is until the live walk below
  corrects it.  The authority for staleness is that walk, never a per-hit
  re-``stat`` (the same stale policy ``docs/claude/viewer/scanning.md``
  records for the other index-seeded listings).
* The viewer always runs a **live walk in parallel** (stale-while-revalidate):
  the index gives instant results, the live walk fills anything the index is
  missing (un-indexed sub-trees) and corrects anything stale.  So a cold or
  out-of-date index only ever means "a bit more live walking", never a wrong
  answer.  A full (re)build or "clear" resolves drift.

Two concerns share one database file (both are parts of the one "search"
concern, so this does not violate the one-budget-per-concern rule the other
caches follow):

* ``node`` — one row per file *and* folder under any indexed root, for the
  PostGrid recursive file-name search.  Indexed **indiscriminately** (no
  ``post.md`` requirement, no suffix filter) so coverage matches the live
  :func:`folder_scan.walk_for_search`.
* ``postref`` — ``(service, post_id) → folder`` for the MarkdownView
  "downloaded-post link" feature (schema version 2, see :meth:`_migrate`).

A third table, ``post`` (one row per ``post.md`` holding its full text for the
standalone full-text search dialog), existed up to schema version 2.  That
dialog was retired in favour of the filter box's ``body:`` syntax, which reads
``post.md`` lazily from the filesystem (issue #81), so version 3 **drops** the
table: an index built by an older version sheds its stored bodies on first
open (the file is not ``VACUUM``-ed — freed pages are reused by later writes).

**Only ``node`` is subject to the capacity limit.**  ``postref`` rows are
*derived from ``post.md`` files* (a NAS read + parse per file) and tiny, so
:meth:`prune` **never evicts them** — they persist regardless of the byte
budget, which governs only the cheaply regenerable file-name (``node``) index.

Matching semantics mirror the live path exactly: the *authority* is Python
``casefold() in`` (``query in rel``).  SQL ``instr`` on a stored casefolded
path is used only as a **superset prefilter** to narrow rows before the exact
Python check — never as the final arbiter — so the existing 1–2 character /
symbol semantics are preserved byte-for-byte.

Thread-safe: the scanner's metadata pool, the recursive search worker, and
the cache builder all touch it concurrently, so every sqlite access is guarded by a
lock (WAL allows concurrent reads; the lock serialises writes).
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ..common.post_meta import KEY_POST_ID, KEY_SERVICE
from ._sqlite_cache import SqliteCacheBase

# Approximate fixed bytes charged per row on top of measured text lengths
# (integer/real columns, PK + LRU index entries, sqlite row overhead).
_NODE_ROW_OVERHEAD = 96
_POSTREF_ROW_OVERHEAD = 96
# Per-row text footprint of a ``node`` row (CAST AS BLOB so LENGTH counts UTF-8
# bytes, not characters).  One definition shared by the budget estimate and the
# prune loop's running total so the two can never drift apart.
_NODE_SIZE_SQL = (
    "LENGTH(CAST(path AS BLOB))+LENGTH(CAST(name AS BLOB))"
    "+LENGTH(CAST(cpath AS BLOB))"
)
# Rowid window per locked chunk of the root-case candidate scan
# (``_canonicalize_root`` phase 1).  That scan has no usable index (a
# casefolded range / ``LOWER()`` cannot seek), so it walks the table — chunking
# it by rowid bounds the time the index lock is held per statement, letting the
# GUI's ``resolve_postref`` interleave instead of waiting out a whole-table
# sweep (#92).
_SCAN_WINDOW = 20000
# Upper-bound sentinel for a prefix range scan: the highest legal Unicode
# scalar (U+10FFFF, UTF-8 ``F4 8F BF BF``) so ``path < lo + _HI`` captures
# every descendant of ``lo`` — including names beginning with a non-BMP
# character (emoji ``🐱`` U+1F431 etc.).  A BMP sentinel (U+FFFF) sorts *below*
# such names in both Python codepoint order and SQLite UTF-8 memcmp, silently
# dropping those sub-trees from the range.  This mirrors the tagger's writer
# sentinel (``tagger/tagdb.py``).
_HI = "\U0010FFFF"
# Cap on the number of seed-hit rows whose ``used_at`` a single
# ``query_filenames`` call refreshes (項目54).  A 1-character query can hit
# the 50 000-row limit; touching *every* hit inside the query executed a
# ~50 000-row ``UPDATE`` while holding the index lock (~0.8 s measured on a
# 120k-node index), stalling the GUI's ``resolve_postref`` and the scan
# workers' ``upsert_nodes`` behind it.  Touching only the first N hits (the
# rows actually handed to the UI as the instant seed) — and doing it in a
# separate, short lock acquisition after the query — keeps the LRU signal
# ("this subtree's seed rows are in active use") at a bounded cost.
TOUCH_USED_LIMIT = 512
# Whether path spellings differing only in letter case may denote the SAME
# directory.  True only on Windows: on POSIX hosts (Linux / NAS direct
# mounts) ``/data/Lib`` and ``/data/lib`` are two *different* real
# directories, so the root case fold below must never run there — it would
# destructively merge two distinct libraries' rows.  Module-level so tests
# can simulate a case-sensitive host.
_CASE_INSENSITIVE_FS = os.name == "nt"


@dataclass(frozen=True)
class NodeRow:
    """One indexed filesystem entry (file or folder)."""

    path: Path
    name: str  # original-case basename (used as the entry title)
    is_dir: bool
    mtime: float
    size: int


def _prefix_bounds(root: Path) -> tuple[str, str]:
    """Return ``(lo, hi)`` so ``lo <= path < hi`` selects strict descendants.

    ``lo`` is ``str(root)`` + the OS separator, so the root row itself is
    excluded and only entries *under* it match (mirrors ``walk_for_search``
    excluding the root).
    """
    lo = str(root)
    if not lo.endswith(os.sep):
        lo += os.sep
    return lo, lo + _HI


def postref_row(
    meta: Mapping[str, str], folder: Path | str, mtime: float,
) -> tuple[str, str, str, float] | None:
    """Build one ``postref`` upsert row from parsed ``post.md`` meta.

    The single place that maps a :class:`~snappix.viewer.post_md.ParsedPost`'s
    meta dict to the ``(service, post_id, folder, mtime)`` tuple
    :meth:`SearchIndex.upsert_postrefs` consumes — the browse-time metadata
    scan (``scan_children``) and the full build (``cache_builder``) both call
    this, so the meta-key names stay tied to the shared ``post.md`` contract
    (:data:`~snappix.common.post_meta.KEY_SERVICE` /
    :data:`~snappix.common.post_meta.KEY_POST_ID`) in one spot.  Returns
    ``None`` when either key is absent/empty (older ``post.md`` — no usable
    lookup key).
    """
    service = meta.get(KEY_SERVICE)
    post_id = meta.get(KEY_POST_ID)
    if not service or not post_id:
        return None
    return service, post_id, str(folder), float(mtime)


class SearchIndex(SqliteCacheBase):
    """File-name search index + downloaded-post link map (one DB, one budget)."""

    # 所有テーブル（open_with_recovery の "no such table" 破損分類に使う）。
    SCHEMA_TABLES = ("node", "postref")
    # Only ``node`` is LRU-touched: ``postref`` is never pruned
    # (their ``used_at`` is set at upsert time and left alone), so the base
    # class's touch buffer is wired to the ``node`` table alone (#178).
    # A node's ``used_at`` means "recently *served to the UI* as a seed hit"
    # (only the first TOUCH_USED_LIMIT hits per query are touched — 項目54),
    # not "matched by some query at some point".
    _TOUCH_SQL = "UPDATE node SET used_at=? WHERE path=?"

    def __init__(
        self, db_path: Path, *, max_bytes: int, use_fts: bool = False,
    ) -> None:
        self._max_bytes = max_bytes
        self._use_fts = use_fts  # reserved: trigram candidate prefilter (off by default)
        # Roots already case-folded this session (see ``_canonicalize_root``):
        # the fold runs at most once per distinct root.  Keyed by the
        # current-case ``lo`` bound.
        self._root_case_done: set[str] = set()
        # Cross-thread: the scanner's metadata pool, the search worker and
        # the cache builder all touch this index concurrently.
        super().__init__(db_path, check_same_thread=False)

    # ------------------------------------------------------------- schema

    def _migrate(self) -> None:
        with self._lock:
            ver = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS node (
                        path    TEXT PRIMARY KEY,
                        name    TEXT NOT NULL,
                        cpath   TEXT NOT NULL,
                        is_dir  INTEGER NOT NULL,
                        mtime   REAL NOT NULL,
                        size    INTEGER NOT NULL,
                        used_at REAL NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_node_used_at ON node(used_at)"
                )
                # (Schema version 1 also created the ``post`` full-text table
                # — dropped again by version 3 below, so a fresh index never
                # creates it.)
                self._conn.execute("PRAGMA user_version=1")
                self._conn.commit()
                ver = 1
            if ver < 2:
                # ``postref`` — point-lookup map ``(service, post_id) → folder``
                # for the MarkdownView "downloaded-post link" feature.  Unlike
                # ``node`` (queried by prefix), this is a pure PK lookup: a
                # body link's URL is parsed to ``(service, post_id)`` and
                # resolved to the local folder.  Populated incrementally by
                # the metadata scan (visited roots' direct children) and fully
                # by ``CacheBuilder`` (recursive).  It is derived from
                # ``post.md`` files, so :meth:`prune` **never evicts it**
                # (only ``node`` is bounded by the byte budget — see the module
                # docstring); its ``used_at`` is set at upsert time and kept
                # only as ordering metadata.
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS postref (
                        service TEXT NOT NULL,
                        post_id TEXT NOT NULL,
                        folder  TEXT NOT NULL,
                        mtime   REAL NOT NULL,
                        used_at REAL NOT NULL,
                        PRIMARY KEY (service, post_id)
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_postref_used_at"
                    " ON postref(used_at)"
                )
                self._conn.execute("PRAGMA user_version=2")
                self._conn.commit()
                ver = 2
            if ver < 3:
                # The standalone full-text search dialog is gone (issue #81);
                # its ``post`` table (full ``post.md`` bodies, never pruned —
                # hundreds of MB on a big library) has no reader left.  Drop
                # it so an index built by an older version stops carrying the
                # dead corpus.  ``IF EXISTS`` keeps this idempotent for a
                # fresh file (version 1 above no longer creates the table).
                self._conn.execute("DROP TABLE IF EXISTS post")
                self._conn.execute("PRAGMA user_version=3")
                self._conn.commit()

    # ``set_bulk_writes`` / ``_maybe_commit_locked`` / ``flush`` / ``close``
    # come from SqliteCacheBase.

    # --------------------------------------------------------- node writes

    def upsert_nodes(self, rows: list[NodeRow]) -> None:
        """Insert/replace a batch of filesystem entries."""
        if not rows:
            return
        now = time.time()
        params = [
            (
                str(r.path),
                r.name,
                str(r.path).casefold(),
                1 if r.is_dir else 0,
                float(r.mtime),
                int(r.size),
                now,
            )
            for r in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO node"
                "(path, name, cpath, is_dir, mtime, size, used_at)"
                " VALUES (?,?,?,?,?,?,?)",
                params,
            )
            self._maybe_commit_locked()

    # ------------------------------------------------------- root case fold

    def _collect_root_variants(
        self, table: str, col: str, cf_col: str | None, n: int, lo: str, hi: str,
    ) -> set[str]:
        """Distinct stored spellings of *root*'s prefix in *table* (≠ current).

        The ``_canonicalize_root`` phase-1 helper.  Neither predicate can use an
        index (``node`` has no index on the casefolded ``cpath``; ``postref``
        applies ``LOWER()``), so this necessarily walks the table —
        but it does so in **bounded ``rowid`` windows, taking the lock once per
        window and releasing it in between**, so a big library's scan no longer
        blocks the GUI's ``resolve_postref`` (or the search worker) for the
        whole sweep (#92).  ``substr`` isolates the stored root prefix so rows
        already at the current case are skipped.
        """
        if cf_col is not None:
            # ``node`` carries a casefolded ``cpath`` (folds non-ASCII too).
            cond = f"{cf_col} >= ? AND {cf_col} < ?"
            lo_cf = lo.casefold()
            bounds: tuple = (lo_cf, lo_cf + _HI)
        else:
            # No casefolded column: fall back to sqlite ``LOWER()`` (ASCII-only
            # — exactly the drive-letter case this targets).
            cond = f"LOWER({col}) >= LOWER(?) AND LOWER({col}) < LOWER(?)"
            bounds = (lo, hi)
        sql = (
            f"SELECT DISTINCT substr({col}, 1, ?) FROM {table}"
            f" WHERE rowid > ? AND rowid <= ? AND {cond}"
            f" AND substr({col}, 1, ?) <> ?"
        )
        out: set[str] = set()
        with self._lock:
            # O(1): the last entry of the table b-tree.
            top = self._conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
        start = 0
        top = int(top or 0)
        while start < top:
            end = start + _SCAN_WINDOW
            with self._lock:
                out.update(
                    row[0]
                    for row in self._conn.execute(
                        sql, (n, start, end) + bounds + (n, lo)
                    ).fetchall()
                )
            start = end
        return out

    def _canonicalize_root(self, root: Path) -> tuple[str, str]:
        """Return current-case prefix bounds, folding any stored case-variant.

        Windows paths are case-insensitive, but ``path`` / ``folder`` store
        whatever letter case the indexing walk enumerated and every prefix
        range scan here is a **byte** comparison (case-sensitive even on a
        case-insensitive filesystem).  So indexing ``D:\\lib`` and later
        browsing ``d:\\lib`` made the seed (``node``) range query silently
        return zero rows — and, because writes went on under the new spelling,
        accumulated a *second* copy of every row under the variant case
        (``postref`` is never pruned, so its links double-persisted).

        Mirrors the AI plugin's ``tag_db.resolve_root_case`` case correction,
        but adapted to this **read/write** index: rather than merely *reading*
        the stored spelling, it **rewrites** stored rows to the current one so
        reads, subsequent writes and the duplicate cleanup all converge on a
        single case.  Runs at most once per distinct root per session
        (cached).  Each case-variant row's root prefix is renamed to the
        current spelling; a pre-existing duplicate at the canonical key is
        resolved **by ``mtime`` — the newer row wins** (a stale variant left
        from an earlier walk must never clobber a freshly-indexed canonical
        row: the variant is dropped first when the canonical duplicate is at
        least as new, otherwise the rename's ``OR REPLACE`` retires the older
        canonical row).  ``COLLATE NOCASE`` / a schema migration is
        deliberately avoided (item 22).

        The fold is **guarded three ways** so it can never merge or corrupt
        rows of a genuinely different root:

        * It runs only on a case-insensitive filesystem host
          (:data:`_CASE_INSENSITIVE_FS`, i.e. Windows) — on POSIX two
          spellings are two distinct real directories.
        * A stored spelling is folded only after :func:`os.path.samefile`
          confirms it denotes the **same physical directory** as *root*
          (Windows volumes can have per-directory case sensitivity enabled,
          e.g. via WSL, where ``Lib`` and ``lib`` genuinely coexist).
          Unverifiable spellings (offline NAS, vanished dir) are left alone.
        * Only **length-preserving** case variants are folded
          (``casefold`` can change length — ``"ß"`` → ``"ss"`` — which would
          desync the fixed-position prefix splice below and cut into child
          names; NTFS does not treat such names as equal anyway).

        The ``samefile`` vetting stats each candidate directory — a NAS
        round-trip that can block or fail on an offline mount — so it is run
        **outside the index lock**, between the candidate-collect phase (itself
        chunked so it never holds the lock for a whole-table sweep, see
        :meth:`_collect_root_variants`) and the locked rewrite phase.  A slow
        or vanished share then
        never freezes the other index users (search worker, scanner pool,
        GUI) behind a held lock; an ``OSError`` aborts that candidate at once
        rather than blocking.
        """
        lo, hi = _prefix_bounds(root)
        if not _CASE_INSENSITIVE_FS:
            return lo, hi
        if lo in self._root_case_done:
            return lo, hi
        lo_cf = lo.casefold()
        n = len(lo)
        # Phase 1 (in-DB only — no filesystem I/O): collect the distinct
        # *stored spellings* of this root's prefix that differ from the current
        # one.  Each table is walked in rowid windows that take the lock one
        # window at a time (see :meth:`_collect_root_variants`).
        with self._lock:
            if lo in self._root_case_done:
                return lo, hi
        cands: set[str] = set()
        for table, col, cf_col in (
            ("node", "path", "cpath"),
            ("postref", "folder", None),
        ):
            cands |= self._collect_root_variants(table, col, cf_col, n, lo, hi)
        # Phase 2 (UNLOCKED): vet each candidate.  ``os.path.samefile`` stats
        # the directory (the NAS round-trip) — kept out of the lock so an
        # offline share cannot stall other index users.
        variants: list[str] = []
        for v in cands:
            # Length-preserving case variant only: a same-length prefix whose
            # casefold equals ``lo``'s.  This rejects ``ß``/``ss``-style
            # length-changing folds, where splicing at ``n`` would cut into
            # (or duplicate) child-name characters.
            if len(v) != n or v.casefold() != lo_cf:
                continue
            # Same-directory confirmation: fold only spellings the filesystem
            # itself says are the same folder as *root*.  An OSError (offline
            # NAS, vanished dir) aborts this candidate immediately.
            try:
                if not os.path.samefile(v, str(root)):
                    continue
            except (OSError, ValueError):
                continue
            variants.append(v)
        # Phase 3 (locked): rewrite the confirmed variants, preferring the
        # row with the newer ``mtime`` on any primary-key collision.
        with self._lock:
            if lo in self._root_case_done:
                return lo, hi
            if variants:
                self._flush_touches_locked()
                for v in variants:
                    v_hi = v + _HI
                    # ``node`` PK is the path: a canonical duplicate may
                    # already exist.  Drop the variant row first when the
                    # canonical one is at least as new (canonical wins ties);
                    # the ``OR REPLACE`` rename then retires any *older*
                    # canonical row the surviving (newer) variant supersedes.
                    self._conn.execute(
                        "DELETE FROM node WHERE path >= ? AND path < ?"
                        " AND EXISTS (SELECT 1 FROM node c"
                        "   WHERE c.path = ? || substr(node.path, ?)"
                        "     AND c.mtime >= node.mtime)",
                        (v, v_hi, lo, n + 1),
                    )
                    self._conn.execute(
                        "UPDATE OR REPLACE node SET path = ? || substr(path, ?)"
                        " WHERE path >= ? AND path < ?",
                        (lo, n + 1, v, v_hi),
                    )
                    # ``postref`` PK is ``(service, post_id)``; the folder
                    # rewrite never changes the key, so no collision (hence no
                    # mtime merge) is possible — this is pure case correction.
                    # ``folder`` is the ``post.md``'s parent: for a ``post.md``
                    # sitting directly in the root it equals the root itself
                    # (no trailing separator, sorting *below* the range bound
                    # ``v``), so that exact value is matched and mapped
                    # separately rather than spliced (#93).
                    self._conn.execute(
                        "UPDATE OR REPLACE postref"
                        " SET folder = CASE WHEN folder = ?"
                        "       THEN ? ELSE ? || substr(folder, ?) END"
                        " WHERE folder = ? OR (folder >= ? AND folder < ?)",
                        (v[:-1], str(root), lo, n + 1, v[:-1], v, v_hi),
                    )
                self._conn.commit()
                logger.debug(
                    "search index: folded case-variant rows onto {!r}", lo
                )
            self._root_case_done.add(lo)
        return lo, hi

    # --------------------------------------------------------- node reads

    def query_filenames(
        self,
        root: Path,
        includes: list[str],
        excludes: list[str],
        *,
        or_terms: list[str] | None = None,
        limit: int = 50000,
    ) -> list[NodeRow]:
        """Return indexed descendants of *root* matching the query.

        Mirrors the live :func:`folder_scan.walk_for_search`: only **strict
        descendants** (direct children of *root* are excluded — the PostGrid
        direct filter owns those) whose forward-slash relative path (``rel``)
        contains every casefolded *include* term and no *exclude* term.  SQL
        narrows by a superset ``instr`` prefilter on the casefolded full path
        (cheap, returns only candidates); the **exact** match against ``rel``
        is then applied in Python so semantics equal ``query in rel`` (incl.
        1–2 char terms).  Excludes are applied in Python only — an exclude
        term that happens to sit in *root*'s own prefix must not drop rows.

        *or_terms* is the Danbooru-style ``~`` pool (#3): when non-empty, a
        row must additionally contain at least ONE member in ``rel``.  Pool
        members deliberately stay OUT of the ``instr`` prefilter (an AND
        there would drop legitimate single-alternative matches — the
        prefilter must remain a superset), so a pool-only query scans the
        subtree range and filters strictly in Python: correctness first.

        **LRU semantics (項目54):** ``used_at`` marks rows *served to the UI
        as a seed*, not every row a query ever matched — only the first
        :data:`TOUCH_USED_LIMIT` hits are touched, and the touch happens in
        its own short lock acquisition after the query lock is released, so
        a 50 000-hit one-character query no longer holds the index lock for
        the duration of a 50 000-row ``UPDATE`` (which stalled the GUI's
        :meth:`resolve_postref` for up to ~1.5 s measured).
        """
        if not includes and not or_terms:
            return []
        lo, hi = self._canonicalize_root(root)
        lo_cf_len = len(lo.casefold())
        sql = "SELECT path, name, cpath, is_dir, mtime, size FROM node WHERE path >= ? AND path < ?"
        params: list = [lo, hi]
        # Superset prefilter: include terms (already casefolded) must each
        # appear somewhere in the casefolded full path.  rel ⊂ full path, so
        # this never drops a true match.  ``cpath`` keeps the OS separator
        # while ``rel`` (the Python-side authority below) is forward-slashed,
        # so a term spanning path components ("sub/name") must have its ``/``
        # mapped to ``os.sep`` for the prefilter — on Windows the raw term
        # would match zero ``cpath`` rows and silently kill the instant seed
        # phase (#14).  Windows file names cannot contain ``/``, and on POSIX
        # the replace is a no-op, so this stays a strict superset.
        for inc in includes:
            sql += " AND instr(cpath, ?) > 0"
            params.append(inc.replace("/", os.sep))
        sql += " LIMIT ?"
        params.append(int(limit))
        out: list[NodeRow] = []
        touched: list[str] = []
        with self._lock:
            cur = self._conn.execute(sql, params)
            for path_s, name, cpath, is_dir, mtime, size in cur.fetchall():
                # rel = casefolded path relative to root, forward-slashed to
                # match walk_for_search's ``as_posix()`` form.
                rel = cpath[lo_cf_len:].replace(os.sep, "/")
                if "/" not in rel:
                    continue  # direct child — handled by the direct filter
                if any(exc in rel for exc in excludes):
                    continue
                if not all(inc in rel for inc in includes):
                    continue
                if or_terms and not any(alt in rel for alt in or_terms):
                    continue
                out.append(
                    NodeRow(
                        path=Path(path_s),
                        name=name,
                        is_dir=bool(is_dir),
                        mtime=mtime,
                        size=int(size),
                    )
                )
                touched.append(path_s)
        # Apply the LRU touch OUTSIDE the query's lock hold, and only for the
        # first TOUCH_USED_LIMIT rows actually returned to the UI (項目54).
        # Re-acquiring the lock gives a waiting resolve_postref / upsert_nodes
        # a chance to run in between; the bounded executemany then costs
        # milliseconds instead of ~0.8 s for a 50 000-hit query.
        if touched:
            with self._lock:
                self._touch_many_locked(touched[:TOUCH_USED_LIMIT])
        return out

    # ------------------------------------------------------ postref writes

    def upsert_postrefs(
        self, rows: list[tuple[str, str, str, float]]
    ) -> None:
        """Insert/replace ``(service, post_id, folder, mtime)`` rows.

        Batched like :meth:`upsert_nodes` (one ``executemany`` + a single
        commit honouring bulk-write batching).  Rows with an empty service
        or post_id are skipped — those carry no usable lookup key.
        """
        if not rows:
            return
        now = time.time()
        params = [
            (service, post_id, str(folder), float(mtime), now)
            for (service, post_id, folder, mtime) in rows
            if service and post_id
        ]
        if not params:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO postref"
                "(service, post_id, folder, mtime, used_at)"
                " VALUES (?,?,?,?,?)",
                params,
            )
            self._maybe_commit_locked()

    # ------------------------------------------------------- postref reads

    def resolve_postref(self, service: str, post_id: str) -> Path | None:
        """Return the stored folder for ``(service, post_id)``, or ``None``.

        A pure PK point read.  The returned path is a *candidate* — the caller
        must verify it still exists on disk (the "filesystem is the source of
        truth" contract the index follows).  Kept read-only on purpose: a
        per-resolve ``used_at`` write would leave an open write transaction on
        a hot read path for negligible benefit (postref rows are tiny and the
        shared LRU effectively never evicts them); ``used_at`` set at upsert
        time is sufficient for pruning order.
        """
        if not service or not post_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT folder FROM postref WHERE service=? AND post_id=?",
                (service, post_id),
            ).fetchone()
        return Path(row[0]) if row is not None else None

    # ----------------------------------------------------------- pruning

    def _estimated_bytes_locked(self) -> int:
        """Approximate footprint (monotonic with content, no VACUUM).  Caller holds lock."""
        # CAST AS BLOB so LENGTH counts UTF-8 bytes, not characters.
        n_count, n_text = self._conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({_NODE_SIZE_SQL}), 0) FROM node"
        ).fetchone()
        r_count, r_text = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM("
            "LENGTH(CAST(service AS BLOB))+LENGTH(CAST(post_id AS BLOB))"
            "+LENGTH(CAST(folder AS BLOB))), 0) FROM postref"
        ).fetchone()
        return (
            int(n_text) + int(n_count) * _NODE_ROW_OVERHEAD
            + int(r_text) + int(r_count) * _POSTREF_ROW_OVERHEAD
        )

    def _node_bytes_locked(self) -> int:
        """Approximate footprint of the prunable ``node`` table.  Caller holds lock."""
        n_count, n_text = self._conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({_NODE_SIZE_SQL}), 0) FROM node"
        ).fetchone()
        return int(n_text) + int(n_count) * _NODE_ROW_OVERHEAD

    def estimated_bytes(self) -> int:
        """Approximate footprint (monotonic with content, no VACUUM)."""
        with self._lock:
            return self._estimated_bytes_locked()

    def count(self) -> dict[str, int]:
        with self._lock:
            n = int(self._conn.execute("SELECT COUNT(*) FROM node").fetchone()[0])
            r = int(self._conn.execute("SELECT COUNT(*) FROM postref").fetchone()[0])
            return {"node": n, "postref": r}

    def prune(self, max_bytes: int | None = None) -> int:
        """Evict least-recently-used ``node`` rows to fit the budget.

        Only the file-name ``node`` index is bounded.  ``postref``
        (downloaded-post link map) rows are derived from ``post.md`` files and
        are **never evicted** — they are kept regardless of the capacity limit
        (see the module docstring).  So the budget here
        governs the cheaply regenerable ``node`` index alone.  Eviction order
        is ``used_at`` ascending, where ``used_at`` means "recently served to
        the UI as a seed hit" — a query touches only its first
        :data:`TOUCH_USED_LIMIT` hits (項目54), so rows merely *matched* by a
        huge query keep their old stamp and are evicted first, which is the
        intended bias: the rows the user actually saw stay warm.

        The lock is taken **once per ~10% pass** and released in between (not
        held across the whole sweep), so concurrent GUI-side search queries
        interleave with a long sweep instead of blocking until it finishes.
        Like the base class's :meth:`._sqlite_cache.SqliteCacheBase._prune_lru`,
        the running total is **decremented by each deleted row's measured
        bytes instead of re-measured per pass** — re-running the ``SUM`` over
        the whole ``node`` table every pass turned a large overshoot into a
        dozen-plus full-table scans, each holding the lock (#94).  Rows written
        concurrently between passes are simply caught by the next prune (the
        budget is soft).

        Doubles as the startup corruption probe for
        :func:`._sqlite_cache.open_with_recovery` (passed as its ``verify``).
        The eviction loop only walks ``node``, so before it starts we lightly
        touch **every** owned table (``node`` / ``postref``) via
        :meth:`_probe_schema_tables_locked` — otherwise ``postref``-only
        corruption slips past the probe here and blows up the link resolver
        every session instead of being quarantined + rebuilt.
        """
        deleted = 0
        with self._lock:
            # 全表へ軽く触れる（postref 限定破損も退避シームに乗せる）。
            # budget 短絡や node 空でも必ず走るよう eviction ループより前に置く。
            self._probe_schema_tables_locked()
        budget = self._max_bytes if max_bytes is None else max_bytes
        if budget <= 0:
            return 0
        with self._lock:
            self._flush_touches_locked()
            total = self._node_bytes_locked()
        while total > budget:
            with self._lock:
                n_total = self._conn.execute(
                    "SELECT COUNT(*) FROM node"
                ).fetchone()[0]
                if n_total == 0:
                    break
                slice_n = max(1, n_total // 10)
                rows = self._conn.execute(
                    f"SELECT path, {_NODE_SIZE_SQL} FROM node"
                    " ORDER BY used_at ASC LIMIT ?",
                    (slice_n,),
                ).fetchall()
                if not rows:
                    break
                # 10% スライスは**読み取りのバッチ**であって退避の割り当て
                # ではない: 予算を 1 バイト超えただけで丸ごと消すと、温かい
                # 索引の 10% を毎回捨て、ビューアはそれを NAS の再走査で
                # 払い直す。予算を下回った時点で打ち切る（``_prune_lru`` /
                # ``ThumbDiskCache.prune`` と同じ形）— ここは executemany で
                # 一括削除するので、先に必要な行数までスライスする。
                needed = 0
                running = total
                for _row_path, row_bytes in rows:
                    if running <= budget:
                        break
                    running -= int(row_bytes) + _NODE_ROW_OVERHEAD
                    needed += 1
                victims = rows[:needed]
                if not victims:
                    break
                self._conn.executemany(
                    "DELETE FROM node WHERE path=?", [(r[0],) for r in victims]
                )
                for row_path, row_bytes in victims:
                    self._pending_touch.pop(row_path, None)
                    total -= int(row_bytes) + _NODE_ROW_OVERHEAD
                    deleted += 1
                self._conn.commit()
        if deleted:
            logger.debug("Pruned {} search-index node rows (budget {} B)", deleted, budget)
        return deleted

    def set_max_bytes(self, max_bytes: int) -> None:
        self._max_bytes = int(max_bytes)

    def clear(self) -> int:
        """Delete all node + postref rows.  Returns rows removed."""
        with self._lock:
            c = self.count()
            n = c["node"] + c.get("postref", 0)
            self._pending_touch.clear()
            self._root_case_done.clear()
            self._conn.execute("DELETE FROM node")
            self._conn.execute("DELETE FROM postref")
            self._conn.commit()
            return n


__all__ = ["SearchIndex", "NodeRow", "postref_row"]
