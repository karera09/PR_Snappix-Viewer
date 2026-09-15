"""Shared ``post.md`` format contract (external writers ⇄ viewer).

``post.md`` is written by *external* tools and consumed read-only by
the viewer (this product ships no writer — see the 切り出し元との関係 notes in
CLAUDE.md), so the format contract lives here in ``common/`` as the single
compatibility definition:

- writer side: external tools (out of this repository) that follow the
  public spec in [docs/formats/post-md.md]
- parser: ``viewer/post_md.py`` (tolerant inverse parse)

This module centralises the pieces the parser must agree on with any writer:

- the meta-line key names (``- post_id:`` … ``- downloaded_at:``),
- :func:`head_meta`, the boundary-honouring reader that extracts the leading
  meta block of an existing file back into a ``{key: value}`` dict,
- the *leading meta block* boundary rule itself (:func:`scan_head` /
  :func:`scan_head_lines` returning a :class:`HeadScan` — **the** single
  implementation, used by the writer-side :func:`patch_meta_lines`, the
  reader-side :func:`head_meta`, and the viewer's ``post_md.parse_post_md`` /
  ``post_md.read_post_ref``), and
- the markdown link filename encoding (``urllib.parse.quote(..., safe="")``)
  and its inverse.

Changing anything here changes the on-disk compatibility contract — keep the
public spec [docs/formats/post-md.md] in sync.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote as _url_quote
from urllib.parse import unquote as _url_unquote

# ---------------------------------------------------------------------------
# Meta-line key names (fixed writer output order — see post_writer.py)
# ---------------------------------------------------------------------------

KEY_POST_ID = "post_id"
KEY_URL = "url"
KEY_CREATOR = "creator"
KEY_SERVICE = "service"
KEY_POSTED_AT = "posted_at"
KEY_PLAN = "plan"
KEY_PLAN_PRICE = "plan_price"
KEY_TAGS = "tags"
KEY_LOCKED_CONTENTS = "locked_contents"
KEY_FAVORITES = "favorites"
KEY_DOWNLOADED_AT = "downloaded_at"

#: The writer emits the leading meta block in exactly this order.
META_KEYS_IN_ORDER = (
    KEY_POST_ID,
    KEY_URL,
    KEY_CREATOR,
    KEY_SERVICE,
    KEY_POSTED_AT,
    KEY_PLAN,
    KEY_PLAN_PRICE,
    KEY_TAGS,
    KEY_LOCKED_CONTENTS,
    KEY_FAVORITES,
    KEY_DOWNLOADED_AT,
)

# ---------------------------------------------------------------------------
# Structural regexes (title / meta block boundary / image refs)
# ---------------------------------------------------------------------------

#: The ``# {title}`` H1 heading. ``re.MULTILINE`` so the writer can
#: ``search``/``subn`` whole-file text; per-line ``match`` (viewer) works too.
#:
#: The whitespace classes are ``[^\S\n]`` (horizontal whitespace) rather than
#: ``\s`` **on purpose**: ``\s`` matches newlines, so under ``re.MULTILINE``
#: the trailing ``\s*`` backtracks past the heading's own line break and eats
#: the blank line that separates the H1 from the meta block.  Per-line
#: ``match`` callers never noticed, but ``patch_title_heading``'s whole-file
#: ``subn`` replaced that range wholesale and silently dropped the blank line —
#: which then made every later ``write_post_text`` see a content difference and
#: spawn an ``old/`` archive copy for an unchanged post (項目#32).
TITLE_LINE_RE = re.compile(r"^#[^\S\n]+(.+?)[^\S\n]*$", re.MULTILINE)

#: Matches any ``- key:`` meta line (key only) for callers that need the key
#: without the value.  Key charset is deliberately the same strict ``[a-z_]+``
#: as :data:`META_LINE_RE` (the writer only emits such keys) so a body list
#: item like ``- 補足: …`` is never mistaken for a meta line — matching the
#: viewer parser's boundary judgement (``viewer/post_md.py``).  The head scan
#: itself (``scan_head_lines``) uses :data:`META_LINE_RE` to capture the value
#: in the same pass.
META_KEY_RE = re.compile(r"^-\s+([a-z_]+):")

#: Viewer-side meta line matcher capturing ``(key, value)``. Key charset is
#: intentionally strict (``[a-z_]+`` — the writer only emits such keys) so
#: body list items are less likely to be mistaken for meta lines.
META_LINE_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*)$")

#: Match ``![alt](./<path>)`` — only the relative-path form the writing tool
#: produces; absolute / external URLs are not post-local images.
IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(\.\/([^)]+)\)")

# ---------------------------------------------------------------------------
# Reading an existing post.md back (head_meta + value-level helpers)
# ---------------------------------------------------------------------------

#: Removal variant for :func:`strip_downloaded_at` — consumes the trailing
#: newline too, so stripping the line never leaves a blank line behind.
#: A deliberate whole-file regex (unlike :func:`head_meta`): the strip is a
#: byte-compare normalisation, so a stray body line must be removed too or the
#: comparison stays unequal for a reason the caller can't see.
_DOWNLOADED_AT_STRIP_RE = re.compile(r"^- downloaded_at:.*\n?", re.MULTILINE)

#: Extracts the creator id from a ``- creator:`` meta *value* ("Name (id)").
#: The value-level twin the viewer's head scanner uses lives in
#: ``viewer/post_md.py``; writers/readers that already went through
#: :func:`head_meta` use :func:`creator_id_from_value` on the value instead of
#: a whole-line regex.
CREATOR_ID_IN_VALUE_RE = re.compile(r"\(([^)]*)\)\s*$")


def creator_id_from_value(value: str) -> str:
    """Return the creator id from a ``- creator:`` value ("Name (id)" → "id").

    Returns ``""`` when the value carries no trailing ``(id)`` group (older
    files, hand-edited values).
    """
    m = CREATOR_ID_IN_VALUE_RE.search(value)
    return m.group(1).strip() if m else ""

# ---------------------------------------------------------------------------
# Markdown link filename encoding
# ---------------------------------------------------------------------------


def encode_md_ref(filename: str) -> str:
    """Percent-encode a filename for use inside a markdown link target.

    ``safe=""`` so spaces / ``#`` / CJK never break the link. The viewer
    reverses this with :func:`decode_md_ref`.
    """
    return "./" + _url_quote(filename, safe="")


def decode_md_ref(encoded: str) -> str:
    """Inverse of :func:`encode_md_ref` (without the ``./`` prefix)."""
    return _url_unquote(encoded)


#: Fallback H1 title when a post has no usable title (empty / whitespace-only).
#: A bare ``# `` heading fails :data:`TITLE_LINE_RE`, which pushes the whole
#: meta block into the body (2026-08 review 項目8).
UNTITLED_HEADING = "(無題)"


def heading_title(raw: str | None) -> str:
    """Normalise a post title into a safe single-line H1 heading value.

    The parser (:data:`TITLE_LINE_RE`) requires the ``# `` heading to sit on
    one line with at least one non-whitespace character; otherwise the
    leading-meta-block boundary rule folds the entire meta block into the body
    and every meta reader comes back empty.  Titles from the wild break this
    in two ways: they can be empty / whitespace-only (a Patreon post may have
    no title at all), or they can contain embedded line breaks that split
    ``# {title}`` across lines and orphan the meta block.  Collapse any line
    break to a space and fall back to :data:`UNTITLED_HEADING` when nothing
    printable survives.

    The title twin of :func:`meta_value`, and here for the same reason: it is
    part of the format contract that writer and parser share, not a private
    nicety of whichever writer happens to exist.
    """
    if not raw:
        return UNTITLED_HEADING
    # ``splitlines`` covers every Unicode line break (LF / CR / VT / FF /
    # U+2028 and friends), each of which would break the single-line heading.
    collapsed = " ".join(raw.splitlines()).strip()
    return collapsed or UNTITLED_HEADING


def meta_value(raw: str | None) -> str:
    """Normalise a string into a safe single-line meta value.

    The leading meta block is contiguous ``- key: value`` lines by contract
    (docs/formats/post-md.md §2): a value carrying an embedded line break
    splits its line in two, so every following meta line is read as body by
    the parser — and a crafted value like ``"Evil\\n- post_id: 999"`` would
    even inject a meta line the parser then trusts.  Any writer-side line
    assembly must therefore collapse line breaks to a space (the parser
    strips surrounding whitespace anyway; ``splitlines`` covers every Unicode
    line break).  Lives here in ``common/`` because it is part of the format
    contract, not a private writer nicety — :func:`patch_meta_lines` applies
    it itself, and external writers building full files should call it on
    every value (2026-08 review 項目70; an in-process writer plugin may
    re-export it).  Idempotent, so double application is harmless.
    """
    if not raw:
        return ""
    return " ".join(raw.splitlines()).strip()


# ---------------------------------------------------------------------------
# Leading-meta-block boundary rule
# ---------------------------------------------------------------------------


def _split_bom(text: str) -> tuple[str, str]:
    """Split a leading UTF-8 BOM off *text*, returning ``(bom, rest)``.

    ``post.md`` is a public format written by external tools
    (docs/formats/post-md.md), and PowerShell 5.1 / several editors write a
    BOM.  A BOM left on line 1 matches neither :data:`TITLE_LINE_RE` nor
    :data:`META_LINE_RE`, which would make :func:`scan_head_lines` stop on
    the very first line and silently demote the whole head to body — the
    viewer parser already guards against exactly this
    (``viewer/post_md.py::parse_post_md`` — ``text.lstrip("\\ufeff")``, #24),
    and this module claims the *same* boundary rule, so it must strip it too
    (レビュー 2026-09-03 項目 #34).

    The BOM is returned rather than dropped so writer-side callers
    (:func:`patch_meta_lines`) can put it back and keep the file's bytes
    otherwise unchanged.
    """
    rest = text.lstrip("﻿")
    return text[: len(text) - len(rest)], rest


class HeadScan(NamedTuple):
    """What the leading-meta-block boundary rule found in one file.

    ``title`` is the leading H1's text (``""`` when the file has none),
    ``meta`` is ``(line index, key, raw value)`` per meta line in file order
    (a repeated key appears once per occurrence — consumers that want a dict
    keep the **last**, matching the viewer parser), ``body_start`` is the
    index of the first body line (``len(lines)`` when the file is all head),
    ``lines`` is the line list the indices refer to, and ``title_index`` is
    the index of the line ``title`` came from (``None`` when there is no
    leading H1) — writers that patch the heading in place need the position,
    not just the text, so they never reach a ``# …`` line in the body.
    """

    title: str
    meta: list[tuple[int, str, str]]
    body_start: int
    lines: list[str]
    title_index: int | None = None


def scan_head_lines(lines: list[str]) -> HeadScan:
    """Apply the leading-meta-block boundary rule to an already-split file.

    **The single implementation of the boundary rule** (docs/formats/post-md.md
    §2), shared by the writer-side :func:`patch_meta_lines`, the reader-side
    :func:`head_meta`, and the viewer's ``post_md.parse_post_md`` /
    ``post_md.read_post_ref``.  Before レビュー 2026-09-03 項目 #111 the same
    rule was hand-written three times and the copies had already drifted
    (Unicode line separators / a lone CR were split differently), so a change
    to the rule had to be made in three places at once.

    The scan tolerates a single leading ``# title`` H1 (matched by
    :data:`TITLE_LINE_RE`) and blank lines above/around the block, matches
    meta lines with the strict ``[a-z_]+`` key charset (:data:`META_LINE_RE`),
    and stops at the first body line — so a body-level markdown list item
    (``- 補足: …``, or an English-keyed ``- plan: …`` look-alike deep in the
    body) or a deeper ``## sub`` heading is never mistaken for part of the
    meta block.

    *lines* must already have the BOM split off (:func:`_split_bom`) — a
    leading UTF-8 BOM would match neither regex and stop the scan on line 1
    (項目 #34).  Each line is ``rstrip``-ed before matching, so CRLF files
    never leak a trailing ``\\r`` into values.  Readers split with
    :func:`scan_head` (``str.splitlines``); the byte-preserving writer splits
    on ``"\\n"`` itself and calls this directly — see :func:`patch_meta_lines`.
    """
    title = ""
    title_index: int | None = None
    meta: list[tuple[int, str, str]] = []
    body_start = len(lines)
    seen_title = False
    seen_meta = False
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if not seen_title and not seen_meta:
            m = TITLE_LINE_RE.match(stripped)
            if m:
                # Only the leading ``# title`` H1 is taken; a second title or
                # a deeper ``## sub`` heading is body.
                title = m.group(1).strip()
                title_index = i
                seen_title = True
                body_start = i + 1
                continue
        m = META_LINE_RE.match(stripped)
        if m:
            seen_meta = True
            meta.append((i, m.group(1), m.group(2)))
            body_start = i + 1
            continue
        if not stripped:
            if seen_meta:
                # Blank line after the meta block ends it — body follows.
                body_start = i + 1
                break
            # Blank line above the block (or between title and block) — head.
            body_start = i + 1
            continue
        # First non-blank body line (list item, image ref, prose): the meta
        # block is done, so a list item deeper in the file is never mistaken
        # for a meta line.
        body_start = i
        break
    return HeadScan(title, meta, body_start, lines, title_index)


def scan_head(text: str) -> HeadScan:
    """:func:`scan_head_lines` over *text*, split with ``str.splitlines``.

    The reader-side entry point.  ``splitlines`` — not ``split("\\n")`` — is
    what every reader of this format uses, so a lone ``\\r`` or a Unicode line
    separator (U+2028 / U+2029 / U+0085 / VT / FF) ends a line for all of them
    alike (項目 #111).  Strip the BOM first (:func:`_split_bom`) when the text
    may carry one.
    """
    return scan_head_lines(text.splitlines())


def head_meta(text: str) -> dict[str, str]:
    """Read the leading meta block of *text* back as ``{key: value}``.

    The reader-side counterpart of :func:`patch_meta_lines`, sharing its
    boundary rule via ``scan_head`` — a ``- plan:`` / ``- posted_at:``
    look-alike line in the *body* is never picked up, unlike the whole-file
    ``re.search`` per-key regexes this replaces (2026-08 review 項目67, whose
    failure mode was "the head lacks the key, the body has a look-alike").

    Values are whitespace-stripped (CRLF-safe — the line scan ``rstrip``\\ s
    the ``\\r`` before capturing).  A malformed head that repeats a key keeps
    the **last** occurrence, matching the viewer parser's judgement (#107) so
    a reader and the parser can't disagree.  Absent keys are simply absent —
    callers use ``.get(KEY_…)``.

    A leading UTF-8 BOM is stripped before the scan (:func:`_split_bom`), so
    a BOM-carrying ``post.md`` reads back the same dict as a plain one — the
    consumers read the file with plain ``utf-8`` (the viewer and any
    in-process writer plugin alike), so the BOM would
    otherwise reach line 1 and empty the whole result (項目 #34).
    """
    meta: dict[str, str] = {}
    _bom, scan_text = _split_bom(text)
    for _i, key, value in scan_head(scan_text).meta:
        meta[key] = value.strip()
    return meta


#: Upper bound (characters) read by :func:`read_head`.  The leading title +
#: meta block is a few hundred bytes at most, and the public spec
#: [docs/formats/post-md.md] §8 declares the first 64 KiB as the head every
#: reader is guaranteed to see — so no consumer of the head has any reason to
#: pull a multi-megabyte body off a NAS.  ``viewer/post_md.py`` reads the same
#: bound for its own head scanners.
HEAD_READ_LIMIT = 64 * 1024


def read_head(path: Path, limit: int = HEAD_READ_LIMIT) -> str:
    """Read at most *limit* **bytes** from the start of a ``post.md`` and decode.

    The bounded counterpart of ``path.read_text()`` for callers that only
    consume the leading head (:func:`head_meta` / :func:`head_title`).  An
    unbounded read is a real cost on a library: a bulk rename walks every
    ``post.md`` in the tree, and post bodies can carry tens of KiB of prose
    each — over a network share that is the difference between one read of a
    few hundred bytes and pulling the whole library through the wire.

    ``utf-8-sig`` so a BOM-writing external tool's file still starts at the
    title line, ``errors="replace"`` so one bad byte truncates nothing (the
    head scan tolerates a replacement character in a value).  ``OSError``
    propagates — callers already decide what an unreadable file means.

    The file is opened in **binary** mode and the slice decoded here, because
    ``TextIOWrapper.read(n)`` counts *characters*: on an all-CJK body (three
    bytes each) a "64 KiB" text read pulls 192 KiB off the share — three times
    the cap this constant (and ``docs/formats/post-md.md``) promises.  A
    multi-byte character split by the cut becomes a single U+FFFD, which can
    only ever land far past the head block.
    """
    with path.open("rb") as fh:
        return fh.read(limit).decode("utf-8-sig", errors="replace")


def head_title(text: str) -> str | None:
    """Return the *leading* H1 heading's text, or ``None`` when there is none.

    The title twin of :func:`head_meta`, sharing the same boundary rule via
    ``scan_head``.  A whole-file ``TITLE_LINE_RE.search`` would happily pick
    up a ``# 見出し`` line deep in the *body* of a post that has no heading of
    its own — exactly the "the head lacks the key, the body has a look-alike"
    failure the meta side retired in 2026-08 review 項目67.  The viewer parser
    (``post_md.parse_post_md``) has always taken only the leading H1, so any
    writer-side reader must agree or the two tools disagree about a post's
    title.

    A leading UTF-8 BOM is stripped first (:func:`_split_bom`), same as
    :func:`head_meta`.  A blank heading (``# ``) reads as no title.
    """
    _bom, scan_text = _split_bom(text)
    return scan_head(scan_text).title or None


def patch_head_title(text: str, new_title: str) -> str:
    """Return *text* with its leading H1 replaced by ``# {new_title}``.

    The write-side twin of :func:`head_title`: only the heading line the
    boundary rule identified as the leading H1 is rewritten, so a post whose
    body happens to contain a ``# …`` line is left alone instead of having
    that body line silently retitled.  *text* is returned unchanged when the
    file has no leading H1 (the caller decides whether that is worth a write).

    Byte-preserving in the same way as :func:`patch_meta_lines`: the split is
    ``split("\\n")`` (never ``splitlines``) so a CRLF file keeps its ``\\r``
    terminators, and *new_title* must already be normalised by the caller
    (an embedded newline would split the heading across lines).
    """
    bom, body = _split_bom(text)
    # See patch_meta_lines: ``split("\n")`` so untouched lines survive
    # byte-for-byte (``splitlines`` would rewrite \r / U+2028 / … as \n).
    lines = body.split("\n")
    idx = scan_head_lines(lines).title_index
    if idx is None:
        return text
    eol_suffix = "\r" if lines[idx].endswith("\r") else ""
    lines[idx] = f"# {new_title}{eol_suffix}"
    return bom + "\n".join(lines)


def patch_meta_lines(text: str, updates: dict[str, str]) -> str:
    """Return *text* with the given ``- key: value`` meta lines refreshed.

    Used to refresh selected meta values (favorites / plan / price) on an
    existing ``post.md`` **without** rewriting the body — the lock-regression
    path needs to keep the previously-downloaded body intact while still
    surfacing the latest post-level metadata.

    For each ``key`` in *updates*: if a ``- key:`` line already exists, its
    value is replaced in place (no line reordering — and in a malformed head
    that repeats the key, *every* occurrence is refreshed so a first-match
    reader and a last-match reader can't disagree); if it's absent, the line
    is inserted right after the last existing meta line (so older ``post.md``
    files that predate a meta field still gain it).  Values are passed through
    :func:`meta_value` (line breaks collapsed to a space — 項目70), so a value
    from an unconstrained source can never split its line and push the
    following meta lines into the body.  Rewritten and inserted
    lines keep the file's dominant EOL (LF or CRLF — both are allowed by the
    spec), so a CRLF file never gains mixed line endings.  The title, body,
    and any meta line not named in *updates* (notably ``- locked_contents:``,
    which the lock-regression guard must keep at its lower recorded value) are
    left byte-for-byte unchanged.

    Only the *leading* meta block is considered, via ``scan_head_lines`` —
    the **same boundary rule** the viewer parser
    (``viewer/post_md.py::parse_post_md``) and the reader-side
    :func:`head_meta` use.  All sides therefore agree on where the head ends,
    so a body-level markdown list item (``- 補足: …``) or a deeper ``## sub``
    heading is never mistaken for part of the meta block — a missing key is
    inserted after the leading block, never into the body, and the refreshed
    value is exactly what the parser reads back.
    """
    if not updates:
        return text
    # A leading UTF-8 BOM would match neither the title nor the meta regex,
    # so the scan must not see it; it is put back verbatim on the way out so
    # the file keeps its bytes (項目 #34 — same rule as :func:`head_meta` and
    # the viewer parser).
    bom, text = _split_bom(text)
    # NOTE: ``split("\n")``, deliberately — NOT the readers' ``splitlines``.
    # This function rebuilds the file with ``"\n".join`` and promises the
    # untouched lines survive byte-for-byte; ``splitlines`` also breaks on
    # ``\r`` / U+2028 / U+2029 / U+0085 / VT / FF, so rejoining would rewrite
    # every one of them as ``\n`` — turning a CRLF ``post.md`` wholesale into
    # LF, which is exactly the "the content changed" false positive the EOL
    # handling below exists to avoid.  The *boundary rule* is still the shared
    # one (:func:`scan_head_lines`); only the split differs, and only here
    # (レビュー 2026-09-03 項目 #111).
    lines = text.split("\n")
    # Split/join on ``\n`` keeps each CRLF line's trailing ``\r`` inside the
    # line, so a replaced / inserted line must carry the same terminator or the
    # file ends up with mixed EOLs (the spec allows CRLF —
    # [docs/formats/post-md.md] §1).  Mixed EOLs make an otherwise-unchanged
    # file compare unequal byte-wise for external writers' "did the content
    # change?" checks, provoking needless archival.
    crlf = text.count("\r\n")
    eol_suffix = "\r" if crlf > text.count("\n") - crlf else ""
    #: key → **every** line index it occurs at.  A malformed head that repeats
    #: a key is rewritten at all of them, because a reader that stops at the
    #: first occurrence and the parser (which keeps the last —
    #: ``viewer/post_md.py`` / :func:`head_meta`) would otherwise disagree.
    #: Writing only the first would leave the parser reading the stale later
    #: line, breaking this function's "the refreshed value is exactly what the
    #: parser reads back" guarantee (#107).
    key_to_idx: dict[str, list[int]] = {}
    last_meta = -1
    for i, key, _value in scan_head_lines(lines).meta:
        last_meta = i
        key_to_idx.setdefault(key, []).append(i)
    to_insert: list[str] = []
    for key, value in updates.items():
        # meta_value: a value with an embedded newline would split its line in
        # two, pushing every following meta line into the body (and letting a
        # crafted value inject meta lines) — 項目70.
        new_line = f"- {key}: {meta_value(value)}{eol_suffix}"
        idxs = key_to_idx.get(key)
        if idxs:
            for idx in idxs:
                lines[idx] = new_line
        else:
            to_insert.append(new_line)
    # Insert any missing keys as a block after the last meta line. When the
    # file has no meta block at all (unexpected), skip insertion rather than
    # risk corrupting the body.
    if to_insert and last_meta >= 0:
        lines[last_meta + 1:last_meta + 1] = to_insert
    return bom + "\n".join(lines)


def strip_downloaded_at(text: str) -> str:
    """Return *text* with the ``- downloaded_at:`` line removed.

    The download timestamp changes on every run, so it must be excluded when
    comparing an old vs new ``post.md`` to decide whether the content really
    changed (and thus whether the old copy is worth archiving).

    The whole line *including its newline* is removed — leaving an empty
    line would make an otherwise-identical file written by an old snappix
    version (no ``- downloaded_at:`` line at all) compare unequal and
    trigger one spurious ``old/<date>`` archival.
    """
    return _DOWNLOADED_AT_STRIP_RE.sub("", text)


def render_head(title: str, meta: dict[str, str]) -> list[str]:
    """Build the leading ``# title`` + meta block of a ``post.md``, as lines.

    The format contract's order (docs/formats/post-md.md §2) is
    :data:`META_KEYS_IN_ORDER`, and every value goes through
    :func:`meta_value` — a value carrying an embedded line break splits its
    line in two and pushes every following meta line into the body.  Both
    rules live here rather than in a writer, so a second writer cannot ship
    its own order or forget the normalisation.

    *meta* may omit keys; an absent key still gets its line, with an empty
    value (the parser reads a missing line and an empty one the same way, and
    a fixed shape keeps ``patch_meta_lines`` able to refresh any key in place
    later).  Unknown keys are ignored — the block is the declared set.

    The returned list ends with a blank line, so ``"\n".join(lines)`` can be
    followed directly by the body.
    """
    lines = [f"# {heading_title(title)}", ""]
    lines += [
        f"- {key}: {meta_value(meta.get(key, ''))}"
        for key in META_KEYS_IN_ORDER
    ]
    lines.append("")
    return lines
