"""Dark title bar for top-level windows on Windows.

The single biggest "homemade tool" tell on Windows is a dark-themed app
with a bright white native title bar.  DWM exposes a per-window flag
(``DWMWA_USE_IMMERSIVE_DARK_MODE``) that flips the title bar to the dark
variant; it takes effect immediately, including on already-visible
windows.  No-op on other platforms and on failures (old Windows builds,
non-native widgets) — purely cosmetic, never worth crashing over.
"""

from __future__ import annotations

import sys

# Documented value on Windows 10 20H1+ / Windows 11; insider builds before
# 20H1 used 19.  We try the modern value first and fall back.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19

# Windows 11 22000+ rounded-corner control.  ``DWMWCP_ROUNDSMALL`` = small
# radius, which suits compact popups (tooltips) better than the default large
# window radius.  Ignored on Windows 10 (corners stay square — no artefact).
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUNDSMALL = 3


def apply_titlebar_theme(window, dark: bool) -> None:
    """Switch *window*'s native title bar to dark/light mode (Windows only).

    *window* must be a top-level ``QWidget`` that already has a native
    window handle (i.e. it is shown, or ``winId()`` was called).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.c_void_p(int(window.winId()))
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        # Declare the signature so the 64-bit HWND is passed as a pointer-width
        # value rather than being silently truncated to a C int (which would
        # raise ctypes.ArgumentError for HWNDs >= 2**31 and get swallowed
        # below, leaving the title bar stuck light).
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        for attr in (
            _DWMWA_USE_IMMERSIVE_DARK_MODE,
            _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
        ):
            if dwm.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:  # pragma: no cover - cosmetic best-effort
        pass


def round_window_corners(window) -> None:
    """Ask DWM to round *window*'s corners (Windows 11+; no-op otherwise).

    The compositor clips the window to a rounded rect *at composition time*,
    so an **opaque** window gets clean rounded corners with the desktop
    showing through — unlike a QSS ``border-radius`` on an opaque top-level,
    where the pixels outside the arc keep the (black) window backing.  Used
    for tooltips, which stay opaque (for a distinct, legible fill) yet want
    soft corners.  *window* must already have a native handle (i.e. be shown).
    Best-effort and cosmetic: any failure (Windows 10, old build, non-native
    widget) leaves the corners square, never crashes.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.c_void_p(int(window.winId()))
        pref = ctypes.c_int(_DWMWCP_ROUNDSMALL)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        dwm.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
    except Exception:  # pragma: no cover - cosmetic best-effort
        pass
