"""Shared cancellation / generation primitives for the viewer's scanners.

Every off-thread scanner in the viewer answers the same question — 「このスキャン
結果はまだ現役か」— with two values: a *generation* number (so a consumer can
drop stale payloads that were already emitted) and a cooperative *cancel*
flag (so the producer stops working early).  Historically the pair lived as
two separate fields on every scanner and had to be updated in lock-step by
hand, which drifted: some ``cancel()`` implementations bumped the
generation, others didn't; some tasks early-returned when queued-cancelled,
others didn't (項目#35 — the root mechanism behind 確定指摘 #4/#5/#9).

This module now owns the pairing:

* :class:`ScanSession` — one request's identity: its generation **and** its
  cancel flag, fused into a single object handed to the worker task.
* :class:`SessionOwner` — the scanner-side owner of the succession of
  sessions.  ``start()`` cancels the previous session and issues the next
  generation; ``cancel()`` cancels **and bumps** (so an emit already queued
  under the old generation can never pass a ``latest_generation()``
  comparison — the behaviour ``RecursiveSearchScanner`` pioneered);
  ``accepts(gen)`` is the single predicate "generation matches ∧ not
  cancelled" that completion slots use instead of hand-written pairs.
* :class:`SessionRunnable` — a ``QRunnable`` base whose ``run()`` returns
  before doing any work when the session is already cancelled (a queued
  task cannot be pulled back out of a ``QThreadPool``), then delegates to
  the subclass's ``_run()``.

:class:`CancelToken` remains as a thin alias of :class:`ScanSession` for the
call sites that only need the single-shot flag (``zip_drill``,
``health_dialog``, ``post_grid``'s body filter, ``scan_search``, the AI
plugin's scanners) — same ``cancel()`` / ``is_cancelled()`` contract as
before, no behavioural change.

Threading: only the main thread calls ``start`` / ``cancel``; workers poll
``is_cancelled`` and read ``generation``.  A plain ``bool`` / ``int``
handoff is safe under CPython's GIL — no lock required.
"""

from __future__ import annotations

from PySide6.QtCore import QRunnable


class ScanSession:
    """One scan request's identity: a generation number + a cancel flag.

    Only the main thread calls :meth:`cancel`; only the worker polls
    :meth:`is_cancelled`.  ``generation`` is immutable after construction —
    the *owner* mints a new session per request rather than mutating one.
    """

    __slots__ = ("generation", "_flag")

    def __init__(self, generation: int = 0) -> None:
        self.generation = int(generation)
        self._flag = False

    def cancel(self) -> None:
        self._flag = True

    def is_cancelled(self) -> bool:
        return self._flag


class SessionOwner:
    """Owns a scanner's succession of :class:`ScanSession`\\ s.

    Guarantees the invariant the hand-rolled pairs kept breaking: the cancel
    flag and the generation always move together.
    """

    __slots__ = ("_generation", "_current")

    def __init__(self) -> None:
        self._generation = 0
        self._current: ScanSession | None = None

    def start(self) -> ScanSession:
        """Cancel the previous session (if any) and issue the next one."""
        if self._current is not None:
            self._current.cancel()
        self._generation += 1
        self._current = ScanSession(self._generation)
        return self._current

    def cancel(self) -> None:
        """Cancel the current session **and bump the generation**.

        The bump is what invalidates emissions already sitting in the queued
        signal pipeline (a cancel token cannot recall those): a consumer
        comparing against :meth:`latest_generation` — or a slot using
        :meth:`accepts` — drops them.  Idempotent: a second cancel with
        nothing current is a no-op (no extra bump).
        """
        if self._current is None:
            return
        self._current.cancel()
        self._current = None
        self._generation += 1

    def accepts(self, generation: int) -> bool:
        """世代一致 ∧ 未キャンセル — 完了スロットの単一述語."""
        cur = self._current
        return (
            cur is not None
            and cur.generation == generation
            and not cur.is_cancelled()
        )

    def current(self) -> ScanSession | None:
        """The live session, or ``None`` after a cancel with no restart."""
        return self._current

    def latest_generation(self) -> int:
        return self._generation


class SessionRunnable(QRunnable):
    """``QRunnable`` base that skips work for an already-cancelled session.

    A queued runnable cannot be pulled back out of a ``QThreadPool``; without
    this guard a navigation burst makes every superseded task still pay its
    full enumeration before noticing the cancel (確定指摘 #5 の型).  The
    guard lives in ``run()`` so no subclass can forget it — subclasses
    implement :meth:`_run` instead.
    """

    def __init__(self, session: ScanSession) -> None:
        super().__init__()
        self.session = session
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D401 (Qt API)
        if self.session.is_cancelled():
            return
        self._run()

    def _run(self) -> None:  # pragma: no cover (abstract)
        raise NotImplementedError


# Backwards-compatible aliases: the single-shot flag callers (and the
# historical ``scan_worker._CancelToken`` import path) keep working — a
# ``ScanSession`` with the default generation IS the old ``CancelToken``.
CancelToken = ScanSession
_CancelToken = ScanSession

__all__ = [
    "CancelToken",
    "ScanSession",
    "SessionOwner",
    "SessionRunnable",
    "_CancelToken",
]
