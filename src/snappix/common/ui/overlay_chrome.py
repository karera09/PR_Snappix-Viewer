"""Structural base widgets for chrome drawn ON TOP OF image content.

Companion module to :mod:`overlay`, which owns the **colours** of that
chrome.  This one owns the **structure** those overlays kept re-writing by
hand (UIレビュー 2026-08-28 N-111): the control capsule of the stage
preview (``viewer/image_view_parts/control_bar.py::_ControlBar``), the lightbox's capsule
(``viewer/lightbox_parts/overlays.py::LightboxControlCapsule``) and the two
auto-hiding pill labels (``_HintOverlay`` / ``CenterMessageOverlay``) all
repeated the same four mechanisms independently:

* the ``Qt.WA_StyledBackground`` attribute a **QWidget subclass** needs
  before Qt paints its stylesheet background at all,
* a parent-rect clamp so the floating chrome never leaves its host,
* a single-shot auto-hide timer,
* the scrim / button / label QSS built from the :mod:`overlay` palette.

Duplicating the structure duplicated its defects: the missing
``WA_StyledBackground`` of N-18 (capsule scrims not painted at all, leaving
the buttons invisible on bright photos) existed in *both* capsules in
exactly the same shape.  Everything here is therefore set up in the base
``__init__`` so a new overlay cannot forget it.

**Colours stay in :mod:`overlay`** — this module only takes them as
arguments (the two capsules deliberately differ by 5 in button-hover alpha,
so the palette constant is a parameter, never a default baked into the QSS
text).  Nothing here follows the theme: that is the registered
image-overlay exception of docs/claude/design.md 使用ルール 2.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QLabel, QToolButton, QWidget

from . import overlay
from .timers import DebounceMode, Debouncer

#: Corner radius of a control capsule (the rounded scrim behind a button row).
CAPSULE_RADIUS = 8
#: Corner radius of the hover fill inside a capsule button.
CAPSULE_BUTTON_RADIUS = 4
#: Icon edge for capsule buttons — 18px reads at the same weight as the
#: zoom-percent label beside it.
CAPSULE_ICON_PX = 18
#: Gap kept between a capsule and the edge it is pinned to.
CAPSULE_MARGIN = 12
#: Horizontal breathing room a pill leaves on each side of its parent before
#: its text has to be elided (:meth:`OverlayPill.fit_text_to_parent`).
PILL_EDGE_MARGIN = 24


def capsule_qss(
    object_name: str,
    *,
    scrim: QColor,
    button_hover: QColor,
    text_color: str,
    radius: int = CAPSULE_RADIUS,
    button_radius: int = CAPSULE_BUTTON_RADIUS,
) -> str:
    """Stylesheet for a rounded scrim capsule holding flat overlay buttons.

    *object_name* scopes the scrim rule so the fill lands on the capsule
    itself and not on every descendant ``QWidget``.

    There is deliberately **no** ``QToolButton:disabled { opacity: … }`` rule:
    Qt honours ``opacity`` only on tooltips, so the one this generator used to
    carry never did anything.  The disabled look comes from the icon instead —
    :func:`icons.fixed_icon` registers a ``QIcon.Mode.Disabled`` pixmap at the
    overlay palette's dim step.
    """
    return (
        f"QWidget#{object_name} {{"
        f" background: {overlay.rgba_str(scrim)};"
        f" border-radius: {radius}px;"
        "}"
        "QToolButton {"
        " background: transparent; border: none; padding: 5px;"
        f" border-radius: {button_radius}px;"
        "}"
        f"QToolButton:hover {{ background: {overlay.rgba_str(button_hover)}; }}"
        f"QLabel {{ color: {text_color};"
        " font-weight: bold; padding: 0 6px; }"
    )


def pill_qss(
    *,
    scrim: QColor,
    text_color: str,
    font_pt: int,
    padding_v: int,
    padding_h: int,
    radius: int,
    bold: bool = False,
) -> str:
    """Stylesheet for a rounded translucent text pill on image content."""
    weight = " font-weight: bold;" if bold else ""
    return (
        "QLabel {"
        f" background: {overlay.rgba_str(scrim)};"
        f" color: {text_color};"
        f" font-size: {font_pt}pt;"
        f"{weight}"
        f" padding: {padding_v}px {padding_h}px;"
        f" border-radius: {radius}px;"
        "}"
    )


def auto_hide_timer(owner: QWidget, interval_ms: int, slot) -> Debouncer:
    """Single-shot idle timer parented to *owner* (dies with the widget).

    A :class:`~snappix.common.ui.timers.Debouncer` in ``TRAILING`` mode: every
    ``start()`` / ``trigger()`` pushes the deadline out, which is exactly what
    "hide once the user has been idle for *interval_ms*" means.
    """
    return Debouncer(owner, interval_ms, slot, mode=DebounceMode.TRAILING)


def move_clamped(widget: QWidget, x: int, y: int) -> None:
    """Move *widget* to (*x*, *y*), clamped inside its parent's rect.

    Floating overlay chrome is positioned from a computed anchor (image
    rect, reserved filmstrip height, a 2/3-height rule…) that can land
    partly outside the host at extreme sizes.  Clamping on **both** ends of
    both axes — not just ``max(0, …)`` — keeps the chrome reachable instead
    of half off-screen.  No-op without a parent.
    """
    parent = widget.parentWidget()
    if parent is None:
        return
    max_x = max(0, parent.width() - widget.width())
    max_y = max(0, parent.height() - widget.height())
    widget.move(min(max(0, x), max_x), min(max(0, y), max_y))


class OverlayCapsule(QWidget):
    """Rounded scrim capsule of flat buttons floating over image content.

    Base for the stage's hover-revealed control bar and the lightbox's
    always-on capsule.  It sets up the three things a hand-written capsule
    kept getting wrong:

    * ``Qt.WA_StyledBackground`` — **mandatory** on a ``QWidget`` subclass
      whose background comes from a stylesheet.  Without it Qt paints
      nothing and the scrim silently disappears (N-18); a bare ``QWidget``
      *instance* does not need it, which is why the omission survived review
      for so long.
    * the capsule / button / label QSS, generated once by :func:`capsule_qss`
      from :mod:`overlay` constants passed in by the subclass.
    * uniform button construction (:meth:`make_button`) and the parent-rect
      clamp (:meth:`move_clamped`).

    Layout, contents and positioning policy stay with the subclass — the two
    capsules anchor differently (image rect vs. reserved filmstrip height)
    and only one of them fades.
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        object_name: str,
        button_hover: QColor,
        text_color: str,
        scrim: QColor = overlay.SCRIM_CHIP_STRONG,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        # Required for the stylesheet background of a QWidget *subclass*.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            capsule_qss(
                object_name,
                scrim=scrim,
                button_hover=button_hover,
                text_color=text_color,
            )
        )

    def make_button(self, ic: QIcon, tooltip: str = "") -> QToolButton:
        """Flat capsule button: fixed-colour icon, no focus, hand cursor."""
        btn = QToolButton(self)
        btn.setIcon(ic)
        btn.setIconSize(QSize(CAPSULE_ICON_PX, CAPSULE_ICON_PX))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def move_clamped(self, x: int, y: int) -> None:
        """Move to (*x*, *y*) clamped inside the parent (see the free function)."""
        move_clamped(self, x, y)


class OverlayPill(QLabel):
    """Auto-hiding translucent text pill floating over image content.

    Base for the lightbox's first-run hint and the shared centre message
    (post title / end-of-post notice).  Provides the pill QSS, the
    click-through attribute, the single-shot auto-hide timer and the
    parent-width machinery — :meth:`fit_text_to_parent` measures the real
    rendered width (padding and font size come from QSS, so ``QFontMetrics``
    cannot predict it) and binary-searches the longest prefix that fits.

    Subclasses set their text, then call :meth:`present` to show, raise and
    arm the auto-hide.
    """

    def __init__(
        self,
        parent: QWidget,
        *,
        scrim: QColor,
        text_color: str,
        font_pt: int,
        padding_v: int,
        padding_h: int,
        radius: int,
        auto_hide_ms: int,
        bold: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Pure information: never eat clicks meant for the image beneath.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            pill_qss(
                scrim=scrim,
                text_color=text_color,
                font_pt=font_pt,
                padding_v=padding_v,
                padding_h=padding_h,
                radius=radius,
                bold=bold,
            )
        )
        self.hide()
        self._auto_hide = auto_hide_timer(self, auto_hide_ms, self.hide)

    # ------------------------------------------------------------ lifecycle

    def present(self) -> None:
        """Position, show, raise and (re)start the auto-hide countdown."""
        self.reposition()
        self.show()
        self.raise_()
        self._auto_hide.start()

    def reposition(self) -> None:
        """Centre in the parent.  Subclasses override for other anchors."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.move_clamped(
            (parent.width() - self.width()) // 2,
            (parent.height() - self.height()) // 2,
        )

    def move_clamped(self, x: int, y: int) -> None:
        move_clamped(self, x, y)

    # ---------------------------------------------------------- width clamp

    def parent_width_limit(self, margin: int = PILL_EDGE_MARGIN) -> int:
        """Widest the pill may render before it must be elided."""
        parent = self.parentWidget()
        if parent is None:
            return 0
        return max(1, parent.width() - margin * 2)

    def fit_text_to_parent(
        self, text: str, *, margin: int = PILL_EDGE_MARGIN
    ) -> None:
        """Set *text*, eliding the tail until the pill fits the parent width.

        The rendered width depends on QSS-supplied font size and padding, so
        it is measured (``adjustSize`` → ``width()``) rather than estimated;
        a binary search finds the longest prefix that fits in ~7 steps even
        for an 80-character title.  Runs once per presentation.
        """
        limit = self.parent_width_limit(margin)
        self.setText(text)
        self.adjustSize()
        if not text or limit <= 0 or self.width() <= limit:
            return
        best = "…"
        lo, hi = 0, len(text)
        while lo <= hi:
            mid = (lo + hi) // 2
            cand = text[:mid].rstrip() + "…"
            self.setText(cand)
            self.adjustSize()
            if self.width() <= limit:
                best = cand
                lo = mid + 1
            else:
                hi = mid - 1
        self.setText(best)
        self.adjustSize()


__all__ = [
    "CAPSULE_BUTTON_RADIUS",
    "CAPSULE_ICON_PX",
    "CAPSULE_MARGIN",
    "CAPSULE_RADIUS",
    "PILL_EDGE_MARGIN",
    "OverlayCapsule",
    "OverlayPill",
    "auto_hide_timer",
    "capsule_qss",
    "move_clamped",
    "pill_qss",
]
