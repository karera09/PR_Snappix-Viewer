"""Allow `python -m snappix.viewer` to launch the viewer.

Delegates to the shared entry dispatch (like ``snappix/__main__.py`` and
``launcher.py``) so this alias honours ``--root`` / the bare positional
folder / ``--no-plugins`` instead of silently ignoring them — the launchers
must not carry their own argument branching.
"""

from .._dispatch import run

raise SystemExit(run())
