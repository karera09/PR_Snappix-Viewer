"""Pure date-range helpers for the PostGrid advanced-search panel.

Qt-free so it can be unit-tested without a QApplication.  The advanced-search
panel offers a "投稿日" (posted date) filter with relative presets
(today / last 7 / 30 / 365 days) and an explicit start–end range; these helpers
turn a preset selection into a concrete ``(lo, hi)`` half-open datetime window
and decide whether a folder's ``posted_at`` falls inside it.

The window is **half-open** ``lo <= posted < hi`` so an explicit end date is
inclusive of the whole end day (``hi`` is the day *after* the picked end date).
Entries whose ``posted_at`` is unknown (loose image files, and tag-search folders
collapsed without reading ``post.md``) are kept by default so a relative preset
never silently drops them.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

# Preset keys shared with the combo's item data + ViewerState.
DATE_PRESETS = ("all", "today", "7d", "30d", "1y", "range")

_RELATIVE_DAYS = {"7d": 7, "30d": 30, "1y": 365}


def preset_range(
    preset: str,
    *,
    now: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return the ``(lo, hi)`` half-open window for *preset*.

    * ``all`` → ``(None, None)`` (no filtering).
    * ``today`` → from local midnight today, no upper bound.
    * ``7d`` / ``30d`` / ``1y`` → from N days before today's midnight, no upper.
    * ``range`` → ``(start_midnight, end_midnight + 1 day)`` so the end day is
      fully included.  A missing *start* or *end* leaves that bound ``None``.
      A reversed pair (*start* after *end*) is normalised by **swapping the
      two dates**: returning the raw ``lo > hi`` window would be
      unsatisfiable for every dated entry, and with ``keep_unknown=True`` the
      result set would silently degrade to "only entries without a date" —
      an inexplicable screen, not an obviously-empty one.  The date editors
      clamp against each other in the UI, so this is the defence-in-depth
      layer for values arriving from restored state or future callers.

    ``now`` is injectable for deterministic tests (defaults to ``datetime.now()``).
    A ``None`` bound means "unbounded on that side".
    """
    if preset == "all" or not preset:
        return None, None
    base = now or datetime.now()
    midnight = datetime.combine(base.date(), time.min)
    if preset == "today":
        return midnight, None
    if preset in _RELATIVE_DAYS:
        return midnight - timedelta(days=_RELATIVE_DAYS[preset]), None
    if preset == "range":
        s = start.date() if start is not None else None
        e = end.date() if end is not None else None
        if s is not None and e is not None and s > e:
            # 逆転入力(開始 > 終了)は充足不能な窓になるため日付を入れ替えて
            # 正規化する(docstring 参照)。
            s, e = e, s
        lo = datetime.combine(s, time.min) if s is not None else None
        hi = (
            datetime.combine(e, time.min) + timedelta(days=1)
            if e is not None
            else None
        )
        return lo, hi
    return None, None


def date_matches(
    posted_at: datetime | None,
    lo: datetime | None,
    hi: datetime | None,
    *,
    keep_unknown: bool = True,
) -> bool:
    """True if *posted_at* falls within the half-open window ``[lo, hi)``.

    * ``lo is None and hi is None`` → always True (no filter active).
    * ``posted_at is None`` → ``keep_unknown`` (entries without a parsed date
      are kept by default rather than dropped by a relative preset).
    * otherwise → ``(lo is None or lo <= posted) and (hi is None or posted < hi)``.

    ``posted_at`` may be tz-aware (``post.md``'s ``- posted_at:`` line is
    ISO 8601 and usually carries a timezone) while the bounds
    from :func:`preset_range` are naive local — an aware value is normalised to
    local naive before comparing so the mix never raises ``TypeError``.
    """
    if lo is None and hi is None:
        return True
    if posted_at is None:
        return keep_unknown
    if posted_at.tzinfo is not None:
        posted_at = posted_at.astimezone().replace(tzinfo=None)
    if lo is not None and posted_at < lo:
        return False
    if hi is not None and posted_at >= hi:
        return False
    return True


__all__ = ["DATE_PRESETS", "preset_range", "date_matches"]
