"""Explorer shell integration — the 「Snappix Viewer で開く」 right-click verb.

Portable-first policy (see CLAUDE.md): the viewer NEVER writes to the registry
on its own.  The *only* registry writes in the product are the ones a user
explicitly triggers here — from the in-app 「エクスプローラ統合…」 dialog
(``main_window``) or the shipped ``シェル統合を登録.bat`` / ``…を解除.bat``
scripts.  Both paths read and write the **same HKCU keys**, so whichever one
registers, the other can fully and safely unregister.

Keys (per-user hive → no admin rights, HKLM is never touched)::

    HKCU\\Software\\Classes\\Directory\\shell\\SnappixViewer
    HKCU\\Software\\Classes\\Directory\\Background\\shell\\SnappixViewer

The ``Directory`` verb appears on a folder's right-click menu and Windows
passes the folder as ``%1``; the ``Directory\\Background`` verb appears when
right-clicking the empty area *inside* a folder and passes the open folder as
``%V``.  Both invoke ``"<exe>" "%1"`` / ``"<exe>" "%V"``, received by the
viewer's positional-argument handling (:mod:`snappix._dispatch`, L02).

This module is intentionally **Qt-free and import-light**: ``winreg`` (Windows
only) is imported lazily inside the functions that touch the registry, so the
pure key/command/script builders remain importable and unit-testable on any
platform (and by ``build_portable.py``, which renders the .bat files from the
exact same constants).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NamedTuple

#: Context-menu label written as each key's default value.  Single source of
#: truth shared by the winreg writer and the generated .bat scripts.
MENU_LABEL = "Snappix Viewer で開く"

#: HKCU subkey (below ``HKEY_CURRENT_USER``) for the folder right-click verb.
DIRECTORY_KEY = r"Software\Classes\Directory\shell\SnappixViewer"
#: HKCU subkey for the folder-background (empty-area) right-click verb.
BACKGROUND_KEY = r"Software\Classes\Directory\Background\shell\SnappixViewer"

#: The two verb roots to delete on unregister (deleting each recursively drops
#: its ``command`` subkey too).  Only these exact key names are ever removed —
#: never a parent Classes key we didn't create.
TOP_LEVEL_KEYS: tuple[str, ...] = (DIRECTORY_KEY, BACKGROUND_KEY)

#: The exe token the generated .bat scripts use — ``%~dp0`` expands to the
#: script's own folder, so dropping the scripts beside ``SnappixViewer.exe``
#: makes them self-locating without hardcoding an install path.
_BAT_EXE = "%~dp0SnappixViewer.exe"


class RegEntry(NamedTuple):
    """One registry write: ``value_name=None`` means the key's default value."""

    subkey: str
    value_name: str | None
    value: str


@dataclass(frozen=True)
class ShellIntegrationStatus:
    """Result of :func:`status` — whether the verb is registered and for which
    exe (so the dialog can warn when a stale path from a previous install
    location is still registered)."""

    registered: bool
    exe_path: str | None


def registry_entries(exe_path: str) -> list[RegEntry]:
    """The full set of HKCU writes that register the verb for *exe_path*.

    Single source of truth for :func:`register` (and the reference the .bat
    generator is tested against).  ``%1`` / ``%V`` are the literal placeholders
    Explorer substitutes at invocation time.
    """
    dir_cmd = f'"{exe_path}" "%1"'
    bg_cmd = f'"{exe_path}" "%V"'
    return [
        RegEntry(DIRECTORY_KEY, None, MENU_LABEL),
        RegEntry(DIRECTORY_KEY, "Icon", exe_path),
        RegEntry(DIRECTORY_KEY + r"\command", None, dir_cmd),
        RegEntry(BACKGROUND_KEY, None, MENU_LABEL),
        RegEntry(BACKGROUND_KEY, "Icon", exe_path),
        RegEntry(BACKGROUND_KEY + r"\command", None, bg_cmd),
    ]


# --------------------------------------------------------------------- winreg


def is_available() -> bool:
    """Whether the registry is reachable (i.e. running on Windows)."""
    try:
        import winreg  # noqa: F401
    except ImportError:
        return False
    return True


def register(exe_path: str) -> None:
    """Register the right-click verb for *exe_path* under HKCU.

    Idempotent (``CreateKeyEx`` opens-or-creates, ``SetValueEx`` overwrites),
    so re-registering after moving the exe simply repoints the command.
    """
    import winreg

    for entry in registry_entries(exe_path):
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, entry.subkey, 0, winreg.KEY_WRITE
        )
        try:
            winreg.SetValueEx(
                key, entry.value_name or "", 0, winreg.REG_SZ, entry.value
            )
        finally:
            winreg.CloseKey(key)


def unregister() -> None:
    """Remove the verb keys from HKCU (safe if they don't exist)."""
    import winreg

    for key in TOP_LEVEL_KEYS:
        _delete_tree(winreg.HKEY_CURRENT_USER, key)


def _delete_tree(root: int, subkey: str) -> None:
    """Recursively delete *subkey* and its children (``winreg.DeleteKey`` only
    removes leaf keys, and our verb keys carry a ``command`` child)."""
    import winreg

    try:
        with winreg.OpenKey(
            root, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as handle:
            while True:
                try:
                    child = winreg.EnumKey(handle, 0)
                except OSError:
                    break
                _delete_tree(root, subkey + "\\" + child)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        pass


def status() -> ShellIntegrationStatus:
    """Report whether the verb is registered, and the exe it points at."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, DIRECTORY_KEY + r"\command"
        ) as handle:
            command, _ = winreg.QueryValueEx(handle, "")
    except FileNotFoundError:
        return ShellIntegrationStatus(registered=False, exe_path=None)
    except OSError:
        return ShellIntegrationStatus(registered=False, exe_path=None)
    return ShellIntegrationStatus(
        registered=True, exe_path=exe_from_command(str(command))
    )


def same_exe_path(a: str | None, b: str | None) -> bool:
    """Whether two exe paths name the same file, Windows-style.

    Used by the integration dialog to spot a **stale registration** — the
    portable product is routinely moved as a folder, and the registered verb
    then points at a location that no longer exists (UIレビュー 08-28 N-37).
    Comparison is path-only and read-only: normalised (``abspath`` folds
    ``..`` / separators) and case-folded, because the registered string comes
    from an earlier run's ``sys.executable`` and Windows paths are
    case-insensitive.  ``None`` never matches anything, including ``None``.
    """
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(
        os.path.abspath(b)
    )


def exe_from_command(command: str) -> str | None:
    """Extract the exe path from a ``"<exe>" "%1"`` command string."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end > 0:
            return command[1:end]
    return command.split(" ", 1)[0] or None


# ----------------------------------------------------------------- .bat text


def _reg_add_default(key: str, value: str) -> str:
    return f'reg add "HKCU\\{key}" /ve /d "{value}" /f'


def _reg_add_named(key: str, name: str, value: str) -> str:
    return f'reg add "HKCU\\{key}" /v {name} /d "{value}" /f'


def _reg_add_command(key: str, exe: str, placeholder: str) -> str:
    # reg.exe needs inner quotes escaped as \" inside the /d argument, and a
    # literal '%' the value should keep (``%1`` / ``%V``) must be doubled so
    # cmd passes it through unexpanded — while ``%~dp0`` in *exe* stays single
    # so cmd DOES expand it to the script's folder.
    value = f'\\"{exe}\\" \\"%%{placeholder}\\"'
    return f'reg add "HKCU\\{key}\\command" /ve /d "{value}" /f'


def render_register_script() -> str:
    """The ``シェル統合を登録.bat`` body (CRLF, UTF-8).

    Writes the *same* HKCU keys as :func:`register`, so the two registration
    paths are interchangeable and either can be undone by the other.
    """
    lines = [
        "@echo off",
        "rem Snappix Viewer - エクスプローラ右クリック統合を登録します (HKCU / 管理者不要)。",
        "rem アプリ内の「エクスプローラ統合...」ダイアログと同じキーを書き込みます。",
        "rem ポータブル運用の方針上、アンインストール前に「シェル統合を解除.bat」で解除してください。",
        "chcp 65001 >nul",
        "setlocal",
        "",
        _reg_add_default(DIRECTORY_KEY, MENU_LABEL),
        _reg_add_named(DIRECTORY_KEY, "Icon", _BAT_EXE),
        _reg_add_command(DIRECTORY_KEY, _BAT_EXE, "1"),
        _reg_add_default(BACKGROUND_KEY, MENU_LABEL),
        _reg_add_named(BACKGROUND_KEY, "Icon", _BAT_EXE),
        _reg_add_command(BACKGROUND_KEY, _BAT_EXE, "V"),
        "",
        "echo.",
        "echo フォルダの右クリックに「Snappix Viewer で開く」を登録しました。",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def render_unregister_script() -> str:
    """The ``シェル統合を解除.bat`` body (CRLF, UTF-8).

    Deletes the same top-level keys :func:`unregister` removes, so a
    registration made from the in-app dialog is fully undone here too.
    """
    lines = [
        "@echo off",
        "rem Snappix Viewer - エクスプローラ右クリック統合を解除します。",
        "rem アプリ内ダイアログ / 登録.bat のどちらで登録した場合も完全に解除できます。",
        "chcp 65001 >nul",
        "setlocal",
        "",
        f'reg delete "HKCU\\{DIRECTORY_KEY}" /f 2>nul',
        f'reg delete "HKCU\\{BACKGROUND_KEY}" /f 2>nul',
        "",
        "echo.",
        "echo 「Snappix Viewer で開く」の右クリック統合を解除しました。",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


__all__ = [
    "MENU_LABEL",
    "DIRECTORY_KEY",
    "BACKGROUND_KEY",
    "TOP_LEVEL_KEYS",
    "RegEntry",
    "ShellIntegrationStatus",
    "registry_entries",
    "is_available",
    "register",
    "unregister",
    "status",
    "exe_from_command",
    "same_exe_path",
    "render_register_script",
    "render_unregister_script",
]
