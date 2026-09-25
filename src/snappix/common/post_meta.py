"""Shared ``post.md`` format contract (external writers ⇄ viewer).

``post.md`` is written by *external* tools and consumed read-only by
the viewer (this product ships no writer of its own), so the format
contract lives here in ``common/`` as the single compatibility definition:

- writer side: external tools (out of this repository) that follow the
  public spec in [docs/formats/post-md.md]
- parser: ``viewer/post_md.py`` (tolerant inverse parse)

This module holds only the pieces the parser must agree on with any writer —
no write operation lives here (a writer builds its own on top of these):

- the meta-line key names (``- post_id:`` … ``- downloaded_at:``) and their
  recommended order,
- :func:`head_meta`, the boundary-honouring reader that extracts the leading
  meta block of an existing file back into a ``{key: value}`` dict,
- the *leading meta block* boundary rule itself (:func:`scan_head` /
  :func:`scan_head_lines` returning a :class:`HeadScan` — **the** single
  implementation, used by :func:`head_meta`, the viewer's
  ``post_md.parse_post_md`` / ``post_md.read_post_ref``, and any in-place
  patcher that must agree with them on where the head ends),
- the bounded head read (:func:`read_head`), and
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
# Meta-line key names (recommended output order — docs/formats/post-md.md §3)
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

#: The ``# {title}`` H1 heading. ``re.MULTILINE`` so whole-file
#: ``search``/``subn`` works; per-line ``match`` (viewer) works too.
#:
#: The whitespace classes are ``[^\S\n]`` (horizontal whitespace) rather than
#: ``\s`` **on purpose**: ``\s`` matches newlines, so under ``re.MULTILINE``
#: the trailing ``\s*`` backtracks past the heading's own line break and eats
#: the blank line that separates the H1 from the meta block.  Per-line
#: ``match`` callers never notice, but a whole-file ``subn`` that rewrites the
#: heading would replace that range wholesale and silently drop the blank
#: line, changing the file's bytes beyond the heading itself.
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
# Value-level helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Leading-meta-block boundary rule
# ---------------------------------------------------------------------------


def split_bom(text: str) -> tuple[str, str]:
    """Split a leading UTF-8 BOM off *text*, returning ``(bom, rest)``.

    ``post.md`` is a public format written by external tools
    (docs/formats/post-md.md), and PowerShell 5.1 / several editors write a
    BOM.  A BOM left on line 1 matches neither :data:`TITLE_LINE_RE` nor
    :data:`META_LINE_RE`, which would make :func:`scan_head_lines` stop on
    the very first line and silently demote the whole head to body — the
    viewer parser already guards against exactly this
    (``viewer/post_md.py::parse_post_md`` — ``text.lstrip("\\ufeff")``),
    and this module claims the *same* boundary rule, so it must strip it too.

    The BOM is returned rather than dropped so a byte-preserving writer can
    put it back and keep the file's bytes otherwise unchanged.
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
    §2), shared by the reader-side :func:`head_meta`, the viewer's
    ``post_md.parse_post_md`` / ``post_md.read_post_ref``, and any in-place
    patcher that must agree with them.  Hand-written copies of the same
    rule drift apart
    (Unicode line separators / a lone CR end up split differently), so a change
    to the rule would have to be made in several places at once.

    The scan tolerates a single leading ``# title`` H1 (matched by
    :data:`TITLE_LINE_RE`) and blank lines above/around the block, matches
    meta lines with the strict ``[a-z_]+`` key charset (:data:`META_LINE_RE`),
    and stops at the first body line — so a body-level markdown list item
    (``- 補足: …``, or an English-keyed ``- plan: …`` look-alike deep in the
    body) or a deeper ``## sub`` heading is never mistaken for part of the
    meta block.

    *lines* must already have the BOM split off (:func:`split_bom`) — a
    leading UTF-8 BOM would match neither regex and stop the scan on line 1.
    Each line is ``rstrip``-ed before matching, so CRLF files
    never leak a trailing ``\\r`` into values.  Readers split with
    :func:`scan_head` (``str.splitlines``); a byte-preserving writer splits
    on ``"\\n"`` itself (so a rejoin keeps CRLF / U+2028 verbatim) and calls
    this directly.
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
    alike.  Strip the BOM first (:func:`split_bom`) when the text
    may carry one.
    """
    return scan_head_lines(text.splitlines())


def head_meta(text: str) -> dict[str, str]:
    """Read the leading meta block of *text* back as ``{key: value}``.

    The dict-shaped reader of the leading meta block, sharing the boundary
    rule via ``scan_head`` — a ``- plan:`` / ``- posted_at:``
    look-alike line in the *body* is never picked up (per-key whole-file
    ``re.search`` regexes would misread exactly the case "the head lacks
    the key, the body has a look-alike").

    Values are whitespace-stripped (CRLF-safe — the line scan ``rstrip``\\ s
    the ``\\r`` before capturing).  A malformed head that repeats a key keeps
    the **last** occurrence, matching the viewer parser's judgement so
    a reader and the parser can't disagree.  Absent keys are simply absent —
    callers use ``.get(KEY_…)``.

    A leading UTF-8 BOM is stripped before the scan (:func:`split_bom`), so
    a BOM-carrying ``post.md`` reads back the same dict as a plain one — the
    consumers may read the file with plain ``utf-8``, so the BOM would
    otherwise reach line 1 and empty the whole result.
    """
    meta: dict[str, str] = {}
    _bom, scan_text = split_bom(text)
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
    consume the leading head (:func:`head_meta` / :func:`scan_head`).  An
    unbounded read is a real cost on a library: a library-wide walk touches
    every ``post.md`` in the tree, and post bodies can carry tens of KiB of prose
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
