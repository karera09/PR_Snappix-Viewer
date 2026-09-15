"""Shared drag-to-export helper (file → Explorer / editors / chat clients).

``GalleryView`` (grid tiles) and ``ImageView`` (the centre image preview)
both let the user drag the shown file into other applications.  The
payload (``QMimeData.setUrls``), the drag-preview downscale cap and the
hot-spot centring used to be duplicated in each view; this module is the
single implementation so future preview surfaces get the identical
behaviour from one call.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDrag, QPixmap

#: Longest edge (logical px) of the drag-preview pixmap.  Thumbnails and
#: full-resolution previews larger than this are downscaled so the ghost
#: image under the cursor stays a compact affordance rather than covering
#: the drop target.
DRAG_PREVIEW_EDGE = 160


def start_file_export_drag(
    source,
    path: Path,
    pixmap: QPixmap | None = None,
    *,
    preview_edge: int = DRAG_PREVIEW_EDGE,
) -> None:
    """Begin a copy-action file drag for *path* originating from *source*.

    *source* is the widget the drag starts from (``QDrag``'s parent).
    *pixmap*, when provided and non-null, becomes the drag preview —
    downscaled to *preview_edge* if larger, with the hot spot centred so
    the ghost follows the cursor symmetrically.

    Blocks in ``QDrag.exec`` until the user drops or cancels (standard Qt
    drag semantics); callers must have already cleared their own
    press/drag arming state before calling.
    """
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    drag = QDrag(source)
    drag.setMimeData(mime)
    if pixmap is not None and not pixmap.isNull():
        pm = pixmap
        # Grid thumbnails carry a devicePixelRatio (``thumbnail_loader``
        # stamps it), so both the cap and the hot spot must be computed in
        # *logical* px: ``QPixmap.width()`` is physical while
        # ``QDrag.setHotSpot`` takes logical coordinates.  Comparing the
        # physical width against the logical cap shrank the ghost to 1/dpr
        # of the intended edge, and the physical half-width put the cursor
        # at the ghost's bottom-right corner on hi-DPI displays.
        dpr = pm.devicePixelRatio() or 1.0
        logical = pm.deviceIndependentSize()
        if logical.width() > preview_edge or logical.height() > preview_edge:
            target = round(preview_edge * dpr)
            pm = pm.scaled(
                target, target,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            logical = pm.deviceIndependentSize()
        drag.setPixmap(pm)
        drag.setHotSpot(
            QPoint(int(logical.width()) // 2, int(logical.height()) // 2)
        )
    drag.exec(Qt.CopyAction)


__all__ = ["DRAG_PREVIEW_EDGE", "start_file_export_drag"]
