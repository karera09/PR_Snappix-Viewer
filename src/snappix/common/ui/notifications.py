"""Per-window notification history ("通知ボックス") for the snappix GUIs.

The toast surface (:mod:`.toast`) shows transient, non-modal messages in the
corner of a window.  On their own toasts are *lossy* — once one fades out the
message is gone.  A :class:`NotificationCenter` gives a window a small, capped
history so a toast can be *evacuated* (moved off the screen but kept) instead
of merely vanishing:

* a transient toast (info / success) that times out is recorded here,
* an important toast (warning / error) stays on screen until the user
  dismisses it, and is recorded here when they do,
* right- or left-clicking any on-screen toast evacuates it here immediately.

The center is a plain data store (no user-facing strings, no i18n) so it can
live in the shared design layer.  A tool that wants a visible history builds
its own panel on top by binding to the :attr:`NotificationCenter.record_added`
/ :attr:`NotificationCenter.changed` signals — no snappix tool ships such a
panel today.

Discovery mirrors the toast manager: one center is attached to a host window
as a child ``QObject`` keyed by object name, so :func:`notification_center_for`
can find (or create) it without the caller threading a reference through.  A
window that never creates one keeps the old lossy toast behaviour (the viewer
does not attach a center, so its toasts are unaffected).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class NotificationRecord:
    """One historical notification: the message, its kind, and when it arrived."""

    message: str
    kind: str = "info"
    timestamp: datetime = field(default_factory=datetime.now)


class NotificationCenter(QObject):
    """Capped, per-window notification history other widgets can observe.

    Attached to a host window as a child ``QObject`` (keyed by
    :attr:`_OBJECT_NAME`) so :func:`notification_center_for` can look it up.
    Emits :attr:`record_added` for a single new entry and :attr:`changed` for
    any mutation (add / clear) so a bound panel can refresh incrementally or
    wholesale.
    """

    _OBJECT_NAME = "_snappix_notification_center"

    #: Hard cap on retained records — the oldest are dropped past this so a
    #: long-running session can't grow the list without bound.
    MAX_RECORDS = 200

    record_added = Signal(object)  # NotificationRecord
    changed = Signal()

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setObjectName(self._OBJECT_NAME)
        self._records: list[NotificationRecord] = []

    def record(
        self,
        message: str,
        kind: str = "info",
        *,
        timestamp: datetime | None = None,
    ) -> NotificationRecord:
        """Append a notification to the history (newest last) and signal it."""
        rec = NotificationRecord(
            message=message,
            kind=kind,
            timestamp=timestamp if timestamp is not None else datetime.now(),
        )
        self._records.append(rec)
        # Trim from the front so the list never exceeds the cap.
        overflow = len(self._records) - self.MAX_RECORDS
        if overflow > 0:
            del self._records[:overflow]
        self.record_added.emit(rec)
        self.changed.emit()
        return rec

    def records(self) -> list[NotificationRecord]:
        """A snapshot copy of the history, oldest first."""
        return list(self._records)

    def count(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        """Drop all history (the panel's 「すべてクリア」)."""
        if not self._records:
            return
        self._records.clear()
        self.changed.emit()


def notification_center_for(
    window: QWidget | None, *, create: bool = False
) -> NotificationCenter | None:
    """Return (optionally creating) the notification center for *window*.

    Resolves *window* to its top-level window first (mirroring ``show_toast``),
    so passing a child widget still finds the one center attached to the frame.
    Returns ``None`` when no center exists and *create* is False — the caller
    (a toast) then falls back to its lossy behaviour.

    Direct children only: centers are always created as immediate children of
    their host window, and a recursive ``findChild`` could wrongly hand a
    parent window the center belonging to one of its child dialogs (whose
    history would then absorb the wrong window's toasts) — same rationale as
    ``toast._manager_for``.
    """
    host = window.window() if window is not None else None
    if host is None:
        return None
    existing = host.findChild(
        NotificationCenter,
        NotificationCenter._OBJECT_NAME,
        Qt.FindChildOption.FindDirectChildrenOnly,
    )
    if existing is not None:
        return existing
    if not create:
        return None
    return NotificationCenter(host)
