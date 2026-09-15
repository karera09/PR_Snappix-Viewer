"""Shared SQLite plumbing for the viewer's on-disk caches / indexes.

Every persistent store shares the same boilerplate — connection setup
(WAL + ``synchronous=NORMAL``), an ``RLock``, batched LRU touches, a
bulk-write commit mode, byte estimation and the "evict the oldest ~10%
until under budget" prune loop:

* :class:`~snappix.viewer.thumb_meta_cache.ThumbMetaCache`
* :class:`~snappix.viewer.thumb_disk_cache.ThumbDiskCache`
* :class:`~snappix.viewer.folder_preview_cache.FolderPreviewCache`
* :class:`~snappix.viewer.search_index.SearchIndex`

Before this module each class re-implemented that stack (and the
``_MTIME_EPS`` constant) verbatim, so adding a new cache kind — or fixing
one of the shared behaviours — meant copying / syncing four files.  Each
store now only owns its schema DDL, its row ↔ dataclass conversion and any
store-specific behaviour (blob files, enumeration queries, ...).

The connection-level plumbing (construction, ``busy_timeout`` pin, the
durability PRAGMAs, the migration-failure close guard, the ``RLock`` and
``close``) is further split into :class:`SqliteStoreBase`, so the
**non-cache** :class:`~snappix.viewer.user_meta.UserMetaStore` shares it
too — with ``synchronous=FULL`` overridden via ``_PRAGMAS`` and none of the
cache-only machinery (LRU touch / bulk-write / prune) that would endanger
non-regenerable user data.

**Attached stores (one connection, several tables).**  The first three caches
above are all path-keyed point lookups over the *same* portable ``data/``
tree, so they live in ONE database file
(:class:`~snappix.viewer.viewer_cache.ViewerCacheStore`, ``viewer_cache.db``)
instead of three.  The mechanism is the ``share=`` argument below: a store
constructed with ``share=<host>`` adopts the host's connection and ``RLock``
rather than opening its own, and its :meth:`~SqliteStoreBase.close` flushes
but leaves the handle to the host.  Each attached store keeps its OWN table,
its own LRU touch buffer and its own byte budget — sharing the file changes
where the bytes live, never which rows a budget may evict.  ``share=None``
(the default) keeps the historical standalone behaviour, which is what
``SearchIndex`` (a separate, prefix-enumerable index) and unit tests use.

**Stale-row policy (documented here once):** the filesystem is always the
source of truth; a row whose validation key (``mtime`` / ``size``) no longer
matches is treated as a miss.  Whether the stale row is *deleted* on that
miss depends on what the caller's key means:

* :meth:`ThumbDiskCache.lookup` and :meth:`FolderPreviewCache.get` receive a
  key the caller just read from the filesystem (a fresh ``stat`` /
  ``DirEntry``), so a mismatch proves the ROW is outdated → delete eagerly.
* :meth:`ThumbMetaCache.get` / ``get_many`` receive keys from scan *entries*
  that may themselves be outdated (the row can be FRESHER than the query,
  e.g. right after a re-probe) — deleting on mismatch would destroy the
  newer row, so stale rows are left to the LRU ``prune``.
* Batch reads on the GUI thread (``get_many``) never delete, to stay
  allocation/write-light; the next live resolve overwrites the row anyway.

This module is Qt-free (stdlib ``sqlite3`` only) so every store keeps unit
testing without a ``QApplication``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from loguru import logger

# mtime is stored as REAL; round-trips through IEEE-754 double are exact,
# but compare with a tiny epsilon to be robust to filesystem precision.
MTIME_EPS = 1e-6
# sqlite caps host parameters per statement (default 999); chunk IN-lists
# comfortably below that.
QUERY_CHUNK = 800
# Flush batched LRU touches once this many reads have accumulated, to avoid
# a DB write on every cache hit (write amplification on fast scroll).
TOUCH_FLUSH_THRESHOLD = 64
# In bulk-write mode (CacheBuilder warming a whole tree) commit once per this
# many writes instead of once each — fewer commits and less time holding the
# write lock when many workers write concurrently.
BULK_COMMIT_EVERY = 128
# 破損したキャッシュ DB を退避するときに付ける接尾辞。<name>.db → <name>.db.corrupt
# （-wal / -shm も同名規則で <name>.db-wal.corrupt / <name>.db-shm.corrupt へ）。
CORRUPT_SUFFIX = ".corrupt"
# 同一ポータブルフォルダから 2 個目のビューアを起動したときなど、別インスタンスが
# 一時的に書き込みロックを保持していると PRAGMA / 書き込みが "database is locked"
# で失敗し得る。busy_timeout のロック解放待ちで、健全な DB が一過性のロック競合で
# 誤って破損退避（open_with_recovery）へ回るのを防ぐ。なお Python の
# ``sqlite3.connect`` は既定でも ``timeout=5.0``（= busy_timeout 5000ms 相当の
# busy ハンドラ）を張るため、この PRAGMA は待ちを**新設**するものではなく既定値の
# 明示ピン留め。既定より短い値（旧 3000）は待ちをむしろ短縮してしまうので、
# 既定と同じ 5000 に揃えている。
BUSY_TIMEOUT_MS = 5000


def mtime_matches(cached: float, actual: float) -> bool:
    """True iff two stored/observed mtimes match within :data:`MTIME_EPS`."""
    return abs(cached - actual) <= MTIME_EPS


def iter_chunks(items: list, size: int = QUERY_CHUNK):
    """Yield *items* in ``size``-long slices (for IN-list parameter caps)."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SqliteStoreBase:
    """Minimal connection plumbing shared by every sqlite-backed store.

    The lowest common denominator between the four rebuildable caches
    (:class:`SqliteCacheBase`) and the non-regenerable user store
    (:class:`~snappix.viewer.user_meta.UserMetaStore`): connection
    construction with the ``busy_timeout`` pin, the durability PRAGMAs
    (overridable via :attr:`_PRAGMAS` — the caches keep WAL +
    ``synchronous=NORMAL``, the user store overrides to ``FULL``), the
    "close the handle before re-raising a failed migration" guard, an
    ``RLock`` guarding the single shared connection, and :meth:`close`.
    Nothing cache-flavoured (LRU touches, bulk-write commits, prune) lives
    here — those would be actively harmful to a store that must never evict
    or defer user data.

    Subclasses must implement :meth:`_migrate` (schema DDL, guarded by
    ``PRAGMA user_version``).

    Pass ``share=<another store>`` to **attach** to that store's connection
    instead of opening one: the attached instance adopts the host's
    ``sqlite3.Connection`` and ``RLock``, runs only its own :meth:`_migrate`,
    and never closes the handle (the host owns it).  That is how the three
    path-keyed viewer caches live in one file — see the module docstring.
    """

    #: このストアのスキーマが所有するテーブル名（:func:`open_with_recovery` の
    #: ``schema_tables`` に渡す）。``no such table: <name>`` を「ストア自身の
    #: テーブルに限って」破損（異種 sqlite ファイル）と分類するために使う。
    SCHEMA_TABLES: tuple[str, ...] = ()

    #: ``PRAGMA`` name/value pairs applied at connect time, in order, after
    #: the ``busy_timeout`` pin.  Subclasses override to change durability
    #: (e.g. ``synchronous=FULL`` for non-regenerable user data).
    _PRAGMAS: tuple[tuple[str, str], ...] = (
        ("journal_mode", "WAL"),
        ("synchronous", "NORMAL"),
    )

    def __init__(
        self,
        db_path: Path,
        *,
        check_same_thread: bool = False,
        share: "SqliteStoreBase | None" = None,
    ) -> None:
        if share is not None:
            # Attached mode: the host already connected, pinned the PRAGMAs
            # and owns the handle.  Adopt its connection + lock so every
            # table in the file is serialised by ONE lock, then run only this
            # store's schema DDL.  A failure here is NOT ours to clean up —
            # the host's constructor closes its own handle before re-raising
            # (:class:`~snappix.viewer.viewer_cache.ViewerCacheStore`).
            self._owns_connection = False
            self._lock = share._lock
            self._conn = share._conn
            self._migrate()
            return
        self._owns_connection = True
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=check_same_thread,
        )
        # DB が破損していると、接続自体は遅延成立で通っても直後の PRAGMA / 移行で
        # sqlite3.DatabaseError（"file is not a database" 等）が飛ぶ。ここで確実に
        # 接続を閉じてから送出しないと、Windows では掴んだままのファイルハンドルが
        # 破損 DB のリネーム退避（:func:`open_with_recovery`）を妨げ、退避しない
        # ストア（user_meta）でも ``None`` 劣化後にハンドルが GC までリークする。
        try:
            # busy_timeout FIRST — but as an explicit pin, not a rescue:
            # sqlite3.connect's default timeout=5.0 already installs a 5000 ms
            # busy handler, so the WAL journal_mode switch below (which needs a
            # write lock) would wait even without this PRAGMA.  Setting the
            # explicit value first just keeps it in force for everything that
            # follows instead of relying on the implicit connect default.
            self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            for name, value in self._PRAGMAS:
                self._conn.execute(f"PRAGMA {name}={value}")
            self._migrate()
        except BaseException:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover (defensive)
                pass
            raise

    # ------------------------------------------------------------- schema

    def _migrate(self) -> None:
        raise NotImplementedError

    def _probe_schema_tables_locked(self) -> None:
        """:attr:`SCHEMA_TABLES` の全表へ 1 行 SELECT で軽く触れる（破損プローブ）。

        起動時プローブ（``open_with_recovery`` の ``verify``）は通常その store の
        ``prune`` が兼ねるが、``prune`` が LRU 対象の 1 表しか走査しない store
        （:class:`~snappix.viewer.search_index.SearchIndex` の ``node`` —
        ``postref`` は非退避なので走査されない）では、走査されない表に限った
        破損（テーブル欠落・内部ページ破損）が起動時プローブを素通りし、後段の
        検索ワーカーで毎セッション例外化する。各表 1 行 SELECT で b-tree のルート
        （+ 先頭リーフ）に触れ、テーブル欠落は ``no such table``、ヘッダ / ルート
        ページ破損は ``malformed`` 等を送出させて、:func:`open_with_recovery` の
        退避+再作成シームへ乗せる。``LIMIT 1`` に絞りコストは軽量（全表走査は
        しない）。``SCHEMA_TABLES`` が空の store では no-op。呼び出し側はロックを
        保持していること。

        **``SELECT *`` であることが本質**（項目#148 の隣、項目#52）: ``SELECT 1``
        は列値を一切参照しないため、sqlite は ``used_at`` などの索引だけで
        被覆スキャンして答えられてしまい（実測・当時の ``post`` 表: ``SCAN post
        USING COVERING INDEX idx_post_used_at``）、**表本体の b-tree ページに一度も触れない**。
        内部ページだけが壊れた DB がプローブを素通りし、後段の実クエリで
        ``malformed`` になる。全列を要求すれば必ず表本体を読む。
        """
        for table in self.SCHEMA_TABLES:
            self._conn.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()

    # --------------------------------------------------------------- close

    def _flush_before_close_locked(self) -> None:
        """Subclass hook: commit anything deferred.  Caller holds the lock.

        Runs for attached stores too (they must not leave their rows in the
        host's open transaction), which is why it is separate from the
        ownership-gated handle close below.
        """

    def close(self) -> None:
        """Flush, then close the connection **if this store owns it**.

        The flush is best-effort and guarded separately from the close: a
        failing flush (disk full, media pulled, DB turned read-only) must not
        skip ``conn.close()`` — a leaked sqlite handle keeps the ``-wal`` /
        ``-shm`` files locked on Windows, which blocks both a second instance
        from the same portable folder and the quarantine rename in
        :func:`open_with_recovery`.  This mirrors the "never leave a handle
        behind" rule :meth:`__init__` and ``_open_and_verify`` already follow.

        An **attached** store (``share=``) flushes and returns: the handle
        belongs to its host, and closing it here would take the file's other
        tables down with it.  Closing the host closes the file once.

        **稼働していたストアを GUI スレッドから直接閉じないこと（issue
        #132）**: WAL の最後の接続を閉じるとチェックポイント（``-wal`` の本体
        への反映 + ``-wal`` / ``-shm`` の削除）が走るため、DB が到達不能な SMB
        共有上にあるとここは OS の I/O タイムアウトぶん — 実測で数十〜数百秒
        — ブロックする。``busy_timeout`` は sqlite のロック待ちにしか効かず、
        この待ちは止められない（open 直後の失敗経路 —
        :func:`open_with_recovery` の ``_open_and_verify`` — は書き込みが
        1 つも無くチェックポイントも無いので対象外）。
        ``ViewerWindow.closeEvent`` は
        :func:`snappix.common.teardown.run_tasks_before_deadline` の予算付き
        ワーカーからここを呼び、予算超過時は**放棄して窓を閉じる**。

        放棄しても安全なのは WAL の性質による: コミット済みトランザクションは
        ``-wal`` に残り、次回 open 時に自動で回収される。失われるのはチェック
        ポイント（＝起動が一度だけ少し重くなる）だけで、キャッシュは
        そもそも再生成可能、``user_meta.db`` は ``synchronous=FULL`` + 書き込み
        ごと commit なのでユーザーデータ自体は落ちない。
        """
        with self._lock:
            try:
                self._flush_before_close_locked()
            except sqlite3.Error:  # pragma: no cover (defensive)
                pass
            finally:
                if self._owns_connection:
                    try:
                        self._conn.close()
                    except sqlite3.Error:  # pragma: no cover (defensive)
                        pass


class SqliteCacheBase(SqliteStoreBase):
    """Connection + lock + LRU-touch + bulk-write plumbing for one cache.

    Extends :class:`SqliteStoreBase` (connection / PRAGMAs / lock / close
    guard) with the machinery that only makes sense for **rebuildable**
    cache data: batched LRU touches, the bulk-write commit mode and the
    byte-budget prune loop.  Subclasses must implement :meth:`_migrate`
    (schema DDL, guarded by ``PRAGMA user_version``) and may declare:

    * ``_TOUCH_SQL`` — ``"UPDATE <table> SET <used_col>=? WHERE <key>=?"``
      to enable the batched LRU touch buffer (``None`` disables it).
    * ``_TABLE`` / ``_KEY_COL`` / ``_ORDER_COL`` / ``_SIZE_SQL`` /
      ``_ROW_OVERHEAD`` — to enable the generic byte estimate
      (:meth:`_estimated_bytes_locked`) and the LRU prune loop
      (:meth:`_prune_lru`).

    Every helper suffixed ``_locked`` assumes the caller holds ``self._lock``
    (single-threaded stores may simply never contend on it — an uncontended
    ``RLock`` costs nanoseconds).
    """

    #: ``"UPDATE <table> SET <used_col>=? WHERE <key_col>=?"`` or ``None``.
    _TOUCH_SQL: str | None = None
    #: Generic-prune declarations (all-or-nothing; see class docstring).
    _TABLE: str | None = None
    _KEY_COL: str = "path"
    _ORDER_COL: str = "used_at"
    # CAST AS BLOB so LENGTH counts UTF-8 bytes, not characters (a plain
    # LENGTH(text) undercounts non-ASCII paths/titles by up to 3x).
    _SIZE_SQL: str = "LENGTH(CAST(path AS BLOB))"
    #: Approximate fixed bytes charged per row (integer/real columns, index
    #: entries, sqlite row overhead) on top of the measured text lengths.
    _ROW_OVERHEAD: int = 96

    def __init__(
        self,
        db_path: Path,
        *,
        check_same_thread: bool = False,
        share: SqliteStoreBase | None = None,
    ) -> None:
        self._pending_touch: dict[str, float] = {}
        # Bulk-write mode (set by CacheBuilder): defer commits to amortise
        # them across many writes.  ``_bulk_pending`` counts uncommitted ones.
        #
        # The flag is per STORE, the commit is per CONNECTION: two caches
        # attached to one file each keep their own counter, so a commit from
        # either also durably lands the other's deferred rows.  That only ever
        # makes durability *earlier* than asked for, which is the safe
        # direction for rebuildable data.
        self._bulk_writes = False
        self._bulk_pending = 0
        super().__init__(
            db_path, check_same_thread=check_same_thread, share=share,
        )

    # -------------------------------------------------------- bulk writes

    def set_bulk_writes(self, enabled: bool) -> None:
        """Batch commits during a bulk warm (CacheBuilder); flush on disable.

        While enabled, :meth:`_maybe_commit_locked` commits only every
        :data:`BULK_COMMIT_EVERY` writes rather than once each.  Disabling
        flushes any pending writes immediately.  Safe on a connection shared
        with live readers: uncommitted rows are still visible to subsequent
        reads on the same connection — only crash-durability is deferred,
        which is acceptable for rebuildable cache data.
        """
        with self._lock:
            self._bulk_writes = bool(enabled)
            if not enabled and self._bulk_pending:
                self._conn.commit()
                self._bulk_pending = 0

    def _maybe_commit_locked(self) -> None:
        """Commit honouring bulk-write batching.  Caller holds the lock."""
        if self._bulk_writes:
            self._bulk_pending += 1
            if self._bulk_pending >= BULK_COMMIT_EVERY:
                self._conn.commit()
                self._bulk_pending = 0
        else:
            self._conn.commit()

    # --------------------------------------------------------- LRU touches

    def _touch_locked(self, key: str) -> None:
        """Record an LRU touch; batch-flush to avoid a write per cache hit."""
        self._pending_touch[key] = time.time()
        if len(self._pending_touch) >= TOUCH_FLUSH_THRESHOLD:
            self._flush_touches_locked()

    def _touch_many_locked(self, keys: list[str]) -> None:
        """Batch variant of :meth:`_touch_locked` (one timestamp for all)."""
        if not keys:
            return
        now = time.time()
        for k in keys:
            self._pending_touch[k] = now
        if len(self._pending_touch) >= TOUCH_FLUSH_THRESHOLD:
            self._flush_touches_locked()

    def _flush_touches_locked(self) -> None:
        """Write out the batched LRU touches.

        The buffer is cleared **whatever happens**: touches are pure eviction
        hints, so losing a batch costs nothing, whereas keeping a failed batch
        around costs everything.  A full disk makes the ``commit`` raise, and
        an un-cleared buffer is already at the flush threshold — the very next
        cache hit re-enters this method, raises again, and every read through
        the store keeps failing until the disk frees up.  The buffer would
        also grow to one entry per distinct key touched in the session.
        """
        if not self._pending_touch or self._TOUCH_SQL is None:
            return
        try:
            self._conn.executemany(
                self._TOUCH_SQL,
                [(ts, k) for k, ts in self._pending_touch.items()],
            )
            # Under bulk-write mode the touch flush rides the next batched
            # commit — same durability trade as the deferred writes themselves.
            # It must count as a pending write, or a touch-only batch (no
            # subsequent write) would slip past the disable/flush/close commit
            # guards and be rolled back when the connection closes.
            if self._bulk_writes:
                self._bulk_pending += 1
            else:
                self._conn.commit()
        finally:
            self._pending_touch.clear()

    # ---------------------------------------------------- estimate / prune

    def _estimated_bytes_locked(self) -> int:
        """Approximate on-disk footprint of the declared ``_TABLE``.

        Uses ``SUM(_SIZE_SQL) + count * _ROW_OVERHEAD`` rather than the
        sqlite file size: it's monotonic with row count (so the prune loop
        terminates without a VACUUM) and good enough for a soft budget.
        """
        count, text_bytes = self._conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM({self._SIZE_SQL}), 0)"
            f" FROM {self._TABLE}"
        ).fetchone()
        return int(text_bytes) + int(count) * self._ROW_OVERHEAD

    def _prune_lru(self, max_bytes: int) -> int:
        """Evict oldest rows (by ``_ORDER_COL``) until under ``max_bytes``.

        The shared "read the oldest ~10% per pass, delete until the budget is
        met" loop used by the row-only stores — the slice is the read batch,
        not the eviction quota, so a small overshoot evicts a few rows rather
        than a tenth of the table.  Returns the number of rows deleted.
        **Manages the
        lock itself**, taking it once per pass and releasing it in between so
        GUI-side cache reads interleave with a long sweep instead of blocking
        until the whole prune finishes (the sweep runs on the post-build
        daemon thread while the GUI keeps querying).  Callers must NOT hold
        the lock.  Pending touches are flushed up front so the LRU order is
        current; the running ``total`` is decremented instead of re-measured
        per pass (rows written concurrently between passes are simply caught
        by the next prune — the budget is soft).  Stores whose rows have
        side-effects (blob files) keep their own prune.

        Doubles as the startup corruption probe (``open_with_recovery`` takes
        ``prune`` as its ``verify``), so :meth:`_probe_schema_tables_locked`
        runs **first** — before the budget short-circuit below (項目#52).  The
        sweep's own ``_estimated_bytes_locked`` can be answered from a covering
        index (``ThumbMetaCache``: ``SELECT COUNT(*), SUM(LENGTH(path)) FROM
        aspect`` → ``SCAN aspect USING COVERING INDEX``), so without the probe
        an internally corrupt table opens "clean" every session and only blows
        up later in ``get`` (or silently misses in ``get_many``).
        """
        with self._lock:
            self._probe_schema_tables_locked()
        if max_bytes <= 0:
            return 0
        deleted = 0
        with self._lock:
            self._flush_touches_locked()
            total = self._estimated_bytes_locked()
        while total > max_bytes:
            with self._lock:
                n = self._conn.execute(
                    f"SELECT COUNT(*) FROM {self._TABLE}"
                ).fetchone()[0]
                if n == 0:
                    break
                to_delete = max(1, n // 10)
                rows = self._conn.execute(
                    f"SELECT {self._KEY_COL}, {self._SIZE_SQL} FROM {self._TABLE}"
                    f" ORDER BY {self._ORDER_COL} ASC LIMIT ?",
                    (to_delete,),
                ).fetchall()
                for row_key, row_bytes in rows:
                    if total <= max_bytes:
                        # Stop inside the slice, exactly as ThumbDiskCache's
                        # own prune does: the 10% slice is the read batch, not
                        # the eviction quota.  Deleting the whole slice for a
                        # one-byte overshoot throws away ~10% of a warm cache
                        # every time the budget is reached, and the viewer pays
                        # it back with NAS re-reads.
                        break
                    self._conn.execute(
                        f"DELETE FROM {self._TABLE} WHERE {self._KEY_COL}=?",
                        (row_key,),
                    )
                    self._pending_touch.pop(row_key, None)
                    total -= int(row_bytes) + self._ROW_OVERHEAD
                    deleted += 1
                self._conn.commit()
        return deleted

    # ------------------------------------------------------ flush / close

    def _flush_pending_locked(self) -> None:
        """Flush deferred bulk-write commits.  Caller holds the lock."""
        if self._bulk_pending:
            self._conn.commit()
            self._bulk_pending = 0

    def flush(self) -> None:
        """Flush batched LRU touches and any deferred bulk commits."""
        with self._lock:
            self._flush_touches_locked()
            self._flush_pending_locked()

    def _flush_before_close_locked(self) -> None:
        """Commit batched LRU touches + bulk-deferred rows before the close.

        Fills :meth:`SqliteStoreBase._flush_before_close_locked`: close would
        otherwise roll back the open transaction and drop them.  Runs for
        attached stores too, whose ``close`` flushes without touching the
        host's handle.

        呼び出しスレッドの制約は :meth:`SqliteStoreBase.close` と同じ（#132）
        — 到達不能な共有ではフラッシュも close も長時間ブロックしうるので、
        GUI スレッドから直接呼ばないこと。
        """
        self._flush_touches_locked()
        self._flush_pending_locked()


# ----------------------------------------------------- 破損 DB のリカバリ


# 実 sqlite の "no such table: <name>"（まれに "main.<name>" と修飾される）から
# テーブル名を取り出す。破損分類（下記）で使う。
_NO_SUCH_TABLE_RE = re.compile(r"no such table:\s*(?:[\w]+\.)?([\w]+)")

#: 「このファイルは壊れている / sqlite ですらない」を確定させる sqlite の
#: プライマリ結果コード（下位 8 bit）: SQLITE_CORRUPT / SQLITE_FORMAT /
#: SQLITE_NOTADB。``sqlite3`` 例外の ``sqlite_errorcode`` は Python 3.11+。
_CORRUPT_SQLITE_CODES = frozenset({11, 24, 26})


def _is_corrupt_db_error(
    exc: sqlite3.DatabaseError, schema_tables: tuple[str, ...] = (),
) -> bool:
    """*exc* が「破損 / 非 sqlite ファイル」を示すなら ``True``（退避対象）。

    ``sqlite3.DatabaseError`` のサブクラス ``OperationalError`` には破損以外の
    **一過性**失敗（``database is locked`` / ``attempt to write a readonly
    database`` / ``unable to open database file``）が含まれる。これらは健全な DB
    でも起こり得るため退避してはならず、``False`` を返して呼び出し側の ``None``
    劣化（従来挙動）へ逃がす。

    判定:

    * ``exc.sqlite_errorcode``（Python 3.11+）の下位 8 bit が
      ``SQLITE_CORRUPT``(11) / ``SQLITE_FORMAT``(24) / ``SQLITE_NOTADB``(26)
      → 破損（``True``）。これが第一判定なのは、コードが sqlite の C API 定数
      であり、メッセージ文面（sqlite のバージョン / ビルドで変わる）よりも、
      Python 例外クラスの割り当て（CPython の実装都合で変わる）よりも安定して
      いるため。誤分類の向きが安全側でない（破損を一過性と読むと毎起動 ``None``
      劣化が恒久化する）ので、判定は最も安定した材料から始める。
    * 破損メッセージ（``malformed`` / ``not a database`` / ``file is encrypted``
      / ``unsupported file format`` / ``could not decode``）を含む → 破損
      （``True``）。"file is not a database" は CPython では ``DatabaseError``
      直下だが、将来 ``OperationalError`` に変わってもメッセージ判定で確実に破損と
      分類する。``could not decode`` は bit 化けで TEXT 列が不正 UTF-8 になった
      破損で、実 sqlite が ``OperationalError: Could not decode to UTF-8 column
      ...`` として投げる（型だけでは一過性と誤分類され毎起動 ``None`` 劣化が恒久化
      する — 項目1 の取りこぼし）。
    * ``no such table: <name>`` で ``<name>`` が *schema_tables*（= ストア自身が
      所有するテーブル。:attr:`SqliteCacheBase.SCHEMA_TABLES`）に含まれる → 破損
      （``True``）。「``user_version`` はマイグレーション済みを示すのに対象テーブル
      が無い」異種 sqlite ファイルは、実 sqlite では ``OperationalError: no such
      table: <name>`` として表面化するため（項目1 の中核ケース）、型だけでは一過性
      と誤分類され毎起動 ``None`` 劣化が恒久化してしまう。ストア自身のテーブル名に
      限定するのは、将来 ``verify`` が別 DB / オプショナルなテーブルへ触れても
      巻き込み退避しないため。
    * それ以外は、``OperationalError`` なら一過性（``False``）、非
      ``OperationalError`` の ``DatabaseError``（``IntegrityError`` 等）は破損とみなす
      （``True``）。
    """
    if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in _CORRUPT_SQLITE_CODES:
        return True
    msg = str(exc).lower()
    if (
        "malformed" in msg
        or "not a database" in msg
        or "file is encrypted" in msg
        or "unsupported file format" in msg
        or "could not decode" in msg
    ):
        return True
    m = _NO_SUCH_TABLE_RE.search(msg)
    if m is not None and m.group(1) in {t.lower() for t in schema_tables}:
        return True
    return not isinstance(exc, sqlite3.OperationalError)


def _quarantine_corrupt_db(db_path: Path) -> bool:
    """破損した DB ファイル（と ``-journal`` / ``-wal`` / ``-shm``）を退避する。

    メイン DB と付随するサイドカーを ``<name>.db.corrupt`` 等へ ``os.replace``
    でリネーム移動する（退避先が既にあれば置換 = 残骸の無限増殖を防ぐ）。全て
    再生成可能な加速データなので、退避 = 実質的な破棄で構わない。

    サイドカーに ``-journal`` を含めるのは、sqlite が「DB とジャーナル / WAL は
    必ずセット」という契約を持つため。WAL へ切り替える前に落ちた異種 sqlite
    ファイルを退避すると ``-journal``（ホットジャーナル）がその場に残り、直後に
    作られる新しい空 DB の隣に**別 DB の**ロールバック対象が居ることになる。

    成否は **メイン DB を退けたか** で決まる。メインさえ退ければ再作成は成立
    するので、サイドカーのリネーム失敗（アンチウイルスの一時ロック等）は
    warning に落として成功として扱う。ここで ``False`` を返すと、呼び出し側は
    「退避は起きなかった」として例外を再送出するのに、実際のフォルダは ``.db``
    だけ消えた半端な状態になる。メイン DB のリネーム自体に失敗した場合（他
    プロセスが掴んでいる、読み取り専用ボリューム等）は誤って生きた DB を
    巻き込まないよう ``False`` を返し、呼び出し側は従来どおり ``None`` 劣化に
    委ねる。
    """
    corrupt = db_path.with_name(db_path.name + CORRUPT_SUFFIX)
    try:
        # メイン DB が無ければ退避しようがない（そのまま再作成に委ねる）。
        if db_path.exists():
            os.replace(db_path, corrupt)
    except OSError:
        # 静かに諦める（例外は上へ漏らさない）。呼び出し側で None 劣化する。
        return False
    # ジャーナル / WAL / SHM も同名規則で一緒に退避（残っていれば）。
    for suffix in ("-journal", "-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        try:
            if side.exists():
                os.replace(side, side.with_name(side.name + CORRUPT_SUFFIX))
        except OSError as exc:
            logger.warning("退避できないサイドカーが残りました {}: {}", side, exc)
    logger.warning("破損キャッシュ {} を {} へ退避しました", db_path, corrupt)
    return True


def open_with_recovery(
    factory, db_path: Path, *, verify=None, schema_tables: tuple[str, ...] = (),
    on_quarantined=None,
):
    """``factory()`` でキャッシュを開き、破損 DB は 1 回だけ退避+再作成する。

    ``factory`` は引数なしで対象のキャッシュ（:class:`SqliteCacheBase` 派生）を
    構築するコーラブル。``db_path`` は ``factory`` が開く DB ファイルのパス。
    ``schema_tables`` はストアが所有するテーブル名（各ストアの
    :attr:`~SqliteCacheBase.SCHEMA_TABLES`）で、``no such table: <name>`` を
    「ストア自身のテーブルに限って」破損（= 異種 sqlite ファイル）と分類する
    ために :func:`_is_corrupt_db_error` へ渡す。

    ``verify`` は省略可の「初回検証フェーズ」コーラブルで、``factory()`` が返した
    キャッシュを受け取り、起動時 ``prune``（LRU 予算調整 = 全表走査）や
    ``PRAGMA quick_check`` 相当の**全ページ走査**を行う。接続直後の PRAGMA /
    スキーマ移行は page1（ヘッダ + sqlite_master + user_version）しか読まないため、
    page2 以降だけが破損した DB や「user_version>=1 だが対象テーブルが無い」異種
    sqlite ファイルは ``factory`` を素通りし、``verify`` の全表走査で初めて
    ``sqlite3.DatabaseError`` として表面化する。この検証まで退避の管轄に入れる
    ことで、破損が prune 側で発覚するケース（issue #40 の残穴）でも退避+再作成が
    走る。

    **破損系の** ``sqlite3.DatabaseError`` （``factory`` 内の接続直後の PRAGMA /
    スキーマ移行、および ``verify`` の全表走査のいずれで送出されても）を検出した
    ときに **限り**、:func:`_quarantine_corrupt_db` で破損ファイルを退避してから
    ``factory()`` + ``verify`` を **1 回だけ** 再試行する。「破損系」の判定は
    :func:`_is_corrupt_db_error`（メッセージが ``malformed`` / ``not a database``
    等、ストア自身のテーブルへの ``no such table``、または非 ``OperationalError``
    の ``DatabaseError``）に委ねる。退避に失敗した
    とき、および再作成もなお失敗したときは例外をそのまま送出し、呼び出し側
    （``main_window._open_cache``）の best-effort 経路で ``None`` 劣化させる。

    ``verify`` が例外を投げたときは、退避（リネーム）の前に必ず開いたキャッシュを
    ``close()`` する — Windows では掴んだままのファイルハンドルが破損 DB の
    リネーム退避を妨げ、さらに ``None`` 劣化してもハンドルがセッション中リーク
    し続けるため（手動削除も阻害）。

    **一過性の** ``sqlite3.OperationalError`` （``database is locked`` / 読み取り
    専用ボリューム / ``unable to open database file`` 等）はリカバリ対象にしない。
    これらは ``DatabaseError`` のサブクラスであって ``OSError`` ではない点に注意
    （旧 docstring は「ロック中は OSError 系」と誤記していた）。同一ポータブル
    フォルダから 2 個目のビューアを起動して 1 個目が書き込み中、といった競合では
    健全な DB がこの経路に来るため、退避せずそのまま送出して ``None`` 劣化させる
    （接続時の ``busy_timeout`` で遭遇自体も減らす）。純粋な ``OSError`` 系も同様に
    素通しする。

    ``on_quarantined`` は省略可の引数なしコーラブルで、退避が**実際に成功した
    直後**（再作成の前）に 1 度だけ呼ばれる。索引 DB の外側に本体を持つストア
    （``ThumbDiskCache`` の WebP blob ツリー）で、退避によって全件が孤児になった
    サイドカーを掃除するためのシーム — 索引が空になった以上、それらは
    ``SUM(bytes)`` の予算にも lookup にも二度と現れず、掃除の機会が無くなる。
    Qt 非依存を保つため、ここは呼ぶだけで中身を知らない。コーラブルの例外は
    握って warning に落とす（退避と再作成を止めない）。
    """
    def _open_and_verify():
        cache = factory()
        if verify is not None:
            try:
                verify(cache)
            except BaseException:
                # 初回検証（prune 等）で破損が発覚 → 退避の前にハンドルを閉じ、
                # None 劣化時のハンドルリークも防ぐ。close 自体の失敗は握り潰す
                # （SqliteCacheBase.close は既に defensive）。
                cache.close()
                raise
        return cache

    try:
        return _open_and_verify()
    except sqlite3.DatabaseError as exc:
        # 破損系のみ退避対象。ロック中・読み取り専用等の一過性
        # OperationalError は健全な DB なので退避せずそのまま送出（None 劣化）。
        if not _is_corrupt_db_error(exc, schema_tables):
            raise
        if not _quarantine_corrupt_db(db_path):
            raise
        if on_quarantined is not None:
            try:
                on_quarantined()
            except Exception as cb_exc:  # pragma: no cover (best-effort)
                logger.warning("退避後のサイドカー掃除に失敗しました: {}", cb_exc)
        # 退避済み。今度は空の DB を新規作成する経路になる（失敗すれば送出。
        # その際も verify 失敗なら _open_and_verify が close 済み）。
        return _open_and_verify()


__all__ = [
    "BULK_COMMIT_EVERY",
    "CORRUPT_SUFFIX",
    "MTIME_EPS",
    "QUERY_CHUNK",
    "SqliteCacheBase",
    "SqliteStoreBase",
    "TOUCH_FLUSH_THRESHOLD",
    "iter_chunks",
    "mtime_matches",
    "open_with_recovery",
]
