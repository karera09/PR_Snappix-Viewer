"""Off-thread scanning of a folder's immediate children (both viewer panes).

`scan_children()` is deliberately shallow — ``os.scandir`` + stat only, no
``post.md`` reads — so even on network drives the *initial* grid appears
quickly (comparable to Windows Explorer).  Per-folder ``post.md`` metadata
is then read in parallel via a follow-up metadata pass, and individual
entries are enriched in batches as results arrive.

Both panes share a single :class:`ChildrenScanner` driven by a
``with_metadata`` flag:

* Left pane (PostGrid) — ``with_metadata=True``: full two-stage pipeline
  with progressive ``post.md`` metadata batches.
* Right pane (FileListView) — ``with_metadata=False``: shallow scan only,
  pre-sorted into Explorer-style "directories first, ``post.md`` leading
  the files".  Metadata pass is skipped entirely.

Each scan request is one :class:`~.cancel_token.ScanSession` minted by the
scanner's :class:`~.cancel_token.SessionOwner` (#35): the session's
*generation* is stamped on every emission — when the user navigates again
before a scan finishes, the in-flight task still completes and emits its
result, but the consumer compares the emitted generation against
:meth:`ChildrenScanner.latest_generation` (or its stored request
generation) and discards stale payloads.  This avoids having to cancel the
blocking filesystem call (which would be racy and platform-specific) while
keeping the UI coherent.  The same session doubles as the cooperative
cancel flag, polled inside the stat loop and the metadata pass so a fresh
``request()`` short-circuits the previous task before it floods the NAS
with redundant scandir / preview calls; a task still queued when its
session is cancelled never starts at all
(:class:`~.cancel_token.SessionRunnable`).

The recursive / tag / vector search scanners live in :mod:`scan_search`;
:mod:`scan_worker` re-exports both for backwards-compatible imports.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import QObject, QThreadPool, Signal

if TYPE_CHECKING:  # import-time-free annotations for injected collaborators
    from .folder_preview_cache import FolderPreviewCache
    from .search_index import SearchIndex

from ._fanout import SKIPPED, map_unordered
from .cancel_token import ScanSession, SessionOwner, SessionRunnable
from .folder_scan import (
    FolderEntry,
    _preview_to_read_tuple,
    apply_preview,
    read_folder_preview_cached,
    scan_children_checked,
    sort_children_dir_first,
)
from .gil_pacing import GilPacer
from .perf import measure, recorder
from .search_index import NodeRow, postref_row

# How many post.md reads to run concurrently.  Kept modest (6) because
# the thumbnail loader also hits the NAS in parallel — stacking both at
# high fan-out saturates the SMB client's credit window, which slows
# every request (even tiny UI round-trips like file-dialogs) to a crawl
# and makes the main thread feel frozen.  6 metadata workers plus ~4
# thumbnail workers is enough to amortise NAS latency without drowning
# the client.
_METADATA_PARALLELISM = 6
# The per-folder preview read (scandir + post.md parse) is a pure-Python
# burst once the OS / SMB cache is warm; 6 such workers starve the GUI
# thread of the GIL — the "opening an unscanned folder stutters the whole
# app" symptom (see gil_pacing.py).  Shared pacer, per-thread counters.
_META_PACER = GilPacer()


def _read_folder_preview_paced(folder, mtime, cache, should_cancel=None):
    out = read_folder_preview_cached(
        folder, mtime, cache, should_cancel=should_cancel
    )
    _META_PACER.tick()
    return out
# Emit progressive metadata batches in chunks so the grid updates while
# the scan is still in flight.  Smaller batches = more responsive UI,
# at the cost of more signal round-trips — cheap here since each delivery
# is just a list of dataclasses.
_METADATA_BATCH_SIZE = 8


class _ScanSignals(QObject):
    scan_done = Signal(int, list)         # (generation, list[FolderEntry])
    scan_failed = Signal(int, str)        # (generation, error message)
    # (generation, 分類できなかった子の件数) — 一覧は立つが穴がある。
    # ``scan_done`` の**直前**に出る（``walk_incomplete`` と同じ規約）。
    scan_partial = Signal(int, int)
    metadata_batch = Signal(int, list)    # (generation, list[FolderEntry])
    metadata_done = Signal(int)           # (generation,) — end of all work


class _ScanChildrenTask(SessionRunnable):
    """Run :func:`scan_children` on a worker thread.

    When ``with_metadata`` is true, a 6-way parallel ``post.md`` pass runs
    after the shallow scan, emitting progressive ``metadata_batch`` signals
    until exhausted; ``metadata_done`` is then emitted to mark the end of
    all work.  When ``with_metadata`` is false, the shallow result is
    pre-sorted via :func:`sort_children_dir_first` (so the right pane sees
    the Explorer-style "dirs first, post.md leading files" order without
    needing its own sort UI) and ``metadata_done`` fires immediately after
    ``scan_done``.

    ``session`` fuses the generation stamped on every emission with the
    cooperative cancel flag the task polls (項目#35) — the base class's
    ``run()`` already skips a task whose session was cancelled while it sat
    queued (a queued task cannot be pulled back out of a ``QThreadPool``,
    and ``scan_children`` only polls the token *after* ``os.scandir`` +
    ``list(it)`` — the part that dominates on a NAS; holding the ↓ key over
    a folder list would otherwise make every skipped folder pay a full
    enumeration ahead of the one the user actually landed on).
    """

    def __init__(
        self,
        session: ScanSession,
        root: Path,
        signals: _ScanSignals,
        *,
        with_metadata: bool,
        metadata_parallelism: int = _METADATA_PARALLELISM,
        folder_cache: FolderPreviewCache | None = None,
        search_index: SearchIndex | None = None,
    ) -> None:
        super().__init__(session)
        self.generation = session.generation
        self.root = root
        self.signals = signals
        # 歴史的な別名: 本体は ``session`` そのもの（ScanSession は旧
        # CancelToken 互換の cancel()/is_cancelled() を持つ）。
        self.cancel = session
        self.with_metadata = with_metadata
        self.metadata_parallelism = max(1, int(metadata_parallelism))
        # Optional FolderPreviewCache shared with the loader pool: a cache hit
        # skips this folder's scandir + post.md read on revisit.
        self.folder_cache = folder_cache
        # Optional SearchIndex: the shallow scan already has every direct
        # child's (path, name, is_dir, mtime, size), so we warm the file-name
        # index for free as the user browses.
        self.search_index = search_index
        self.setAutoDelete(True)

    def _warm_node_index(self, entries: list[FolderEntry]) -> None:
        """Upsert the shallow scan's children into the file-name index.

        Free data (the entries already carry path/mtime/size/is_dir) but NOT
        free work: on a folder with tens of thousands of children the NodeRow
        build + sqlite ``executemany`` + commit (fsync) costs tens to hundreds
        of ms.  Always call this **after** ``scan_done`` has been emitted so
        the index write never sits in front of the grid's first paint — the
        whole point of the shallow scan is Explorer-grade initial response
        (#89).  Same "display first, index warm second" ordering as
        ``_RecursiveSearchTask``.  Best-effort: index failures never break the
        scan.

        Skipped when the token has been cancelled: the caller navigated away
        while ``scan_done`` was still crossing threads, and the write is pure
        cost from there on — tens to hundreds of ms holding ``SearchIndex``'s
        lock, which the GUI thread takes synchronously (``resolve_postref``
        from MarkdownView's link classification).  The next visit re-warms
        the same rows for free.
        """
        if self.search_index is None or not entries or self.cancel.is_cancelled():
            return
        try:
            self.search_index.upsert_nodes(
                [
                    NodeRow(e.path, e.path.name, e.is_dir, e.mtime, e.size)
                    for e in entries
                ]
            )
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("search index upsert_nodes failed: {}", exc)

    def _run(self) -> None:
        # NOTE: the queued-while-cancelled early return lives in
        # ``SessionRunnable.run`` (項目#35) — emitting nothing is the
        # existing cancellation contract (see the check after the scan).
        try:
            with measure("scan_children", str(self.root)):
                scan = scan_children_checked(
                    self.root, should_cancel=self.cancel.is_cancelled
                )
            entries, reason = scan.entries, scan.failure
        except Exception as exc:
            # A scan that BLOWS UP (offline NAS, revoked permission, unplugged
            # drive) is not the same as an empty folder — report it as a
            # failure so the grid can show an error state + retry instead of
            # the misleading "このフォルダは空です" (I01).
            logger.warning("scan_children failed for {}: {}", self.root, exc)
            if not self.cancel.is_cancelled():
                self.signals.scan_failed.emit(self.generation, str(exc))
                # Always close out the load: consumers wire metadata_done to
                # loading_changed(False), which must fire on failure too.
                self.signals.metadata_done.emit(self.generation)
            return
        # An empty list means "genuinely empty" ONLY when the scan reported no
        # failure: an unreachable root (offline NAS, unplugged drive, a share
        # that started denying access) never reaches the except above, it comes
        # back as a *reason* alongside an empty list.  Raise the I01 error card
        # for it instead of the misleading empty/welcome state.  This used to
        # be a second is_dir()+scandir probe of the same root, which could not
        # see a transient failure (an SMB share that dropped mid-enumeration
        # and recovered) — a folder with contents was then announced as
        # 「このフォルダは空です」 (レビュー 2026-09-03 項目 #216).
        if reason is not None and not self.cancel.is_cancelled():
            logger.warning(
                "scan_children could not list {}: {}", self.root, reason,
            )
            self.signals.scan_failed.emit(self.generation, reason)
            # Close out the load like the raised-failure path above.
            self.signals.metadata_done.emit(self.generation)
            return
        # If the user has already navigated away, drop the partial result
        # on the floor — no signals, no metadata pass.  Saves both a large
        # cross-thread marshalling of the entries list and, more importantly,
        # a 6-way parallel NAS hammering from the metadata pass that would
        # otherwise compete with the new scan.
        if self.cancel.is_cancelled():
            return
        if scan.incomplete:
            if not entries:
                # 穴が空いた上で 1 件も分類できていない = 「読めた範囲がたま
                # たま空」と区別できない。「このフォルダは空です」ではなく
                # 読み取り失敗を名乗る（最近追加一覧の ``incomplete`` かつ
                # 0 件と同じ裁定）。
                message = (
                    f"一部の項目を読み取れませんでした "
                    f"({scan.unclassified} 件): {self.root}"
                )
                logger.warning("scan_children dropped every child of {}", self.root)
                self.signals.scan_failed.emit(self.generation, message)
                self.signals.metadata_done.emit(self.generation)
                return
            # 一覧は有効なので**結果より先に穴だけを告げる**（``walk_incomplete``
            # と同じ規約）。分類できない子が 1 件あるだけで正常な子ごと捨てて
            # 走査失敗カードを出すのは、穴の告知としては過剰。
            logger.warning(
                "scan_children could not classify {} child entries of {}",
                scan.unclassified, self.root,
            )
            self.signals.scan_partial.emit(self.generation, scan.unclassified)

        if not self.with_metadata:
            # Right-pane path: pre-sort here so the consumer doesn't need a
            # post-receive sort step.  PostGrid's own _apply_filter_and_sort
            # handles the with_metadata=True case using user-selected sort
            # mode, so we deliberately don't sort there.
            entries = sort_children_dir_first(entries)
            self.signals.scan_done.emit(self.generation, entries)
            self.signals.metadata_done.emit(self.generation)
            self._warm_node_index(entries)
            return

        # Emit the shallow scan immediately so the grid populates right away.
        self.signals.scan_done.emit(self.generation, entries)
        self._warm_node_index(entries)

        # Follow-up: one scandir per sub-folder, gathering BOTH post.md
        # metadata and the first-image path for thumbnail resolution.  The
        # alternative — letting the thumbnail loader run its own scandir
        # later — doubles SMB round-trips on NAS shares.  Results flush in
        # chunks so the grid updates progressively.
        pending = [e for e in entries if e.is_dir and not e.metadata_loaded]
        if not pending:
            self.signals.metadata_done.emit(self.generation)
            return
        batch: list[FolderEntry] = []
        # Accumulate (service, post_id, folder, mtime) for the downloaded-post
        # link index.  Flushed in batches alongside ``batch`` so the SQLite
        # writes stay off the hot per-folder path.  Best-effort: a missing
        # service/post_id (older post.md) just yields no row.
        postref_rows: list[tuple[str, str, str, float]] = []

        def _flush_postrefs() -> None:
            if self.search_index is None or not postref_rows:
                return
            try:
                self.search_index.upsert_postrefs(list(postref_rows))
            except Exception as exc:  # pragma: no cover (defensive)
                logger.debug("search index upsert_postrefs failed: {}", exc)
            postref_rows.clear()

        # 温かいフォルダは executor に載せず、``get_many`` 1 回で一括解決する
        # （#37）。従来は全 pending を無差別に 6 並列スレッドへ投げ、キャッシュ
        # 判定を各ワーカー内（``read_folder_preview_cached`` 先頭）で 1 件ずつ
        # 行っていたため、全ヒットでもスレッドプールを立てて
        # ``FolderPreviewCache.get`` を RLock 越しに直列化していた。ここは
        # スキャンワーカースレッド上（GUI スレッドではない）なので、バッチ
        # 解決しても scanning.md の「左ペインでメインスレッド・シードしない」
        # 不変条件（右ペイン ``_seed_folder_resolution`` 限定）とは衝突しない
        # — 解決結果は従来どおり ``metadata_batch`` 1 経路にだけ流れる。
        # ヒット行は live 読みと同じ ``_preview_to_read_tuple`` → ``postref_row``
        # → ``apply_preview`` の変換を通す（``apply_cached_preview`` と等価だが
        # postref 行の生成に ``parsed`` が要るため展開形で書く）。
        if self.folder_cache is not None and not self.cancel.is_cancelled():
            try:
                hits = self.folder_cache.get_many(
                    [(e.path, e.mtime) for e in pending if e.mtime]
                )
            except Exception as exc:  # pragma: no cover (cache best-effort)
                logger.debug("folder preview batch lookup failed: {}", exc)
                hits = {}
            if hits:
                misses: list[FolderEntry] = []
                for entry in pending:
                    # キャンセルはループ**内**でも見る（レビュー 2026-09-03
                    # 項目 #82）。``get_many`` の手前の 1 回きりだと、走行中に
                    # 届いたキャンセルに反応できず、全ヒット分の変換と 8 件
                    # ごとの ``metadata_batch`` emit を最後まで走らせてしまう
                    # — live 読みの as_completed ループ・全ヒット経路と同じ
                    # 「キャンセル後は emit しない」契約に揃える。
                    if self.cancel.is_cancelled():
                        return
                    preview = hits.get(str(entry.path))
                    if preview is None:
                        misses.append(entry)
                        continue
                    parsed, thumb_marker, non_thumb_marker, file_names = (
                        _preview_to_read_tuple(preview)
                    )
                    if parsed is not None:
                        row = postref_row(parsed.meta, entry.path, entry.mtime)
                        if row is not None:
                            postref_rows.append(row)
                    batch.append(
                        apply_preview(
                            entry, parsed, thumb_marker, non_thumb_marker,
                            file_names,
                        )
                    )
                    if len(batch) >= _METADATA_BATCH_SIZE:
                        self.signals.metadata_batch.emit(self.generation, batch)
                        batch = []
                        _flush_postrefs()
                # ミスだけを 6 並列の live 読みへ回す（0 件なら executor 自体を
                # 作らない — ``ThreadPoolExecutor(max_workers=0)`` は不正）。
                pending = misses

        if not pending:
            # 全件キャッシュヒット: executor（ワーカースレッド）を一切立てずに
            # 端数バッチを流して閉じる（#37）。
            if self.cancel.is_cancelled():
                return
            if batch:
                self.signals.metadata_batch.emit(self.generation, batch)
            _flush_postrefs()
            self.signals.metadata_done.emit(self.generation)
            return

        rec = recorder()
        meta_pass_start = time.perf_counter() if rec.is_enabled() else 0.0
        try:
            # デーモンワーカーでファンアウトする（``_fanout`` の docstring）:
            # ``ThreadPoolExecutor`` のワーカーは非デーモンで、インタプリタ
            # 終了時に必ず join される — 死んだ共有で止まったワーカーが 1 本
            # あるだけで、窓を閉じてもプロセスが I/O タイムアウトぶん終われ
            # なくなる。まだ始めていない項目はワーカー側のキャンセル判定で
            # 飛ばすので、``Future.cancel()`` の効果も保たれる（走行中の 1 件
            # はプレビュー読み自身が同じトークンを見て降りる）。
            for entry, result, exc in map_unordered(
                lambda e: _read_folder_preview_paced(
                    e.path, e.mtime, self.folder_cache,
                    self.cancel.is_cancelled,
                ),
                pending,
                workers=self.metadata_parallelism,
                name="viewer-meta",
                should_cancel=self.cancel.is_cancelled,
            ):
                if self.cancel.is_cancelled():
                    break
                if exc is not None:  # pragma: no cover (defensive)
                    logger.warning(
                        "folder preview failed for {}: {}", entry.path, exc,
                    )
                    parsed, thumb_marker, non_thumb_marker, file_names = (
                        None, None, None, []
                    )
                elif result is SKIPPED:
                    continue
                else:
                    parsed, thumb_marker, non_thumb_marker, file_names = result
                if parsed is not None:
                    # Shared meta → postref-row mapping (same helper as
                    # the CacheBuilder's full build), keyed to the
                    # post.md contract constants.
                    row = postref_row(parsed.meta, entry.path, entry.mtime)
                    if row is not None:
                        postref_rows.append(row)
                batch.append(
                    apply_preview(
                        entry, parsed, thumb_marker, non_thumb_marker, file_names
                    )
                )
                if len(batch) >= _METADATA_BATCH_SIZE:
                    self.signals.metadata_batch.emit(self.generation, batch)
                    batch = []
                    _flush_postrefs()
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("metadata pass failed for {}: {}", self.root, exc)
        if self.cancel.is_cancelled():
            return
        if batch:
            self.signals.metadata_batch.emit(self.generation, batch)
        _flush_postrefs()
        self.signals.metadata_done.emit(self.generation)
        if meta_pass_start:
            dt_ms = (time.perf_counter() - meta_pass_start) * 1000.0
            rec.record(
                "metadata_pass_total",
                dt_ms,
                f"{self.root} ({len(pending)} folders)",
            )


class ChildrenScanner(QObject):
    """Scan a directory's immediate children off the main thread.

    Emits:

    * :sig:`finished(int generation, list[FolderEntry])` — the shallow
      scan result (fast, all entries with placeholder metadata for folders).
    * :sig:`metadata_ready(int generation, list[FolderEntry])` — progressive
      batches of entries with ``post.md`` metadata merged in.  Only emitted
      when ``with_metadata=True``; never emitted otherwise.
    * :sig:`failed(int generation, str message)` — the shallow scan itself
      blew up (offline share, permission error).  Emitted INSTEAD of
      ``finished``; ``metadata_finished`` still follows so loading state
      closes out.  Consumers show an error state rather than "empty" (I01).
    * :sig:`partial(int generation, int unclassified)` — the listing stands
      but some children could not be classified at all.  Emitted just BEFORE
      ``finished`` (same 「結果より先に穴を告げる」 shape as
      ``RecursiveSearchScanner.walk_incomplete``), so a consumer can show the
      result *and* say it is incomplete instead of choosing between them.
    * :sig:`metadata_finished(int generation)` — always emitted last, after
      all work has completed (and after a possible empty metadata pass).
      Consumers can wire this to ``loading_changed(False)``.

    Consumers should compare the received generation to
    :meth:`latest_generation` and ignore stale emissions.
    """

    finished = Signal(int, list)
    failed = Signal(int, str)
    partial = Signal(int, int)
    metadata_ready = Signal(int, list)
    metadata_finished = Signal(int)

    def __init__(
        self,
        *,
        with_metadata: bool,
        folder_cache: FolderPreviewCache | None = None,
        search_index: SearchIndex | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._with_metadata = bool(with_metadata)
        self._folder_cache = folder_cache
        self._search_index = search_index
        self._signals = _ScanSignals()
        # 転送先は**メソッド**にする（``self.finished.emit`` を直接つながない）。
        # bound method の受け手は QObject の ``self`` なので、self が破棄されると
        # Qt が接続ごと外し、破棄後に着地した queued 結果は黙って捨てられる。
        # ``Signal.emit`` は QObject ではないただの callable で、PySide は
        # グローバル受け手経由でつなぐため self の破棄に追随しない — 窓を
        # 破棄した後にワーカーが終わると "Signal source has been deleted" を
        # 送出していた（閉じた窓を破棄するテストハーネスで実測: 窓 1 つに
        # つき左右 2 スキャナ × scan/metadata の 2〜3 件 — 2026-09-03）。
        self._signals.scan_done.connect(self._forward_finished)
        self._signals.scan_failed.connect(self._forward_failed)
        self._signals.scan_partial.connect(self._forward_partial)
        self._signals.metadata_batch.connect(self._forward_metadata_ready)
        self._signals.metadata_done.connect(self._forward_metadata_finished)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        # 世代とキャンセルのペアリングは SessionOwner が 1 箇所で持つ
        # （項目#35 — 旧 ``_generation`` + ``_current_cancel`` の手同期を排除）。
        self._sessions = SessionOwner()
        # Runtime-tunable via :meth:`set_metadata_parallelism` — applies to
        # the *next* scan; scans already in flight keep their original value.
        # Only meaningful when ``with_metadata=True``.
        self._metadata_parallelism: int = _METADATA_PARALLELISM

    def set_metadata_parallelism(self, value: int) -> None:
        """Update the post.md worker pool size for the next scan.

        No-op for scanners constructed with ``with_metadata=False`` — they
        never run the parallel pass.
        """
        self._metadata_parallelism = max(1, int(value))

    # ---- ワーカーブリッジ → 公開シグナルの転送 slot（受け手 = self） ----

    def _forward_finished(self, *args) -> None:
        self.finished.emit(*args)

    def _forward_failed(self, *args) -> None:
        self.failed.emit(*args)

    def _forward_partial(self, *args) -> None:
        self.partial.emit(*args)

    def _forward_metadata_ready(self, *args) -> None:
        self.metadata_ready.emit(*args)

    def _forward_metadata_finished(self, *args) -> None:
        self.metadata_finished.emit(*args)

    def request(self, root: Path) -> int:
        # Drop tasks that are still queued (not yet started) on our private
        # pool: they are all stale by definition — every one of them was
        # requested before this call — and with maxThreadCount=2 a burst of
        # navigation otherwise queues up N full enumerations that the new
        # scan has to wait behind.  Running tasks are unaffected (Qt cannot
        # take those back); they stop at their own cancel checks.
        self._pool.clear()
        # ``start()`` cancels the in-flight scan's session — otherwise a scan
        # of a folder with tens of thousands of entries keeps its stat loop +
        # 6-way metadata pool running on the NAS long after the user has
        # navigated elsewhere, stealing SMB credits from the new scan and
        # making the UI feel frozen — and issues the next generation (#35).
        session = self._sessions.start()
        task = _ScanChildrenTask(
            session,
            root,
            self._signals,
            with_metadata=self._with_metadata,
            metadata_parallelism=self._metadata_parallelism,
            folder_cache=self._folder_cache,
            search_index=self._search_index,
        )
        self._pool.start(task)
        return session.generation

    def cancel(self) -> None:
        """Cancel the in-flight scan without starting a new one.

        ``SessionOwner.cancel`` bumps the generation too (#35): an emit
        already sitting in the queued-connection pipeline cannot be recalled,
        so the bump is what makes it stale for consumers comparing against
        :meth:`latest_generation` — previously only
        ``RecursiveSearchScanner`` did this, and ``ChildrenGrid`` compensated
        with a hand-rolled ``_pending_scan_generation = -1``.
        """
        self._pool.clear()
        self._sessions.cancel()

    def latest_generation(self) -> int:
        return self._sessions.latest_generation()


__all__ = [
    "ChildrenScanner",
    "_ScanChildrenTask",
    "_ScanSignals",
]
