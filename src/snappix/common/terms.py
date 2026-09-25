"""First-run consent gate for the snappix 利用規約・免責事項.

The shipped terms document (:data:`~snappix.common.terms_text.TERMS_TEXT`,
also written next to the EXE as ``利用規約・免責事項.txt``) is turned into a
click-through agreement: each GUI entry point calls :func:`require_consent`
on startup, which shows the terms and refuses to proceed unless the user
agrees; a GUI-less entry point may call :func:`has_accepted`
and refuses to run unattended when consent has not yet been given via a GUI.

Acceptance is recorded in ``data/terms_accepted.json`` — portable (next to
the EXE, never ``%APPDATA%``/registry/home) and keyed by
:data:`~snappix.common.terms_text.TERMS_VERSION`, so bumping the version
re-prompts every user.  Both EXEs share the same ``data/`` directory, so
agreeing in one satisfies the other.

Qt is imported lazily (only inside :func:`require_consent`, via
``terms_dialog``) so the headless runner — which must never import PySide6 —
can import :func:`has_accepted` from this module safely.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from .paths import get_paths
from .terms_text import TERMS_VERSION

#: Filename (under ``data/``) recording the accepted terms version.
ACCEPTANCE_FILENAME = "terms_accepted.json"

#: Matches the PID-unique temp names written by :func:`record_acceptance`
#: (``terms_accepted.json.<pid>.tmp``); group 1 captures the PID.
_TMP_NAME_RE = re.compile(re.escape(ACCEPTANCE_FILENAME) + r"\.(\d+)\.tmp\Z")

#: Minimum age (seconds) before an orphaned temp file is swept.  A live
#: writer holds its temp file for milliseconds only (write → ``os.replace``),
#: so an hour-old temp can only be crash debris — including one whose PID
#: number matches ours, which on Windows just means the number was recycled.
_STALE_TMP_MAX_AGE = 3600.0


def _acceptance_path(data_dir: Path | None) -> Path:
    return (data_dir or get_paths().data) / ACCEPTANCE_FILENAME


def _sweep_stale_tmp_files(
    data_dir: Path | None = None, *, max_age: float = _STALE_TMP_MAX_AGE
) -> None:
    """Best-effort removal of crash-stranded acceptance temp files.

    The PID-unique temp names that make concurrent writes safe have a flip
    side: a hard crash between write and replace strands
    ``terms_accepted.json.<pid>.tmp`` forever (no later run reuses that name,
    unlike the old fixed-name scheme which self-healed on the next write).
    Called on every consent-gate startup and before each write, this sweeps
    such debris.

    Age alone decides.  A live writer holds its temp file for the
    milliseconds between ``write`` and ``os.replace``, so anything older than
    *max_age* cannot be in flight — not even our own.  Carving out our PID
    instead would spare exactly the file that most needs sweeping: Windows
    recycles PIDs, so a crashed past instance's debris can carry the number
    this process was just handed, and that one file would then survive every
    sweep for the whole session.  Errors are swallowed — housekeeping must
    never break the gate.
    """
    directory = data_dir or get_paths().data
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    now = time.time()
    for entry in entries:
        if _TMP_NAME_RE.fullmatch(entry.name) is None:
            continue
        try:
            if now - entry.stat().st_mtime > max_age:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _read_acceptance_record(data_dir: Path | None = None) -> dict | None:
    """Best-effort read of the raw acceptance record, or ``None``.

    Shared by :func:`has_accepted` and :func:`previously_accepted_version` —
    both treat a missing/unreadable/corrupt file identically (as "nothing
    recorded"), so the parsing lives in one place.

    **Never raises.**  The gate runs before the main window exists
    (``viewer/app.py`` calls :func:`require_consent` ahead of MainWindow and
    outside its ``except OSError`` startup guard), so an escaping exception
    ends the process with no window and — in a windowed frozen build — no
    stderr to say why.
    """
    try:
        raw = _acceptance_path(data_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # ``UnicodeDecodeError`` is a ``ValueError``, *not* an ``OSError``:
        # a non-UTF-8 acceptance file (hand-edited, restored from a backup
        # with the wrong encoding, partially damaged on a NAS) would escape
        # a bare ``except OSError`` — the same defect class already fixed in
        # ``viewer/plugin_host/manifest.py``.  Undecodable == nothing
        # recorded: the gate re-prompts rather than crashing.
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def has_accepted(data_dir: Path | None = None, *, version: str = TERMS_VERSION) -> bool:
    """Return ``True`` iff the current terms *version* was already accepted.

    Robust to a missing, unreadable, or corrupt acceptance file (all treated
    as "not accepted"), so a damaged ``data/`` never silently bypasses the
    gate nor crashes startup.
    """
    record = _read_acceptance_record(data_dir)
    return record is not None and record.get("accepted_version") == version


def previously_accepted_version(data_dir: Path | None = None) -> str | None:
    """Return a prior run's recorded ``accepted_version``, if any.

    Unlike :func:`has_accepted` (which only answers yes/no for *one*
    version), this surfaces whatever version was last recorded — used by
    :func:`require_consent` to tell "a genuine first run" (nothing recorded)
    apart from "the terms were revised since this install last agreed"
    (an older version is on file). A missing/unreadable/corrupt record reads
    as "nothing recorded" (``None``), same as :func:`has_accepted`.
    """
    record = _read_acceptance_record(data_dir)
    if record is None:
        return None
    version = record.get("accepted_version")
    return version if isinstance(version, str) else None


def record_acceptance(
    data_dir: Path | None = None, *, version: str = TERMS_VERSION
) -> None:
    """Persist acceptance of the current terms *version* (atomic write)."""
    target = _acceptance_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_tmp_files(data_dir)
    payload = {
        "accepted_version": version,
        "accepted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # PID-unique temp name: with a FIXED ".tmp" name the viewer and tag
    # scanner (which share this ``data/`` dir) agreeing near-simultaneously
    # would interleave write/replace on the same temp file — a Windows sharing
    # violation that would propagate to the consent handler.  Distinct temp
    # files keep each replace atomic; the outcome is plain last-writer-wins
    # (both write the same accepted version).  Mirrors ``shared_prefs.py``.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
    except OSError:
        # Best-effort cleanup so a failed write doesn't strand a temp file
        # next to the record (the original error is re-raised for callers).
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def require_consent(*, data_dir: Path | None = None, parent=None) -> bool:
    """Gate a GUI entry point on acceptance of the terms.

    Returns ``True`` to proceed (already accepted, or accepted just now via
    the dialog); ``False`` if the user declined — the caller must then exit
    without showing its window.  The PySide6-dependent dialog is imported
    lazily here so this module stays Qt-free for the headless runner.
    """
    data_dir = data_dir or get_paths().data
    # Startup housekeeping: reap temp files stranded by a hard crash of an
    # earlier run (record_acceptance may never run again once accepted).
    _sweep_stale_tmp_files(data_dir)
    if has_accepted(data_dir):
        return True
    from .terms_dialog import prompt_consent

    # A version mismatch here can mean two very
    # different things — a genuine first run (nothing recorded yet) or a
    # returning user whose prior acceptance was invalidated by a terms
    # revision. The dialog shows a different introduction for the latter so
    # "I already agreed to this" doesn't read as a malfunction.
    previous_version = previously_accepted_version(data_dir)
    if prompt_consent(parent, previous_version=previous_version):
        try:
            record_acceptance(data_dir)
        except OSError as exc:
            # The user DID agree — a failure to persist that (full disk, an AV
            # holding the file, read-only media) must not abort startup: the
            # frozen build is windowed, so an escaping OSError would look like
            # the app vanishing right after "同意する".
            # The session proceeds; the gate simply re-prompts next launch.
            logger.warning("could not record terms acceptance: {}", exc)
        return True
    return False
