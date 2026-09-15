"""Shared entry-point dispatch for the frozen and dev launchers.

``launcher.py`` (PyInstaller, repo root) and ``snappix/__main__.py``
(``python -m snappix``) must interpret the folder argument identically; both
are thin shells around :func:`run`.  The only supported application is the
viewer — this indirection exists so argument handling / windowed-build error
logging live in exactly one place.

Two ways to point the viewer at a folder are accepted, so Explorer's native
gestures work (L02):

* ``--root <path>`` — the explicit flag (used by the dev launcher / tests).
* a bare positional ``<path>`` — what Windows passes when a folder is dropped
  onto ``SnappixViewer.exe``, when the shell "Snappix Viewer で開く" verb runs
  ``"<exe>" "%1"``, or when the user types ``SnappixViewer.exe D:\\Photos``.

``--root`` wins when both are present.  A file path (rather than a folder) is
resolved to its parent folder with the file pre-selected — that happens
downstream in ``viewer.app`` where the filesystem probe already lives.
"""

from __future__ import annotations

import argparse
import sys


def _launcher_breadcrumb(line: str) -> None:
    """Append one diagnostic line to ``data/logs/launcher.log`` (best effort).

    The frozen viewer is windowed — no console — so this file is the only
    place a startup-time diagnosis can surface.  Never raises: a failure to
    log must not take the app down."""
    try:
        from snappix.common.paths import get_paths

        log = get_paths().logs / "launcher.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # pragma: no cover - best-effort diagnostics
        pass


def parse_viewer_args() -> argparse.Namespace:
    """Parse the viewer args, surfacing argparse errors in windowed builds.

    The frozen viewer is a PyInstaller *windowed* app: it has no console, so
    argparse's default ``SystemExit(2)`` + stderr message is invisible and
    the app looks like it "does nothing".  Catch that and write the reason to
    ``data/logs/launcher.log`` before re-raising, so the failure is at least
    diagnosable.  (The dev entry shares this path, so ``python -m snappix``
    gets the same breadcrumb.)

    Unknown arguments stay tolerated (``parse_known_args``) but are logged
    too, so a mistyped safe-mode flag (``--no_plugins``; note that argparse
    still ACCEPTS the abbreviation ``--no-plugin``) is diagnosable instead of
    vanishing without a trace.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", default=None)
    # Safe mode: skip loading every plugin (viewer/plugin_host).  The escape
    # hatch when a broken/malicious plugin prevents normal startup — documented
    # in docs/PLUGIN_DEVELOPMENT.md.  SNAPPIX_NO_PLUGINS=1 works too (for
    # frozen windowed builds launched without a console).
    parser.add_argument("--no-plugins", action="store_true", default=False)
    # Positional fallback for Explorer folder-drop / file association / shell
    # verb (L02).  Distinct dest from ``--root`` so both can coexist; ``run()``
    # prefers the explicit flag when both are supplied.
    parser.add_argument("root_pos", nargs="?", default=None)
    try:
        args, unknown = parser.parse_known_args()
    except SystemExit:
        _launcher_breadcrumb(f"viewer argument error; argv={sys.argv!r}")
        raise
    if unknown:
        # Unknown flags are deliberately tolerated (they must not abort the
        # windowed app), but a silent drop is invisible exactly when it hurts:
        # a user escaping a broken plugin who mistypes ``--no-plugins`` (e.g.
        # ``--no_plugins``) gets the plugins loaded again with no clue why.
        # Leave a breadcrumb.
        _launcher_breadcrumb(f"viewer ignored unknown argument(s): {unknown!r}")
    return args


def run() -> int:
    """Launch the viewer; return its exit code."""
    args = parse_viewer_args()

    from snappix.viewer.app import main as viewer_main

    # The explicit ``--root`` flag wins; otherwise fall back to the bare
    # positional path (Explorer drop / association / shell verb).
    initial_root = args.root if args.root is not None else args.root_pos
    return viewer_main(initial_root=initial_root, no_plugins=args.no_plugins)
