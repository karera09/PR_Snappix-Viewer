"""Open the shipped legal documents from the running GUI.

The portable dist carries ``THIRD_PARTY_LICENSES.txt`` (plus the stand-alone
``licenses/`` LGPL/GPL texts) and ``利用規約・免責事項.txt`` next to the two
EXEs — placed there by ``build_portable.py::write_license_notices`` /
``write_terms_document`` and verified by ``check_dist_complete``. In a dev
checkout the same files exist at the repo root as committed copies, so
``get_paths().base`` resolves them in both frozen and dev runs.

The viewer's Help menu uses :func:`open_third_party_licenses` so users can
reach the notices without hunting through the install folder; the scanner may
not import snappix, so its Help menu re-implements the lookup in
``tagger/_terms.py`` against the same dist layout. Qt is imported lazily —
this module stays importable headlessly (mirrors ``common/terms.py``).
"""

from __future__ import annotations

from pathlib import Path

from .paths import get_paths

THIRD_PARTY_LICENSES_NAME = "THIRD_PARTY_LICENSES.txt"

#: The shipped quick-start guide, rendered from ``README.md`` at build time
#: (``build_portable.py::README_DIST_NAME`` — keep the two in step).
GETTING_STARTED_NAME = "はじめにお読みください.txt"

#: Dev-run fallback for :func:`getting_started_path`: the committed source the
#: build renders the shipped .txt from.  A dev checkout has no dist root, so
#: without this the Help entry could only ever report "file missing" locally.
_GETTING_STARTED_DEV_SOURCE = "README.md"


def third_party_licenses_path() -> Path:
    """The shipped third-party notice file (next to the EXEs / at repo root)."""
    return get_paths().base / THIRD_PARTY_LICENSES_NAME


def getting_started_path() -> Path | None:
    """The shipped 「はじめにお読みください.txt」, or ``None`` when absent.

    Frozen builds carry the rendered plain-text guide beside the EXEs; a dev
    checkout has only the committed ``README.md`` it is rendered from, so that
    is used as the fallback (UIレビュー 08-28 N-114).
    """
    base = get_paths().base
    shipped = base / GETTING_STARTED_NAME
    if shipped.is_file():
        return shipped
    dev_source = base / _GETTING_STARTED_DEV_SOURCE
    return dev_source if dev_source.is_file() else None


def open_shipped_file(path: Path | None, parent, missing_name: str) -> bool:
    """Open *path* with the OS default viewer; warn if it's missing.

    Shared by the Help-menu doc openers — plugin-agnostic on purpose (#178):
    a pack that ships its own document hands the resolved path in (via its
    ``ai_pack`` provider), so this module never knows any pack's internal
    layout.  Returns ``True`` when the open was dispatched.

    Both failure modes are reported here rather than by the callers (the Help
    menu entries discard the return value, so a silent ``False`` would leave
    the menu item looking dead): when *path* is ``None`` or absent (mangled
    install / provider not registered) a message box names *missing_name*;
    when the file is there but the OS refuses to open it (no association for
    ``.txt``, policy / AV blocking the shell verb) a different box says so and
    points at the dist folder.
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    from .i18n import t

    if path is None or not path.is_file():
        QMessageBox.warning(
            parent,
            t("common.legal.file_missing_title"),
            t("common.legal.file_missing_body", name=missing_name),
        )
        return False
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.warning(
            parent,
            t("common.legal.open_failed_title"),
            t("common.legal.open_failed_body", name=missing_name),
        )
        return False
    return True


def open_third_party_licenses(parent=None) -> bool:
    """Open ``THIRD_PARTY_LICENSES.txt`` with the OS default text viewer.

    Returns ``True`` when the open was dispatched; when the file is missing
    (mangled install), shows a message box and returns ``False``.
    """
    return open_shipped_file(
        third_party_licenses_path(), parent, THIRD_PARTY_LICENSES_NAME
    )
