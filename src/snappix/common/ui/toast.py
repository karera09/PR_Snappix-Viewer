"""Non-modal toast notifications for the snappix GUI tools.

A toast is a small, self-dismissing message floated in the bottom-right of a
window.  It is the design system's answer to *success* feedback: the "成功=
非モーダル、失敗=モーダル、破壊的操作=確認モーダル" principle (see
docs/claude/design.md) — success and other transient status should never
interrupt with a modal dialog.

Public API::

    show_toast(window, message, kind="info", duration_ms=3000) -> Toast

``kind`` is one of ``"info"`` / ``"success"`` / ``"warning"`` / ``"error"``;
it only colours a thin accent stripe (the body always uses the raised
surface + primary text so the message stays legible in either theme).

All colours come from the app-wide QSS (``qss.py::build_qss`` styles
``QFrame#toast``; the ``kind`` stripe is selected via the ``toastKind``
dynamic property set at construction) — nothing here hardcodes a hex value
or a point size, and theme switches restyle live toasts automatically with
the rest of the application (#184; the per-widget ``setStyleSheet`` this
replaced needed a ``changeEvent(PaletteChange)`` hook that re-entered
itself on Windows — #117/#117追補).  Toasts stack upward from the corner,
dismiss on click, fade out after ``duration_ms``, and follow their host
window's resize/move via an event filter.  They are children of the host
window, so they are torn down with it and never outlive it.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QWidget,
)

from .notifications import notification_center_for
from .timers import DebounceMode, Debouncer

# Valid ``kind`` values → the semantic token role driving the accent stripe
# (the QSS side of the mapping lives in ``qss.py::build_qss``'s
# ``QFrame#toast[toastKind=...]`` rules — parity is test-guarded).
# "info" uses the accent (primary) colour; the rest map to status roles.
_KIND_ROLES: dict[str, str] = {
    "info": "accent",
    "success": "success",
    "warning": "warning",
    "error": "danger",
}

# Layout metrics (px).  Spacing between stacked toasts and the window edge —
# kept here rather than hardcoded at call sites.
_MARGIN = 16
_GAP = 8
# Fraction of the host window width a toast may grow to before wrapping.
_MAX_WIDTH_FRACTION = 0.40
# Horizontal chrome around the label = the frame layout's left + right margins
# (see ``setContentsMargins`` below).  Kept as a named constant because
# ``refresh_size`` measures the text and has to add exactly this back.
_LABEL_H_PADDING = 28
_FADE_MS = 220
# Most toasts kept visible at once before the oldest are evacuated (to the
# notification history when a center is attached, otherwise dismissed — see
# ``_ToastManager._enforce_visible_cap``).  Prevents a burst of completion /
# queue toasts from stacking up the whole edge of the window.
_MAX_VISIBLE = 4


class Toast(QFrame):
    """A single floating notification pinned to a host window's corner.

    Instances are created via :func:`show_toast`; the class is public only so
    callers can type-annotate the return value or dismiss a toast early.
    """

    def __init__(
        self,
        window: QWidget,
        message: str,
        kind: str = "info",
        duration_ms: int = 3000,
        action_text: str | None = None,
        on_action: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(window)
        self._host = window
        self._kind = kind if kind in _KIND_ROLES else "info"
        self._message = message
        # warning / error are "important": when a notification center is
        # attached they stay on screen until the user dismisses them (see the
        # dismiss-timer gate below), rather than auto-expiring like info /
        # success.  Recorded to the center on removal regardless.
        self._important = self._kind in ("warning", "error")
        self._closing = False

        # A child (not a top-level tool window) so it is destroyed with the
        # host and clipped to its client area; no native title bar.
        self.setObjectName("toast")
        # The app-wide QSS (qss.py) selects the kind's accent stripe on this
        # dynamic property; it is fixed for the toast's lifetime and set
        # before show(), so no unpolish/repolish wiring is needed (#184).
        self.setProperty("toastKind", self._kind)
        self.setFrameShape(QFrame.NoFrame)
        self.setAttribute(Qt.WA_StyledBackground, True)
        # Clicks anywhere dismiss immediately; let clicks pass to the label too.
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_LABEL_H_PADDING // 2, 10, _LABEL_H_PADDING // 2, 10)
        layout.setSpacing(0)
        self._label = QLabel(message, self)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(self._label)

        # Optional follow-up action (「ログフォルダを開く」 after a bulk delete —
        # UIレビュー 2026-08-28 N-40).  A plain QPushButton so it inherits the
        # app-wide QSS (no colours here); its click is consumed by the button,
        # so the frame's dismiss-on-click never fires for it.  The toast is
        # dismissed explicitly afterwards — the affordance is spent.
        self._action_btn: QPushButton | None = None
        if action_text and on_action is not None:
            btn = QPushButton(action_text, self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.clicked.connect(lambda: self._run_action(on_action))
            layout.addSpacing(10)
            layout.addWidget(btn)
            self._action_btn = btn

        # Opacity effect drives the fade-in / fade-out animation.
        # Deliberately NO QGraphicsDropShadowEffect anywhere on this frame:
        # Qt does not support nesting graphics effects — a second effect on
        # any descendant (the label) re-enters the offscreen paint of this
        # one and floods stderr with "QPainter::begin: A paint device can
        # only be painted by one painter at a time" (+ a cascade of "Painter
        # not active") on every repaint, and the shadow never renders
        # correctly anyway.  Depth comes from the 1px border in the app QSS.
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        # Tracks whichever QPropertyAnimation (fade-in or fade-out) is
        # currently driving ``_opacity`` so a dismiss triggered mid-fade-in
        # (click or timer firing before the fade-in finishes) stops the old
        # animation before starting a new one — two QPropertyAnimations
        # racing on the same opacity effect otherwise fight over the value.
        self._anim: QPropertyAnimation | None = None

        self._dismiss_timer = Debouncer(
            self, max(0, int(duration_ms)), self.dismiss, mode=DebounceMode.TRAILING
        )
        # When a notification center is attached to the host window, important
        # toasts (warning / error) do not auto-expire — they stay on screen
        # until the user dismisses / evacuates them; only transient (info /
        # success) toasts time out into the history.  With no center (e.g. the
        # viewer) every kind auto-expires as before, so this is a no-op there.
        center_present = notification_center_for(window, create=False) is not None
        self._sticky_important = center_present and self._important
        if duration_ms > 0 and not self._sticky_important:
            self._dismiss_timer.trigger()
        # Following the host's resize / move is the *stack's* job, not each
        # toast's: ``_ToastManager`` owns the single event filter (#186).

    # -------------------------------------------------------------- sizing
    def _max_width(self) -> int:
        return max(160, int(self._host.width() * _MAX_WIDTH_FRACTION))

    def refresh_size(self) -> None:
        """Recompute width (bounded by the host) and height for the message.

        The width is **measured**, not left to ``QLabel``'s size hint: with
        ``wordWrap=True`` Qt's aspect-ratio heuristic settled every toast at
        137–146px even though ``_max_width()`` allowed 576, so short messages
        wrapped onto two lines for no reason and the stack came out ragged
        (UIレビュー 2026-08-28 N-56, 旧 N-154 統合).  Measuring the longest
        line and pinning that width fixes both at once; wrapping still kicks
        in for anything past ``_max_width()``.
        """
        cap = self._max_width()
        reserved = _LABEL_H_PADDING
        if self._action_btn is not None:
            # The action button is laid out beside the label, so the text has
            # that much less room before it must wrap.
            reserved += self._action_btn.sizeHint().width() + 10
        fm = self._label.fontMetrics()
        # ``horizontalAdvance`` measures one line, so split explicit newlines
        # and take the widest.
        longest = max(
            (fm.horizontalAdvance(line) for line in self._label.text().split("\n")),
            default=0,
        )
        # Pin the LABEL, not the frame: ``adjustSize`` then derives the frame
        # width from it, so the layout margins, the action button AND the QSS
        # border widths are accounted for by Qt instead of re-guessed here.
        inner_cap = max(80, cap - reserved)
        self._label.setFixedWidth(min(longest + 1, inner_cap))
        self.setMaximumWidth(cap)
        self.adjustSize()

    # ----------------------------------------------------------- lifecycle
    def show_toast(self) -> None:
        """Make visible and fade in."""
        self.refresh_size()
        self.show()
        self.raise_()
        self._stop_running_animation()
        anim = QPropertyAnimation(self._opacity, b"opacity", self)
        anim.setDuration(_FADE_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._anim = anim  # keep a ref until it self-deletes / is stopped

    def dismiss(self) -> None:
        """Fade out then remove from the manager and destroy."""
        if self._closing:
            return
        self._closing = True
        self._dismiss_timer.stop()
        # A dismiss can land while the fade-in animation above is still
        # running (fast click, or a very short duration_ms); stop it first
        # so it doesn't keep racing the fade-out on the same opacity effect.
        self._stop_running_animation()
        anim = QPropertyAnimation(self._opacity, b"opacity", self)
        anim.setDuration(_FADE_MS)
        anim.setStartValue(self._opacity.opacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(self._finalize)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._anim = anim

    def _stop_running_animation(self) -> None:
        """Stop and drop the reference to any in-flight fade animation.

        Both fade animations are started with ``DeleteWhenStopped``, so once a
        fade finishes on its own the underlying C++ ``QPropertyAnimation`` is
        destroyed while ``self._anim`` still holds the now-dangling Python
        wrapper.  Calling ``.stop()`` on that wrapper raises ``RuntimeError``
        (``libshiboken: Internal C++ object ... already deleted``).  Drop the
        reference *before* touching it and treat an already-deleted animation
        as a no-op — a finished animation needs no stopping.
        """
        anim, self._anim = self._anim, None
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                pass

    def _finalize(self) -> None:
        # Evacuate the message into the window's notification history (if one is
        # attached) so a dismissed / expired toast is not silently lost — the
        # user can still find it in the 通知履歴 panel.  Idempotent because
        # dismiss()'s ``_closing`` guard makes _finalize run at most once.
        center = notification_center_for(self._host, create=False)
        if center is not None:
            center.record(self._message, self._kind)
        mgr = _manager_for(self._host, create=False)
        if mgr is not None:
            mgr.remove(self)
        self.hide()
        self.deleteLater()

    # -------------------------------------------------------------- events
    # NOTE deliberately no ``changeEvent`` override: toast colours live in
    # the app-wide QSS (``qss.py``), which ``apply_theme`` regenerates —
    # every live toast (including a ``duration_ms=0`` sticky one) is
    # re-polished with the rest of the application on a theme switch.  The
    # per-widget restyle hook this replaced re-entered itself on Windows
    # (``setStyleSheet`` re-delivers ``PaletteChange``) and needed a re-entry
    # guard to avoid an infinite recursion (#117 / #117追補 / #184).

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self.dismiss()
        super().mousePressEvent(event)

    def _run_action(self, callback: Callable[[], None]) -> None:
        """Invoke the follow-up action, then retire the toast.

        Failures in *callback* must not take the toast (or the window) down —
        it is opportunistic UI, and the caller has already reported the real
        outcome in the message itself.
        """
        try:
            callback()
        except Exception:  # pragma: no cover (defensive)
            logger.exception("toast action failed")
        self.dismiss()


class _ToastManager(QObject):
    """Per-window registry that lays stacked toasts out from the corner.

    One manager is attached to each host window (as a child ``QObject`` keyed
    by object name) so multiple toasts on the same window share a stack and
    are all torn down with the window.

    It also owns the **single** event filter on the host: the stack's geometry
    is a property of the stack, so one filter re-lays it out once per host
    Resize / Move / Show.  Installing one filter per toast (as this did
    originally) made a single window drag run ``reposition`` — itself an
    all-toasts relayout — once per live toast, i.e. O(N²) work for one
    event (#186).  Lifetime needs no bookkeeping either: the manager is a
    child of the host and dies with it.
    """

    _OBJECT_NAME = "_snappix_toast_manager"

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setObjectName(self._OBJECT_NAME)
        self._window = window
        self._toasts: list[Toast] = []
        window.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self._window and event.type() in (
            QEvent.Resize,
            QEvent.Move,
            QEvent.Show,
        ):
            if self._toasts:
                self.reposition()
        return False

    def add(self, toast: Toast) -> None:
        self._toasts.append(toast)
        toast.show_toast()
        self._enforce_visible_cap()
        self.reposition()

    def _visible_cap(self, alive: list[Toast]) -> int:
        """How many toasts fit in the host's corner, never more than the flat cap.

        :func:`reposition` stacks upward from the bottom and clamps the top at
        ``_MARGIN``, so everything past the host's height piles onto the same
        row and the ones underneath become unreadable.  A short host (a
        collapsed window, a small dialog) hits that well before the flat cap
        does, so the geometry has a say too.  At least one toast is always
        kept — a cramped message still beats no message.
        """
        usable = self._window.height() - 2 * _MARGIN - self._status_height()
        row = max((t.height() for t in alive), default=0) + _GAP
        if row <= _GAP:  # nothing measurable yet
            return _MAX_VISIBLE
        return max(1, min(_MAX_VISIBLE, usable // row))

    def _status_height(self) -> int:
        """Height of the host's visible status bar (0 when it has none)."""
        if not isinstance(self._window, QMainWindow):
            return 0
        for sb in self._window.findChildren(
            QStatusBar, options=Qt.FindDirectChildrenOnly
        ):
            if sb.isVisible():
                return sb.height()
        return 0

    def _enforce_visible_cap(self) -> None:
        """Evacuate the oldest toasts once the on-screen stack exceeds the cap.

        Applies to every host.  Where a notification center is attached the
        evicted toasts land in its history; without one they are dismissed,
        which is the same loss as expiring — and strictly better than being
        buried under the stack with no way to read them (:func:`reposition`
        clamps at the top edge, so the overflow all lands on one row).
        Transient toasts are evicted before important (warning / error) ones;
        ties break oldest-first (``self._toasts`` is append-ordered).
        """
        alive = [t for t in self._toasts if not _is_deleted(t) and not t._closing]
        over = len(alive) - self._visible_cap(alive)
        if over <= 0:
            return
        transient = [t for t in alive if not t._important]
        important = [t for t in alive if t._important]
        for toast in (transient + important)[:over]:
            toast.dismiss()

    def remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        self.reposition()

    def reposition(self) -> None:
        """Stack live toasts bottom-right, newest lowest, growing upward.

        On a QMainWindow the stack starts above the status bar so a toast
        never covers its path / load-status text (UIレビュー #22).  Probed
        via findChild — ``statusBar()`` would *create* one on windows that
        don't have it.

        堅牢化 (UIレビュー 07-25 #138): 直接の子に QStatusBar が複数ある
        （非表示の予備バーを保持する等）構成でも、``findChildren`` から
        **可視のもの**を選ぶ — 先頭 1 個を掴んで「不可視バーの高さ 0」に
        当たると、実バーの上ではなくバーの裏側にトーストが積まれる。
        """
        alive = [t for t in self._toasts if not _is_deleted(t)]
        self._toasts = alive
        y = self._window.height() - _MARGIN - self._status_height()
        for toast in reversed(alive):  # newest (appended last) sits lowest
            toast.refresh_size()
            w, h = toast.width(), toast.height()
            x = self._window.width() - _MARGIN - w
            y -= h
            toast.move(max(_MARGIN, x), max(_MARGIN, y))
            toast.raise_()
            y -= _GAP


def _is_deleted(obj: QObject) -> bool:
    """True if the underlying C++ object has already been destroyed."""
    try:
        obj.objectName()
    except RuntimeError:
        return True
    return False


def _manager_for(window: QWidget, *, create: bool) -> _ToastManager | None:
    """Return (optionally creating) the toast manager for a host window.

    Direct children only: managers are always created as immediate children
    of their host window, and a recursive ``findChild`` could wrongly hand a
    parent window the manager belonging to one of its child dialogs (whose
    toasts would then stack inside the wrong window).
    """
    existing = window.findChild(
        _ToastManager,
        _ToastManager._OBJECT_NAME,
        Qt.FindChildOption.FindDirectChildrenOnly,
    )
    if existing is not None:
        return existing
    if not create:
        return None
    return _ToastManager(window)


def show_toast(
    window: QWidget,
    message: str,
    kind: str = "info",
    duration_ms: int = 3000,
    action_text: str | None = None,
    on_action: Callable[[], None] | None = None,
) -> Toast:
    """Float a self-dismissing *message* in *window*'s bottom-right corner.

    ``kind`` ∈ ``{"info", "success", "warning", "error"}`` selects the accent
    stripe colour (unknown values fall back to ``"info"``).  ``duration_ms``
    is the visible lifetime before the fade-out; pass ``0`` to keep the toast
    until it is clicked (or the window is destroyed).

    ``action_text`` + ``on_action`` add **one** follow-up button beside the
    message (「ログフォルダを開く」 after a bulk delete — UIレビュー 2026-08-28
    N-40).  It is an optional shortcut, never the only way to reach the thing
    it opens: a toast is transient, so anything essential belongs in a
    persistent surface.  Give an actionable toast a longer ``duration_ms``
    than the 3 s default — the user has to notice, read and aim at it.

    The toast is parented to *window* (a top-level window is expected — if a
    child widget is passed, its own window is used) so it is clipped to and
    destroyed with it.  Returns the :class:`Toast` for early ``dismiss()``.
    """
    host = window.window() if window is not None else None
    if host is None:  # pragma: no cover - defensive
        raise ValueError("show_toast requires a window")
    toast = Toast(
        host,
        message,
        kind=kind,
        duration_ms=duration_ms,
        action_text=action_text,
        on_action=on_action,
    )
    manager = _manager_for(host, create=True)
    assert manager is not None
    manager.add(toast)
    return toast
