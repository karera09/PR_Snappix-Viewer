"""Pure-Python parser for the ``post.md`` files written by external tools.

GUI-free so the viewer can run unit tests without any Qt dependency.

The format is the inverse of the writing tool's output; the shared contract —
meta key names, line regexes, the leading-meta-block boundary rule and the
markdown-link filename encoding — lives in :mod:`snappix.common.post_meta`
(the only non-viewer module the viewer may import besides other ``common``
code):

    # {title}

    - post_id: {post_id}
    - url: {url}
    - creator: {creator_name} ({creator_id})
    - service: {service_id}
    - posted_at: {iso_datetime}
    - plan: {plan_name}
    - tags: {comma,separated}
    - locked_contents: {N}
    - favorites: {N}
    - downloaded_at: {iso_datetime}

    ![](./{url_encoded_filename})
    ...body content...

Image references use ``urllib.parse.quote(..., safe="")`` so we reverse with
:func:`snappix.common.post_meta.decode_md_ref` when extracting filenames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path, PureWindowsPath

from ..common.post_meta import (
    HEAD_READ_LIMIT,
    IMG_REF_RE,
    KEY_CREATOR,
    KEY_FAVORITES,
    KEY_LOCKED_CONTENTS,
    KEY_PLAN,
    KEY_PLAN_PRICE,
    KEY_POSTED_AT,
    KEY_POST_ID,
    KEY_SERVICE,
    KEY_TAGS,
    creator_id_from_value,
    decode_md_ref,
    read_head,
    scan_head,
)

# Match `![alt](./<path>)` — we only consume the relative-path form the
# writing tools produce; absolute / external URLs are ignored as thumbnails.
_IMG_RE = IMG_REF_RE

#: Upper bound (bytes) read by the head scanners below.  The title + meta
#: block is a few hundred bytes at most, so 64 KiB is plenty — and iterating
#: line-by-line would read the whole file when it contains no newlines.
#:
#: The scanners open the file in **binary** mode and decode the slice
#: themselves, because ``TextIOWrapper.read(n)`` counts *characters*: on an
#: all-CJK body that is three bytes each, a "64 KiB" head read pulled 192 KiB
#: off the share — three times the cap this constant (and
#: ``docs/formats/post-md.md``) promises.  ``errors="replace"`` makes a
#: multi-byte character split by the cut a single U+FFFD, which can only ever
#: land far past the head block.  正本は :data:`~snappix.common.post_meta.
#: HEAD_READ_LIMIT`（書式契約の共有定義）— ここに private な別名を置くと、
#: 同じ値を欲しがる別モジュールがモジュール跨ぎで private 名を import する
#: 形になるので、別名は持たない。


def _read_head_bytes(md_path) -> str:
    """Read at most :data:`~snappix.common.post_meta.HEAD_READ_LIMIT`
    **bytes** and decode them.

    "utf-8-sig": tolerate a UTF-8 BOM (PowerShell 5.1 / BOM-writing editors)
    — with a BOM left in place the first line matches neither the title nor
    the meta regex and the whole head reads as body.  Raises whatever the
    open / read raises; each caller has its own ``OSError`` policy.
    """
    return read_head(md_path, HEAD_READ_LIMIT)


@dataclass
class ParsedPost:
    title: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    locked_count: int = 0
    posted_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    #: Post favorite / like count from the ``- favorites:`` meta line.
    #: ``None`` when the line is absent (older post.md) or non-numeric.
    favorites: int | None = None
    #: Plan / tier name from the ``- plan:`` meta line (``""`` when absent).
    plan_name: str = ""
    #: Plan price from the ``- plan_price:`` meta line, kept verbatim with its
    #: currency symbol (e.g. ``¥500`` / ``$5``).  ``""`` when absent.
    plan_price: str = ""
    body: str = ""

    @cached_property
    def thumbnail(self) -> str | None:
        """First body image ref's decoded filename (e.g. ``"絵.jpg"``), lazily.

        製品コードにこのヒントの消費者は居ない（サムネイル解決は常に
        ``folder_scan.find_first_image`` — apply_metadata / apply_preview の
        docstring 参照）。``parse_post_md`` はキャッシュ構築の最重量経路で全文
        （64 KiB 上限なし）を受け取るため、毎回本文全体へ正規表現を掛けると
        実測で parse 時間の約 25% が誰も読まない抽出に消える — 初アクセス時に
        1 回だけ評価する
        （``cached_property``。dataclass のフィールドではないので等価比較や
        repr には関与しない）。
        """
        return extract_thumbnail_candidate(self.body)


def _decode_ref(encoded: str) -> str:
    return decode_md_ref(encoded)


def extract_thumbnail_candidate(text: str) -> str | None:
    """Return the first ``![](./...)`` filename (URL-decoded), or None.

    戻り値は**単一のファイル名コンポーネント**に正規化する:
    復号結果が区切り文字（``/`` / ``\\``）を含む・``.`` / ``..`` である・
    空である場合は ``None``。``decode_md_ref``（= ``urllib.parse.unquote``）は
    ``..%2F..%2Fsecret.jpg`` を素通しでパス脱出値へ復号してしまうため、
    将来この値をフォルダへ join する消費者が現れても「配布された post.md が
    フォルダ外を指す」経路にならないよう、契約の側で 1 コンポーネントを保証
    する（書き込み側ツールは ``encode_md_ref(ファイル名, safe="")`` で
    書くので、正規の post.md でここに区切りが現れることはない）。

    判定は **Windows の規則**で行う（配布先が Windows なので、そこで脱出でき
    ないことが契約）。区切り文字が無くても脱出は成立する: ``C:evil.jpg`` は
    ドライブ相対パスなので ``Path("D:/lib/post") / "C:evil.jpg"`` が基準
    フォルダを丸ごと捨て、``a.jpg:stream`` は NTFS の代替データストリームを
    指す。コロンと NUL も落とす。
    """
    m = _IMG_RE.search(text)
    if not m:
        return None
    name = _decode_ref(m.group(1))
    if not name or name in (".", ".."):
        return None
    if ":" in name or "\x00" in name:
        return None
    p = PureWindowsPath(name)
    if p.drive or p.root or p.name != name:
        return None
    return name


def _parse_posted_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        # ISO 8601, usually with a timezone (docs/formats/post-md.md §3)
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_post_identity(md_path: Path) -> tuple[str | None, str | None]:
    """Return ``(service, post_id)`` from a ``post.md`` head, or ``(None, None)``.

    Reads only the leading title + meta block and stops at the first body
    line, so a large body isn't loaded just to map a post to its folder
    (used by the "downloaded-post link" resolution index).
    """
    service, post_id, _creator = read_post_ref(md_path)
    return service, post_id


def read_post_ref(md_path: Path) -> tuple[str | None, str | None, str | None]:
    """Return ``(service, post_id, creator_id)`` from a ``post.md`` head.

    Same bounded head scan as :func:`read_post_identity` plus the creator id
    parsed out of the ``- creator: Name (id)`` meta line — the triple that
    identifies which service/creator/post a folder belongs to (used e.g. by
    the health check for context).  Missing / unreadable pieces are ``None``.

    Built on :func:`read_post_meta_checked` — the one bounded reader on the
    viewer side — rather than a second scan of its own, so the head contract
    (byte cap, BOM, ``errors="replace"``, what counts as meta) can only be
    changed in one place.  The creator id comes from the shared
    :func:`snappix.common.post_meta.creator_id_from_value`; the empty string it
    returns for a value with no ``(id)`` group is folded to ``None`` here
    because this triple reports "unknown" as ``None``.
    """
    parsed = read_post_meta_checked(md_path)[0]
    if parsed is None:
        return None, None, None
    meta = parsed.meta
    creator_id = creator_id_from_value(meta.get(KEY_CREATOR, ""))
    return (
        meta.get(KEY_SERVICE) or None,
        meta.get(KEY_POST_ID) or None,
        creator_id or None,
    )


def read_post_meta(md_path: Path) -> ParsedPost | None:
    """Read *md_path* and return its :class:`ParsedPost`, or ``None``.

    Only the leading title + meta block is needed for a metadata display, so
    this reads a bounded head
    (:data:`~snappix.common.post_meta.HEAD_READ_LIMIT`) rather than the whole
    post body — the meta block is always at the top, and truncating the tail
    only affects :attr:`ParsedPost.body` (unused by meta consumers).

    Returns ``None`` when the file is unreadable (``OSError``) or yields
    neither meta lines nor a title (a plain ``.md`` with no ``- key:`` lines
    and no ``# heading``) — the caller treats both as "no post metadata to
    show".  A ``post.md`` carrying **only a title** is still returned: the spec (docs/formats/post-md.md §3/§8) makes every meta key
    optional and promises the title still displays, and the grid tile path
    (``folder_scan`` → ``parse_post_md``) already shows such a title — the
    info panel / stage header, which go through this reader, must agree
    instead of silently falling back to the folder name.  Intended to run
    **off the GUI thread** (e.g. via
    :class:`snappix.viewer._runnable.GuardedStream`) because a cold-NAS read can
    block for seconds.
    """
    return read_post_meta_checked(md_path)[0]


def read_post_meta_checked(md_path: Path) -> "tuple[ParsedPost | None, bool]":
    """:func:`read_post_meta` + a *transient-failure* flag.

    Returns ``(parsed, retryable)``.  ``retryable`` is ``True`` only when the
    read failed with an :class:`OSError` **other than** "the file genuinely
    isn't there" (``FileNotFoundError`` / ``NotADirectoryError``) — e.g. a
    network-share hiccup or a sharing violation while a writer still holds the
    file.  A missing file and a present-but-meta-less ``.md`` both return
    ``(None, False)``: re-reading those cannot change the answer.  Callers that
    must not stick on a one-off failure (the 情報パネル meta card) use the flag
    to schedule a bounded retry; callers that don't care keep using
    :func:`read_post_meta`.
    """
    try:
        head = _read_head_bytes(md_path)
    except (FileNotFoundError, NotADirectoryError):
        return None, False
    except OSError:
        return None, True
    parsed = parse_post_md(head)
    return (parsed if (parsed.meta or parsed.title) else None), False


def parse_post_md(text: str) -> ParsedPost:
    """Parse ``post.md`` text.

    Tolerant: missing meta lines / unknown order / extra blank lines do not
    fail.  Returns sensible defaults for anything not present.

    A leading UTF-8 BOM is stripped: several callers read the file themselves
    with plain ``utf-8`` (folder scan / search / cache builder), and a BOM
    left on line 1 would make it match neither the title nor the meta regex —
    silently demoting the whole head to body.

    The head boundary itself (which lines are title / meta / body) is decided
    by the shared ``common.post_meta.scan_head`` — the single implementation
    of the public rule in docs/formats/post-md.md §2.  Only the value → type conversions below are this parser's own.
    """
    text = text.lstrip("﻿")
    head = scan_head(text)
    lines = head.lines
    parsed = ParsedPost()
    parsed.title = head.title
    # A malformed head that repeats a key keeps the **last** occurrence
    # (``head_meta`` agrees, so a reader and the parser can't disagree).
    for _i, key, value in head.meta:
        parsed.meta[key] = value.strip()
    body_start = head.body_start

    if KEY_TAGS in parsed.meta:
        parsed.tags = [
            t.strip() for t in parsed.meta[KEY_TAGS].split(",") if t.strip()
        ]
    if KEY_LOCKED_CONTENTS in parsed.meta:
        try:
            parsed.locked_count = int(parsed.meta[KEY_LOCKED_CONTENTS])
        except ValueError:
            parsed.locked_count = 0
    if KEY_POSTED_AT in parsed.meta:
        parsed.posted_at = _parse_posted_at(parsed.meta[KEY_POSTED_AT])
    if KEY_FAVORITES in parsed.meta:
        raw = parsed.meta[KEY_FAVORITES].strip()
        if raw:
            try:
                parsed.favorites = int(raw)
            except ValueError:
                parsed.favorites = None
    if KEY_PLAN in parsed.meta:
        parsed.plan_name = parsed.meta[KEY_PLAN].strip()
    if KEY_PLAN_PRICE in parsed.meta:
        parsed.plan_price = parsed.meta[KEY_PLAN_PRICE].strip()

    parsed.body = "\n".join(lines[body_start:]).lstrip("\n")
    # ``parsed.thumbnail`` is a lazy ``cached_property`` over the body — no eager regex sweep of the (possibly unbounded) full text here.
    return parsed
