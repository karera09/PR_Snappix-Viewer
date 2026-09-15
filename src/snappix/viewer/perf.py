"""Thread-safe performance recorder for viewer diagnostics.

Captures per-operation timings so the user can see which stage is the
actual bottleneck when folder scans / thumbnails feel slow:

* ``scan_children`` — shallow ``os.scandir`` + stat on the current root
* ``folder_preview`` — per-subfolder ``scandir`` that collects post.md +
  first image (NAS round-trip dominates)
* ``post_md_read`` — just the ``read_text`` of a post.md
* ``thumb_decode`` — ``QImageReader.read()`` cost on a worker thread
  (disk/network + JPEG/PNG decode)
* ``thumb_scale`` — ``QImage.scaled()`` CPU cost on a worker thread
* ``pixmap_convert`` — ``QPixmap.fromImage`` + ``setIcon`` main-thread
  cost per icon flush
* ``visible_request`` — viewport scan that enqueues thumbnail tasks
* ``populate_step`` — one chunked grid-insertion round
* ``metadata_batch`` — applying one enriched batch onto the grid

The recorder is disabled by default (record() becomes a cheap
``_enabled`` boolean check).  The user toggles it from the diagnostics
menu.  Both the scan worker pool and the thumbnail pool call into this
module, so every mutation sits behind a lock.

This module is deliberately Qt-free — it is also imported by the folder
scanning code which runs on the ``scan_worker`` thread pool before any
Qt object has been constructed on those threads.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import median


@dataclass
class EventRecord:
    category: str
    duration_ms: float
    detail: str = ""
    ts: float = 0.0  # time.perf_counter() at completion


@dataclass
class CategoryStats:
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=512))

    def add(self, duration_ms: float) -> None:
        if self.count == 0:
            self.min_ms = duration_ms
            self.max_ms = duration_ms
        else:
            if duration_ms < self.min_ms:
                self.min_ms = duration_ms
            if duration_ms > self.max_ms:
                self.max_ms = duration_ms
        self.count += 1
        self.total_ms += duration_ms
        self.samples.append(duration_ms)

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def median_ms(self) -> float:
        return median(self.samples) if self.samples else 0.0


class PerfRecorder:
    def __init__(self, max_events: int = 4000) -> None:
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=max_events)
        self._stats: dict[str, CategoryStats] = defaultdict(CategoryStats)
        self._enabled = False
        self._started_at: float | None = None

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self._enabled:
                self._started_at = time.perf_counter()
            self._enabled = enabled

    def is_enabled(self) -> bool:
        # Read without the lock: a stale ``False`` just means the very
        # first event after toggling on might be missed, which is fine —
        # saves us a mutex per record() call on the thumbnail hot path.
        return self._enabled

    def record(self, category: str, duration_ms: float, detail: str = "") -> None:
        with self._lock:
            if not self._enabled:
                return
            self._events.append(EventRecord(
                category=category,
                duration_ms=duration_ms,
                detail=detail,
                ts=time.perf_counter(),
            ))
            self._stats[category].add(duration_ms)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._stats.clear()
            self._started_at = time.perf_counter() if self._enabled else None

    def snapshot(self) -> tuple[list[EventRecord], dict[str, CategoryStats], float]:
        """Return (events, stats, elapsed_since_start_sec).

        Both collections are copied so the caller doesn't need the lock
        while iterating.  Elapsed is since the most recent enable/clear;
        0.0 if recording is currently disabled and has never been run.
        """
        with self._lock:
            events = list(self._events)
            stats = {}
            for k, v in self._stats.items():
                dup = CategoryStats(
                    count=v.count,
                    total_ms=v.total_ms,
                    min_ms=v.min_ms,
                    max_ms=v.max_ms,
                    samples=deque(v.samples),
                )
                stats[k] = dup
            if self._started_at is None:
                elapsed = 0.0
            else:
                elapsed = time.perf_counter() - self._started_at
            return events, stats, elapsed


_recorder = PerfRecorder()


def recorder() -> PerfRecorder:
    return _recorder


class measure:
    """Context manager that records elapsed time into the singleton.

    When the recorder is disabled, only a boolean check runs on enter
    and exit — no ``perf_counter`` calls, no dict lookups.  Cheap enough
    to leave in place on hot paths (thumbnail decode, scandir loops).

    Usage::

        with measure("scan_children", str(root)):
            entries = scan_children(root)
    """

    __slots__ = ("_category", "_detail", "_start")

    def __init__(self, category: str, detail: str = "") -> None:
        self._category = category
        self._detail = detail
        self._start = 0.0

    def __enter__(self) -> "measure":
        if _recorder.is_enabled():
            self._start = time.perf_counter()
        else:
            self._start = 0.0
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._start != 0.0:
            duration_ms = (time.perf_counter() - self._start) * 1000.0
            _recorder.record(self._category, duration_ms, self._detail)


__all__ = ["PerfRecorder", "CategoryStats", "EventRecord", "measure", "recorder"]
