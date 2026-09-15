"""Touch scrolling helpers.

`QScroller.grabGesture(target, TouchGesture)` enables browser-like
kinetic flick scrolling on a scrollable widget.  It is mouse-safe by
design: only real ``QTouchEvent``\\s are intercepted, so mouse drag /
wheel / click behaviour is untouched.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QScroller,
    QScrollerProperties,
    QWidget,
)


#: Deceleration applied to a flick.  Higher than Qt's default, so a flick
#: coasts to a stop sooner (the value is a factor, not a distance).
_DECELERATION_FACTOR = 0.2
#: Finger travel (in units of the physical screen size, i.e. ~metres) that
#: must be exceeded before a touch turns into a scroll.  Higher than Qt's
#: default, so a tap is not stolen from the widget underneath.
_DRAG_START_DISTANCE = 0.008


def _build_properties() -> QScrollerProperties:
    """Browser-ish flick feel without bounce-back overshoot.

    Overshoot is disabled because the default rubber-band rebound looks
    odd inside a productivity app (lists, log view, markdown) and tends
    to expose painting artefacts in views with custom delegates.
    """
    props = QScrollerProperties()
    props.setScrollMetric(
        QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy,
        QScrollerProperties.OvershootPolicy.OvershootAlwaysOff,
    )
    props.setScrollMetric(
        QScrollerProperties.ScrollMetric.VerticalOvershootPolicy,
        QScrollerProperties.OvershootPolicy.OvershootAlwaysOff,
    )
    # Faster deceleration than Qt's default (0.125) so flicks stop near
    # where the finger leaves the screen — matches browser feel better
    # than Qt's stock "slides forever" tuning.
    props.setScrollMetric(
        QScrollerProperties.ScrollMetric.DecelerationFactor,
        _DECELERATION_FACTOR,
    )
    # Raise the press-trigger threshold above Qt's default (0.005 = 5 mm)
    # so the small finger travel of an ordinary tap on a dense grid tile
    # stays a click instead of being stolen by the scroller.  A deliberate
    # flick crosses 8 mm well before the press timeout.
    props.setScrollMetric(
        QScrollerProperties.ScrollMetric.DragStartDistance,
        _DRAG_START_DISTANCE,
    )
    return props


def enable_touch_scroll(widget: QWidget) -> None:
    """Enable kinetic touch-flick scrolling on *widget*.

    Safe to call on any widget; if it's a :class:`QAbstractScrollArea`
    subclass the gesture is attached to its viewport so child widgets
    (item delegates, embedded controls) keep receiving their own events
    normally.  For :class:`QAbstractItemView` subclasses the scroll mode
    is also switched to per-pixel for smoother flicks.

    Mouse / wheel / keyboard behaviour is unaffected — ``TouchGesture``
    only intercepts ``QTouchEvent``.
    """
    if isinstance(widget, QAbstractItemView):
        widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        widget.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    if isinstance(widget, QAbstractScrollArea):
        target = widget.viewport()
    else:
        target = widget

    QScroller.grabGesture(target, QScroller.ScrollerGestureType.TouchGesture)
    scroller = QScroller.scroller(target)
    scroller.setScrollerProperties(_build_properties())
