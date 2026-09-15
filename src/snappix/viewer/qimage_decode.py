"""Shared worker-thread image decoding for the viewer.

Centralises two Qt bugs whose workarounds were originally proven in
``thumbnail_loader.py`` (diagnostic history: a faulthandler snapshot during
the fast-scroll freeze showed every worker stuck inside ``QImageReader``
while the main thread sat in ``app.exec`` with no Python frames — the
hallmark of GIL contention):

1. **SMB/CJK path bug** — Qt 6's ``QImageReader(str(path))`` fails with
   "File not found" on some SMB/NAS paths containing CJK characters plus
   full-width brackets/commas.  Workaround: read the bytes with Python's
   ``open()`` (which handles those paths fine) and decode from memory
   via ``QBuffer`` — or skip Qt entirely and decode via Pillow.

2. **GIL contention** — PySide6's ``QImageReader.read()`` holds the GIL
   for the whole native decode, so decoding on a ``QThreadPool`` worker
   still blocks the GUI thread (the "fast-scroll freeze").  Workaround:
   Pillow-first decode (its C decoders release the GIL for the duration
   of the actual decode), with ``QImageReader`` + ``QBuffer`` as the
   fallback for formats Pillow can't handle (e.g. AVIF without
   ``pillow-avif-plugin``) or the odd corrupt file Qt tolerates.

``thumbnail_loader.py`` keeps its richer *pipeline* (disk-cache tiers,
video / PDF branches) but delegates the plain "bytes → QImage" decode and
the "PIL master open" steps to this module, so the Pillow-first strategy,
the QImageReader fallback ladder and the :data:`_EXT_TO_FORMAT` hint table
have a single home.  The other preview call sites (``image_view``,
``markdown_view``, ``folder_preview_view``, ``detail_window``) and the
aspect probe (``aspect_probe.read_image_aspect``) share the same cores.

Everything here is safe on worker threads: only ``QImage`` is ever
constructed, never ``QPixmap``.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

from loguru import logger
from PIL import Image, ImageOps, UnidentifiedImageError
from PySide6.QtCore import QBuffer, QByteArray, QSize, Qt
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader

from .image_scale import pil_to_qimage, qimage_to_pil

# Pillow 10+ relocated resampling constants under ``Image.Resampling``.
_PIL_LANCZOS = Image.Resampling.LANCZOS

# Map common image extensions → QImageReader format hint (the single
# definition — thumbnail_loader shares this decode path).  Skipping Qt's
# byte-sniffing auto-detection is a small speedup and avoids false
# negatives on exotic JPEG variants.
_EXT_TO_FORMAT = {
    ".jpg": b"jpeg",
    ".jpeg": b"jpeg",
    ".png": b"png",
    ".webp": b"webp",
    ".gif": b"gif",
    ".bmp": b"bmp",
    ".avif": b"avif",
}

# EXIF orientation values that imply a 90°/270° rotation (width/height
# swap).  The single definition — ``aspect_probe.read_image_aspect`` reads
# headers through :func:`pil_header_size` below.
_SWAP_ORIENTATIONS = frozenset({5, 6, 7, 8})
_EXIF_ORIENTATION_TAG = 0x0112

# Serialises every ``QImageReader`` decode/probe across worker threads.
#
# Workaround (3) — process-wide deadlock (review 2026-07-16 item 1, verified
# 5/5 by real render): when several thumbnail workers fall into the
# QImageReader fallback concurrently on files Pillow can't identify (0-byte
# JPEGs, extension-spoofed PNGs, truncated JPEGs — the typical output of an
# interrupted download), PySide6's ``QImageReader.size()`` / ``.read()`` hold
# the GIL for the whole native call while Qt's image-plugin factory takes its
# own internal lock; the two lock orders cross and the entire process
# (including the GUI thread stuck in ``processEvents``) freezes permanently,
# recoverable only by a force-kill.  Holding this single module lock for the
# duration of each ``QImageReader`` interaction makes those native calls
# strictly sequential, so the factory lock is never contended from two GIL
# holders at once.  The Pillow-first path (the common case) never touches this
# lock, so normal images keep full worker parallelism; only files that fall
# through to the Qt fallback pay the serialisation.
#
# GUI-thread paths that realise ``QStyle.standardIcon`` pixmaps take this
# same lock (gallery_view placeholder glyphs, advanced_search seed-thumb
# fallback, and every ``QLineEdit`` clear button via
# :func:`enable_clear_button`): the style's icons decode embedded PNG
# resources through the same image-plugin factory, so an unguarded
# ``QIcon.pixmap()`` during paint could recreate the crossed lock order
# against a concurrent worker decode (rare permanent hang observed under
# pytest -n auto, 2026-07-19; again 2026-08-30 with the GUI thread inside
# ``setClearButtonEnabled`` while a worker sat in ``reader.read()``).  The
# lock is process-wide and non-reentrant — never nest acquisitions.
_QT_READER_LOCK = threading.Lock()


def enable_clear_button(edit) -> None:
    """``edit.setClearButtonEnabled(True)`` under :data:`_QT_READER_LOCK`.

    The clear button's icon is a style ``standardIcon`` whose embedded PNG
    is decoded through Qt's image-plugin factory *synchronously inside the
    call*, so a bare ``setClearButtonEnabled(True)`` on the GUI thread can
    recreate the crossed factory/GIL lock order against a concurrent worker
    decode (workaround 3 above — CI faulthandler capture 2026-08-30).  Every
    line edit that wants a clear button goes through here; don't call
    ``setClearButtonEnabled`` directly.

    Duck-typed (no QtWidgets import): any object with the QLineEdit shim
    API works.
    """
    with _QT_READER_LOCK:
        edit.setClearButtonEnabled(True)


def read_file_bytes(path: Path) -> bytes | None:
    """Read *path* fully into memory via Python ``open()``.

    This is the load-bearing half of workaround (1): Python's ``open``
    handles the SMB/CJK paths that ``QImageReader(str)`` chokes on.
    Returns ``None`` (with a warning) on I/O failure.
    """
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        logger.warning("Cannot open image file {}: {}", path, exc)
        return None


# ---------------------------------------------------------------- QImage

def decode_qimage(path: Path, *, target_size: QSize | None = None) -> QImage | None:
    """Decode *path* into a detached ``QImage``, or ``None`` on failure.

    Bytes-first read (workaround 1), Pillow-first decode (workaround 2),
    ``QImageReader`` + ``QBuffer`` fallback.  EXIF orientation is always
    applied (Pillow's ``exif_transpose`` mirrors Qt's
    ``setAutoTransform(True)``).

    *target_size* bounds the decode: the result fits within the box with
    aspect ratio preserved, and is **never upscaled** past the source
    resolution.  ``None`` decodes at full resolution.
    """
    data = read_file_bytes(path)
    if data is None:
        return None
    return decode_qimage_bytes(data, path, target_size=target_size)


def decode_qimage_bytes(
    data: bytes,
    path: Path,
    *,
    target_size: QSize | None = None,
    skip_pillow: bool = False,
) -> QImage | None:
    """In-memory variant of :func:`decode_qimage` for callers that already
    hold the file bytes (avoids a second NAS round-trip).

    ``skip_pillow`` goes straight to the ``QImageReader`` ladder.  A caller
    that has *just* watched Pillow refuse these exact bytes (the thumbnail
    loader's master attempt) would otherwise pay a second, by-definition
    failing Pillow attempt — and for a truncated JPEG that is not a cheap
    header peek: ``Image.open`` succeeds and ``load()`` decodes every DCT
    block up to the cut before raising.  The ladder is what that fallback
    wanted in the first place ("formats Pillow cannot open").
    """
    if not skip_pillow:
        image = _decode_via_pillow(data, path, target_size)
        if image is not None and not image.isNull():
            return image
    return _decode_qt_ladder(data, path, target_size)


def _decode_via_pillow(
    data: bytes, path: Path, target_size: QSize | None,
) -> QImage | None:
    """Decode *data* through Pillow and convert to a detached ``QImage``.

    The single implementation of the Pillow decode step (thumbnail_loader
    reaches it via :func:`decode_qimage_bytes`).  The open / orient / load
    core is :func:`open_pil_oriented` — one implementation shared with the
    thumbnail master path, so the ``draft()`` fast path runs in the right
    order here too (it must precede the ``load()`` that ``exif_transpose``
    triggers; the previous hand-written copy of this sequence drafted after
    the load and therefore never took effect).  ``target_size`` is passed on
    as ``max_edge`` so libjpeg can decode at a scaled DCT ratio; the
    ``thumbnail`` below then lands on the exact box, keeps aspect and never
    upscales.  The conversion to ``QImage`` is
    :func:`~.image_scale.pil_to_qimage` (its ``.copy()`` moves pixel
    ownership into Qt's heap so the image survives the ``bytes`` buffer
    going out of scope).

    Returns ``None`` on any Pillow failure so the caller falls through
    to the QImageReader path.
    """
    max_edge: int | None = None
    tw = th = 0
    if target_size is not None:
        tw = target_size.width()
        th = target_size.height()
        if tw > 0 and th > 0:
            max_edge = max(tw, th)
    pil = open_pil_oriented(data, path, max_edge=max_edge)
    if pil is None:
        return None
    try:
        if max_edge is not None and (pil.width > tw or pil.height > th):
            # Palette / bilevel sources must leave their mode BEFORE the
            # downscale or Pillow forces NEAREST (see ``ensure_resamplable``);
            # the RGBA conversion inside ``pil_to_qimage`` would come too late.
            pil = ensure_resamplable(pil)
            pil.thumbnail((tw, th), _PIL_LANCZOS)
        return pil_to_qimage(pil)
    except (OSError, ValueError) as exc:
        logger.debug(
            "Pillow decode failed for {} ({}); falling back to QImageReader",
            path, exc,
        )
        return None
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("Pillow decode error for {}: {}", path, exc)
        return None


def _decode_qt_ladder(
    data: bytes, path: Path, target_size: QSize | None,
) -> QImage | None:
    """QImageReader fallback ladder (mirrors thumbnail_loader's ordering).

    Tries (format hint, scaled) → (hint, unscaled) → (auto-detect,
    scaled) → (auto-detect, unscaled).  The hint-less retries save files
    whose extension lies about the content (PNG bytes in a ``.jpg``
    name); the unscaled retries save plugins that mishandle
    ``setScaledSize``.
    """
    format_hint = _EXT_TO_FORMAT.get(path.suffix.lower())
    hints = [format_hint, None] if format_hint is not None else [None]
    scaled_opts = [True, False] if target_size is not None else [False]
    # ONE ``QByteArray`` copy of the file for up to four attempts.  Building it
    # per attempt meant a file-sized temporary ×4 per worker, and the ladder's
    # only routine full run (AVIF without the Pillow plugin) is exactly where
    # the files are large.
    payload = QByteArray(data)
    for hint in hints:
        for scaled in scaled_opts:
            image = _decode_qt_bytes(
                payload, path, hint, target_size if scaled else None,
            )
            if image is not None and not image.isNull():
                return image
    return None


def _decode_qt_bytes(
    payload: QByteArray,
    path: Path,
    format_hint: bytes | None,
    target_size: QSize | None,
) -> QImage | None:
    """Single QImageReader+QBuffer decode attempt.

    The buffer must outlive the reader (PySide6 doesn't keep a Python
    reference for us), so both are held as locals here; *payload* is the
    caller's single copy of the bytes, shared across the ladder's attempts
    (``QByteArray`` is copy-on-write and the reader only reads).  Scaling
    keeps aspect and never upscales, matching the Pillow path.

    The whole ``QImageReader`` interaction (construction → ``size()`` →
    ``read()``) runs under :data:`_QT_READER_LOCK` so concurrent workers can
    never contend Qt's image-plugin factory lock from two GIL holders at once
    (the process-wide freeze — see the lock's docstring).
    """
    buffer = QBuffer()
    buffer.setData(payload)
    if not buffer.open(QBuffer.ReadOnly):
        return None
    try:
        with _QT_READER_LOCK:
            if format_hint is not None:
                reader = QImageReader(buffer, format_hint)
            else:
                reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            if target_size is not None:
                src = reader.size()
                if (
                    src.isValid() and not src.isEmpty()
                    and (
                        src.width() > target_size.width()
                        or src.height() > target_size.height()
                    )
                ):
                    reader.setScaledSize(src.scaled(target_size, Qt.KeepAspectRatio))
            image = reader.read()
            err = reader.errorString()
        if image.isNull():
            logger.debug(
                "QImageReader decode null (hint={!r}, scaled={}) for {} (err={!r})",
                format_hint, target_size is not None, path, err,
            )
            return None
        return image
    finally:
        buffer.close()


# ------------------------------------------------------------------- PIL

def decode_pil(path: Path) -> Image.Image | None:
    """Decode *path* into a full-resolution RGBA ``PIL.Image``.

    For ``ImageView``'s LANCZOS pipeline, which keeps the PIL source
    around for repeated resampling.  Same Pillow-first / Qt-fallback
    strategy as :func:`decode_qimage`; output is always RGBA and
    EXIF-transposed, matching what the previous
    ``QImageReader(autoTransform)`` → ``qimage_to_pil`` path produced.
    """
    data = read_file_bytes(path)
    if data is None:
        return None
    return decode_pil_bytes(data, path)


#: PIL modes ``Image.resize`` refuses to resample: it silently forces
#: ``NEAREST`` for bilevel / palette sources regardless of the ``resample``
#: argument (Pillow ``Image.resize``: ``if self.mode in ("1", "P"): resample =
#: NEAREST``).  ``thumbnail()`` goes through ``resize()``, so a palette PNG /
#: GIF downscaled straight from its source mode loses every thin line, dot and
#: checkerboard — the exact aliasing ``image_scale`` exists to avoid.  ``PA``
#: is included for symmetry (palette + alpha).
_NEAREST_ONLY_MODES = ("1", "P", "PA")


def ensure_resamplable(im: Image.Image) -> Image.Image:
    """Return *im* in a mode LANCZOS can actually resample.

    A no-op for the usual RGB / RGBA / L sources; bilevel becomes ``L`` (same
    1 byte per pixel, so a huge fax TIFF doesn't balloon) and palette becomes
    ``RGBA`` (keeps any palette transparency).  Callers must convert *before*
    downscaling — converting after is too late, the pixels are already
    destroyed.
    """
    if im.mode == "1":
        return im.convert("L")
    if im.mode in _NEAREST_ONLY_MODES:
        return im.convert("RGBA")
    return im


def open_pil_oriented(
    data: bytes, path: Path, *, max_edge: int | None = None,
) -> Image.Image | None:
    """Fully load *data* as an EXIF-transposed ``PIL.Image``, mode preserved.

    The shared "``Image.open`` → ``exif_transpose`` → ``load``" core: unlike
    :func:`decode_pil_bytes` it does NOT convert to RGBA (callers that
    re-encode — e.g. the thumbnail disk-cache master — keep the source mode
    so an RGB JPEG doesn't grow an alpha channel) and does NOT fall back to
    Qt.  Returns ``None`` on any Pillow failure (*path* is for logging only).

    ``max_edge`` declares "I am going to shrink this to at most N px anyway":
    it lets Pillow ``draft()`` the source before the decode, i.e. libjpeg
    decodes a JPEG at a scaled DCT ratio (1/2, 1/4, 1/8) instead of full
    resolution.  The result is never *smaller* than the requested box (draft
    only picks a ratio that still covers it), so the caller's own
    ``thumbnail()`` produces perceptually indistinguishable pixels (the test
    suite tolerates a per-channel maxdiff of up to 12 — the scaled-DCT
    approximation vs. a full decode, not a functional difference) while an
    8000×6000 JPEG decodes ~3.5× faster and materialises a 2.4 MB
    intermediate instead of 144 MB.  A no-op for formats without a draft
    implementation (PNG/WebP/…), and for sources already near the box.

    A source above Pillow's decompression-bomb hard limit (``2 ×
    Image.MAX_IMAGE_PIXELS`` ≈ 179 Mpx, i.e. roughly 13,400²) is refused here
    **and** by Qt's ``QImageReader`` allocation limit further down the ladder,
    so such a file gets no thumbnail at all.  That is a deliberate ceiling —
    a single RGBA buffer at that size is ~716 MB per worker — but it is
    reported at ``warning`` level with the refused pixel count so the user has
    something to find, instead of one tile that silently stays blank.
    """
    try:
        im = Image.open(io.BytesIO(data))
        if max_edge is not None and max_edge > 0:
            # Must happen before ``load()`` (which ``exif_transpose`` triggers)
            # or the scaled-DCT fast path is already gone.  ``mode=None`` keeps
            # the source's channel layout — this function's contract.  The box
            # is square, so an EXIF rotation applied afterwards can't turn a
            # drafted edge into an under-sized one.
            im.draft(None, (max_edge, max_edge))
        out = ImageOps.exif_transpose(im)
        try:
            out.load()
        finally:
            # exif_transpose always returns a new object; close original
            if out is not im:
                im.close()
        return out
    except Image.DecompressionBombError as exc:
        # Not a corrupt file — a real image that is simply too large for both
        # decoders.  ``debug`` would bury the only explanation the user could
        # ever get for "this one tile never appears".
        logger.warning(
            "画像が大きすぎてサムネイルを作れません {} ({}): 上限 {} px",
            path, exc, 2 * (Image.MAX_IMAGE_PIXELS or 0),
        )
        return None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.debug("Pillow decode failed for {} ({})", path, exc)
        return None
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("Pillow decode error for {}: {}", path, exc)
        return None


def decode_pil_bytes(data: bytes, path: Path) -> Image.Image | None:
    """In-memory variant of :func:`decode_pil`."""
    out = open_pil_oriented(data, path)
    if out is not None:
        if out.mode != "RGBA":
            out = out.convert("RGBA")
        return out
    qimage = _decode_qt_ladder(data, path, None)
    if qimage is None or qimage.isNull():
        return None
    return qimage_to_pil(qimage)


# ------------------------------------------------------------ header size

def pil_header_size(path: Path) -> tuple[int, int] | None:
    """Header-only displayed ``(width, height)`` via Pillow, or ``None``.

    The Pillow-only core shared by :func:`read_image_size` (which adds a Qt
    fallback) and ``aspect_probe.read_image_aspect`` (which stays
    Pillow-only — probe workers must not fall into ``QImageReader``, whose
    PySide6 binding holds the GIL for the whole call).  ``Image.open`` reads
    just enough to populate ``.size`` without decoding pixels; the EXIF
    orientation swap is applied so the result matches what the (also
    exif-transposing) decode paths produce.
    """
    try:
        with Image.open(path) as im:
            w, h = _oriented_size(im)
        if w > 0 and h > 0:
            return int(w), int(h)
        return None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.debug("Pillow size probe failed for {} ({})", path, exc)
        return None
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("Image size probe error for {}: {}", path, exc)
        return None


def read_image_size(path: Path) -> tuple[int, int] | None:
    """Header-only ``(width, height)`` of *path*, or ``None``.

    Pillow-first (:func:`pil_header_size` — Python's file I/O handles the
    SMB/CJK paths that break ``QImageReader(str)``), falling back to
    ``QImageReader`` + ``QBuffer`` for formats Pillow can't identify.
    """
    wh = pil_header_size(path)
    if wh is not None:
        return wh
    data = read_file_bytes(path)
    if data is None:
        return None
    return _qt_size_from_bytes(data, path)


def image_size_from_bytes(data: bytes, path: Path) -> tuple[int, int] | None:
    """In-memory variant of :func:`read_image_size` for callers that
    already hold the file bytes."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = _oriented_size(im)
        if w > 0 and h > 0:
            return int(w), int(h)
        return None
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.debug(
            "Pillow size probe failed for {} ({}); trying QImageReader",
            path, exc,
        )
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("Image size probe error for {}: {}", path, exc)
        return None
    return _qt_size_from_bytes(data, path)


def _oriented_size(im: Image.Image) -> tuple[int, int]:
    """``im.size`` with the EXIF orientation swap applied."""
    w, h = im.size
    try:
        orient = im.getexif().get(_EXIF_ORIENTATION_TAG)
    except Exception:  # pragma: no cover (defensive — broken EXIF)
        orient = None
    if orient in _SWAP_ORIENTATIONS:
        w, h = h, w
    return w, h


def _qt_size_from_bytes(data: bytes, path: Path) -> tuple[int, int] | None:
    """QImageReader header-size fallback (orientation-aware, no decode).

    Runs under :data:`_QT_READER_LOCK` for the same reason as
    :func:`_decode_qt_bytes` — ``reader.size()`` hits the same Qt image-plugin
    factory, so it must not be entered concurrently with a fallback decode.
    """
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QBuffer.ReadOnly):
        return None
    try:
        with _QT_READER_LOCK:
            reader = QImageReader(buffer)
            size = reader.size()
            err = reader.errorString()
            if not size.isValid() or size.isEmpty():
                logger.debug(
                    "QImageReader size probe failed for {} (err={!r})",
                    path, err,
                )
                return None
            w, h = size.width(), size.height()
            # Match the auto-transformed decode output: swap when the EXIF
            # transform includes a 90° rotation.
            try:
                transform = reader.transformation()
                if transform & QImageIOHandler.Transformation.TransformationRotate90:
                    w, h = h, w
            except Exception:  # pragma: no cover (defensive)
                pass
        return w, h
    finally:
        buffer.close()


__all__ = [
    "decode_pil",
    "decode_pil_bytes",
    "decode_qimage",
    "decode_qimage_bytes",
    "image_size_from_bytes",
    "open_pil_oriented",
    "pil_header_size",
    "read_file_bytes",
    "read_image_size",
]
