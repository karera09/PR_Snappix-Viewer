# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the portable Snappix Viewer (viewer app).

Run via: python build_portable.py   (or: pyinstaller snappix_viewer.spec)

No native runtime binaries are bundled beyond what the Python wheels carry
(Qt DLLs incl. the Qt Multimedia FFmpeg backend come from the PySide6 wheel).
The tag scanner is a separate frozen app — see the AI plugin's
plugins/snappix_ai/snappix_tagger.spec (built by its build_hook.py).
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is a PyInstaller-provided global: the directory containing this
# spec file (= the repo root, where build_portable.py lives).
sys.path.insert(0, SPECPATH)
from build_portable import (
    DIST_METADATA_ENV,
    VERSION_INFO_ENV,
    frozen_stdlib_module_names,
)

# Both build inputs below are GENERATED from pyproject.toml's project.version
# by build_portable.py, which hands their paths over in the environment (a
# spec runs in the PyInstaller subprocess, so that is the only channel).  A
# bare ``pyinstaller snappix_viewer.spec`` is a dev convenience and simply
# gets neither: the exe then carries no version resource and the app reports
# its placeholder version.  Shipping builds go through build_portable.py,
# whose check_dist_complete requires both.
version_info = os.environ.get(VERSION_INFO_ENV) or None
dist_metadata = os.environ.get(DIST_METADATA_ENV)

# Qt's own Japanese translations for the standard dialogs (QMessageBox の
# はい/いいえ, QFileDialog の列見出し・ボタン, QInputDialog の OK/Cancel).
# Without this single ~130 KB catalog the first screen a user meets — the
# folder picker — is fully English on a Japanese product (UIレビュー
# 2026-08-28 N-01).  It is part of Qt itself (LGPL), already covered by the
# PySide6 entry in THIRD_PARTY_LICENSES.txt: no new dependency, no new notice.
#
# Destination is the frozen PySide6 tree, NOT a new top-level folder: a fresh
# directory under _internal/ would read as an unlicensed package leak to
# build_portable.check_dist_complete's reconciliation.  The runtime resolves
# it from there (snappix/common/qt_i18n.py).
import PySide6

_QT_QM_NAME = "qtbase_ja.qm"
_pyside_root = Path(PySide6.__file__).resolve().parent
datas = []
for _sub in ("translations", "Qt/translations"):
    _qm = _pyside_root / _sub / _QT_QM_NAME
    if _qm.is_file():
        datas.append((str(_qm), "PySide6/translations"))
        break
else:
    raise SystemExit(
        f"{_QT_QM_NAME} not found under {_pyside_root} — the Japanese product "
        "would ship English Qt dialogs (N-01).  Check the PySide6 install."
    )

# The distribution metadata ``snappix.__version__`` reads at runtime: a frozen
# app has no installed distribution, so it ships as data at the root of
# _internal (= the frozen sys.path), where importlib.metadata finds it.  It is
# a *.dist-info directory, which the license reconciliation skips by name.
if dist_metadata:
    _meta = Path(dist_metadata)
    datas += [
        (str(p), _meta.name) for p in sorted(_meta.iterdir()) if p.is_file()
    ]

hiddenimports = []
# The launcher reaches snappix.viewer.app through a function-level import;
# collect the whole first-party tree so a lazily-imported viewer module can
# never be missed by static analysis.
hiddenimports += collect_submodules("snappix")
# Audio/video preview (viewer/media_view.py imports QtMultimedia lazily).
hiddenimports += collect_submodules("PySide6.QtMultimedia")
hiddenimports += collect_submodules("PySide6.QtMultimediaWidgets")
# PDF preview + PDF thumbnails: both import QtPdf lazily now
# (content.pdf_view.PdfView.__init__ / thumbnail_loader._decode_pdf), so declare
# them the same way as QtMultimedia above rather than relying on a
# module-level import for static analysis to find (レビュー 2026-08-30).
hiddenimports += collect_submodules("PySide6.QtPdf")
hiddenimports += collect_submodules("PySide6.QtPdfWidgets")
# The frozen viewer hosts in-process plugins, whose vendored third-party
# dependencies may import stdlib modules the viewer's own import graph never
# touches (static analysis once shipped a bundle without e.g. http.cookies).
# Bundle the complete standard library, minus the host-owned denylist in
# build_portable.FROZEN_STDLIB_SKIP (tkinter and friends).  Non-package /
# unavailable-on-this-platform names fall back to a plain hidden-import
# entry, which PyInstaller resolves or warns about harmlessly.
for _name in frozen_stdlib_module_names():
    try:
        hiddenimports += collect_submodules(_name)
    except Exception:
        hiddenimports.append(_name)

a = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Never freeze the tagger's heavy stack into the viewer.
        # "_tkinter" is the C-extension backing tkinter; excluding "tkinter"
        # alone does not stop PyInstaller's hook-_tkinter from bundling the
        # Tcl/Tk runtime, so name it explicitly (also denylisted in
        # build_portable.FROZEN_STDLIB_SKIP).
        "torch", "torchvision", "timm", "tensorflow", "matplotlib",
        "tkinter", "_tkinter",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        # numpy is the semantic-search math dependency (viewer/vector_index.py,
        # imported ONLY lazily via viewer/ai_pack.py).  The plain distribution
        # ships without AI search, so numpy is excluded here and supplied by
        # the paid AI plugin's vendor/ folder instead (build_portable.py
        # assembles it).  check_dist_complete verifies the exclusion held.
        "numpy",
        # rich / pygments / python-dotenv are pulled ONLY through pydantic's
        # OPTIONAL imports (pydantic._internal._core_utils → rich → pygments;
        # pydantic.v1.env_settings → dotenv).  pydantic guards each with a
        # try/except ImportError, so excluding them does not break the viewer —
        # but left in, PyInstaller freezes them into the PYZ (rich 91 /
        # pygments 336 / dotenv 5 modules) with NO entry in
        # THIRD_PARTY_LICENSES.txt (the notice is the pyproject dependency
        # closure, which does not include these).  That is an MIT/BSD notice-
        # retention violation in the paid dist (issue #38).  dotenv is not a
        # viewer dependency at all (a plugin-layered venv may carry it via
        # pydantic-settings), so it must never ride into the plain viewer.  check_dist_complete's reconciliation is the
        # permanent backstop should a future optional import reintroduce them.
        "rich", "pygments", "dotenv",
        # setuptools (with its vendored jaraco/more_itertools/packaging/… and
        # _distutils_hack, pkg_resources) is dragged in ONLY by PyInstaller's
        # pyi_rth_setuptools runtime hook — no viewer code imports it.  Left in
        # it freezes ~an entire MIT package tree into the plain dist unlisted in
        # THIRD_PARTY_LICENSES.txt (issue #41).  Excluding it drops the dead
        # weight; the runtime hook is a no-op without the package.
        "setuptools", "_distutils_hack", "pkg_resources",
    ],
    # Shipped-source anonymity: compile the frozen bytecode at optimization
    # level 2 so no docstring (development history) survives into the PYZ.
    # asserts go with it — none in the shipped code carries behaviour (they
    # only narrow types).  build_portable.check_dist_complete proves it on the
    # produced bytecode.
    optimize=2,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SnappixViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="snappix-viewer",
)
