"""Screen-aware placement for anchored popovers / popup frames.

Every toolbar popover in the GUI is a frameless ``Qt.Popup`` ``QFrame``
positioned with a bare ``move(anchor.mapToGlobal(...))``.  Unlike ``QMenu``,
a plain popup widget gets no automatic screen clamping from Qt, so a popover
opened while the window sits at a screen edge (or maximized) can extend past
the screen and become unreadable / unclickable.  :func:`popover_position` is
the single place that turns "under this anchor" into a global position kept
inside the anchor's screen.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize
from PySide6.QtWidgets import QWidget

__all__ = ["popover_position"]


def popover_position(
    anchor: QWidget, size: QSize, *, align: str = "left"
) -> QPoint:
    """Global top-left for a popup of *size* opened under *anchor*.

    ``align="left"`` hangs the popup from the anchor's bottom-left corner;
    ``align="right"`` right-aligns it to the anchor's bottom-right (the
    trailing-edge toolbar buttons).  The naive position is then clamped into
    the anchor's screen ``availableGeometry``: horizontally it is shifted
    back inside, vertically it flips above the anchor when there is no room
    below (falling back to a bottom clamp when it fits neither side) and is
    clamped at the top edge as well — an anchor can sit above the work area
    (a window pushed under a top-docked taskbar), and the restore guard only
    promises the window intersects *some* screen.
    """
    rect = anchor.rect()
    if align == "right":
        x = anchor.mapToGlobal(rect.bottomRight()).x() - size.width() + 1
    else:
        x = anchor.mapToGlobal(rect.bottomLeft()).x()
    y = anchor.mapToGlobal(rect.bottomLeft()).y()
    screen = anchor.screen()
    if screen is not None:
        avail = screen.availableGeometry()
        x = max(avail.left(), min(x, avail.right() - size.width() + 1))
        if y + size.height() - 1 > avail.bottom():
            above = anchor.mapToGlobal(rect.topLeft()).y() - size.height()
            if above >= avail.top():
                y = above
            else:
                y = max(avail.top(), avail.bottom() - size.height() + 1)
        # Same both-ends clamp the x axis already gets.
        y = max(avail.top(), y)
    return QPoint(x, y)
