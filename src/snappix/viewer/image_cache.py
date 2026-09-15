"""Bounded LRU cache for decoded viewer images.

Shared by :class:`MarkdownView` (post.md embedded images, keyed by URL) and
:class:`ImageView` (standalone image files, keyed by path).  Memory use is
bounded by three independent limits:

* ``max_bytes`` — running total of cached payload bytes.
* ``max_entries`` — hard cap on the number of live entries.
* ``max_single_bytes`` — reject any individual value larger than this
  threshold.  Huge RAW/PSD decodes would otherwise evict the entire cache
  on a single ``put`` and still not fit.

``put`` returns the list of keys that were evicted to make room, so the
caller can run any side-effect cleanup (e.g. swapping a placeholder back
into a ``QTextDocument`` resource cache).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Collection, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


# --------- Tunables (bytes unless noted) --------------------------------

# MarkdownView: images decoded at viewport_px × dpr, stored as QImage.
# 256 MiB fits ~30 full-HD images on a 1.5 dpr screen; evicted images
# are re-decoded when they scroll back near the viewport by
# ``MarkdownView._refresh_visible_images`` (QTextDocument never re-invokes
# ``loadResource`` for a resource it still holds, so the view drives the
# reload itself rather than relying on Qt to re-request it).
MARKDOWN_CACHE_MAX_BYTES = 256 * 1024 * 1024
MARKDOWN_CACHE_MAX_ENTRIES = 128
MARKDOWN_CACHE_MAX_SINGLE_BYTES = 64 * 1024 * 1024

# ImageView: full-resolution PIL images, used as LANCZOS source for zoom.
# Budget is sized for modern CG collections: a 4K RGB image is ~25 MB but
# a 5000×7000 RGBA page decodes to ~130 MB — the old 512 MiB budget held
# only 3 such pages, so the radius-2 prefetch churned the current/next
# entries straight out of the LRU.  2 GiB keeps a ±3 neighborhood of even
# those pages resident.  (``state.py`` carries the same default for the
# settings dialog, with a 512→2048 migration in ``load_state``.)
IMAGEVIEW_CACHE_MAX_BYTES = 2048 * 1024 * 1024
IMAGEVIEW_CACHE_MAX_ENTRIES = 16
IMAGEVIEW_CACHE_MAX_SINGLE_BYTES = 200 * 1024 * 1024

# Number of neighboring image files to prefetch on each side of the
# currently shown image in ImageView.
IMAGEVIEW_PREFETCH_RADIUS = 2


class BoundedImageCache(Generic[V]):
    """Byte-budgeted LRU cache with an oversize rejection rule."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_entries: int,
        max_single_bytes: int,
        sizeof: Callable[[V], int],
    ) -> None:
        if max_bytes <= 0 or max_entries <= 0 or max_single_bytes <= 0:
            raise ValueError("cache limits must be positive")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._max_single_bytes = max_single_bytes
        self._sizeof = sizeof
        self._entries: OrderedDict[Hashable, tuple[V, int]] = OrderedDict()
        self._total_bytes = 0

    # ------------------------------------------------------------------ API

    def get(self, key: Hashable) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(
        self,
        key: Hashable,
        value: V,
        *,
        protect: Collection[Hashable] = (),
    ) -> list[Hashable]:
        """Insert *value* and return the keys evicted to fit within budget.

        If *value* is larger than ``max_single_bytes`` the insert is
        skipped silently (returns ``[]``) — the cache is intentionally a
        fast path only for values that can reasonably be kept around.

        *protect* names existing entries that must survive this insert.
        When *value* cannot fit without evicting a protected entry, the
        insert itself is rejected (returns ``[]``).  ImageView's prefetch
        uses this so a far neighbor landing late never pushes the
        on-screen image or a nearer neighbor out of the LRU — without it,
        a byte budget smaller than the prefetch working set evicts
        exactly the entries the user is about to hit.  Keys in *protect*
        that are absent (or equal to *key*) are ignored.
        """
        size = max(0, int(self._sizeof(value)))
        if size > self._max_single_bytes:
            # Oversized: also drop any existing entry at this key so a
            # stale small version isn't resurrected later.
            existing = self._entries.pop(key, None)
            if existing is not None:
                self._total_bytes -= existing[1]
            return []

        protected = {k for k in protect if k != key and k in self._entries}
        if protected:
            protected_bytes = sum(self._entries[k][1] for k in protected)
            if (
                size + protected_bytes > self._max_bytes
                or len(protected) + 1 > self._max_entries
            ):
                # Fitting this value would require evicting a protected
                # entry — reject the insert instead (existing entry at
                # this key, if any, stays valid: same key ⇒ same decode).
                return []

        existing = self._entries.pop(key, None)
        if existing is not None:
            self._total_bytes -= existing[1]

        self._entries[key] = (value, size)
        self._total_bytes += size

        evicted: list[Hashable] = []
        if protected:
            # Evict LRU→MRU skipping protected keys and the new entry.
            # Terminates before exhausting the dict: the feasibility check
            # above proved the new entry + protected set fit the budget.
            for candidate in list(self._entries):
                if (
                    self._total_bytes <= self._max_bytes
                    and len(self._entries) <= self._max_entries
                ):
                    break
                if candidate == key or candidate in protected:
                    continue
                _, candidate_size = self._entries.pop(candidate)
                self._total_bytes -= candidate_size
                evicted.append(candidate)
            return evicted
        while self._entries and (
            self._total_bytes > self._max_bytes
            or len(self._entries) > self._max_entries
        ):
            # ``popitem(last=False)`` pops the LRU end.  The just-inserted
            # key is at the MRU end and won't be touched unless it's the
            # only entry and already over budget — handled below.
            evicted_key, (_, evicted_size) = self._entries.popitem(last=False)
            self._total_bytes -= evicted_size
            if evicted_key == key:
                # The entry we just put was the one pushed out (cache
                # was already over budget with this single value).  Report
                # it as evicted so callers can clean up, then bail out so
                # we don't loop forever on an empty cache.
                evicted.append(evicted_key)
                break
            evicted.append(evicted_key)
        return evicted

    def pop(self, key: Hashable) -> V | None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return None
        self._total_bytes -= entry[1]
        return entry[0]

    def clear(self) -> None:
        self._entries.clear()
        self._total_bytes = 0

    def reconfigure(
        self,
        *,
        max_bytes: int,
        max_entries: int,
        max_single_bytes: int,
    ) -> list[Hashable]:
        """Apply new limits, evicting LRU entries to fit.  Returns evicted keys.

        Entries already larger than the new ``max_single_bytes`` are
        removed wholesale — a later ``put`` for the same key would be
        rejected anyway, and keeping them around would pin memory the
        user just asked to reclaim.
        """
        if max_bytes <= 0 or max_entries <= 0 or max_single_bytes <= 0:
            raise ValueError("cache limits must be positive")
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._max_single_bytes = max_single_bytes

        evicted: list[Hashable] = []
        for key in list(self._entries.keys()):
            _, size = self._entries[key]
            if size > max_single_bytes:
                del self._entries[key]
                self._total_bytes -= size
                evicted.append(key)
        while self._entries and (
            self._total_bytes > self._max_bytes
            or len(self._entries) > self._max_entries
        ):
            key, (_, size) = self._entries.popitem(last=False)
            self._total_bytes -= size
            evicted.append(key)
        return evicted

    def bytes(self) -> int:
        return self._total_bytes

    def __contains__(self, key: Hashable) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)
