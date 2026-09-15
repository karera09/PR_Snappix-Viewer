"""Shared human-readable formatting helpers.

Single home for byte-size formatting across the tools (the viewer's
status bar, grid captions, detail window, settings dialog — and any
future plugin surface).  The viewer previously carried three
divergent copies ("1.5 MiB" vs "1.5 MB" vs "1.50 MB" for the same
maths); every consumer now goes through :func:`format_bytes` so the
same file size reads identically everywhere.
"""

from __future__ import annotations


def format_bytes(n: int) -> str:
    """Format *n* bytes for humans: ``512 B`` / ``1.5 KiB`` / ``3.0 MiB``.

    1024-based, so the binary unit names (KiB/MiB/…) are used — mixing a
    1024 divisor with decimal labels ("MB") is the inconsistency this
    helper exists to remove.  Bytes are shown as an integer; every larger
    unit gets one decimal place.  The unit choice accounts for the display
    rounding: a value just under a unit boundary that would *round* to
    1024.0 (e.g. ``1024*1024 - 1`` bytes) is carried into the next unit
    ("1.0 MiB", never "1024.0 KiB").
    """
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(n)
    for unit in units:
        if unit == "B":
            if size < 1024:
                return f"{int(size)} {unit}"
        elif round(size, 1) < 1024 or unit == units[-1]:
            # round() mirrors the f-string's own rounding, so the printed
            # number is guaranteed < 1024 for every non-cap unit.
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # unreachable
