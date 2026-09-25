"""Shared ZIP extraction core (viewer ZIP drill-in / preview).

The viewer consumes this module in two places: ``viewer/zip_drill.py``
extracts a double-clicked archive into a temp folder via
:func:`extract_zip_to_dir`, and ``viewer/content/zip_view.py`` uses
:func:`decode_zip_member_name` to show readable names in the ZIP preview
list.  (The module predates the viewer's split from a larger tool suite,
where it also powered automatic archive extraction — that entry point is
gone; only the generic extraction core remains.)

Design notes
------------
* **Structure preserving**: subdirectories inside the archive are recreated
  as-is under the target folder; nothing is flattened.  Directory entries are
  materialised in their own pass, so an archive that stores empty folders gets
  them back too (they are not counted in ``extracted`` / progress).
* **Collision safe**: an entry keeps its original filename whenever that name is
  free. Only when it would clash with an already-extracted file (e.g. two names
  collapse to the same after sanitization, or the archive genuinely contains
  duplicates) is a ``_2`` / ``_3`` … suffix appended — the original file is
  never overwritten. Uniquification touches only the filename, never the
  enclosing folders, so the directory layout is left intact.
* **Zip-slip safe**: every member's resolved path must stay within the target
  directory; entries that escape (``../`` / absolute paths) are skipped.
* **Filename fidelity**: the last path component is sanitized as a *filename*
  (``sanitize_filename``), so a dotfile (``.gitignore``) keeps its leading dot
  instead of losing it to the component rules; only the enclosing directory
  components use ``sanitize_component``.
* **CJK filenames**: ZIPs created on Japanese Windows often store names in
  cp932 without the UTF-8 flag, which Python decodes as cp437 mojibake. When
  the flag is absent we recover the raw bytes and re-decode, trying utf-8
  first (flagless-UTF-8 archives) and falling back to cp932. Names made
  purely of paired halfwidth katakana are a known, accepted casualty of the
  utf-8-first order — see :func:`decode_zip_member_name`.
"""

from __future__ import annotations

import lzma
import os
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .sanitize import (
    MAX_COMPONENT_BYTES,
    sanitize_component,
    sanitize_filename,
    truncate_utf8,
)

# Fixed byte cost charged per extracted member against ``max_total_bytes``.
# The cumulative-bytes guard alone counts only *decompressed content*, so an
# archive of e.g. a million 0-byte members would inflate to millions of files
# (inodes / directory entries / open-close syscalls) while adding nothing to
# the running total — never tripping the guard. Charging a fixed overhead per
# entry makes that class of bomb count toward the same cap.
#
# The *magnitude* is load-bearing. The sole caller
# (``viewer/zip_drill.py``) derives ``max_total_bytes`` as
# ``<size ceiling> * 100`` (``_EXTRACT_SIZE_RATIO``). Every ZIP member already
# costs at least ~76 bytes *inside the archive* (a 30-byte local header + a
# 46-byte central-directory record) plus its name, so a degenerate all-empty
# archive of N members lifts that cap by ≳ ``N * 76 * 100`` = ``N * 7600``.
# The previous 4096-byte charge sat *below* that product, so the
# charge/cap ratio stayed under 1 no matter how many members piled up — the
# guard was a structural no-op for the very "inode bomb" it was written to
# stop. Charging well above ``100 * 76`` per entry makes the running charge
# outpace the cap for such archives, so the guard actually fires. 16 KiB keeps
# a comfortable margin over that floor while staying far below any real media
# file's footprint: genuine members carry KiB-to-MiB of decompressed content,
# whose cap contribution dwarfs this fixed charge, so legitimate archives
# never trip on it.
_PER_ENTRY_OVERHEAD_BYTES = 16384


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of :func:`extract_zip_to_dir`.

    ``completed`` is ``False`` when the extraction stopped early because
    *should_cancel* returned ``True``.  ``extracted`` counts the members
    actually written; ``skipped`` counts non-directory members that were
    passed over with only a log warning (zip-slip / out-of-root entries,
    names with no usable path components, and entries whose parent path
    component already exists as a file).  A non-zero ``skipped`` means the
    output is missing content even when ``completed`` is ``True`` — callers
    must not present such a result as a full extraction（設計原則:
    打ち切り・欠落を「全件」に見せない）.
    """

    completed: bool
    extracted: int
    skipped: int


class ExtractSizeLimitError(RuntimeError):
    """Raised by :func:`extract_zip_to_dir` when the cumulative extracted
    bytes exceed ``max_total_bytes`` (zip-bomb guard)."""

    def __init__(self, max_total_bytes: int) -> None:
        super().__init__(
            f"extracted size exceeds limit ({max_total_bytes} bytes)"
        )
        self.max_total_bytes = max_total_bytes


def _numbered_name(stem: str, suffix: str, i: int) -> str:
    """``<stem>_<i><suffix>`` shortened to stay within the byte limit.

    ``_safe_member_path`` already ran the name through
    :func:`~snappix.common.sanitize.sanitize_component`, so a long CJK name can
    sit exactly on the ``MAX_COMPONENT_BYTES`` (255) budget. Appending ``_2``
    naively would overflow it and ``open("wb")`` would raise ``OSError`` (Errno
    36 on ext4/XFS), aborting the *whole* extraction — so the stem is truncated
    to make room for the counter instead.
    """
    marker = f"_{i}"
    budget = (
        MAX_COMPONENT_BYTES
        - len(marker.encode("utf-8"))
        - len(suffix.encode("utf-8"))
    )
    if budget < 1:
        # Pathological: the extension alone (nearly) fills the budget. Nothing
        # can be kept of the stem, so cap the assembled name as a whole.
        return truncate_utf8(f"{stem}{marker}{suffix}", MAX_COMPONENT_BYTES)
    return f"{truncate_utf8(stem, budget)}{marker}{suffix}"


def _unique_file(
    target: Path, claimed: set[Path], next_suffix: dict[Path, int] | None = None,
) -> Path:
    """Return *target* or the first ``_2`` / ``_3`` … filename variant that's free.

    A name is considered taken if it already exists on disk **or** was already
    handed out during this extraction (*claimed*). Only the filename component is
    varied — the parent directory is preserved so the archive's folder structure
    stays intact.  The numbered variant is kept within the 255-byte component
    limit by :func:`_numbered_name`.

    *next_suffix* is an optional per-extraction ``{target: counter}`` **hint**
    so a run of members collapsing onto the same name doesn't rescan
    ``_2 … _N`` from scratch every time (without it an
    archive of N same-named entries would cost O(N²) ``exists()`` calls — on a NAS,
    one stat round-trip each).  It is only a starting point: the loop still
    advances past anything taken, so truncation collisions from
    :func:`_numbered_name` and files created by someone else mid-extraction
    behave exactly as before.
    """
    if target not in claimed and not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 2 if next_suffix is None else next_suffix.get(target, 2)
    while True:
        candidate = target.with_name(_numbered_name(stem, suffix, i))
        if candidate not in claimed and not candidate.exists():
            if next_suffix is not None:
                next_suffix[target] = i + 1
            return candidate
        i += 1


def decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    """Best-effort decode of a ZIP member name, handling cp932 mojibake.

    ``zipfile`` decodes names with cp437 when the UTF-8 flag (bit 11) is not
    set. Japanese archives commonly store cp932 bytes without that flag, so the
    cp437-decoded string is mojibake. Recover the original bytes and try to
    decode them.

    The re-encode source is :attr:`~zipfile.ZipInfo.orig_filename`, **not**
    ``filename``: on Windows ``ZipInfo.__init__`` rewrites ``os.sep`` (``\\``)
    to ``/`` in ``filename``, which destroys the 0x5C trailing byte of cp932
    "ダメ文字" (e.g. ソ ``0x83 0x5C`` / 表 ``0x95 0x5C``) before we can recover
    them. ``orig_filename`` preserves the raw cp437-decoded string, so it
    round-trips faithfully. We fall back to ``filename`` only when recovery
    fails.

    We try ``utf-8`` **before** ``cp932``: some archivers (older Info-ZIP /
    a few Linux tools) store genuine UTF-8 name bytes yet omit the UTF-8 flag.
    A large fraction of such UTF-8 Japanese names *also* happen to decode
    silently (but wrongly) as cp932, so a cp932-first order would mojibake
    them. cp932 *double-byte* characters (lead bytes 0x81-0x9F / 0xE0-0xFC)
    virtually always fail strict utf-8 decoding — 0x81-0x9F can only occur as
    UTF-8 continuation bytes, never as a sequence start — and thus fall
    through to the cp932 attempt correctly.

    **Known trade-off (halfwidth katakana)**: cp932 *single-byte* halfwidth
    katakana (0xA1-0xDF) are NOT covered by that safety net. A pair whose
    first byte is 0xC2-0xDF (ﾂ-ﾟ) and second 0xA1-0xBF (｡-ｿ) forms a valid
    UTF-8 two-byte sequence (930 such combinations), so a flagless cp932 name
    whose non-ASCII runs consist solely of such pairs decodes "successfully"
    as utf-8 mojibake — e.g. ﾈｺ.jpg (``C8 BA``) → Ⱥ.jpg, ﾂｱ.jpg (``C2 B1``)
    → ±.jpg. We accept this deliberately: such byte strings are valid in
    *both* encodings, so no byte-level rule can disambiguate them — genuine
    flagless-UTF-8 names like ``café.jpg`` (é = ``C3 A9``) are byte-wise
    "kana pairs" too, and preferring cp932 for them would mojibake real UTF-8
    names instead. The affected class is narrow (a name containing any
    double-byte cp932 char, an unpaired kana, or a pair starting with ｡-ﾁ
    still falls through to cp932 correctly), while flagless-UTF-8 archives
    are the more common input today.
    """
    flag_bits = info.flag_bits
    if flag_bits & 0x800:
        # UTF-8 flag set — zipfile already decoded correctly.
        return info.filename
    try:
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _safe_member_path(
    dest_root: Path,
    member_name: str,
    *,
    is_dir: bool = False,
    dest_root_resolved: Path | None = None,
) -> Path | None:
    """Map a (decoded) ZIP member name to a sanitized path under *dest_root*.

    Returns ``None`` when the entry would escape *dest_root* (zip-slip) or has
    no usable path components.

    *dest_root_resolved* is the loop invariant ``dest_root.resolve()``; the
    extraction loop resolves it once and hands it down, because ``resolve()``
    is a real filesystem round trip on Windows (``GetFinalPathNameByHandle``)
    and re-running it per member is a measurable share of the zip-slip check.
    Omitted, it is resolved here (the callers outside the loop).

    Directory components use ``sanitize_component``; the final component of a
    **file** member additionally keeps filename semantics via
    ``sanitize_filename`` (a leading dot survives — ``sanitize_component``
    would strip it — and the extension is split before the byte cap).
    ``is_dir=True`` therefore sanitizes every component the directory way, so
    a directory entry and the parents implied by the files inside it always
    produce the *same* name (otherwise an exotic folder name could land as two
    sibling directories).
    """
    # Normalise separators; zip uses forward slashes by spec but be defensive.
    parts = [p for p in member_name.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        logger.warning("zip extract: skipping path-traversal entry {!r}", member_name)
        return None
    if not parts:
        return None
    # Intermediate components are plain path components; a file member's LAST
    # one is a *filename* and goes through :func:`sanitize_filename`, which
    # keeps a leading dot (``.gitignore`` — ``sanitize_component`` strips it
    # via ``.strip(" .")``) and splits the extension.  ``max_bytes`` is passed
    # explicitly: the 250-byte default reserves room for a ``.part`` suffix
    # that only the download path needs, while an extraction target may use
    # the full 255-byte component budget (which the existing byte-limit
    # collision test pins).
    safe_parts = [sanitize_component(p) for p in parts[:-1]]
    safe_parts.append(
        sanitize_component(parts[-1])
        if is_dir
        else sanitize_filename(parts[-1], max_bytes=MAX_COMPONENT_BYTES)
    )
    target = dest_root.joinpath(*safe_parts)
    root = dest_root_resolved if dest_root_resolved is not None else dest_root.resolve()
    try:
        target.resolve().relative_to(root)
    except ValueError:
        logger.warning("zip extract: skipping out-of-root entry {!r}", member_name)
        return None
    return target


def _apply_member_mtime(target: Path, info: zipfile.ZipInfo) -> None:
    """Best-effort: stamp *target* with the ZIP member's stored mtime.

    ``ZipInfo.date_time`` is a 6-tuple ``(Y, M, D, h, m, s)`` in the
    archive's local time with no timezone.  We convert it with
    :func:`time.mktime` (local-time interpretation, matching how most
    archivers write it) and apply it via :func:`os.utime`.  Silently
    ignored on any failure — timestamps are cosmetic and a bad/zeroed
    ``date_time`` (e.g. ``(1980, 0, 0, ...)``) must never abort extraction.
    """
    dt = info.date_time
    if dt[0] < 1980:
        return
    try:
        mtime = time.mktime((*dt, 0, 0, -1))
        os.utime(target, (mtime, mtime))
    except (OSError, OverflowError, ValueError):  # pragma: no cover - defensive
        pass


def extract_zip_to_dir(
    zip_path: Path,
    dest: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    max_total_bytes: int | None = None,
) -> ExtractResult:
    """Extract every member of *zip_path* under *dest* (shared core).

    Member names are decoded via :func:`decode_zip_member_name` (cp932
    mojibake recovery), zip-slip entries are skipped, and name collisions get
    ``_2`` / ``_3`` … suffixes — see the module docstring for the full
    semantics.  Used by the viewer's ZIP drill-in
    (``viewer/zip_drill.py``).

    *should_cancel* is polled between members **and** between chunks inside a
    member (so a cancel during one huge file still takes effect promptly);
    when it returns ``True`` the extraction stops early, the partially
    written file is deleted and the returned result has ``completed=False``
    (the caller decides what to do with the partial output).  *on_progress*
    is called as ``on_progress(done, total)`` after each extracted member,
    where *total* counts non-directory entries.

    *max_total_bytes* caps the **cumulative decompressed** size (zip-bomb
    guard — the archive's own compressed size says nothing about how much it
    inflates to).  Each member additionally charges a fixed
    ``_PER_ENTRY_OVERHEAD_BYTES`` against the cap so an archive of many empty /
    tiny members (a 0-byte zip bomb, which adds no content bytes) still trips
    the guard.  On overflow the partially written file is deleted and
    :class:`ExtractSizeLimitError` is raised.

    Opening the archive raises on failure (``zipfile.BadZipFile`` /
    ``OSError``) so callers can surface the error their own way.  A failure
    that belongs to **one member** — an unsupported compression method, an
    encrypted member in an otherwise plain archive, a CRC mismatch from bit
    rot, a corrupted compressed stream caught mid-read (``zlib.error`` /
    ``lzma.LZMAError`` / a bz2 ``OSError`` / a truncated member's
    ``EOFError``), or a target path the filesystem itself refuses (parent
    ``mkdir`` / file ``open`` raising ``OSError``, e.g. ``ENAMETOOLONG`` with
    long paths disabled) — is counted in ``skipped`` like a zip-slip entry as
    long as some other member did extract, so one bad member cannot cost the
    caller the whole output directory (it throws that away when extraction
    fails).  An ``OSError`` from *writing* the destination file (disk full
    etc.) is not a member fault and is left to propagate and abort the whole
    extraction.  When **nothing** came out and the reason was a member
    decode failure, the exception is raised after all: there is no partial
    result worth keeping, and a caller showing "0 extracted" says much less
    than the error does.

    Returns an :class:`ExtractResult`; entries that could not be extracted
    (zip-slip, unusable names, a parent path component that is a file, an
    undecodable member) are counted in its ``skipped`` — the archive
    extracted fully only when ``completed`` is ``True`` **and** ``skipped``
    is ``0``, and callers presenting the outcome to a user must not show a
    non-zero ``skipped`` as a complete extraction.
    """
    claimed: set[Path] = set()
    # Per-target "where to resume numbering" hints for :func:`_unique_file`
    # — a hint only, never authoritative.
    next_suffix: dict[Path, int] = {}
    written_total = 0
    skipped = 0
    # Loop invariant: resolving it per member is a filesystem round trip each
    # time (see :func:`_safe_member_path`).
    dest_resolved = dest.resolve()
    #: First per-member decode failure, re-raised only if nothing extracts.
    member_error: Exception | None = None

    def _skip(
        what: str,
        member: str,
        exc: Exception | None = None,
        *,
        member_fault: bool = False,
    ) -> None:
        # One skipped entry: log it, count it, and remember the first
        # member-decode failure (*member_fault*) for the nothing-extracted case.
        nonlocal skipped, member_error
        if exc is None:
            logger.warning("zip extract: skipping {} {!r}", what, member)
        else:
            logger.warning("zip extract: skipping {} {!r}: {}", what, member, exc)
        if member_fault and member_error is None:
            member_error = exc
        skipped += 1

    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.infolist()
        # Directory entries first, so an archive that stores empty folders
        # reproduces them (the module docstring's "structure preserving"
        # promise — the per-file ``mkdir`` alone would recreate only folders
        # that happen to contain a file).  They
        # go through the same ``_safe_member_path`` gate so zip-slip is judged
        # identically, and they are NOT counted in ``extracted`` / the
        # progress denominator (the existing progress contract is per file).
        for info in entries:
            if not info.is_dir():
                continue
            if should_cancel is not None and should_cancel():
                return ExtractResult(False, 0, skipped)
            dir_target = _safe_member_path(
                dest,
                decode_zip_member_name(info),
                is_dir=True,
                dest_root_resolved=dest_resolved,
            )
            if dir_target is None:
                skipped += 1
                continue
            # Directory entries are inodes too: charge the same per-entry
            # overhead as a file member so an "inode bomb" made of directory
            # entries alone still trips the cumulative guard (the file loop
            # below charges it after each write).
            written_total += _PER_ENTRY_OVERHEAD_BYTES
            if max_total_bytes is not None and written_total > max_total_bytes:
                raise ExtractSizeLimitError(max_total_bytes)
            try:
                dir_target.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError, OSError):
                # Same policy as the file branch: a parent component that is
                # already a file (or any other FS refusal) skips the entry
                # instead of aborting the whole extraction.
                _skip("unusable directory entry", info.filename)
        infos = [info for info in entries if not info.is_dir()]
        total = len(infos)
        done = 0
        for info in infos:
            if should_cancel is not None and should_cancel():
                return ExtractResult(False, done, skipped)
            name = decode_zip_member_name(info)
            target = _safe_member_path(
                dest, name, dest_root_resolved=dest_resolved
            )
            if target is None:
                skipped += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                # A parent path component already exists as a *file* (e.g. the
                # archive holds both ``a`` and ``a/b``). We can't create a
                # directory over it — skip this entry the same way zip-slip
                # entries are skipped rather than aborting the whole extraction.
                _skip("entry whose parent path is a file", name)
                continue
            except OSError as exc:
                # E.g. ENAMETOOLONG / Windows WinError 206 when the target
                # path exceeds MAX_PATH and long paths are disabled (the
                # Windows default). Same policy as an unusable *name* — skip
                # this entry rather than aborting the whole extraction.
                _skip("entry with unusable target path", name, exc)
                continue
            target = _unique_file(target, claimed, next_suffix)
            claimed.add(target)
            try:
                src = zf.open(info)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                # The member's local header alone already rules it out: an
                # unsupported compression method (Deflate64 / zstd / PPMd) or
                # an encrypted member inside an otherwise plain archive. Skip
                # it the way an unusable *name* is skipped rather than
                # aborting — the caller throws the whole temp directory away
                # on failure, so letting this escape would delete every
                # member that did extract. Kept so the end of the loop can
                # still raise when nothing extracted at all.
                _skip("undecodable member", name, exc, member_fault=True)
                continue
            try:
                dst = target.open("wb")
            except OSError as exc:
                src.close()
                _skip("entry with unusable target path", name, exc)
                continue
            cancelled = overflow = read_failed = False
            try:
                with src, dst:
                    while True:
                        if should_cancel is not None and should_cancel():
                            cancelled = True
                            break
                        try:
                            chunk = src.read(1024 * 1024)
                        except (
                            zipfile.BadZipFile,
                            RuntimeError,
                            NotImplementedError,
                            zlib.error,
                            EOFError,
                            lzma.LZMAError,
                            OSError,
                        ) as exc:
                            # The stream itself is broken past what the local
                            # header revealed: a corrupted deflate/LZMA
                            # stream, a bz2 member raising ``OSError``
                            # ("Invalid data stream"), or a truncated member
                            # (``EOFError``) — bit rot / a bad transfer, not a
                            # format the header could rule out up front. Same
                            # skip-and-continue policy as the header-time
                            # failures above.
                            _skip(
                                "undecodable member", name, exc, member_fault=True
                            )
                            read_failed = True
                            break
                        if not chunk:
                            break
                        written_total += len(chunk)
                        if (
                            max_total_bytes is not None
                            and written_total > max_total_bytes
                        ):
                            overflow = True
                            break
                        dst.write(chunk)
            except ExtractSizeLimitError:
                # Our own guard (a RuntimeError subclass) — never a member fault.
                raise
            if read_failed:
                # Already counted by ``_skip``. The name stays claimed: a
                # later member must not land on it.
                target.unlink(missing_ok=True)
                continue
            if not cancelled and not overflow:
                # Charge the per-entry overhead so an archive of many tiny /
                # empty members (0-byte zip bomb) still trips the cumulative
                # guard, which content bytes alone would never do.
                written_total += _PER_ENTRY_OVERHEAD_BYTES
                if max_total_bytes is not None and written_total > max_total_bytes:
                    overflow = True
            if cancelled or overflow:
                # Never leave a half-written file behind.
                target.unlink(missing_ok=True)
                if overflow:
                    # overflow is only ever set under the not-None guards above
                    assert max_total_bytes is not None
                    raise ExtractSizeLimitError(max_total_bytes)
                return ExtractResult(False, done, skipped)
            _apply_member_mtime(target, info)
            done += 1
            if on_progress is not None:
                on_progress(done, total)
        if done == 0 and member_error is not None:
            raise member_error
    return ExtractResult(True, done, skipped)
