"""Image downscaling for the viewer.

Two quality tiers:

* :func:`smooth_downscale` — Qt-only progressive (mipmap-style) halving.
  Cheap, works on ``QImage`` / ``QPixmap`` via duck typing.  Used for
  thumbnails where we decode many images in parallel and want minimal
  main-thread cost.
* :func:`lanczos_downscale_pil` + :func:`pil_to_qpixmap` —
  Pillow ``LANCZOS`` resize.  Higher quality, ~10x slower than Qt
  bilinear.  Used only for the centre-pane preview, which handles one
  image at a time and can absorb the cost for a noticeably crisper
  result on fine lines / dots / checker patterns.

Rationale: Qt's ``Qt.SmoothTransformation`` is a single-pass bilinear
filter (2x2 source neighbourhood per output pixel).  Beyond ~2x
reduction it aliases high-frequency detail into block noise.  Iterative
halving approximates a box filter and mitigates this.  LANCZOS is the
reference quality for downscaling fine detail.
"""

from __future__ import annotations

from PIL import Image
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap

# Pillow 10+ moved the resampling constants under ``Image.Resampling``.
_LANCZOS = Image.Resampling.LANCZOS
_BICUBIC = Image.Resampling.BICUBIC


def smooth_downscale(img, target: QSize):
    """Progressive halving downscale. No-op for upscale or equal size."""
    if img.isNull() or target.isEmpty():
        return img
    tw = target.width()
    th = target.height()
    w = img.width()
    h = img.height()
    if w <= tw and h <= th:
        return img
    scale = min(tw / w, th / h)
    final_w = max(1, round(w * scale))
    final_h = max(1, round(h * scale))
    while w > final_w * 2 and h > final_h * 2:
        w //= 2
        h //= 2
        img = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    if img.width() != final_w or img.height() != final_h:
        img = img.scaled(
            final_w, final_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
        )
    return img


# ---------------------------------------------------------------------------
# PIL <-> Qt bridges for the LANCZOS preview path.  The pair is symmetric:
# :func:`qimage_to_pil` / :func:`pil_to_qimage`; ``pil_to_qpixmap`` is the
# GUI-thread wrapper over the latter.
# ---------------------------------------------------------------------------

def qimage_to_pil(qimg: QImage) -> Image.Image:
    """Convert a ``QImage`` to a Pillow RGBA image.

    Normalises to ``Format_RGBA8888`` first so byte order matches Pillow's
    ``RGBA`` expectation regardless of Qt's native ARGB32 layout.  Strips
    any per-row padding (``bytesPerLine > width * 4``) before handing the
    buffer to Pillow, since Pillow's ``frombytes`` assumes a packed layout.
    """
    if qimg.format() != QImage.Format_RGBA8888:
        qimg = qimg.convertToFormat(QImage.Format_RGBA8888)
    w = qimg.width()
    h = qimg.height()
    bpl = qimg.bytesPerLine()
    buf = bytes(qimg.constBits())
    expected = w * 4
    if bpl != expected:
        buf = b"".join(buf[i * bpl : i * bpl + expected] for i in range(h))
    return Image.frombytes("RGBA", (w, h), buf)


def pil_to_qimage(pil: Image.Image) -> QImage:
    """Convert a Pillow image to ``QImage`` via ``Format_RGBA8888``.

    Counterpart of :func:`qimage_to_pil`.  We ``.copy()`` the intermediate
    ``QImage`` because its pixel data points at a Python ``bytes`` buffer
    that would be freed once this function returns — Qt must own the
    memory before we hand it out.

    Callers that only need a ``QImage`` (clipboard payloads, worker
    threads) must use this instead of ``pil_to_qpixmap(...).toImage()``:
    the pixmap round-trip costs a second full-pixel copy plus a platform
    surface allocation that is thrown away immediately.
    """
    if pil.mode != "RGBA":
        pil = pil.convert("RGBA")
    data = pil.tobytes("raw", "RGBA")
    qimg = QImage(data, pil.width, pil.height, QImage.Format_RGBA8888)
    return qimg.copy()


def pil_to_qpixmap(pil: Image.Image) -> QPixmap:
    """Convert a Pillow image to ``QPixmap`` (GUI thread only)."""
    return QPixmap.fromImage(pil_to_qimage(pil))


def lanczos_downscale_pil(pil: Image.Image, target: QSize) -> Image.Image:
    """Fit *pil* into *target* preserving aspect ratio.

    Uses ``LANCZOS`` for reductions (highest-quality filter for fine
    detail preservation) and ``BICUBIC`` for upscales.  Returns the
    source untouched if already within the target box.
    """
    if target.isEmpty():
        return pil
    tw = target.width()
    th = target.height()
    w, h = pil.size
    if w <= 0 or h <= 0:
        return pil
    scale = min(tw / w, th / h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    if (new_w, new_h) == (w, h):
        return pil
    resample = _LANCZOS if scale < 1.0 else _BICUBIC
    return pil.resize((new_w, new_h), resample)
