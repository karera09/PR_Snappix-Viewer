"""Filename and path component sanitization utilities."""

from __future__ import annotations

import re

# Windows-illegal characters + control chars + leading/trailing dots/spaces.
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Lone/unpaired surrogate code points (U+D800–U+DFFF).  These are valid Python
# ``str`` characters (e.g. from ``os.fsdecode`` of undecodable bytes, or
# ``surrogateescape``) but cannot be UTF-8 encoded — every ``.encode("utf-8")``
# below would raise ``UnicodeEncodeError``.  Sanitize is the defensive boundary
# layer and must never raise, so we scrub surrogates to "_" at each entry point
# before any byte-length work.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# Reserved Windows device names.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _is_reserved(name: str) -> bool:
    """True if *name*'s base (up to the first dot) is a reserved device name.

    Win32 classic path resolution redirects a file whose *base name* is a DOS
    device to the device itself, so "CON.txt" / "con.tar.gz" are just as
    dangerous as a bare "CON".  Match the stem, not the whole component.

    Windows also ignores spaces between the device name and the dot
    (``RtlIsDosDeviceName_U`` strips trailing spaces before matching), so
    "CON .txt" resolves to the CON device too — rstrip the stem before
    comparing.
    """
    return name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED

# Public byte-length contract values (do NOT relax — see CLAUDE.md).  They
# are consumed outside this module (an optional writer plugin's storage
# layer builds every folder / file name against them), so they are part of
# the shared layer's public API.
#
# ext4/XFS/btrfs use 255 UTF-8 bytes per path component.
MAX_COMPONENT_BYTES = 255
# Filenames reserve 5 bytes for the ".part" temp suffix a downloading writer
# appends while the file is still in flight.
MAX_FILENAME_BYTES = 250
# Video-embed filenames need a much larger reserve. Video fetch tools write DASH
# intermediates named ``<stem>.f<format_id>.<ext>.part`` (e.g. ``.f399.mp4.part``)
# — ~20 UTF-8 bytes beyond the stem, far more than a regular writer's
# 5-byte ``.part``. Capping the final filename at 225 bytes keeps that temp
# file within the 255-byte component limit on NAS filesystems (ext4/XFS);
# otherwise the write fails with ``[Errno 22] Invalid argument``.
MAX_VIDEO_FILENAME_BYTES = 225

# Backward-compat aliases (the constants predate their public names; existing
# importers keep working — prefer the public names in new code).
_MAX_COMPONENT_BYTES = MAX_COMPONENT_BYTES
_MAX_FILENAME_BYTES = MAX_FILENAME_BYTES
_MAX_VIDEO_FILENAME_BYTES = MAX_VIDEO_FILENAME_BYTES


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to fit within *max_bytes* of UTF-8 at character boundaries.

    A negative *max_bytes* means "no budget at all", not "cut from the end":
    ``encoded[:max_bytes]`` follows Python's slice rules and would count from
    the right, returning bytes instead of nothing.  Callers outside this
    module derive budgets arithmetically (``limit - len(prefix)``), so the
    clamp belongs on this side of the public API.
    """
    max_bytes = max(0, max_bytes)
    # Scrub lone surrogates first — they are not UTF-8 encodable and would make
    # the ``.encode`` below raise (see ``_SURROGATE_RE``).
    text = _SURROGATE_RE.sub("_", text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# Backward-compat alias (predates the public name — prefer ``truncate_utf8``).
_truncate_utf8 = truncate_utf8


def sanitize_component(
    name: str,
    *,
    max_bytes: int = MAX_COMPONENT_BYTES,
) -> str:
    # A negative budget means "nothing fits" — see ``truncate_utf8``; without
    # the clamp the slice below would count from the right and return a tail.
    max_bytes = max(0, max_bytes)
    # Scrub lone surrogates before any encoding work (they are valid str but
    # not UTF-8 encodable — the byte-length guards below would raise).
    name = _SURROGATE_RE.sub("_", name)
    cleaned = _ILLEGAL_RE.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "untitled"
    if _is_reserved(cleaned):
        cleaned = f"_{cleaned}"
    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        cleaned = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .") or "untitled"
        # Truncation can re-expose a reserved name (e.g. "CONNECT" cut to
        # "CON", or "CON.log" cut back to a reserved stem) — re-apply the guard.
        # Truncate again so the "_" prefix never pushes the result past
        # max_bytes ("_" is ASCII, so the re-truncated result still starts with
        # "_" and cannot be reserved).
        if _is_reserved(cleaned):
            cleaned = truncate_utf8(f"_{cleaned}", max_bytes)
    return cleaned


def sanitize_filename(
    name: str,
    *,
    max_bytes: int = MAX_FILENAME_BYTES,
) -> str:
    # Scrub lone surrogates up front so the extension-splitting path (which
    # encodes ``ext`` directly, bypassing sanitize_component) never hits a
    # non-encodable character.
    name = _SURROGATE_RE.sub("_", name)
    # A dotfile's leading dot is part of the name, not an extension separator
    # (a leading dot is legal on Windows and NAS; ``sanitize_component`` alone
    # would strip it, and the extension split below would mangle ".gitignore"
    # into "untitled.gitignore").  Peel the leading dots off, sanitize the
    # rest with the normal rules and re-prepend them.  This covers multi-dot
    # dotfiles too (".env.local" keeps its dot and its ".local" extension) —
    # only stripping the leading dot for THOSE was the inconsistency behind
    # review #114.
    stripped = name.lstrip(".")
    if stripped and name.startswith("."):
        n_dots = len(name) - len(stripped)
        if n_dots >= max_bytes:
            # The leading dots alone fill (or overflow) the budget, so nothing
            # of the body would survive the cap below and the result would be a
            # dots-only name — which Windows refuses to create at all
            # (PermissionError), the exact opposite of this function's job
            # (review #179).  The dots cannot be preserved, so drop them and
            # sanitize the body against the full budget.
            return sanitize_filename(stripped, max_bytes=max_bytes)
        dots = "." * n_dots
        # Cap the assembled result too: when the leading dots (nearly) fill the
        # budget, the recursive call's own byte cap cannot help (its budget is
        # clamped to >= 0, and the "untitled" fallback ignores a zero budget),
        # so without this the branch could return more than max_bytes bytes.
        # The recursion terminates after one step: ``stripped`` never starts
        # with a dot, so this branch cannot be re-entered.
        result = dots + sanitize_filename(
            stripped, max_bytes=max(0, max_bytes - n_dots)
        )
        return truncate_utf8(result, max_bytes)
    # Preserve extension if any.
    if "." in name:
        stem, _, ext = name.rpartition(".")
        ext = "." + _ILLEGAL_RE.sub("_", ext).strip(" .")
        # Fix 1: a trailing dot in the original name (e.g. "file.") produces
        # ext == "." after stripping — that suffix is illegal on Windows.
        if ext == ".":
            ext = ""
    else:
        stem, ext = name, ""
    ext_bytes = len(ext.encode("utf-8"))
    # Fix 2: oversized extension — clamp ext so the budget for the stem is
    # never negative.  If the extension alone already fills max_bytes we
    # drop it entirely and treat the whole name as a stem, which
    # sanitize_component then truncates normally.
    if ext_bytes >= max_bytes:
        stem = name
        ext = ""
        ext_bytes = 0
    safe_stem = sanitize_component(
        stem,
        max_bytes=max_bytes - ext_bytes,
    )
    result = safe_stem + ext
    # Final safety net: the assembled result must never exceed max_bytes.
    # This can only trigger when ext_bytes is large enough to push the total
    # over the limit even after the stem was truncated (e.g. a 240-byte
    # extension with a 1-byte stem that sanitize_component returned).
    result_bytes = len(result.encode("utf-8"))
    if result_bytes > max_bytes:
        # Try to keep a short extension; if ext itself is too big just
        # truncate the whole assembled string.
        if ext and ext_bytes < max_bytes:
            stem_budget = max_bytes - ext_bytes
            safe_stem = truncate_utf8(safe_stem, stem_budget).rstrip(" .") or "untitled"
            result = safe_stem + ext
        else:
            result = truncate_utf8(result, max_bytes).rstrip(" .") or "untitled"
    return result
