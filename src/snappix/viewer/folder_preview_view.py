"""Center pane: read-only folder preview (thumbnail + child-tile grid).

Shown when a single-click / scroll lands on a folder entry in the right
pane *without* drilling into it.  All disk I/O — directory scan, ``post.md``
parse, image decode — runs off the GUI thread (the injected
``ThumbnailLoader`` for child tiles, this view's own
:class:`~._runnable.GuardedStream` for the rest) so the GUI never freezes
while a slow NAS folder loads.

The child grid is a :class:`gallery_view.GalleryView` in square-grid mode —
the same rendering base as the main children panes — so tile painting,
DPR handling, placeholder glyphs and hit testing are shared rather than
re-implemented on a raw ``QListWidget`` (whose ``setUniformItemSizes``
promise this view used to depend on, with undefined behaviour whenever the
per-item geometry drifted).

Thumbnail data flow (項目#14):

* **Child tiles** go through an injected :class:`~.thumbnail_loader.
  ThumbnailLoader` (``ViewerWindow`` builds a small dedicated instance —
  same shape as the lightbox filmstrip loader — and threads it in via
  ``ContentView.set_folder_thumbnail_loader``).  That buys the persistent
  disk-cache tier (a session revisit paints from local disk instead of
  re-reading the NAS), a bounded 2-worker pool, queue cancellation on
  folder switch, and the closeEvent drain — none of which the old
  view-local ``QThreadPool.globalInstance()`` decode had.
* **The centre image** stays view-local: its decode target is the label's
  resolution (up to native 8000px), which doesn't fit the loader's
  disk-cache tiers.  It shares this view's :class:`~._runnable.GuardedStream`
  (2 threads) with the directory scan, so a folder switch drops the queued
  stale decodes and trips the running one in a single ``cancel()``, plus the
  byte-budgeted LRU for instant revisits.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never, cast

from loguru import logger
from PySide6.QtCore import (
    QEvent,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QIcon,
    QImage,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import FONT_BODY_PT, FONT_SUBTITLE_PT, hint_style
from ..common.ui.timers import DebounceMode, Debouncer
from ._runnable import GuardedStream, StreamJob, StreamOutcome
from .children_grid import build_tile
from .edge_nav import navigate_on_wheel
from .folder_scan import (
    IMAGE_SUFFIXES,
    read_folder_preview_checked,
    scan_children,
)
from .gallery_view import GalleryView, Tile
from .image_cache import BoundedImageCache
from .justified_layout import LayoutParams
from . import view_prefs
from .qimage_decode import (
    decode_qimage_bytes,
    image_size_from_bytes,
    read_file_bytes,
)

if TYPE_CHECKING:  # annotation only — no runtime dependency edge
    from .thumbnail_loader import ThumbnailLoader


# In-memory decoded-thumbnail LRU budget for the folder preview.
#
# NOTE (tuning point): these are *not* wired to ViewerState / the settings
# dialog the way ImageView / MarkdownView caches are (their reconfigure_cache
# is driven from apply_cache_settings).  ``folder_preview_cache_max_mib`` in
# state.py is a *different* cache — the on-disk folder-resolution/metadata DB
# (folder_preview_cache.py) — not this in-memory QImage LRU.  Adding a UI knob
# is blocked here because settings_dialog.py is out of scope; promoting the
# budget to module level (from the old class constants) at least makes it a
# single documented place to adjust, and lowers the default from 512 MiB so
# that, combined with the ImageView / MarkdownView LRUs, resident memory stays
# bounded even when the folder preview is used heavily.  Wiring a proper
# ViewerState field + FolderPreviewView.apply_settings(state) (BoundedImageCache
# .reconfigure) is left as a future task (see review finding #160).
_FOLDER_PREVIEW_CACHE_MAX_BYTES = 256 * 1024 * 1024
_FOLDER_PREVIEW_CACHE_MAX_ENTRIES = 96
_FOLDER_PREVIEW_CACHE_MAX_SINGLE_BYTES = 128 * 1024 * 1024


#: フォルダプレビューの off-thread 仕事の結末（走査 2 種 + 中央画像 2 種）。
#: 1 本のストリームに同居させるので ``kind`` で畳む:
#:
#: * ``scan`` — ``(thumb_path: Path | None, entries: list[FolderEntry])``
#: * ``scan_failed`` — エラーメッセージ
#: * ``thumb`` — ``(QImage, Path, target_px, is_native)``。``Path`` を載せる
#:   のは、走行中にタイルクリックで中央画像が切り替わった場合に GUI が
#:   取り違えないため。``target_px`` はキャッシュが同じパスの複数品質を
#:   比べるため。``is_native`` は「要求サイズ以下の原寸をそのまま使った」
#:   ことを表し、真なら将来どの要求も再デコードで良くならない = この
#:   エントリで必ず足りる（原寸を覚えずに同じ判断ができる）。
#: * ``thumb_failed`` — ``Path``。中央ラベルが古いプレースホルダのまま
#:   固まらないよう、失敗も必ず着地させる。
#:
#: 子タイルのデコードはここを通らない — 注入された
#: :class:`~.thumbnail_loader.ThumbnailLoader` の ``loaded`` / ``failed``
#: をビューが直接受ける（項目#14）。
_PreviewKind = Literal["scan", "scan_failed", "thumb", "thumb_failed"]


def _scan_folder_preview(job: StreamJob, folder: Path) -> StreamOutcome | None:
    """Off-thread ``scan_children`` + thumbnail-path resolution (純関数).

    Runs both passes sequentially because they share a working-set
    folder and the second is cheap once the first has warmed the OS
    cache.  Any error is reported as ``scan_failed`` so the GUI can
    fall back gracefully on permission / network failures.

    協調キャンセル（項目#136）: フォルダ切替と窓の close でストリームが
    ``cancel`` され、走行中の scandir / post.md 読みが**次のチェックポイント
    で**止まる。これが無いと ``QThreadPool`` のデストラクタが走行中の
    runnable を無期限に待ち、到達不能な NAS を選んだ直後に閉じるとプロセス
    終了が SMB タイムアウトぶん遅れる。
    """
    cancelled = job.cancel.is_cancelled
    if cancelled():
        return None
    try:
        _parsed, marker, other, _file_names, _ok = read_folder_preview_checked(
            folder, should_cancel=cancelled,
        )
    except Exception as exc:  # noqa: BLE001 — worker boundary
        return None if cancelled() else StreamOutcome("scan_failed", str(exc))
    if cancelled():
        return None
    thumb_path = marker if marker is not None else other
    try:
        entries = scan_children(folder, should_cancel=cancelled)
    except Exception as exc:  # noqa: BLE001 — worker boundary
        return None if cancelled() else StreamOutcome("scan_failed", str(exc))
    if cancelled():
        return None
    return StreamOutcome("scan", (thumb_path, entries))


def _load_main_thumb(
    job: StreamJob, path: Path, target_px: int,
) -> StreamOutcome | None:
    """Decode + pre-scale the CENTRE image off the GUI thread (純関数).

    ``QImage`` is the cross-thread payload — converting to ``QPixmap`` is
    reserved for the GUI thread per the viewer-tree invariant documented in
    CLAUDE.md.  Every failure path returns ``thumb_failed`` so the centre
    label settles instead of showing a stale placeholder forever.

    未着手のまま窓が閉じた分はここで降りる（項目#136）— デコードそのものは
    分割できないので、止められるのは開始前だけ。
    """
    if job.cancel.is_cancelled():
        return None
    failed = StreamOutcome("thumb_failed", path)
    try:
        # Bytes-first read + Pillow-first decode (see qimage_decode):
        # fixes SMB/CJK paths and keeps the GIL released during the
        # decode.  One read serves both the size probe and the decode.
        data = read_file_bytes(path)
        if data is None:
            return failed
        wh = image_size_from_bytes(data, path)
        will_downscale = wh is not None and (
            wh[0] > target_px or wh[1] > target_px
        )
        target = QSize(target_px, target_px) if will_downscale else None
        image = decode_qimage_bytes(data, path, target_size=target)
    except Exception as exc:  # noqa: BLE001 — worker boundary
        logger.warning("Folder preview thumb failed for {}: {}", path, exc)
        return failed
    if image is None or image.isNull():
        return failed
    # ``is_native`` is True when no downscaling happened — the source
    # is at or below the requested target, so this entry already
    # contains every pixel the file can ever give us.
    return StreamOutcome("thumb", (image, path, target_px, not will_downscale))


class FolderPreviewView(QWidget):
    """Read-only preview of a sub-folder: thumbnail + child tiles grid.

    Shown when scroll / single-click in the right pane lands on a folder
    entry, *without* drilling into it.  Double-click is the only path that
    still triggers root navigation (see ``main_window.py``).

    All disk I/O — directory scan, ``post.md`` parse, image decode — runs
    off the GUI thread so the GUI never freezes while a slow NAS folder
    loads: child-tile thumbnails via the injected ``ThumbnailLoader``
    (:meth:`set_thumbnail_loader`, 項目#14 — cancellable pending queue +
    persistent disk cache), the directory scan and the centre decode on a
    dedicated :class:`~._runnable.GuardedStream` owned by this view.  Both
    are submitted *additively* (``submit_batch``) because they share one
    stream: a superseding submit would silently drop the sibling work
    already queued on it (see :meth:`_submit_main_thumb`).  Folding is the
    job of :meth:`set_folder` / :meth:`clear` / :meth:`shutdown`, whose
    ``cancel`` drops the queued work, trips the session's ``CancelToken``
    (the scan passes it on as ``should_cancel``, the centre decode reads
    ``job.cancel`` directly) and moves the generation on so results of
    already-*running* workers are discarded.

    Wheel handling: the internal child grid consumes wheel notches itself
    (``GalleryView.wheelEvent`` accepts them), so ``navigate_requested``
    only ever comes from the surrounding chrome — see :meth:`wheelEvent`,
    which routes that case through ``edge_nav.navigate_on_wheel``.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)

    _MAX_CHILDREN = 60
    _CHILD_ICON_PX = 96  # logical pixels — DPR applied at decode time
    # Loader-key namespace for child-tile requests (項目#14).  The loader
    # instance is dedicated to this view today, but the prefix keeps its
    # keys collision-free if it is ever shared (same pattern as the
    # lightbox filmstrip's "lightbox:<id>:" prefix).
    _CHILD_KEY_PREFIX = "folderpreview:"
    # Child tiles are decoded at 2× the logical tile edge (pre-DPR) — the
    # loader multiplies by DPR itself — matching the old view-local
    # supersampled target so grid tiles stay crisp when the cell is
    # repainted slightly larger.
    _CHILD_REQUEST_EDGE = _CHILD_ICON_PX * 2
    # Caption strip under each child tile (two elided lines à la the main
    # grid panes; the tooltip carries the full path).
    _CHILD_CAPTION_H = 32
    # Centre thumbnail decode floor: even when the widget hasn't been
    # laid out yet, never decode below this (covers the case where the
    # view is built off-screen and queried before showEvent).  The
    # actual target is computed dynamically from
    # ``thumb_label.size() × dpr`` once the widget has geometry, and
    # the cache logic upgrades to higher resolutions on demand.
    _MAIN_TARGET_PX_FLOOR = 1024
    # Byte-budgeted LRU cache.  At 4K resolution a single QImage can be
    # 50–75 MB so a count-only cap is the wrong abstraction — re-use
    # the project's :class:`BoundedImageCache` instead.  Budgets live at
    # module level now (see the ``_FOLDER_PREVIEW_CACHE_*`` docstring for
    # why they aren't settings-wired yet).
    _CACHE_MAX_BYTES = _FOLDER_PREVIEW_CACHE_MAX_BYTES
    _CACHE_MAX_ENTRIES = _FOLDER_PREVIEW_CACHE_MAX_ENTRIES
    _CACHE_MAX_SINGLE_BYTES = _FOLDER_PREVIEW_CACHE_MAX_SINGLE_BYTES
    # Splitter starts biased toward the centre thumbnail so the
    # enlarged-by-default behaviour matches "プレビュー時の中央画像を
    # 拡大できるようにしてください"; the user can still drag the handle
    # to give the children grid more space.
    _SPLITTER_INITIAL_RATIO: tuple[int, int] = (2, 1)  # thumb : grid
    # How long the splitter / window must be still before we issue an
    # upgrade decode request — matches the ImageView LANCZOS debounce.
    _REDECODE_DEBOUNCE_MS = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._folder: Path | None = None
        # True after a scan failed for the current folder so a re-click on
        # the same folder is allowed to retry (the idempotency guard would
        # otherwise leave the "読み込み失敗" state stuck forever).
        self._last_scan_failed: bool = False
        # Representative images whose decode failed in THIS folder session
        # (09-03 #197).  The thumb cache only remembers successes, so without
        # this every splitter drag / resize / show re-queued the same doomed
        # decode through the debounce timer — a NAS round-trip plus a worker
        # slot each time, on exactly the files that are slowest to fail.
        # Cleared whenever the folder actually changes (``set_folder`` past
        # its idempotency guard) or the view is cleared, so re-entering the
        # folder always re-validates a file that may have been replaced.
        # Keyed by ``str(path)`` to avoid Path comparison differences.
        self._failed_thumb_paths: set[str] = set()
        self._main_thumb_path: Path | None = None
        self._main_thumb_source: QPixmap | None = None
        # Injected child-tile loader (項目#14).  ``None`` until the host
        # threads one in (``ContentView.set_folder_thumbnail_loader``);
        # without it child tiles settle on their static placeholder glyph
        # instead of decoding (a standalone view never blocks on C03 dots).
        self._child_loader: "ThumbnailLoader | None" = None
        # Dedicated stream for the directory scan + centre decode (項目#14).
        # Owning it (instead of ``QThreadPool.globalInstance()``) makes
        # queued stale work cancellable on a folder switch and stops these
        # tasks from crowding out the lightweight probes that share the
        # global pool.  2 threads: one scan + one decode can overlap,
        # mirroring the old effective concurrency.
        #
        # 投入は**加算的**（``submit_batch``）— 走査の着地が中央画像の
        # デコードを積み、タイルクリックとリサイズ後の高解像度化も積み足す。
        # 世代を畳むのは :meth:`set_folder` / :meth:`clear` /
        # :meth:`shutdown` の ``cancel`` だけで、それが同時に「キュー待ちを
        # 捨てる」「走行中の scandir / post.md 読みへ降りるよう伝える」の
        # 両方を担う（項目#136 — 走行中を止められないと ``~QThreadPool`` が
        # 無期限に待ち、到達不能な NAS を選んだ直後に閉じるとプロセス終了が
        # SMB タイムアウトぶん遅れる）。
        self._stream = GuardedStream(self, max_threads=2)
        self._stream.bind(self._on_preview_landed)
        # Path → (QImage, target_px, is_native) byte-budgeted LRU cache
        # for the CENTRE image (child tiles cache inside the injected
        # loader since 項目#14).  Persists across folder switches so
        # re-visiting a folder (or scrolling back through the right pane)
        # restores the centre thumbnail instantly without re-decoding.
        # Keyed on the *string* form of Path so equality is the same as
        # ``Path(str(p)) == p`` would give on every platform.
        #
        # The ``target_px`` tag tells us at what request size the entry
        # was decoded — a future request at ≤ that size can re-use it.
        # The ``is_native`` flag tells us the source was natively at or
        # below the requested target (no downscale happened), in which
        # case any future request is already satisfied regardless of
        # size.
        self._thumb_cache: BoundedImageCache[tuple[QImage, int, bool]] = (
            BoundedImageCache(
                max_bytes=self._CACHE_MAX_BYTES,
                max_entries=self._CACHE_MAX_ENTRIES,
                max_single_bytes=self._CACHE_MAX_SINGLE_BYTES,
                sizeof=lambda v: v[0].sizeInBytes(),
            )
        )
        # Debounce upgrades when the splitter is being dragged or the
        # window resized — every motion fires resize events, but we
        # only want to spawn a high-res worker once the user pauses.
        self._redecode_timer = Debouncer(
            self,
            self._REDECODE_DEBOUNCE_MS,
            self._maybe_upgrade_main_thumb,
            mode=DebounceMode.TRAILING,
        )
        # ``shutdown`` の 2 層目（``ChildrenGrid`` と同じ形）。タイマーを止めた
        # あとに再武装させる経路（showEvent / DPR 変化 / ラベルのリサイズ）が
        # 3 本あるので、止めるだけでは掃いたプールへ新しいデコードが戻る。
        self._shutdown = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self._title = QLabel()
        self._title.setStyleSheet(
            f"font-size: {FONT_SUBTITLE_PT}pt; font-weight: bold;"
        )
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        # ----- Centre thumbnail (top half of the splitter) ----------------
        self._thumb_label = QLabel()
        self._thumb_label.setAlignment(Qt.AlignCenter)
        self._thumb_label.setMinimumHeight(120)
        self._thumb_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._thumb_label.setStyleSheet(hint_style())
        # The pixmap is rescaled whenever the label's size changes so a
        # splitter drag (or window resize) keeps the centre thumb filling
        # the available area.  An event filter is the cleanest hook —
        # QLabel doesn't expose a resized() signal.
        self._thumb_label.installEventFilter(self)

        # ----- Children grid container (bottom half of the splitter) -----
        grid_container = QWidget()
        grid_layout = QVBoxLayout(grid_container)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(4)
        self._children_label = QLabel(t("viewer.folder_preview_view.children_heading"))
        self._children_label.setStyleSheet(hint_style(font_pt=FONT_BODY_PT))
        grid_layout.addWidget(self._children_label)

        # Square-grid GalleryView: same rendering base as the main panes
        # (aspect-preserving thumb paint inside uniform square cells, dimmed
        # placeholder glyphs, folder badges) — no fixed-canvas compositing
        # or uniform-item-size promises to maintain.  NoFocus keeps this a
        # read-only preview: the window's ←/→ file navigation must never be
        # stolen by the embedded grid's own key handling.
        #
        # ``zoom_handler`` は渡さない（この面にサイズスライダが無いので
        # 変える先が無い）。渡さないと ``GalleryView`` は Ctrl+ホイールを
        # 消費せず ``event.ignore()`` で親へ流すので、ジェスチャは
        # :meth:`wheelEvent` の「ホイール = ファイル送り」へ落ちる — 席の
        # chrome と同じ意味になり、死に手にならない。
        self._grid = GalleryView()
        self._grid.setFocusPolicy(Qt.NoFocus)
        self._grid.configure(
            view_mode="icon",
            thumb_layout="square",
            params=LayoutParams(
                target_size=self._CHILD_ICON_PX,
                spacing=6,
                caption_height=self._CHILD_CAPTION_H,
            ),
        )
        # Clicking (selecting) a child image tile swaps the centre
        # thumbnail — staying in folder-preview mode rather than drilling
        # in.  Folder tiles are deliberately no-op (drilling is reserved
        # for double-click in the right-pane file list).
        self._grid.selection_changed.connect(self._on_child_selected)
        grid_layout.addWidget(self._grid, 1)

        # ----- Vertical splitter wraps thumb + grid container -------------
        # Lets the user drag to enlarge either side; default is biased
        # toward the centre thumbnail per the user's request.
        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setHandleWidth(6)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._thumb_label)
        self._splitter.addWidget(grid_container)
        # ``setStretchFactor`` governs how *future* resizes redistribute
        # space; ``setSizes`` pins the *initial* split.  Use both so the
        # first paint matches the requested ratio and later window
        # resizes preserve it proportionally.  Qt scales the setSizes
        # values to whatever total height the splitter ends up with, so
        # the absolute numbers here are just the desired ratio.
        self._splitter.setStretchFactor(0, self._SPLITTER_INITIAL_RATIO[0])
        self._splitter.setStretchFactor(1, self._SPLITTER_INITIAL_RATIO[1])
        self._splitter.setSizes([
            self._SPLITTER_INITIAL_RATIO[0] * 1000,
            self._SPLITTER_INITIAL_RATIO[1] * 1000,
        ])
        layout.addWidget(self._splitter, 1)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------ API

    def set_thumbnail_loader(self, loader: "ThumbnailLoader | None") -> None:
        """Inject the shared child-tile :class:`ThumbnailLoader` (項目#14).

        The host (``ViewerWindow`` → ``ContentView``) hands in a small
        dedicated loader wired to the persistent disk / folder caches.
        Child tiles are requested through it under the
        ``folderpreview:`` key namespace; its ``loaded`` / ``failed``
        signals route back into the grid.  The loader is drained by the
        window's ``_drain_loader_pools`` at close, so no decode outlives
        the caches it writes to.
        """
        if self._child_loader is not None:
            self._child_loader.loaded.disconnect(self._on_child_loader_loaded)
            self._child_loader.failed.disconnect(self._on_child_loader_failed)
        self._child_loader = loader
        if loader is not None:
            loader.loaded.connect(self._on_child_loader_loaded)
            loader.failed.connect(self._on_child_loader_failed)

    def set_folder(
        self,
        folder: Path,
        *,
        placeholder_icon: QIcon | None = None,
        force: bool = False,
    ) -> None:
        """Render *folder* asynchronously as a non-navigating preview.

        Returns immediately after spawning the scan worker; the grid and
        main thumbnail populate progressively as workers complete.

        *placeholder_icon* (optional) — a low-resolution thumbnail the
        caller already has on screen (typically the right-pane row's
        icon).  Painted into the centre immediately so the user never
        sees an empty preview, and replaced once the high-res decode
        finishes on a worker thread.

        *force* — re-scan even when *folder* matches the current one (e.g.
        an explicit refresh after new files were downloaded into it).
        """
        # Idempotency: if the same folder is being set again (e.g. the
        # scroll-driven ``folder_selected`` signal and the explicit
        # ``show_path`` call in ``_on_content_navigate`` both reach us),
        # leave the in-flight or already-rendered state alone — bumping
        # the generation would discard valid worker results and flash
        # the placeholder back over an already-resolved thumbnail.
        #
        # Two exceptions re-scan the same folder: an explicit ``force``
        # refresh, and recovery after the previous scan *failed* (a
        # transient network blip must not leave the "読み込み失敗" state
        # stuck until the user detours through another folder).
        if folder == self._folder and not force and not self._last_scan_failed:
            return

        # Cancel work for the folder we are leaving: queued child-tile
        # requests are dropped from the loader (項目#14 — in-flight decodes
        # can't be pre-empted, but their results land on keys the rebuilt
        # grid no longer holds, so ``set_thumb`` no-ops them).  ストリームの
        # ``cancel`` はキュー待ちの走査 / 中央デコードを捨て、走行中の 1 本にも
        # 降りるよう伝える（項目#136 — 結果はどのみち世代で落ちるので、
        # 続けさせても NAS I/O を無駄に握るだけ）。着地の選別は ``bind`` が
        # 担うので、スロット側に世代ガードは無い。
        if self._child_loader is not None:
            self._child_loader.discard_pending_outside(set())
        self._stream.cancel()

        self._folder = folder
        self._last_scan_failed = False
        # New folder (or an explicit refresh): forget which representatives
        # were unreadable so a replaced / repaired file gets another chance.
        self._failed_thumb_paths.clear()
        self._main_thumb_path = None
        self._main_thumb_source = None

        self._title.setText(folder.name)
        self._title.setToolTip(str(folder))

        # Seed the main thumb with the caller-provided low-res icon (if
        # any) so the user has something to look at while the high-res
        # worker runs.  Falls back to the "loading" label otherwise.
        if placeholder_icon is not None and not placeholder_icon.isNull():
            # QIcon.pixmap takes *logical* pixels.  The floor is a sensible
            # seed — the cached low-res row icon won't be sharper than this
            # anyway, and the high-res worker upgrades it shortly after.
            pix = placeholder_icon.pixmap(
                self._MAIN_TARGET_PX_FLOOR,
                self._MAIN_TARGET_PX_FLOOR,
            )
            if not pix.isNull():
                self._main_thumb_source = pix
                self._rescale_main_thumb()
        if self._main_thumb_source is None:
            self._thumb_label.setPixmap(QPixmap())
            self._thumb_label.setText(t("common.status.loading"))

        self._children_label.setText(
            t("viewer.folder_preview_view.children_heading_loading")
        )
        self._grid.clear()

        self._stream.submit_batch(
            lambda job, f=folder: _scan_folder_preview(job, f)
        )

    def clear(self) -> None:
        # ``cancel`` invalidates any in-flight worker result and drops the
        # queued (not-yet-started) work outright (項目#14 / #136).
        self._stream.cancel()
        self._redecode_timer.stop()
        if self._child_loader is not None:
            self._child_loader.discard_pending_outside(set())
        self._folder = None
        self._last_scan_failed = False
        self._failed_thumb_paths.clear()
        self._main_thumb_path = None
        self._main_thumb_source = None
        self._title.clear()
        self._title.setToolTip("")
        self._thumb_label.clear()
        self._thumb_label.setText("")
        self._children_label.setText(t("viewer.folder_preview_view.children_heading"))
        self._grid.clear()

    # ----------------------------------------------------------- LRU cache

    def _cache_get(
        self, path: Path,
    ) -> tuple[QImage, int, bool] | None:
        """Return ``(image, target_px, is_native)`` for *path*, or ``None``."""
        return self._thumb_cache.get(str(path))

    def _cache_put(
        self,
        path: Path,
        image: QImage,
        target_px: int,
        is_native: bool,
    ) -> None:
        """Store *image* under *path*, preferring higher-quality decodes.

        An existing native-resolution entry is never replaced — once we
        know the source is naturally at-or-below any reasonable request,
        further decodes can't yield more pixels.  Otherwise the entry
        with the larger ``target_px`` wins so a later 192-px tile decode
        doesn't clobber an earlier 2K-pixel centre decode of the same
        image.
        """
        key = str(path)
        existing = self._thumb_cache.get(key)
        if existing is not None:
            _, ex_target, ex_native = existing
            if ex_native:
                return  # already best-possible
            if ex_target >= target_px:
                return  # equal or higher tier already cached
        self._thumb_cache.put(key, (image, target_px, is_native))

    @staticmethod
    def _cache_satisfies(
        entry: tuple[QImage, int, bool] | None,
        required_target_px: int,
    ) -> bool:
        """True when *entry* needs no upgrade for a *required_target_px* request."""
        if entry is None:
            return False
        _, target_px, is_native = entry
        return is_native or target_px >= required_target_px

    # --------------------------------------------- target-size resolution

    def _effective_dpr(self) -> float:
        """DPR that respects both the widget and its screen.

        ``QWidget.devicePixelRatioF`` returns 1.0 until the widget has
        actually been mapped to a screen, which would silently make
        every initial decode soft on HiDPI displays.  Falling back to
        the screen's own ratio keeps quality correct on the first
        paint, and harmlessly reports the higher value when both agree.
        """
        widget_dpr = self.devicePixelRatioF()
        screen = self.screen() or QApplication.primaryScreen()
        screen_dpr = screen.devicePixelRatio() if screen is not None else 1.0
        return max(widget_dpr, screen_dpr) or 1.0

    def _required_main_target_px(self) -> int:
        """Return the centre thumbnail's required *physical*-pixel target.

        The result is ``max(label_width, label_height) × dpr`` — the
        longest side a ``setScaledSize`` decode needs to satisfy.  When
        the widget hasn't been laid out yet, falls back to
        :attr:`_MAIN_TARGET_PX_FLOOR`; later resizes trigger
        :meth:`_maybe_upgrade_main_thumb` via the debounce timer once
        the real geometry is known.
        """
        label_size = self._thumb_label.size()
        longest_logical = max(label_size.width(), label_size.height())
        dpr = self._effective_dpr()
        if longest_logical < 64:
            # Not yet laid out — pick a sane default; will be upgraded
            # on the first resize event after the widget is shown.
            return round(self._MAIN_TARGET_PX_FLOOR * dpr)
        physical = round(longest_logical * dpr)
        return max(self._MAIN_TARGET_PX_FLOOR, physical)

    # ----------------------------------------------- background upgrade

    def _maybe_upgrade_main_thumb(self) -> None:
        """Spawn a higher-resolution decode if the current cache is short.

        Called from the debounce timer after splitter / window resize
        settles.  No-op when the cache already has a native-resolution
        entry or a target large enough to satisfy the new label size.
        """
        if self._shutdown or self._main_thumb_path is None:
            return
        if str(self._main_thumb_path) in self._failed_thumb_paths:
            # Already proven undecodable in this folder session — a bigger
            # box won't change that (09-03 #197).
            return
        if self._main_thumb_path.suffix.lower() not in IMAGE_SUFFIXES:
            # Non-image representative (e.g. a PDF): ``_on_scan_ready`` showed
            # the label-only fallback and never decoded it — a resize must not
            # spawn a pointless full decode (nor overwrite the label with
            # whatever QImageReader makes of a PDF).
            return
        required = self._required_main_target_px()
        cached = self._cache_get(self._main_thumb_path)
        if self._cache_satisfies(cached, required):
            return
        self._submit_main_thumb(self._main_thumb_path, required)

    def _submit_main_thumb(self, path: Path, target_px: int) -> None:
        """中央画像のデコードを**積み足す**（先行を捨てない）。

        走査の着地・タイルクリック・リサイズ後の高解像度化がここへ集まる。
        追い越しにすると、走査が積んだ 1 本目をタイルクリックが黙って捨てる
        （= 中央ラベルが読み込み中のまま残る）。集合ごと畳むのはフォルダ
        切替 / クリア / 窓じまいだけ。
        """
        self._stream.submit_batch(
            lambda job, p=path, px=target_px: _load_main_thumb(job, p, px)
        )

    # ----------------------------------------------------------- slot impls

    def _on_preview_landed(self, payload: object) -> None:
        """走査 / 中央画像の着地（4 つの結末を受ける 1 本口）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_PreviewKind, payload.kind)
        match kind:
            case "scan":
                scan = cast("tuple[Path | None, list]", payload.value)
                self._apply_scan(*scan)
            case "scan_failed":
                self._apply_scan_failed(cast(str, payload.value))
            case "thumb":
                thumb = cast(
                    "tuple[QImage, Path, int, bool]", payload.value
                )
                self._apply_main_thumb(*thumb)
            case "thumb_failed":
                self._apply_main_thumb_failed(cast(Path, payload.value))
            case _:
                assert_never(kind)

    def _apply_scan(
        self,
        thumb_path: Path | None,
        entries: list,
    ) -> None:
        self._main_thumb_path = thumb_path

        # Kick off the main thumbnail decode (or label-only fallback) as
        # soon as we know the path.  It races the child-tile decodes.
        if thumb_path is None:
            if self._main_thumb_source is None:
                # No placeholder either — show "no thumbnail" label.
                self._thumb_label.setPixmap(QPixmap())
                self._thumb_label.setText(
                    t("viewer.folder_preview_view.thumb_none")
                )
        elif thumb_path.suffix.lower() not in IMAGE_SUFFIXES:
            # Non-image candidate (e.g. PDF first page) — label-only.
            # Drop the seeded placeholder as well: ``_rescale_main_thumb``
            # (resize / splitter drag / showEvent) repaints
            # ``_main_thumb_source`` unconditionally and would blow the
            # right pane's low-res row icon up over this label the moment
            # the widget is resized.  Same release ``_on_main_thumb_failed``
            # performs.
            self._main_thumb_source = None
            self._thumb_label.setPixmap(QPixmap())
            self._thumb_label.setText(
                t("viewer.folder_preview_view.thumb_non_image", name=thumb_path.name)
            )
        else:
            required = self._required_main_target_px()
            cached = self._cache_get(thumb_path)
            if self._cache_satisfies(cached, required):
                # Cache satisfies the current size — paint and skip the worker.
                self._main_thumb_source = QPixmap.fromImage(cached[0])
                self._rescale_main_thumb()
            else:
                if cached is not None:
                    # Use the lower-quality cache as a placeholder.
                    self._main_thumb_source = QPixmap.fromImage(cached[0])
                    self._rescale_main_thumb()
                self._submit_main_thumb(thumb_path, required)

        # Order: directories first (alphabetical), then files
        # (alphabetical).  ``scan_children`` returns mixed entries with
        # placeholder titles for folders — entry.title is just the
        # filename in that case, so a case-insensitive sort is enough.
        dirs = sorted(
            (e for e in entries if e.is_dir),
            key=lambda c: c.title.lower(),
        )
        files = sorted(
            (e for e in entries if not e.is_dir),
            key=lambda c: c.title.lower(),
        )
        ordered = dirs + files

        shown_entries = ordered[: self._MAX_CHILDREN]
        skipped_count = len(ordered) - len(shown_entries)

        # Image children to request from the injected loader (項目#14 —
        # requested only after set_tiles so every tile is in place before
        # a synchronous cache-hit emit can post back).
        thumb_targets: list[tuple[str, Path]] = []
        # Tiles no decode will ever settle: sub-folders (their
        # representative image resolution is a panes-side concern), files
        # ``scan_children`` tagged as thumbnailable that this preview
        # doesn't decode (PDF / video), and — with no loader injected —
        # image files too.  ``GalleryView`` treats a tile with a thumbnail
        # source — or an unresolved folder — as "a decode is in flight"
        # and paints the 「読み込み中」 dots until the host settles it, so
        # say so up front or they stay pending forever (C03).
        never_decoded: list[str] = []
        tiles: list[Tile] = []
        for entry in shown_entries:
            path = entry.path
            key = str(path)
            # エントリ → タイルの写像は席をまたぐ 1 実装（``build_tile``）。
            # この席が決めるのは名前空間・キャプション・ツールチップだけで、
            # 「薄く描く」「代替サムネイルの警告枠」等の表示属性は左右ペインと
            # 同じ規則で決まる（手組みだと片側だけ欠ける形が繰り返し出る）。
            tiles.append(build_tile(
                entry, key=key, caption=path.name, tooltip=str(path),
            ))
            if (
                not entry.is_dir
                and path.suffix.lower() in IMAGE_SUFFIXES
                and self._child_loader is not None
            ):
                thumb_targets.append((key, path))
            else:
                never_decoded.append(key)
        self._grid.set_tiles(tiles)
        for key in never_decoded:
            self._grid.mark_thumb_failed(key)

        if not shown_entries:
            self._children_label.setText(
                t("viewer.folder_preview_view.children_heading_empty")
            )
        elif skipped_count > 0:
            self._children_label.setText(
                t(
                    "viewer.folder_preview_view.children_heading_truncated",
                    shown=len(shown_entries),
                    skipped=skipped_count,
                )
            )
        else:
            self._children_label.setText(
                t("viewer.folder_preview_view.children_heading")
            )

        # Child-tile thumbnails via the injected loader (項目#14): the
        # loader owns caching (in-memory LRU + persistent disk masters),
        # DPR scaling and the bounded worker pool.  A cache hit emits
        # ``loaded`` synchronously on this same stack — tiles are already
        # set above, so the slot lands correctly.
        if self._child_loader is not None and thumb_targets:
            size = QSize(self._CHILD_REQUEST_EDGE, self._CHILD_REQUEST_EDGE)
            dpr = self._effective_dpr()
            for key, path in thumb_targets:
                loader_key = self._CHILD_KEY_PREFIX + key
                self._child_loader.request(loader_key, path, size, dpr=dpr)
                idx = self._grid.index_of_key(key)
                tile = self._grid.tile_at(idx) if idx is not None else None
                if tile is not None and not tile.thumb_loaded:
                    # ソースがボックスより小さいキーの再要求にローダーは
                    # 沈黙する（thumbnail_loader #10 — 呼び出し側が既に絵を
                    # 持っている前提）。作り直したタイルは持っていないので、
                    # 常駐デコードがあれば ``cached_image`` から再シードする
                    # （他ホストと同じ流儀）。
                    image = self._child_loader.cached_image(loader_key)
                    if image is not None and not image.isNull():
                        self._grid.set_thumb(key, QPixmap.fromImage(image))

    def _apply_scan_failed(self, message: str) -> None:
        # Mark the failure so a re-click on the same folder retries rather
        # than being short-circuited by the idempotency guard in set_folder.
        self._last_scan_failed = True
        logger.warning("Folder preview scan failed: {}", message)
        self._children_label.setText(
            t(
                "viewer.folder_preview_view.children_heading_scan_failed",
                message=message,
            )
        )
        self._thumb_label.setText(t("viewer.folder_preview_view.thumb_load_failed"))

    def _apply_main_thumb(
        self,
        image: QImage,
        path: Path,
        target_px: int,
        is_native: bool,
    ) -> None:
        if image is None or image.isNull():
            return
        # Always cache — even if the user has since clicked a different
        # tile, this decode is still valuable for the next visit.
        self._cache_put(path, image, target_px, is_native)
        # But only paint when the path still matches the centre — a
        # tile-click may have superseded the original main-thumb load
        # while it was still on the worker thread.
        if path != self._main_thumb_path:
            return
        self._main_thumb_source = QPixmap.fromImage(image)
        self._rescale_main_thumb()

    def _apply_main_thumb_failed(self, path: Path) -> None:
        if path != self._main_thumb_path:
            return
        # Recorded AFTER the stream's own guard and the path check, so a
        # straggler from a folder the user already left never bans a path
        # in this one.
        self._failed_thumb_paths.add(str(path))
        self._main_thumb_source = None
        self._thumb_label.setPixmap(QPixmap())
        self._thumb_label.setText(t("viewer.folder_preview_view.thumb_unreadable"))

    def _on_child_loader_loaded(self, key: str, image: QImage) -> None:
        """A child-tile decode from the injected loader landed (項目#14).

        Staleness needs no generation counter here: keys embed the child's
        absolute path, and ``GalleryView.set_thumb`` no-ops on a key the
        current grid doesn't hold — a late result from a folder the user
        already left simply misses.
        """
        if not key.startswith(self._CHILD_KEY_PREFIX):
            return  # not ours (a shared loader serving another consumer)
        if image is None or image.isNull():
            return
        tile_key = key[len(self._CHILD_KEY_PREFIX):]
        self._grid.set_thumb(tile_key, QPixmap.fromImage(image))

    def _on_child_loader_failed(self, key: str) -> None:
        """Settle a child tile whose decode failed (C03).

        Without this a broken / truncated image keeps painting the
        「読み込み中」 dots for the whole session — ``_tile_awaits_thumb``
        only clears once the host reports success or failure.
        """
        if not key.startswith(self._CHILD_KEY_PREFIX):
            return
        self._grid.mark_thumb_failed(key[len(self._CHILD_KEY_PREFIX):])

    def _on_child_selected(self, index: int) -> None:
        """User clicked a child tile — promote it to the centre thumbnail.

        Folder tiles and non-image files are no-ops.  For images, the
        tile's already-decoded grid thumbnail is used as an immediate
        low-res placeholder while the centre-resolution worker decodes
        the full version.
        """
        tile = self._grid.tile_at(index)
        if tile is None or tile.is_dir:
            return
        path = tile.path
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            return
        if path == self._main_thumb_path:
            # Already the centre thumbnail.
            return

        self._main_thumb_path = path
        required = self._required_main_target_px()
        cached = self._cache_get(path)
        if self._cache_satisfies(cached, required):
            # Cache is good enough for the current centre size — paint
            # and skip the worker.
            self._main_thumb_source = QPixmap.fromImage(cached[0])
            self._rescale_main_thumb()
            return

        # Cache miss or undersized: show the best placeholder we have
        # (cached lower-res decode, or the grid tile's thumbnail)
        # immediately and queue a centre-resolution decode in the
        # background.
        placeholder_pix: QPixmap | None = None
        if cached is not None:
            placeholder_pix = QPixmap.fromImage(cached[0])
        elif tile.pixmap is not None and not tile.pixmap.isNull():
            placeholder_pix = tile.pixmap
        if placeholder_pix is not None:
            self._main_thumb_source = placeholder_pix
            self._rescale_main_thumb()

        self._submit_main_thumb(path, required)

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """走行中のプレビュー読みを止めてストリームを有界に空にする（項目#136）.

        ``ViewerWindow.closeEvent`` が ``_zip_drill`` / ``_cache_ctrl`` /
        サムネローダーに掛けている close 前ドレインの、このビュー版。
        ``QThreadPool`` のデストラクタは走行中の ``QRunnable`` が終わるまで
        呼び出しスレッドを**無期限に**ブロックするので、これが無いと到達
        不能な NAS フォルダを選んだ直後に閉じたとき、窓が消えた後の破棄
        シーケンス（``viewer/app.py`` の局所変数解放）が SMB タイムアウト
        ぶん止まる。「協調キャンセル → キュー破棄 → 有界待ち」は
        :meth:`~._runnable.GuardedStream.request_shutdown` の 1 本に畳んで
        あり、待ちは有界 — 諦めてもワーカーはキャッシュへ書かない
        （``read_folder_preview_checked`` / ``scan_children`` は読み取り
        だけ）ので、閉じたストアへの書き込み事故にはならない。
        """
        self._shutdown = True
        self._redecode_timer.stop()
        if not self._stream.request_shutdown(max(0, timeout_ms)):
            logger.warning(
                "フォルダプレビューのワーカーが {}ms で終わりませんでした",
                timeout_ms,
            )

    def refresh_fit(self) -> None:
        """中央画像のフィットを測り直す（設定コミット後の反映点 — 項目#61）.

        F03「フィット表示: 等倍以上に拡大しない」は ``view_prefs`` の
        モジュール変数を live に読むので値そのものは即座に効くが、既に
        表示している 1 枚は次のラベルリサイズまで描き直されない。中央ペインの
        ``ImageView.refresh_fit`` と同じ役割の反映点を持たせ、
        ``ContentView.apply_view_settings`` から呼ばせる。
        """
        self._rescale_main_thumb()

    # ----------------------------------------------------------- internals

    def _rescale_main_thumb(self) -> None:
        if self._shutdown:
            return
        pix = self._main_thumb_source
        if pix is None or pix.isNull():
            return
        # Match :class:`ImageView` 's DPR-aware paint path: scale to the
        # label's *physical* pixel box so the source is rendered 1:1
        # against the display's true resolution, then stamp the DPR on
        # the resulting pixmap so QLabel still lays it out at logical
        # size.  Skipping ``setDevicePixelRatio`` here is what made the
        # 4K case look soft — Qt would otherwise treat a physical-
        # resolution pixmap as logical and downscale it again.
        dpr = self._effective_dpr()
        max_w_logical = max(120, self._thumb_label.width() - 8)
        max_h_logical = max(120, self._thumb_label.height() - 8)
        max_w_phys = max(1, round(max_w_logical * dpr))
        max_h_phys = max(1, round(max_h_logical * dpr))
        if view_prefs.get_image_fit_no_upscale():
            # F03「フィット表示: 等倍以上に拡大しない」(既定 ON) はこの面にも
            # 効く（項目#61）。``QPixmap.scaled`` は指定サイズへ**拡大もする**
            # ので、クランプしないと 2×3px の ``#thumb#`` アイコンのような
            # 小さい代表画像がラベル箱いっぱいの単色ブロックに引き伸ばされる
            # — 同じ画像を中央 ImageView で開くと等倍のまま出るのに、
            # フォルダプレビューだけ設定を無視していた（対実装の片側欠落）。
            max_w_phys = min(max_w_phys, max(1, pix.width()))
            max_h_phys = min(max_h_phys, max(1, pix.height()))
        scaled = pix.scaled(
            max_w_phys, max_h_phys,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        if dpr > 1.0:
            scaled.setDevicePixelRatio(dpr)
        self._thumb_label.setPixmap(scaled)
        self._thumb_label.setText("")
        # If the cache is now short of what the displayed area needs,
        # ask for a higher-resolution decode after the splitter / window
        # finishes resizing.  Debounced so dragging at 60 Hz doesn't
        # spawn dozens of workers — only the final size matters.
        if self._main_thumb_path is not None:
            self._redecode_timer.trigger()

    # --------------------------------------------------------- event filter

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        # Re-fit the centre pixmap whenever the splitter (or the outer
        # window resize) changes the thumb label's geometry.  QLabel
        # has no built-in ``resized`` signal, hence the filter.
        if obj is self._thumb_label and event.type() == QEvent.Resize:
            self._rescale_main_thumb()
        return super().eventFilter(obj, event)

    def closeEvent(self, event):  # noqa: N802 (Qt API)
        """ペインを閉じる = 背景の読みを止める（``ChildrenGrid`` と同型）.

        ``shutdown`` は窓の ``closeEvent`` からしか届いていなかったので、窓を
        伴わずにペインだけ閉じると専用プールの走行中ワーカーが残り、C++ の
        デストラクタが**無期限に**それを待つ（``~QThreadPool``）。``shutdown``
        は純粋なキャンセルで冪等なので、ここでも呼んで二重に困ることはない。
        """
        self.shutdown()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802 (Qt API)
        super().showEvent(event)
        # close → show の往復で再武装する（``_shutdown`` は「閉じている」状態で
        # あって一方通行の停止スイッチではない — ``ChildrenGrid`` と同じ扱い）。
        self._shutdown = False
        # ``devicePixelRatioF`` only returns the *real* screen ratio once
        # the widget has been mapped to a window manager.  Until then it
        # reports 1.0, which would silently make every first decode soft
        # on a 4K display.  Trigger the upgrade-check timer on the first
        # show so the (now-correct) DPR is folded into the required
        # target and any too-low-resolution main thumb is re-decoded.
        if self._main_thumb_path is not None:
            self._redecode_timer.trigger()

    def changeEvent(self, event):  # noqa: N802 (Qt API)
        # Multi-monitor setups: if the user drags the snappix window from
        # a 1.0-DPR display to a 4K (2.0-DPR) one, the cached decode is
        # now soft for the new screen.  Re-trigger the upgrade check.
        # ``DevicePixelRatioChange`` fires on the widget when its
        # effective DPR changes (Qt 6.0+).
        if event.type() == QEvent.DevicePixelRatioChange:
            if not self._shutdown and self._main_thumb_path is not None:
                self._redecode_timer.trigger()
        super().changeEvent(event)

    # ------------------------------------------------------------- events

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._rescale_main_thumb()

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        # Wheel events that reach the view itself (cursor over the title,
        # centre thumbnail or children-label area — anywhere *outside*
        # the grid) always advance the right-pane selection, mirroring
        # :class:`FileInfoView`'s "any wheel = navigate" semantics.  The
        # children :class:`GalleryView` consumes plain wheels internally for
        # its own scrolling, so this normally only triggers on the
        # surrounding chrome — matching the user's intent of "scroll outside
        # the list → move to the next file like a normal image preview".
        # Ctrl+wheel over the grid also lands here: that seat takes no
        # ``zoom_handler`` (no size slider to move), so the grid declines the
        # gesture instead of swallowing it, and the seat's own meaning wins.
        if navigate_on_wheel(event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)
