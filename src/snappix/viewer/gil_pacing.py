"""GIL-convoy pacing for pure-Python worker-thread hot loops.

**Why this exists** (measured 2026-07, do not remove): worker threads that
run *pure-Python* CPU bursts back-to-back — image header parsing
(``PIL.Image.open`` header plugins + ``getexif``), ``post.md`` parsing,
``os.scandir`` entry loops on cached/local trees — starve the GUI thread of
the GIL even though every heavy decode/encode/sqlite call correctly releases
it.  With 6 header-probe workers the GUI thread's longest GIL wait measured
**~300 ms** (vs a ~30 ms idle baseline); that is the whole-app stutter seen
during cache builds and folder scans.  This is CPython's GIL convoy /
starvation behaviour: on each 5 ms switch interval *any* waiting thread may
win, and a pack of always-ready workers keeps winning over the event loop.

Remedies that do NOT work (measured):

* ``sys.setswitchinterval(0.001)`` — no improvement (winner is still random).
* ``sys.setswitchinterval(0.0001)`` — fixes stalls but slashes worker
  throughput ~20× *process-wide*.
* Fewer workers (6 → 2) — still ~80 ms stalls.
* ``time.sleep(0)`` yields — actively worse (~800 ms).

What works: a real ``time.sleep(0.001)`` every N items *per worker thread*.
The sleeping thread parks off the GIL for a full timer quantum (~1–16 ms on
Windows), which reliably hands the GUI thread a slot.  End-to-end (real
``CacheBuilder``, aspect-only, 20k local files, GUI timer-lag probe):
no pacing → 212 ms max GUI stall; every-8 → 57–94 ms; **every-4 → 7.7 ms
(= timer noise) at +29 % build time** on a local-SSD worst case — on a NAS
the sleeps overlap I/O waits, so the real cost is smaller still.

Usage: create one :class:`GilPacer` per loop/pool (sharing across the pool's
threads is fine — counters are per-thread) and call :meth:`GilPacer.tick`
once per processed item.  Only worth adding where **multiple** worker
threads run pure-Python bursts; single-threaded loops are already preempted
fine by the switch interval, and Pillow decode / WebP encode / sqlite all
release the GIL (verified — pacing there is unnecessary).
"""

from __future__ import annotations

import threading
import time

# Sleep once per this many ticks (per worker thread).  4 is the measured
# sweet spot in the end-to-end build: GUI stalls drop to timer noise
# (~8 ms) where 8 still let 57–94 ms spikes through, and the throughput
# cost stays well above NAS-served rates.
PACE_EVERY = 4
# One switch-interval-sized sleep.  The effective park is the OS timer
# granularity (up to ~16 ms on Windows), which is exactly what hands the
# GUI thread a clean GIL slot.
PACE_SECONDS = 0.001


class GilPacer:
    """Per-thread "sleep 1 ms every N items" pacing for worker loops.

    Thread-safe by construction: the tick counter lives in a
    ``threading.local``, so one instance can be shared across an executor's
    threads and each thread still paces its *own* item stream (the semantics
    the fix was measured with).
    """

    __slots__ = ("_every", "_local")

    def __init__(self, every: int = PACE_EVERY) -> None:
        self._every = max(1, int(every))
        self._local = threading.local()

    def tick(self) -> None:
        """Count one processed item; sleep when this thread hits the stride."""
        count = getattr(self._local, "count", 0) + 1
        if count >= self._every:
            count = 0
            time.sleep(PACE_SECONDS)
        self._local.count = count


__all__ = ["GilPacer", "PACE_EVERY", "PACE_SECONDS"]
