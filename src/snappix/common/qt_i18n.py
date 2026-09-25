"""Load Qt's **own** translations (``qtbase_<locale>.qm``) into the app.

The app catalog (:mod:`snappix.common.i18n`) owns every string *we* author,
but the widgets Qt draws itself — ``QMessageBox`` の はい/いいえ,
standard dialog buttons, ``QInputDialog`` の OK/Cancel — come out of Qt's own resources and are
English until Qt's ``qtbase`` translation is installed.  On a Japanese
product the first screen a new user meets (フォルダピッカー) would therefore
be fully English.

We ship the single ``qtbase_ja.qm`` (≈130 KB) that lives in the PySide6
wheel and install it at startup.  It is part of Qt itself (LGPL — already
covered by ``THIRD_PARTY_LICENSES.txt``'s PySide6 entry) so it adds no
dependency and no new notice.

Resolution follows :mod:`snappix.common.paths`' flavour — everything is
resolved relative to the running program, never ``Path.home()`` /
``%APPDATA%`` / a Qt install registered system-wide:

* **frozen build** — ``snappix_viewer.spec`` copies the ``.qm`` next to the
  frozen PySide6 tree (``_internal/PySide6/translations/``); it is found via
  PyInstaller's ``sys._MEIPASS``, falling back to
  ``get_paths().base / "_internal"`` (the same directory, derived the way
  ``paths.py`` derives the base).
* **development run** — the PySide6 wheel's own ``translations`` folder
  (``PySide6/translations`` on Windows wheels, ``PySide6/Qt/translations``
  on some platforms).

A missing ``.qm`` is **not** an error: :func:`install_qt_translator` returns
``None`` and the app keeps Qt's English defaults rather than refusing to
start (a mangled install must still be usable enough to reinstall from).

Qt is imported lazily so this module stays importable headlessly, matching
:mod:`snappix.common.legal_docs` / :mod:`snappix.common.terms`.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Qt's base-library catalog — the one that carries the standard dialog and
#: button strings.  Other Qt catalogs (qtdeclarative / assistant / …) cover
#: modules the viewer does not use, so only this one is shipped.
QM_STEM = "qtbase"

#: Folder (relative to the PyInstaller bundle root) the spec copies the
#: ``.qm`` into.  Deliberately *inside* the frozen ``PySide6`` tree: a new
#: top-level directory under ``_internal`` would look like an unlicensed
#: package leak to ``build_portable.check_dist_complete``'s reconciliation.
BUNDLE_SUBDIR = ("PySide6", "translations")


def qm_file_name(locale: str) -> str:
    """``"ja"`` → ``"qtbase_ja.qm"`` (Qt's own naming for its catalogs)."""
    return f"{QM_STEM}_{locale}.qm"


def _bundle_dirs() -> list[Path]:
    """Candidate folders inside a frozen build (empty during a dev run)."""
    if not getattr(sys, "frozen", False):
        return []
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass).joinpath(*BUNDLE_SUBDIR))
    # Same folder, derived the way paths.py derives the portable base — a
    # backstop should a future PyInstaller drop ``_MEIPASS``.
    from .paths import get_paths

    dirs.append(
        get_paths(ensure=False).base.joinpath("_internal", *BUNDLE_SUBDIR)
    )
    return dirs


def _wheel_dirs() -> list[Path]:
    """The PySide6 wheel's own translations folder (development run)."""
    try:
        import PySide6
    except Exception:  # pragma: no cover (PySide6 is a hard dependency)
        return []
    spec_file = getattr(PySide6, "__file__", None)
    if not spec_file:  # pragma: no cover (namespace package — never here)
        return []
    root = Path(spec_file).resolve().parent
    return [root / "translations", root / "Qt" / "translations"]


def qm_search_dirs() -> list[Path]:
    """Every folder searched for a Qt ``.qm``, in priority order."""
    seen: list[Path] = []
    for path in _bundle_dirs() + _wheel_dirs():
        if path not in seen:
            seen.append(path)
    return seen


def find_qt_qm(locale: str) -> Path | None:
    """Locate Qt's ``qtbase_<locale>.qm``, or ``None`` when not shipped."""
    name = qm_file_name(locale)
    for folder in qm_search_dirs():
        candidate = folder / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover (unreadable network path)
            continue
    return None


def install_qt_translator(app, locale: str | None = None) -> Path | None:
    """Install Qt's own translations on *app*; returns the loaded ``.qm``.

    Call once at startup, after :func:`snappix.common.i18n.set_locale` and
    before any window is built — Qt's standard dialogs read the installed
    translators when they are *constructed*.  Returns ``None`` (and changes
    nothing) when no catalog ships for *locale*, so the caller can log the
    degraded state without treating it as a startup failure.

    The :class:`~PySide6.QtCore.QTranslator` is parented to *app* so it
    outlives this call — ``installTranslator`` does **not** take ownership,
    and a garbage-collected translator silently reverts every dialog to
    English.
    """
    from PySide6.QtCore import QTranslator

    from .i18n import get_locale

    target = locale or get_locale()
    path = find_qt_qm(target)
    if path is None:
        return None
    translator = QTranslator(app)
    if not translator.load(str(path)):
        translator.deleteLater()
        return None
    if not app.installTranslator(translator):  # pragma: no cover (defensive)
        translator.deleteLater()
        return None
    return path


__all__ = [
    "QM_STEM",
    "BUNDLE_SUBDIR",
    "qm_file_name",
    "qm_search_dirs",
    "find_qt_qm",
    "install_qt_translator",
]
