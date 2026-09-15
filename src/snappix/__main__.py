"""``python -m snappix`` entry point — launches the viewer.

Shares the argument handling with ``launcher.py`` (the frozen-build entry
point) so dev and frozen invocations behave identically:
``python -m snappix [--root <folder>]``.
"""

from __future__ import annotations

from ._dispatch import run

raise SystemExit(run())
