"""Filter-box query syntax + sort-key engine for the left pane (pure logic).

Everything here is Qt-free and side-effect-free (apart from the bounded
``_BODY_CACHE`` read cache), so it is unit-testable without a display —
the same design rule as ``justified_layout.py`` / ``folder_scan.py``.

Split out of ``post_grid.py``:

* :func:`_parse_query` — the original tiny whitespace-AND / ``-`` exclusion
  syntax, still used by the advanced-search panel for the tag input.
* :class:`_FilterTerm` / :func:`_parse_filter_query` /
  :func:`_match_filter_terms` — the field-prefixed superset
  (``tags:`` / ``plan:`` / ``body:`` …) driving the main filter box, plus
  ``~a ~b`` Danbooru-style OR pooling of plain/field terms.
* :data:`_CONTROL_FIELDS` / :func:`parse_control_tokens` /
  :func:`strip_control_tokens` — the ``type:`` / ``rating:`` / ``score:``
  GUI-control tokens.  These are *parsed* here (so they don't leak into the
  substring match) but *applied* by ``PostGrid``, which reflects them into the
  filter-bar 種別 combo / AI-panel 年齢区分 band / AI-tag 精度 spin — one source
  of truth per dimension, so a token and its control never double-apply.
* **The field tables themselves are DERIVED from the condition-dimension
  registry** (条件次元レジストリ第3段 — 2026-08-28 提案2):
  :func:`_rebuild_field_tables` reads ``search_dimensions.TOKEN_FIELDS`` (the
  same ledger the prefix completer and the syntax-help table enumerate) once
  at module load and fills :data:`_FILTER_FIELD_GETTERS` /
  :data:`_NUMERIC_FIELDS` / :data:`_CURATION_FIELDS` /
  :data:`_CONTROL_FIELDS` **in place**, so adding a ledger row surfaces the
  field in the completer, the help table AND this parser at once — the sets
  can no longer drift apart.  Per-parse lookups stay plain dict/set
  membership (no per-keystroke ledger walk).  The control tokens' **value**
  tables (:data:`_TYPE_TOKEN_ALIASES` / :data:`_RATING_TOKEN_ALIASES`) are
  derived the same way from the dimension ledger's ``value_keys`` /
  ``token_values`` / ``token_aliases``
  (:func:`_rebuild_token_alias_tables`), so a new combo choice can't become a
  token the combo emits and the parser ignores.
* :data:`_FILTER_FIELD_GETTERS` / :func:`_entry_body_text` /
  :func:`parsed_post_cached` / :data:`_BODY_CACHE` — per-field haystack
  suppliers, including the lazy mtime-validated post.md reader behind the
  ``body:`` field.  ``_BODY_CACHE`` holds the whole parsed post (not just the
  body), so a ``body:`` read and any other post.md field read of the same file
  parse it only once.  The ``body:`` evaluation itself stays here on purpose:
  ``PostGrid``'s ``_BodyFilterTask`` runs :func:`_entry_body_text` on a
  single-thread pool so the module cache stays effectively single-owner.
* ``favorites:`` uses numeric comparison (``favorites:>=1000`` / exact
  ``favorites:5``) via :func:`_match_numeric_field`, not substring.
* :func:`_sort_spec` — sort-mode → ``(key_fn, reverse)`` mapping shared by
  the folders-first sort (entries without ``posted_at`` / ``favorites``
  always sink to the end regardless of direction).

Names keep their original leading underscore so ``post_grid`` re-exports
them unchanged (tests and callers import them from either module).
"""

from __future__ import annotations

import re
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import search_dimensions as _dimensions
from .folder_scan import FolderEntry
from .post_md import parse_post_md


def _posted_seconds(dt: datetime) -> float:
    """Return ``dt`` as POSIX seconds without ever touching the OS clock.

    ``datetime.timestamp()`` on a **tz-naive** value delegates to the platform
    ``mktime``, which raises ``OSError``/``OverflowError`` for instants the
    local timezone cannot represent (e.g. a naive ``1970-01-01T00:00:00`` on a
    JST machine, whose local epoch is 1970-01-01 09:00). External tools legally
    write such naive placeholders (post-md.md marks timezone only as
    *recommended*), so a naive value is treated as UTC — the ``timestamp()`` of
    an *aware* datetime is pure arithmetic and OS-independent. Any residual
    overflow falls back to a monotonic ordinal so the sort key can never raise.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (OSError, OverflowError, ValueError):
        # datetime.min/max territory: preserve ordering via the proleptic
        # Gregorian ordinal (day resolution is enough at these extremes).
        return float(dt.toordinal()) * 86400.0


def _strip_term_prefix(
    raw: str, *, casefold: bool = True,
) -> tuple[str, bool, bool] | None:
    """Split *raw* into ``(residue, exclude, or_group)`` — or ``None`` to drop it.

    Shared by :func:`_parse_query`, :func:`_parse_tag_groups` and
    :func:`_parse_filter_query` so the three parsers of the same tiny
    ``-``/``~`` grammar can't drift apart. Before item #58 they had: a token
    carrying **only one** marker stripped correctly, but a token carrying
    **both** (``-~foo`` — e.g. produced by toggling an existing ``~foo`` OR
    chip to an exclusion) only had the first marker peeled off in two of the
    three parsers, leaving a literal ``~foo`` residue — a needle that can
    never match a real tag/name, so the exclusion silently became a no-op
    *and* the term vanished from the OR pool it came from.

    * ``~foo`` → OR-pooled include (``or_group=True``).
    * ``-foo`` → exclusion (``exclude=True``).
    * ``~-foo`` / ``-~foo`` — both markers are stripped and ``-`` wins over
      ``~`` regardless of which one came first (``exclude=True,
      or_group=False``), matching :class:`_FilterTerm`'s contract that a
      ``~`` term is never also an exclusion.
    * A residue that is empty, or made up only of leftover ``-``/``~``
      symbols (``--`` → ``-``, ``~~`` → ``~``, ``-~`` → ``~`` → ``""``), is
      mid-typing noise, not a needle, so ``None`` is returned (item #25).  A
      residue that starts with a *repeat of the same* marker it was just
      stripped of (``--foo``) is **not** collapsed further — the leftover
      dash stays part of the literal needle, same as any other ``-``-prefixed
      needle that happens to start with ``-``.

    ``casefold=False`` keeps the residue in the user's original spelling, for
    surfaces that only *display* the terms (:func:`split_tag_terms`).  The
    drop verdict is taken on the residue either way — it only looks at ``-`` /
    ``~`` symbols, which have no case — so both spellings agree on which
    tokens survive.
    """
    token = raw
    exclude = False
    or_group = False
    if token.startswith("~") and len(token) > 1:
        or_group = True
        token = token[1:]
        if token.startswith("-") and len(token) > 1:
            exclude = True
            or_group = False  # ``-`` wins when both prefixes appear.
            token = token[1:]
            # Same rule as the ``-``-first branch below: once ``-`` has won,
            # a leftover leading ``~`` is the OR marker it overrode, not
            # literal text — leaving it in produced an unmatchable ``~foo``
            # exclusion (a no-op exclusion that also left the OR pool).
            token = token.lstrip("~")
    elif token == "~":
        return None
    elif token.startswith("-") and len(token) > 1:
        exclude = True
        token = token[1:]
        # ``-`` already committed to exclusion; a leftover leading ``~`` here
        # is the OR marker ``-`` just overrode, not literal text (item #58).
        token = token.lstrip("~")
    elif token == "-":
        return None
    if casefold:
        token = token.casefold()
    if not token.strip("-~"):
        return None
    return token, exclude, or_group


def split_tag_terms(text: str) -> tuple[list[str], list[str], list[str]]:
    """``(includes, or_terms, excludes)`` in the user's original spelling.

    The **display** counterpart of :func:`_parse_tag_groups`: same grammar,
    same drop rules, same ``-`` wins over ``~`` verdict — but the residues are
    not casefolded and the OR terms are handed back separately so a caller can
    render them as one pool (項目#240).  Surfaces that merely echo the query
    back to the user (the AIタグ一覧's 「現在の検索条件」 strip) used to peel
    only the first marker off by hand, so ``-~cat`` showed as ``~cat`` while
    the real query excluded ``cat``.
    """
    includes: list[str] = []
    or_terms: list[str] = []
    excludes: list[str] = []
    for raw in text.split():
        parsed = _strip_term_prefix(raw, casefold=False)
        if parsed is None:
            continue
        term, exclude, or_group = parsed
        if exclude:
            excludes.append(term)
        elif or_group:
            or_terms.append(term)
        else:
            includes.append(term)
    return includes, or_terms, excludes


def _parse_query(text: str) -> tuple[list[str], list[str]]:
    """Split a filter query into AND-include and AND-exclude term lists.

    Syntax (intentionally tiny):

    * Whitespace separates terms — all positive terms must match (AND).
    * A term prefixed with ``-`` is an exclusion; an item matches only
      when none of the exclusion terms appear in the haystack.
    * A term prefixed with ``~`` is an OR alternative (Danbooru style).  This
      function **flattens** ``~`` terms into the include list (the ``~`` is
      stripped) so every existing caller that only needs "are there include
      terms / how many" keeps working; the grouped OR structure — used to
      build the AI-tag SQL — is produced by :func:`_parse_tag_groups`.
    * Empty tokens, a lone ``-`` / ``~``, and any tokens that reduce to empty
      after stripping the prefix are dropped silently — the user's mid-
      typing state ("foo -") shouldn't suddenly invalidate the query.
    * A token whose residue after the prefix is itself only ``-`` / ``~``
      symbols (``--`` → ``-``, ``~~`` → ``~``) is likewise mid-typing noise,
      not a needle, so it is dropped rather than becoming a literal ``-`` /
      ``~`` term that would silently exclude/include a huge slice (item #25).
    * A token carrying **both** markers (``-~foo`` / ``~-foo``) has both
      stripped — ``-`` wins — rather than leaving a dangling, unmatchable
      ``~foo`` exclusion (item #58); see :func:`_strip_term_prefix`.

    All returned terms are casefolded so callers can compare against a
    pre-casefolded haystack without re-folding per term.
    """
    includes: list[str] = []
    excludes: list[str] = []
    for raw in text.split():
        parsed = _strip_term_prefix(raw)
        if parsed is None:
            continue
        term, exclude, _or_group = parsed
        # Flattened here (unlike _parse_tag_groups): every surviving
        # non-excluded term — plain or ``~`` OR-pooled — is a positive AND
        # include.
        if exclude:
            excludes.append(term)
        else:
            includes.append(term)
    return includes, excludes


def _parse_tag_groups(text: str) -> tuple[list[list[str]], list[str]]:
    """Parse an AI-tag query into OR-grouped includes + a flat exclude list.

    Extends :func:`_parse_query` with Danbooru-style ``~`` OR pooling for the
    AI-tag search (item E03).  Returns ``(include_groups, excludes)`` where:

    * each **plain** include term becomes its own singleton AND-group — every
      such group must be satisfied (``a b`` ⇒ ``a AND b``);
    * every ``~``-prefixed term pools into a **single** trailing OR-group — an
      image satisfies that group when it carries *any* of the pooled terms
      (``~a ~b c`` ⇒ ``(a OR b) AND c``);
    * ``-`` exclusions are returned flat (an OR of an exclusion is
      ill-defined — ``-`` wins), matching the filter box's
      ``_parse_filter_query``.

    Duplicate terms are removed within each group (a repeated tag in a strict
    AND would make the DISTINCT-group count exceed the tags any image can carry
    and return zero); the OR group is appended **last** so the AND-group order
    stays stable for the query signature.  All terms are casefolded.

    A token carrying **both** markers (``-~foo`` / ``~-foo`` — e.g. toggling
    an existing ``~foo`` OR chip to an exclusion) has both stripped and ``-``
    wins: the tag is excluded and also removed from the OR pool, rather than
    becoming a dangling ``~foo`` exclusion that can never match a real tag
    while silently vanishing from the pool (item #58); see
    :func:`_strip_term_prefix`.

    **Cross-group duplicates are normalised here (item E03)** as a query
    simplification (since review item #36 the engine also counts a duplicated
    tag toward every group it belongs to, so this fold is no longer
    load-bearing for correctness).  By the absorption law, an OR group
    containing a tag that already appears as a **mandatory plain singleton**
    is unconditionally satisfied (``cat AND (cat OR dog)`` ≡ ``cat`` — an
    image with ``cat`` always clears the OR arm), so the **whole OR group is
    discarded** (``cat ~cat`` ⇒ ``[[cat]]``, ``cat ~cat ~dog`` ⇒ ``[[cat]]``).
    Stripping only the duplicated member — the pre-#20 behaviour — would
    promote the surviving alternatives into a new mandatory AND group
    (``cat AND dog``), silently narrowing the query relative to the filter
    box's OR-pool evaluation of the same ``~`` syntax (review #20).
    """
    plain: list[str] = []
    or_terms: list[str] = []
    excludes: list[str] = []
    for raw in text.split():
        parsed = _strip_term_prefix(raw)
        if parsed is None:
            continue
        term, exclude, or_group = parsed
        if exclude:
            if term not in excludes:
                excludes.append(term)
        elif or_group:
            if term not in or_terms:
                or_terms.append(term)
        else:
            plain.append(term)
    groups: list[list[str]] = []
    seen_plain: set[str] = set()
    for term in plain:
        if term in seen_plain:
            continue
        seen_plain.add(term)
        groups.append([term])
    if or_terms:
        # Absorption (review #20): an OR group containing a tag already
        # required as a plain singleton is unconditionally satisfied
        # (``cat AND (cat OR dog)`` ≡ ``cat``), so the WHOLE group is dropped.
        # Stripping only that member would promote the remaining alternatives
        # into a new mandatory AND group and narrow the query
        # (``cat ~cat ~dog`` must not become ``cat AND dog``).
        if not any(term in seen_plain for term in or_terms):
            groups.append(or_terms)
    return groups, excludes


#: Parsed ``post.md`` (whole :class:`~.post_md.ParsedPost`) keyed by
#: ``str(md_path)`` and validated by mtime, so an edited post is re-parsed.
#: Holding the *whole* parse — not just the body string — means the ``body:``
#: filter and any other post.md-derived read (posted_at / tags) share a single
#: parse per file instead of each re-reading + re-parsing it (#108).  Only
#: populated lazily (first ``body:`` match / first :func:`parsed_post_cached`
#: call), so most sessions never touch it; a bounded LRU caps memory on huge
#: libraries.  The stored casefolded body rides alongside the parse so the hot
#: ``body:`` path doesn't re-casefold on every cache hit.
#:
#: Each value is ``(mtime, ParsedPost, folded_body, cost)`` where ``cost`` is
#: the entry's content size in characters (raw body + folded copy).
_BODY_CACHE: "OrderedDict[str, tuple[float, object, str, int]]" = OrderedDict()
#: Entry-count ceiling (cheap guard against unbounded key growth).
_BODY_CACHE_MAX = 4096
#: Content ceiling in characters.  A count-only cap says nothing about how much
#: text is resident: one entry holds a post's whole body *and* its casefolded
#: copy, so 4,096 entries of MB-sized bodies (posts with embedded base64 or very
#: long text) could pin gigabytes in a portable viewer that otherwise budgets its
#: caches in bytes (``FolderPreviewCache`` / ``SearchIndex`` take ``max_bytes``).
#: Ordinary libraries — a few KB per post — are still bounded by the entry count
#: (4,096 × ~8 KB ≒ this budget), so this only bites the pathological case
#: (#160).  An entry whose own content exceeds the budget is simply not retained.
_BODY_CACHE_MAX_CHARS = 32 * 1024 * 1024
#: Running sum of the cached entries' ``cost`` (kept in step by
#: :func:`_body_cache_store`, the only writer).
_BODY_CACHE_CHARS = 0


def _body_cache_store(key: str, mtime: float, parsed, folded: str) -> None:
    """Insert one entry and evict LRU-first until both budgets hold."""
    global _BODY_CACHE_CHARS
    previous = _BODY_CACHE.pop(key, None)
    if previous is not None:
        _BODY_CACHE_CHARS -= previous[3]
    cost = len(parsed.body) + len(folded)
    _BODY_CACHE[key] = (mtime, parsed, folded, cost)
    _BODY_CACHE_CHARS += cost
    while _BODY_CACHE and (
        len(_BODY_CACHE) > _BODY_CACHE_MAX
        or _BODY_CACHE_CHARS > _BODY_CACHE_MAX_CHARS
    ):
        _evicted_key, evicted = _BODY_CACHE.popitem(last=False)
        _BODY_CACHE_CHARS -= evicted[3]


def parsed_post_cached(md_path, *, mtime: float | None = None):
    """Return the mtime-validated cached :class:`ParsedPost` for *md_path*.

    Reads + parses ``post.md`` once and caches the result keyed by mtime, so
    repeated reads of the same file (the ``body:`` filter *and* a posted_at /
    tags read) share one parse.  Returns ``None`` when the file can't be read.
    The returned object is the shared cached instance — treat it as read-only.

    *mtime* is the caller's already-obtained ``st_mtime`` for *md_path*: the
    ``body:`` fast path (:func:`_entry_body_text`) stats the file itself to
    check the cache without re-casefolding, and passing that value through
    keeps the cold path from stat-ing the very same file a second time.  A
    ``body:`` term is evaluated against **every** entry of the current view, so
    on a cold NAS folder with 1,200 children the doubled round-trips were
    directly visible (#161).
    """
    key = str(md_path)
    if mtime is None:
        try:
            mtime = md_path.stat().st_mtime
        except OSError:
            return None
    cached = _BODY_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        _BODY_CACHE.move_to_end(key)
        return cached[1]
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = parse_post_md(text)
    _body_cache_store(key, mtime, parsed, parsed.body.casefold())
    return parsed


def _entry_body_text(entry: FolderEntry) -> str:
    """Return the casefolded ``post.md`` body text for *entry* (cached).

    The body (post text below the title + meta block, per
    :func:`parse_post_md`) backs the ``body:`` filter field.  Reads happen
    lazily — only the first time a folder is matched against a ``body:`` term —
    and share the mtime-keyed :func:`parsed_post_cached` cache so a body read and
    any other post.md field read of the same file parse it only once (#108).
    Returns ``""`` for entries with no readable post.md.
    """
    if not entry.is_dir or not entry.has_post_md:
        return ""
    md = entry.path / "post.md"
    # Fast path: return the pre-casefolded body stored alongside the parse
    # without re-casefolding on every keystroke hit.
    key = str(md)
    try:
        mtime = md.stat().st_mtime
    except OSError:
        return ""
    cached = _BODY_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        _BODY_CACHE.move_to_end(key)
        return cached[2]
    # Hand the mtime we just read to the cold path so it doesn't stat the same
    # post.md again (#161) — this runs once per entry of the current view.
    parsed = parsed_post_cached(md, mtime=mtime)
    if parsed is None:
        return ""
    # parsed_post_cached just populated the entry; read its stored folded body.
    entry_c = _BODY_CACHE.get(key)
    return entry_c[2] if entry_c is not None else parsed.body.casefold()


# --------------------------------------------------------------------------
# Ledger-derived field tables (条件次元レジストリ第3段 — 2026-08-28 提案2)
#
# The four tables below used to be hand-written here while the prefix
# completer / syntax help enumerated ``search_dimensions.TOKEN_FIELDS`` — two
# lists of the same fields, kept equal only by a set-equality test.  They are
# now DERIVED from that ledger: :func:`_rebuild_field_tables` runs once at
# module load (and again from tests that temporarily extend the ledger) and
# mutates the containers IN PLACE, so ``from``-imported bindings elsewhere
# (``post_grid`` re-exports) stay live.  Per-parse lookups are unchanged —
# plain dict/set membership on prebuilt containers, never a ledger walk.
# --------------------------------------------------------------------------

#: Fast haystack suppliers for the known ledger ``source`` values (kept as
#: direct lambdas so the per-entry match path costs exactly what the old
#: hand-written getters did).  ``body`` is the odd one out — it reads (and
#: caches) the post.md body lazily rather than using an already-parsed
#: FolderEntry attribute.
_SOURCE_GETTERS: dict[str, "callable"] = {
    "tags": lambda e: " ".join(e.tags).casefold(),
    "plan_name": lambda e: e.plan_name.casefold(),
    "plan_price": lambda e: e.plan_price.casefold(),
    "title": lambda e: e.title.casefold(),
    "path_name": lambda e: e.path.name.casefold(),
    "body": _entry_body_text,
}


def _generic_text_getter(attr: str):
    """Haystack supplier for a ledger row naming any ``FolderEntry`` attribute.

    Fallback for ``source`` values without a dedicated fast getter above, so a
    NEW ledger row over an existing entry attribute works without touching
    this module (the stage-3 acceptance test adds such a row).  Slightly
    slower than the dedicated lambdas (one ``getattr`` + ``isinstance``), so
    the shipping rows keep their fast getters.
    """
    def get(e, _attr=attr):
        value = getattr(e, _attr, None)
        if value is None:
            return ""
        if isinstance(value, str):
            return value.casefold()
        try:
            return " ".join(str(x) for x in value).casefold()
        except TypeError:
            return str(value).casefold()
    return get


def _empty_haystack(_e) -> str:
    """Placeholder getter for fields that never match by substring."""
    return ""


#: Field prefixes recognised in the main filter box.  ``field:value`` scopes a
#: term to one post.md-derived attribute instead of the default
#: name+title+tags+filenames haystack; ``-field:value`` excludes.  Each maps to
#: a function returning the casefolded haystack for that field on an entry.
#: Curation fields resolve against the injected user_meta lookup and numeric
#: fields compare by value, so their getters are placeholders (only their
#: *names* need to be here so ``_parse_filter_query`` treats them as
#: field-scoped) — actual matching is in :func:`_match_filter_terms`.
#: DERIVED from the ledger by :func:`_rebuild_field_tables`.
_FILTER_FIELD_GETTERS: dict[str, "callable"] = {}

#: ``field`` → numeric value getter for :data:`_NUMERIC_FIELDS` rows (the
#: ledger's ``source`` attribute read raw — ``None`` when the entry has no
#: value).  DERIVED from the ledger by :func:`_rebuild_field_tables`.
_NUMERIC_GETTERS: dict[str, "callable"] = {}

#: Fields evaluated numerically rather than by substring (see
#: :func:`_match_numeric_field`).  ``favorites`` is a count, so ``favorites:5``
#: means *exactly* 5 and ``favorites:>=1000`` a range — a plain substring match
#: would wrongly accept 15 / 50 / 500 for ``favorites:5``.  (``star`` is also
#: numeric but lives in :data:`_CURATION_FIELDS` — it's resolved via the
#: injected curation lookup, not the entry, and matched *before* this set.)
#: DERIVED from the ledger (``match="numeric"`` rows).
_NUMERIC_FIELDS: set[str] = set()

#: Curation fields whose value comes from the user_meta store, not the
#: ``FolderEntry``.  ``star`` (numeric 0–5, same ``[op]number`` grammar as
#: ``favorites:``), ``mytags`` (user tags — substring with ``-`` exclusion, like
#: ``tags:``), ``later`` (``yes``/``no`` boolean).  Matched via a curation
#: lookup the caller injects (the entry carries no curation data — it's keyed by
#: path in the store), and checked ahead of :data:`_NUMERIC_FIELDS`.
#: DERIVED from the ledger (``match="curation"`` rows).
_CURATION_FIELDS: set[str] = set()

#: Recognised control-field prefixes (``type:`` / ``rating:`` / ``score:``).
#: See :func:`parse_control_tokens`.  DERIVED from the ledger
#: (``match="control"`` rows).
_CONTROL_FIELDS: set[str] = set()


def _rebuild_field_tables() -> None:
    """(Re)derive the parser's field tables from the token ledger.

    Reads ``search_dimensions.TOKEN_FIELDS`` (the single source the prefix
    completer and the syntax-help table already enumerate) and refills the
    four field tables **in place** — identities are preserved so
    ``from``-imports of these names (``post_grid``'s re-exports, the
    ``strip_control_tokens`` default argument) observe the rebuild.  Called
    once at module load; tests that temporarily extend the ledger call it
    again after patching ``search_dimensions.TOKEN_FIELDS`` (and once more to
    restore).
    """
    getters: dict[str, "callable"] = {}
    numeric_getters: dict[str, "callable"] = {}
    numeric: set[str] = set()
    curation: set[str] = set()
    control: set[str] = set()
    for tok in _dimensions.TOKEN_FIELDS:
        if tok.match == "control":
            control.add(tok.field)
        elif tok.match == "curation":
            curation.add(tok.field)
            getters[tok.field] = _empty_haystack
        elif tok.match == "numeric":
            numeric.add(tok.field)
            numeric_getters[tok.field] = (
                lambda e, _attr=(tok.source or tok.field): getattr(e, _attr, None)
            )
            getters[tok.field] = _empty_haystack
        else:  # "text"
            getter = _SOURCE_GETTERS.get(tok.source or "")
            if getter is None:
                getter = _generic_text_getter(tok.source or tok.field)
            getters[tok.field] = getter
    _FILTER_FIELD_GETTERS.clear()
    _FILTER_FIELD_GETTERS.update(getters)
    _NUMERIC_GETTERS.clear()
    _NUMERIC_GETTERS.update(numeric_getters)
    _NUMERIC_FIELDS.clear()
    _NUMERIC_FIELDS.update(numeric)
    _CURATION_FIELDS.clear()
    _CURATION_FIELDS.update(curation)
    _CONTROL_FIELDS.clear()
    _CONTROL_FIELDS.update(control)
    _rebuild_token_alias_tables()


#: ``favorites:>=1000`` / ``favorites:<50`` / ``favorites:=5`` / ``favorites:5``.
#: Group 1 = optional operator (``>=`` ``<=`` ``>`` ``<`` ``=``), group 2 = the
#: integer.  A bare number (no operator) means exact equality.
#:
#: The digit run is **capped**: ``int(str)`` raises ``ValueError`` past 4,300
#: digits (CPython's conversion limit), which would turn a pasted mega-number
#: into an exception mid-filter instead of the inert term this module's
#: contract promises (``favorites:abc`` matches nothing).  18 digits covers
#: every value a favourites count / star rating can hold, so a longer run is
#: simply not a recognised numeric needle — inert, and kept in the chip.
_NUM_TERM_RE = re.compile(r"^(>=|<=|>|<|=)?(\d{1,18})$")


def _parse_num_term(value: str) -> tuple[str, int] | None:
    """``(op, number)`` for a ``[op]number`` needle, or ``None`` when malformed.

    Interpreting the needle is a property of the **term**, not of the entry
    being compared, so it happens once per parse (:func:`_parse_filter_query`
    fills :attr:`_FilterTerm.num`) instead of once per entry: the match loop
    runs over every entry of the current view on every keystroke.
    """
    m = _NUM_TERM_RE.match(value.strip())
    if not m:
        return None
    return m.group(1) or "=", int(m.group(2))


def _compare_num(actual: int, spec: tuple[str, int]) -> bool:
    """Evaluate a parsed ``[op]number`` needle against *actual*."""
    op, num = spec
    if op == "=":
        return actual == num
    if op == ">":
        return actual > num
    if op == ">=":
        return actual >= num
    if op == "<":
        return actual < num
    return actual <= num  # op == "<="


# --------------------------------------------------------------------------
# GUI-control field tokens (``type:`` / ``rating:`` / ``score:``)
#
# These are recognised as field-scoped tokens so ``_parse_filter_query`` does
# not treat them as plain substring needles, but they carry NO matching logic
# in :func:`_match_filter_terms` — they are consumed by ``PostGrid`` which
# reflects them into the existing single dimension state (the filter-bar 種別
# combo, the AI-panel 年齢区分 band, and the AI-tag 精度 spin).  Keeping them
# equivalent to operating those controls means there is exactly one source of
# truth per dimension (no double application, no combo/token divergence).
# --------------------------------------------------------------------------

#: ``type:`` token value → filter-bar media key (all/image/video/audio/
#: document/archive).  DERIVED from the dimension ledger by
#: :func:`_rebuild_token_alias_tables`.
_TYPE_TOKEN_ALIASES: dict[str, str] = {}

#: ``rating:`` token value → AI-panel age-band key.  Note the band literally
#: keyed ``sfw`` is the *safe+questionable* R-15 band, so its canonical token
#: is ``r15``; ``safe``/``sfw`` map to the all-ages ``safe`` band, matching
#: common "safe for work" intuition.  DERIVED from the ledger, same as above.
_RATING_TOKEN_ALIASES: dict[str, str] = {}

#: Token prefix → the alias table the parser reads for it (the two tables
#: above, by the ``token_prefix`` their ledger row declares).
_TOKEN_ALIAS_TABLES: dict[str, dict[str, str]] = {
    "type": _TYPE_TOKEN_ALIASES,
    "rating": _RATING_TOKEN_ALIASES,
}

#: ``token_prefix`` → ledger dimension id for the tables above.
_ALIAS_SOURCE_DIMS: dict[str, str] = {"type": "media", "rating": "rating"}


def _rebuild_token_alias_tables() -> None:
    """(Re)derive the control-token value tables from the dimension ledger.

    The right-hand side of these tables is the **stored value** of a condition
    dimension — the combo ``data`` key, and for ``rating:`` the key
    ``advanced_search._RATING_BANDS`` is keyed by.  Deriving them means a new
    choice in ``value_keys`` is a recognised token the moment it exists,
    instead of a value the combo and the syntax preview emit but the parser
    silently ignores.  Three layers, later wins:

    1. every stored value maps to itself (``type:image``);
    2. ``token_values`` — the canonical token spelling :func:`token_expression`
       writes (``rating:r15`` → the ``sfw`` band);
    3. ``token_aliases`` — the input vocabulary (``mp4`` / ``全年齢`` …), last
       so an alias can redirect a spelling that collides with a stored value
       (``rating:sfw`` means the all-ages band, not the R-15 one).

    Rebuilt **in place** for the same reason as the field tables above:
    ``from``-imported bindings stay live.
    """
    for prefix, table in _TOKEN_ALIAS_TABLES.items():
        dim = _dimensions.get(_ALIAS_SOURCE_DIMS[prefix])
        built: dict[str, str] = {}
        if dim is not None:
            for value, _label_key in dim.value_keys:
                built[value] = value
            for value, token in dim.token_values:
                built[token] = value
            for alias, value in dim.token_aliases:
                built[alias] = value
        table.clear()
        table.update(built)


# Both derivations run once at module load (the alias tables are filled from
# inside :func:`_rebuild_field_tables`, so a test that extends the ledger and
# re-derives gets both halves from one call).
_rebuild_field_tables()

#: ``score:>0.5`` / ``score:>=0.5`` / ``score:=0.5`` / ``score:0.5`` — an AI-tag
#: precision FLOOR.  ``>`` / ``>=`` / ``=`` / bare all set the floor to the
#: number; ``<`` / ``<=`` have no floor meaning and are rejected (inert).
#: (The integer part is capped for the same reason as :data:`_NUM_TERM_RE`;
#: the fraction is free — ``float`` saturates to ``inf`` rather than raising.)
_SCORE_TERM_RE = re.compile(r"^(>=|>|=)?(\d{1,18}(?:\.\d+)?)$")


def _parse_score_token(value: str) -> float | None:
    """The AI-tag precision floor a ``score:`` token requests, or ``None``.

    Accepts ``>N`` / ``>=N`` / ``=N`` / bare ``N`` (all "floor at N"); a
    ``<`` / ``<=`` comparison or any non-numeric value is inert (returns
    ``None``) rather than silently doing something surprising.  Not clamped
    here — the caller clamps to the DB-recorded ``[floor, 1.0]`` range.
    """
    m = _SCORE_TERM_RE.match(value.strip())
    if not m:
        return None
    try:
        num = float(m.group(2))
    except ValueError:  # pragma: no cover (regex already guaranteed numeric)
        return None
    return num if num >= 0.0 else None


@dataclass(frozen=True)
class ControlTokens:
    """The GUI-control dimensions a filter query requests, if any.

    Each field is ``None`` when the corresponding control token is absent, so
    the caller only overrides a dimension the user actually named.  ``media``
    and ``rating`` are the resolved combo/band keys; ``score`` is the raw
    (un-clamped) precision floor.
    """

    media: str | None = None
    rating: str | None = None
    score: float | None = None


def parse_control_tokens(text: str) -> ControlTokens:
    """Extract ``type:`` / ``rating:`` / ``score:`` control tokens from *text*.

    Returns a :class:`ControlTokens` describing the dimensions the query names.
    Unrecognised token values (``type:foo`` / ``rating:xyz`` / ``score:<0.1``)
    are ignored.  When a dimension is named more than once the LAST occurrence
    wins (matching the "later edit overrides" feel of typing).  Excluded
    control tokens (``-type:video``) are ignored — negating a control makes no
    sense; clear it instead.  ``~`` OR members are ignored for the same reason:
    a control is a single value, so applying ``~type:video`` as a plain
    ``type:video`` would silently drop the OR the user asked for (review #100).
    Both stay visible in the 絞り込み chip (:func:`strip_control_tokens` keeps
    prefixed tokens) rather than vanishing without a trace.
    """
    media = rating = None
    score = None
    for term in _parse_filter_query(text):
        if term.field not in _CONTROL_FIELDS or term.exclude or term.or_group:
            continue
        if term.field == "type":
            key = _TYPE_TOKEN_ALIASES.get(term.value)
            if key is not None:
                media = key
        elif term.field == "rating":
            key = _RATING_TOKEN_ALIASES.get(term.value)
            if key is not None:
                rating = key
        elif term.field == "score":
            f = _parse_score_token(term.value)
            if f is not None:
                score = f
        # A ledger-declared control field without wiring here stays inert
        # (recognised as field-scoped, applied nowhere) rather than being
        # silently mis-read as a score floor.
    return ControlTokens(media=media, rating=rating, score=score)


def _control_token_consumed(field: str, value: str) -> bool:
    """Whether :func:`parse_control_tokens` actually *applies* ``field:value``.

    Mirrors the value lookups above, so :func:`strip_control_tokens` can drop
    exactly the tokens that own a dimension chip and keep the ones that don't
    (``type:foo`` / ``score:<0.1`` / the half-typed ``type:``).  Fields outside
    :data:`_CONTROL_FIELDS` (the caller may also pass ``star`` / ``later``,
    which it has already vetted with its own ``_syncable_curation_value``) keep
    the plain head-name behaviour.
    """
    if field == "type":
        return value.casefold() in _TYPE_TOKEN_ALIASES
    if field == "rating":
        return value.casefold() in _RATING_TOKEN_ALIASES
    if field == "score":
        return _parse_score_token(value) is not None
    return True


def strip_control_tokens(text: str, fields=_CONTROL_FIELDS) -> str:
    """Return *text* with control tokens for *fields* removed (order preserved).

    Used both to build the "絞り込み" banner chip label (control tokens show as
    their own dimension chips, not inside the filter-box chip) and to drop a
    single control token when its dimension chip's × is clicked, so clearing a
    dimension actually sticks instead of the token re-applying on rebuild.
    A token is stripped when its head — before the ``:`` — casefolds into
    *fields*, it carries no ``~`` / ``-`` prefix, **and its value is one
    :func:`parse_control_tokens` actually applies**.  Everything else is kept:

    * Prefixed control tokens: :func:`parse_control_tokens` does not consume
      them, so stripping them too made ``-type:video`` an input that is neither
      applied, nor matched (``_match_filter_terms`` skips control fields), nor
      visible anywhere — asymmetric with the other inert tokens
      (``favorites:abc``) which stay in the chip (review #100).  This also
      matches the curation-token contract, where ``-``/``~`` members stay
      ordinary text terms.
    * Tokens with an unrecognised value (``type:foo``, ``rating:xyz``,
      ``score:<0.1``) and the half-typed ``type:`` — same reasoning, the value
      simply never reaches a control, so the token owns no dimension chip and
      must stay visible in the 絞り込み chip instead of vanishing (#162).
    """
    kept: list[str] = []
    for raw in text.split():
        if raw[:1] in ("~", "-"):
            kept.append(raw)
            continue
        head, sep, tail = raw.partition(":")
        prefix = head.casefold()
        if sep and prefix in fields and _control_token_consumed(prefix, tail):
            continue
        kept.append(raw)
    return " ".join(kept)


def _owning_token_index(
    raws: list[str], fields, extra_consumed=None,
) -> set[int]:
    """Positions of the tokens that actually **own** a dimension chip.

    Same qualification as :func:`strip_control_tokens` (head in *fields*, no
    ``~`` / ``-`` prefix, a value the control applies), plus the tie-break the
    application side already uses: when one dimension is named more than once,
    **the last occurrence wins** (:func:`parse_control_tokens`), so only that
    one owns the chip.  *extra_consumed* answers the same "is this value
    actually applied?" question for fields outside :data:`_CONTROL_FIELDS`
    (the caller's ``star:`` / ``mytags:`` / ``later:`` — it has its own
    syncable-value rule); without it such a field qualifies on the head name
    alone, as before.
    """
    owners: dict[str, int] = {}
    for idx, raw in enumerate(raws):
        if raw[:1] in ("~", "-"):
            continue
        head, sep, tail = raw.partition(":")
        prefix = head.casefold()
        if not sep or prefix not in fields:
            continue
        consumed = (
            _control_token_consumed(prefix, tail)
            if prefix in _CONTROL_FIELDS
            else (extra_consumed is None or extra_consumed(prefix, tail))
        )
        if consumed:
            owners[prefix] = idx
    return set(owners.values())


def strip_owned_control_tokens(
    text: str, fields=_CONTROL_FIELDS, extra_consumed=None,
) -> str:
    """*text* minus the tokens that own a dimension chip (order preserved).

    :func:`strip_control_tokens` removes **every** occurrence, which is what a
    dimension chip's × needs (leave one behind and it re-applies on the next
    rebuild, so the chip never goes away).  The 絞り込み chip label needs the
    other half of that contract: a dimension named twice
    (``type:image type:video``) is applied last-wins, so the losing token
    reaches no control at all — dropping it from the label too made it an
    input that is neither applied, nor matched (``_match_filter_terms`` skips
    control fields), nor visible anywhere (#162).  It stays in the chip for
    exactly the reason ``type:foo`` / ``-type:video`` do.
    """
    raws = text.split()
    owned = _owning_token_index(raws, fields, extra_consumed)
    return " ".join(raw for idx, raw in enumerate(raws) if idx not in owned)


def _entry_field_number(entry: FolderEntry, field: str) -> int | None:
    """The numeric value of *field* on *entry*, or ``None`` when absent.

    The getter comes from the ledger (``match="numeric"`` rows —
    :data:`_NUMERIC_GETTERS`); a non-int value is treated as absent so a
    mis-declared ledger row matches nothing instead of raising mid-compare.
    """
    getter = _NUMERIC_GETTERS.get(field)
    if getter is None:  # pragma: no cover (guarded by _NUMERIC_FIELDS)
        return None
    value = getter(entry)
    return value if isinstance(value, int) else None


def _match_numeric_field(
    entry: FolderEntry, field: str, value: str,
    spec: tuple[str, int] | None = None,
) -> bool:
    """Compare a numeric field against a ``[op]number`` needle.

    ``value`` is one of ``N`` (exact), ``=N``, ``>N``, ``>=N``, ``<N``, ``<=N``.
    Returns ``False`` when the entry has no value for the field (e.g. a folder
    with no favorites count never matches a ``favorites:`` term) or when
    ``value`` isn't a recognised numeric comparison (a malformed term matches
    nothing rather than silently falling back to substring, so ``favorites:abc``
    is inert instead of surprising).

    *spec* is the term's already-parsed needle (:attr:`_FilterTerm.num`); it is
    re-parsed from *value* only for callers that hand-build a term.
    """
    actual = _entry_field_number(entry, field)
    if actual is None:
        return False
    if spec is None:
        spec = _parse_num_term(value)
        if spec is None:
            return False
    return _compare_num(actual, spec)


@dataclass(frozen=True)
class _FilterTerm:
    """One parsed token of the filter query.

    * ``field`` is ``None`` for a plain term (matched against the combined
      name + title + tags + filenames haystack), a key of
      :data:`_FILTER_FIELD_GETTERS` for a field-scoped term, or a member of
      :data:`_CONTROL_FIELDS` (``type:`` / ``rating:`` / ``score:``) for a
      GUI-control token — those are recognised so they aren't treated as plain
      substring needles, but they are matched by the GUI (they drive the
      filter-bar / AI-panel controls), not inside :func:`_match_filter_terms`.
    * ``value`` is the casefolded needle (never empty).
    * ``exclude`` is ``True`` for ``-`` / ``-field:`` terms.
    * ``or_group`` is ``True`` for ``~`` terms — Danbooru-style OR: all
      ``~`` terms are pooled and the entry matches when *any* of them is
      present, while non-``~`` terms keep the default AND semantics.  A ``~``
      term is never also an exclusion (``-`` wins if both prefixes appear).
    * ``num`` is the ``[op]number`` needle already interpreted, for the numeric
      and ``star:`` fields (``None`` for every other field and for a malformed
      value).  Interpreting the needle is a property of the term, so it runs
      once in :func:`_parse_filter_query` instead of once per entry inside the
      match loop — which walks the whole view on every keystroke.
    """

    field: str | None
    value: str
    exclude: bool
    or_group: bool = False
    num: tuple[str, int] | None = None


def _parse_filter_query(text: str) -> list[_FilterTerm]:
    """Parse the filter box into structured :class:`_FilterTerm` tokens.

    Extends :func:`_parse_query`'s tiny whitespace-AND / ``-`` exclusion syntax
    with ``field:value`` prefixes (e.g. ``tags:風景``, ``plan_price:100``,
    ``-tags:風景``).  A ``head:tail`` token is only treated as field-scoped when
    ``head`` is a recognised field (:data:`_FILTER_FIELD_GETTERS`); otherwise
    the whole token stays a plain term, so an ordinary needle that happens to
    contain a colon keeps matching as before.  Empty values, a lone ``-`` and a
    residue that is only ``-`` / ``~`` symbols (``--`` → ``-``, ``~~`` → ``~``)
    are dropped (mid-typing tolerance — item #25).  A token carrying **both**
    markers (``-~foo`` / ``~-foo``) has both stripped, with ``-`` winning
    either way round, rather than leaving a dangling ``~foo`` exclusion that
    can never match a real field value (item #58); see
    :func:`_strip_term_prefix`.
    """
    terms: list[_FilterTerm] = []
    for raw in text.split():
        parsed = _strip_term_prefix(raw)
        if parsed is None:
            continue
        token, exclude, or_group = parsed
        field: str | None = None
        value = token
        if ":" in token:
            head, _, tail = token.partition(":")
            head_cf = head.casefold()
            if head_cf in _FILTER_FIELD_GETTERS or head_cf in _CONTROL_FIELDS:
                field = head_cf
                value = tail
        if not value:
            continue
        num = (
            _parse_num_term(value)
            if field is not None
            and (field in _NUMERIC_FIELDS or field in _CURATION_FIELDS)
            else None
        )
        terms.append(_FilterTerm(field, value, exclude, or_group, num))
    return terms


def _curation_present(term: _FilterTerm, star: int, tags: tuple, later: bool) -> bool:
    """Whether a curation-field *term* matches the given ``(star, tags, later)``.

    * ``star`` — ``[op]number`` comparison (``star:>=3`` / ``star:=5`` /
      ``star:0`` for un-starred) via the same ``_NUM_TERM_RE`` grammar.
    * ``mytags`` — substring over the joined user tags (``mytags:構図参考``);
      exclusion is handled by the caller via ``term.exclude``.
    * ``later`` — ``yes``/``true``/``1`` ⇒ flag set; ``no``/``false``/``0`` ⇒
      flag clear.  Any other value never matches (inert, like a bad number).
    """
    if term.field == "star":
        spec = term.num or _parse_num_term(term.value)
        if spec is None:
            return False
        return _compare_num(star, spec)
    if term.field == "mytags":
        return term.value in " ".join(tags).casefold()
    # later
    if term.value in ("yes", "true", "1", "on"):
        return later
    if term.value in ("no", "false", "0", "off"):
        return not later
    return False


def _match_filter_terms(
    entry: FolderEntry,
    terms: list[_FilterTerm],
    *,
    include_file_names: bool,
    curation=None,
) -> bool:
    """Return whether *entry* satisfies every parsed filter term (AND).

    Plain terms match against the combined haystack (built lazily once);
    field-scoped terms match against just that field.  An include term must be
    present; an exclude term must be absent.  ``~`` (OR-group) include terms are
    pooled: when any exist, the entry must satisfy *at least one* of them (in
    addition to every plain AND / exclusion term).  Control-field tokens
    (:data:`_CONTROL_FIELDS`) are skipped here — they are applied by the GUI.

    *curation* is an optional ``path -> (star, tags_tuple, later)`` lookup
    supplying the ``star:`` / ``mytags:`` / ``later:`` fields (the entry carries
    no curation data — it lives in ``user_meta.db`` keyed by path).  When it is
    ``None`` a curation term treats the entry as un-curated (``0, (), False``),
    so an include term fails and an exclude term passes — the same "absent field"
    semantics the other getters follow.
    """
    general: str | None = None
    cur: tuple[int, tuple, bool] | None = None
    or_seen = False  # at least one ~ term present in the query
    or_hit = False   # at least one ~ term matched
    for term in terms:
        # Control tokens (type:/rating:/score:) drive GUI dimensions, not the
        # per-entry match — treat them as satisfied here.
        if term.field in _CONTROL_FIELDS:
            continue
        if term.field in _CURATION_FIELDS:
            if cur is None:
                cur = (
                    curation(entry.path) if curation is not None else (0, (), False)
                )
            present = _curation_present(term, cur[0], cur[1], cur[2])
        elif term.field in _NUMERIC_FIELDS:
            # Numeric fields (favorites) compare by value / range, not substring
            # — so ``favorites:5`` is exactly 5, not "contains 5" (#107).
            present = _match_numeric_field(
                entry, term.field, term.value, term.num
            )
        else:
            if term.field is None:
                if general is None:
                    parts = [entry.path.name, entry.title, " ".join(entry.tags)]
                    if entry.file_names and include_file_names:
                        parts.append(" ".join(entry.file_names))
                    general = " ".join(parts).casefold()
                hay = general
            else:
                hay = _FILTER_FIELD_GETTERS[term.field](entry)
            present = term.value in hay
        if term.or_group:
            # Pooled OR (never an exclusion — the parser clears or_group for ``-``
            # terms): defer the verdict until every term is scanned.
            or_seen = True
            or_hit = or_hit or present
            continue
        if term.exclude:
            if present:
                return False
        elif not present:
            return False
    if or_seen and not or_hit:
        return False
    return True


_MAX_TS = 1 << 62  # sentinel for "no posted_at" → push such entries to the end


def _sort_spec(mode: str, *, random_seed: int = 0, star_of=None):
    """Return ``(key_fn, reverse)`` for ``list.sort``.

    Entries without ``posted_at`` always sort to the end of the list,
    regardless of direction.

    ``star_desc`` keys on the user's 0–5 star rating supplied by *star_of*
    (``path -> star``; ``None`` treats everything as 0).  Un-starred entries
    (star 0) sink to the end regardless of direction, mirroring the posted_at /
    favorites handling; among starred entries higher stars sort first, ties
    broken by casefolded name for stability.

    ``size_asc`` / ``size_desc`` key on ``FolderEntry.size`` — populated for
    files by the shallow scandir pass (the same value the caption's byte-size
    display uses), so no extra I/O.  Folders carry ``size=0`` and therefore
    stay in a stable name-independent group of their own.

    ``random`` shuffles deterministically for a given *random_seed*: the key
    is a CRC32 of ``"{seed}:{path}"``, so the order is stable across rebuilds
    (filter tweaks, metadata batches) within a session and only changes when
    the caller supplies a new seed (``PostGrid.reshuffle_random_sort`` — F5).
    """

    def _name(e: FolderEntry) -> str:
        return (e.title or e.path.name).casefold()

    def _posted_key(e: FolderEntry) -> tuple[int, float]:
        if e.posted_at is None:
            return (1, 0.0)
        return (0, _posted_seconds(e.posted_at))

    def _fav_key(e: FolderEntry) -> tuple[int, int]:
        # Entries without a favorites count sort to the end regardless of
        # direction (group 1), mirroring the posted_at handling.
        if e.favorites is None:
            return (1, 0)
        return (0, e.favorites)

    if mode == "name_desc":
        return _name, True
    if mode == "posted_asc":
        return _posted_key, False
    if mode == "posted_desc":
        # Keep "no posted_at" group at the end by inverting only inside group.
        def _key(e: FolderEntry) -> tuple[int, float]:
            if e.posted_at is None:
                return (1, 0.0)
            return (0, -_posted_seconds(e.posted_at))

        return _key, False
    if mode == "favorites_asc":
        return _fav_key, False
    if mode == "favorites_desc":
        # Invert only inside the "known" group so unknowns stay at the end.
        def _fkey(e: FolderEntry) -> tuple[int, int]:
            if e.favorites is None:
                return (1, 0)
            return (0, -e.favorites)

        return _fkey, False
    if mode == "mtime_asc":
        return (lambda e: e.mtime), False
    if mode == "mtime_desc":
        return (lambda e: e.mtime), True
    if mode == "size_asc":
        return (lambda e: e.size), False
    if mode == "size_desc":
        return (lambda e: e.size), True
    if mode == "star_desc":
        def _star_key(e: FolderEntry) -> tuple[int, int, str]:
            star = star_of(e.path) if star_of is not None else 0
            if star <= 0:
                # Un-starred to the end; name-stable within that group.
                return (1, 0, (e.title or e.path.name).casefold())
            # Higher star first: negate so ascending sort puts 5 before 1.
            return (0, -int(star), (e.title or e.path.name).casefold())

        return _star_key, False
    if mode == "random":
        def _rand_key(e: FolderEntry) -> int:
            return zlib.crc32(
                f"{random_seed}:{e.path}".encode("utf-8", "surrogatepass")
            )

        return _rand_key, False
    return _name, False
