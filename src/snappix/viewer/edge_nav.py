"""Shared wheel → sibling-file navigation routing for the preview sub-views.

Every centre-pane preview implements the same gesture: when the mouse
wheel can no longer scroll the content (the scrollbar is already at its
limit in the wheel's direction), the view emits
``navigate_requested(delta, immediate)`` so the hosting ``ContentView``
can advance the sibling-file selection — with the grace-period gating
applied there (see ``ContentView._on_navigate_requested``).

Historically each view hand-rolled the「delta 取得 → ``_classify_edge`` →
emit → accept」boilerplate with slight per-view drift; these helpers are
the single home for that routing.  Three tiers cover every consumer:

* :func:`navigate_on_wheel` — non-scrollable chrome (``FileInfoView``,
  ``ZipView``/``TextView`` outside their inner widget,
  ``FolderPreviewView``): any vertical wheel navigates immediately.
* :func:`navigate_at_edge` — scrollable content whose intra-view
  scrolling is handled elsewhere (``PdfView``'s ``QPdfView``, the inner
  ``eventFilter`` of ``ZipView``/``TextView``): only the at-edge wheel
  navigates, everything else falls through to the default handler.
* :func:`route_scroll_or_navigate` — full preview panes that also own
  their scrolling speed (``ImageView``, ``MarkdownView``): at-edge wheel
  navigates, otherwise the view scrolls by the user-tunable
  ``view_prefs`` pixel step.

``emit`` is the bound ``navigate_requested.emit`` — the helpers stay
signal-agnostic so they are unit-testable without a widget.
"""

from __future__ import annotations

import time

from . import view_prefs
from .view_prefs import _classify_edge, _scroll_with_pixels


def wheel_nav_delta(event) -> int:
    """Sibling step for a wheel event: ``-1`` (wheel up → previous file),
    ``+1`` (wheel down → next file), or ``0`` when there is no vertical
    delta (pure-horizontal wheels never navigate)."""
    dy = event.angleDelta().y()
    if not dy:
        return 0
    return -1 if dy > 0 else 1


def navigate_on_wheel(event, emit) -> bool:
    """Unconditional immediate navigation (non-scrollable chrome).

    Returns ``True`` (and accepts the event) when a navigation was
    emitted; ``False`` when the event has no vertical delta and the
    caller should fall through to ``super().wheelEvent``.
    """
    delta = wheel_nav_delta(event)
    if delta == 0:
        return False
    emit(delta, True)
    event.accept()
    return True


def navigate_at_edge(scrollbar, event, emit) -> bool:
    """Emit navigation only when the wheel hits *scrollbar*'s travel limit.

    ``immediate`` is ``True`` when the content fits the viewport (no
    reading-via-scroll gesture to protect — see ``_classify_edge``), which
    skips the grace period downstream.  Returns ``True`` (event accepted)
    only when a navigation was emitted; otherwise the caller lets the
    default scrolling happen.
    """
    dy = event.angleDelta().y()
    if not dy:
        return False
    immediate, at_edge = _classify_edge(scrollbar, dy)
    if not at_edge:
        return False
    emit(-1 if dy > 0 else 1, immediate)
    event.accept()
    return True


def route_scroll_or_navigate(widget, event, emit) -> bool:
    """Full preview-pane wheel routing: edge navigation, else user-speed
    scrolling.

    *widget* is a ``QAbstractScrollArea``-like object (must expose
    ``verticalScrollBar()``).  Returns ``True`` when the event was
    consumed (either a navigation was emitted or the view scrolled by
    the ``view_prefs`` pixel step); ``False`` means the caller should
    fall through to ``super().wheelEvent`` (e.g. modifier-key or
    horizontal wheels that :func:`view_prefs._scroll_with_pixels`
    deliberately leaves alone).
    """
    if navigate_at_edge(widget.verticalScrollBar(), event, emit):
        return True
    return _scroll_with_pixels(widget, event)


class WheelNavGate:
    """Grace-period gate for at-edge wheel navigation.

    Extracted from ``ContentView._on_navigate_requested`` so the fullscreen
    lightbox (``lightbox.py``) can reuse the exact same gesture semantics
    without re-implementing them:

    * ``immediate`` events (content fits the viewport — no reading-via-scroll
      gesture to protect) fire right away and reset any pending grace state.
    * Otherwise the first at-edge wheel only *arms* the gate; the user must
      keep wheeling in the same direction for ``view_prefs._WHEEL_NAV_GRACE_SEC``
      before :meth:`check` returns ``True``.  Direction changes and idle gaps
      longer than ``view_prefs._WHEEL_NAV_IDLE_RESET_SEC`` restart the gesture.

    The ``view_prefs`` values are read live (module-attribute access) so the
    settings dialog's runtime changes apply without reconstruction.  *now* is
    injectable for tests; production callers omit it.
    """

    def __init__(self) -> None:
        self._dir: int = 0          # direction of the current grace window
        self._first_ts: float = 0.0  # when grace started
        self._last_ts: float = 0.0   # last at-edge wheel — for idle reset

    def reset(self) -> None:
        self._dir = 0
        self._first_ts = 0.0
        self._last_ts = 0.0

    def check(self, delta: int, immediate: bool, now: float | None = None) -> bool:
        """Return ``True`` when the navigation should actually fire."""
        if immediate:
            self.reset()
            return True
        if now is None:
            now = time.monotonic()
        direction = 1 if delta > 0 else -1
        idle = now - self._last_ts
        if (
            self._dir != direction
            or idle > view_prefs._WHEEL_NAV_IDLE_RESET_SEC
        ):
            self._dir = direction
            self._first_ts = now
            self._last_ts = now
            # 猶予 0 は「最初の端ホイールで即ナビ」という設定の契約
            # （view_prefs の既定値コメント / 設定ダイアログのヒント文言）。
            # アームだけして False を返すと 2 ノッチ目まで切り替わらず、
            # 文言と実挙動が食い違う。
            return view_prefs._WHEEL_NAV_GRACE_SEC <= 0
        self._last_ts = now
        if now - self._first_ts < view_prefs._WHEEL_NAV_GRACE_SEC:
            return False
        self._first_ts = now
        return True


__all__ = [
    "WheelNavGate",
    "navigate_at_edge",
    "navigate_on_wheel",
    "route_scroll_or_navigate",
    "wheel_nav_delta",
]
