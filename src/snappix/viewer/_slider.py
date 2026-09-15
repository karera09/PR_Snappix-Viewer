"""Shared QSlider helpers for the viewer."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider


class _ClickJumpFilter(QObject):
    # Qt's default reaction to a left-click on the slider groove is a
    # page-step. Modern media UIs jump the handle to the cursor instead.
    # We intercept the mouse press, move the handle so it is centred on
    # the click, and return False so QSlider's own mousePressEvent runs
    # next — by then the handle is under the cursor, so Qt enters its
    # normal drag flow (sliderPressed / sliderReleased fire as usual).
    def eventFilter(self, obj, event):  # noqa: ANN001
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and isinstance(obj, QSlider)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            _jump_to_click(obj, event)
        return False


def _jump_to_click(slider: QSlider, event) -> None:  # noqa: ANN001
    opt = QStyleOptionSlider()
    slider.initStyleOption(opt)
    style = slider.style()
    groove = style.subControlRect(
        QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, slider
    )
    handle = style.subControlRect(
        QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, slider
    )
    pos = event.position().toPoint()
    if handle.contains(pos):
        return
    if slider.orientation() == Qt.Horizontal:
        length = handle.width()
        lo = groove.x()
        hi = groove.right() - length + 1
        px = pos.x() - length // 2 - lo
    else:
        length = handle.height()
        lo = groove.y()
        hi = groove.bottom() - length + 1
        px = pos.y() - length // 2 - lo
    span = max(1, hi - lo)
    new_val = QStyle.sliderValueFromPosition(
        slider.minimum(),
        slider.maximum(),
        px,
        span,
        opt.upsideDown,
    )
    slider.setValue(new_val)


def enable_click_jump(slider: QSlider) -> None:
    # Parent the filter to the slider so it lives exactly as long as the
    # slider does, and is not garbage-collected on the Python side.
    filt = _ClickJumpFilter(slider)
    slider.installEventFilter(filt)
