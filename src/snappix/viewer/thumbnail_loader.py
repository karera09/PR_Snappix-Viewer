"""Async thumbnail loading with bounded LRU cache.

Uses ``QThreadPool`` so multiple thumbnails can be decoded in parallel.
Critically, ``QPixmap`` is NOT thread-safe — workers must produce ``QImage``
and emit it back to the main thread, which then converts via
``QPixmap.fromImage(...)``.

Large source images (4K+) are scaled at decode time via
``QImageReader.setScaledSize`` so we never materialise the full pixel buffer.

Parallelism is deliberately modest (4 workers by default).  Stacking this
on top of the metadata-scan pool hammered NAS shares hard enough that even
mouse events back on the main thread arrived seconds late — the SMB client
runs out of credits and every round-trip stalls.  Four thumbnail workers
alongside six metadata workers is the sweet spot we've seen: fast enough
to keep thumbnails streaming in, slack enough to keep the UI responsive.
"""

from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from PIL import Image, UnidentifiedImageError
from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QPainter

from ..common.ui.timers import DebounceMode, Debouncer
from .folder_scan import PDF_SUFFIXES, VIDEO_SUFFIXES, read_folder_preview_cached
from .image_scale import pil_to_qimage, qimage_to_pil, smooth_downscale
from .perf import measure, recorder
from .qimage_decode import (
    decode_qimage_bytes,
    ensure_resamplable,
    open_pil_oriented,
    read_file_bytes,
)

if TYPE_CHECKING:  # annotations only — avoid import cycles at runtime
    from .folder_preview_cache import FolderPreviewCache
    from .thumb_disk_cache import ThumbDiskCache

# Pillow 10+ relocated resampling constants under ``Image.Resampling``.
_PIL_LANCZOS = Image.Resampling.LANCZOS

# Default longest edge (px) of the master thumbnail persisted to the disk
# cache.  The view scales the master down to each tile's box; tiles needing
# more pixels than this bypass the cache and decode the original at the
# exact size, so a cached thumb is never upscaled (never blurry).
_DEFAULT_CACHE_EDGE = 1024
_WEBP_QUALITY = 88

# Tolerance (physical px, longest edge) for the "source-limited" cache
# classification in :meth:`ThumbnailLoader.request`.  An aspect-preserving
# downscale can round the produced longest edge to just under the requested
# box edge even though the source has plenty of pixels (box width comes from
# ``round(row_height × aspect)``, PIL/Qt round the fitted edge again) — such
# a near-miss must NOT be treated as "the source can't do better", or every
# later upgrade request is silently swallowed and the tile stays blurry
# until the cache clears (#18).  2 px covers both rounding stages.
_SOURCE_LIMIT_SLACK = 2


def fitted_edge(have: QSize, box: QSize) -> float:
    """Longest edge *have* reaches when drawn into *box* keeping its aspect.

    The single ruler for "does this raster still have enough pixels for that
    box" — shared by the loader's cache-adequacy / source-limited checks and
    by ``ChildrenGrid``'s resolution-upgrade check, so the two sides can never
    disagree about what was asked for and what was produced.

    Comparing the box's longest edge against the raster's (the previous
    ruler) breaks on anisotropic boxes: a list row is ~526×24 logical px, the
    decode is pinned by the 24 px side, yet 526 got recorded as the requested
    resolution — after which every grid-sized re-request was dismissed as
    "the source can't do better" and the tile stayed at row resolution
    forever (#F1D-1).
    """
    hw, hh = have.width(), have.height()
    if hw <= 0 or hh <= 0:
        return 0.0
    return max(hw, hh) * min(box.width() / hw, box.height() / hh)


@dataclass(frozen=True)
class _PendingSpec:
    """Captured arguments for a deferred thumbnail decode.

    Stored in the loader's pending queues until a worker becomes free.
    Frozen so two queues can hold the same spec without worrying about
    mutation, and because we never need to edit an entry in place
    (re-requesting the same key is a no-op when already pending).
    """

    path: Path
    phys_size: QSize
    resolve_in_folder: bool
    dpr: float
    # Folder mtime for the preview-cache key (only meaningful when
    # ``resolve_in_folder`` is True); 0 means "don't cache the resolution".
    folder_mtime: float = 0.0

class _Signals(QObject):
    loaded = Signal(str, QImage)  # (key, image)
    failed = Signal(str)


class _ThumbnailTask(QRunnable):
    def __init__(
        self,
        key: str,
        path: Path,
        size: QSize,
        signals: _Signals,
        resolve_in_folder: bool = False,
        dpr: float = 1.0,
        disk_cache=None,
        cache_edge: int = _DEFAULT_CACHE_EDGE,
        folder_cache=None,
        folder_mtime: float = 0.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.key = key
        self.path = path
        # size is the *physical*-pixel target (caller has already multiplied
        # by dpr); dpr is stamped on the resulting QImage so that
        # ``QPixmap.fromImage()`` on the GUI thread produces a hi-DPI pixmap
        # that renders 1:1 at the screen's physical resolution.
        self.size = size
        self.signals = signals
        self.resolve_in_folder = resolve_in_folder
        self.dpr = dpr
        # Optional ThumbDiskCache.  When present and the requested size fits
        # within ``cache_edge``, a decoded master is persisted / reused so
        # folder revisits paint from local disk instead of the NAS.
        self._disk_cache = disk_cache
        self._cache_edge = max(1, int(cache_edge))
        # Optional FolderPreviewCache: caches the folder→representative-image
        # resolution so ``resolve_in_folder`` skips the scandir on revisit.
        self._folder_cache = folder_cache
        self.folder_mtime = folder_mtime
        # Shared teardown flag from the owning loader.  Only consulted at
        # coarse boundaries (task start, video wait) — a set event means the
        # loader is shutting down and results are no longer wanted.
        self._cancel_event = cancel_event
        self.setAutoDelete(True)

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    def run(self) -> None:  # noqa: D401 (Qt API)
        rec = recorder()
        task_start = time.perf_counter() if rec.is_enabled() else 0.0
        try:
            if self._cancelled():
                # Loader is shutting down — don't start a decode whose result
                # nobody will consume (and whose signals object may already be
                # gone).  The emit is best-effort for the same reason.
                self._emit_failed_safe()
                return
            path = self.path
            if self.resolve_in_folder:
                # Lazy thumbnail resolution: caller only has a folder path,
                # scan its contents on this worker thread instead of in the
                # directory-scan pass.  The preview cache front skips the
                # scandir on a revisit and persists the result on a miss, so
                # the right pane's folder rows warm up like the left pane's.
                with measure("thumb_resolve_folder", str(path)):
                    # ``should_cancel`` は必須（#F2B-2）。これが無いと
                    # ``request_shutdown()`` が BFS 降下中のワーカーへ届かず、
                    # 到達不能な共有では max_dirs(32) 回ぶんの scandir を完走
                    # するまで抜けられない ＝ closeEvent の bounded drain が
                    # 収束せず、close 済みキャッシュの窓を跨いでプールスレッド
                    # が生き残る。他の呼び出し元（scan_children / post_grid /
                    # scan_search 等）は全て述語を渡している。
                    _parsed, marker, non_marker, _names = read_folder_preview_cached(
                        path, self.folder_mtime, self._folder_cache,
                        should_cancel=self._cancelled,
                    )
                resolved = marker if marker is not None else non_marker
                if resolved is None:
                    rec.record("thumb_failed", 0.0, f"resolve_in_folder None: {path}")
                    self.signals.failed.emit(self.key)
                    return
                path = resolved

            image = self._produce(path)
            if image is None or image.isNull():
                rec.record("thumb_failed", 0.0, f"decode null: {path}")
                self.signals.failed.emit(self.key)
                return
            # Stamp the DPR so Qt treats the raster as hi-DPI when it lands
            # in a QPixmap / QIcon — target pixel counts are already in
            # physical space, this just tells Qt the *logical* size.
            if self.dpr > 1.0:
                image.setDevicePixelRatio(self.dpr)
            if task_start:
                dt_ms = (time.perf_counter() - task_start) * 1000.0
                rec.record("thumb_task_total", dt_ms, str(path))
            self.signals.loaded.emit(self.key, image)
        except Exception as exc:  # pragma: no cover (QImageReader is robust)
            logger.warning("Thumbnail decode failed for {}: {}", self.path, exc)
            recorder().record("thumb_failed", 0.0, f"exception: {self.path}")
            # During teardown the signals QObject can be C++-deleted while
            # this worker is still finishing — emitting then raises out of
            # ``run()`` and Qt logs a scary "Error calling Python override"
            # even though nothing is wrong.  Swallow that one case.
            self._emit_failed_safe()

    def _emit_failed_safe(self) -> None:
        try:
            self.signals.failed.emit(self.key)
        except RuntimeError:  # signals object already deleted (teardown)
            pass

    # ----------------------------------------------------- tiered produce

    def _produce(self, path: Path) -> QImage | None:
        """Produce the final box-sized QImage, using the disk cache tier.

        * Disk cache off, or the requested size exceeds ``cache_edge``
          (huge slider / hi-DPI big tile): decode the original at the exact
          box size — never upscales a cached master, so never blurry.
        * Otherwise: serve a downscaled master from the disk cache (local,
          no NAS), regenerating + persisting it on a miss.
        """
        required = max(self.size.width(), self.size.height())
        if self._disk_cache is None or required > self._cache_edge:
            return self._decode_to_size(path)
        return self._produce_cached(path, required)

    def _produce_cached(self, path: Path, required_edge: int) -> QImage | None:
        try:
            st = path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            # Can't validate the cache without a stat — decode directly.
            return self._decode_to_size(path)

        try:
            blob = self._disk_cache.lookup(path, mtime, size, required_edge)
        except Exception as exc:  # pragma: no cover (cache read best-effort)
            # The cache is pure acceleration: a store that cannot answer must
            # degrade to a plain decode, never to a failed tile.  Every other
            # sqlite-cache call in the viewer is already wrapped this way; this
            # one was not, so a store-level failure (a full disk wedging the
            # LRU flush, a lock, a schema surprise) surfaced as "every tile in
            # the grid failed" even though the originals decode fine.
            logger.debug("thumb cache lookup failed for {}: {}", path, exc)
            blob = None
        if blob is not None:
            img = self._load_cached_master(blob)
            if img is not None and not img.isNull():
                return img
            # Corrupt / unreadable blob — fall through and regenerate.

        master, data = self._decode_master_pil(path)
        if master is None:
            suffix = path.suffix.lower()
            if suffix in VIDEO_SUFFIXES or suffix in PDF_SUFFIXES:
                # Video / PDF already went through their dedicated decoder
                # above and it failed.  ``_decode_to_size`` dispatches on the
                # same suffix, so it would run the SAME decoder a second time
                # — for a corrupt / unsupported clip that means paying the
                # ``_decode_video`` 8 s QEventLoop timeout twice, occupying one
                # of the 4 worker threads for 16 s per file and jamming the
                # whole pool when several broken videos are on screen (#90).
                # The fallback below exists for *images* Pillow can't open
                # (AVIF without the plugin), which the ladder inside
                # ``_decode`` can still handle.
                return None
            # Pillow couldn't build a master (e.g. AVIF without plugin) —
            # fall back to the QImageReader path and skip caching.  The bytes
            # the master attempt already read are handed over so the fallback
            # doesn't pay a second NAS round-trip on exactly the files that
            # are slowest to give up (truncated / extension-spoofed / 0-byte);
            # ``skip_pillow`` likewise skips a second *decode* attempt that is
            # refuted by definition — for a truncated JPEG that attempt is a
            # full DCT decode up to the cut, not a header peek.
            return self._decode_to_size(path, data=data, skip_pillow=True)

        long_edge = max(master.width, master.height)
        full = long_edge < self._cache_edge
        try:
            buf = io.BytesIO()
            save_img = master if master.mode in ("RGB", "RGBA") else master.convert("RGBA")
            save_img.save(buf, "WEBP", quality=_WEBP_QUALITY, method=4)
            self._disk_cache.store(
                path, mtime, size, buf.getvalue(),
                edge=long_edge, full=full,
            )
        except Exception as exc:  # pragma: no cover (cache write best-effort)
            logger.debug("thumb cache store failed for {}: {}", path, exc)
        return self._pil_to_box_qimage(master)

    def _decode_master_pil(
        self, path: Path,
    ) -> "tuple[Image.Image | None, bytes | None]":
        """Decode *path* into a PIL master image bounded by ``cache_edge``.

        Handles images (Pillow), video frames, and PDF page 0.  Returns
        ``(None, ...)`` when Pillow can't open the source so the caller can
        fall back to the QImageReader decode path (which is never cached).

        The second element is the file bytes when this attempt read them (the
        image branch only — video / PDF go through their own decoders and
        return ``None`` there), so a caller that falls back can reuse them
        instead of reading the file a second time.
        """
        suffix = path.suffix.lower()
        edge = self._cache_edge
        if suffix in VIDEO_SUFFIXES:
            qimg = self._decode_video(path)
            if qimg is None or qimg.isNull():
                return None, None
            pil = qimage_to_pil(qimg)
        elif suffix in PDF_SUFFIXES:
            qimg = self._decode_pdf(path, target=QSize(edge, edge))
            if qimg is None or qimg.isNull():
                return None, None
            pil = qimage_to_pil(qimg)
        else:
            pil, data = self._decode_image_pil(path)
            if pil is None:
                return None, data
        if max(pil.width, pil.height) > edge:
            # Palette / bilevel masters must leave their mode before the
            # downscale — Pillow forces NEAREST otherwise and the destroyed
            # pixels get baked into the persisted WebP master (which is
            # RGBA-converted at save time anyway, so nothing is lost here).
            pil = ensure_resamplable(pil)
            pil.thumbnail((edge, edge), _PIL_LANCZOS)
        return pil, None

    def _decode_image_pil(
        self, path: Path,
    ) -> "tuple[Image.Image | None, bytes | None]":
        """Open *path* via Pillow, bounded by ``cache_edge`` (EXIF-transposed).

        Delegates to the shared :func:`qimage_decode.open_pil_oriented`
        core — mode is preserved (no RGBA conversion) so the WebP master
        keeps the source's channel layout.  ``max_edge`` is the master edge
        the caller (:meth:`_decode_master_pil`) shrinks to anyway, so Pillow
        can ``draft()`` a JPEG down at decode time instead of materialising
        the full-resolution buffer first (#48).

        Returns ``(image, data)``: the bytes are returned alongside so a
        failed decode doesn't force the fallback path to re-read the file.
        """
        with measure("thumb_decode_pil", str(path)):
            data = read_file_bytes(path)
            if data is None:
                return None, None
            return open_pil_oriented(data, path, max_edge=self._cache_edge), data

    def _pil_to_box_qimage(self, pil: "Image.Image") -> QImage | None:
        """Downscale a PIL master to ``self.size`` and convert to QImage.

        **Consumes** *pil*: ``thumbnail`` downscales in place, so the caller
        must not use the image afterwards (both call sites hand over a master
        whose lifetime ends here — the WebP encode already happened, and the
        cache-hit path closes its handle right after).  The defensive
        ``copy()`` this used to make was a full-master pixel copy per
        thumbnail (~2.4 MB / 0.6 ms for a 1024 px master) that nothing ever
        read back (#146); if a future caller needs the master intact, it
        should copy at the call site.
        """
        tw = self.size.width()
        th = self.size.height()
        work = pil
        if tw > 0 and th > 0 and (work.width > tw or work.height > th):
            work = ensure_resamplable(work)
            work.thumbnail((tw, th), _PIL_LANCZOS)
        return pil_to_qimage(work)

    def _load_cached_master(self, blob: Path) -> QImage | None:
        """Load a cached WebP master and downscale to ``self.size``."""
        try:
            with measure("thumb_cache_hit", str(blob)):
                with Image.open(blob) as im:
                    im.load()
                    return self._pil_to_box_qimage(im)
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            logger.debug("cached thumb unreadable {} ({})", blob, exc)
            return None

    def _decode_to_size(
        self, path: Path, *, data: bytes | None = None,
        skip_pillow: bool = False,
    ) -> QImage | None:
        """Decode *path* straight to ``self.size`` (the un-cached path).

        This is the original decode pipeline: video frame / PDF page / image
        via Pillow-then-QImageReader, followed by a forced ``smooth_downscale``
        so the QImage shipped to the GUI thread is always small.

        *data* is the file's bytes when the caller already read them (the
        master-decode fallback in :meth:`_produce_cached`); it only ever
        reaches the image branch, since the video / PDF decoders take the
        path itself.  *skip_pillow* travels with it for the same reason.
        """
        if path.suffix.lower() in VIDEO_SUFFIXES:
            image = self._decode_video(path)
        elif path.suffix.lower() in PDF_SUFFIXES:
            image = self._decode_pdf(path)
        else:
            image = self._decode(path, data=data, skip_pillow=skip_pillow)
        if image is None or image.isNull():
            return None
        tw = self.size.width()
        th = self.size.height()
        if tw > 0 and th > 0 and (image.width() > tw or image.height() > th):
            with measure("thumb_scale", str(path)):
                image = smooth_downscale(image, QSize(tw, th))
        return image

    def _decode(
        self, path: Path, *, data: bytes | None = None,
        skip_pillow: bool = False,
    ) -> QImage | None:
        """Decode *path* into a QImage bounded by ``self.size``.

        Delegates to the shared :func:`qimage_decode.decode_qimage_bytes`
        core: bytes are read with Python ``open()`` (Qt 6's
        ``QImageReader(str)`` chokes on some SMB paths with full-width
        brackets + CJK), the decode is Pillow-first (its C decoders release
        the GIL, so the worker pool doesn't starve the GUI thread — the
        "fast-scroll freeze"), and ``QImageReader`` + ``QBuffer`` is the
        fallback ladder for formats Pillow can't handle (e.g. AVIF without
        ``pillow-avif-plugin``).  See that module's docstrings for the
        diagnostic history; keeping one implementation there is what stops
        those workaround fixes drifting apart (#98).

        *data* short-circuits the file read for callers that already hold the
        bytes (the master-decode fallback), so an image Pillow can't open is
        fetched from the NAS once, not twice.  *skip_pillow* completes that
        de-duplication: the same caller already watched Pillow refuse these
        bytes, so the ladder is the only step left that can succeed.
        """
        if data is None:
            with measure("thumb_file_read", str(path)):
                data = read_file_bytes(path)
        if data is None:
            return None
        with measure("thumb_decode", str(path)):
            return decode_qimage_bytes(
                data, path, target_size=self.size, skip_pillow=skip_pillow,
            )

    def _decode_pdf(self, path: Path, target: QSize | None = None) -> QImage | None:
        """Render page 0 of *path* to a QImage for use as a thumbnail.

        Creates a throw-away ``QPdfDocument`` on this worker thread — the
        Qt docs state ``render()`` is safe to call from background threads
        once the document has loaded, and each task owns its own instance
        so there's no cross-thread sharing.  The rendered page is flattened
        onto a white background so transparent PDFs don't turn black
        against the viewer's dark-theme palette.

        ``target`` overrides the render bound (used to render a larger
        master for the disk cache); defaults to the requested box size.

        ``QtPdf`` is imported here rather than at module level so a build
        whose Qt6Pdf DLL is missing or AV-quarantined fails on PDF
        thumbnails instead of failing to start: this module is imported at
        module level by ``main_window`` (and five other viewer modules), so
        a module-level ``QtPdf`` import made the DLL a hard startup
        dependency — and a windowed frozen build has no stderr to say why
        nothing happened.  ``ContentView`` defers ``QtPdfWidgets`` the same
        way (``_ensure_pdf``); this is the other half of that pair.
        """
        try:
            from PySide6.QtPdf import QPdfDocument  # noqa: PLC0415
        except ImportError:
            # 動画側 (``_decode_video``) と対称の降格。無いと DLL 欠落環境で
            # PDF フォルダを開くたびにファイル単位で再試行し、``run()`` の
            # 広い except が「Thumbnail decode failed」を量産する。
            logger.warning("QtPdf not available; skipping PDF thumb for {}", path)
            return None

        doc = QPdfDocument()
        with measure("thumb_pdf_load", str(path)):
            status = doc.load(str(path))
        if status != QPdfDocument.Error.None_:
            logger.warning("PDF thumbnail load failed for {}: {}", path, status)
            return None
        if doc.pageCount() <= 0:
            return None
        page_size = doc.pagePointSize(0)
        if page_size.isEmpty():
            return None
        bound = target if target is not None else self.size
        tw = bound.width()
        th = bound.height()
        if tw <= 0 or th <= 0:
            return None
        ratio = min(tw / page_size.width(), th / page_size.height())
        render_size = QSize(
            max(1, int(round(page_size.width() * ratio))),
            max(1, int(round(page_size.height() * ratio))),
        )
        with measure("thumb_pdf_render", str(path)):
            rendered = doc.render(0, render_size)
        if rendered.isNull():
            logger.warning("PDF thumbnail render null for {}", path)
            return None
        # Flatten onto white — PDF pages have no implicit background and
        # QPdfDocument returns an ARGB image; the surrounding icon cell
        # would otherwise show the viewport's dark background through
        # transparent regions.
        flat = QImage(rendered.size(), QImage.Format_RGB32)
        flat.fill(Qt.white)
        painter = QPainter(flat)
        try:
            painter.drawImage(0, 0, rendered)
        finally:
            painter.end()
        return flat


    def _decode_video(self, path: Path) -> QImage | None:
        """Extract a representative frame from *path* as a thumbnail.

        Runs a local QEventLoop on the worker thread so QMediaPlayer's
        signal-based pipeline can deliver frames back to us.  We seek to
        ~10% of the duration (capped at 5 s) for a more representative
        frame than t=0 (which is often a black leader).

        The wait ends on the FIRST of:

        * a decodable frame arriving (success),
        * the backend reporting the media is unplayable — ``errorOccurred``
          or a terminal ``mediaStatusChanged`` (``InvalidMedia`` /
          ``EndOfMedia`` without a frame).  Corrupt / truncated / empty files
          fail this way within ~100 ms; without it every broken file parked a
          worker for the full timeout below, and a wait that straddled app
          teardown could deadlock the worker inside ``loop.exec()`` forever,
          keeping the (non-daemon) pool thread alive and blocking process
          exit (#69),
        * the owning loader's cancel event being set (polled from a coarse
          timer) — ``request_shutdown()`` unblocks the wait from outside so
          ``closeEvent``'s bounded drain actually converges,
        * an 8 s wall-clock backstop for codecs that never emit anything.

        All Qt Multimedia objects are created and destroyed on this
        worker thread — no cross-thread ownership issues.
        """
        try:
            from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: PLC0415
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink  # noqa: PLC0415
        except ImportError:
            logger.warning("QtMultimedia not available; skipping video thumb for {}", path)
            return None

        if self._cancelled():
            return None
        try:
            if path.stat().st_size == 0:
                # A zero-byte file can never yield a frame — don't spin up a
                # media pipeline just to watch it fail.
                return None
        except OSError:
            return None

        result: list[QImage] = []
        # Non-empty once the wait is over (frame, error, or terminal status).
        # The backend delivers signals from its own threads, so an error can
        # land DURING ``setSource``/``play`` — before ``loop.exec()`` starts —
        # and a bare ``loop.quit()`` there would be a no-op, silently
        # reverting to the 8 s backstop.  Checking this flag before entering
        # the loop closes that race.
        done: list[bool] = []
        loop = QEventLoop()
        player = QMediaPlayer()
        sink = QVideoSink()
        player.setVideoSink(sink)

        def _finish() -> None:
            done.append(True)
            loop.quit()

        def _try_seek() -> None:
            dur = player.duration()
            if dur > 2000:
                player.setPosition(min(5000, dur // 10))

        def _on_status(status) -> None:
            if status == QMediaPlayer.MediaStatus.LoadedMedia:
                _try_seek()
            elif status in (
                QMediaPlayer.MediaStatus.InvalidMedia,
                QMediaPlayer.MediaStatus.EndOfMedia,
            ):
                # Unplayable, or played through without a decodable frame —
                # either way no frame is coming anymore.
                _finish()

        def _on_error(_error, _message) -> None:
            _finish()

        def _on_frame(frame) -> None:
            if result:
                return
            if frame.isValid():
                img = frame.toImage()
                if img and not img.isNull():
                    # Normalise to a stable displayable format so
                    # smooth_downscale and QPixmap.fromImage work reliably.
                    if img.format() != QImage.Format_RGB32:
                        img = img.convertToFormat(QImage.Format_RGB32)
                    result.append(img.copy())
                    _finish()

        player.mediaStatusChanged.connect(_on_status)
        player.errorOccurred.connect(_on_error)
        sink.videoFrameChanged.connect(_on_frame)
        # Absolute backstop: an owned QTimer (not ``QTimer.singleShot``) so
        # the early-success path can stop it.  A fire-and-forget single shot
        # stays armed on this worker thread's event loop for the full 8 s and
        # calls ``quit`` on a loop that a later decode may already be running
        # — the symmetric ``stop()`` below is what keeps the two timers in
        # this function on the same contract.
        backstop = QTimer()
        backstop.setSingleShot(True)
        backstop.timeout.connect(loop.quit)
        backstop.start(8000)
        # ``request_shutdown()`` is called from the GUI thread; QEventLoop has
        # no thread-safe quit, so poll the flag from a timer that lives on
        # THIS thread's event loop.
        cancel_poll = QTimer()
        cancel_poll.setInterval(100)

        def _poll_cancel() -> None:
            if self._cancelled():
                _finish()

        cancel_poll.timeout.connect(_poll_cancel)
        cancel_poll.start()

        with measure("thumb_video_load", str(path)):
            player.setSource(QUrl.fromLocalFile(str(path)))
            player.play()
            if not done:
                loop.exec()

        cancel_poll.stop()
        backstop.stop()
        player.stop()
        player.setVideoSink(None)

        return result[0] if result else None


class ThumbnailLoader(QObject):
    """Public façade for thumbnail loading.

    Callers connect to ``loaded(key, QImage)`` / ``failed(key)`` and submit
    requests via ``request(key, path, size)``.  A cache hit is emitted
    **synchronously on the same stack** via the ``loaded`` signal — i.e.
    ``request()`` can re-enter the caller's ``loaded`` slot before it returns,
    so callers must not invoke ``request()`` from a context that can't tolerate
    that re-entrancy (e.g. mid-layout).  A miss dispatches to the worker pool
    and emits later, on the GUI thread, when the decode completes.

    **Viewport prioritization**: the loader holds submissions in an
    internal pending queue instead of immediately pushing them to
    ``QThreadPool``.  Callers can then signal which keys are currently
    in view via :meth:`mark_visible`; the dispatcher drains the visible
    queue first so rapid scroll doesn't leave the user staring at
    placeholders while 200 off-screen thumbnails finish first.  Only
    ``max_threads`` tasks are ever dispatched to the pool at once so
    priority changes always take effect on the next dispatch.

    **Two entry points, two contracts.**  :meth:`request` is the viewport
    entry point: it is allowed to answer from the in-memory LRU, to fold a
    duplicate into an already-running decode, and to stay *silent* when the
    caller demonstrably already holds the image (see its docstring) — all
    optimisations that are only sound because a viewport host re-requests on
    its own box check.  :meth:`warm` is the bulk entry point for a cache
    builder that counts one completion per submission: it skips every one of
    those optimisations and promises exactly one ``loaded``/``failed`` per
    call.  Both share the same decode/master path
    (:meth:`_dispatch_pending` → ``_ThumbnailTask`` → ``_produce``), so the
    disk-master ladder stays a single implementation.
    """

    loaded = Signal(str, QImage)
    failed = Signal(str)
    #: Outstanding thumbnail work changed — payload is :meth:`pending_count`
    #: (UIレビュー 07-25 #75).  Emitted only when the number actually moves,
    #: so a status-bar consumer can bind straight to it without debouncing
    #: and without polling on a timer.
    pending_changed = Signal(int)

    def __init__(self, cache_size: int = 256, max_threads: int = 4,
                 parent: QObject | None = None,
                 disk_cache: ThumbDiskCache | None = None,
                 cache_edge: int = _DEFAULT_CACHE_EDGE,
                 folder_cache: FolderPreviewCache | None = None) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[str, QImage] = OrderedDict()
        # Per-cached-key (produced_size, requested_size) in physical px so the
        # cache is size-aware: a request for MORE pixels than the cached copy
        # holds re-decodes (crisp), while a request the cache can already
        # satisfy — or that is source-resolution-limited — is served as-is.
        # BOTH axes are kept: the decode fits the image inside the requested
        # box, so an anisotropic box (a list row: full width × ~24 px) is
        # constrained by its SHORT side and a longest-edge ruler would record
        # a resolution that was never asked of the source (#F1D-1).
        self._cache_edges: dict[str, tuple[QSize, QSize]] = {}
        self._cache_size = cache_size
        self._inflight: set[str] = set()
        # Physical box each in-flight task was dispatched for (read back in
        # the completion slot to record the cached copy's requested size).
        self._inflight_req: dict[str, QSize] = {}
        # Two-tier pending queue: ``_pending_visible`` for keys the
        # caller last tagged as in-viewport, ``_pending_other`` for the
        # rest.  OrderedDict preserves submission order so that within a
        # tier we still process earliest-requested first.
        self._pending_visible: OrderedDict[str, _PendingSpec] = OrderedDict()
        self._pending_other: OrderedDict[str, _PendingSpec] = OrderedDict()
        self._visible: set[str] = set()
        # Bulk lane (:meth:`warm`).  A plain FIFO of ``(key, spec)`` pairs —
        # NOT keyed by ``key`` like the viewport queues — because the warm
        # contract is "exactly one completion per submission"; collapsing two
        # submissions of the same key onto one entry would silently drop a
        # completion the counting consumer is waiting for.
        self._pending_warm: deque[tuple[str, _PendingSpec]] = deque()
        # Warm tasks in flight.  A count, not a key set: warm never dedups.
        self._inflight_warm = 0
        self._max_threads = max_threads
        # Optional persistent thumbnail cache shared with the worker tasks.
        self._disk_cache = disk_cache
        self._cache_edge = max(1, int(cache_edge))
        # Optional folder-preview resolution cache (used by resolve_in_folder).
        self._folder_cache = folder_cache

        # Last value handed to ``pending_changed`` so the signal only fires on
        # a real transition (a full grid submits hundreds of requests in one
        # burst — one emission per queue mutation would be pure churn).
        self._last_pending = 0
        # Mid-range count changes are coalesced through this zero-interval
        # single-shot: a grid submitting hundreds of requests in one burst
        # would otherwise fire ``pending_changed`` once per submission (and
        # once per completion), re-rendering the consumer's label every time.
        # The 0↔nonzero edges stay synchronous — see ``_notify_pending_lazy``.
        self._notify_timer = Debouncer(
            self, 0, self._flush_pending_notify, mode=DebounceMode.LEADING_WINDOW
        )

        self._signals = _Signals()
        self._signals.loaded.connect(self._on_task_loaded)
        self._signals.failed.connect(self._on_task_failed)
        # Separate bridge for the warm lane so the completion slot knows which
        # contract the finished task ran under without threading a flag
        # through the signal payload.
        self._warm_signals = _Signals()
        self._warm_signals.loaded.connect(self._on_warm_loaded)
        self._warm_signals.failed.connect(self._on_warm_failed)

        # One-way teardown flag shared with every dispatched task.  Set by
        # :meth:`request_shutdown`; running video decodes poll it so their
        # bounded event-loop wait can be released from outside (#69).
        self._cancel_event = threading.Event()

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)

    def pending_count(self) -> int:
        """Thumbnails still to come: queued + currently decoding (#75).

        The status bar used to say 「読み込み完了」 the moment the *directory
        scan* finished, while a cold NAS folder still had hundreds of tiles
        left to decode — minutes of grey placeholders with no channel saying
        whether to keep waiting (UIレビュー 07-25 #75).  This is that channel:
        the number drops to 0 exactly when the last tile lands, so a consumer
        bound to :data:`pending_changed` clears itself with no extra
        bookkeeping.

        In-flight tasks are included because a running ``QRunnable`` is work
        the user is still waiting on — Qt gives us no way to cancel one, so
        "pending" and "running" are the same thing from the viewport.
        """
        return (
            len(self._pending_visible)
            + len(self._pending_other)
            + len(self._inflight)
            + len(self._pending_warm)
            + self._inflight_warm
        )

    def _notify_pending(self) -> None:
        """Publish the outstanding count now (the synchronous flavour).

        Used by the explicit queue-drop operations (:meth:`clear_cache` /
        :meth:`discard_pending_outside`) so their callers observe the drained
        queue immediately.  Also re-publishes when a coalesced notification
        was still scheduled — the consumer's last-seen value is stale then,
        even if the count happens to match the last emission.
        """
        stale = self._notify_timer.isActive()
        self._notify_timer.stop()
        count = self.pending_count()
        if count != self._last_pending or stale:
            self._last_pending = count
            self.pending_changed.emit(count)

    def _notify_pending_lazy(self) -> None:
        """Publish the count, coalescing mid-range churn to one per loop turn.

        The 0↔nonzero edges are emitted synchronously — the consumer's label
        appears the instant a burst starts and clears the instant the last
        tile lands (#75 の契約).  Everything in between (2→3→4…) is deferred
        to a zero-interval single-shot so a submission burst costs one
        emission at the edge plus one with the settled total.
        """
        count = self.pending_count()
        if count == self._last_pending:
            return
        if count == 0 or self._last_pending == 0:
            self._notify_timer.stop()
            self._last_pending = count
            self.pending_changed.emit(count)
            return
        self._notify_timer.trigger()

    def _flush_pending_notify(self) -> None:
        """Deliver the coalesced :data:`pending_changed` emission."""
        count = self.pending_count()
        if count != self._last_pending:
            self._last_pending = count
            self.pending_changed.emit(count)

    def set_disk_cache(
        self, disk_cache: ThumbDiskCache | None,
        cache_edge: int | None = None,
    ) -> None:
        """Attach (or replace) the persistent thumbnail cache at runtime."""
        self._disk_cache = disk_cache
        if cache_edge is not None:
            self._cache_edge = max(1, int(cache_edge))

    def set_folder_cache(
        self, folder_cache: FolderPreviewCache | None,
    ) -> None:
        """Attach (or replace) the folder-preview resolution cache."""
        self._folder_cache = folder_cache

    def request_shutdown(self) -> None:
        """Ask in-flight decodes to give up as soon as possible (one-way).

        Qt has no API to cancel a running ``QRunnable``, but the video
        decoder's event-loop wait — the one code path that can park a worker
        for seconds (or, across app teardown, forever: #69) — polls this
        flag and bails within ~100 ms.  Not-yet-started tasks return
        immediately.  Call it right before :meth:`wait_for_done` during
        shutdown so the bounded drain actually converges; there is no undo —
        a loader that has been shut down should not receive new requests.
        """
        self._cancel_event.set()

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        """Block until the internal worker pool drains (or *timeout_ms*).

        The pool is an implementation detail; this is the supported way for
        an owner (e.g. ``CacheBuilder.wait_for_pools`` during shutdown) to
        make sure no worker is still writing to a shared cache DB before
        that DB is closed.  ``timeout_ms < 0`` waits unbounded (Qt's
        ``QThreadPool.waitForDone`` semantics); returns ``True`` when the
        pool is idle.  Queued-but-unstarted tasks still run first — call
        :meth:`clear_cache` beforehand to drop the pending queues.
        """
        return self._pool.waitForDone(timeout_ms)

    def cached_image(self, key: str) -> QImage | None:
        """Return the resident decoded image for *key*, or ``None``.

        Read-only peek (no LRU touch, no dispatch).  Hosts use this to
        re-seed a rebuilt tile that lost its pixmap while the loader still
        holds the decode — the size-aware serve path stays SILENT for a
        source-limited key on re-request (assuming the caller already holds
        the image), so without this a tile hidden by a filter and shown
        again would wait for a ``loaded`` emit that never comes (#10).
        """
        return self._cache.get(key)

    def clear_cache(self) -> None:
        # ``_inflight`` / ``_inflight_req`` are deliberately kept: running
        # QRunnables can't be cancelled, and forgetting them here would let
        # ``_pump`` over-dispatch past the pool ceiling and an immediate
        # re-request start a duplicate decode of a key already in flight.
        # The running task lands normally; a size mismatch is healed by the
        # host's box check re-requesting once it does.
        self._cache.clear()
        self._cache_edges.clear()
        self._pending_visible.clear()
        self._pending_other.clear()
        # Warm submissions are dropped for the same reason: they are queued
        # work nobody is waiting on any more.  In-flight warm tasks still land
        # and still emit — their consumer discards them by generation.
        self._pending_warm.clear()
        self._notify_pending()

    def apply_settings(self, cache_size: int, max_threads: int) -> None:
        """Apply new tunables live.

        The cache is an :class:`OrderedDict` used as an LRU; we shrink by
        evicting the oldest entries, or just record the new ceiling for a
        larger value.  The thread-pool ceiling takes effect on the next
        dispatch, so pump once to pick up any newly-available slots.
        Called on the GUI thread after the settings dialog commits.
        """
        self._cache_size = max(1, int(cache_size))
        while len(self._cache) > self._cache_size:
            evicted, _ = self._cache.popitem(last=False)
            self._cache_edges.pop(evicted, None)
        self._max_threads = max(1, int(max_threads))
        self._pool.setMaxThreadCount(self._max_threads)
        self._pump()

    def discard_pending_outside(self, keep_keys: Iterable[str]) -> None:
        """Drop queued (not-yet-started) requests whose key isn't in *keep_keys*.

        Scroll-driven callers use this to keep the pending queue from
        ballooning during fast flings: once a row scrolls well past the
        viewport there's no point letting a worker eventually decode it
        and then stall the main thread in ``_flush_icon_updates``.  Keys
        already being decoded (``_inflight``) are untouched — Qt has no
        cancel API for running ``QRunnable``\\s — but the 4-wide pool is
        small enough that those finish quickly and stop crowding.
        """
        keep = set(keep_keys)
        for k in list(self._pending_visible.keys()):
            if k not in keep:
                del self._pending_visible[k]
        for k in list(self._pending_other.keys()):
            if k not in keep:
                del self._pending_other[k]
        self._notify_pending()

    def mark_visible(self, keys: Iterable[str]) -> None:
        """Declare which keys are currently in the viewport.

        Keys already pending migrate between the visible and non-visible
        queues so the next dispatch drains the ones the user is actually
        looking at.  In-flight decodes can't be preempted, but since we
        cap concurrency at ``max_threads`` the next free worker will
        pick from the visible queue first — after at most a few seconds
        the display catches up with the current scroll position.
        """
        new_visible = set(keys)
        # Demote keys that left the viewport so a fresh visible set from
        # a newer scroll position actually wins the next dispatch.
        for k in list(self._pending_visible.keys()):
            if k not in new_visible:
                self._pending_other[k] = self._pending_visible.pop(k)
        # Promote keys that just entered the viewport.
        for k in list(self._pending_other.keys()):
            if k in new_visible:
                self._pending_visible[k] = self._pending_other.pop(k)
        self._visible = new_visible
        self._pump()

    def request(
        self,
        key: str,
        path: Path,
        size: QSize,
        resolve_in_folder: bool = False,
        dpr: float = 1.0,
        folder_mtime: float = 0.0,
    ) -> bool:
        """Request a thumbnail for *path*.

        *size* is in logical pixels (the widget's ``iconSize``); the loader
        internally decodes at ``size × dpr`` so that the emitted ``QImage``
        has enough pixels to render sharp on a hi-DPI display.  Caller
        passes ``self.devicePixelRatioF()`` as *dpr*.

        If *resolve_in_folder* is ``True``, *path* is treated as a directory
        and the worker picks the alphabetically-first image inside it.

        The cache is **size-aware** on BOTH axes: re-requesting a key at a box
        the cached copy would have to be upscaled into re-decodes it (the disk
        master serves the bigger downscale locally), so a tile whose box grew
        after its aspect settled — or changed shape when the pane switched
        from list rows to grid tiles (#F1D-1) — is upgraded to a crisp
        thumbnail instead of upscaling.

        Returns whether a completion signal answers this call: ``True`` when
        ``loaded`` was emitted synchronously (cache hit) or a decode is now
        queued/running for it, ``False`` on the three *silent* paths — a
        source-limited cached copy the caller is assumed to already hold, a
        duplicate of an in-flight decode, and a duplicate of an already-queued
        submission.  A viewport host can ignore the result (its box check
        re-requests); a consumer that counts one completion per submission
        must not — see :meth:`warm` for an entry point without silent paths.
        """
        if dpr > 1.0:
            phys_size = QSize(
                max(1, round(size.width() * dpr)),
                max(1, round(size.height() * dpr)),
            )
        else:
            # Copy: the box is recorded in ``_cache_edges`` / ``_inflight_req``
            # and must not alias a QSize the caller may reuse.
            phys_size = QSize(size)

        cached = self._cache.get(key)
        if cached is not None:
            produced, prev_req = self._cache_edges.get(
                key, (phys_size, phys_size)
            )
            if fitted_edge(produced, phys_size) <= max(
                produced.width(), produced.height()
            ):
                # Cache already has enough pixels for this box: drawn with
                # ``KeepAspectRatio`` the cached copy fills it without being
                # stretched (it already covers the binding axis).
                self._cache.move_to_end(key)
                self.loaded.emit(key, cached)
                return True
            if (
                produced.width() + _SOURCE_LIMIT_SLACK < prev_req.width()
                and produced.height() + _SOURCE_LIMIT_SLACK < prev_req.height()
            ):
                # The decode was source-limited (the original is smaller than
                # the box it was asked for), so a bigger request can't do
                # better.  The caller already holds this image — keep serving
                # it WITHOUT re-emitting, so a box larger than the source never
                # churns the flush/repaint path.
                #
                # The slack matters: aspect-preserving downscales round, so a
                # source *larger* than the box can still land at req-1 (e.g. a
                # 400×900 original in a 53×120 justified box produces 119 <
                # 120).  Treating that near-miss as source-limited would
                # silently swallow every future upgrade request and pin the
                # tile to a blurry upscale until the loader cache clears (#18).
                # A genuinely source-limited image within the slack merely
                # re-decodes once per box growth — bounded, no per-scroll churn
                # (the host only re-requests when the box outgrows the pixmap).
                #
                # BOTH axes must fall short: a decode that filled the box on
                # one axis was limited by the BOX, not by the source, so a
                # differently-shaped (bigger on that axis) box can still do
                # better.  Judging this on the longest edge alone froze every
                # tile at list-row resolution once the pane switched back to a
                # grid layout (#F1D-1).
                self._cache.move_to_end(key)
                return False
            # Cache holds a smaller copy than needed and the source can do
            # better → drop and re-decode below.
            del self._cache[key]
            self._cache_edges.pop(key, None)

        spec = _PendingSpec(
            path=path, phys_size=phys_size,
            resolve_in_folder=resolve_in_folder, dpr=dpr,
            folder_mtime=folder_mtime,
        )
        if key in self._inflight:
            # A decode is already running.  If it was dispatched for a smaller
            # box, the host's box check re-requests once it lands; cheap,
            # since the disk master serves the larger downscale locally.
            return False
        for queue in (self._pending_visible, self._pending_other):
            existing = queue.get(key)
            if existing is not None:
                # Upgrade an already-queued request to the larger size.  The
                # per-axis maximum, so a re-request with a differently shaped
                # box (list row → grid tile) never *drops* pixels the queued
                # box would have produced.
                cur = existing.phys_size
                if (
                    phys_size.width() > cur.width()
                    or phys_size.height() > cur.height()
                ):
                    queue[key] = replace(
                        spec,
                        phys_size=QSize(
                            max(cur.width(), phys_size.width()),
                            max(cur.height(), phys_size.height()),
                        ),
                    )
                return False
        if key in self._visible:
            self._pending_visible[key] = spec
        else:
            self._pending_other[key] = spec
        self._pump()
        return True

    def warm(
        self,
        key: str,
        path: Path,
        phys_size: QSize,
        resolve_in_folder: bool = False,
        folder_mtime: float = 0.0,
    ) -> None:
        """Bulk entry point: decode *path* into the disk master, exactly once.

        For consumers that submit a whole subtree and count one completion per
        submission (:class:`~snappix.viewer.cache_builder.CacheBuilder`).  It
        shares the decode/master path with :meth:`request` — same pending
        dispatcher, same ``_ThumbnailTask``, same disk-cache write-through —
        but **none** of the viewport optimisations:

        * the in-memory LRU is neither read nor written (a builder visits each
          key once, so caching them is pure churn — and it is exactly what
          makes the silent source-limited serve reachable);
        * an already-running decode of the same key is not folded in;
        * an already-queued submission is not upgraded/absorbed.

        The contract is therefore total: every call produces exactly one
        ``loaded`` **or** ``failed`` emission, always asynchronously (never
        re-entrant on the caller's stack), so a counting consumer terminates
        structurally instead of patching up the silent paths after the fact.

        *phys_size* is in **physical** pixels (no dpr scaling is applied — a
        builder writes device-independent masters).
        """
        self._pending_warm.append((
            key,
            _PendingSpec(
                path=path, phys_size=QSize(phys_size),
                resolve_in_folder=resolve_in_folder, dpr=1.0,
                folder_mtime=folder_mtime,
            ),
        ))
        self._pump()

    def _pump(self) -> None:
        """Dispatch pending tasks, then publish the outstanding count.

        The dispatch itself lives in :meth:`_dispatch_pending`; this wrapper
        exists so **every** path that changes the queues ends with a
        :data:`pending_changed` emission (#75) — ``_pump`` is already the
        single funnel called from :meth:`request`, :meth:`mark_visible`,
        :meth:`apply_settings` and both task-completion slots.  The lazy
        flavour keeps a submission burst from emitting once per request.
        """
        self._dispatch_pending()
        self._notify_pending_lazy()

    def _dispatch_pending(self) -> None:
        """Dispatch pending tasks until the thread pool is saturated.

        Visible queue drains first; we only touch the non-visible queue
        when no visible work is pending, and the bulk :meth:`warm` lane last
        of all (a background cache build must never outrank the tiles the
        user is looking at).  Called from :meth:`request`, :meth:`warm`,
        :meth:`mark_visible`, and task-completion slots so priority changes
        are honored as soon as a worker becomes free.
        """
        while len(self._inflight) + self._inflight_warm < self._max_threads:
            warm = False
            if self._pending_visible:
                key, spec = self._pending_visible.popitem(last=False)
            elif self._pending_other:
                key, spec = self._pending_other.popitem(last=False)
            elif self._pending_warm:
                key, spec = self._pending_warm.popleft()
                warm = True
            else:
                return
            if warm:
                self._inflight_warm += 1
            else:
                self._inflight.add(key)
                self._inflight_req[key] = spec.phys_size
            task = _ThumbnailTask(
                key, spec.path, spec.phys_size,
                self._warm_signals if warm else self._signals,
                resolve_in_folder=spec.resolve_in_folder, dpr=spec.dpr,
                disk_cache=self._disk_cache, cache_edge=self._cache_edge,
                folder_cache=self._folder_cache, folder_mtime=spec.folder_mtime,
                cancel_event=self._cancel_event,
            )
            self._pool.start(task)

    # ------------------------------------------------------------------ slots

    def _on_task_loaded(self, key: str, image: QImage) -> None:
        self._inflight.discard(key)
        produced = QSize(image.width(), image.height())
        req_size = self._inflight_req.pop(key, produced)
        self._cache[key] = image
        self._cache_edges[key] = (produced, req_size)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            evicted, _ = self._cache.popitem(last=False)
            self._cache_edges.pop(evicted, None)
        self.loaded.emit(key, image)
        self._pump()

    def _on_task_failed(self, key: str) -> None:
        self._inflight.discard(key)
        self._inflight_req.pop(key, None)
        self.failed.emit(key)
        self._pump()

    def _on_warm_loaded(self, key: str, image: QImage) -> None:
        """A :meth:`warm` decode landed: publish it, cache nothing.

        The image is already persisted (``_ThumbnailTask`` write-through to the
        disk master); keeping a copy in the in-memory LRU would evict live
        viewport entries for keys the builder will never ask for again.
        """
        self._inflight_warm = max(0, self._inflight_warm - 1)
        self.loaded.emit(key, image)
        self._pump()

    def _on_warm_failed(self, key: str) -> None:
        self._inflight_warm = max(0, self._inflight_warm - 1)
        self.failed.emit(key)
        self._pump()
