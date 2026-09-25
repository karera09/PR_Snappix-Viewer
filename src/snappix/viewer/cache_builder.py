"""Pre-build the thumbnail / aspect caches for a whole subtree.

A user-initiated maintenance op: pick a folder, walk it for image (and PDF)
files, and warm the persistent caches so the next time those folders are
browsed the gallery paints with zero reflow (Eagle's "the whole library is
already warm" feel), even over a slow NAS.

Three modes (the slow part of a full build is decoding + re-encoding + writing
every thumbnail; aspect alone is a header-only read; the index-only mode skips
all image work entirely):

* **Full** (``aspect_only=False``) — decode each image into the persistent
  disk cache (WebP master) AND record its aspect.  A dedicated
  :class:`ThumbnailLoader` (its own worker pool) does the decoding and
  **writes through to the shared disk cache**, reusing the exact decode/master
  path the live gallery uses.  Submissions go through its bulk entry point
  ``ThumbnailLoader.warm`` — same decode path, but without the viewport
  optimisations (memory LRU, in-flight dedup, silent source-limited serve)
  whose "the caller already holds this image" premise a counting consumer
  can't satisfy.  Slow but makes revisits paint from local disk.
* **Aspect-only** (``aspect_only=True``) — read just each image's header
  (``read_image_aspect``) to record its aspect ratio.  No pixel decode, no
  re-encode, no disk write — dramatically faster over a NAS (header bytes
  vs the whole file).  Gives zero-reflow justified layout on revisit; the
  thumbnails themselves still decode on first view.
* **Index-only** (``index_only=True``) — touch no image at all: only warm the
  search index from the walk's directory listing (file/folder names) and the
  downloaded-post link map from every ``post.md``.  The fastest mode — no
  header reads, no decodes — for users who only want the recursive file-name
  search / post-link resolution to be instant.  No-op unless a
  ``search_index`` was supplied.

**This runs as an exclusive modal operation** (the GUI shows a blocking
progress dialog), so unlike the live browse pools it does *not* have to share
the NAS SMB-credit budget with metadata / thumbnail / aspect scanning.  That
lets it push much higher concurrency, tuned independently of the live pools:

* The directory walk is **parallel + streaming** — an internal pool of
  ``os.scandir`` workers lists directories concurrently and emits matching
  files in chunks *as it discovers them*, so the decode / probe pools start
  working while deeper directories are still being listed.  Over a
  high-latency NAS this overlaps thousands of serial directory round-trips
  with real work instead of sitting idle until the whole tree is known.
* Decode (full) and header-read (aspect) pools run at their own, wider
  parallelism (see :class:`~snappix.viewer.state.ViewerState`).
* Full builds put the shared disk cache into **bulk-write mode** so commits
  are batched instead of one-per-image.
* 発見されたファイルは即プールへ流し込まず、``CacheBuilder`` 自前のディス
  パッチキューに積み、**in-flight を小さな窓（ワーカー数×2〜3）に保って完了
  のたびに補充する**。ウォーク（``os.scandir`` のみ）はデコード/プローブより
  桁違いに速く完走するため、旧来の「列挙した端から全部プールへ投入」では
  プール側に数時間分のバックログが積まれ、``pause()``（新規列挙の停止）を
  押しても負荷が下がらなかった。補充制にすれば ``pause()`` は「補充を止め
  る」だけで in-flight 分が数秒で掃けて完全に効く。詳細は :meth:`_pump` /
  :meth:`pause` を参照。

**3 つの非同期レーンは 3 本の :class:`~._runnable.GuardedStream`**（walk /
aspect / post）で、どれも「世代 + 協調キャンセル + 専用プール + 着地の選別」を
機構から受け取る。かつては 3 組の手組み ``QObject`` ブリッジ + ``QRunnable``
が並び、世代ガードの書き方も計数の進め方も対ごとに違っていた — 同じ機構を
3 通りに手書きしたぶんだけ、抜けも 3 通りになる（投入側がタスク参照を保たず
``QRunnable`` の Python ラッパが走行中に GC される形もその 1 つ）。計数の側は
:class:`LaneLedger` 1 つに畳んである。

Because ``total`` is unknown until the walk drains, ``progress(done, total)``
reports a *growing* total during discovery; ``finished`` is emitted exactly
once, only after the walk has completed AND every discovered item has been
processed (or on cancellation).
"""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, assert_never, cast

from loguru import logger
from PySide6.QtCore import QObject, QSize, Signal

from ._fanout import CompletionQueue, DaemonExecutor
from ._runnable import GuardedStream, StreamJob, StreamOutcome
from .aspect_probe import read_image_aspect
from .folder_scan import (
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    POST_MD_NAME,
    admit_dir_identity,
    dir_identity,
    preview_from_parsed,
    select_preview_candidates,
)
from ..common.post_meta import HEAD_READ_LIMIT, read_head
from .gil_pacing import GilPacer
from .post_md import parse_post_md
from .search_index import NodeRow, SearchIndex, postref_row
from .thumbnail_loader import ThumbnailLoader

if TYPE_CHECKING:  # annotations only
    from .folder_preview_cache import FolderPreviewCache
    from .thumb_disk_cache import ThumbDiskCache
    from .thumb_meta_cache import ThumbMetaCache

# What a pre-scan considers.  Video is intentionally excluded: each frame
# grab spins a media pipeline (~seconds, 8 s timeout) so bulk-processing
# hundreds of clips would be punishingly slow for little gain — videos still
# cache on demand when first viewed.  PDFs can't be header-probed for an
# aspect ratio cheaply, so they're only included in the full (decode) mode.
_IMAGE_SUFFIXES = IMAGE_SUFFIXES
_FULL_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES

# Aspect rows buffered on the GUI thread before a ``put_many`` (one sqlite
# transaction + fsync each).  512 keeps the commit *frequency* low enough
# that a background build doesn't stutter the UI with fsync pauses — the
# rows themselves are tiny, so the larger in-memory window only widens
# crash exposure for re-generatable cache data.
_ASPECT_FLUSH_BATCH = 512
# Files are streamed out of the walk in chunks this big so processing starts
# while the tree is still being enumerated (overlap walk latency with work),
# without one cross-thread signal per directory.
_WALK_EMIT_CHUNK = 256
# GIL pacing (see gil_pacing.py): the header probe, the scandir entry loop
# and the post.md parse are pure-Python bursts; a build runs up to
# walk+aspect+post pools' worth of such threads at once, which starves the
# GUI thread of the GIL (~300 ms stalls measured) unless each worker sleeps
# 1 ms every few items.  One shared pacer per concern — counters are
# per-thread, so sharing across a pool preserves the measured semantics.
# post.md parses are the heaviest single-item bursts (tens of ms on large
# bodies), hence the shorter stride.
_WALK_PACER = GilPacer()
_ASPECT_PACER = GilPacer()
_POST_PACER = GilPacer(every=2)
# Aspect probing is dispatched one job PER CHUNK (not per file): each task
# header-reads this many images and reports them in a single batched
# landing.  Header reads are fast, so a per-file job + per-file completion
# signal floods the GUI thread with tens of thousands of queued slot calls on
# a large library and freezes the UI right after start.  Batching collapses
# that to O(files / chunk) jobs and landings while keeping parallelism
# ample (chunk × parallelism files in flight).
_PROBE_TASK_CHUNK = 64
# bounded in-flight 投入の窓幅: プール/loader へ同時に出す仕事量を「ワーカー
# スレッドあたり N 個」に制限し、完了通知（GUI スレッドのスロット）が次を
# 補充する。係数 1 だと GUI スレッドが一瞬詰まっただけでワーカーがアイドルに
# なるので、各スレッドに「実行中 1 + 待機 1〜2」を持たせる 2〜3 がスループット
# を落とさない最小値。この窓が小さいほど pause()（補充停止）から静止までの
# 残り仕事が小さくなる — 全ツリー分のバックログ（数時間）に対し、窓は数秒〜
# 数十秒で掃ける規模に収まる。
_INFLIGHT_ITEMS_PER_THREAD = 3   # フルデコード / post.md 読み（1 件 = 1 投入）
_INFLIGHT_CHUNKS_PER_THREAD = 2  # アスペクトプローブ（1 投入 = _PROBE_TASK_CHUNK 件）
# ウォーク内部 executor への scandir submit も同じ bounded 方式:
# 発見したサブディレクトリを全件即 submit すると、幅広ツリー（ルート直下に
# 数千の投稿フォルダ）ではルート 1 回の scandir で数千 future が executor の
# 内部キューに積まれ、pause()（コーディネータ停止）してもワーカーが自律的に
# キューを消化し続けて NAS への scandir が数分止まらない。コーディネータが
# 保持するフロンティア（deque）から窓の空きぶんだけ submit すれば、pause 中に
# 走るのは in-flight 窓（ワーカー数 × 本係数）だけになり、「pause は数秒で
# 全負荷が静止する」契約がウォークフェーズでも成立する。
_INFLIGHT_DIRS_PER_THREAD = 2    # ウォークの scandir（1 件 = 1 ディレクトリ）
# post レーンのキュー上限。``_post_queue`` / ``_folder_seeds`` はウォークが
# 積み、post ワーカーだけが抜く — ウォークの方が桁違いに速いので、窓を持たない
# と数十万フォルダのツリーで両者が常駐メモリを際限なく食う。in-flight 窓
# （ワーカー数 × _INFLIGHT_ITEMS_PER_THREAD）の 20 倍を「先読みキュー」として
# 許し、超えたらウォークの scandir 補充を止める。20 倍あれば post ワーカーが
# 空転しない一方、上限は数百件（= 常駐メモリ数 MB）に収まる。
_PENDING_POSTS_PER_THREAD = _INFLIGHT_ITEMS_PER_THREAD * 20
# 窓が閉じたまま消費側が一切進まないときに背圧を諦めるまでの秒数。背圧は
# 常駐メモリの最適化であって、**ウォークの完走は契約**: post レーンを進める
# のは GUI スレッド（``_on_post_landed``）なので、ビルダーが放置された / GUI が
# イベントを回さないまま捨てられた、という形で消費が永久に止まりうる。その
# とき窓を守り続けると、ウォークスレッドが 0.1 秒周期で永久に回り続け、
# ``QThreadPool`` のデストラクタが終了時にそれを無期限に待つ。
_POST_GATE_STALL_S = 30.0

#: ウォークが ``progress`` で流す 2 種のバッチ（:class:`StreamOutcome` の kind）。
_WalkKind = Literal["files", "nodes"]


# --------------------------------------------------------------- 計数の帳簿


@dataclass(slots=True)
class LaneLedger:
    """1 レーンぶんの計数規律（発見 → 投入 → 完了）を持つ値型。

    3 つの非同期レーン（画像 / node / post.md）は「分母をいつ進めるか」
    「分子をいつ進めるか」「失敗をどう数えるか」「どこで打ち切るか」を
    それぞれ別の整数フィールドと別の手順で持っていた。同型の規律が 3 通りに
    分かれていると、片方だけ直した修正（重複発見の正規化・straggler の
    誤減算）が他方に届かない。ここへ畳んで 1 種類にする。

    * :attr:`discovered` — 発見した**件数**。進捗の分母への寄与。
    * :attr:`settled` — 決着した**件数**（= :attr:`ok` + :attr:`failed`
      とは限らない: post レーンは成否を進捗に載せないので両方 0 のまま
      settled だけ進む）。進捗の分子。
    * :attr:`inflight` — 投入済み未決着の**投入単位**の数。1 単位が 1 件の
      レーン（full / post）と 1 チャンクのレーン（aspect）がある。
      :attr:`window` と同じ単位で比べるためのカウンタなので、件数とは別勘定。
    * :attr:`window` — in-flight 窓（投入単位）。:meth:`has_room` が見る。
    * :attr:`backlog_cap` — 先読みキューの上限（0 = 無制限）。超えた間は
      :meth:`saturated` が True になり、供給側（ウォーク）が自分を絞る。
    * :attr:`seen` — 「1 パス = 1 計数」を守るための既知キー集合。同じパスが
      二度発見されても :attr:`discovered` は 1 度しか進まない（full レーンの
      完了計数は path キーなので、二重計上すると分子が分母に追いつかない）。
    """

    window: int = 1
    backlog_cap: int = 0
    discovered: int = 0
    settled: int = 0
    inflight: int = 0
    ok: int = 0
    failed: int = 0
    seen: set[str] = field(default_factory=set)

    def reset(self) -> None:
        """次のビルドのために帳簿を空にする（窓と上限は据え置き）。"""
        self.discovered = 0
        self.settled = 0
        self.inflight = 0
        self.ok = 0
        self.failed = 0
        self.seen.clear()

    def first_sight(self, key: str) -> bool:
        """*key* が初出なら記録して True（二度目以降は False）。"""
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def admit(self, count: int = 1) -> None:
        """発見を分母へ載せる。"""
        self.discovered += count

    def dispatch(self, units: int = 1) -> None:
        """投入単位を in-flight へ載せる。"""
        self.inflight += units

    def settle(
        self, *, ok: int = 0, failed: int = 0,
        items: int | None = None, units: int = 1,
    ) -> None:
        """着地を分子へ載せ、in-flight 窓を空ける。

        *items* を省くと ``ok + failed`` 件が決着したものとして数える
        （成否を進捗に載せないレーンは ``items`` を明示する）。
        """
        self.ok += ok
        self.failed += failed
        self.settled += (ok + failed) if items is None else items
        self.inflight -= units

    @property
    def outstanding(self) -> int:
        """まだ決着していない件数（キュー内 + in-flight）。"""
        return self.discovered - self.settled

    def has_room(self) -> bool:
        """in-flight 窓に空きがあるか（= もう 1 単位出してよいか）。"""
        return self.inflight < self.window

    def saturated(self) -> bool:
        """先読みキューが上限に達しているか（背圧の述語）。"""
        return self.backlog_cap > 0 and self.outstanding >= self.backlog_cap


# ------------------------------------------------------------- ウォーク本体


@dataclass(frozen=True, slots=True)
class _WalkPlan:
    """1 回のウォークに渡す設定一式（ワーカースレッドからのみ読む）。

    * ``node_sink(batch)`` — node バッチを検索索引へ upsert する（**この
      ウォークスレッド上**で。sqlite の executemany + commit を GUI スレッド
      でやると fsync ポーズが操作をカクつかせる）。
    * ``folder_sink(seed)`` — post.md を持たないディレクトリの
      :class:`FolderPreviewCache` 行を即書く。
    * ``post_seed_sink(post_md_path, seed)`` — post.md を持つディレクトリの
      ウォーク側情報を退避し、post レーンが ``ParsedPost`` と合流させて書く
      （post.md 本文を読まないモードでは ``None`` = そのフォルダは書かない:
      has_post_md=False の行を焼くと左ペインのタイトル/タグ/投稿日が消える）。
    * ``posts_saturated()`` — post レーンの窓が閉じている間 True。
    * ``resume_event`` — set = 実行中 / clear = 一時停止。
    """

    root: Path
    suffixes: frozenset
    parallelism: int
    collect_nodes: bool = False
    resume_event: object | None = None
    node_sink: Callable[[list], None] | None = None
    folder_sink: Callable[[tuple], None] | None = None
    post_seed_sink: Callable[[str, tuple], None] | None = None
    posts_saturated: Callable[[], bool] | None = None


class _WalkGate:
    """コーディネータの「止めてよい / 止めてはいけない」判定（ウォーク局所）。

    一時停止（``resume_event``）と post レーンの背圧（``posts_saturated``）の
    2 つを、1 回の走行のあいだだけ持つストール時計と一緒に扱う。ストリームや
    ビルダーの状態は持たない（走行ごとに作り捨てる）。
    """

    __slots__ = ("_job", "_resume", "_saturated", "_since")

    def __init__(self, job: StreamJob, plan: _WalkPlan) -> None:
        self._job = job
        self._resume = plan.resume_event
        self._saturated = plan.posts_saturated
        # 窓が閉じ始めた時刻（0 = 開いている）。
        self._since = 0.0

    def post_lane_full(self) -> bool:
        """True while the builder's post.md lane is over its queue window.

        Backpressure for the *walk*: the coordinator stops submitting new
        ``scandir`` work until the post readers catch up, so ``_post_queue``
        and ``_folder_seeds`` stay bounded on huge trees.  Never raises — a
        failing predicate must not abort the walk.

        **不変**: 背圧はウォークを遅らせてよいが、止めてはならない。窓が
        :data:`_POST_GATE_STALL_S` を超えて閉じたままなら消費側（GUI スレッド
        の ``_on_post_landed``）が動いていないと判断し、以後この走行では背圧を
        完全に降ろす（メモリ最適化を捨てて完走を採る）。
        """
        if self._saturated is None:
            return False
        try:
            full = bool(self._saturated())
        except Exception:  # pragma: no cover (defensive)
            return False
        if not full:
            self._since = 0.0
            return False
        now = time.monotonic()
        if self._since == 0.0:
            self._since = now
            return True
        if now - self._since < _POST_GATE_STALL_S:
            return True
        logger.warning(
            "post.md 読みが {} 秒進まないため、ウォークの背圧を解除します",
            int(_POST_GATE_STALL_S),
        )
        self._saturated = None
        self._since = 0.0
        return False

    def wait_if_paused(self) -> None:
        """Block while paused, but stay responsive to cancellation.

        Polls the resume event with a short timeout so a ``cancel()`` (which
        also sets the event, see :meth:`CacheBuilder.cancel`) unblocks
        promptly even if resume itself never comes.

        A pause that actually blocked restarts the post-lane stall clock:
        the consumer (``_pump_posts``) is stopped by the same pause, so the
        gate cannot open while we wait.  Without the reset, resuming after a
        pause longer than :data:`_POST_GATE_STALL_S` makes the very next
        :meth:`post_lane_full` read the pause itself as "the consumer is
        dead", drop backpressure for the rest of the run, and log a stall that
        never happened.
        """
        ev = self._resume
        if ev is None:
            return
        if ev.is_set():
            return
        while not ev.wait(0.1):
            if self._job.cancel.is_cancelled():
                self._since = 0.0
                return
        self._since = 0.0


def _scan_one(
    plan: _WalkPlan, dirpath: str, dir_mtime: float = 0.0,
) -> tuple[
    list[tuple[str, object, float]],
    list[tuple[str, float, int]],
    list[tuple[str, str, bool, float, int]],
]:
    """List one directory (ウォーカースレッド上の純関数).

    Returns ``(subdirs, matching file specs, node specs)`` where each
    subdir is ``(path, identity, mtime)`` — *identity* being the
    ``dir_identity`` cycle-guard key (or ``None``) so the coordinator can
    skip a directory already visited via another path, and *mtime* the
    parent-scandir ``entry.stat().st_mtime`` that keys the subdir's own
    eventual :class:`FolderPreviewCache` row (same stat source as
    ``scan_children``'s ``DirEntry.stat``, per the scanning.md mtime
    invariant; ``0.0`` when folder warming is off).  Node specs are
    populated only when ``plan.collect_nodes`` is set; they cover every file
    and folder indiscriminately so the search index matches
    ``walk_for_search``'s coverage (no ``post.md`` / suffix requirement).

    *dir_mtime* is this directory's own mtime as observed by the parent's
    scandir (``0.0`` for the root / when unavailable).  When folder
    warming is enabled the listing additionally feeds one preview row for
    *this* directory through ``folder_sink`` / ``post_seed_sink`` — built
    purely from information already in hand (names + the shared
    :func:`~snappix.viewer.folder_scan.select_preview_candidates` rule,
    no extra scandir).  Directories whose representative would need the
    BFS descent (no direct candidate but subdirs exist), whose listing
    was incomplete (I/O error — the same "don't poison the cache" rule as
    the live read), or whose mtime is unknown are NOT written; a miss
    just falls back to the live resolve.
    """
    subdirs: list[tuple[str, object, float]] = []
    files: list[tuple[str, float, int]] = []
    nodes: list[tuple[str, str, bool, float, int]] = []
    want_rows = plan.folder_sink is not None or plan.post_seed_sink is not None
    post_md_path: str | None = None
    file_names_lower: list[str] = []
    candidate_names: list[str] = []
    scan_ok = True
    try:
        with os.scandir(dirpath) as it:
            for i, entry in enumerate(it):
                # 巨大なフラットディレクトリでも 1 ディレクトリ 1 tick では
                # バーストが有界化されない: 下のループは Windows では全て
                # scandir バッファ由来の純 Python で、実測 2.3µs/エントリ →
                # 2 万件で 46ms のバーストになり、それを walk_parallelism 本が
                # 同時に回すと GUI が GIL を取れない。256 件ごとに挟んで、
                # ストライドをディレクトリのサイズに依存させない
                # （``scan_children`` のキャンセルポーリングと同じ形）。
                if i and (i & 0xFF) == 0:
                    _WALK_PACER.tick()
                try:
                    # Symlinks AND Windows junctions are both followed
                    # (``follow_symlinks`` defaults to True; junctions
                    # report ``is_dir()==True``) so this walk covers
                    # exactly what ``walk_for_search`` / ``scan_children``
                    # cover — a library assembled out of linked folders
                    # would otherwise be skipped wholesale here and never
                    # warm its aspect / thumb / node index rows.
                    # Junctions used to be excluded outright as a loop
                    # guard, which left their whole subtree permanently
                    # cold even though the live walk browses it fine;
                    # ジャンクションは Windows で無権限に作れるぶん symlink
                    # より一般的で、実害が大きかった。ループはコーディネータの
                    # ``dir_identity`` 循環ガードが見る（実測: ジャンクションの
                    # identity は必ずリンク先へ解決されるので、祖先を指す
                    # ジャンクションは visited にヒットして降下しない）。
                    is_dir = entry.is_dir()
                    is_file = (not is_dir) and entry.is_file()
                    if is_dir:
                        child_mtime = 0.0
                        if plan.collect_nodes or want_rows:
                            st = entry.stat()
                            child_mtime = st.st_mtime
                            if plan.collect_nodes:
                                nodes.append(
                                    (entry.path, entry.name, True, st.st_mtime, 0)
                                )
                        subdirs.append(
                            (entry.path, dir_identity(entry), child_mtime)
                        )
                    elif is_file:
                        st = entry.stat()
                        if plan.collect_nodes:
                            nodes.append(
                                (entry.path, entry.name, False, st.st_mtime, st.st_size)
                            )
                        if want_rows:
                            lower = entry.name.lower()
                            if lower == POST_MD_NAME:
                                post_md_path = entry.path
                            else:
                                file_names_lower.append(lower)
                                candidate_names.append(entry.name)
                        suffix = os.path.splitext(entry.name)[1].lower()
                        if suffix in plan.suffixes:
                            files.append((entry.path, st.st_mtime, st.st_size))
                except OSError:
                    # 個別エントリの分類失敗はスキップだが、フォルダ行に
                    # とっては「実際より小さく見える不完全な列挙」なので
                    # live 読みの ok=False と同じく書き込み対象から外す。
                    scan_ok = False
                    continue
    except OSError:
        scan_ok = False
    _feed_folder_row(
        plan, dirpath, dir_mtime, scan_ok, post_md_path,
        candidate_names, file_names_lower, bool(subdirs),
    )
    # On Windows the entry.stat() calls are served from the scandir
    # buffer (no syscall), so a cached/local tree makes this loop a
    # pure-Python burst — pace it (see gil_pacing.py).
    _WALK_PACER.tick()
    return subdirs, files, nodes


def _feed_folder_row(
    plan: _WalkPlan,
    dirpath: str,
    dir_mtime: float,
    scan_ok: bool,
    post_md_path: str | None,
    candidate_names: list[str],
    file_names_lower: list[str],
    has_subdirs: bool,
) -> None:
    """Route one directory's preview info into the folder-cache sinks.

    Runs on the walk worker thread (like the node sink).  Skips: folder
    warming off / unknown mtime (the root) / incomplete listing (never
    poison the cache) / representative would need the BFS descent (a miss
    degrades correctly to the live resolve, which can descend).
    """
    if plan.folder_sink is None and plan.post_seed_sink is None:
        return
    if not dir_mtime or not scan_ok:
        return
    marker_name, other_name = select_preview_candidates(candidate_names)
    if marker_name is None and other_name is None and has_subdirs:
        return  # gallery-per-subfolder: needs the bounded BFS — don't bake
    seed = (dirpath, dir_mtime, marker_name, other_name, file_names_lower)
    try:
        if post_md_path is None:
            if plan.folder_sink is not None:
                plan.folder_sink(seed)
        elif plan.post_seed_sink is not None:
            plan.post_seed_sink(post_md_path, seed)
    except Exception as exc:  # pragma: no cover (cache best-effort)
        logger.debug("folder preview warm failed for {}: {}", dirpath, exc)


def _walk_tree(job: StreamJob, plan: _WalkPlan) -> StreamOutcome:
    """Parallel, streaming enumeration of cacheable files under a root.

    Runs as one :class:`GuardedStream` job but fans the directory listing
    out to an internal daemon executor (``_fanout.DaemonExecutor``): each worker ``os.scandir``-s
    one directory, queues its subdirectories, and the coordinator reports
    matching files in :data:`_WALK_EMIT_CHUNK` batches as they accumulate.
    The coordinator polls the cancel token between completed directories so
    navigating away abandons the rest of the tree.

    途中経過は ``job.report`` で 2 種類流す（:class:`StreamOutcome` の kind）:
    ``"files"`` = マッチしたファイルのバッチ、``"nodes"`` = 全エントリの
    バッチ + 索引 upsert の成否。最後の 1 回は戻り値（``"walked"``）で、
    ストリームが ``done`` として配送する — 同一スレッドからの emit なので
    全バッチの**後**に必ず 1 回届く。
    """
    gate = _WalkGate(job, plan)
    buffer: list[tuple[str, float, int]] = []
    node_buffer: list[tuple[str, str, bool, float, int]] = []
    cancelled = False

    def report_nodes(batch: list) -> None:
        """Persist *batch* via the node sink (on this thread), then report."""
        ok = True
        if plan.node_sink is not None:
            try:
                plan.node_sink(batch)
            except Exception as exc:  # pragma: no cover (defensive)
                ok = False
                logger.debug("walk-thread node upsert failed: {}", exc)
        job.report(StreamOutcome("nodes", (batch, ok)))

    # Cycle guard: a junction / symlink pointing at an ancestor would
    # otherwise make the walk re-enumerate the same subtree forever
    # (``discovered`` growing without bound).  ここが**唯一の**ループ対策:
    # 識別子 ((st_dev, st_ino)) が既にキュー済みのディレクトリは、どの経路
    # から来ても降下しない。``dir_identity`` はリンクを辿った先の id を返すので
    # ジャンクション / symlink / 実体のどれで到達しても同じ鍵になる。
    # The coordinator loop is single-threaded, so no lock is needed.
    visited: set[object] = set()
    root_id = dir_identity(str(plan.root))
    if root_id is not None:
        visited.add(root_id)
    # 発見済みだが未 submit のディレクトリ（フロンティア）。executor へは
    # in-flight 窓（ワーカー数 × _INFLIGHT_DIRS_PER_THREAD）の空きぶんしか
    # submit しない: 全件即 submit すると executor 内部キューに積まれた
    # scandir は pause 中もワーカーが自律消化し続け、「pause で数秒静止」契約が
    # ウォークフェーズで破れる。コーディネータは pause 中 wait_if_paused で
    # 止まる（= 補充されない）ので、in-flight 窓ぶんが自然完了するだけで
    # scandir 負荷が静止する。各要素は (path, 親 scandir 由来の mtime)。
    # ルートは親が無いので 0.0（= フォルダ行は書かない —
    # ``read_folder_preview_cached`` の「mtime=0 は焼かない」規約と同じ）。
    frontier: deque[tuple[str, float]] = deque(((str(plan.root), 0.0),))
    max_inflight = plan.parallelism * _INFLIGHT_DIRS_PER_THREAD
    # 完了は ``CompletionQueue`` で受ける（完了 1 件 = キュー操作 1 回。
    # ``wait(FIRST_COMPLETED)`` の O(in-flight) バーストはこのループに GilPacer が
    # 無いぶん効くので戻さない）。
    done_q = CompletionQueue()
    try:
        # デーモン版 executor: キャンセルで break したら出口は走行中の
        # ``os.scandir`` を待たずに返る（止まった scandir が GuardedStream の
        # スレッドを握り続けない。``ThreadPoolExecutor`` にしない理由は
        # ``_fanout`` の docstring）。
        with DaemonExecutor(plan.parallelism, name="viewer-walk") as ex:
            pending: set[concurrent.futures.Future] = set()
            while pending or frontier:
                if job.cancel.is_cancelled():
                    cancelled = True
                    for f in pending:
                        f.cancel()
                    break
                # Block here while paused (cancel still breaks out via the
                # cancel-aware wait) so no fresh directory listings are
                # enqueued until the user resumes.
                gate.wait_if_paused()
                if job.cancel.is_cancelled():
                    cancelled = True
                    for f in pending:
                        f.cancel()
                    break
                while (
                    frontier
                    and len(pending) < max_inflight
                    and not gate.post_lane_full()
                ):
                    d, d_mtime = frontier.popleft()
                    fut = ex.submit(_scan_one, plan, d, d_mtime)
                    pending.add(fut)
                    # 追加後に登録する（既に完了済みならこの場で積まれるが、
                    # その時点で pending には居る）。
                    done_q.watch(fut)
                # 1 件の完了を待ち、その時点で溜まっている分はまとめて
                # 回収する。タイムアウト付きなのは cancel / pause を
                # 完了を待たずに拾うため。
                for fut in done_q.take(0.1):
                    pending.discard(fut)
                    try:
                        subdirs, files, nodes = fut.result()
                    except concurrent.futures.CancelledError:
                        continue
                    except Exception:  # pragma: no cover (defensive)
                        continue
                    for d, ident, d_mtime in subdirs:
                        # ``_scan_one`` がワーカースレッド側で計算した
                        # identity を、``_iter_tree`` と共通の訪問可否判定
                        # （folder_scan.should_descend の admission 部）へ
                        # 通す — 両ウォーカーのカバレッジ定義を 1 箇所に
                        # するための差し替え。
                        if admit_dir_identity(ident, visited):
                            frontier.append((d, d_mtime))
                    if files:
                        buffer.extend(files)
                        if len(buffer) >= _WALK_EMIT_CHUNK:
                            job.report(StreamOutcome("files", buffer))
                            buffer = []
                    if nodes:
                        node_buffer.extend(nodes)
                        if len(node_buffer) >= _WALK_EMIT_CHUNK:
                            report_nodes(node_buffer)
                            node_buffer = []
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("cache walk failed: {}", exc)
    if not cancelled and buffer:
        job.report(StreamOutcome("files", buffer))
    if not cancelled and node_buffer:
        report_nodes(node_buffer)
    return StreamOutcome("walked")


# ------------------------------------------------------ 画像 / post.md 本体


def _probe_aspects(
    job: StreamJob, specs: list,
) -> list[tuple[str, float, int, int, int]]:
    """Header-read a chunk of images' aspect ratios (no pixel decode).

    *specs* は ``[(path_str, mtime, size), ...]`` で、mtime/size はタスクと
    一緒に往復する（GUI スレッドのスロットが横持ちの dict なしでアスペクトを
    記録できるように）。戻り値は **必ず *specs* と同じ長さ** — 読めなかった
    ファイルは ``w=h=0`` で載せる。長さが揃っていることが
    :class:`LaneLedger` の「投入した件数は必ず決着する」規律の土台になる。
    """
    out: list[tuple[str, float, int, int, int]] = []
    for path_str, mtime, size in specs:
        wh: tuple[int, int] | None = None
        if not job.cancel.is_cancelled():
            try:
                wh = read_image_aspect(Path(path_str))
            except Exception:  # pragma: no cover (defensive)
                wh = None
            # Header parsing is a pure-Python burst; without pacing a pool
            # of these tasks starves the GUI thread (see gil_pacing.py).
            _ASPECT_PACER.tick()
        w, h = wh if wh is not None else (0, 0)
        out.append((path_str, mtime, size, w, h))
    return out


def _read_post(
    job: StreamJob,
    md_path: str,
    search_index: SearchIndex | None,
    folder_row_cb: Callable[[str, object], None] | None,
) -> StreamOutcome:
    """Read + parse one ``post.md`` and feed the derived stores (off the GUI
    thread): the downloaded-post link map (``postref``) in the search index
    and, via *folder_row_cb*, the folder-preview cache row.  The body itself
    is **not** stored anywhere — the retired full-text dialog was its only
    reader; the filter box's ``body:`` reads post.md lazily.

    ``folder_row_cb(md_path, parsed)``, when given, receives the parsed
    result on this worker thread so the builder can join it with the walk's
    per-directory preview seed and warm the :class:`FolderPreviewCache` row
    for the folder — the one piece of the row the walk itself cannot
    produce (title / tags / posted_at need the body head).
    """
    def release_seed() -> None:
        """Hand a "no parse" result to the row callback so it drops the seed.

        The two early returns below never produce a ``ParsedPost``; without
        this the builder's parked walk seed for that folder would stay in its
        dict for the rest of the build.
        """
        if folder_row_cb is None:
            return
        try:
            folder_row_cb(md_path, None)
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview seed release failed for {}: {}",
                         md_path, exc)

    if job.cancel.is_cancelled():
        release_seed()
        return StreamOutcome("post", False)
    p = Path(md_path)
    try:
        # ``stat`` は read の**前**。後に取ると、read と索引化の間に外部
        # ツールが post.md を差し替えたとき「旧内容 + 新検証キー」の自己矛盾
        # 行が焼き付き、鮮度判定を通り抜けて恒久的に古い解決結果を返す。
        st = p.stat()
        # 先頭ブロックだけを上限付きで読む（``docs/formats/post-md.md`` が
        # 公開仕様として宣言する読み取り契約。この読みの消費者は
        # ``postref_row`` とフォルダプレビュー行の 2 つで、本文 = ``body``
        # はどこにも保存しない）。無上限の read では本文の大きい
        # ``post.md`` を持つライブラリでフルビルドが NAS からライブラリ
        # 全体を線で引くことになる。
        text = read_head(p, HEAD_READ_LIMIT)
    except OSError:
        release_seed()
        return StreamOutcome("post", False)
    parsed = parse_post_md(text)
    if folder_row_cb is not None:
        # Warm this folder's FolderPreviewCache row (walk seed + this
        # parse) — on the worker thread, like the index upserts below.
        try:
            folder_row_cb(md_path, parsed)
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview warm failed for {}: {}", md_path, exc)
    ok = True
    if search_index is not None and not job.cancel.is_cancelled():
        # Feed the downloaded-post link index from this parse — a full
        # build is the one place that covers the entire tree, not just
        # visited roots' direct children.  ``postref_row`` is the shared
        # meta → row mapping (same helper as the browse-time metadata
        # scan), keyed to the post.md contract constants.
        try:
            row = postref_row(parsed.meta, p.parent, st.st_mtime)
            if row is not None:
                search_index.upsert_postrefs([row])
        except Exception as exc:  # pragma: no cover (defensive)
            ok = False
            logger.debug("search index upsert_postrefs failed: {}", exc)
    # parse_post_md is the heaviest pure-Python burst in the build
    # (tens of ms on a large body) — pace it (see gil_pacing.py).
    _POST_PACER.tick()
    return StreamOutcome("post", ok)


class CacheBuilder(QObject):
    """Walk a folder tree and warm the thumbnail + aspect caches.

    Signals:

    * ``progress(done, total)`` — ``total`` grows as the streaming walk
      discovers files (0 until the first batch lands); ``done`` counts
      completed items.
    * ``finished(ok, failed, total)`` — emitted exactly once, after the walk
      drains and all discovered items finish (or on cancellation).
    """

    progress = Signal(int, int)
    finished = Signal(int, int, int)

    def __init__(
        self,
        disk_cache: ThumbDiskCache | None,
        meta_cache: ThumbMetaCache | None,
        *,
        cache_edge: int,
        full_parallelism: int = 8,
        aspect_parallelism: int = 24,
        walk_parallelism: int = 8,
        search_index: SearchIndex | None = None,
        index_parallelism: int = 8,
        folder_cache: "FolderPreviewCache | None" = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._disk_cache = disk_cache
        self._meta_cache = meta_cache
        self._search_index = search_index
        # フォルダ代表解決キャッシュ。渡されるとウォークが 1 ディレクトリ
        # 1 行を温める（post.md 有りフォルダは本文を読むモードでのみ）。
        # ``None``（既定）で従来挙動が完全に不変。
        self._folder_cache = folder_cache
        # post.md 持ちフォルダのウォーク側シード（md_path → seed）。ウォーク
        # ワーカーが書き、post ワーカーが pop する — どちらも dict の単一
        # 操作なので GIL 下でロック不要。行は (path, mtime) キーの冪等な
        # 加速データなので、旧ビルドの straggler が混ざっても害はない。
        self._folder_seeds: dict[str, tuple] = {}
        self._cache_edge = max(1, int(cache_edge))
        self._full_parallelism = max(1, int(full_parallelism))
        self._aspect_parallelism = max(1, int(aspect_parallelism))
        self._walk_parallelism = max(1, int(walk_parallelism))
        self._index_parallelism = max(1, int(index_parallelism))
        # Created lazily on the first dispatched batch so aspect-only builds
        # never spin a decode pool.
        self._loader: ThumbnailLoader | None = None

        # --- 3 つの非同期レーン（どれも GuardedStream の 1 本）-------------
        # ウォークは 2 本立て: cancel 直後の ``build()`` 再利用では、まだ
        # 巻き戻り中の旧ウォークと新ウォークが一時的に重なる（旧実装が共有
        # グローバルプールへ載せて得ていた性質）。1 本に絞ると新ビルドの列挙が
        # 旧ウォークの巻き戻りを待つので、そのぶんだけ広げる。
        self._walk_stream = GuardedStream(self, max_threads=2)
        self._walk_stream.bind(self._on_walk_done)
        self._walk_stream.bind_progress(self._on_walk_batch)
        self._aspect_stream = GuardedStream(
            self, max_threads=self._aspect_parallelism,
        )
        self._aspect_stream.bind(self._on_aspect_batch_done)
        self._post_stream = GuardedStream(
            self, max_threads=self._index_parallelism,
        )
        self._post_stream.bind(self._on_post_landed)

        # Full-mode only: maps an in-flight thumbnail key → (mtime, size) so
        # the loader callback can record the aspect.  Aspect-only builds carry
        # mtime/size inside the batched result instead (no side dict).
        # ``ThumbnailLoader`` のシグナルは世代を運ばないので、この dict の
        # メンバーシップが full レーンの世代ガードそのものでもある。
        self._specs: dict[str, tuple[float, int]] = {}
        self._aspect_batch: list[tuple[str, float, int, int, int]] = []
        # --- 3 レーンの帳簿（計数規律は 1 種類）-----------------------------
        # ``_items`` = 画像（full / aspect のどちらか一方だけが走る）、
        # ``_nodes`` = index-only の node 行（ウォークスレッドで索引化済みな
        # ので発見と同時に決着する）、``_posts`` = post.md 読み（進捗の分母
        # /分子には載らず、``finished`` のゲートと背圧だけを担う）。
        self._items = LaneLedger()
        self._nodes = LaneLedger()
        self._posts = LaneLedger(
            backlog_cap=self._index_parallelism * _PENDING_POSTS_PER_THREAD,
        )
        self._aspect_only = False
        self._index_only = False
        self._apply_windows()
        self._walk_finished = False
        self._finished_emitted = False
        # ビルドのキャンセル状態。世代と協調キャンセルは 3 本のストリームが
        # それぞれ持つので（``build()`` / :meth:`cancel` が同時に動かす）、
        # ビルダー側に残るのはこの 1 ビットだけ — ``_pump`` / ``pause`` /
        # loader レーンの着地ガードが読む。
        self._cancelled = False
        # 自前ディスパッチキュー。ウォークが発見したファイル / post.md は
        # まず（帳簿の ``admit`` と一緒に）ここへ積まれ、``_pump()`` が
        # in-flight 窓の空きぶんだけストリーム / loader へ出す。完了スロットが
        # 窓を空けて再度 ``_pump()`` を呼ぶことで補充が続く。進捗計数はこの
        # キューが単一情報源: ``discovered`` は投入（発見）時、``settled`` は
        # 完了時に進む。
        self._file_queue: deque[tuple[str, float, int]] = deque()
        self._post_queue: deque[str] = deque()
        # ``_pump`` の再入ラッチ。``loader.warm`` は同期 emit を持たないので
        # full レーンからの再入は起きないが、``_pump`` は 3 レーンぶんの補充を
        # 1 関数で回す唯一の入口なので、将来どのレーンが同一スタックで完了
        # スロットを叩いても補充ループが二重に回らないようにここで塞ぐ
        # （再入分は何もせず戻り、外側ループが状態を再評価して続行する）。
        self._pumping = False
        # Pause/resume (background builds).  ``_resume_event`` (set = running)
        # gates the walk job's directory enumeration; ``pause()`` は加えて
        # ``_pump`` を no-op 化してディスパッチキューからの補充を止める。
        # In-flight decodes/probes are left to finish naturally (QRunnables
        # can't be cancelled) — bounded 投入により in-flight は常に小さな窓に
        # 収まっているので、pause は実質数秒で全負荷を解放する。
        self._paused = False
        self._resume_event = threading.Event()
        self._resume_event.set()

    # --- 進捗の読み口（3 レーンの帳簿を足した派生値）---------------------
    # 進捗は「画像レーン + node レーン」の和で、post レーンは載らない
    # （post.md 自体が 1 つの node なので、読みを別に数えると二重計上になる）。

    @property
    def _total(self) -> int:
        return self._items.discovered + self._nodes.discovered

    @property
    def _done(self) -> int:
        return self._items.settled + self._nodes.settled

    @property
    def _ok(self) -> int:
        return self._items.ok + self._nodes.ok

    @property
    def _failed(self) -> int:
        return self._items.failed + self._nodes.failed

    @property
    def _pending_posts(self) -> int:
        """post.md 読みの「キュー内 + in-flight」件数（``finished`` のゲート）。"""
        return self._posts.outstanding

    def _apply_windows(self) -> None:
        """各レーンの in-flight 窓を現在のモードから引き直す。

        窓幅はモジュール定数（テストが差し替える）と ``_aspect_only`` から
        決まるので、``__init__`` と :meth:`_pump` の入口で毎回引き直す —
        窓の値を構築時に焼き付けると、モード切り替えで片方のレーンだけ古い
        窓のまま走る。
        """
        self._items.window = (
            self._aspect_parallelism * _INFLIGHT_CHUNKS_PER_THREAD
            if self._aspect_only
            else self._full_parallelism * _INFLIGHT_ITEMS_PER_THREAD
        )
        self._posts.window = self._index_parallelism * _INFLIGHT_ITEMS_PER_THREAD

    # ------------------------------------------------------------------ run

    def build(
        self, root: Path, *, aspect_only: bool = False, index_only: bool = False,
    ) -> None:
        """Start the walk → process pipeline for *root* (returns immediately).

        ``aspect_only`` records only aspect ratios (fast header reads); the
        default also decodes + persists thumbnails to the disk cache.
        ``index_only`` skips every image (no aspect, no decode) and only warms
        the search index from file names + ``post.md`` bodies — a no-op without
        a ``search_index``.  It takes precedence over ``aspect_only``.

        **Reuse contract.** The same instance may be reused for successive
        builds: every call resets the per-build ledgers/flags so a reused
        instance never reports cumulative totals.  A prior build need not have
        *drained* first — calling ``build()`` right after ``cancel()`` (while
        the cancelled build's in-flight decodes/header-reads/post-reads are
        still running) is safe: each lane's :class:`GuardedStream` starts a
        fresh session, so a straggler from the cancelled build fails
        ``accepts`` on arrival and is discarded instead of polluting the new
        build's counters (which would otherwise emit a premature or miscounted
        ``finished``).  ``wait_for_pools()`` before reuse is therefore
        optional, not required for correctness.
        """
        self._aspect_only = aspect_only and not index_only
        self._index_only = index_only
        self._walk_finished = False
        self._finished_emitted = False
        self._cancelled = False
        # 新セッション: aspect / post は加算投入（``submit_batch``）なので
        # 明示的に現行セッションを畳む（次の投入が新世代を開く）。ウォークは
        # ``submit_job`` が前セッションを畳んでから新世代を開くので、ここでは
        # 触らない。どちらも「旧ビルドの in-flight タスクの ``is_cancelled``
        # が立ち、着地は ``accepts`` で落ちる」形になる。
        self._aspect_stream.cancel()
        self._post_stream.cancel()
        # Per-build session state: without these resets a reused instance
        # reports cumulative ok/failed/total across builds.
        self._specs.clear()
        self._aspect_batch.clear()
        self._items.reset()
        self._nodes.reset()
        self._posts.reset()
        self._apply_windows()
        self._paused = False
        self._resume_event.set()
        # 前ビルドの未ディスパッチ残（cancel 後の再利用時）をリセット。
        # 前ビルドの in-flight straggler は着地の世代ガードで弾かれるため、
        # ここで 0 に戻しても誤減算されない。
        self._file_queue.clear()
        self._post_queue.clear()
        self._folder_seeds.clear()
        # 再利用時は loader の未ディスパッチ分（前ビルドの warm 投入残）を
        # 捨てる。誰も待っていない仕事を新ビルドと並んで走らせないための
        # 後始末で、in-flight 分は完走して emit するが ``_specs`` の世代ガードが
        # 弾く。LRU 側は ``warm`` レーンが読み書きしないので常に空
        # （旧: ``request`` 経由だったため「source-limited 無音 return」が
        # 前ビルドの残エントリに当たって finished が出なくなる穴があった）。
        if self._loader is not None:
            self._loader.clear_cache()
        if index_only:
            # No image work at all: an empty suffix set means the walk streams
            # zero files to decode/probe while still collecting nodes (names)
            # and post.md bodies for the index.
            suffixes = frozenset()
        elif aspect_only:
            suffixes = _IMAGE_SUFFIXES
        else:
            suffixes = _FULL_SUFFIXES
        self._set_bulk_writes(True)
        plan = _WalkPlan(
            root=Path(root),
            suffixes=suffixes,
            parallelism=self._walk_parallelism,
            collect_nodes=self._search_index is not None,
            resume_event=self._resume_event,
            node_sink=self._upsert_node_batch
            if self._search_index is not None else None,
            folder_sink=self._write_folder_row
            if self._folder_cache is not None else None,
            # post.md 持ちフォルダのシードは「post.md 本文を実際に読む
            # ビルド」でしか積まない = それ以外ではそのフォルダは書かない
            # （has_post_md=False の行を焼くとメタが消える正しさ事故）。
            # 本文読みは search_index があり aspect モードでないときだけ走る
            # （``_on_nodes_found``）ので、その条件をそのまま鏡写しにする —
            # 外れたモードでシードを積むと消費者が居らず宙に残る。post.md
            # 無しフォルダは ``folder_sink`` 経由で全モード安全に書ける。
            post_seed_sink=self._stash_folder_seed
            if (
                self._folder_cache is not None
                and not self._aspect_only
                and self._search_index is not None
            )
            else None,
            posts_saturated=self._posts_saturated,
        )
        self._walk_stream.submit_job(lambda job: _walk_tree(job, plan))

    def _set_bulk_writes(self, enabled: bool) -> None:
        """3 つの永続ストアの bulk-write モードをまとめて切り替える。

        有効化はビルド開始 / resume、解除（= flush）は pause と終了。片方だけ
        書くと「pause 中にクラッシュしたら未 commit が消える」「終了しても
        commit されない」のどちらかが片側に残る。full ビルド以外はサムネ
        ディスクキャッシュに触らないので、そこだけモードで場合分けする。
        """
        if (
            not self._aspect_only and not self._index_only
            and self._disk_cache is not None
        ):
            try:
                self._disk_cache.set_bulk_writes(enabled)
            except AttributeError:  # pragma: no cover (cache without bulk API)
                pass
        for store in (self._search_index, self._folder_cache):
            if store is None:
                continue
            try:
                store.set_bulk_writes(enabled)
            except AttributeError:  # pragma: no cover (store without bulk API)
                pass

    # ------------------------------------------------- folder-preview warm

    def _write_folder_row(self, seed: tuple, parsed=None) -> None:
        """Persist one directory's preview row (walk / post worker thread).

        ``seed`` is ``(dirpath, mtime, marker_name, other_name,
        file_names_lower)`` gathered by :func:`_scan_one` from the parent's
        scandir + this directory's own listing — the exact information
        ``read_folder_preview`` would re-derive live.  *parsed* carries the
        ``post.md`` head for post folders (None otherwise).  The cache is
        RLock+WAL thread-safe, and bulk-write mode batches the commits for
        the duration of the build.
        """
        cache = self._folder_cache
        if cache is None:
            return
        dirpath, mtime, marker_name, other_name, file_names = seed
        folder = Path(dirpath)
        marker = folder / marker_name if marker_name else None
        other = folder / other_name if other_name else None
        try:
            cache.put(
                folder, mtime,
                preview_from_parsed(parsed, marker, other, file_names),
            )
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview cache put failed for {}: {}",
                         dirpath, exc)

    def _stash_folder_seed(self, post_md_path: str, seed: tuple) -> None:
        """Park a post-folder's walk seed until its post.md read runs.

        Called on the walk worker thread; popped by
        :meth:`_folder_row_for_post` on the post worker thread.  Single dict
        operations on both sides — atomic under the GIL, no lock needed.
        """
        self._folder_seeds[post_md_path] = seed

    def _posts_saturated(self) -> bool:
        """True while the post.md lane is over its queue window.

        Called from the WALK COORDINATOR THREAD; the ledger it reads is
        written only by the GUI thread (``_on_nodes_found`` /
        ``_on_post_landed`` — the single source of truth for "queued +
        in-flight"), and the two plain int reads behind
        :attr:`LaneLedger.outstanding` are each atomic under the GIL, so no
        lock is needed (same reasoning as :meth:`_stash_folder_seed`).  A
        torn pair can only read *lower* than the truth (a fresher
        ``settled`` against a staler ``discovered``), which relaxes the gate
        for one poll — it can never wedge the walk shut.  Gating on the
        outstanding count rather than ``len(self._folder_seeds)`` is
        deliberate: a cancelled or unreadable ``post.md`` leaves its seed
        behind, so a seed-length gate would stall the walk permanently once
        enough posts failed to parse.
        """
        return self._posts.saturated()

    def _folder_row_for_post(self, md_path: str, parsed) -> None:
        """Join a parsed ``post.md`` with its walk seed and write the row.

        Runs on the post worker thread (called by :func:`_read_post` right
        after ``parse_post_md``).  A missing seed just means the folder was
        skipped by the walk (needs-descent / incomplete listing / superseded
        build cleared the dict) — the live resolve covers it later.

        ``parsed is None`` means the read never happened (cancelled, or the
        file went away): release the parked seed without writing a row, so a
        tree full of unreadable ``post.md`` files can't pin seeds in memory
        for the whole build.
        """
        seed = self._folder_seeds.pop(md_path, None)
        if seed is None or parsed is None:
            return
        self._write_folder_row(seed, parsed)

    def _upsert_node_batch(self, nodes: list) -> None:
        """Persist one raw node batch into the search index.

        Called from the WALK THREAD (:func:`_walk_tree`) — the index is
        RLock+WAL thread-safe, and doing the executemany + commit there
        keeps background builds from stuttering the GUI with fsync pauses.
        Exceptions propagate to the caller, which reports ``indexed_ok``.
        """
        self._search_index.upsert_nodes(
            [NodeRow(Path(p), name, bool(is_dir), mtime, size)
             for (p, name, is_dir, mtime, size) in nodes]
        )

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        # 3 レーンとも「キュー済みを捨てる + 現セッションを cancel + 世代++」。
        # 世代も進むので、既にシグナルキューへ載っている同世代の完了通知も
        # ``accepts`` に落ちる。走行中の QRunnable は止められないが、渡した
        # ``job.cancel`` を刻みで見るワーカー（ウォーク / post）は早期に降りる。
        self._walk_stream.cancel()
        self._aspect_stream.cancel()
        self._post_stream.cancel()
        # Unblock the walk if it is parked in ``_WalkGate.wait_if_paused`` so
        # it observes the cancel and unwinds instead of hanging paused forever.
        self._resume_event.set()
        # 未ディスパッチのキューを破棄（``_pump`` は ``_cancelled`` ガードで
        # 以後 no-op だが、再利用前にメモリを持ち越さないよう明示的に空にする）。
        self._file_queue.clear()
        self._post_queue.clear()
        self._items.seen.clear()
        self._folder_seeds.clear()
        if self._loader is not None:
            self._loader.clear_cache()  # drop pending; in-flight finish fast
        self._emit_finished()

    def request_decode_shutdown(self) -> None:
        """Release decodes parked in a bounded wait (teardown only).

        Forwards to the owned loader's ``request_shutdown`` so a video
        decode stuck waiting for a frame (broken file — up to 8 s, or
        forever if the wait straddles interpreter teardown) exits
        within ~100 ms and :meth:`wait_for_pools` actually converges.
        One-way — NOT part of :meth:`cancel`, which must leave the loader
        reusable for the next build.
        """
        if self._loader is not None:
            self._loader.request_shutdown()

    def _lane_pools(self) -> tuple[GuardedStream, ...]:
        """ドレイン対象の 3 レーン（ウォーク / aspect / post）。

        ウォークも所有プール上で走るので、``wait_for_pools`` / ``pools_idle``
        は 3 本を同じ扱いで見れば足りる — **旧ビルドのウォークも同じプールに
        載っている**（``submit_job`` は走行中を追い越すだけで待たない）ので、
        「新しいビルドが始まったら前のウォークが待機対象から外れる」形には
        原理的にならない。
        """
        return (self._walk_stream, self._aspect_stream, self._post_stream)

    def wait_for_pools(self, timeout_ms: int = 5000) -> bool:
        """Block (bounded) until this build's worker pools drain.

        Called during viewer shutdown after :meth:`cancel`, so no worker is
        still writing to a shared cache DB when the caller closes it.  Waits on
        the loader AND all three lane pools with a shared deadline (never
        unbounded): the full-mode decode pool writes the disk cache, and the
        walk / post workers write the search index — all from worker threads —
        so draining them before the caller closes those DBs removes the
        write-after-close race.

        Returns ``True`` only when **every** wait converged inside the budget.
        予算を使い切って抜けた場合は ``False`` — 呼び出し元はこれを見て
        「掃けなかった writer が残っている」を警告にできる（他のドレイン経路と
        同じ『有界待ち → 返り値 → 警告ログ』の形）。値を捨てても従来どおり
        動くが、捨てると超過が**どこにも現れない**。
        """
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0

        def _remaining() -> int:
            return int(max(0.0, deadline - time.monotonic()) * 1000)

        drained = True
        # ThumbnailLoader owns its pool internally; drain it through its
        # public ``wait_for_done`` API so the loader's pool layout can change
        # without silently breaking this write-after-close guard.
        if self._loader is not None:
            drained = bool(self._loader.wait_for_done(_remaining())) and drained
        for stream in self._lane_pools():
            drained = stream.wait_for_done(_remaining()) and drained
        return drained

    def pools_idle(self) -> bool:
        """True when no worker owned by this builder is still running.

        Non-blocking probe (zero-timeout ``waitForDone`` — never waits).
        ``CacheBuildController`` はユーザーキャンセル/完了後の破棄をこれで
        ゲートする: builder の子には loader と 3 レーンの QThreadPool が
        ぶら下がっており、QThreadPool のデストラクタは in-flight QRunnable の
        完了を**無制限に** ``waitForDone`` で待つため、in-flight の NAS
        デコードが残ったまま ``deleteLater()`` すると次のイベントループ周回で
        GUI が数秒〜数十秒フリーズする。ドレインを待ってから破棄すること。
        """
        if self._loader is not None and not self._loader.wait_for_done(0):
            return False
        return all(stream.wait_for_done(0) for stream in self._lane_pools())

    # ---------------------------------------------------------- pause/resume

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """ディスパッチキューからの補充を止める（:meth:`resume` まで）。

        ウォークは新規のディレクトリ列挙を止め、``_pump`` は no-op になる
        （発見済みバッチはキューに積まれたまま出て行かない）。プール /
        loader に出ているのは常に小さな in-flight 窓（ワーカー数×2〜3）だけ
        なので、それが自然完了すれば全負荷（デコード・WebP エンコード・
        ディスク書き込み・SMB I/O）が数秒で静止する — ウォーク完走後に押して
        も効く（旧実装は既にプールへ積まれた全件バックログを止められ
        なかった）。in-flight の QRunnable 自体はキャンセルできない仕様の
        まま。To bound crash exposure while parked, the batched disk-cache /
        search-index commits are flushed (partial commit); they re-batch on
        resume.
        """
        if self._paused or self._cancelled or self._finished_emitted:
            return
        self._paused = True
        self._resume_event.clear()
        # Flush deferred commits so a crash while paused doesn't lose the work
        # already done.  Re-enabled on resume.
        self._set_bulk_writes(False)
        self._flush_aspect()

    def resume(self) -> None:
        """補充を再開する（ウォーク再開 + キューからのディスパッチ再開）。"""
        if not self._paused or self._cancelled or self._finished_emitted:
            return
        self._paused = False
        # Re-enter bulk-write mode for the rest of the build.
        self._set_bulk_writes(True)
        # ウォークを再開し、pause 中に積まれたキューから補充を始める。
        self._resume_event.set()
        self._pump()
        # The walk may have fully drained WHILE paused (its landing hit the
        # pause gate in ``_maybe_finish``) with nothing left to dispatch —
        # e.g. an index-only build whose node counts advanced synchronously.
        # Re-check completion so a build paused past its walk still emits
        # ``finished``.
        self._maybe_finish()

    # ----------------------------------------------------- streamed walk

    def _on_walk_batch(self, payload: object) -> None:
        """ウォークの途中経過（``files`` / ``nodes``）を振り分ける。

        着地の選別（世代一致 ∧ 未キャンセル）は :class:`GuardedStream` の
        ``bind_progress`` が済ませているので、ここに世代ガードは無い —
        cancel 後に再利用された旧ウォークからの straggler（旧ルートのファイル）
        は届かない。
        """
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_WalkKind, payload.kind)
        match kind:
            case "files":
                self._on_files_found(cast(list, payload.value))
            case "nodes":
                batch, indexed_ok = cast(tuple, payload.value)
                self._on_nodes_found(batch, bool(indexed_ok))
            case _:
                assert_never(kind)

    def _on_files_found(self, files: list) -> None:
        """A batch of files surfaced by the still-running walk: enqueue it.

        キューへの投入と同時に帳簿の分母を進める（``discovered`` は「発見
        済み」の単一情報源 — bounded 投入ではディスパッチは常に分母より
        遅れて追走する）。実際のプール投入は :meth:`_pump` が in-flight 窓の
        空きぶんだけ行う。pause 中も投入と計数はする（ウォークは止まっている
        ので届くのは送信済みの端数バッチのみ）が、``_pump`` が no-op なので
        新しい仕事は始まらない。

        既知パス（:attr:`LaneLedger.seen`）は投入せず、分母は**未知パスの
        投入時のみ**進む: full レーンの完了計数は path キーの ``_specs`` dict
        に乗るため、同一パスの二重投入は分母だけを進めて ``finished`` を
        永久に不成立にする。
        """
        if not files or self._index_only:
            return
        fresh = [spec for spec in files if self._items.first_sight(spec[0])]
        if not fresh:
            return
        self._items.admit(len(fresh))
        self._file_queue.extend(fresh)
        self._pump()
        self.progress.emit(self._done, self._total)

    def _on_nodes_found(self, nodes: list, indexed_ok: bool = True) -> None:
        """A batch of all-entry node specs from the walk: count them (the
        walk thread already upserted the rows into the search index) and
        dispatch post.md body reads (full / index-only modes)."""
        if not nodes or self._search_index is None:
            return
        # In the index-only build the node rows ARE the cached unit, so they
        # drive both the progress bar and the final "cached N" count (the
        # full/aspect builds count images instead).  Nodes were upserted on
        # the walk thread before this slot fired, so they are admitted and
        # settled in the same breath; ``finished`` is still gated on the walk
        # draining AND the async post.md reads below.  Each post.md is itself
        # a node, so counting node rows already covers the post.md files —
        # counting the reads too would double-count.
        if self._index_only:
            n = len(nodes)
            self._nodes.admit(n)
            self._nodes.dispatch(n)
            self._nodes.settle(
                ok=n if indexed_ok else 0,
                failed=0 if indexed_ok else n,
                units=n,
            )
            self.progress.emit(self._done, self._total)
        # post.md reads (postref links + folder-preview rows) only in the full
        # and index-only builds — an extra post.md read is marginal next to
        # decoding every image, but would roughly double the I/O of the
        # header-only aspect build.
        if self._aspect_only:
            return
        post_paths = [
            p for (p, name, is_dir, _m, _s) in nodes
            if not is_dir and name.casefold() == POST_MD_NAME
        ]
        if post_paths:
            # 帳簿の ``outstanding``（キュー内 + in-flight）が ``finished`` を
            # ゲートする。実ディスパッチは ``_pump`` の post レーンが窓の
            # 空きぶんだけ行う。
            self._posts.admit(len(post_paths))
            self._post_queue.extend(post_paths)
            self._pump()

    def _on_post_landed(self, _payload: object) -> None:
        # The read AND the upserts already ran on the worker thread (see
        # :func:`_read_post`); this slot only drains the ledger.  A straggler
        # from a superseded build never gets here (the stream's ``bind``
        # drops it) — crediting it would drive ``outstanding`` negative and
        # fire ``finished`` before the new build's own post reads land.
        self._posts.settle(items=1)
        self._pump()  # 窓が空いた: 次の post.md 読みを補充
        self._maybe_finish()

    def _on_walk_done(self, _payload: object = None) -> None:
        # A landing from a superseded walk never reaches this slot (the
        # stream's ``bind`` drops it), so ``_walk_finished`` can only be
        # flipped by the current build's own walk — otherwise
        # ``_maybe_finish`` could fire ``finished`` before it has drained.
        self._walk_finished = True
        # Re-emit progress so a zero-file (or already-drained) build updates
        # the dialog, then check whether processing already caught up.
        self.progress.emit(self._done, self._total)
        self._maybe_finish()

    # ------------------------------------------------- bounded dispatch pump

    def _pump(self) -> None:
        """キューから in-flight 窓の空きぶんだけストリーム / loader へ補充する。

        投入経路（``_on_files_found`` / ``_on_nodes_found``）と完了スロット
        （``_on_loaded`` / ``_on_failed`` / ``_on_aspect_batch_done`` /
        ``_on_post_landed``）の両方から呼ばれる。pause / cancel / finished 後は
        no-op — これが ``pause()`` を「補充停止」として完全に効かせる仕組み。
        エラー完了でも窓は空くので補充は止まらない。
        """
        if self._pumping:
            # loader のキャッシュヒットは request() の同一スタックで loaded を
            # 再入発火する。再入分は何もしない（外側の補充ループが _specs の
            # 減少を見て続行する）。
            return
        self._pumping = True
        try:
            self._apply_windows()
            self._pump_files()
            self._pump_posts()
        finally:
            self._pumping = False

    def _pump_files(self) -> None:
        if self._paused or self._cancelled or self._finished_emitted:
            return
        if self._aspect_only:
            # 1 投入 = 最大 _PROBE_TASK_CHUNK 件のヘッダ読み（per-file
            # ジョブ/シグナルの洪水対策）。窓は投入単位で数える: 各スレッドに
            # 実行中 1 + 待機 1 を持たせる。
            while self._file_queue and self._items.has_room():
                take = min(_PROBE_TASK_CHUNK, len(self._file_queue))
                chunk = [self._file_queue.popleft() for _ in range(take)]
                self._items.dispatch()
                # ``submit_batch`` = 先行を捨てずに積む投入。``submit`` で
                # 積むと 1 つ前のチャンクが未着手のまま捨てられ、受付済み
                # （分母に載せた）件の答えが二度と返らない。
                self._aspect_stream.submit_batch(
                    lambda job, specs=chunk: _probe_aspects(job, specs)
                )
            return
        # full モード: in-flight は帳簿と ``_specs``（loader へ出した未完了
        # キー）が対で数える。loader 内の pending キューには最大でも
        # (窓 - スレッド数) 件しか積まれないので、pause 後は窓ぶんの
        # デコードが自然完了するだけで静止する。
        size = QSize(self._cache_edge, self._cache_edge)
        while (
            self._file_queue
            and self._items.has_room()
            and not self._cancelled
            and not self._finished_emitted
        ):
            loader = self._loader
            if loader is None:
                loader = ThumbnailLoader(
                    # ``warm`` レーンはメモリ LRU を読み書きしないので、この
                    # 値はビルダー経路では使われない（ThumbnailLoader の
                    # 必須引数として最小限を渡すだけ）。
                    cache_size=32,
                    max_threads=self._full_parallelism,
                    parent=self,
                    disk_cache=self._disk_cache,
                    cache_edge=self._cache_edge,
                )
                loader.loaded.connect(self._on_loaded)
                loader.failed.connect(self._on_failed)
                self._loader = loader
            path_str, mtime, sz = self._file_queue.popleft()
            self._specs[path_str] = (mtime, sz)
            self._items.dispatch()
            # ``warm`` はビルダー用の入口（``request`` ではない）: メモリ LRU を
            # 読み書きせず、in-flight / キュー済みの重複折り畳みもしないので、
            # 1 投入 = 必ず 1 回の loaded/failed が**非同期で**返る。ビューポート
            # 供給用の ``request`` が持つ 3 つの無音経路（source-limited 無音
            # return ほか）は「呼び出し側が既に画像を持っている」前提でのみ
            # 正しく、件数を数えるビルダーでは ``_specs`` が居座って finished が
            # 出なくなる。
            loader.warm(path_str, Path(path_str), size)

    def _pump_posts(self) -> None:
        if self._paused or self._cancelled or self._finished_emitted:
            return
        index = self._search_index
        row_cb = (
            self._folder_row_for_post if self._folder_cache is not None else None
        )
        while self._post_queue and self._posts.has_room():
            p = self._post_queue.popleft()
            self._posts.dispatch()
            self._post_stream.submit_batch(
                lambda job, md=p: _read_post(job, md, index, row_cb)
            )

    # full-mode loader callbacks -------------------------------------------

    def _on_loaded(self, key: str, image) -> None:
        # ``cancel()`` emits ``finished`` immediately; a decode already in
        # flight then completes and its ``loaded`` signal is delivered
        # afterwards.  Ignore it so the ledger doesn't advance past the
        # already-final counters (which would emit ``progress`` after
        # ``finished`` — violating "finished is last, exactly once") and so
        # ``_record_aspect`` doesn't append to ``_aspect_batch`` and try to
        # ``put_many`` into an already-closed ThumbMetaCache during shutdown.
        if self._cancelled or self._finished_emitted:
            self._specs.pop(key, None)
            return
        # ``ThumbnailLoader``'s signals carry no generation, so ``_specs``
        # membership IS the generation guard for full builds: ``build()`` clears
        # ``_specs`` and only the current build re-populates it in
        # ``_pump_files``.  A straggler from a cancelled prior build (whose
        # decode was left in flight) therefore has no ``_specs`` entry — drop it
        # so it can't inflate the new build's counters (which would emit a
        # premature/miscounted ``finished``).
        ms = self._specs.pop(key, None)
        if ms is None:
            return
        if image.width() > 0 and image.height() > 0:
            self._record_aspect(key, ms, image.width(), image.height())
        self._settle_item(ok=True)

    def _on_failed(self, key: str) -> None:
        if self._cancelled or self._finished_emitted:
            self._specs.pop(key, None)
            return
        # Same generation guard as ``_on_loaded``: a failure for a key not owned
        # by the current build is a straggler — drop it without counting.
        if self._specs.pop(key, None) is None:
            return
        self._settle_item(ok=False)

    def _settle_item(self, *, ok: bool) -> None:
        """full レーンの 1 件が着地した（計数 → 進捗 → 完了判定 → 補充）。"""
        self._items.settle(ok=1 if ok else 0, failed=0 if ok else 1)
        self.progress.emit(self._done, self._total)
        self._maybe_finish()
        self._pump()  # 窓が空いた: エラー完了でも補充を続ける

    # aspect-only callback --------------------------------------------------

    def _on_aspect_batch_done(self, payload: object) -> None:
        """A whole chunk of header reads landed: record + count in one pass.

        Processing the chunk in a single GUI-thread slot (rather than one slot
        invocation per file) is what keeps a large build from freezing — the
        progress signal fires once per chunk, not once per file.

        A chunk from a cancelled/superseded build never reaches this slot
        (the stream's ``bind`` drops it), so it can't inflate the new build's
        counters and emit a premature ``finished``.  The ``_finished_emitted``
        guard covers the same-build late-arrival case (results landing after
        ``cancel()`` re-emitted ``finished``).
        """
        if self._finished_emitted:
            return
        if not isinstance(payload, list):  # pragma: no cover (defensive)
            logger.debug("aspect chunk landed without results")
            self._items.settle(items=0)
            return
        ok = failed = 0
        for path_str, mtime, size, w, h in payload:
            if w > 0 and h > 0:
                self._record_aspect(path_str, (mtime, size), w, h)
                ok += 1
            else:
                failed += 1
        self._items.settle(ok=ok, failed=failed)
        self.progress.emit(self._done, self._total)
        self._pump()  # 窓が空いた: 次のチャンクを補充
        self._maybe_finish()

    # shared ----------------------------------------------------------------

    def _record_aspect(self, path: str, ms: tuple[float, int], w: int, h: int) -> None:
        if self._meta_cache is None:
            return
        self._aspect_batch.append((path, ms[0], ms[1], w, h))
        if len(self._aspect_batch) >= _ASPECT_FLUSH_BATCH:
            self._flush_aspect()

    def _maybe_finish(self) -> None:
        """Finish only once the walk has drained AND all items are processed.

        「決着していない件数がゼロ」は走査の途中でも一時的に真になりうる
        （処理が発見に追いついている局面）ので、``_walk_finished`` ゲートが
        「もうファイルは増えない」を保証するまで ``finished`` を出さない。
        post レーンの ``outstanding`` は非同期の post.md 読み（postref リンク
        /プレビュー行）が掃けるまで追加で待たせる。

        ディスパッチキューの残はゲートに暗黙に含まれる: 帳簿の分母は投入
        （発見）時に進み分子は完了時にしか進まないので、``_file_queue`` が
        空でなければ画像レーンの ``outstanding`` が正、``_post_queue`` が
        空でなければ post レーンの ``outstanding`` が正になる（キューが計数の
        単一情報源）。``not _paused`` ゲートは「pause 中に全 in-flight が
        掃け切っていても finished は出さない」ため — 出してしまうと
        :meth:`resume` が no-op（``_finished_emitted``）になる。resume が
        ディスパッチ再開後に再チェックするので、ウォーク完走後に pause された
        ビルドも必ず終端する。
        """
        if self._paused or not self._walk_finished:
            return
        if any(
            lane.outstanding > 0
            for lane in (self._items, self._nodes, self._posts)
        ):
            return
        self._emit_finished()

    def _flush_aspect(self) -> None:
        if not self._aspect_batch or self._meta_cache is None:
            self._aspect_batch.clear()
            return
        try:
            self._meta_cache.put_many(self._aspect_batch)
        except Exception:  # pragma: no cover (cache write best-effort)
            pass
        self._aspect_batch.clear()

    def _emit_finished(self) -> None:
        if self._finished_emitted:
            return
        self._finished_emitted = True
        self._flush_aspect()
        # Flush any commits deferred under bulk-write mode.
        self._set_bulk_writes(False)
        self.finished.emit(self._ok, self._failed, self._total)


__all__ = ["CacheBuilder", "LaneLedger"]
