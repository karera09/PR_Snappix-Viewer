"""PyInstaller entry point for the viewer.

This module sits outside the snappix package so PyInstaller can execute it
as __main__ without triggering relative-import errors.  Argument handling
(``--root``) lives in ``snappix._dispatch`` (shared with the
``python -m snappix`` dev entry so the frozen and dev invocations can never
drift apart).
"""

from __future__ import annotations

from snappix._dispatch import run

raise SystemExit(run())
