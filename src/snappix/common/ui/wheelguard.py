"""App-wide guard that removes mouse-wheel value editing.

Scrolling the wheel over a ``QComboBox`` or a spin box (``QSpinBox`` /
``QDoubleSpinBox`` / ``QDateEdit`` — all ``QAbstractSpinBox`` subclasses)
changes its value by default.  In a scrollable settings panel this is a
frequent source of *silent* setting changes: the user scrolls the panel,
the cursor happens to sit over a combo / spin box, and a concurrency count,
cache size or request delay shifts without being noticed.

:func:`install_wheel_value_guard` installs one application-wide event filter
that swallows the wheel gesture on those widget types.  When the widget sits
inside a scroll area the wheel is forwarded to that area's viewport, so the
surrounding panel still scrolls smoothly; otherwise the event is simply
dropped.  Every other interaction — typing, clicking the arrows, keyboard,
drag — is untouched; only the wheel gesture stops mutating the value.

Sliders and scroll bars are intentionally left alone: ``QScrollBar``'s wheel
handling *is* scrolling, and a ``QSlider`` wheel is a deliberate
direct-manipulation gesture (media volume / seek, thumbnail size), not a
silent accident.  We therefore match ``QComboBox`` and ``QAbstractSpinBox``
only — never ``QAbstractSlider``.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)

#: Widget types whose wheel-to-change-value behaviour we remove.  Both are
#: base classes: ``QAbstractSpinBox`` covers ``QSpinBox`` / ``QDoubleSpinBox``
#: / ``QDateEdit`` / ``QDateTimeEdit``; ``QComboBox`` covers editable and
#: non-editable combos.  ``QAbstractSlider`` (sliders + scroll bars) is
#: deliberately excluded.
_TARGET_TYPES = (QComboBox, QAbstractSpinBox)


def _enclosing_scroll_viewport(widget: QWidget) -> QWidget | None:
    """Return the viewport of *widget*'s nearest scroll-area ancestor, or None."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent.viewport()
        parent = parent.parentWidget()
    return None


class _WheelValueGuard(QObject):
    """Application event filter: neutralise wheel value edits (see module doc)."""

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Type.Wheel:
            return False
        if not isinstance(obj, _TARGET_TYPES):
            return False
        # Swallow the value-changing wheel.  If the widget lives inside a
        # scroll area, hand the same gesture to that area's viewport so the
        # panel still scrolls; otherwise just drop it.  The viewport is a
        # plain QWidget, so this filter ignores the forwarded event (no loop).
        viewport = _enclosing_scroll_viewport(obj)
        if viewport is not None:
            QApplication.sendEvent(viewport, event)
        return True


_guard: _WheelValueGuard | None = None
#: The application :data:`_guard` is installed on — the idempotence key.
#: "Does a guard object exist?" would skip the install on a *second*
#: QApplication in the same process, leaving the wheel editing values again
#: with nothing to show for it.
_guard_app: QApplication | None = None


def install_wheel_value_guard(app: QApplication) -> None:
    """Install the wheel-value guard on *app*, once per application.

    Idempotent: repeated calls (e.g. every ``apply_theme`` on a theme switch)
    reuse the single installed filter.  A different application gets the
    filter installed on it as well — the guard object is parentless and holds
    no per-application state, so it is reused rather than rebuilt.
    """
    global _guard, _guard_app
    if _guard_app is app:
        return
    if _guard is None:
        _guard = _WheelValueGuard()
    app.installEventFilter(_guard)
    _guard_app = app
