"""Build the portable Snappix distributions (plain viewer + plugin packs).

This script is the **host** build driver: it builds and verifies the
plain (plugin-free) viewer, and drives each repo plugin's own **build hook**
(``plugins/<id>/build_hook.py``) for the plugin packs.  The split mirrors the
repo's public-split rule — the public repository receives a snapshot of
the main repository (``tools/public_export.py``) that carries no
``plugins/``, so:

* everything in this file works with an absent/empty ``plugins/`` folder
  (the build then produces just the plain dist), and
* every plugin-specific build step (staging, vendoring, pack verification,
  the pack's own license notice) lives inside that plugin's folder.

Outputs with the official plugins present — everything under ``dist/`` is a
release artefact, and nothing else lives there:

* **dist/snappix-viewer/** — the shipped viewer folder WITH every repo plugin
  already installed under ``plugins/<id>/``.  This is the tree a developer
  runs / smoke-tests locally; it is not itself uploaded.
* **dist/snappix-viewer-v<版数>.zip** — the plain (plugin-free) distribution,
  zipped from ``dist/snappix-viewer/`` BEFORE the plugins are installed, with
  ``snappix-viewer/`` as its single top-level folder.  No tagger, no numpy, no
  AI docs; the AI search UI never appears (viewer/ai_pack.py gates it on the
  official plugin's presence).  ``check_plain_zip`` pins the archive to the
  just-verified folder, so every plain-dist negative holds for it too.
* **dist/plugins/<plugin_id>-v<版数>.zip** — one distributable zip per repo
  plugin (every ``plugins/*/`` folder carrying a ``plugin.json``; future
  plugins are picked up automatically).  Each zip holds the drop-in
  ``plugins/<id>/`` layout so extracting it into the viewer folder installs
  the plugin.  A plugin whose hook defines ``staged_pack_root`` zips its
  verified staged pack as a FAITHFUL, unfiltered archive (the hook already
  excluded its dev-only entries when staging, so no further filtering runs —
  the zip equals the staged pack that check_dist verified, and
  check_plugin_zips reconciles the two); others zip their
  committed source with the plugin-level dev-only entries removed.  A zip
  larger than ``--max-part-mib`` is replaced by raw byte parts
  ``<id>-v<版数>.zip.001`` / ``.002`` … (GitHub caps one release asset at
  2 GiB); the unsplit file is not kept.

Every asset name carries the product version (``v`` + pyproject's
``project.version``, one source of truth — :func:`release_version`), so a
downloaded file says which release it came from without being opened, and two
releases can sit in one folder.  The plugin packs are versioned in LOCKSTEP
with the product (``check_version_sync`` enforces the manifests), so one
version tag names the whole release.
* **dist/SHA256SUMS.txt / dist/README-assets.txt** — the release manifest and
  the Japanese reader's guide (what each asset is, how to re-join the split
  parts, how to verify a hash, how to install a plugin).
* **dist/public/** — the same two files rendered from the viewer asset alone,
  for the PUBLIC repository's release (plain viewer only, no plugin named;
  the public repository's build-release workflow attaches them next to the
  viewer zip it built).

The pack STAGING folders are intermediates, not artefacts: each hook builds
its pack under ``build/packs/<id>/`` (``api.pack_dir()``), and the host
installs / zips from there.  ``dist/`` therefore holds release assets only,
so ``tools/release_assets.py upload`` can enumerate it without a denylist.

Steps:
  1. Verify the viewer's version resource matches pyproject.toml, then give
     each plugin hook its fast-fail turn (``check_build_env``).
  2. Run PyInstaller with snappix_viewer.spec → dist/snappix-viewer/.
  3. Run each plugin hook's ``build`` (stages its pack under build/packs/<id>/).
  4. Assemble THIRD_PARTY_LICENSES.txt (viewer-only dependency closure) +
     licenses/ + 利用規約・免責事項.txt + the user docs + the plugins/
     folder (with PLUGIN_DEVELOPMENT.md) + the Explorer shell-integration
     scripts (シェル統合を登録.bat / …を解除.bat) into the plain dist.
  5. Verify the plain dist (completeness, no GPL-only Qt DLL, no plugin
     payload) + each hook's plain-dist negatives (``check_plain_dist``) +
     each hook's own pack (``check_dist``).
  6. Write dist/snappix-viewer-v<版数>.zip from the just-verified plain dist
     and reconcile the ZIP against that folder (``verify_archive_matches_tree``
     — same entry set, same recorded size/CRC-32, one top folder).  The order
     matters: the
     plain zip must be taken before step 7 touches the folder, so the shipped
     plain distribution can never contain a plugin.
  7. Install every repo plugin into dist/snappix-viewer/plugins/<id>/ (from
     the verified pack) and verify the installed folder.
  8. Zip every repo plugin into dist/plugins/<id>-v<版数>.zip and verify them
     (drop-in layout + each hook's ``check_zip``) — then split the oversize
     ones into ``.zip.NNN`` parts.
  9. Write SHA256SUMS.txt + README-assets.txt (the full pair, and the
     viewer-only pair under dist/public/) and verify the final layout.

Hook protocol — a ``plugins/<id>/build_hook.py`` may define any of:

    check_build_env(api)             # fast-fail env checks, before PyInstaller
    build(api)                       # stage the pack under api.pack_dir()
    check_plain_dist(api) -> [str]   # negatives against the PLAIN dist
    check_dist(api)                  # verify the staged pack (SystemExit)
    staged_pack_root(api) -> Path    # ship this folder's CONTENTS (else the
                                     # committed source is shipped)
    check_zip(api, zip_path, names) -> [str]   # zip-content negatives

``api`` is a :class:`HookApi` — repo/dist paths (incl. ``pack_dir()``, the
hook's own staging folder under the build workpath) plus the shared helpers
(run / GPL-Qt prune + check / version-resource check /
``check_tree_path_lengths`` for the MAX_PATH budget / shipped-source
anonymity: ``strip_python_sources`` for the plugin sources a hook stages,
``unstripped_source_offenders`` + ``frozen_docstring_offenders`` for its
``check_dist``).

A hook's negatives run against the tree it STAGES, not against the zip the
host writes from it: ``verify_archive_matches_tree`` pins the two together,
so ``check_zip`` is for content rules the staged tree cannot express, not for
re-running what ``check_dist`` already proved.  Every pack-wide discipline lives there as ONE host
implementation so a new plugin gets it by calling ``api.*`` instead of
copying it (a guard that lives inside one hook silently leaves every other
pack unguarded).  Build hooks and
plugin tests are dev-only files: they are excluded from source zips here and
must be excluded from staged packs by the hooks themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
import zlib
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parent
LICENSES_DIR = ROOT / "licenses"
#: The product's own terms of use / disclaimer (utf-8), shipped next to the
#: EXE.  Kept in parity with ``src/snappix/common/terms_text.py::TERMS_TEXT``
#: by ``tests/test_terms.py`` (the module constant is the runtime source of
#: truth for the first-run consent dialog; this file is the human-readable
#: copy).
TERMS_FILE = ROOT / "利用規約・免責事項.txt"
VIEWER_SPEC = ROOT / "snappix_viewer.spec"
DIST_ROOT = ROOT / "dist"
DIST_DIR = DIST_ROOT / "snappix-viewer"
BUILD_DIR = ROOT / "build"

#: Where the per-plugin distributable zips land — a SIBLING of the viewer
#: folder.  The PLAIN distribution zip must stay free of plugin payloads;
#: ``check_dist_complete`` / ``check_plain_zip`` verify.
PLUGIN_ZIP_DIR = DIST_ROOT / "plugins"

#: Where each plugin hook stages its pack (``api.pack_dir()``).  Under the
#: BUILD workpath, not dist/: a staged pack is an intermediate, and dist/ holds
#: release assets only (tools/release_assets.py enumerates dist/ wholesale).
PACKS_DIR = BUILD_DIR / "packs"

#: Release manifest + reader's guide written next to the assets.
SUMS_NAME = "SHA256SUMS.txt"
README_ASSETS_NAME = "README-assets.txt"

#: The PUBLIC release's manifest pair (same two names, viewer-only content).
#: The public repository's release carries the plain viewer and nothing else
#: (plugins are distributed separately from the public viewer), and
#: its manifests must not even NAME a plugin pack, so the pair is rendered
#: from the viewer asset alone by the same two renderers.  A sibling folder
#: because both files keep their names (a release asset is named after its
#: file).  The public repository's build-release workflow (which runs this
#: same script on the plugin-free public tree) attaches dist/public/ next to
#: the viewer zip it built; the local build's copy is the self-check that the
#: pair never names a plugin.
PUBLIC_DIR = DIST_ROOT / "public"

#: The manifest pair for a release that leaves the plain zip OUT (same two
#: names, content drawn from everything but the viewer).  A final version's
#: plain zip is built and attached by the public repository's workflow, so the
#: local build's release attaches only the plugin packs — and the manifests it
#: attaches must describe exactly those (the full pair next to the assets
#: would list the locally built viewer zip, whose hash never matches the
#: publicly distributed one).  ``tools/release_assets.py`` attaches this pair
#: instead of the full one whenever it leaves the viewer out.
WITHOUT_VIEWER_DIR = DIST_ROOT / "without-viewer"

#: Split threshold for a release asset (MiB).  GitHub refuses a single release
#: asset over 2 GiB; 1900 MiB leaves room without making the part count silly.
DEFAULT_MAX_PART_MIB = 1900

#: Sequential I/O chunk — a multi-GB asset must never be read into memory.
CHUNK_BYTES = 8 * 1024 * 1024

#: zlib deflate level for the plain viewer zip.  Unlike the plugin packs
#: (already-incompressible CUDA/torch DLLs, see PLUGIN_ZIP_COMPRESSLEVEL) the
#: plain dist is mostly compressible Python/Qt payload and small enough that
#: zlib's default costs seconds, so the download size wins.
VIEWER_ZIP_COMPRESSLEVEL = 6

#: Dev-only entries inside a plugin folder that must never ship (neither in
#: a source zip built here nor in a staged pack — the hooks mirror this).
#: The human-authored dev-only dirs/files are bounded to the plugin's OWN top
#: level (a same-named directory deeper in shipped content — e.g.
#: ``vendor/greenlet/tests/`` — must survive), so both tuples are
#: consumed by _iter_plugin_zip_files / check_plugin_zips against the FIRST
#: path segment only.  Adding an entry here really does change what ships.
PLUGIN_DEV_ONLY_DIRS = ("tests",)
PLUGIN_DEV_ONLY_FILES = ("build_hook.py", "CLAUDE.md")
#: Build artefacts that are dropped at ANY depth of a source-zipped plugin
#: (a dev working tree accrues them under every package), unlike the
#: top-level-bounded PLUGIN_DEV_ONLY_DIRS above.
PLUGIN_ARTEFACT_DIRS = ("__pycache__",)

#: zlib deflate level for the per-plugin distributable zips.  The AI pack is
#: ~3.8 GB, ~98% of it already-hard-to-compress CUDA/torch DLLs (compress to
#: ~64% at any level).  Measured on those DLLs: level 1 runs ~2x faster than
#: zlib's default (level 6) — ~33 MB/s vs ~17 MB/s — for only ~+3% final size.
#: STORED would be fastest but bloats the DOWNLOADED artefact by ~1.4 GB, a bad
#: trade for a shipped zip.  Level 1 is the sweet spot; bump toward 6 if archive
#: size matters more than build time for a given release.
PLUGIN_ZIP_COMPRESSLEVEL = 1

#: User-facing docs shipped into the PLAIN dist/docs/ (paths relative to
#: docs/).  Plugin docs live INSIDE each plugin's folder and ship with its
#: pack — the plain distribution must not mention features it doesn't have.
SHIPPED_DOCS = [
    "formats/post-md.md",
]

#: The quick-start guide copied to the dist root.  Named in Japanese so the
#: (Japanese-first, paid) user actually notices "read me first" among the many
#: files beside the EXE — an English README.txt was easy to overlook (A05).
README_DIST_NAME = "はじめにお読みください.txt"

#: The plugin developer guide.  Host-owned public spec: the committed source
#: lives at ``docs/PLUGIN_DEVELOPMENT.md`` (NOT under plugins/ — that folder
#: is absent from the public viewer-only snapshot); the build copies it into
#: ``dist/.../plugins/`` so every shipped folder carries the guide next to
#: where users drop plugins.
PLUGIN_DOC_NAME = "PLUGIN_DEVELOPMENT.md"
PLUGIN_DOC_SRC = ROOT / "docs" / PLUGIN_DOC_NAME

#: Optional Explorer shell-integration scripts written to the dist root.
#: They call ``reg add`` / ``reg delete`` on the exact HKCU keys the in-app
#: dialog uses (via ``snappix.viewer.shell_integration``), so either path can
#: register and the other can fully unregister.
SHELL_REGISTER_BAT = "シェル統合を登録.bat"
SHELL_UNREGISTER_BAT = "シェル統合を解除.bat"

# ---------------------------------------------------------------------------
# Qt runtime DLL policy (allowlist-first).
#
# THIRD_PARTY_LICENSES.txt states the application uses Qt under the LGPL v3
# and *enumerates* the shipped Qt modules — an allowlist claim.  A denylist
# keeps missing something (Qt6Location, DLLs outside the PySide6 root, Qt3D
# wheel names, qml/plugin companion binaries) because it can never enumerate
# everything Qt ships, so the primary gate is
# the same shape as the legal claim: a ``Qt6*.dll`` may ship ONLY if its stem
# is on :data:`ALLOWED_QT_DLL_STEMS`; anything else fails the build.  The
# denylists below remain as the SECOND layer: they identify components that
# are *known* unwanted (GPL-only, or declared-unshipped) and therefore safe
# for ``prune_gpl_qt_tree`` to delete, and they cover the plugin/companion
# binaries whose names are not ``Qt6*.dll`` at all.

#: The Qt *module* runtime DLL stems the frozen apps ship — kept in lockstep
#: with the module list THIRD_PARTY_LICENSES.txt declares ("Only
#: LGPL-licensed Qt modules are used (…)"); tests/test_build_portable.py
#: parses the committed notice and fails on any drift.  This is the UNION of
#: the viewer's Qt imports (snappix_viewer.spec: QtCore/QtGui/QtWidgets/
#: QtSvg/QtPdf/QtPdfWidgets/QtMultimedia/QtMultimediaWidgets, plus QtNetwork
#: transitively — Qt6Pdf and Qt6Multimedia link Qt6Network) and the tagger's
#: (plugins/snappix_ai/snappix_tagger.spec: QtCore/QtGui/QtWidgets — a strict
#: subset), so one list serves both frozen apps via HookApi.
SHIPPED_QT_MODULE_DLL_STEMS: tuple[str, ...] = (
    "Qt6Core",
    "Qt6Gui",
    "Qt6Widgets",
    "Qt6Svg",
    "Qt6Network",
    "Qt6Multimedia",
    "Qt6MultimediaWidgets",
    "Qt6Pdf",
    "Qt6PdfWidgets",
)
#: qtbase infrastructure DLLs the shipped modules LINK (not importable Qt
#: modules of their own, so not in the notice's module list — they fall under
#: the blanket "Qt framework … under LGPL v3" statement): Qt6Concurrent is a
#: link dependency of Qt6Multimedia; Qt6DBus of Qt6Gui/Qt6Multimedia (pulled
#: on non-Windows wheel layouts of the same freeze recipe — harmless to allow
#: everywhere, both are LGPL qtbase libraries).
SHIPPED_QT_BASE_DLL_STEMS: tuple[str, ...] = (
    "Qt6Concurrent",
    "Qt6DBus",
)
#: Every ``Qt6*.dll`` stem allowed in a shipped dist.  EXACT stem match (not
#: a prefix — a prefix would silently admit e.g. Qt6PdfQuick.dll via Qt6Pdf).
ALLOWED_QT_DLL_STEMS: tuple[str, ...] = (
    SHIPPED_QT_MODULE_DLL_STEMS + SHIPPED_QT_BASE_DLL_STEMS
)
_ALLOWED_QT_DLL_STEMS_LOWER: frozenset[str] = frozenset(
    s.lower() for s in ALLOWED_QT_DLL_STEMS
)

# KNOWN-unwanted Qt DLLs (GPL-only in open-source Qt per
# https://doc.qt.io/qt-6/licensing.html, or LGPL but declared-unshipped by
# the notice, like Qt WebEngine / Qt WebView).  Under the allowlist these
# would all fail verification anyway; keeping them named serves prune:
# a component matched here is *known* safe to delete, whereas an unmatched
# non-allowlisted Qt6 DLL makes prune fast-fail instead (see
# ``prune_gpl_qt_tree``).
GPL_ONLY_QT_DLL_PREFIXES: tuple[str, ...] = (
    # Qt 3D is commercial/GPL only in open-source Qt (no LGPL option; the
    # tagger spec's excludes already treated it as a concern) — the PySide6
    # wheel ships it as Qt63DAnimation / Qt63DCore / Qt63DExtras / Qt63DInput /
    # Qt63DLogic / Qt63DQuick*.dll, none of which start with the module-name
    # prefixes below, so it needs its own numeric prefix.
    "Qt63D",
    "Qt6CanvasPainter",
    "Qt6Charts",
    "Qt6DataVisualization",
    "Qt6Graphs",
    "Qt6HttpServer",
    # Qt Location is commercial/GPLv3 only (no LGPL option; re-introduced under
    # GPLv3 in Qt 6.5, per https://doc.qt.io/qt-6/qtlocation-index.html). The
    # related Qt Positioning module keeps its LGPLv3 option, so only the
    # Qt6Location prefix is denied. This covers Qt6Location.dll in the PySide6
    # wheel.
    "Qt6Location",
    # Qt Lottie Animation is commercial/GPLv3 only (no LGPL option, per
    # https://doc.qt.io/qt-6/qtlottieanimation-index.html); the prefix covers
    # both Qt6Lottie.dll and Qt6LottieVectorImage*.dll in the PySide6 wheel.
    "Qt6Lottie",
    "Qt6NetworkAuth",
    # Qt Qml / Qt Quick / Qt OpenGL are LGPL, but declared-unshipped by the
    # notice (like Qt WebView) and present in the freeze ONLY as link
    # dependencies of the denylisted virtual-keyboard input-context plugin:
    # PyInstaller's binary-dependency walk pulls qtvirtualkeyboardplugin.dll →
    # Qt6VirtualKeyboard.dll → Qt6Qml/Qt6Quick → Qt6OpenGL + Qt6QmlMeta/
    # QmlModels/QmlWorkerScript.  A whole-tree import scan of both frozen
    # apps (2026-08-29 release-candidate build) confirmed nothing else
    # references them, so after the virtual keyboard is pruned they are
    # orphans — known safe to delete.  "Qt6Qml" subsumes Qt6QmlCompiler and
    # "Qt6Quick" subsumes Qt6Quick3D/Qt6QuickTimeline below; those stay
    # listed because their rationale differs (GPL-only, not merely
    # declared-unshipped).
    "Qt6OpenGL",
    "Qt6Qml",
    "Qt6QmlCompiler",
    "Qt6Quick",
    "Qt6Quick3D",
    "Qt6QuickTimeline",
    "Qt6VirtualKeyboard",
    "Qt6WebEngine",
    # Qt WebView is LGPL-licensed, but THIRD_PARTY_LICENSES.txt lists the
    # shipped Qt modules as an allowlist that does not include it — guarded
    # here like Qt WebEngine so the notice stays true (declared-unshipped).
    "Qt6WebView",
)
# File/dir NAME substrings (case-insensitive) that identify KNOWN-unwanted Qt
# plugin/companion components whose names are not ``Qt6*.dll`` and therefore
# sit outside the allowlist gate: the on-screen virtual-keyboard input method
# (platforminputcontexts plugin DLL, plugins/virtualkeyboard/), and the qml /
# service-plugin companion binaries of the GPL-only modules.  The PySide6
# plugins/ subtree carries many LEGITIMATE binaries (imageformats, platforms,
# multimedia, tls, …) whose set varies across PySide6 releases, so it is NOT
# allowlisted — this name denylist is its second-layer guard.  Both
# ``prune_gpl_qt_tree`` and ``_check_no_gpl_qt`` consume the ONE shared
# predicate (``_iter_qt_component_violations``), so prune (deletion) and
# check (verification) cannot drift apart.  The name match applies to
# BINARIES (.dll/.pyd/.so) and DIRECTORIES only — harmless auxiliary files a
# future PyInstaller may start collecting (e.g. metatypes JSON like
# qt6virtualkeyboard_metatypes.json) must not hard-fail the build (item 38
# follow-up).
GPL_ONLY_QT_PLUGIN_NAME_SUBSTRINGS: tuple[str, ...] = (
    "virtualkeyboard",
    # Companion binaries of the denylisted modules above.  Their qml plugin
    # DLLs and service plugins carry names that start with neither "Qt6" nor
    # the module prefix (e.g. qml/QtCharts/qtchartsqml2plugin.dll,
    # qml/QtLocation/declarative_locationplugin.dll,
    # plugins/geoservices/qtgeoservices_osm.dll, qml/Qt3D/*/quick3d*plugin.dll),
    # so the prefix denylist alone cannot see them.  PyInstaller's QtQml hook
    # collects the qml/ tree without a per-module filter, so one transitive
    # QtQml import is enough to drag these in.
    "qt3d",
    "qtcharts",
    "datavisualization",
    "qtgraphs",
    "qtlocation",
    "locationplugin",
    "geoservices",
    "lottie",
    "quick3d",
    "quicktimeline",
    "webview",
    "webengine",
)
# Qt6ShaderTools is deliberately NOT denylisted here: since Qt 6.3 the Shader
# Tools runtime library is available under LGPLv3 (or GPLv2) — only the qsb
# CLI tool, which is never bundled, is GPL-only
# (https://doc.qt.io/qt-6/qtshadertools-index.html).  It is still not on
# ALLOWED_QT_DLL_STEMS (nothing shipped needs it), so its unexpected
# appearance fast-fails the build as an UNLISTED DLL rather than being
# silently deleted as known-GPL.

#: Stdlib top-level modules deliberately left OUT of the frozen viewer.
#: The frozen viewer is the HOST for in-process plugins (the plugins/ folder
#: next to the EXE), and a plugin's vendored third-party code may import any
#: stdlib module the viewer itself never touches — PyInstaller's static
#: analysis of the viewer alone cannot see those.  snappix_viewer.spec
#: therefore bundles the COMPLETE standard library minus this denylist of
#: clearly-dead weight (GUI toolkits, demo/test trees, packaging machinery
#: no runtime library may rely on).  Kept here — PyInstaller-free — so a
#: plugin's test suite can assert that its dependency closure never needs a
#: skipped module (the frozen viewer would then lack it at runtime while the
#: dev environment happily provides it).
FROZEN_STDLIB_SKIP: frozenset[str] = frozenset({
    # ``_tkinter`` is the C extension behind ``tkinter``; it is a stdlib
    # module name but NOT a builtin (it ships as a ``.pyd``), so the
    # ``startswith("_") and in builtin_module_names`` filter below lets it
    # through.  Left unlisted it drags PyInstaller's hook-_tkinter /
    # pyi_rth__tkinter — i.e. the whole Tcl/Tk runtime (~7.4MB, unlicensed in
    # THIRD_PARTY_LICENSES.txt) — into the plain paid dist.  Skip both.
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "lib2to3",
    "test", "antigravity", "this", "ensurepip", "venv", "distutils",
})


def frozen_stdlib_module_names() -> list[str]:
    """Top-level stdlib module names the frozen viewer must bundle.

    Everything in :data:`sys.stdlib_module_names` except
    :data:`FROZEN_STDLIB_SKIP` and the private builtins already baked into
    the interpreter binary.  ``snappix_viewer.spec`` expands each name into
    hidden imports via PyInstaller's ``collect_submodules``; this helper
    stays importable without PyInstaller so tests share the exact policy
    instead of drifting from a copy.
    """
    return [
        name
        for name in sorted(sys.stdlib_module_names)
        if name not in FROZEN_STDLIB_SKIP
        and not (name.startswith("_") and name in sys.builtin_module_names)
    ]


# ---------------------------------------------------------------------------
# Frozen-package reconciliation.
#
# The hand-maintained negatives in check_dist_complete (no numpy dir, no GPL Qt
# DLL, no Tcl/Tk) only catch leaks someone already thought to name.  This
# machinery instead reconciles the COMPLETE set of top-level packages/modules
# frozen into the plain viewer against what may legitimately be there — stdlib,
# the THIRD_PARTY_LICENSES.txt dependency closure, and a small
# infra/first-party allowlist — so ANY unlicensed bundled package fails the
# build, present and future, without editing a denylist.

#: Top-level import names legitimately in the frozen plain viewer that are
#: neither stdlib nor a THIRD_PARTY_LICENSES.txt closure member: first-party
#: code and PyInstaller's own frozen-app entry script.
FROZEN_ALLOWED_EXTRA_TOPLEVELS: frozenset[str] = frozenset({
    "snappix",   # the first-party package
    "launcher",  # the frozen entry script (launcher.py)
})

#: Stdlib modules MISSING from ``sys.stdlib_module_names`` (CPython's
#: generated list has gaps).  ``_wmi`` is the Windows-only C extension behind
#: ``platform`` (new in 3.12) — the other Windows-only names (``winreg`` /
#: ``_winapi`` / ``msvcrt``…) are listed on every platform, but ``_wmi`` was
#: left out of the generated header, so a Windows build freezes ``_wmi.pyd``
#: (via ``platform``) and the reconciliation would flag genuine stdlib as an
#: unlicensed package.  It is CPython's own code, covered by the CPython
#: runtime notice like the rest of the stdlib.
FROZEN_STDLIB_NAME_GAPS: frozenset[str] = frozenset({"_wmi"})

#: PyInstaller injects its own bootstrap loader, per-package runtime hooks and
#: runtime-utility modules into the PYZ (pyiboot01_bootstrap, pyimod0*_*,
#: pyi_rth_*, _pyi_rth_utils).  They are the freezer's own machinery —
#: PyInstaller's bootloader ships under the GPL-with-exception already
#: described in the CPython runtime notice — not third-party libraries the app
#: bundles, so allow them by name prefix (both the ``pyi_*`` and the newer
#: ``_pyi_*`` spellings).  The prefixes are deliberately AS NARROW as the real
#: module names: a bare ``pyi`` prefix also matched unrelated third-party
#: distributions whose name merely starts with those three letters (``pyicu``,
#: ``pyinstrument``), silently exempting them from the unlicensed-package
#: reconciliation this whole check exists for.
FROZEN_INFRA_PREFIXES: tuple[str, ...] = (
    "pyiboot", "pyimod", "pyi_", "_pyi_", "PyInstaller",
)

#: Loose binaries (``*.dll``) that legitimately materialise DIRECTLY under
#: ``_internal`` as part of the CPython interpreter runtime or its MSVC / UCRT
#: redistributable — NOT third-party packages the app bundles.  They carry no
#: import name to reconcile (``python3.dll`` is the interpreter, not a module),
#: so the single-file scan (:func:`_internal_package_toplevels`) skips them by
#: lowercase name prefix: the interpreter (``python3*.dll``), the MSVC runtime
#: (``vcruntime*`` / ``msvcp*`` / ``concrt*``), the UCRT (``ucrtbase`` /
#: ``api-ms-win-*``), and the C libraries CPython's OWN stdlib extensions link
#: (``libffi`` = ctypes, ``libssl`` / ``libcrypto`` = _ssl/_hashlib, ``sqlite3``
#: = _sqlite3).  Tcl/Tk DLLs are deliberately absent — they must stay caught by
#: the dedicated Tcl/Tk negative in :func:`check_dist_complete`.
FROZEN_RUNTIME_BINARY_PREFIXES: tuple[str, ...] = (
    "python3",
    "vcruntime",
    "msvcp",
    "concrt",
    "ucrtbase",
    "api-ms-win-",
    "libffi",
    "libssl",
    "libcrypto",
    "sqlite3",
)


def _is_frozen_runtime_binary(name: str) -> bool:
    """True for a loose ``_internal`` binary that is CPython/MSVC runtime.

    Such a file is the interpreter's own runtime (:data:`
    FROZEN_RUNTIME_BINARY_PREFIXES`), not a third-party package the app
    bundles, so the single-file reconciliation skips it."""
    return name.lower().startswith(FROZEN_RUNTIME_BINARY_PREFIXES)


def unlicensed_frozen_toplevels(present: set[str], allowed: set[str]) -> list[str]:
    """Top-level names frozen into the viewer that nothing accounts for.

    *present* is the set of top-level package/module names actually bundled
    (from the ``_internal`` tree and the PYZ archive); *allowed* is everything
    that may legitimately be there (stdlib + license-notice closure +
    :data:`FROZEN_ALLOWED_EXTRA_TOPLEVELS`).  A name in neither — and not part
    of PyInstaller's own infra (:data:`FROZEN_INFRA_PREFIXES`) — is an
    unlicensed bundled package.  Pure function so tests need
    no real build."""
    return sorted(
        name
        for name in present
        if name not in allowed and not name.startswith(FROZEN_INFRA_PREFIXES)
    )


def _dist_top_level_import_names(dist_name: str) -> set[str]:
    """Import (top-level) names a distribution installs into site-packages.

    Name → ``Distribution`` resolution only; the derivation itself lives in
    the shared harvesting library (``gen_third_party_licenses``'s
    ``dist_top_level_import_names``) because a plugin's vendoring step needs
    the very same mapping.  Empty when the dist is absent from the build venv
    (the caller then simply lacks that mapping — a package frozen in without a
    corresponding installed dist would be flagged)."""
    import importlib.metadata as im

    try:
        dist = im.distribution(dist_name)
    except im.PackageNotFoundError:
        return set()
    return _genlic().dist_top_level_import_names(dist)


def _genlic():
    """The host's shared license-harvesting library (``tools/``)."""
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gen_third_party_licenses as genlic

    return genlic


def frozen_stdlib_toplevels() -> set[str]:
    """The complete stdlib top-level name set used by the reconciliations.

    ``sys.stdlib_module_names`` plus :data:`FROZEN_STDLIB_NAME_GAPS`.  Split
    out so a plugin hook freezing its own app builds its allow-set from the
    same base the plain viewer uses (one implementation of "what counts as
    stdlib", not a per-pack copy)."""
    return set(sys.stdlib_module_names) | set(FROZEN_STDLIB_NAME_GAPS)


def frozen_allowed_toplevels() -> set[str]:
    """Every top-level import name allowed in the frozen plain viewer.

    The complete stdlib (:data:`sys.stdlib_module_names` — canonical, incl.
    private helpers like ``_pydecimal`` — plus the names CPython's generated
    list omits, :data:`FROZEN_STDLIB_NAME_GAPS`), the plain viewer's
    THIRD_PARTY_LICENSES.txt dependency closure (dist names →
    :func:`_dist_top_level_import_names`), and the infra/first-party
    allowlist.  Computed from the build venv (where every closure member is
    installed)."""
    genlic = _genlic()
    allowed = frozen_stdlib_toplevels() | set(FROZEN_ALLOWED_EXTRA_TOPLEVELS)
    for dist_name in genlic.viewer_distribution_names():
        allowed |= _dist_top_level_import_names(dist_name)
    return allowed


def _internal_package_toplevels(internal: Path) -> set[str]:
    """Top-level packages/modules that MATERIALISE directly under ``_internal``.

    Two shapes count:

    * **directories** — PyInstaller freezes a package's pure-Python modules into
      the PYZ and leaves only its binaries / data files as a directory under
      ``_internal`` (PIL/, PySide6/, pydantic_core/, and — when they leak —
      numpy/, setuptools/).  The directory NAME is the top-level import name (a
      pure-data leak dir like ``_tcl_data`` is deliberately surfaced too — it
      has no license entry either).
    * **single files** — a top-level extension module with NO package directory
      (a bare ``foo.pyd`` / ``foo.so`` single-file dist) or a stray data DLL
      lands as a loose file whose stem is the top-level name (item 33).  These
      belong to no directory and were previously invisible to the
      reconciliation, letting an unlicensed single-file binary ship unnoticed.

    ``*.dist-info`` / ``*.data`` / ``*.egg-info`` metadata dirs are not packages
    and are skipped; loose CPython/MSVC runtime binaries
    (:func:`_is_frozen_runtime_binary` — ``python3.dll``, the MSVC/UCRT
    redistributable, the C libs the stdlib links) are the interpreter's own
    runtime, carry no import name, and are skipped too.  A pure-Python leak with
    no data files instead hides in the PYZ (:func:`_pyz_toc_toplevels`)."""
    names: set[str] = set()
    if not internal.is_dir():
        return names
    for entry in sorted(internal.iterdir()):
        if entry.is_dir():
            if entry.name.endswith((".dist-info", ".data", ".egg-info")):
                continue
            names.add(entry.name)
            continue
        # Loose single file: an importable extension module (.pyd/.so) or a
        # stray binary (.dll) with no package directory.  Its stem is the
        # top-level name to reconcile; the interpreter's own runtime DLLs are
        # skipped (they are not app-bundled packages).
        if entry.suffix.lower() in (".pyd", ".so", ".dll"):
            if _is_frozen_runtime_binary(entry.name):
                continue
            names.add(entry.name.split(".")[0])
    return names


def _pyz_toc_toplevels(toc_path: Path) -> set[str]:
    """Top-level module names in a PyInstaller ``PYZ-*.toc``.

    The toc lists every pure-Python module baked into the embedded PYZ
    archive — where a pure-Python leak (rich / pygments / python-dotenv)
    shows up, since it never materialises as an ``_internal``
    directory.

    PyInstaller 6.x writes the toc as a Python literal — either a bare list of
    ``(name, path, typecode)`` tuples, or (current layout) a 2-tuple
    ``(pyz_path, [ (name, path, typecode), … ])`` wrapping that list.  Both are
    handled; on an unparseable format a regex fallback extracts the leading
    tuple element of each entry."""
    import ast

    text = toc_path.read_text(encoding="utf-8")
    names: set[str] = set()
    try:
        data = ast.literal_eval(text)
    except (ValueError, SyntaxError):  # pragma: no cover (format drift)
        for m in re.finditer(
            r"\(\s*['\"]([A-Za-z0-9_][A-Za-z0-9_.]*)['\"]\s*,", text
        ):
            names.add(m.group(1).split(".")[0])
        return names
    if (
        isinstance(data, tuple)
        and len(data) == 2
        and isinstance(data[1], list)
    ):
        entries = data[1]          # (pyz_path, [entries]) wrapper
    elif isinstance(data, list):
        entries = data             # bare list of entries
    else:  # pragma: no cover (format drift)
        entries = []
    for entry in entries:
        modname = entry[0] if isinstance(entry, (list, tuple)) else entry
        names.add(str(modname).split(".")[0])
    return names


def _find_pyz_toc(
    spec_stem: str = VIEWER_SPEC.stem, build_dir: Path | None = None
) -> Path | None:
    """Locate a frozen app's PYZ table-of-contents in PyInstaller's workpath.

    Present after a real build (``<build_dir>/<spec>/PYZ-*.toc``).  Returns
    None when the workpath is absent — the PYZ scan is then skipped
    (best-effort, as the directive puts it: "PYZ inspection if possible"),
    e.g. in unit tests running against a synthetic dist with no real build
    behind it.

    *spec_stem* defaults to the viewer's own spec and *build_dir* to the
    repository workpath; a plugin hook passes its own frozen app's spec stem
    and ``api.build_dir`` (the same guard, one implementation — and a hook
    driven at a scratch workpath must not read the repository's leftovers)."""
    build_subdir = (BUILD_DIR if build_dir is None else build_dir) / spec_stem
    if not build_subdir.is_dir():
        return None
    tocs = sorted(build_subdir.glob("PYZ-*.toc"))
    return tocs[0] if tocs else None


def run(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print("[build] $", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env, cwd=ROOT)


def _read_pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def release_version() -> str:
    """``v<pyproject version>`` — the tag every release asset is named after.

    Derived, never stored: pyproject.toml's ``project.version`` is the single
    source of truth for the product version, and ``check_version_sync``
    already forces every other copy of it (exe version resources, the package
    constant, the plugin manifests) to agree.
    """
    return f"v{_read_pyproject_version()}"


def viewer_zip_path() -> Path:
    """``dist/snappix-viewer-v<版数>.zip`` — the plain distribution's archive.

    Written from :data:`DIST_DIR` *before* the plugins are installed into it;
    its single top-level folder stays the UNversioned ``snappix-viewer/``, so
    extracting it anywhere yields the shipped folder and an upgrade lands on
    the same path the user already knows.
    """
    return DIST_ROOT / f"{DIST_DIR.name}-{release_version()}.zip"


def plugin_zip_path(plugin_id: str) -> Path:
    """``dist/plugins/<id>-v<版数>.zip`` — one plugin's distributable archive.

    The version is the PRODUCT's: official plugins ship in lockstep with the
    viewer, so a user pairing a download with their viewer only has to compare
    the two names.
    """
    return PLUGIN_ZIP_DIR / f"{plugin_id}-{release_version()}.zip"


def _read_pyproject_name() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["name"]


#: Everything a build generates for PyInstaller out of pyproject.toml.  Lives
#: under the workpath because it is a build input, not a source file: nothing
#: here is committed, so no copy of the version can drift from the one source.
GENERATED_DIR = BUILD_DIR / "generated"

#: How a generated build input reaches a spec.  A spec is evaluated inside the
#: PyInstaller subprocess, so the environment is the only channel; both frozen
#: apps (viewer + the AI plugin's tagger) use the same one.  snappix_viewer.spec
#: imports this name; a PLUGIN's spec cannot (a plugin folder is distributed on
#: its own) and spells the VALUE out, so changing it here without changing there
#: would leave that exe silently without a version resource — PyInstaller skips
#: ``version=None`` without a word.  Each such spec has a test pinning the two
#: together (plugins/snappix_ai/tests/test_ai_build_hook.py).
VERSION_INFO_ENV = "SNAPPIX_VERSION_INFO"
#: Same channel for the generated distribution metadata (viewer only — it is
#: what ``snappix.__version__`` resolves against in the frozen app).
DIST_METADATA_ENV = "SNAPPIX_DIST_METADATA"

#: ``ProductName`` in every shipped exe's version resource (the viewer and the
#: AI plugin's tagger are one product).
PRODUCT_NAME = "Snappix Viewer"

_VERSION_INFO_TEMPLATE = """\
# UTF-8
#
# Generated by build_portable.py from pyproject.toml's project.version.
# Not a committed file: the exe version resource has no copy of its own.

VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple_text},
    prodvers={tuple_text},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'041104B0',
        [StringStruct(u'CompanyName', u''),
        StringStruct(u'FileDescription', u'{description}'),
        StringStruct(u'FileVersion', u'{version}'),
        StringStruct(u'InternalName', u'{internal_name}'),
        StringStruct(u'LegalCopyright', u''),
        StringStruct(u'OriginalFilename', u'{original_filename}'),
        StringStruct(u'ProductName', u'{product_name}'),
        StringStruct(u'ProductVersion', u'{version}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1041, 1200])])
  ]
)
"""


def version_tuple_text(version: str) -> str:
    """``"0.0.2.1"`` → ``"(0, 0, 2, 1)"`` — the filevers/prodvers 4-tuple.

    VSVersionInfo's fixed file info is four INTEGERS, while a project version
    is a string with a free shape: fewer than four segments are zero-padded
    (``"1.2"`` → ``(1, 2, 0, 0)``) and a segment carrying a PEP 440 suffix
    contributes its numeric prefix (``"0.0.3a1"`` → ``(0, 0, 3, 0)``; the
    FileVersion STRING keeps the full spelling).  A segment with no leading
    digit cannot be represented at all, so the build stops rather than
    emitting a spec PyInstaller would choke on.
    """
    numbers: list[str] = []
    for part in version.split(".")[:4]:
        match = re.match(r"\d+", part)
        if match is None:
            raise SystemExit(
                f"[build] cannot derive an exe version tuple from {version!r}: "
                f"segment {part!r} starts with no digit. Give "
                "pyproject.toml's project.version a numeric leading segment."
            )
        numbers.append(match.group(0))
    while len(numbers) < 4:
        numbers.append("0")
    return "(" + ", ".join(numbers) + ")"


def write_version_info(
    dest: Path,
    *,
    description: str,
    internal_name: str,
    original_filename: str,
    product_name: str = PRODUCT_NAME,
    version: str | None = None,
) -> Path:
    """Generate a PyInstaller exe version resource at *dest*; return *dest*.

    The shipped exe properties (FileVersion / ProductVersion strings and the
    filevers/prodvers tuples) are DERIVED from pyproject.toml's
    ``project.version`` at build time instead of being kept in a committed
    file that a bump can leave stale.  Shared with the plugin build hooks
    (via :class:`HookApi`) for their own frozen apps' version resources.
    """
    if version is None:
        version = _read_pyproject_version()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        _VERSION_INFO_TEMPLATE.format(
            tuple_text=version_tuple_text(version),
            version=version,
            description=description,
            internal_name=internal_name,
            original_filename=original_filename,
            product_name=product_name,
        ),
        encoding="utf-8",
    )
    return dest


def frozen_metadata_dir_name(version: str | None = None) -> str:
    """``snappix_viewer-<版数>.dist-info`` — the name the frozen app looks for."""
    if version is None:
        version = _read_pyproject_version()
    normalised = re.sub(r"[-_.]+", "_", _read_pyproject_name()).lower()
    return f"{normalised}-{version}.dist-info"


def write_frozen_metadata(
    dest_root: Path | None = None, version: str | None = None
) -> Path:
    """Generate the minimal ``*.dist-info`` the frozen viewer reports as its
    version; return the created directory.

    ``snappix.__version__`` is the installed distribution's metadata version —
    one number, no second copy to keep in sync.  A frozen app has no installed
    distribution, so the spec bundles this directory into ``_internal`` (which
    is the frozen ``sys.path``) and ``importlib.metadata`` finds it there.

    Written from scratch rather than copied from the build venv's dist-info:
    an editable install's metadata carries ``direct_url.json`` / ``RECORD``
    with the developer's absolute paths, which must not ship (shipped-source
    anonymity).  Name + Version are all ``importlib.metadata.version`` reads.
    """
    if version is None:
        version = _read_pyproject_version()
    root = GENERATED_DIR if dest_root is None else dest_root
    dist_info = root / frozen_metadata_dir_name(version)
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        f"Name: {_read_pyproject_name()}\n"
        f"Version: {version}\n",
        encoding="utf-8",
    )
    return dist_info


def check_version_sync(version: str | None = None) -> None:
    """Fail fast when a repo plugin's manifest version drifts from pyproject.

    The plugins developed in this repo ship in LOCKSTEP with the product: one
    release, one version, one tag.  The manifest's ``version`` is what the
    plugin-management dialog shows and what the plugin's release asset is
    named after, so a plugin left at its own numbering makes a user pair
    "AI 2.0.0" with "viewer 0.0.2" and have no way to tell whether they
    belong together.  Nothing else compares the two — the manifests are hand
    written and the host never imports them at build time.

    Plugin-NAME independent (globbed via :func:`discover_repo_plugins`), so a
    future plugin is covered the moment its folder exists; third-party
    plugins are unaffected — they are not in this repo, and the plugin host
    never compares their version to anything.

    The ONLY hand-written copy of the version left to check.  Everywhere else
    the number appears it is GENERATED from pyproject.toml at build time (the
    exe version resources, the distribution metadata ``snappix.__version__``
    resolves against, the release asset names), so there is nothing there to
    drift; a manifest cannot follow because ``plugin.json`` is the PUBLIC
    plugin format third parties author by hand.
    """
    if version is None:
        version = _read_pyproject_version()
    problems: list[str] = []
    for plugin_id in discover_repo_plugins():
        path = ROOT / "plugins" / plugin_id / "plugin.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"plugins/{plugin_id}/plugin.json: cannot read ({exc})")
            continue
        found = data.get("version") if isinstance(data, dict) else None
        if found != version:
            problems.append(
                f"plugins/{plugin_id}/plugin.json: version {found!r} does not "
                f"match pyproject.toml version ({version})"
            )
    if problems:
        raise SystemExit(
            "[build] plugin manifest version check failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n    Repo plugins ship in lockstep with the product: set each "
            'plugin.json\'s "version" to pyproject.toml\'s project.version, '
            "then re-run."
        )


def run_pyinstaller() -> None:
    # DIST_ROOT is ALWAYS wiped: every downstream verification (completeness,
    # GPL-only Qt DLL, numpy / plugin-leak negatives, each hook's check_dist)
    # re-runs against a freshly COLLECT-ed dist, so a stale artefact can never
    # survive into a shipped tree regardless of the workpath state.
    #
    # BUILD_DIR is PyInstaller's --workpath — the cached import *Analysis*
    # (shared by the viewer AND, since the AI hook reuses this same dir, the
    # tagger's torch graph). By default we wipe it too, for a guaranteed-clean
    # rebuild (unchanged behaviour). Setting SNAPPIX_BUILD_INCREMENTAL=1 keeps
    # it so PyInstaller can skip re-analysing unchanged modules (the tagger's
    # large torch closure is the expensive one) — opt-in because PyInstaller's
    # own source-change detection, not this script, then owns the correctness
    # of the reused cache. A clean rebuild remains the default and is always
    # available by leaving the variable unset.
    incremental = os.environ.get("SNAPPIX_BUILD_INCREMENTAL") == "1"
    if BUILD_DIR.exists() and not incremental:
        shutil.rmtree(BUILD_DIR)
    # The pack staging folders live under the workpath but are NOT a reusable
    # analysis cache — a stale one would be installed / zipped as if it were
    # this build's output, so wipe them even in incremental mode.
    if PACKS_DIR.exists():
        shutil.rmtree(PACKS_DIR)
    if DIST_ROOT.exists():
        shutil.rmtree(DIST_ROOT)
    # The generated build inputs go in AFTER the wipe, and reach the spec
    # through the environment — a spec is evaluated inside the PyInstaller
    # subprocess, so there is no other channel.
    version_info = write_version_info(
        GENERATED_DIR / "version_info_viewer.txt",
        description="Snappix Viewer — high-performance image library viewer",
        internal_name="SnappixViewer",
        original_filename="SnappixViewer.exe",
    )
    run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm",
            "--distpath", str(DIST_ROOT),
            "--workpath", str(BUILD_DIR),
            str(VIEWER_SPEC),
        ],
        env={
            **os.environ,
            VERSION_INFO_ENV: str(version_info),
            DIST_METADATA_ENV: str(write_frozen_metadata()),
        },
    )


def prune_gpl_qt_tree(internal: Path) -> None:
    """Remove KNOWN-unwanted Qt components PyInstaller's hooks drag in.

    PySide6's hooks collect the ``platforminputcontexts`` plugins for QtGui,
    which include the virtual-keyboard input context; its binary dependency
    then pulls ``Qt6VirtualKeyboard.dll`` (GPL-only in open-source Qt) into
    the dist even though nothing imports the module.  The plugin is an
    optional on-screen-keyboard input method a desktop app never needs, so
    both the plugin and the DLL are deleted here — along with every other
    component the shared predicate marks prunable (denylisted Qt6 DLLs, their
    qml/service-plugin companion binaries, and any qml/ tree);
    ``_check_no_gpl_qt`` then verifies nothing the predicate flags remains.
    Shared with the plugin build hooks (via :class:`HookApi`) for their own
    frozen PySide6 apps.

    DELIBERATE ASYMMETRY: a ``Qt6*.dll`` that is neither on
    :data:`ALLOWED_QT_DLL_STEMS` nor known-unwanted is NOT deleted — the
    build fast-fails here instead.  Silently deleting an unknown Qt DLL that
    a new PySide6 release made a real link dependency of a shipped module
    would produce a dist that crashes at runtime on user machines; failing
    the build forces a conscious decision (extend the allowlist + the notice,
    or denylist/exclude the module).  The check keeps full symmetry with the
    predicate: everything it flags was either deleted here or already
    stopped the build.
    """
    pyside = internal / "PySide6"
    if not pyside.is_dir():
        # Every frozen app this is called on is a PySide6 GUI — a missing
        # PySide6 dir means the PyInstaller layout changed and the prune (and
        # the later check) would silently stop covering this dist.
        raise SystemExit(
            f"[build] {pyside} not found — cannot prune GPL-only Qt "
            "DLLs (did the PyInstaller output layout change?)"
        )
    removed: list[str] = []
    unlisted: list[str] = []
    walk_errors: list[OSError] = []
    for target, prunable in _iter_qt_component_violations(pyside, walk_errors):
        rel = target.relative_to(pyside).as_posix()
        if not prunable:
            unlisted.append(rel)
            continue
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(rel + "/")
        else:
            target.unlink()
            removed.append(rel)
    if removed:
        print(f"[build] Pruned GPL-only Qt components from {internal}: "
              + ", ".join(removed))
    if walk_errors:
        raise SystemExit(
            f"[build] {internal}: could not enumerate "
            + ", ".join(
                str(getattr(exc, "filename", None) or pyside) for exc in walk_errors
            )
            + " while pruning GPL-only Qt components — a subtree the prune "
            "could not walk cannot be cleared (a MAX_PATH overflow on the "
            "BUILD machine looks exactly like this). Move the checkout to a "
            "shallower path and rebuild."
        )
    if unlisted:
        raise SystemExit(
            f"[build] {internal}: Qt runtime DLLs outside the shipped-module "
            "allowlist (build_portable.ALLOWED_QT_DLL_STEMS): "
            + ", ".join(sorted(unlisted))
            + " — a PySide6/PyInstaller change is bundling Qt DLLs this build "
            "does not account for. If a shipped module genuinely needs one, "
            "add its stem to the allowlist AND to the Qt module list in "
            "THIRD_PARTY_LICENSES.txt (via tools/gen_third_party_licenses.py); "
            "otherwise exclude/denylist the module that dragged it in. "
            "Refusing to delete it silently — it may be a real runtime "
            "dependency."
        )


def prune_gpl_qt() -> None:
    """Prune GPL-only Qt DLLs from the plain viewer dist.

    Plugin packs that freeze their own PySide6 app (the AI plugin's tagger)
    run the same prune inside their build hooks via
    ``HookApi.prune_gpl_qt_tree``.
    """
    prune_gpl_qt_tree(DIST_DIR / "_internal")


def _qt6_dll_stem(name: str) -> str | None:
    """``"Qt6Xyz"`` when *name* is a Qt runtime DLL (``Qt6*.dll``), else None."""
    if name.lower().endswith(".dll") and name.lower().startswith("qt6"):
        return name[:-4]
    return None


def _iter_qt_component_violations(
    pyside_dir: Path, errors: list[OSError] | None = None
) -> Iterator[tuple[Path, bool]]:
    """Yield ``(path, prunable)`` for every Qt component that must not ship.

    The ONE predicate both ``prune_gpl_qt_tree`` (deletion / fast-fail) and
    ``_check_no_gpl_qt`` (verification) consume, so the two can never drift
    apart (two separate lists let prune sweep a narrower set than the check
    scans, and neither learns about a new companion binary).  Flags:

    - ``prunable=True`` — KNOWN-unwanted, safe to delete:
      * ``Qt6*.dll`` whose name starts with a denylisted prefix
        (:data:`GPL_ONLY_QT_DLL_PREFIXES`), wherever it sits in the tree;
      * any directory named ``qml`` — the viewer and the tagger use no QML,
        so a collected qml/ tree is unexpected wholesale (PyInstaller's
        QtQml hook collects it without a per-module filter, dragging in the
        GPL-only modules' qml plugin binaries);
      * binaries (.dll/.pyd/.so) and DIRECTORIES whose name contains a
        denylisted substring (:data:`GPL_ONLY_QT_PLUGIN_NAME_SUBSTRINGS`,
        case-insensitive).  Non-binary auxiliary files (e.g. metatypes JSON)
        are deliberately ignored (item 38 follow-up).
    - ``prunable=False`` — any other ``Qt6*.dll`` whose stem is not on
      :data:`ALLOWED_QT_DLL_STEMS`: unknown, possibly a real runtime
      dependency, so prune refuses to delete it and fast-fails instead (see
      ``prune_gpl_qt_tree``); the check fails it too.

    Matched directories are yielded whole without descending into them.

    Enumeration failures are appended to *errors* (when given) instead of
    escaping as a raw traceback — the same reason :func:`pack_relpaths` walks
    with ``onerror``: on a build machine whose checkout sits deep, the
    directories this scan must see are exactly the ones ``iterdir`` can fail
    on, so "could not look" must be reported, never silently treated as
    "nothing there".  Callers decide what that means (prune fast-fails; the
    check records a problem).
    """
    stack = [pyside_dir]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            if errors is None:
                raise
            errors.append(exc)
            continue
        for entry in entries:
            lname = entry.name.lower()
            if entry.is_dir():
                if lname == "qml" or any(
                    s in lname for s in GPL_ONLY_QT_PLUGIN_NAME_SUBSTRINGS
                ):
                    yield entry, True
                else:
                    stack.append(entry)
                continue
            stem = _qt6_dll_stem(entry.name)
            if stem is not None:
                if entry.name.startswith(GPL_ONLY_QT_DLL_PREFIXES):
                    yield entry, True
                elif stem.lower() not in _ALLOWED_QT_DLL_STEMS_LOWER:
                    yield entry, False
                continue
            if lname.endswith((".dll", ".pyd", ".so")) and any(
                s in lname for s in GPL_ONLY_QT_PLUGIN_NAME_SUBSTRINGS
            ):
                yield entry, True


def _check_no_gpl_qt(internal: Path, problems: list[str]) -> None:
    pyside_dir = internal / "PySide6"
    if not pyside_dir.is_dir():
        # Every frozen app this is called on bundles PySide6 — its absence
        # means the PyInstaller layout changed and this check would silently
        # verify nothing.
        problems.append(
            f"{internal}: PySide6 directory missing — the GPL-only Qt DLL "
            "check cannot run (did the PyInstaller output layout change?)"
        )
        return
    # Same predicate as prune_gpl_qt_tree: whatever prune
    # would have deleted (or fast-failed on) must fail verification if it is
    # still present, so a PySide6/PyInstaller upgrade that relocates or
    # renames a component cannot slip past the check just because prune ran
    # before it moved.
    denied: set[str] = set()
    unlisted: set[str] = set()
    walk_errors: list[OSError] = []
    for p, prunable in _iter_qt_component_violations(pyside_dir, walk_errors):
        rel = p.relative_to(pyside_dir).as_posix()
        (denied if prunable else unlisted).add(rel)
    for exc in walk_errors:
        target = getattr(exc, "filename", None) or pyside_dir
        problems.append(
            f"{internal}: could not enumerate {target} while checking for "
            f"GPL-only Qt components ({exc}) — the check cannot clear a "
            "subtree it failed to walk"
        )
    if denied:
        problems.append(
            f"{internal}: GPL-only / declared-unshipped Qt components present: "
            + ", ".join(sorted(denied))
        )
    if unlisted:
        problems.append(
            f"{internal}: Qt runtime DLLs outside the shipped-module "
            "allowlist (build_portable.ALLOWED_QT_DLL_STEMS): "
            + ", ".join(sorted(unlisted))
            + " — extend the allowlist AND THIRD_PARTY_LICENSES.txt's Qt "
            "module list if a shipped module genuinely needs it, otherwise "
            "exclude/denylist whatever dragged it in"
        )


# ---------------------------------------------------------------------------
# Pack-relative path length (Windows MAX_PATH).
#
# 出荷する zip を**どのプラグインが作っても**同じ規律に掛けたいので、判定は
# ホスト側に 1 実装だけ置き、各フックは :class:`HookApi` 経由で呼ぶ
# （``prune_gpl_qt_tree`` / ``check_no_gpl_qt`` と同じ受け皿パターン）。
# 1 つのフック内に閉じた判定は、他のパックを丸ごと無検査のまま残すため。

#: パック内相対パス長の上限。
#:
#: Windows の ``MAX_PATH`` は終端 NUL 込みの 260 なので、``LongPathsEnabled=0``
#: の既定環境で扱えるフルパスは 259 文字まで。「展開先ベース + ``\`` + パック内
#: 相対パス」がこれを超えると、``Expand-Archive`` は **1 ファイルも展開せず**
#: （しかもエラー文がパス長を示さない）、``[IO.Compression.ZipFile]`` は途中で
#: 例外を吐いて壊れたパックを残す（VM 実測 2026-08-30）。130 に抑えれば展開先
#: ベースに 128 文字使える。
#:
#: 上限を**緩める**前に、なぜそのパスが必要かを疑うこと（過去の 153 文字は
#: 実行時未使用の C++ ヘッダだった）。パックごとの実測ベースライン
#: （``PACK_RELPATH_BASELINE_LEN``）は各フックが持つ — 上限は全パック共通、
#: 「今どれだけ余裕があるか」はパック固有だから。
MAX_PACK_RELPATH_LEN = 130

#: 素の配布（``dist/snappix-viewer/``）の実測ベースライン。プラグインパックの
#: ``PACK_RELPATH_BASELINE_LEN`` と同じ役割で、上限に触れる前に「伸びた」こと
#: だけを警告するための基準値。素ビューアは PySide6 のプラグイン DLL が最長
#: （``_internal/PySide6/plugins/networkinformation/…`` = 68 文字・2026-09 実測）
#: で余裕は厚いが、依存が 1 つ増えれば同じ理由で伸びる。
PLAIN_DIST_RELPATH_BASELINE_LEN = 68


def overlong_pack_paths(names) -> list[str]:
    """:data:`MAX_PACK_RELPATH_LEN` を超えるパック内相対パスを長い順に返す.

    純粋関数（文字列の集まり → 文字列のリスト）にしてあるのは、ステージング
    フォルダ（フックの ``check_dist``）と実際に出荷する zip のエントリ名
    （``check_zip``）の両方を**同じ判定**に掛けるため。
    """
    return sorted(
        (n for n in names if len(n) > MAX_PACK_RELPATH_LEN),
        key=lambda n: (-len(n), n),
    )


def overlong_path_problems(where, names) -> list[str]:
    """:func:`overlong_pack_paths` の結果をビルドを止める問題文に変換する。

    長いものから 5 件だけ出す（``torch/include/`` の退行は数千件単位で出る）。
    """
    overlong = overlong_pack_paths(names)
    problems = [
        f"{where}: pack-relative path is {len(n)} chars "
        f"(limit {MAX_PACK_RELPATH_LEN}) — extracting the pack into a deep "
        f"folder would break on MAX_PATH: {n}"
        for n in overlong[:5]
    ]
    if len(overlong) > 5:
        problems.append(
            f"{where}: … and {len(overlong) - 5} more path(s) over "
            f"{MAX_PACK_RELPATH_LEN} chars"
        )
    return problems


def report_pack_path_headroom(where, names, *, baseline_len: int) -> str:
    """最長のパック内相対パスを**毎ビルド**ログへ出し、伸びたときだけ警告する。

    :data:`MAX_PACK_RELPATH_LEN` の fast-fail だけだと、上限に触れた日に
    初めて存在を知る（そして「定数を上げる」以外の逃げ道が無い状態で気付く）。
    AI パックの実測余裕は 3 文字しかなく、``lxml`` 等がリソース名を数文字
    伸ばしただけで止まり得る。

    そこで役割を 2 段に分ける:

    * **非警告行** — 最長値と残り文字数（``127/130 (3 to spare)``）を毎ビルド
      出す。「余裕が薄い」という常時真の事実はここが伝える。
    * **警告** — *baseline_len*（呼び出し側のパックが実測してピン留めして
      いる既知の最長値）を**超えた**ときだけ。常時真の事実で警告を焚くと
      最初のビルドから毎回（check_dist と check_zip で 2 ブロック）出て
      読み飛ばされるので、捉えるのは近さではなく変化にする。

    最長パスを返す（テスト・呼び出し側の観測用。空なら ``""``）。
    """
    longest = ""
    for name in names:
        if len(name) > len(longest):
            longest = name
    if not longest:
        return ""
    n = len(longest)
    print(
        f"[build] Longest pack-relative path: {n}/{MAX_PACK_RELPATH_LEN} chars "
        f"({MAX_PACK_RELPATH_LEN - n} to spare) in {where}: {longest}"
    )
    if n > baseline_len:
        print(
            f"[build] WARNING: the longest pack-relative path GREW past the "
            f"known baseline ({baseline_len} → {n} chars), "
            f"leaving {MAX_PACK_RELPATH_LEN - n} of the "
            f"MAX_PACK_RELPATH_LEN={MAX_PACK_RELPATH_LEN} budget (#134) — a "
            "dependency renaming one resource can break the build. Look for a "
            "runtime-unreachable tree to prune BEFORE raising the limit "
            "(raising it eats the user's extraction-path budget). If the "
            "growth is intended, re-measure and update "
            "PACK_RELPATH_BASELINE_LEN."
        )
    return longest


def pack_relpaths(pack_root: Path, problems: list[str]) -> list[str]:
    """*pack_root* 配下の全ファイルのパック内相対パス（POSIX 区切り）を返す。

    ``Path.rglob`` を使わないのは、走査中の ``OSError`` を pathlib が**黙って
    握り潰す**ため。この検査が探しているのは「MAX_PATH に触れる長いパス」で、
    Windows ではまさにその長いエントリの列挙自体が ``OSError`` になり得る
    （ビルド機のリポジトリが深い場所にあるとき）— つまり **検出したい状況
    そのもので盲目になる**。``os.walk`` の ``onerror`` で列挙失敗を捕まえ、
    「検査できなかった」を問題として扱う（黙って合格させない）。
    """
    names: list[str] = []
    errors: list[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(pack_root, onerror=errors.append):
        rel_dir = Path(dirpath).relative_to(pack_root)
        for filename in filenames:
            names.append((rel_dir / filename).as_posix())
    for exc in errors:
        target = getattr(exc, "filename", None) or pack_root
        problems.append(
            f"{pack_root}: could not enumerate {target} while checking "
            f"pack-relative path lengths ({exc}) — the length check cannot "
            "clear a tree it failed to walk (a MAX_PATH overflow on the BUILD "
            "machine looks exactly like this)"
        )
    return names


def check_tree_path_lengths(
    root: Path,
    problems: list[str],
    *,
    baseline_len: int | None = None,
    prefix: str = "",
) -> list[str]:
    """Apply the whole MAX_PATH discipline to one tree, in one call.

    Every tree a buyer extracts — the plain dist, the installed ``plugins/``
    folder, each staged pack — gets the same three steps here rather than as a
    copied idiom: enumerate (``pack_relpaths`` turns a failed walk into a
    problem instead of a silent pass), fail the build over
    :data:`MAX_PACK_RELPATH_LEN`, and print the headroom line — warning only
    when *baseline_len* is supplied and the longest name grew past that pack's
    measured value.  *prefix* covers a tree walked below the name the user
    sees (the installed plugins tree is walked at ``plugins/``).

    Archives need no call of their own: :func:`verify_archive_matches_tree`
    pins a shipped zip to the tree checked here.  Returns the names so a
    caller needing them for a further rule does not walk the tree twice.
    """
    names = [prefix + n for n in pack_relpaths(root, problems)]
    problems.extend(overlong_path_problems(root, names))
    if baseline_len is not None:
        report_pack_path_headroom(root, names, baseline_len=baseline_len)
    return names


# ---------------------------------------------------------------------------
# Archive ↔ folder reconciliation.
# ---------------------------------------------------------------------------


def zip_entry_name_problems(
    names: Iterable[str], *, top_name: str | None
) -> list[str]:
    """Entry names that are not the normalised form this build writes.

    Pure over the names, so the writers assert it BEFORE an artefact exists
    and :func:`verify_archive_matches_tree` re-applies it to what an archive
    carries: ``/`` separators, relative, no ``..`` segment, and — with
    *top_name* — the single top folder a buyer sees after extracting.
    """
    problems: list[str] = []
    top = f"{top_name}/" if top_name else None
    for name in names:
        bare = name[:-1] if name.endswith("/") else name
        if "\\" in name:
            problems.append(f"{name}: zip entry names use '/' separators only")
        elif not bare or bare.startswith("/") or ".." in bare.split("/"):
            problems.append(f"{name}: zip entry names must be relative paths")
        elif top is not None and not name.startswith(top):
            problems.append(
                f"{name}: every entry must sit under {top} — the archive's "
                "single top-level folder"
            )
    return problems


def _file_fingerprint(path: Path) -> tuple[int, int]:
    """``(size, CRC-32)`` — the pair a zip already stores for every entry.

    CRC-32 rather than a cryptographic digest: the comparison is between an
    archive and the very tree it was written from moments earlier, so what it
    has to catch is an archiver fault, not a forgery.  Taking the archive's half
    from the central directory costs nothing, so verifying a multi-gigabyte
    pack never decompresses it.
    """
    crc = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_BYTES):
            crc = zlib.crc32(chunk, crc)
    return path.stat().st_size, crc


def _report_divergence(
    expected: Iterable[str],
    actual: Iterable[str],
    problems: list[str],
    *,
    missing: str,
    extra: str,
) -> None:
    """Append the first few one-sided names of a set comparison (5 each way)."""
    left, right = set(expected), set(actual)
    for name in sorted(left - right)[:5]:
        problems.append(f"{name}: {missing}")
    for name in sorted(right - left)[:5]:
        problems.append(f"{name}: {extra}")


def verify_archive_matches_tree(
    zip_path: Path, tree_root: Path, *, expected_toplevel: str | None
) -> list[str]:
    """Prove a written archive is a faithful copy of the folder it came from.

    Every shipped archive is written from a tree the folder-side verifications
    have just cleared (the plain dist; a hook's staged pack).  Re-stating each
    of those negatives against the entry names would mean the host repeating
    rules it does not own — a plugin hook's pack negatives, for one — so this
    asserts instead the ONE property they all follow from: the archive holds
    exactly that tree's files, under exactly those names, carrying exactly
    that content.  Whatever the folder was cleared of is then absent from the
    artefact by construction, which is what makes the folder checks the single
    place each rule is written.

    "Carrying that content" is read off the central directory (the size and
    CRC-32 the writer recorded per entry), which is what every negative here
    rests on: a rule cleared on the tree holds for the archive because no
    OTHER file reached it under that name.  It is deliberately not a test that
    the stored streams decompress — that would mean inflating a multi-gigabyte
    pack on every build, and a stream that does not match its recorded CRC-32
    fails loudly in the extractor rather than handing the buyer wrong bytes.

    *expected_toplevel* is the one folder every entry sits under (the plain
    zip's ``snappix-viewer/``); pass ``None`` for an archive whose entries are
    the tree's own relative names (a pack's drop-in ``plugins/<id>/`` layout).
    """
    if not zip_path.is_file():
        return [f"{zip_path}: was not written"]
    problems: list[str] = []
    top = f"{expected_toplevel}/" if expected_toplevel else ""
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
    problems.extend(
        zip_entry_name_problems(
            (i.filename for i in infos), top_name=expected_toplevel
        )[:5]
    )
    shipped = {
        i.filename[len(top):]: (i.file_size, i.CRC)
        for i in infos
        if not i.is_dir() and i.filename.startswith(top)
    }
    expected = {
        name: _file_fingerprint(tree_root.joinpath(*name.split("/")))
        for name in pack_relpaths(tree_root, problems)
    }
    _report_divergence(
        expected, shipped, problems,
        missing=f"in the verified tree {tree_root} but missing from "
                f"{zip_path.name} (tree and archive diverged)",
        extra=f"in {zip_path.name} but not in the verified tree {tree_root} "
              "(tree and archive diverged)",
    )
    differing = sorted(n for n in expected.keys() & shipped.keys()
                       if expected[n] != shipped[n])
    for name in differing[:5]:
        problems.append(
            f"{name}: the archived copy differs from the verified tree "
            f"(size/CRC {shipped[name]} vs {expected[name]})"
        )
    return problems


# ---------------------------------------------------------------------------
# Plugin build hooks.


# ---------------------------------------------------------------------------
# Shipped-source anonymity: no comments / docstrings in the distributables.
# ---------------------------------------------------------------------------
#
# The product ships bytecode (the frozen viewer / tagger PYZ) and, for the
# in-process plugins, plain .py sources.  Neither may carry the development
# history written into comments and docstrings (ticket numbers, audit labels,
# past-defect narratives): the frozen apps compile with ``optimize=2`` (their
# specs), the staged plugin sources go through :func:`strip_python_sources`,
# and the verifications below prove both held on the produced artefacts.
# String constants are the one channel that survives; the repo guard
# ``history_reference_offenders`` (tools/testing/repo_guards.py) keeps them
# free of history at the source.


def _docstring_nodes(tree):
    """Yield ``(owner, expr)`` for every module / class / function docstring."""
    import ast

    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, owners):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            yield node, body[0]


def _program_shape(tree) -> str:
    """``ast.dump`` of ``tree`` with docstrings normalised away (in place).

    A docstring that is its owner's only statement becomes ``pass`` (what the
    stripper writes); any other docstring is dropped.  Two sources describe
    the same program iff their shapes are equal.
    """
    import ast

    for owner, expr in list(_docstring_nodes(tree)):
        if len(owner.body) == 1 and not isinstance(owner, ast.Module):
            owner.body[0] = ast.Pass()
        else:
            owner.body.remove(expr)
    return ast.dump(tree, include_attributes=False)


def _line_ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def strip_python_source(text: str) -> str:
    """Return ``text`` with every comment and docstring removed.

    Layout is otherwise untouched — the line count is preserved (a stripped
    docstring leaves blank lines, or ``pass`` when it was the only statement),
    so tracebacks from the shipped file keep the repo's line numbers.  Comments
    are located by the tokenizer (a ``#`` inside a string is not a comment);
    docstrings by the AST (a bare string that is the first statement of a
    module / class / function).  Anything following a docstring on its last
    line (a ``; x = 1`` chained after the closing quotes) is kept.  The result is re-parsed and its
    program shape compared with the input's — a mismatch raises
    ``ValueError`` rather than shipping a silently altered module.
    """
    import ast
    import io
    import tokenize

    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    tree = ast.parse(text)

    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        line = lines[row - 1]
        lines[row - 1] = line[:col].rstrip() + _line_ending(line)

    for owner, expr in _docstring_nodes(tree):
        first_idx, last_idx = expr.lineno - 1, expr.end_lineno - 1
        first, last = lines[first_idx], lines[last_idx]
        head = first.encode("utf-8")[: expr.col_offset].decode("utf-8")
        tail = last.encode("utf-8")[expr.end_col_offset :].decode("utf-8")
        # Statements chained after the closing quotes (``; x = 1``) stay; the
        # separator itself goes with the docstring.
        rest = tail.rstrip("\r\n").strip()
        if rest.startswith(";"):
            rest = rest[1:].lstrip()
        sole = len(owner.body) == 1 and not isinstance(owner, ast.Module)
        replacement = "pass" if sole else ""
        if replacement and rest:
            replacement += "; "
        lines[first_idx] = (
            (head + replacement + rest).rstrip() + _line_ending(first)
        )
        for idx in range(first_idx + 1, last_idx + 1):
            lines[idx] = _line_ending(lines[idx])

    stripped = "".join(lines)
    if _program_shape(ast.parse(stripped)) != _program_shape(tree):
        raise ValueError("strip_python_source would change the program")
    return stripped


def _python_files(root: Path, skip_dirs: tuple[str, ...]) -> list[Path]:
    # Match ``skip_dirs`` against the path RELATIVE to root: matching the
    # absolute parts would silently skip the whole tree when a component of
    # the build machine's checkout path happens to be named "vendor" /
    # "runtime" / "tagger" (the strip would then rewrite nothing and its
    # negative check would pass vacuously).
    return [
        p
        for p in sorted(root.rglob("*.py"))
        if not any(
            part in skip_dirs or part == "__pycache__"
            for part in p.relative_to(root).parts
        )
    ]


def strip_python_sources(root: Path, *, skip_dirs: tuple[str, ...] = ()) -> int:
    """Strip comments + docstrings from every ``*.py`` under ``root`` in place.

    ``skip_dirs`` names directory components to leave alone (a vendored
    third-party tree, whose sources are not ours to rewrite).  Returns the
    number of files rewritten.  Meant for a STAGED copy — never point it at
    the repository.
    """
    count = 0
    for path in _python_files(root, skip_dirs):
        text = path.read_text(encoding="utf-8-sig")
        stripped = strip_python_source(text)
        if stripped != text:
            path.write_text(stripped, encoding="utf-8", newline="")
            count += 1
    return count


def unstripped_source_offenders(
    root: Path, *, skip_dirs: tuple[str, ...] = ()
) -> list[str]:
    """``*.py`` files under ``root`` still carrying a comment or a docstring.

    The negative check a pack's ``check_dist`` runs after staging: it proves
    :func:`strip_python_sources` actually ran over the shipped tree (a hook
    that forgets to call it, or stages a file after the call, fails here).
    """
    import ast
    import io
    import tokenize

    offenders: list[str] = []
    for path in _python_files(root, skip_dirs):
        text = path.read_text(encoding="utf-8-sig")
        rel = path.relative_to(root).as_posix()
        comment = next(
            (
                tok.start[0]
                for tok in tokenize.generate_tokens(io.StringIO(text).readline)
                if tok.type == tokenize.COMMENT
            ),
            None,
        )
        if comment is not None:
            offenders.append(f"{rel}:{comment}: comment survived into the pack")
            continue
        docstring = next(
            (expr.lineno for _owner, expr in _docstring_nodes(ast.parse(text))),
            None,
        )
        if docstring is not None:
            offenders.append(f"{rel}:{docstring}: docstring survived into the pack")
    return offenders


def _code_carries_any(code, needles: set[str]) -> bool:
    import types

    for const in code.co_consts:
        if isinstance(const, str) and const in needles:
            return True
        if isinstance(const, types.CodeType) and _code_carries_any(const, needles):
            return True
    return False


def find_frozen_pyz(build_dir: Path, spec_stem: str) -> Path | None:
    """PyInstaller's PYZ archive for a spec, or None if no real build ran.

    ``<workpath>/<spec>/PYZ-NN.pyz`` — the ZlibArchive holding the compiled
    code object of every pure-Python module the Analysis freezes, at the
    spec's target optimization level.  This is the artefact to inspect: with
    ``noarchive=False`` and the default collection mode, PyInstaller writes
    NO per-module ``.pyc`` anywhere (``localpycs/`` holds only the five
    bootstrap modules, always compiled at optimization level 0).  Absent when
    no real build ran (unit tests against a synthetic dist), in which case
    the bytecode checks are skipped — the same best-effort contract as
    :func:`_find_pyz_toc`.
    """
    build_subdir = build_dir / spec_stem
    if not build_subdir.is_dir():
        return None
    archives = sorted(build_subdir.glob("PYZ-*.pyz"))
    return archives[0] if archives else None


def _frozen_module_name(rel: Path, package: str) -> str:
    """Dotted module name a source path relative to a freeze root gets."""
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join([p for p in (package, *parts) if p])


def frozen_docstring_offenders(
    pyz_path: Path, source_root: Path, *, package: str = ""
) -> list[str]:
    """Modules whose docstrings survived into the frozen bytecode.

    For every ``source_root/**/*.py`` present in the PYZ at ``pyz_path``
    (matched by the dotted module name the freeze gives it — ``package`` is
    the prefix the freeze root maps to: ``"snappix"`` for ``src/snappix``,
    ``""`` for a spec whose ``pathex`` IS the source root), the docstrings the
    source declares must not appear among the code object's constants — with
    ``optimize=2`` the compiler drops them entirely.  A PYZ that matches no
    source at all is itself reported (an archive/layout drift would otherwise
    pass vacuously).
    """
    import ast

    try:
        from PyInstaller.archive.readers import ZlibArchiveReader
    except Exception as exc:  # pragma: no cover (PyInstaller API drift)
        return [f"{pyz_path}: cannot import PyInstaller's PYZ reader ({exc})"]
    try:
        reader = ZlibArchiveReader(str(pyz_path))
        toc = reader.toc
    except Exception as exc:  # pragma: no cover (archive format drift)
        return [f"{pyz_path}: cannot read the frozen PYZ ({exc})"]

    offenders: list[str] = []
    matched = 0
    for src in sorted(source_root.rglob("*.py")):
        if "__pycache__" in src.parts:
            continue
        rel = src.relative_to(source_root)
        name = _frozen_module_name(rel, package)
        if not name or name not in toc:
            continue
        matched += 1
        docstrings = {
            expr.value.value
            for _owner, expr in _docstring_nodes(
                ast.parse(src.read_text(encoding="utf-8-sig"))
            )
        }
        if not docstrings:
            continue
        try:
            code = reader.extract(name)
        except Exception as exc:  # pragma: no cover (interpreter mismatch)
            offenders.append(f"{rel.as_posix()}: cannot read frozen bytecode ({exc})")
            continue
        if _code_carries_any(code, docstrings):
            offenders.append(
                f"{rel.as_posix()}: docstring survived into the frozen bytecode "
                "(is the spec's Analysis(optimize=2) still in place?)"
            )
    if not matched:
        offenders.append(
            f"{pyz_path}: no frozen module matched a source under {source_root} "
            f"(package prefix {package!r} — PYZ layout drift?)"
        )
    return offenders


class HookApi:
    """The host-side surface a plugin build hook works against.

    Paths default to this module's constants; tests construct instances with
    throwaway paths.  Helpers are the shared host implementations (GPL-Qt
    prune/check, version-resource sync, subprocess runner).
    """

    def __init__(
        self,
        plugin_id: str,
        *,
        root: Path | None = None,
        dist_root: Path | None = None,
        plain_dist: Path | None = None,
        licenses_dir: Path | None = None,
        build_dir: Path | None = None,
        plugin_src: Path | None = None,
        packs_root: Path | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.root = root or ROOT
        self.dist_root = dist_root or DIST_ROOT
        self.plain_dist = plain_dist or DIST_DIR
        self.licenses_dir = licenses_dir or LICENSES_DIR
        self.build_dir = build_dir or BUILD_DIR
        self.plugin_src = plugin_src or (self.root / "plugins" / plugin_id)
        self.packs_root = packs_root or PACKS_DIR

    def pack_dir(self, plugin_id: str | None = None) -> Path:
        """The hook's own staging folder (``build/packs/<id>/``).

        A hook assembles its pack HERE, not under ``dist/``: the staged tree is
        an intermediate (the host installs it into the shipped viewer folder
        and zips it into ``dist/plugins/<id>-v<版数>.zip``), while ``dist/`` holds
        nothing but release assets so the upload tool can enumerate it without
        knowing any plugin's name.  The folder is created on demand and wiped
        at the start of every build (:func:`run_pyinstaller`).

        *plugin_id* defaults to this api's plugin; pass one explicitly for a
        SECOND staging folder the same hook owns (e.g. a frozen helper app
        that is later nested into the pack).
        """
        return self.packs_root / (plugin_id or self.plugin_id)

    # Shared helpers (host implementations).
    run = staticmethod(run)
    prune_gpl_qt_tree = staticmethod(prune_gpl_qt_tree)
    check_no_gpl_qt = staticmethod(_check_no_gpl_qt)
    write_version_info = staticmethod(write_version_info)
    version_info_env = VERSION_INFO_ENV
    gpl_only_qt_dll_prefixes = GPL_ONLY_QT_DLL_PREFIXES
    allowed_qt_dll_stems = ALLOWED_QT_DLL_STEMS
    # Pack-relative path length (MAX_PATH) — ONE call per tree for every
    # pack; each hook supplies its own measured PACK_RELPATH_BASELINE_LEN.
    # A hook checks its STAGED pack only: the host pins the shipped zip to
    # that pack entry-for-entry, so the budget carries over to the artefact.
    max_pack_relpath_len = MAX_PACK_RELPATH_LEN
    check_tree_path_lengths = staticmethod(check_tree_path_lengths)
    pack_relpaths = staticmethod(pack_relpaths)
    overlong_pack_paths = staticmethod(overlong_pack_paths)
    # Shipped-source anonymity — a pack strips the comments / docstrings out
    # of the plugin sources it stages and proves it in check_dist; a frozen
    # app proves its optimize=2 bytecode carries no docstring.
    strip_python_sources = staticmethod(strip_python_sources)
    unstripped_source_offenders = staticmethod(unstripped_source_offenders)
    find_frozen_pyz = staticmethod(find_frozen_pyz)
    frozen_docstring_offenders = staticmethod(frozen_docstring_offenders)
    # License-notice closure vs. what the frozen app actually carries.  The
    # plain viewer has had this cross-check since the start; a pack that
    # freezes its own app needs the same one, or a package PyInstaller's
    # hooks drag in (setuptools / certifi) ships without ever appearing in
    # the pack's notice.  Same three probes, one implementation.
    unlicensed_frozen_toplevels = staticmethod(unlicensed_frozen_toplevels)
    internal_package_toplevels = staticmethod(_internal_package_toplevels)
    pyz_toc_toplevels = staticmethod(_pyz_toc_toplevels)
    find_pyz_toc = staticmethod(_find_pyz_toc)
    frozen_stdlib_toplevels = staticmethod(frozen_stdlib_toplevels)
    dist_top_level_import_names = staticmethod(_dist_top_level_import_names)


def discover_repo_plugins() -> list[str]:
    """List the repo's shippable plugins (``plugins/*/`` with a plugin.json).

    Discovery is automatic so future plugins developed in this repo get a
    distributable zip without touching the build script.  In the public
    viewer-only snapshot the plugins/ folder is absent and this is empty.
    """
    plugins_root = ROOT / "plugins"
    if not plugins_root.is_dir():
        return []
    return sorted(
        p.name
        for p in plugins_root.iterdir()
        if p.is_dir() and (p / "plugin.json").is_file()
    )


def load_build_hooks() -> list[tuple[str, object]]:
    """Load every repo plugin's ``build_hook.py`` (if it has one).

    Returns ``(plugin_id, module)`` pairs, sorted by plugin id (deterministic
    build order — discover_repo_plugins sorts).  A plugin without a hook
    simply has no plugin-specific build steps — its committed source still
    gets a zip.
    """
    import importlib.util

    hooks: list[tuple[str, object]] = []
    for plugin_id in discover_repo_plugins():
        hook_path = ROOT / "plugins" / plugin_id / "build_hook.py"
        if not hook_path.is_file():
            continue
        module_name = f"snappix_build_hook__{plugin_id}"
        if module_name in sys.modules:
            hooks.append((plugin_id, sys.modules[module_name]))
            continue
        spec = importlib.util.spec_from_file_location(module_name, hook_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        hooks.append((plugin_id, mod))
    return hooks


def _hook_call(hook, name: str, *args, default=None):
    fn = getattr(hook, name, None)
    if fn is None:
        return default
    return fn(*args)


# ---------------------------------------------------------------------------
# Plain-dist assembly.


def write_license_notices() -> None:
    """Assemble THIRD_PARTY_LICENSES.txt into dist/ and copy the license texts.

    Regenerated against the *build* environment (the same venv PyInstaller
    froze from) so the shipped notice matches exactly the package versions
    bundled into this dist — see tools/gen_third_party_licenses.py.  The
    notice covers the plain viewer only; each plugin pack carries its own
    notice, written by its build hook.  The canonical ``licenses/`` full
    texts (GNU LGPL/GPL v3) are also copied in as stand-alone files for easy
    reference.
    """
    import importlib

    sys.path.insert(0, str(ROOT / "tools"))
    genlic = importlib.import_module("gen_third_party_licenses")
    out = DIST_DIR / "THIRD_PARTY_LICENSES.txt"
    genlic.generate(out)
    dst = DIST_DIR / "licenses"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(LICENSES_DIR, dst)
    print(f"[build] Wrote {out.name} + licenses/ into dist/")


def write_terms_document() -> None:
    """Copy the product's terms of use / disclaimer next to the EXE.

    The committed ``利用規約・免責事項.txt`` is kept in parity with
    ``src/snappix/common/terms_text.py::TERMS_TEXT`` (the runtime source the
    first-run consent dialog reads) by ``tests/test_terms.py``, so shipping
    the committed copy is equivalent to regenerating it from the constant.
    """
    dst = DIST_DIR / TERMS_FILE.name
    shutil.copy(TERMS_FILE, dst)
    print(f"[build] Wrote {dst.name} into dist/")


#: Markdown → プレーンテキストの整形規則。
#: 「はじめにお読みください.txt」はメモ帳で開かれる前提の**テキスト**なので、
#: README.md を逐語コピーすると ``### 見出し`` / ``**強調**`` / `` `コード` `` が
#: そのまま見える。変換は**ビルド時のみ**で README.md 自体は不変（公開リポジトリ
#: の README を兼ねるため）。汎用の Markdown レンダラは持ち込まない —
#: README が使う 4 記法だけを落とす最小の整形。
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_EMPHASIS_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_CODE_RE = re.compile(r"`([^`]+)`")
# 1 行に閉じた HTML コメント（README の生成マーカー・開発者向け注記）。
# 同梱テキストには載せない。
_MD_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")


def _render_link(match: "re.Match[str]") -> str:
    """``[text](url)`` → ``text (url)``; ``text`` alone when they're identical
    (README links a doc by its own path, and 「path (path)」 reads as noise)."""
    text, url = match.group(1), match.group(2)
    return text if text == url else f"{text} ({url})"


def markdown_to_plain_text(text: str) -> str:
    """Render *text* (a small Markdown subset) as readable plain text.

    Only the constructs README.md actually uses are handled: ATX headings,
    ``-``/``*`` bullets, ``**強調**``, inline code spans, inline links and
    single-line HTML comments (dropped — they carry generator markers and
    developer notes). Fenced code blocks are passed through with their
    fences dropped; anything else is left verbatim, which is the safe
    failure mode for a document that is *already* meant to be read as prose.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line)
            continue
        if _MD_HTML_COMMENT_RE.match(line):
            continue
        line = _MD_HEADING_RE.sub("", line)
        line = _MD_BULLET_RE.sub(r"\1・", line)
        line = _MD_LINK_RE.sub(_render_link, line)
        line = _MD_EMPHASIS_RE.sub(r"\1", line)
        line = _MD_CODE_RE.sub(r"\1", line)
        out.append(line)
    return "\n".join(out) + "\n"


def rewrite_shipped_doc_links(text: str) -> str:
    """Point README references at where the shipped docs actually land.

    ``write_docs`` flattens ``docs/formats/post-md.md`` to ``docs/post-md.md``
    (the plain distribution doesn't carry the repo's docs/ hierarchy), but
    README.md — which doubles as the public repo's README and so must keep
    repo-relative paths — links the source location.  Copied verbatim, the
    shipped 「はじめにお読みください.txt」 sent readers to a path that doesn't
    exist in the distribution.  Rewrite at packaging time; the committed
    README stays unchanged.
    """
    for rel in SHIPPED_DOCS:
        src_ref = f"docs/{rel}"
        dst_ref = f"docs/{Path(rel).name}"
        if src_ref == dst_ref:
            continue
        # 素の ``str.replace`` は部分一致で壊す — README に
        # ``https://github.com/…/docs/formats/post-md.md`` のような絶対 URL
        # や ``…post-md.md.bak`` が入ると、配布物でだけ URL が壊れる（しかも
        # ビルドは緑のまま）。前後を「パス片の途中ではない」に限定する。
        # ``./docs/formats/post-md.md``（明示的な相対形式）も書き換える —
        # 素の lookbehind だけだと先頭の ``.`` に弾かれて、その表記だけが
        # repo レイアウトのまま配布物に残る。``./`` は
        # 保ってパス部分だけ平坦化する。``../docs/…`` は対象外のまま（README
        # は dist 直下・docs/ はその隣なので、親参照は配布物では意味を
        # 持たない）。
        pattern = (
            r"(?<![\w/.-])(\./)?" + re.escape(src_ref) + r"(?![\w.-])"
        )
        text = re.sub(
            pattern, lambda m, _dst=dst_ref: (m.group(1) or "") + _dst, text,
        )
    return text


def write_docs() -> None:
    """Ship the user-facing docs (formats) + README."""
    docs_dst = DIST_DIR / "docs"
    docs_dst.mkdir(exist_ok=True)
    for rel in SHIPPED_DOCS:
        src = ROOT / "docs" / rel
        dst = docs_dst / Path(rel).name
        shutil.copy(src, dst)
    readme_src = ROOT / "README.md"
    if readme_src.is_file():
        (DIST_DIR / README_DIST_NAME).write_text(
            markdown_to_plain_text(
                rewrite_shipped_doc_links(
                    readme_src.read_text(encoding="utf-8")
                )
            ),
            encoding="utf-8",
        )
    print(f"[build] Wrote docs/ + {README_DIST_NAME} into dist/")


def write_plugins_dir() -> None:
    """Create dist plugins/ and ship the developer guide into it.

    The empty folder gives users the drop target for plugin folders (the
    viewer would create it lazily on first run anyway, but shipping it makes
    the extension point discoverable in the unpacked zip), and the guide is
    the self-contained reference for writing plugins (committed at
    docs/PLUGIN_DEVELOPMENT.md).
    """
    plugins_dst = DIST_DIR / "plugins"
    plugins_dst.mkdir(exist_ok=True)
    shutil.copy(PLUGIN_DOC_SRC, plugins_dst / PLUGIN_DOC_NAME)
    print(f"[build] Wrote plugins/ + {PLUGIN_DOC_NAME} into dist/")


def write_shell_integration_scripts() -> None:
    """Write シェル統合を登録.bat / …を解除.bat into the dist root.

    The script bodies come from ``snappix.viewer.shell_integration`` so they
    target byte-for-byte the same HKCU keys as the in-app dialog — the single
    source of truth for the registry layout.  Written UTF-8 **without** a BOM:
    cmd.exe doesn't strip the BOM, so it fuses with the first line's command
    token (``@echo off`` becomes unrecognised). The scripts'
    own ``chcp 65001`` (as their second line) switches the console to UTF-8
    before any Japanese text is echoed, which is enough since ``@echo off``
    and the ``rem`` lines that precede it are ASCII-only / never displayed.
    """
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from snappix.viewer import shell_integration as shell

    (DIST_DIR / SHELL_REGISTER_BAT).write_text(
        shell.render_register_script(), encoding="utf-8"
    )
    (DIST_DIR / SHELL_UNREGISTER_BAT).write_text(
        shell.render_unregister_script(), encoding="utf-8"
    )
    print(
        f"[build] Wrote {SHELL_REGISTER_BAT} / {SHELL_UNREGISTER_BAT} into dist/"
    )


# ---------------------------------------------------------------------------
# Verification.


def plugin_trace_offenders(names: Iterable[str]) -> list[str]:
    """Plugin payload found among the PLAIN distribution's entry names.

    A pure function over dist-relative POSIX paths so the SAME rule covers the
    folder (``check_dist_complete``, walking dist/snappix-viewer/) and the zip
    that actually ships (``check_plain_zip``, over the archive's entry names).
    Verifying only the folder would leave the artefact itself unchecked, and
    the zip is what a buyer downloads.

    The rule is plugin-agnostic — the host must not know any plugin's name
    (public-split rule) — so it is stated structurally: ``plugins/`` is the
    user's drop target and may carry NOTHING but the developer guide.  A
    staged plugin folder, or a plugin zip written to the wrong place, is
    therefore caught by shape rather than by a denylist of names.  Each
    plugin's own extra negatives stay in its hook's ``check_plain_dist``.
    """
    offenders: list[str] = []
    for name in sorted(set(names)):
        parts = name.split("/")
        if parts[0] != "plugins":
            continue
        if len(parts) == 2 and parts[1] == PLUGIN_DOC_NAME:
            continue
        if name.endswith(".zip"):
            offenders.append(
                f"{name}: plugin zips belong in dist/plugins/ next to the "
                "viewer folder, not inside the plain distribution"
            )
        else:
            offenders.append(
                f"{name}: the plain distribution's plugins/ may hold only "
                f"{PLUGIN_DOC_NAME} — a plugin payload must ship in its own "
                "pack zip"
            )
    return offenders


def _check_frozen_metadata_is_minimal(problems: list[str]) -> None:
    """The shipped ``*.dist-info`` must carry METADATA with Name + Version and
    nothing else — the negative half of :func:`write_frozen_metadata`.

    That generator writes the metadata from scratch precisely so the build
    venv's own dist-info never ships: an editable install carries
    ``direct_url.json`` (``file:///<the developer's absolute path>``) and
    ``RECORD``, which would put the development history into a paid download.
    Nothing else in the tree verification would notice — the license
    reconciliation skips ``*.dist-info`` by name, and ``check_plain_zip`` only
    reconciles the zip's entry names against the folder's.

    The failure this catches is not hypothetical carelessness: replacing the
    spec's hand-written ``datas`` with PyInstaller's own ``copy_metadata()``
    is the textbook way to make ``importlib.metadata`` work in a frozen app,
    and it copies the dist-info directory whole.  The other two anonymity
    mechanisms (``optimize=2`` / ``strip_python_sources``) each have their own
    negative check on the produced tree; this gives the third one its.
    """
    meta_dir = DIST_DIR / "_internal" / frozen_metadata_dir_name()
    if not meta_dir.is_dir():
        return  # absence is already reported by the required-files list
    extra = sorted(p.name for p in meta_dir.iterdir() if p.name != "METADATA")
    if extra:
        problems.append(
            f"{meta_dir}: the shipped distribution metadata must be METADATA "
            "and nothing else — an installed dist-info's siblings carry the "
            "developer's absolute paths (direct_url.json / RECORD), which "
            "must not ship.  build_portable.write_frozen_metadata generates "
            f"the minimal one; did a copy_metadata() creep in?  Extra: "
            + ", ".join(extra)
        )
    metadata = meta_dir / "METADATA"
    if not metadata.is_file():
        return  # its absence is already reported by the required-files list
    body = metadata.read_text(encoding="utf-8", errors="replace")
    for marker in ("file://", str(ROOT)):
        if marker in body:
            problems.append(
                f"{meta_dir / 'METADATA'}: contains {marker!r} — the shipped "
                "metadata must name nothing but the distribution and its "
                "version (no build-machine path)"
            )


#: Frozen-in payloads that must never appear under the plain viewer's
#: ``_internal/`` — ``(glob relative to _internal/, why it must not ship)``.
#:
#: One table rather than a hand-written block per family, because every entry
#: is the same kind of fact: the spec excludes it, and a regression in that
#: exclude would ship an unlicensed tree that THIRD_PARTY_LICENSES.txt does
#: not cover.  The mechanical closure reconciliation further down catches the
#: importable packages among them, but only when the license generator can be
#: run (it is wrapped in a try/except), and it sees packages — not the data
#: directories and runtime DLLs a Tcl/Tk regression drags in.  These probes
#: are the unconditional backstop; keep them cheap and additive.
FORBIDDEN_INTERNAL_ENTRIES: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy (it ships only inside the AI plugin pack)"),
    ("numpy.libs", "numpy's bundled shared libraries"),
    ("_tkinter.pyd", "Tcl/Tk's Python extension (_tkinter)"),
    ("_tcl_data", "Tcl's data tree"),
    ("_tk_data", "Tk's data tree"),
    ("tcl8", "Tcl/Tk's script library"),
    ("tcl*.dll", "the Tcl runtime DLL"),
    ("tk*.dll", "the Tk runtime DLL"),
)

#: The one sentence every :data:`FORBIDDEN_INTERNAL_ENTRIES` hit reports.
FORBIDDEN_INTERNAL_MESSAGE = (
    "{what} must not be frozen into the plain viewer — snappix_viewer.spec "
    "excludes it (Tcl/Tk additionally via the FROZEN_STDLIB_SKIP denylist) "
    "and THIRD_PARTY_LICENSES.txt does not cover it. Did the exclude stop "
    "working?"
)


def check_dist_complete(hooks: list[tuple[str, object]] = ()) -> None:
    """Verify the produced PLAIN dist/ tree keeps its promises.

    Positive: the viewer EXE, notices, docs, plugin folder + guide, and the
    generated distribution metadata (``snappix.__version__`` resolves against
    it — without it the About dialog and the ``app_version`` handed to plugins
    would fall back to the placeholder).
    Negative: no GPL-only Qt DLL (which would contradict
    THIRD_PARTY_LICENSES.txt), none of :data:`FORBIDDEN_INTERNAL_ENTRIES`
    (numpy, Tcl/Tk — excluded by snappix_viewer.spec and unlicensed in the
    notice), no plugin payload inside the shipped folder, no docstring in the
    viewer's frozen bytecode (the spec's ``optimize=2``), nothing but METADATA
    inside the shipped ``*.dist-info`` (an installed one leaks the developer's
    absolute paths — see :func:`_check_frozen_metadata_is_minimal`), no path
    over :data:`MAX_PACK_RELPATH_LEN`, plus each plugin hook's own negatives
    (``check_plain_dist`` — e.g. no tagger/, no plugin-only dependency frozen in).

    This is the ONE place these rules are stated: the shipped zip is verified
    by reconciling it against this very tree
    (:func:`verify_archive_matches_tree`), not by re-running the rules."""
    required = [
        DIST_DIR / "SnappixViewer.exe",
        DIST_DIR / "THIRD_PARTY_LICENSES.txt",
        DIST_DIR / TERMS_FILE.name,
        DIST_DIR / "licenses" / "LGPL-3.0.txt",
        DIST_DIR / "licenses" / "GPL-3.0.txt",
        DIST_DIR / "docs" / "post-md.md",
        DIST_DIR / README_DIST_NAME,
        DIST_DIR / SHELL_REGISTER_BAT,
        DIST_DIR / SHELL_UNREGISTER_BAT,
        DIST_DIR / "plugins" / PLUGIN_DOC_NAME,
        DIST_DIR / "_internal" / frozen_metadata_dir_name() / "METADATA",
    ]
    missing = [p for p in required if not p.is_file()]

    problems: list[str] = []
    _check_no_gpl_qt(DIST_DIR / "_internal", problems)
    _check_frozen_metadata_is_minimal(problems)
    # Qt's Japanese catalog for its own standard dialogs.  Searched
    # recursively so a PyInstaller/PySide6 layout change relocates it without
    # a silent regression to English dialogs; the spec is what puts it there.
    _src = ROOT / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
    from snappix.common.i18n import DEFAULT_LOCALE
    from snappix.common.qt_i18n import qm_file_name

    _qm_name = qm_file_name(DEFAULT_LOCALE)
    if not any((DIST_DIR / "_internal").rglob(_qm_name)):
        problems.append(
            f"_internal/: {_qm_name} is missing — Qt's own dialogs "
            "(QMessageBox / QFileDialog / QInputDialog) would ship in English "
            "on a Japanese product (snappix_viewer.spec bundles it)"
        )
    internal = DIST_DIR / "_internal"
    for pattern, what in FORBIDDEN_INTERNAL_ENTRIES:
        for leak in sorted(internal.glob(pattern)):
            problems.append(
                f"{leak}: " + FORBIDDEN_INTERNAL_MESSAGE.format(what=what)
            )
    # No plugin payload anywhere in the plain dist.  The shipped zip inherits
    # this (and every hook's own plain-dist negative) through
    # verify_archive_matches_tree, which pins the archive to this same tree.
    # MAX_PATH applies here exactly as it does to a pack: README-
    # assets.txt tells the buyer to extract somewhere shallow, and this is the
    # check that makes that promise mean something.
    dist_names = check_tree_path_lengths(
        DIST_DIR, problems, baseline_len=PLAIN_DIST_RELPATH_BASELINE_LEN
    )
    problems.extend(f"{DIST_DIR}: {p}" for p in plugin_trace_offenders(dist_names))
    # Mechanical reconciliation: every top-level package/module
    # frozen into the plain viewer must be stdlib, a THIRD_PARTY_LICENSES.txt
    # closure member, or allowed infra/first-party — otherwise it is an
    # unlicensed bundled package the hand-maintained negatives above would miss
    # (rich/pygments/dotenv in the PYZ, setuptools under _internal/, and any
    # future optional-import / hook leak).  The PYZ scan is best-effort: it runs
    # only when PyInstaller's workpath is present (always so after a real
    # build; absent in unit tests against a synthetic dist).
    try:
        allowed = frozen_allowed_toplevels()
    except Exception as exc:  # pragma: no cover (defensive)
        problems.append(
            "could not compute the license-notice closure for the frozen-"
            f"package reconciliation: {exc}"
        )
        allowed = None
    if allowed is not None:
        present = _internal_package_toplevels(internal)
        pyz_toc = _find_pyz_toc()
        if pyz_toc is not None:
            present |= _pyz_toc_toplevels(pyz_toc)
        for name in unlicensed_frozen_toplevels(present, allowed):
            problems.append(
                f"_internal/PYZ freezes top-level '{name}', which is not in "
                "the THIRD_PARTY_LICENSES.txt closure / stdlib / allowlist — "
                "an unlicensed bundled package. Exclude it in "
                "snappix_viewer.spec, or (if it must ship) add its license to "
                "the notice closure and to FROZEN_ALLOWED_EXTRA_TOPLEVELS."
            )

    # Shipped-source anonymity: the viewer's own modules must carry no
    # docstring in the frozen bytecode (snappix_viewer.spec compiles with
    # optimize=2).  Best-effort like the PYZ top-level scan — read out of the
    # PYZ archive itself, present after every real build.
    pyz = find_frozen_pyz(BUILD_DIR, VIEWER_SPEC.stem)
    if pyz is not None:
        for offender in frozen_docstring_offenders(
            pyz, ROOT / "src" / "snappix", package="snappix"
        ):
            problems.append(f"frozen viewer bytecode: {offender}")
    # Each plugin's own "no trace of me in the plain dist" negatives.
    for plugin_id, hook in hooks:
        problems.extend(
            _hook_call(hook, "check_plain_dist", HookApi(plugin_id), default=[])
        )
    if problems:
        raise SystemExit(
            "[build] dist verification failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    if missing:
        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(ROOT))
            except ValueError:
                return str(p)

        rel = [_rel(p) for p in missing]
        raise SystemExit(
            "[build] dist verification failed, missing: " + ", ".join(rel)
        )


# ---------------------------------------------------------------------------
# The plain distribution zip.


def zip_tree(
    src_dir: Path,
    out_zip: Path,
    top_name: str,
    *,
    compresslevel: int = VIEWER_ZIP_COMPRESSLEVEL,
) -> list[str]:
    """Zip *src_dir*'s contents as ``<top_name>/…`` and return the entry names.

    An EMPTY directory is written as its own entry: the plain distribution's
    ``plugins/`` is meaningful as a folder (the user's drop target), and a
    zip that silently loses it would tell the buyer the extension point does
    not exist.  Non-empty directories need no entry — their files carry the
    path.

    The names are asserted against :func:`zip_entry_name_problems` before any
    of them is written, so a mapping mistake fails the build here rather than
    leaving a malformed archive for a later check to find.
    """
    entries: list[tuple[Path, str]] = []
    for path in sorted(src_dir.rglob("*")):
        rel = path.relative_to(src_dir)
        if path.is_dir():
            if any(path.iterdir()):
                continue
            entries.append((path, "/".join((top_name, *rel.parts)) + "/"))
        else:
            entries.append((path, "/".join((top_name, *rel.parts))))
    write_zip_entries(out_zip, entries, top_name=top_name, compresslevel=compresslevel)
    return [arc for _path, arc in entries]


def write_zip_entries(
    out_zip: Path,
    entries: list[tuple[Path, str]],
    *,
    top_name: str | None,
    compresslevel: int,
) -> None:
    """Write ``(file, entry name)`` pairs, refusing a malformed name first.

    Both zip writers go through here so the normalisation rule is asserted at
    the moment of writing — the only moment at which an offending name can
    still be fixed instead of shipped.
    """
    bad = zip_entry_name_problems((arc for _p, arc in entries), top_name=top_name)
    if bad:
        raise SystemExit(
            f"[build] refusing to write {out_zip.name} with malformed entry "
            "names:\n" + "\n".join(f"  - {p}" for p in bad[:5])
        )
    with zipfile.ZipFile(
        out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel
    ) as zf:
        for path, arc in entries:
            zf.write(path, arc)


def write_plain_zip() -> None:
    """Write the plain distribution zip from the verified PLAIN dist folder.

    Called BEFORE :func:`install_plugin_packs` touches the folder — the plain
    (plugin-free) distribution is defined by *when* the archive is taken, so
    no ordering accident can put a plugin inside the plain product.
    """
    out = viewer_zip_path()
    if out.exists():
        out.unlink()
    names = zip_tree(DIST_DIR, out, DIST_DIR.name)
    size = out.stat().st_size / (1024 * 1024)
    print(f"[build] Wrote {out.name} ({len(names)} entries, {size:.1f} MiB)")


def check_plain_zip() -> None:
    """Verify the shipped plain zip against the dist folder it was taken from.

    One property, not a second copy of the rules: the archive holds exactly
    the tree ``check_dist_complete`` just cleared, under exactly those names,
    carrying exactly that content (recorded size + CRC-32 — see
    :func:`verify_archive_matches_tree` for what that does and does not
    prove).  Every folder-side negative — the host's
    (:func:`plugin_trace_offenders`, the MAX_PATH budget) and every hook's
    (no tagger/, no AI doc, …) — therefore holds for the artefact a buyer
    downloads, without the host having to know what any of them look for.
    """
    out = viewer_zip_path()
    problems = verify_archive_matches_tree(
        out, DIST_DIR, expected_toplevel=DIST_DIR.name
    )
    if problems:
        raise SystemExit(
            f"[build] {out.name} verification failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# ---------------------------------------------------------------------------
# Plugin zips.


def _iter_plugin_zip_files(base: Path, *, raw: bool) -> Iterator[tuple[Path, str]]:
    """Yield (file, arcname-relative-to-base) for a plugin zip.

    *raw* (staged packs): a FAITHFUL, unfiltered archive of the staging tree.
    The build hook already excluded its dev-only entries when it staged the
    pack, so re-filtering here would drop legitimately-shipped files whose path
    merely contains a ``tests`` segment or a ``.pyc`` suffix deep inside a
    frozen/vendored subtree (``tagger/_internal/torch/fx/passes/tests/``,
    ``vendor/greenlet/tests/``) — a staged-vs-shipped divergence.
    The zip then equals the verified staged pack byte-for-name (check_plugin_
    zips reconciles the two).

    not *raw* (a committed-source zip, for a future hookless plugin): apply the
    dev-only exclusion, but bound the human-authored dev-only entries (a
    ``tests`` dir, ``build_hook.py`` and the plugin's dev notes) to the plugin's OWN
    top level, so a same-named directory deeper in shipped content is not
    dropped.  ``__pycache__`` / ``*.pyc`` are categorically build artefacts and
    are dropped at any depth (a dev working tree accrues them under any
    package)."""
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if not raw:
            if any(p in PLUGIN_ARTEFACT_DIRS for p in rel.parts):
                continue
            if path.suffix == ".pyc":
                continue
            if rel.parts and rel.parts[0] in PLUGIN_DEV_ONLY_DIRS:
                continue
            if len(rel.parts) == 1 and rel.name in PLUGIN_DEV_ONLY_FILES:
                continue
        yield path, "/".join(rel.parts)


def plugin_shipped_files(
    plugin_id: str, hook: object | None
) -> tuple[Path | None, list[tuple[Path, str]]]:
    """``(staged_pack_root or None, [(file, shipped name), …])`` for a plugin.

    The ONE answer to "what does this plugin ship, under what names" — used by
    the zip writer, the zip reconciliation and the installer, so the folder
    installed into dist/snappix-viewer/ and the zip a user extracts there can
    never describe different trees.

    A hook with ``staged_pack_root`` ships that folder's contents RAW (the
    hook already produced the exact shipping tree, so no further filtering
    runs); anything else ships its committed source under
    ``plugins/<id>/`` with the plugin-level dev-only entries removed.
    """
    staged = (
        _hook_call(hook, "staged_pack_root", HookApi(plugin_id))
        if hook is not None
        else None
    )
    if staged is not None:
        base, prefix, raw = Path(staged), "", True
    else:
        base, prefix, raw = (
            ROOT / "plugins" / plugin_id, f"plugins/{plugin_id}/", False
        )
    entries = [
        (path, prefix + rel) for path, rel in _iter_plugin_zip_files(base, raw=raw)
    ]
    return (Path(staged) if staged is not None else None), entries


def install_plugin_packs(hooks: list[tuple[str, object]] = ()) -> None:
    """Install every repo plugin into dist/snappix-viewer/plugins/<id>/.

    The shipped viewer folder is the fully-loaded product (what a developer
    runs and what the exe smoke test drives); the plugin-free product is the
    zip already written by :func:`write_plain_zip`.  Installing copies exactly
    the files the plugin's own zip carries under ``plugins/<id>/`` — i.e. it
    reproduces what a user gets by extracting that zip into the viewer folder,
    from the same source of truth (:func:`plugin_shipped_files`).

    Pack-root files OUTSIDE ``plugins/`` (a hook's "how to install" guide) are
    deliberately skipped: in an already-installed folder they would be stale
    instructions, and they still ship inside the plugin's own zip.
    """
    hook_by_id = dict(hooks)
    for plugin_id in discover_repo_plugins():
        _staged, entries = plugin_shipped_files(plugin_id, hook_by_id.get(plugin_id))
        dest_root = DIST_DIR / "plugins" / plugin_id
        if dest_root.exists():
            shutil.rmtree(dest_root)
        prefix = f"plugins/{plugin_id}/"
        count = 0
        for src, arc in entries:
            if not arc.startswith(prefix):
                continue
            dst = DIST_DIR.joinpath(*arc.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
        print(
            f"[build] Installed plugin '{plugin_id}' into "
            f"{DIST_DIR.name}/plugins/ ({count} files)"
        )


def plugin_dropin_problems(
    plugin_id: str, names: Iterable[str], where: object
) -> list[str]:
    """The drop-in layout a shipped plugin must have, judged from its names.

    One rule for the two places a plugin's files land — the folder installed
    into the shipped viewer and the zip a user extracts there — so the two can
    never describe different layouts.  Pure over ``plugins/<id>/…`` POSIX
    names: ``plugin.json`` at the plugin's OWN top level (that is what makes
    it a drop-in), and no dev-only entry at that level — bounded to that
    level, because a shipped subtree may legitimately carry a deep ``tests/``
    of its own.
    """
    base = f"plugins/{plugin_id}"
    names = set(names)
    problems: list[str] = []
    if f"{base}/plugin.json" not in names:
        problems.append(
            f"{where}: {base}/plugin.json missing — not a drop-in plugin"
        )
    for dev_only in PLUGIN_DEV_ONLY_FILES:
        if f"{base}/{dev_only}" in names:
            problems.append(
                f"{where}: {base}/{dev_only} is dev-only and must not ship"
            )
    for dev_dir in PLUGIN_DEV_ONLY_DIRS:
        if any(n.startswith(f"{base}/{dev_dir}/") for n in names):
            problems.append(
                f"{where}: {base}/{dev_dir}/ is dev-only and must not ship"
            )
    return problems


def check_installed_plugins(hooks: list[tuple[str, object]] = ()) -> None:
    """Verify dist/snappix-viewer/plugins/ after the packs were installed.

    Generic (no plugin name appears here): every discovered plugin has the
    drop-in layout (:func:`plugin_dropin_problems` — the same rule
    ``check_plugin_zips`` applies to the zip, so the two cannot drift), and no
    dist-relative path exceeds :data:`MAX_PACK_RELPATH_LEN` — the installed
    depth is the same ``plugins/<id>/…`` a user extracts, so the MAX_PATH
    budget applies to the installed folder exactly as it does to the
    pack zip.

    On top of that, the installed folder's file set reconciles EXACTLY with
    :func:`plugin_shipped_files` — the same source of truth the plugin's own
    zip is written from and reconciled against (``check_plugin_zips``).  That
    is what *hooks* is for: with both sides pinned to one answer, the two
    trees are identical by construction, so every hook's own ``check_zip``
    negative (no tagger ``.py`` sources, vendored deps present, no vendored
    Qt, …) transitively holds for the installed folder without the host
    having to know what any of them look for — the same argument
    :func:`check_plain_zip` makes for the plain zip.
    """
    hook_by_id = dict(hooks)
    problems: list[str] = []
    for plugin_id in discover_repo_plugins():
        base = DIST_DIR / "plugins" / plugin_id
        installed = {
            p.relative_to(DIST_DIR).as_posix() for p in base.rglob("*") if p.is_file()
        }
        layout = plugin_dropin_problems(plugin_id, installed, DIST_DIR)
        problems.extend(layout)
        if f"plugins/{plugin_id}/plugin.json" not in installed:
            continue  # not installed at all — the divergence list adds nothing
        prefix = f"plugins/{plugin_id}/"
        _staged, entries = plugin_shipped_files(plugin_id, hook_by_id.get(plugin_id))
        expected = {arc for _path, arc in entries if arc.startswith(prefix)}
        _report_divergence(
            expected, installed, problems,
            missing="shipped by the plugin but missing from the installed folder",
            extra="in the installed folder but not among the files the plugin "
                  "ships (the installed folder and its zip diverged)",
        )
    installed_root = DIST_DIR / "plugins"
    if installed_root.is_dir():
        check_tree_path_lengths(installed_root, problems, prefix="plugins/")
    if problems:
        raise SystemExit(
            "[build] installed-plugin verification failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


def write_plugin_zips(hooks: list[tuple[str, object]] = ()) -> None:
    """Zip every repo plugin into dist/plugins/<id>-v<版数>.zip (drop-in).

    Each zip contains a ``plugins/<id>/`` tree, so extracting it into the
    viewer folder (next to SnappixViewer.exe) installs the plugin — the same
    install story as the official packs.  Runs AFTER the dist verifications
    so a staged-pack zip captures the verified pack (zipping earlier could
    freeze a broken tree into the artefact).
    """
    hook_by_id = dict(hooks)
    PLUGIN_ZIP_DIR.mkdir(parents=True, exist_ok=True)
    for plugin_id in discover_repo_plugins():
        _staged, entries = plugin_shipped_files(plugin_id, hook_by_id.get(plugin_id))
        out = plugin_zip_path(plugin_id)
        if out.exists():
            out.unlink()
        write_zip_entries(
            out, entries, top_name=None, compresslevel=PLUGIN_ZIP_COMPRESSLEVEL
        )
        print(f"[build] Zipped plugin '{plugin_id}' → {out}")


def check_plugin_zips(hooks: list[tuple[str, object]] = ()) -> None:
    """Verify dist/plugins/ carries a well-formed zip for every repo plugin.

    Generic: one zip per discovered plugin, each with the drop-in layout
    (:func:`plugin_dropin_problems` — the same rule the installed folder is
    judged by), and no zip inside the plain dist's plugins/ (check_dist_
    complete runs BEFORE write_plugin_zips, so a zip written to the wrong
    place would slip past that earlier check).  A staged pack's zip is
    additionally pinned to the pack itself; plugin-specific content checks
    come from each hook's ``check_zip``."""
    hook_by_id = dict(hooks)
    missing: list[Path] = []
    problems: list[str] = []
    for zip_leak in sorted((DIST_DIR / "plugins").glob("*.zip")):
        problems.append(
            f"{zip_leak}: plugin zips belong in dist/plugins/ next to the "
            "viewer folder, not inside it"
        )
    for plugin_id in discover_repo_plugins():
        out = plugin_zip_path(plugin_id)
        if not out.is_file():
            missing.append(out)
            continue
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        problems.extend(plugin_dropin_problems(plugin_id, names, out))
        hook = hook_by_id.get(plugin_id)
        # For a staged pack the archive must be a FAITHFUL copy, so the
        # zip is pinned to the pack the hook's check_dist verified — same
        # names, same recorded size/CRC-32.  Every pack-side negative (the
        # MAX_PATH budget and the hook's own) then holds for the artefact, so
        # neither the host nor the hook re-states them against entry names.
        staged, _entries = plugin_shipped_files(plugin_id, hook)
        if staged is not None:
            problems.extend(
                verify_archive_matches_tree(out, staged, expected_toplevel=None)
            )
        if hook is not None:
            problems.extend(
                _hook_call(
                    hook, "check_zip", HookApi(plugin_id), out, names,
                    default=[],
                )
            )
    if problems:
        raise SystemExit(
            "[build] plugin zip verification failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    if missing:
        raise SystemExit(
            "[build] plugin zip verification failed, missing: "
            + ", ".join(str(p) for p in missing)
        )


# ---------------------------------------------------------------------------
# Release assets: 2 GiB splitting, SHA256SUMS.txt, README-assets.txt.


def sha256_file(path: Path) -> str:
    """SHA-256 of *path*, read sequentially (a pack can be several GB)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def part_name(name: str, index: int) -> str:
    """Split-part name (``foo.zip`` part 1 → ``foo.zip.001``)."""
    return f"{name}.{index:03d}"


def split_file(path: Path, max_bytes: int) -> list[Path]:
    """Replace *path* with raw ``.001`` / ``.002`` … parts of *max_bytes*.

    Raw byte splitting (not a multi-volume zip) so the parts re-join with
    nothing but ``copy /b`` on a stock Windows — 7-Zip additionally opens the
    ``.001`` directly.  Read and written sequentially, so a multi-GB pack
    never lands in memory, and the unsplit file is REMOVED: leaving it would
    double the folder's size and let an uploader attach an asset GitHub
    refuses.
    """
    parts: list[Path] = []
    with path.open("rb") as reader:
        index = 1
        while True:
            part = path.with_name(part_name(path.name, index))
            written = 0
            with part.open("wb") as writer:
                while written < max_bytes:
                    chunk = reader.read(min(CHUNK_BYTES, max_bytes - written))
                    if not chunk:
                        break
                    writer.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink()
                break
            parts.append(part)
            if written < max_bytes:
                break
            index += 1
    path.unlink()
    return parts


def join_parts(parts: list[Path], out_path: Path) -> None:
    """Concatenate ``.001`` / ``.002`` … back into one file (``copy /b``)."""
    with out_path.open("wb") as writer:
        for part in parts:
            with part.open("rb") as reader:
                for chunk in iter(lambda: reader.read(CHUNK_BYTES), b""):
                    writer.write(chunk)


class ReleaseAsset:
    """One uploadable asset: its files, and the whole-file hash behind them.

    ``parts`` is what actually gets uploaded (one file, or the split parts).
    ``sha256`` / ``size`` always describe the WHOLE artefact — for a split
    asset that is the file you get back after re-joining, which is exactly the
    thing a user can verify (they never hold the unsplit file otherwise).
    """

    def __init__(
        self, name: str, sha256: str, size: int, parts: list[tuple[str, str]]
    ) -> None:
        self.name = name
        self.sha256 = sha256
        self.size = size
        #: ``[(file name, its own SHA-256), …]`` in upload order.
        self.parts = parts

    @property
    def is_split(self) -> bool:
        return [n for n, _h in self.parts] != [self.name]


def split_oversize_zips(max_bytes: int) -> list[ReleaseAsset]:
    """Split every dist asset over *max_bytes* and describe the results.

    Runs AFTER ``check_plugin_zips`` — every content verification works on the
    complete zip, so a split can never hide a malformed archive from the
    checks.
    """
    assets: list[ReleaseAsset] = []
    candidates: list[Path] = []
    viewer_zip = viewer_zip_path()
    if viewer_zip.is_file():
        candidates.append(viewer_zip)
    candidates += sorted(p for p in PLUGIN_ZIP_DIR.glob("*.zip") if p.is_file())
    for path in candidates:
        size = path.stat().st_size
        digest = sha256_file(path)
        if size <= max_bytes:
            assets.append(ReleaseAsset(path.name, digest, size, [(path.name, digest)]))
            continue
        parts = split_file(path, max_bytes)
        print(
            f"[build] Split {path.name} ({size / (1024 * 1024):.0f} MiB) into "
            f"{len(parts)} parts of at most {max_bytes // (1024 * 1024)} MiB"
        )
        assets.append(
            ReleaseAsset(
                path.name,
                digest,
                size,
                [(p.name, sha256_file(p)) for p in parts],
            )
        )
    return assets


def render_sums(assets: list[ReleaseAsset]) -> str:
    """``SHA256SUMS.txt`` — ``<hash>  <name>`` for every downloadable file.

    A split asset contributes one line per PART (what the user downloads) plus
    one line for the re-joined whole (what they end up with); the guide says
    which is which.  Names are bare file names because that is what a release
    download lands as.
    """
    lines: list[str] = []
    for asset in assets:
        for name, digest in asset.parts:
            lines.append(f"{digest}  {name}")
        if asset.is_split:
            lines.append(f"{asset.sha256}  {asset.name}")
    return "\n".join(lines) + "\n"


def _format_size(size: int) -> str:
    mib = size / (1024 * 1024)
    return f"{mib / 1024:.2f} GiB" if mib >= 1024 else f"{mib:.1f} MiB"


def render_assets_readme(assets: list[ReleaseAsset]) -> str:
    """The Japanese reader's guide shipped next to the release assets."""
    viewer_name = viewer_zip_path().name
    viewer = [a for a in assets if a.name == viewer_name]
    plugins = [a for a in assets if a.name != viewer_name]
    split = [a for a in assets if a.is_split]
    lines: list[str] = []
    lines.append("Snappix Viewer 配布ファイルの案内")
    lines.append("=" * 40)
    lines.append("")
    lines.append("■ 同梱物")
    lines.append("")
    for asset in viewer:
        lines.append(f"  {asset.name}  ({_format_size(asset.size)})")
        lines.append("      Snappix Viewer 本体です。まずこれをダウンロードして")
        lines.append("      ください。展開すると snappix-viewer フォルダができます。")
    for asset in plugins:
        lines.append(f"  {asset.name}  ({_format_size(asset.size)})")
        lines.append("      プラグイン。必要なものだけダウンロードしてください。")
    if not viewer and not plugins:
        lines.append("  (アセットがありません)")
    if not viewer:
        lines.append(f"  ※ 本体（{viewer_name}）はこの配布に含まれません。")
        lines.append("     同じ版数の本体を別途入手してください。")
    lines.append(f"  {SUMS_NAME}  (各ファイルの SHA-256)")
    lines.append(f"  {README_ASSETS_NAME}  (このファイル)")
    lines.append("")
    for asset in split:
        lines.append(
            f"  ※ {asset.name} は {len(asset.parts)} 個に分割してあります"
            f"（{asset.parts[0][0]} … {asset.parts[-1][0]}）。"
        )
    if split:
        lines.append("     下の「分割ファイルの結合」を参照してください。")
        lines.append("")
    lines.append("■ 本体の導入")
    lines.append("")
    lines.append(f"  {viewer_name} を好きな場所へ展開し、中の")
    lines.append("  SnappixViewer.exe を実行します（インストール不要・設定と")
    lines.append("  キャッシュはそのフォルダの中に保存されます）。")
    lines.append("  Windows のパス長制限があるため、なるべく浅い場所")
    lines.append("  （例: C:\\Snappix\\ や D:\\Tools\\Snappix\\）へ置いてください。")
    lines.append("")
    lines.append("■ プラグインの導入")
    lines.append("")
    lines.append("  プラグインの zip は、展開すると plugins\\<プラグイン名>\\ という")
    lines.append("  フォルダになります。SnappixViewer.exe があるフォルダの中で")
    lines.append("  展開すれば、そのまま所定の位置（plugins\\ の中）へ入ります。")
    lines.append("  次回起動時に確認ダイアログが出るので有効化してください。")
    lines.append("  外すときはそのフォルダを削除するだけです。")
    lines.append("")
    lines.append("■ 分割ファイルの結合")
    lines.append("")
    lines.append("  1 ファイルあたりの上限があるため、大きいものは")
    lines.append("  .001 / .002 … に分けてあります。全部を同じフォルダへ")
    lines.append("  ダウンロードしてから、どちらかの方法で 1 本に戻します。")
    lines.append("")
    lines.append("  (a) 7-Zip を使う")
    lines.append("      .001 のファイルを右クリック →「7-Zip」→「展開」。")
    lines.append("      .002 以降は自動で読まれます（結合の操作は不要です）。")
    example = f"例: {split[0].name}" if split else "例: snappix-viewer-vX.Y.Z.zip"
    lines.append(f"      ここで出てくるのは分割前の zip（{example}）そのもので、")
    lines.append("      中身ではありません。")
    lines.append("")
    lines.append("  (b) Windows 標準のコマンドプロンプトで結合する")
    lines.append("      ダウンロードしたフォルダで:")
    lines.append("")
    if split:
        for asset in split:
            joined = " + ".join(n for n, _h in asset.parts)
            lines.append(f"        copy /b {joined} {asset.name}")
    else:
        lines.append("        (今回の配布に分割されたファイルはありません)")
    lines.append("")
    lines.append("  どちらの方法でも、最後にできあがった zip をもう一度展開して")
    lines.append("  ください（本体なら好きな場所へ、プラグインなら")
    lines.append("  SnappixViewer.exe があるフォルダの中で）。")
    lines.append("")
    lines.append("■ SHA-256 の照合（任意）")
    lines.append("")
    lines.append(f"  {SUMS_NAME} に「ハッシュ 半角空白 2 個 ファイル名」の形で")
    lines.append("  並んでいます。コマンドプロンプトで:")
    lines.append("")
    lines.append("        certutil -hashfile <ファイル名> SHA256")
    lines.append("")
    lines.append("  を実行し、表示された値と見比べてください。")
    lines.append("  分割したものは、パートごとの行に加えて『結合後の zip 全体』")
    lines.append("  の行も入っています（その名前のファイルはダウンロードには")
    lines.append("  ありません。結合したあとの zip を照合するための行です）。")
    lines.append("")
    return "\n".join(lines)


def public_release_assets(assets: list[ReleaseAsset]) -> list[ReleaseAsset]:
    """The subset of *assets* the PUBLIC release carries: the viewer only."""
    viewer_name = viewer_zip_path().name
    return [a for a in assets if a.name == viewer_name]


def without_viewer_assets(assets: list[ReleaseAsset]) -> list[ReleaseAsset]:
    """The subset of *assets* a release without the plain zip carries."""
    viewer_name = viewer_zip_path().name
    return [a for a in assets if a.name != viewer_name]


def write_release_manifest(assets: list[ReleaseAsset]) -> None:
    """Write SHA256SUMS.txt + README-assets.txt next to the assets, the
    viewer-only pair for the public release into :data:`PUBLIC_DIR`, and the
    pair without the viewer into :data:`WITHOUT_VIEWER_DIR`.

    Every pair comes from the same *assets* list and the same two renderers in
    the same step, so no pair can describe a different build than the full one.
    """
    _write_manifest_pair(DIST_ROOT, assets)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    _write_manifest_pair(PUBLIC_DIR, public_release_assets(assets))
    WITHOUT_VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    _write_manifest_pair(WITHOUT_VIEWER_DIR, without_viewer_assets(assets))
    print(
        f"[build] Wrote {SUMS_NAME} + {README_ASSETS_NAME} into dist/, "
        f"dist/{PUBLIC_DIR.name}/ (viewer only) and "
        f"dist/{WITHOUT_VIEWER_DIR.name}/ (everything but the viewer)"
    )


def _write_manifest_pair(folder: Path, assets: list[ReleaseAsset]) -> None:
    (folder / SUMS_NAME).write_text(render_sums(assets), encoding="utf-8")
    (folder / README_ASSETS_NAME).write_text(
        render_assets_readme(assets), encoding="utf-8", newline="\r\n"
    )


def _sums_names(sums: Path) -> set[str]:
    """File names listed in a ``SHA256SUMS.txt`` (``<hash>  <name>`` lines)."""
    return {
        line.split("  ", 1)[1]
        for line in sums.read_text(encoding="utf-8").splitlines()
        if "  " in line
    }


_PART_SUFFIX_RE = re.compile(r"^(?P<stem>.+\.zip)\.(?P<index>\d{3})$")


def split_sequence_offenders(names: Iterable[str]) -> list[str]:
    """Broken ``.zip.NNN`` sequences among *names* (bare file names).

    A missing middle part or a part sitting next to its own unsplit zip makes
    the asset unrecoverable for the user, and both are silent — nothing about
    downloading ``.001`` and ``.003`` says the result will be corrupt.
    """
    groups: dict[str, list[int]] = {}
    whole: set[str] = set()
    for name in sorted(set(names)):
        match = _PART_SUFFIX_RE.match(name)
        if match is None:
            if name.endswith(".zip"):
                whole.add(name)
            continue
        groups.setdefault(match["stem"], []).append(int(match["index"]))
    offenders: list[str] = []
    for stem, indexes in sorted(groups.items()):
        indexes.sort()
        if stem in whole:
            offenders.append(
                f"{stem}: both the unsplit zip and its parts are present — "
                "the split must replace the file it came from"
            )
        if indexes != list(range(1, len(indexes) + 1)):
            offenders.append(
                f"{stem}: split parts are not a 1..N run "
                f"({', '.join(f'{i:03d}' for i in indexes)})"
            )
    return offenders


def manifest_pair_offenders(
    folder: Path,
    files: set[str],
    rejoined: set[str],
    forbidden: Iterable[str] = (),
) -> list[str]:
    """Negatives for a sibling manifest pair (:data:`PUBLIC_DIR` /
    :data:`WITHOUT_VIEWER_DIR`).

    The folder must hold exactly the two manifests, its hash list must name
    exactly *files* (plus *rejoined* — the whole of each split asset), and
    neither file may name anything in *forbidden*.
    """
    problems: list[str] = []
    if not folder.is_dir():
        return [f"{folder}: missing from the release layout"]
    expected = {SUMS_NAME, README_ASSETS_NAME}
    present = {p.name for p in folder.iterdir()}
    for name in sorted(expected - present):
        problems.append(f"{folder / name}: missing from the release layout")
    for name in sorted(present - expected):
        problems.append(
            f"{folder / name}: unexpected entry — only the two manifests "
            f"belong in dist/{folder.name}/"
        )
    sums = folder / SUMS_NAME
    if sums.is_file():
        listed = _sums_names(sums)
        for name in sorted(files - listed):
            problems.append(f"{folder.name}/{SUMS_NAME}: no hash listed for {name}")
        for name in sorted(listed - files - rejoined):
            problems.append(
                f"{folder.name}/{SUMS_NAME}: lists {name}, which is not an "
                "asset of this release"
            )
    banned = list(forbidden)
    for manifest in sorted(present & expected):
        text = (folder / manifest).read_text(encoding="utf-8")
        for name in banned:
            if name in text:
                problems.append(f"{folder.name}/{manifest}: names the asset {name}")
    return problems


def _rejoined_names(files: Iterable[str]) -> set[str]:
    """The re-joined whole of every split asset among *files*."""
    return {m["stem"] for n in files if (m := _PART_SUFFIX_RE.match(n)) is not None}


def check_release_layout() -> None:
    """Verify dist/ holds the release layout and nothing else.

    ``dist/`` is what ``tools/release_assets.py upload`` enumerates, so a
    stray file there would be attached to the GitHub release.  Expected:
    the viewer folder, the plain zip, plugins/ (one zip or one complete part
    run per plugin), the two manifest files, public/ with the viewer-only
    manifest pair for the public repository's release, and without-viewer/
    with the pair for a release that leaves the plain zip out.
    """
    problems: list[str] = []

    def _present(stem: str, names: list[str]) -> bool:
        """The asset *stem* is there — either whole, or as its split parts."""
        return stem in names or any(n.startswith(f"{stem}.") for n in names)

    top_files = [p.name for p in sorted(DIST_ROOT.iterdir()) if p.is_file()]
    plugin_files = (
        [p.name for p in sorted(PLUGIN_ZIP_DIR.iterdir()) if p.is_file()]
        if PLUGIN_ZIP_DIR.is_dir()
        else []
    )
    if not DIST_DIR.is_dir():
        problems.append(f"{DIST_DIR}: missing from the release layout")
    for manifest in (SUMS_NAME, README_ASSETS_NAME):
        if not (DIST_ROOT / manifest).is_file():
            problems.append(f"{DIST_ROOT / manifest}: missing from the release layout")
    viewer_zip = viewer_zip_path()
    if not _present(viewer_zip.name, top_files):
        problems.append(
            f"{viewer_zip}: neither the zip nor its split parts are present"
        )
    expected_top = {
        DIST_DIR.name, PLUGIN_ZIP_DIR.name, PUBLIC_DIR.name,
        WITHOUT_VIEWER_DIR.name, SUMS_NAME, README_ASSETS_NAME,
    }
    for entry in sorted(DIST_ROOT.iterdir()):
        if entry.name in expected_top:
            continue
        part = _PART_SUFFIX_RE.match(entry.name)
        if entry.is_file() and (
            entry.name == viewer_zip.name
            or (part is not None and part["stem"] == viewer_zip.name)
        ):
            continue
        problems.append(
            f"{entry}: unexpected entry in dist/ — everything here is "
            "uploaded as a release asset"
        )
    problems.extend(f"{DIST_ROOT}: {p}" for p in split_sequence_offenders(top_files))
    problems.extend(
        f"{PLUGIN_ZIP_DIR}: {p}" for p in split_sequence_offenders(plugin_files)
    )
    for plugin_id in discover_repo_plugins():
        expected = plugin_zip_path(plugin_id)
        if not _present(expected.name, plugin_files):
            problems.append(
                f"{expected}: neither the zip nor its split parts are present"
            )
    viewer_files = {n for n in top_files if n not in (SUMS_NAME, README_ASSETS_NAME)}
    sums = DIST_ROOT / SUMS_NAME
    if sums.is_file():
        listed = _sums_names(sums)
        for name in sorted((viewer_files | set(plugin_files)) - listed):
            problems.append(f"{SUMS_NAME}: no hash listed for {name}")
    # The public pair must not even NAME a plugin pack (the public side must
    # not learn which plugins exist).
    problems.extend(
        manifest_pair_offenders(
            PUBLIC_DIR, viewer_files, _rejoined_names(viewer_files), plugin_files
        )
    )
    problems.extend(
        manifest_pair_offenders(
            WITHOUT_VIEWER_DIR, set(plugin_files), _rejoined_names(plugin_files)
        )
    )
    if problems:
        raise SystemExit(
            "[build] release layout verification failed:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


# ---------------------------------------------------------------------------
# Entry point.


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_portable.py",
        description=(
            "Build the portable Snappix distributions (plain viewer zip + "
            "per-plugin packs) into dist/ as GitHub-release-ready assets."
        ),
    )
    parser.add_argument(
        "--max-part-mib",
        type=int,
        default=DEFAULT_MAX_PART_MIB,
        help=(
            "split an asset larger than this into .zip.001/.002… "
            f"(default: {DEFAULT_MAX_PART_MIB}; GitHub caps one asset at 2 GiB)"
        ),
    )
    parser.add_argument(
        "--max-part-bytes",
        type=int,
        default=None,
        help=argparse.SUPPRESS,  # test back door: the threshold in raw bytes
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    stream = getattr(sys.stdout, "reconfigure", None)
    if stream is not None:
        stream(encoding="utf-8", errors="replace")
    args = build_arg_parser().parse_args(argv)
    max_bytes = (
        args.max_part_bytes
        if args.max_part_bytes is not None
        else args.max_part_mib * 1024 * 1024
    )
    check_version_sync()
    hooks = load_build_hooks()
    if hooks:
        print(
            "[build] Plugin build hooks: "
            + ", ".join(plugin_id for plugin_id, _ in hooks)
        )
    else:
        print("[build] No plugin build hooks (plain viewer only)")
    for plugin_id, hook in hooks:
        _hook_call(hook, "check_build_env", HookApi(plugin_id))
    print("[build] Running PyInstaller (viewer)…")
    run_pyinstaller()
    for plugin_id, hook in hooks:
        print(f"[build] Building plugin pack '{plugin_id}'…")
        _hook_call(hook, "build", HookApi(plugin_id))
    prune_gpl_qt()
    print("[build] Writing third-party license notices…")
    write_license_notices()
    print("[build] Writing terms of use / disclaimer…")
    write_terms_document()
    print("[build] Writing user docs…")
    write_docs()
    print("[build] Writing plugins folder + developer guide…")
    write_plugins_dir()
    print("[build] Writing Explorer shell-integration scripts…")
    write_shell_integration_scripts()
    print("[build] Verifying dist completeness…")
    check_dist_complete(hooks)
    for plugin_id, hook in hooks:
        _hook_call(hook, "check_dist", HookApi(plugin_id))
    # The PLAIN distribution is defined by this moment: the folder has been
    # verified plugin-free, so zipping it here (and verifying the archive's own
    # entry names) is what makes the shipped zip plugin-free — not a filter.
    print("[build] Zipping the plain distribution…")
    write_plain_zip()
    check_plain_zip()
    print("[build] Installing plugin packs into the viewer folder…")
    install_plugin_packs(hooks)
    check_installed_plugins(hooks)
    print("[build] Zipping plugins…")
    # After the verifications: staged-pack zips must capture the VERIFIED
    # packs (zipping earlier could freeze a broken tree into the artefact).
    write_plugin_zips(hooks)
    check_plugin_zips(hooks)
    assets = split_oversize_zips(max_bytes)
    write_release_manifest(assets)
    check_release_layout()
    viewer_name = viewer_zip_path().name
    outputs = [f"dist/{DIST_DIR.name}/ (plugins installed)", f"dist/{viewer_name}"]
    outputs += [
        f"dist/{PLUGIN_ZIP_DIR.name}/{n}"
        for asset in assets
        if asset.name != viewer_name
        for n, _h in asset.parts
    ]
    outputs += [f"dist/{SUMS_NAME}", f"dist/{README_ASSETS_NAME}"]
    outputs += [f"dist/{PUBLIC_DIR.name}/ (viewer-only manifests for the public release)"]
    outputs += [
        f"dist/{WITHOUT_VIEWER_DIR.name}/ (manifests for a release without the viewer)"
    ]
    print("[build] Done. Output: " + " + ".join(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
