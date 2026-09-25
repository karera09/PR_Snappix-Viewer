"""User curation layer — read/write store for ``data/user_meta.db``.

This is the viewer's own **user-authored metadata**: per-entry star ratings
(0–5), free-form user tags, and an "watch later" flag.  Unlike the four
on-disk caches (``thumb_meta`` / ``thumb_disk`` / ``folder_preview`` /
``search_index``, all subclasses of :class:`~._sqlite_cache.SqliteCacheBase`),
this store is **not a cache** — it holds data the user created by hand that can
never be regenerated from the filesystem.  Two consequences follow, and they
are why this module reuses only the minimal
:class:`~._sqlite_cache.SqliteStoreBase` (connection / PRAGMAs / lock / the
migration-failure close guard) and NOT ``SqliteCacheBase``:

* **No size-budget prune, ever.**  ``SqliteCacheBase`` exists to LRU-evict the
  oldest rows once a byte budget is exceeded; applying that here would silently
  delete a user's stars.  There is no ``used_at`` column and no prune loop.
* **Immediate, durable commit.**  Every mutation commits synchronously so a
  crash / kill can't lose a star the user just set (the caches defer commits
  under bulk-write mode because their rows are rebuildable — this store is
  not).  For the same reason ``_PRAGMAS`` overrides the caches' WAL +
  ``synchronous=NORMAL`` to WAL + ``synchronous=FULL``: NORMAL skips the WAL
  fsync on commit, so an OS crash / power loss could still drop the most
  recent commits — acceptable for rebuildable caches, not here.  Writes are a
  handful per session (user actions), so the extra fsyncs are negligible.

Kept, deliberately, from the store base's design: WAL for concurrent reads, an
``RLock`` guarding a single shared connection (``check_same_thread=False``) so
a background ``resolve_moved_entries`` worker can run alongside the GUI
thread, and stdlib ``sqlite3`` only (Qt-free, so the CRUD logic unit-tests
without a ``QApplication``).

**Key design (the load-bearing part).**  A row is addressed by a **normalised
lookup key** (``path_key``, :func:`normalize_entry_key`) while the spelling the
user actually browsed with is kept beside it in ``path`` for display / ``stat``.
A raw ``str(Path)`` primary key would be a
BINARY (byte-exact) comparison in sqlite: opening the same library merely with
different letter case would produce a key that matched nothing and every star
would silently disappear.

The invariant that governs this key: **it is a pure function of the spelling**
— the same string always yields the same key, with no filesystem probe, no
drive-mapping lookup and no cached machine state anywhere in it.  A key that
can change with the machine's mood is strictly *worse* than no normalisation:
the row it addresses stops being stable, so one probe timeout during a NAS
reconnect writes a **second** row for the same folder, the grid's badge
(:class:`CurationMap`) and the info panel (:meth:`UserMetaStore.get`) then read
different rows, and — worst — the one-shot v1 → v2 upgrade would bake whichever
answer the machine happened to give that second into every key permanently.
Determinism first; folding more spellings only where the string decides it.

So the key folds exactly what *is* decidable from the string:

* separators / ``..`` / trailing slash → ``os.path.abspath``,
* letter case → ``os.path.normcase`` (Unicode-aware ``lower()`` on Windows,
  identity on POSIX where case really is significant — so a ``COLLATE NOCASE``
  column, which only folds ASCII, would have left 日本語 paths broken).

(``abspath`` resolves a *relative* spelling against the process cwd, so a
relative library root makes the key cwd-dependent — the one documented
exception to "nothing but the spelling"; see :func:`normalize_entry_key`.)

The **display** column goes through the same first step
(:func:`absolute_spelling`, ``abspath`` without the case fold), so
``normalize_entry_key(row.path) == row.path_key`` holds for every stored row.
That is not cosmetic: :meth:`UserMetaStore.load_all` builds the grid's
re-spelling index by normalising the *stored* spelling, so a relative one
would index under whatever the cwd was at load time and the badge would go
blank while the info panel still showed the star.

The invariant is only as good as the enumeration of **who writes that column**,
and there are exactly five writers — miss one and the split above comes back
on that path alone:

1. :meth:`UserMetaStore._merge` — every user curation write,
2. :meth:`UserMetaStore._fold_legacy_rows` — the one-shot v1 → v2 upgrade,
3. :meth:`UserMetaStore.resolve_moved_entries` — the rename-following
   worker's ``UPDATE entries SET path=?, path_key=?``, whose new spelling
   comes from a caller-side ``os.scandir`` walk of the library root and is
   therefore relative whenever the root is,
4. :meth:`UserMetaStore.rebind_path` — the user-driven 「現在の場所を指定…」
   repair (``viewer/curation_recovery.py``), whose new spelling
   comes from a file dialog and goes through the same
   :func:`absolute_spelling` / :func:`normalize_entry_key` pair,
5. :meth:`UserMetaStore.rebind_prefix` — the same repair applied to a whole
   **set of rows the caller has already proven unreachable**, whose new
   spellings :func:`rebase_spelling` derives from one picked folder (again
   through that same pair).

``tests/test_viewer_user_meta_path_keys.py`` pins the invariant per writer and
statically asserts that no fifth ``SET path=`` / ``INSERT`` appears without
going through :func:`absolute_spelling`.

The same rule governs the **v1 → v2 upgrade** (:meth:`UserMetaStore._migrate`):
it runs inside ``ViewerWindow.__init__`` holding ``BEGIN IMMEDIATE``, so it too
performs no filesystem probe of any kind — a fold that ``stat``-ed candidate
spellings would turn window construction into a stat storm on a half-mounted
share and block a second launch out of its own upgrade.

``Path.resolve()`` is deliberately not used: it is non-strict since 3.6, so it
silently does *not* normalise a path that no longer exists (a star would change
key the moment its folder went missing); it is a filesystem round-trip per tile
on the paint path; and on a dead share it blocks until the SMB timeout.
``os.path.abspath`` is the only syscall left (``GetFullPathNameW``,
~2 µs — it resolves against the process cwd and never touches the named entry),
so key computation can neither block nor fail on an offline volume.

**Deliberately NOT folded: a mapped drive letter vs. its UNC target**
(``Z:\\lib`` vs ``\\\\nas\\lib``).  That equivalence is not a property of
the two strings, it is a property of the machine's current ``net use`` /
``subst`` table — so a key that folded it would break the invariant above
(a 1 s probe timeout during
an SMB reconnect can produce ``('\\\\nas\\share\\lib', …)`` and ``('z:\\lib', …)``
as two live rows for one folder).  Opening the same library through both
spellings therefore still yields two independent sets of curation — exactly the
v1 behaviour for that axis, self-consistent and unchanged, rather than a new
failure mode.  Folding it *automatically* would need an identity that survives
re-mounting (a persisted alias table); what exists instead is the **manual**
repair: the cross-library list shows unreachable rows as
placeholder tiles (``viewer/curation_recovery.py``) and 「現在の場所を指定…」
lets the user re-point one via :meth:`UserMetaStore.rebind_path` — an explicit
per-row action, so the key itself stays pure.

That axis fails by the **volume**, though, not by the row: re-pointing a
drive letter at a new share orphans every row under it at once, and a repair
that only works one modal at a time is no repair at all for a few hundred of
them.  :meth:`UserMetaStore.rebind_prefix` is the same explicit action taken
once for a whole set: the caller names one old base, one new base and the
**exact rows** it wants moved, and gets one transaction.  It is still the
user's own explicit choice (no probing, no alias table, no automatic
following, and nothing the caller did not name moves), so the
key contract above is untouched — what changes is only how many modals the
user pays for the same answer.

Renames are a different axis and are handled separately: a writer-side
naming-pattern change *renames* post folders, which would orphan every star
keyed by the old path.  To follow a rename, a post folder's row also stores its
**postref** (``service`` + ``post_id`` read from ``post.md``); a file's row
stores its parent post's postref plus the file's name **relative to that post
folder**.  :func:`UserMetaStore.resolve_moved_entries` re-points rows whose
``path`` no longer exists onto the current folder that carries the same
postref — so curation survives a rename without the user re-doing anything.
(A folder with no ``post.md`` has no postref and is still not followed across a
rename — deliberately out of scope here.)

**User tags storage.**  A single ``user_tags`` TEXT column holds a
comma-separated list, mirroring the ``post.md`` ``- tags:`` convention
(``tagger``'s tags.db uses a separate row-per-tag table, but that's for a
millions-of-rows AI index queried by tag; user tags are a handful per entry, so
a delimited column keeps the store trivial).  :func:`split_user_tags` /
:func:`join_user_tags` are the single normalisation point (trim, drop empties,
de-dupe case-insensitively while preserving first-seen case).

**Best-effort / graceful degradation.**  A store that can't be opened
(read-only volume, locked file) degrades: :meth:`open_or_none` returns ``None``
and the viewer simply hides the curation UI — normal browsing is unaffected.
Because this is the only **non-regenerable** user data the viewer holds, a
failure NEVER deletes, truncates, quarantines, or recreates the file — a
corrupt ``user_meta.db`` is left on disk untouched so the user (or a later
successful open) can recover it.  Callers that need to *tell* the user why
curation is unavailable use :meth:`open_or_report`, which surfaces the reason
:meth:`open_or_none` drops on the floor.

**このファイルに残るもの / 出たもの.**  ストア :class:`UserMetaStore` と、行の
同一性を決めるキー計算（:func:`absolute_spelling` / :func:`normalize_entry_key`
/ :func:`rebase_spelling`）、★とタグの正規化（:func:`clamp_star` /
:func:`split_user_tags` / :func:`join_user_tags`）、描画側の索引
:class:`CurationMap`、行の併合規約 :func:`_merge_curation_fields` はここに残る。
**キー経路をここから出せない**のは上の静的検査の帰結で、
``tests/test_viewer_user_meta_path_keys.py`` は呼び出し閉包が user_meta.py の中
で閉じることを要求する（呼び先が別モジュールにあると閉包走査が本体を一度も
検査しないまま素通りする — 第 5 ラウンドの変異 C）。``clamp_star`` /
``split_user_tags`` / ``join_user_tags`` も :meth:`UserMetaStore.load_all` →
``_row_to_meta`` 経由でその閉包の中にある。

出たのは**パスを解決する側**で、置き場は
:mod:`~snappix.viewer.user_meta_parts.resolve`: 到達性の判定
（``_is_gone_exc`` / ``_is_definitely_gone`` / ``_anchor_reachable``）、改名
追従の台帳（:class:`MovedResolver` / :func:`build_moved_resolver`）、横断一覧の
パス解決（:class:`CurationResolve` / :func:`resolve_curation_paths` /
:func:`build_curation_entry`）。このファイルは従来の名前を全てそこから
re-export するので、外から見た import 面（``from .user_meta import
resolve_curation_paths`` 等）は分割前と変わらない。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ._sqlite_cache import SqliteStoreBase
from .user_meta_parts.resolve import (
    CurationResolve,
    MovedResolver,
    _anchor_reachable,
    _curation_display_name,
    _is_definitely_gone,
    _is_gone_exc,
    build_curation_entry,
    build_moved_resolver,
    resolve_curation_paths,
)

DB_NAME = "user_meta.db"

#: ``PRAGMA user_version`` of the current schema.  1 = a raw ``str(path)``
#: primary key; 2 = normalised ``path_key`` + display ``path``.
#:
#: (A v3 re-key would be cheap and safe if keys ever needed repair — re-key
#: only the rows where ``path_key != normalize_entry_key(path)``, which touches
#: no filesystem, is deterministic and idempotent, and is a complete no-op on a
#: correct DB.  With nothing to repair it would only make every user re-read
#: their whole curation table once, so the version stays 2.)
SCHEMA_VERSION = 2

#: Valid star range.  0 means "no star" (the row may still carry tags / later).
MIN_STAR = 0
MAX_STAR = 5

#: How many times the one-shot v1 → v2 upgrade may take the write lock.  Two =
#: the first try plus one retry, for the second copy of a portable build racing
#: us through the same upgrade (see :meth:`UserMetaStore._migrate`).
_MIGRATE_ATTEMPTS = 2
#: Pause before that retry.  Short: ``busy_timeout`` has already waited out the
#: lock, this only lets the winner's commit settle.
_MIGRATE_RETRY_DELAY = 0.2
#: Wall-clock ceiling (seconds) on **everything** the upgrade may spend waiting
#: for the write lock, retry and pause included.  Without it the retry would
#: double the worst case: ``busy_timeout`` (5 s, pinned by ``SqliteStoreBase``)
#: per attempt plus the pause = 11.2 s of a frozen ``ViewerWindow.__init__``
#: (measured) — a startup freeze this store must never cause.  5 s keeps
#: the pre-retry worst case while leaving the retry its real job: a genuine
#: contended launch loses the lock only for as long as the *winner's own*
#: upgrade takes (milliseconds on a normal store), never for seconds.
#:
#: **The other side of that trade, spelled out.**  The budget is spent by the
#: *first* attempt's ``busy_timeout``, so a first attempt that waits the whole
#: 5 s and still loses leaves the retry a ``busy_timeout`` of 0: it does not
#: wait at all, fails ``database is locked`` immediately, and the exception
#: leaves ``__init__`` — ``open_or_none`` / ``open_or_report`` then return
#: ``None`` and **that session has no curation UI at all** (stars are neither
#: shown nor settable until the viewer is restarted).  This is chosen over the
#: alternative, because the alternative is a window that never appears: the
#: only way to make the retry meaningful in that case is to keep waiting, and
#: the waiting happens on the GUI thread inside ``ViewerWindow.__init__``.  It
#: costs a session's curation only in a case that already means "another
#: process has held the write lock for 5 s during its own one-shot upgrade",
#: and the DB is left untouched, so the next launch migrates normally.
_MIGRATE_LOCK_BUDGET = 5.0


def _is_locked_error(exc: BaseException) -> bool:
    """True for sqlite's "another connection holds the write lock" errors.

    sqlite reports both ``SQLITE_BUSY`` and ``SQLITE_LOCKED`` as
    ``OperationalError`` with only the message to tell them apart from a real
    failure (a full disk, a corrupt page), so the text is the discriminator —
    deliberately narrow, since everything else must NOT be retried.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def clamp_star(value: int) -> int:
    """Clamp *value* into ``[MIN_STAR, MAX_STAR]``."""
    return max(MIN_STAR, min(MAX_STAR, int(value)))


#: The characters :func:`split_user_tags` treats as separators (comma incl.
#: the Japanese IME forms 「、」「，」 + whitespace), exposed as a regex so the
#: edit dialog's per-token autocomplete (``post_grid._UserTagCompleter``) can decide
#: "where does the token under the cursor start" by the **same** rule instead of
#: growing a second, quietly-diverging copy of the delimiter contract.
USER_TAG_SEPARATOR_RE = re.compile(r"[\s,、，]")


def split_user_tags(text: str) -> list[str]:
    """Parse a comma/whitespace-delimited user-tag string into a clean list.

    Trims each token, drops empties, and de-dupes case-insensitively while
    preserving the first-seen spelling.  Both commas and whitespace separate
    tags so ``"風景, 構図参考 後で印刷"`` yields three tags — matching what the
    edit dialog's placeholder promises.
    """
    raw = [tok for tok in USER_TAG_SEPARATOR_RE.split(text) if tok]
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        tok = tok.strip()
        if not tok:
            continue
        key = tok.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def join_user_tags(tags: list[str]) -> str:
    """Serialise a tag list back to the stored comma-separated form."""
    return ",".join(split_user_tags(",".join(tags)))


# ------------------------------------------------------------- key normalising


def absolute_spelling(path: "Path | str") -> str:
    """*path* made absolute — the **display** half of the key contract.

    :func:`normalize_entry_key` is exactly ``normcase(absolute_spelling(p))``,
    so every spelling the store *stores* (the ``path`` column, and therefore
    the keys of the :class:`CurationMap` the grid paints from) goes through
    this same first step.  ``normalize_entry_key(row.path) == row.path_key``
    then holds for every stored row — but only because **all five** writers of
    that column call this (:meth:`UserMetaStore._merge`,
    :meth:`UserMetaStore._fold_legacy_rows`,
    :meth:`UserMetaStore.resolve_moved_entries`,
    :meth:`UserMetaStore.rebind_path` and
    :meth:`UserMetaStore.rebind_prefix`); it is an invariant maintained
    by enumeration, not one the type system enforces, so a new writer must be
    added to that list (see the module docstring).  The grid quietly depends
    on it: :meth:`UserMetaStore.load_all` re-derives the map's side index from the
    stored spelling, so a stored *relative* spelling would index under whatever
    the cwd happened to be at ``load_all`` time and stop matching the row's own
    key — the info panel (:meth:`UserMetaStore.get`, which normalises the
    caller's absolute path) would show the star while the tile's badge showed
    nothing.  Reachable: ``_dispatch`` accepts a relative positional argument /
    ``--root``, ``viewer/app.py`` passes it through, ``os.scandir`` then spells
    every tile relatively, and ``main_window`` persists it as ``last_root``.

    ``abspath`` is the only syscall the key contract admits (see
    :func:`normalize_entry_key`), so absolutising here costs the display column
    nothing it was not already paying — and never raises.
    """
    raw = str(path)
    if not raw:
        return raw
    try:
        # GetFullPathNameW on Windows / a string join + normpath on POSIX.
        # Resolves against the process cwd; it never looks at the named entry,
        # so an offline share costs nothing here.
        return os.path.abspath(raw)
    except (OSError, ValueError):  # pragma: no cover (defensive)
        return raw


def normalize_entry_key(path: "Path | str") -> str:
    """The store's lookup key for *path* — see the module docstring.

    **Deterministic by contract**: the returned key depends on *nothing but*
    the spelling handed in.  It performs no filesystem access, starts no
    thread, consults no cache and cannot fail on an unreachable volume, so
    the same folder always addresses the same row whatever state the
    machine's shares / mappings are in.  That is the whole point: this key is
    a row *identity* for the only non-regenerable data the viewer holds, and
    an identity that flickers with the network duplicates rows (see the
    module docstring for the two-row DB this rule comes from).

    **The one exception: a relative spelling.**  ``abspath`` completes it
    against the **process cwd**, so ``lib\\post`` keys differently depending
    on where the viewer was launched from.  This is reachable — ``_dispatch``
    accepts a relative positional argument / ``--root`` and ``viewer/app.py``
    passes it through unmodified, after which ``os.scandir`` propagates the
    relative spelling to every tile — and it cannot be fixed here: making a
    relative path absolute *is* asking the cwd.  It is bounded, though: the
    cwd never changes inside a running viewer (nothing calls ``os.chdir``),
    so within one session the key is still a pure function of the spelling,
    and two sessions launched the same way agree.  Only "same relative root,
    different working directory" splits the rows — the v1 behaviour for that
    axis (v1 keyed on the raw relative string, which split even *more*), not
    a regression.  Absolutising the root at the launch seam is the real fix
    and belongs in ``viewer/app.py``.

    Folds separators / ``..`` / a trailing slash (``os.path.abspath``) and
    letter case on Windows (``os.path.normcase``).  It does **not** fold a
    mapped drive letter against its UNC target — that is machine state, not a
    property of the string.  Never raises: a caller writing a star must always
    be able to compute a key.

    How this is held in place (``tests/test_viewer_user_meta_path_keys.py``),
    and — honestly — how far each layer reaches:

    * the **output** is pinned by golden constants: any fold that actually
      differs on the running machine fails, whatever route it took.  Blind to a
      machine where the extra state happens to be absent (an unmapped ``Z:``
      makes a ``winreg`` implementation the identity function);
    * the **source** is checked statically: the key path's call closure may
      not name ``winreg`` / ``ctypes`` / ``realpath`` / ``stat`` / ``environ``
      …, may not import anything, and **must stay inside this module** (a
      callee one file over is exactly how the previous version of that check
      was walked around).  Blind to names built at runtime and to anything
      inside a dependency;
    * a list of **runtime entry points** is spied on, in this process and in a
      pristine child.  Convenient early warning, and openly not exhaustive.

    None of the three is a proof.  Together they mean that writing a
    machine-state read into this function the way one would naturally write it
    fails the suite; a deliberate, obfuscated one can still get through.
    """
    return os.path.normcase(absolute_spelling(path))


def rebase_spelling(
    path: "Path | str", old_base: "Path | str", new_base: "Path | str",
) -> str | None:
    """*path* の根を *old_base* から *new_base* へ付け替えた**絶対綴り**。

    ``Z:\\lib\\creatorA\\post`` を ``old_base=Z:\\lib`` /
    ``new_base=\\\\nas\\lib`` で呼べば ``\\\\nas\\lib\\creatorA\\post``。
    *path* が *old_base* の下に無ければ ``None``（呼び出し側が「当たらな
    かった行」を数えられるように、黙って素通しにしない）。

    :meth:`UserMetaStore.rebind_prefix` と、その確認ダイアログのために
    「何件が既存行へ併合されるか」を**メモリ上の地図だけ**で数える
    ``viewer/curation_recovery.py`` の両方がこの 1 実装を使う — 前者だけが
    知っている綴り算術を後者が手で真似すると、確認ダイアログの件数と実際に
    動く行が静かにズレる。

    照合は :func:`normalize_entry_key` と**同じ畳み方**（要素ごとの
    ``os.path.normcase``）で行うので、鍵が同じ綴り対は必ずここでも同じ根と
    判定される。要素単位で比べるのは、文字列前方一致が ``Z:\\lib`` を
    ``Z:\\library`` の根と読んでしまうため（``normcase`` は Unicode の
    ``lower()`` なので長さが変わり得る点も、要素比較なら関係しない）。

    純文字列 — FS には一切触らない（:func:`absolute_spelling` の
    ``abspath`` だけ）。到達不能なボリュームの綴りを渡されてもブロックしない。
    """
    base_abs = absolute_spelling(old_base)
    dest_abs = absolute_spelling(new_base)
    if not base_abs or not dest_abs:
        return None
    base_parts = Path(base_abs).parts
    parts = Path(absolute_spelling(path)).parts
    if len(parts) < len(base_parts):
        return None
    head = [os.path.normcase(p) for p in parts[:len(base_parts)]]
    if head != [os.path.normcase(p) for p in base_parts]:
        return None
    return absolute_spelling(
        os.path.join(dest_abs, *parts[len(base_parts):])
    )


@dataclass(frozen=True)
class UserMeta:
    """One entry's user curation record.

    ``star`` 0 / empty ``tags`` / ``later`` False is the "absent" state — a row
    all-absent is pruned on write (see :meth:`UserMetaStore.set_*`) so an empty
    row never lingers.
    """

    star: int = 0
    tags: tuple[str, ...] = ()
    later: bool = False

    def is_empty(self) -> bool:
        return self.star == 0 and not self.tags and not self.later


_EMPTY = UserMeta()


class CurationMap(dict[str, UserMeta]):
    """``{spelling-as-written: UserMeta}`` whose lookups tolerate re-spellings.

    :meth:`UserMetaStore.load_all` hands the grid a plain dict that it consults
    while painting (``post_grid.user_meta_for``) and iterates to build the
    cross-library 「スター付き一覧」 pool.  Those two uses want *different* keys:
    the pool wants the real on-disk spelling (it is ``stat``-ed and shown), the
    paint lookup wants the normalised one (the tile's path may be spelled
    differently from the session that set the star).

    So the dict keeps the display spelling as its key — iteration / ``items``
    are unchanged — and carries a side index from
    :func:`normalize_entry_key` to it.  A lookup tries the exact key first
    (the common case: same spelling, zero extra work) and only then normalises.
    An empty map short-circuits entirely, so a library with no curation at all
    pays nothing per tile.

    Only the accessors the viewer actually uses are index-aware
    (``get`` / ``[]`` / ``in`` / ``pop`` / ``del`` / ``clear`` / assignment).
    The bulk mutators / copies that would route *around* the index —
    ``update`` / ``setdefault`` (a second key for one entry) and ``copy`` /
    ``__or__`` (a plain dict, index silently gone, byte-exact lookups back) —
    raise :class:`NotImplementedError` instead of being half-maintained: this
    class exists because a silent fall back to byte-exact keys loses stars.
    ``dict(m)`` / ``{**m}`` cannot be intercepted and are the remaining hole;
    they are a plain snapshot and must not be fed back to the viewer as a
    curation map.
    """

    def __init__(self, items=()) -> None:
        super().__init__()
        self._by_key: dict[str, str] = {}
        for display, meta in items:
            self[display] = meta

    # -- internal ---------------------------------------------------------

    def _resolve(self, key):
        """The stored (display) key for *key*, or *key* itself when unknown."""
        if dict.__contains__(self, key):
            return key
        if not self._by_key or not isinstance(key, (str, Path)):
            return key
        return self._by_key.get(normalize_entry_key(key), key)

    # -- dict surface -----------------------------------------------------

    def __setitem__(self, key, value) -> None:
        if not dict.__contains__(self, key):
            norm = normalize_entry_key(key)
            existing = self._by_key.get(norm)
            if existing is not None and dict.__contains__(self, existing):
                # Same entry under another spelling.  Keep ONE key — and let it
                # be the spelling just written, mirroring what the store does
                # to the ``path`` column on every write (``_merge``).  The two
                # sides used to disagree: the store refreshed ``path`` to the
                # current mount while this map kept the older spelling, so the
                # cross-library pool (which ``stat``s these keys) could go on
                # showing a spelling the store had already abandoned.
                dict.__delitem__(self, existing)
            self._by_key[norm] = key
        dict.__setitem__(self, key, value)

    def clear(self) -> None:
        self._by_key.clear()
        dict.clear(self)

    # -- routes around the index: refuse rather than half-maintain --

    def _unsupported(self, name: str) -> "NotImplementedError":
        return NotImplementedError(
            f"CurationMap.{name}() bypasses the re-spelling index; "
            "use item assignment / pop, or rebuild via UserMetaStore.load_all()"
        )

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        raise self._unsupported("update")

    def setdefault(self, *args, **kwargs):  # type: ignore[override]
        raise self._unsupported("setdefault")

    def copy(self) -> "CurationMap":
        raise self._unsupported("copy")

    def __or__(self, other):
        raise self._unsupported("__or__")

    def __ror__(self, other):
        raise self._unsupported("__ror__")

    def __ior__(self, other):
        raise self._unsupported("__ior__")

    def __getitem__(self, key):
        return dict.__getitem__(self, self._resolve(key))

    def __contains__(self, key) -> bool:
        return dict.__contains__(self, self._resolve(key))

    def get(self, key, default=None):
        return dict.get(self, self._resolve(key), default)

    def __delitem__(self, key) -> None:
        stored = self._resolve(key)
        dict.__delitem__(self, stored)
        self._by_key.pop(normalize_entry_key(stored), None)

    def pop(self, key, *default):
        stored = self._resolve(key)
        if dict.__contains__(self, stored):
            self._by_key.pop(normalize_entry_key(stored), None)
        return dict.pop(self, stored, *default)


def _merge_curation_fields(src, dst):
    """Fold two rows that name **the same entry** into one, losing nothing.

    Three call sites resolve the very same collision — "one object, two rows,
    both non-regenerable": :meth:`UserMetaStore._fold_legacy_rows` (a v1 spelling
    pair), :meth:`UserMetaStore.rebind_path` (the manual recovery flow) and
    :meth:`UserMetaStore.resolve_moved_entries` (rename-following landing on a
    destination the user already curated).  They must answer it identically, so
    the rule lives here once — see :meth:`UserMetaStore._fold_legacy_rows` for
    why merging, not picking a winner, is the only admissible answer:

    * ``star`` — the **highest** wins (an unrated row, star 0, can never erase
      a rating made under the other spelling).
    * ``later`` — logical **OR**.
    * ``user_tags`` — **union**, destination spellings first (the same rule
      :func:`split_user_tags` already applies within one field).
    * postref (``service`` / ``post_id`` / ``rel_name``) — taken as a **group**
      from the destination when it carries one (whoever scanned the object
      that is there *now* wrote it last), otherwise from the source.  Never
      field by field: a ``rel_name`` only means anything against its own post.

    *src* and *dst* are both ``(star, user_tags, later, service, post_id,
    rel_name)`` rows; returns the merged tuple in that same order, with
    ``later`` already as the 0/1 sqlite stores.  Pure — no FS, no sqlite.
    """
    s_star, s_tags, s_later, s_svc, s_pid, s_rel = src
    d_star, d_tags, d_later, d_svc, d_pid, d_rel = dst
    star = max(clamp_star(s_star or 0), clamp_star(d_star or 0))
    tags = join_user_tags(
        split_user_tags(str(d_tags or "")) + split_user_tags(str(s_tags or ""))
    )
    later = 1 if (s_later or d_later) else 0
    if d_svc is not None or d_pid is not None:
        svc, pid, rel = d_svc, d_pid, d_rel
    else:
        svc, pid, rel = s_svc, s_pid, s_rel
    return star, tags, later, svc, pid, rel


@dataclass(frozen=True)
class WriteOutcome:
    """Result of a curation write: the record reflected back plus whether it
    was actually persisted.

    ``ok`` is ``False`` when the write could not be committed (read-only
    volume, disk full, an antivirus lock) or when the pre-write read failed.
    In that case ``meta`` is the record still **on disk** — the pre-change
    value, NOT the value the store failed to persist — so a caller that ignores
    ``ok`` still shows the true persisted state (the historical behaviour of
    :meth:`UserMetaStore.set_star` & friends is preserved).  A caller that
    inspects ``ok`` can warn the user that the star/tag did not stick instead
    of the silent "★★★" success toast the plain setters cannot avoid.
    """

    meta: UserMeta
    ok: bool = True


class UserMetaStore(SqliteStoreBase):
    """Read/write access to ``<data_dir>/user_meta.db``.

    Construct via :meth:`open_or_none` (best-effort) — the raw constructor
    raises ``sqlite3.Error`` if the file can't be opened / migrated (the base
    closes the connection handle before re-raising, so a failed open never
    leaves the file locked on Windows).

    Inherits only the minimal :class:`~._sqlite_cache.SqliteStoreBase`
    (connection / lock / close) — none of ``SqliteCacheBase``'s LRU / prune /
    deferred-commit machinery, see the module docstring.  ``synchronous`` is
    overridden to ``FULL`` because this data is non-regenerable: a commit must
    survive an OS crash / power loss, not just a process kill.
    """

    _PRAGMAS = (
        ("journal_mode", "WAL"),
        ("synchronous", "FULL"),
    )

    # ----------------------------------------------------------- lifecycle

    @classmethod
    def open_or_none(cls, data_dir: Path) -> "UserMetaStore | None":
        """Open (creating if absent) ``<data_dir>/user_meta.db``, or ``None``.

        Unlike ``tags.db`` (a read-only external index that must already
        exist), this store is the viewer's own writable data, so it is created
        on first use.  Any failure (read-only volume, locked file, odd volume)
        degrades to ``None`` — the viewer hides the curation UI and browsing is
        unaffected.  See :meth:`open_or_report` when the caller needs the reason
        to warn the user (this thin wrapper keeps the silent-``None`` contract).
        """
        return cls.open_or_report(data_dir)[0]

    @classmethod
    def open_or_report(
        cls, data_dir: Path,
    ) -> "tuple[UserMetaStore | None, str | None]":
        """Open the store, returning ``(store, None)`` or ``(None, reason)``.

        The counterpart to :meth:`open_or_none` that does **not** discard the
        failure reason.  ``user_meta.db`` holds the only non-regenerable user
        data (stars / user tags / 「あとで見る」), so a failure is handled very
        differently from the four rebuildable caches: the corrupt file is
        **never** deleted, truncated, quarantined, or recreated — it is left on
        disk exactly as found so the user (or a later successful open) can
        recover it.  Instead the reason (``str(exc)``) is returned so the caller
        can surface a one-line "キュレーションデータを開けませんでした（…）" notice
        rather than silently presenting an empty curation surface that looks
        like every star was lost.

        Both ``sqlite3.Error`` (corrupt / locked DB) and ``OSError`` (a
        read-only volume that fails the ``data/`` ``mkdir``, an odd volume) route
        through the same degradation seam.

        **The open is verified by reading every row once** (:meth:`load_all`).
        Connecting, the PRAGMAs and :meth:`_migrate` only touch the header /
        ``sqlite_master``, so a file whose ``entries`` pages alone are damaged
        used to open "successfully": the grid's ``load_all`` then failed, came
        back empty, and the curation UI stayed *enabled* over an empty map —
        exactly the "every star vanished" surface this method exists to avoid,
        while writes through the intact PK index kept appending to the damaged
        file.  The table is small (one row per curated entry), and the full scan
        is what the grid does right after anyway.
        """
        try:
            store = cls(data_dir / DB_NAME)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("user_meta.db unavailable ({}); curation off", exc)
            return None, str(exc)
        try:
            store.load_all()
        except sqlite3.Error as exc:
            store.close()
            logger.warning("user_meta.db unreadable ({}); curation off", exc)
            return None, str(exc)
        return store, None

    def _migrate(self) -> None:
        """Create / upgrade the schema to :data:`SCHEMA_VERSION`.

        v1 keyed rows by the raw ``str(path)``; v2 keys them by
        :func:`normalize_entry_key` and keeps the written spelling in ``path``
        for display.  The upgrade folds every old row through the new key, so
        rows that only differed by spelling **merge** rather than one silently
        winning (see :meth:`_fold_legacy_rows` for the merge rules).

        The whole upgrade runs in one explicit transaction: sqlite DDL is
        transactional, so a failure anywhere (a crash mid-fold, a full disk)
        rolls back to the untouched v1 table — this is the only non-regenerable
        user data the viewer holds, and a half-migrated table would lose stars.
        The exception then propagates out of ``__init__``, where
        ``SqliteStoreBase`` closes the handle before re-raising, and the caller
        degrades to "curation unavailable (reason)" with the file intact.

        **Contended first run.**  A portable build can be launched twice from
        the same folder, and then two processes reach this upgrade at once.  The
        loser used to take ``database is locked`` straight out of ``__init__``
        and that window lost curation entirely for the session (:meth:`get`
        works on no store at all — ``open_or_report`` returned ``(None, …)``).
        ``busy_timeout`` (5 s, pinned by ``SqliteStoreBase``) covers waiting for
        the write lock, but the winner can also finish *after* our wait expires
        anywhere inside its transaction, so a locked failure is retried once —
        and the retry re-reads ``user_version`` **inside** the new transaction,
        because by then the work may simply be done (nothing to redo).  That
        retry is also what makes the explicit ``rollback`` load-bearing rather
        than decorative: without it the connection would still be inside the
        failed transaction and the second ``BEGIN IMMEDIATE`` could not start.

        **The retry may not cost startup time.**  This runs on the GUI thread
        inside ``ViewerWindow.__init__``, and a second attempt at the pinned
        5 s ``busy_timeout`` simply doubled the worst-case freeze.  So the
        whole sequence — both waits and the pause between them — is capped at
        :data:`_MIGRATE_LOCK_BUDGET` by lowering ``busy_timeout`` to whatever
        is left of it before each attempt, and the connection's pinned value
        is restored afterwards so nothing else inherits the shortened wait.
        """
        with self._lock:
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            if version >= SCHEMA_VERSION:
                return
            (pinned,) = self._conn.execute("PRAGMA busy_timeout").fetchone()
            deadline = time.monotonic() + _MIGRATE_LOCK_BUDGET
            try:
                for attempt in range(_MIGRATE_ATTEMPTS):
                    left_ms = max(int((deadline - time.monotonic()) * 1000), 0)
                    self._conn.execute(f"PRAGMA busy_timeout={left_ms}")
                    try:
                        self._upgrade_to_v2()
                        return
                    except BaseException as exc:
                        try:
                            self._conn.rollback()
                        except sqlite3.Error:  # pragma: no cover (defensive)
                            pass
                        last = attempt == _MIGRATE_ATTEMPTS - 1
                        if last or not _is_locked_error(exc):
                            raise
                        logger.info(
                            "user_meta: upgrade contended ({}); retrying once",
                            exc,
                        )
                        time.sleep(_MIGRATE_RETRY_DELAY)
            finally:
                try:
                    self._conn.execute(f"PRAGMA busy_timeout={int(pinned)}")
                except sqlite3.Error:  # pragma: no cover (defensive)
                    pass

    def _upgrade_to_v2(self) -> None:
        """One attempt at the v1 → v2 upgrade (caller rolls back on failure)."""
        self._conn.execute("BEGIN IMMEDIATE")
        # Re-read the state INSIDE the transaction.  Between our pre-check and
        # this point another process may have completed the very same upgrade
        # (the contended-launch case above); acting on the stale answer would
        # rename the *new* table to entries_v1 and fold it through itself.
        # ``user_version`` is only bumped in the same transaction as the DDL,
        # but a v1 store created before that bump landed (or an interrupted
        # upgrade) can still show version 0 with a legacy table present — so
        # both are consulted, and the table wins.
        (version,) = self._conn.execute("PRAGMA user_version").fetchone()
        legacy = self._has_legacy_entries()
        if version >= SCHEMA_VERSION and not legacy:
            self._conn.rollback()  # nothing to do — release the write lock
            return
        if legacy:
            self._conn.execute("ALTER TABLE entries RENAME TO entries_v1")
            # The index follows the renamed table and would collide with the
            # new one (index names share one namespace).
            self._conn.execute("DROP INDEX IF EXISTS idx_entries_postref")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                path_key   TEXT PRIMARY KEY,
                path       TEXT    NOT NULL,
                star       INTEGER NOT NULL DEFAULT 0,
                user_tags  TEXT    NOT NULL DEFAULT '',
                later      INTEGER NOT NULL DEFAULT 0,
                service    TEXT,
                post_id    TEXT,
                rel_name   TEXT
            )
            """
        )
        # Postref lookup for resolve_moved_entries (rename following).
        # A post folder has rel_name IS NULL; a file inside a post has
        # rel_name = its name relative to the post folder.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_postref "
            "ON entries(service, post_id)"
        )
        if legacy:
            self._fold_legacy_rows()
            self._conn.execute("DROP TABLE entries_v1")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def _has_legacy_entries(self) -> bool:
        """True when a v1 ``entries`` table (no ``path_key``) is present."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
        ).fetchone()
        if row is None:
            return False
        cols = {
            str(r[1])
            for r in self._conn.execute("PRAGMA table_info(entries)").fetchall()
        }
        return "path_key" not in cols

    def _fold_legacy_rows(self) -> None:
        """Copy ``entries_v1`` into the v2 table, folding re-spelled duplicates.

        Two v1 rows can normalise to the same key (the very case this migration
        exists for: ``Z:\\lib\\p`` and ``\\\\nas\\lib\\P`` were two rows, each
        holding half the user's curation).  Neither may win outright — both are
        non-regenerable — so they merge, field by field, in a way that can only
        ever *keep* what the user expressed:

        * ``star`` — the **highest** wins (the "OR" of a 0–5 scale: an unrated
          spelling, star 0, can never erase a rating made under another).
        * ``later`` — logical **OR** (flagged anywhere ⇒ flagged).
        * ``user_tags`` — **union**, first-seen spelling kept (the same rule
          :func:`split_user_tags` already applies within one field).
        * postref (``service`` / ``post_id`` / ``rel_name``) — the first row
          that carries one wins; a later NULL never clears it.  These three
          travel together (a ``rel_name`` only means anything against its own
          post), so they are taken as a **group**, never field by field.
        * ``path`` (the display spelling) — **the first row in insertion
          order, unconditionally.  No filesystem probe decides this.**

        That last rule deliberately takes no ``stat`` per candidate spelling
        ("prefer the spelling that still answers").
        Such a probe costs what the key's own purity
        rule exists to forbid, one layer up: this fold runs inside
        ``ViewerWindow.__init__`` while holding ``BEGIN IMMEDIATE``, and
        ``os.stat`` on a dead share has no timeout — a single one can take
        15 s or more, so a 200-group store turns
        into 30 s or more of frozen window construction plus a second launch that
        dies on the held write lock and loses its whole session's curation.
        A capped *call count* bounds neither.

        And it would buy **nothing**: within one fold group the probe could never
        change the answer, because *there is no dead spelling to avoid*.
        The key folds precisely ``abspath`` + ``normcase``, and on Windows that
        is exactly the normalisation Win32 applies to a path **before** it
        reaches the filesystem, so every absolute spelling in one group names
        the same object and they live or die together (measured 2026-08-30 —
        ``…\\live``, ``…\\NOPE\\..\\live`` with a non-existent middle segment,
        a trailing separator, upper-cased, and ``/``-separated all ``stat`` OK
        against one directory).  On POSIX ``normcase`` is the identity, so
        after :func:`absolute_spelling` a group holds one spelling and there is
        nothing to choose between at all.  The "prefer the spelling that still
        answers" rule therefore always re-elected the first-seen row anyway:
        both candidates rank equal, first come wins, 100 % identical output —
        for a per-spelling ``os.stat`` on the GUI thread under the write lock.

        (The earlier justification here — "a stale caption is refreshed by the
        next :meth:`_merge` or by :meth:`resolve_moved_entries`" — was wrong
        about the second half and is not what makes this safe.  A folder with
        no ``post.md`` never enters ``resolve_moved_entries`` at all: its
        candidate query is ``WHERE service IS NOT NULL AND post_id IS NOT
        NULL``.  And a row that *does* carry a postref is excluded a second
        time by that method's ``new_key == old_key`` guard, which is exactly
        the "same object, different spelling" case.  ``_merge`` does refresh
        ``path``, but only if the user curates that entry again.)

        Insertion order also keeps the fold **reproducible**: the same v1 DB
        always upgrades to the same v2 DB, which is the property that makes
        this one-shot, un-undoable pass reviewable at all.  That is why the
        read below is ``ORDER BY rowid`` and not merely whatever order sqlite
        happens to scan a rowid table in.
        """
        rows = self._conn.execute(
            "SELECT path, star, user_tags, later, service, post_id, rel_name "
            "FROM entries_v1 ORDER BY rowid"
        ).fetchall()
        merged: dict[str, list] = {}
        pathless = 0
        for path, star, tags, later, service, post_id, rel_name in rows:
            # Absolutised on the way in, exactly as ``_merge`` does — a v1 row
            # written from a relative ``--root`` would otherwise carry a
            # cwd-dependent display spelling into v2 (see
            # :func:`absolute_spelling`).  ``abspath`` of "" is still "", so the
            # unaddressable-row check below is unaffected.
            display = absolute_spelling(str(path or ""))
            if not display:
                # A v1 row with an empty / NULL path names nothing, so it cannot
                # be carried over — and entries_v1 is dropped right after this,
                # making it the one place the store destroys user data.  Never
                # silently: say how much and what it was worth.
                pathless += 1
                continue
            key = normalize_entry_key(display)
            entry = merged.get(key)
            if entry is None:
                merged[key] = [
                    [display],
                    clamp_star(star or 0),
                    split_user_tags(str(tags or "")),
                    bool(later),
                    service,
                    post_id,
                    rel_name,
                ]
                continue
            if display not in entry[0]:
                entry[0].append(display)
            entry[1] = max(entry[1], clamp_star(star or 0))
            entry[2] = split_user_tags(
                ",".join([*entry[2], *split_user_tags(str(tags or ""))])
            )
            entry[3] = entry[3] or bool(later)
            if entry[4] is None and entry[5] is None:
                entry[4], entry[5], entry[6] = service, post_id, rel_name
        if pathless:
            logger.warning(
                "user_meta: dropped {} v1 row(s) with no path on upgrade "
                "(unaddressable — star/tags could not be carried over)",
                pathless,
            )
        if not merged:
            return
        payload = []
        for key, entry in merged.items():
            spellings, star, tags, later, service, post_id, rel_name = entry
            # First seen wins — see the docstring: no ``stat`` may run here.
            display: str = spellings[0]
            payload.append((
                key, display, star, join_user_tags(tags),
                1 if later else 0, service, post_id, rel_name,
            ))
        self._conn.executemany(
            "INSERT INTO entries "
            "(path_key, path, star, user_tags, later, service, post_id, rel_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        folded = sum(len(e[0]) for e in merged.values()) - len(merged)
        if folded:
            logger.info(
                "user_meta: merged {} re-spelled duplicate row(s) on upgrade",
                folded,
            )

    # ``close`` comes from SqliteStoreBase.

    # --------------------------------------------------------------- reads

    def get(self, path: Path | str) -> UserMeta:
        """Return the curation record for *path* (``_EMPTY`` when absent).

        *path* may be spelled any way that names the same entry — the store
        normalises it (:func:`normalize_entry_key`) before looking it up.
        """
        try:
            return self._get_raising(normalize_entry_key(path))
        except sqlite3.Error as exc:  # pragma: no cover (defensive)
            logger.debug("user_meta get failed: {}", exc)
            return _EMPTY

    def _get_raising(self, key: str) -> UserMeta:
        """:meth:`get` without the ``_EMPTY`` error fallback.

        ``_merge`` must NOT mistake a failed read for "no record": merging onto
        ``_EMPTY`` and then writing would overwrite the user's existing
        star/tags/later with an unknown base.  Write paths use this and abort
        on error; the public :meth:`get` keeps its display-friendly fallback.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT star, user_tags, later FROM entries WHERE path_key=?",
                (key,),
            ).fetchone()
        return _row_to_meta(row)

    def load_all(self) -> CurationMap:
        """Return the whole table as ``{path: UserMeta}`` (one query).

        The grid consults curation synchronously while painting, so the window
        loads the entire store into an in-memory dict once (small — one row per
        curated entry, never the whole library) and looks up there.  This keeps
        the paint path off sqlite entirely, honouring the "no synchronous I/O
        that could touch the NAS while drawing" rule (the DB is local ``data/``,
        but a per-tile query per repaint is still needless overhead).

        The returned :class:`CurationMap` is keyed by the **stored spelling**
        (so iterating it still yields real, ``stat``-able paths) but resolves
        lookups through the normalised key, so a tile whose path is spelled
        differently from the session that starred it still finds its record
        without the caller having to know about normalisation.

        **Raises** ``sqlite3.Error`` when the table cannot be read — a failed
        read must never be reported as an empty curation (that is
        indistinguishable from "every star was lost").  :meth:`open_or_report`
        turns an unreadable table into the "curation unavailable (reason)"
        seam; callers that reload mid-session use :meth:`try_load_all`.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, star, user_tags, later FROM entries"
            ).fetchall()
        return CurationMap(
            (str(path), _row_to_meta((star, tags, later)))
            for path, star, tags, later in rows
        )

    def try_load_all(self) -> CurationMap | None:
        """:meth:`load_all`, or ``None`` when the read fails (logged).

        For reloads over an already-populated in-memory map (rename-following,
        bulk rebind): the caller keeps its current map on ``None`` instead of
        replacing it with an empty one — a transient sqlite error must not
        blank every badge for the rest of the session.
        """
        try:
            return self.load_all()
        except sqlite3.Error as exc:
            logger.warning("user_meta load_all failed: {}", exc)
            return None

    def all_tags(self) -> list[str]:
        """Distinct user tags across every entry (for edit-dialog autocomplete).

        First-seen spelling wins on a case-insensitive collision; sorted
        case-insensitively for a stable completer list.
        """
        seen: dict[str, str] = {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT user_tags FROM entries WHERE user_tags <> ''"
                ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover (defensive)
            logger.debug("user_meta all_tags failed: {}", exc)
            return []
        for (blob,) in rows:
            for tag in split_user_tags(str(blob or "")):
                key = tag.casefold()
                if key not in seen:
                    seen[key] = tag
        return sorted(seen.values(), key=str.casefold)

    # -------------------------------------------------------------- writes

    def set_star(
        self,
        path: Path | str,
        star: int,
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> UserMeta:
        """Set the star (0 clears it) for *path*, returning the merged record.

        Postref columns are (re)written when supplied so a later rename can
        follow the row; passing ``None`` leaves whatever postref the row
        already carries (a re-star shouldn't wipe rename-tracking data).

        Returns only the record (the on-disk value on failure); use
        :meth:`set_star_checked` when the caller must also learn whether the
        write actually persisted.
        """
        return self.set_star_checked(
            path, star,
            service=service, post_id=post_id, rel_name=rel_name,
        ).meta

    def set_star_checked(
        self,
        path: Path | str,
        star: int,
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> WriteOutcome:
        """:meth:`set_star` reporting persistence via a :class:`WriteOutcome`.

        ``outcome.ok`` is ``False`` when the star could not be committed; the UI
        can then warn instead of showing a success toast for a write that did
        not stick.  ``outcome.meta`` is the persisted (pre-change) record
        in that case, matching the plain setter's return.
        """
        star = clamp_star(star)

        def _apply(cur: UserMeta) -> UserMeta:
            return UserMeta(star=star, tags=cur.tags, later=cur.later)

        return self._merge(path, _apply, service, post_id, rel_name)

    def set_later(
        self,
        path: Path | str,
        later: bool,
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> UserMeta:
        """Set the "watch later" flag for *path*, returning the merged record.

        See :meth:`set_later_checked` to also learn whether it persisted.
        """
        return self.set_later_checked(
            path, later,
            service=service, post_id=post_id, rel_name=rel_name,
        ).meta

    def set_later_checked(
        self,
        path: Path | str,
        later: bool,
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> WriteOutcome:
        """:meth:`set_later` reporting persistence via a :class:`WriteOutcome`."""

        def _apply(cur: UserMeta) -> UserMeta:
            return UserMeta(star=cur.star, tags=cur.tags, later=bool(later))

        return self._merge(path, _apply, service, post_id, rel_name)

    def set_tags(
        self,
        path: Path | str,
        tags: list[str],
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> UserMeta:
        """Replace the user tags for *path*, returning the merged record.

        See :meth:`set_tags_checked` to also learn whether it persisted.
        """
        return self.set_tags_checked(
            path, tags,
            service=service, post_id=post_id, rel_name=rel_name,
        ).meta

    def set_tags_checked(
        self,
        path: Path | str,
        tags: list[str],
        *,
        service: str | None = None,
        post_id: str | None = None,
        rel_name: str | None = None,
    ) -> WriteOutcome:
        """:meth:`set_tags` reporting persistence via a :class:`WriteOutcome`."""
        clean = tuple(split_user_tags(",".join(tags)))

        def _apply(cur: UserMeta) -> UserMeta:
            return UserMeta(star=cur.star, tags=clean, later=cur.later)

        return self._merge(path, _apply, service, post_id, rel_name)

    def _merge(
        self,
        path: Path | str,
        apply,
        service: str | None,
        post_id: str | None,
        rel_name: str | None,
    ) -> WriteOutcome:
        key = normalize_entry_key(path)
        # Absolute, so ``normalize_entry_key(display) == key`` holds however the
        # caller spelled it — see :func:`absolute_spelling` for the badge /
        # info-panel split a relative spelling causes here.
        display = absolute_spelling(path)
        with self._lock:
            # ``self._lock`` only serialises *this* process.  A portable build
            # can be launched twice from the same folder (the upgrade path in
            # :meth:`_migrate` is written for exactly that), and then two
            # connections run this read-modify-write against one file.
            # Python's sqlite3 opens no transaction for a SELECT, so without an
            # explicit ``BEGIN IMMEDIATE`` the other process can write between
            # our read and our write, and our merged row — built on the value
            # we read before it — silently discards whichever axis (star /
            # later / tags) it just set.  IMMEDIATE takes the write lock up
            # front, so the loser waits out the pinned ``busy_timeout``.
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                # Hardening, not a precondition: if the lock can't be taken
                # now, carry on with the implicit transaction so the outcome
                # keeps its usual shape (a write that then fails still reports
                # the value actually on disk).
                logger.debug(
                    "user_meta write lock unavailable for {}: {}", display, exc,
                )
            try:
                current = self._get_raising(key)
            except sqlite3.Error as exc:
                # A failed read must not become a destructive write: merging
                # onto _EMPTY would replace the user's existing record with an
                # unknown base (or even DELETE it via the empty-row prune).
                logger.warning(
                    "user_meta read failed for {}; write aborted: {}", display, exc,
                )
                self._rollback_quietly()
                return WriteOutcome(_EMPTY, ok=False)
            try:
                merged = apply(current)
            except Exception:
                # The caller's transform is arbitrary; don't strand the write
                # lock if it raises.
                self._rollback_quietly()
                raise
            try:
                if merged.is_empty():
                    # An all-absent record leaves no row lingering.
                    self._conn.execute(
                        "DELETE FROM entries WHERE path_key=?", (key,)
                    )
                    self._conn.commit()
                    if self._gap_keys is not None:
                        self._gap_keys.discard(key)
                    return WriteOutcome(_EMPTY, ok=True)
                # Preserve any existing postref when the caller didn't supply
                # one (a bare re-star from a surface without post.md context).
                row = self._conn.execute(
                    "SELECT service, post_id, rel_name FROM entries "
                    "WHERE path_key=?",
                    (key,),
                ).fetchone()
                svc = service if service is not None else (row[0] if row else None)
                pid = post_id if post_id is not None else (row[1] if row else None)
                rel = rel_name if rel_name is not None else (row[2] if row else None)
                # ``path`` is refreshed to the spelling the caller just used, so
                # the display / stat column tracks how the library is currently
                # mounted while the identity (``path_key``) stays put.
                self._conn.execute(
                    "INSERT INTO entries "
                    "(path_key, path, star, user_tags, later, "
                    "service, post_id, rel_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path_key) DO UPDATE SET "
                    "path=excluded.path, "
                    "star=excluded.star, user_tags=excluded.user_tags, "
                    "later=excluded.later, service=excluded.service, "
                    "post_id=excluded.post_id, rel_name=excluded.rel_name",
                    (
                        key,
                        display,
                        merged.star,
                        join_user_tags(list(merged.tags)),
                        1 if merged.later else 0,
                        svc,
                        pid,
                        rel,
                    ),
                )
                self._conn.commit()
                # 穴の台帳を書いた内容で更新する（:meth:`fill_postrefs` が
                # SQL 無しで「埋めるものは無い」と答えられるように）。
                self._note_postref_state(key, svc, pid)
            except sqlite3.Error as exc:
                # A failed write must be distinguishable from a successful one.
                # ``ok=False`` lets a caller warn the user; ``meta`` is
                # ``current`` — the record still on disk — NOT ``merged`` (the
                # value we could NOT persist), so a caller that ignores ``ok``
                # still shows the true persisted state and the star/tag doesn't
                # silently reappear-then-vanish across a restart.  Mirrors the
                # read-failure path above (aborts rather than an unknown base).
                logger.warning("user_meta write failed for {}: {}", display, exc)
                self._rollback_quietly()
                return WriteOutcome(current, ok=False)
        return WriteOutcome(merged, ok=True)

    def _rollback_quietly(self) -> None:
        """Roll back the open transaction, swallowing a failure to do so.

        Every early return out of an explicit ``BEGIN IMMEDIATE`` must land
        here: leaving the transaction open keeps this connection's write lock
        and locks a second instance out of its own writes.
        """
        try:
            self._conn.rollback()
        except sqlite3.Error:  # pragma: no cover (defensive)
            pass

    # ------------------------------------------------- 手動の張り替え

    def rebind_path(
        self, old_path: "Path | str", new_path: "Path | str",
    ) -> "UserMeta | None":
        """*old_path* の行を *new_path* へ張り替える（到達不能になった行の復旧導線）。

        ドライブレター⇄UNC の付け替え (a) や post.md を持たないフォルダの
        リネーム (b) で到達不能になった行を救う唯一の手段。
        :meth:`resolve_moved_entries` が postref を持つ行しか追えないのに対し、
        こちらは**ユーザーの明示指定**なので無条件に張り替える（新パスの実在
        検証は呼び出し側のファイルダイアログが担う — ここは FS を一切触らない
        純文字列 + sqlite で、死んだ共有の綴りを渡されてもブロックしない）。

        規則:

        * 旧行が無ければ何もしない（``None`` — 別ウィンドウが先に消した等）。
        * *new_path* 側に既存行があれば :func:`_merge_curation_fields` の
          「ユーザーが表明したものしか残らない」併合（:meth:`_fold_legacy_rows`
          / :meth:`resolve_moved_entries` と同一の規約）: ★は最大 / later は
          OR / タグは和集合（初出綴り優先）。postref 3 列はグループで動き、
          **行き先の行が持っていればそれを保つ**（今そこにある実体を最後に
          スキャンした側が新しい）— 無いときだけ旧行から引き継ぐ。
        * ``path`` / ``path_key`` は :func:`absolute_spelling` /
          :func:`normalize_entry_key` を必ず通す — **4 人目の path 列の
          書き手**（module docstring の列挙とテストの許可リストに登録済み）。
        * 新旧が同じキーへ畳まれる（単なる綴り替え）なら表示綴りだけ更新する。

        Returns 張り替え後の行の :class:`UserMeta`。旧行不在・空の新パス・
        sqlite エラーでは ``None`` を返し、**データは変更しない**（この店の
        規約どおり、失敗は破壊にならない — 途中失敗はロールバックで巻き戻す）。
        """
        old_key = normalize_entry_key(old_path)
        new_display = absolute_spelling(new_path)
        if not new_display:
            return None
        new_key = normalize_entry_key(new_display)
        with self._lock:
            # path_key ごと張り替える経路 — postref の穴の台帳（キーで持つ）は
            # 次の読み込みで取り直す。
            self._gap_keys = None
            try:
                src = self._conn.execute(
                    "SELECT star, user_tags, later, service, post_id, rel_name "
                    "FROM entries WHERE path_key=?",
                    (old_key,),
                ).fetchone()
                if src is None:
                    return None
                s_star, s_tags, s_later, s_svc, s_pid, s_rel = src
                if new_key == old_key:
                    # 同じ実体の綴り替え — 表示綴りだけ今の指定へ追随させる
                    # （``_merge`` が書き込みのたびに行うのと同じ扱い）。
                    self._conn.execute(
                        "UPDATE entries SET path=? WHERE path_key=?",
                        (new_display, old_key),
                    )
                    self._conn.commit()
                    return _row_to_meta((s_star, s_tags, s_later))
                dst = self._conn.execute(
                    "SELECT star, user_tags, later, service, post_id, rel_name "
                    "FROM entries WHERE path_key=?",
                    (new_key,),
                ).fetchone()
                if dst is None:
                    # 行き先は空席 — 行ごと張り替える（resolve_moved_entries の
                    # 張り替え UPDATE と同形）。
                    self._conn.execute(
                        "UPDATE entries SET path=?, path_key=? "
                        "WHERE path_key=?",
                        (new_display, new_key, old_key),
                    )
                    self._conn.commit()
                    return _row_to_meta((s_star, s_tags, s_later))
                star, tags, later, svc, pid, rel = _merge_curation_fields(
                    (s_star, s_tags, s_later, s_svc, s_pid, s_rel), dst,
                )
                # 旧行の削除と行き先の上書きは 1 トランザクション — 途中で
                # 落ちたら両方巻き戻る（再生成不能データを半分だけ消さない）。
                self._conn.execute(
                    "DELETE FROM entries WHERE path_key=?", (old_key,)
                )
                self._conn.execute(
                    "UPDATE entries SET path=?, star=?, user_tags=?, later=?, "
                    "service=?, post_id=?, rel_name=? WHERE path_key=?",
                    (new_display, star, tags, later, svc, pid, rel, new_key),
                )
                self._conn.commit()
                return _row_to_meta((star, tags, later))
            except sqlite3.Error as exc:
                logger.warning(
                    "user_meta rebind failed for {}: {}", new_display, exc,
                )
                try:
                    self._conn.rollback()
                except sqlite3.Error:  # pragma: no cover (defensive)
                    pass
                return None

    def rebind_prefix(
        self,
        old_base: "Path | str",
        new_base: "Path | str",
        paths: "Iterable[Path | str]",
    ) -> int:
        """*paths* の行をまとめて *old_base* → *new_base* へ張り替える。

        失敗の単位は**ボリューム**（ドライブレターの付け替え、ライブラリ
        フォルダごとの移動）なのに :meth:`rebind_path` の復旧単位は行なので、
        数百行が一度に孤児化すると復旧が数百回のモーダル往復になる。これは
        同じ張り替えを 1 トランザクションで一度に行う口。

        **過剰適用しない**のがこの API の要点: 動かすのは *paths* に名指し
        された行**だけ**で、``old_base`` 配下を走査して当たった行を勝手に
        巻き込むことはしない。呼び出し側（横断一覧のゴースト集合）は「解決
        が到達不能と確定させた行」しか渡さないので、同じボリュームに生きて
        いる行がユーザーの知らないうちに書き換わる窓が開かない。

        規約は :meth:`rebind_path` と同じ — FS 非接触（綴りの検証は呼び出し
        側のフォルダダイアログが済ませている）、行き先に既存行があれば
        :func:`_merge_curation_fields` の併合（★は最大 / later は OR / タグは
        和集合 / postref はグループで行き先優先）、``path`` /``path_key`` は
        :func:`absolute_spelling` / :func:`normalize_entry_key` を必ず通す
        （**5 人目の path 列の書き手**）。

        全体が 1 つの ``BEGIN IMMEDIATE`` トランザクション: 途中で落ちたら
        1 行も動かない（再生成不能データを半分だけ動かした状態を作らない）。
        移す行を**先に全部読んで消してから**行き先へ入れるので、渡された集合
        の中に「ある行の行き先が別の行の旧位置」という重なりがあっても、
        処理順で結果が変わらない。

        Returns 実際に動かした行数（0 = 該当行なし / 書き込み失敗 — どちらも
        データは無変更）。
        """
        dest_base = absolute_spelling(new_base)
        if not dest_base:
            return 0
        # 旧鍵 → 新しい表示綴り。鍵で持つので、同じ行を 2 度名指しされても
        # 1 回しか動かない。
        moves: dict[str, str] = {}
        for p in paths:
            new_display = rebase_spelling(p, old_base, dest_base)
            if not new_display:
                continue  # 根が違う = この一括の対象外
            moves.setdefault(normalize_entry_key(p), new_display)
        if not moves:
            return 0
        with self._lock:
            # path_key ごと動かす経路 — postref の穴の台帳は次の読み込みで
            # 取り直す（:meth:`rebind_path` と同じ扱い）。
            self._gap_keys = None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                logger.debug(
                    "user_meta bulk rebind lock unavailable for {}: {}",
                    dest_base, exc,
                )
            try:
                # 1 回の SELECT で母集合を読み、移す行を Python 側で選ぶ
                # （``IN (?, ?, …)`` はホスト変数の上限に当たり得る。この店の
                # 行数ではどちらも一瞬で、こちらは上限を持たない）。
                src_rows = {
                    row[0]: row[1:]
                    for row in self._conn.execute(
                        "SELECT path_key, star, user_tags, later, "
                        "service, post_id, rel_name FROM entries"
                    )
                    if row[0] in moves
                }
                if not src_rows:
                    self._rollback_quietly()
                    return 0
                self._conn.executemany(
                    "DELETE FROM entries WHERE path_key=?",
                    [(key,) for key in src_rows],
                )
                for key, src in src_rows.items():
                    new_display = moves[key]
                    new_key = normalize_entry_key(new_display)
                    dst = self._conn.execute(
                        "SELECT star, user_tags, later, service, post_id, "
                        "rel_name FROM entries WHERE path_key=?",
                        (new_key,),
                    ).fetchone()
                    row = src if dst is None else _merge_curation_fields(src, dst)
                    star, tags, later, svc, pid, rel = row
                    self._conn.execute(
                        "INSERT INTO entries "
                        "(path_key, path, star, user_tags, later, "
                        "service, post_id, rel_name) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(path_key) DO UPDATE SET "
                        "path=excluded.path, "
                        "star=excluded.star, user_tags=excluded.user_tags, "
                        "later=excluded.later, service=excluded.service, "
                        "post_id=excluded.post_id, rel_name=excluded.rel_name",
                        (
                            new_key, new_display, clamp_star(star or 0),
                            join_user_tags(split_user_tags(str(tags or ""))),
                            1 if later else 0, svc, pid, rel,
                        ),
                    )
                self._conn.commit()
                return len(src_rows)
            except sqlite3.Error as exc:
                logger.warning(
                    "user_meta bulk rebind failed for {}: {}", dest_base, exc,
                )
                self._rollback_quietly()
                return 0

    # ----------------------------------------------------- rename following

    def resolve_moved_entries(
        self,
        resolver: "MovedResolver",
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> int:
        """Re-point rows whose ``path`` vanished onto their postref's new folder.

        A writer-side naming-pattern change renames post folders, orphaning
        every star keyed by the old path.  *resolver* maps a postref back to
        the current folder that carries it (built by the caller from a scan of
        the library, off the GUI thread) — see :class:`MovedResolver`.

        For each stored row whose ``path`` no longer exists on disk but whose
        postref resolves to a *different* current folder, the row is rewritten
        under the new path (a post folder → the new folder; a file → the new
        folder / ``rel_name``).  A destination that already has its own row is
        **merged with**, never overwritten and never dropped:
        :func:`_merge_curation_fields` folds the two rows (star = max, later =
        OR, tags = union, postref as a group), exactly as :meth:`rebind_path`
        and :meth:`_fold_legacy_rows` fold the same "one object, two rows"
        collision.  It must not ``DELETE`` the source row outright on
        the grounds that "the user's newer curation wins" — nothing in the
        schema can tell which row is newer (there is no ``used_at`` column, and
        the destination may well be an older duplicate in another library), so
        a star 5 plus its tags could be erased by a star 1.
        The store's rule is the one the other two paths already follow: only
        what the user never expressed may be lost.

        Returns how many rows the store actually **changed** (repointed +
        merged) — taken from ``cursor.rowcount``, not from the number of
        statements attempted, so a repoint whose source row vanished under us
        (another window's write between the lock-free probe and here) neither
        inflates the 「N 件張り替えました」 log line nor makes the caller repaint
        for nothing.  Both kinds count because both change what
        :meth:`load_all` returns.

        "No longer exists" is deliberately strict: only a provable
        absence counts (:func:`_is_definitely_gone`) **and** only while the
        entry's own drive / share still answers (:func:`_anchor_reachable`).
        An unreachable volume otherwise looks exactly like a deleted folder and
        would move the star onto a local copy carrying the same postref — an
        irreversible rewrite of non-regenerable data.

        Runs off the GUI thread (a background worker at startup, see
        ``main_window``).  The slow stat probes — each a NAS round-trip
        that can block for seconds on a half-mounted share — run WITHOUT the
        store lock, so a concurrent GUI curation write (set_star/set_tags,
        which share this lock) is never stalled behind them.  Only the snapshot
        read and the final repoint statements briefly hold the lock.

        *should_cancel* is polled before every candidate row of step 2 — the
        same cooperative contract :func:`build_moved_resolver` and
        :func:`resolve_curation_paths` already offer.  This is the third leg of
        rename following and the only one that had no way to hear a cancel, so
        closing the window (or switching roots) left a dead share's stat loop
        running to the last row.  Repoints already decided when the cancel
        lands are still applied — each one is independent.
        """
        # 1. Snapshot candidate rows under the lock, then release it.
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT path, service, post_id, rel_name FROM entries "
                    "WHERE service IS NOT NULL AND post_id IS NOT NULL"
                ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover (defensive)
            logger.debug("resolve_moved_entries scan failed: {}", exc)
            return 0
        # 2. Resolve moves lock-free (the exists() stat is the slow part).
        #    (old_key, new_display, new_key) — the row is addressed by its
        #    normalised key, but the new spelling is what lands in ``path``.
        repoints: list[tuple[str, str, str]] = []
        anchor_ok: dict[str, bool] = {}
        for old_path, service, post_id, rel_name in rows:
            if should_cancel is not None and should_cancel():
                break
            old = Path(old_path)
            if not _is_definitely_gone(old):
                continue  # still where we left it (or we can't tell)
            if not _anchor_reachable(old, anchor_ok):
                # The whole volume is offline — "missing" here means "invisible",
                # not "renamed".
                continue
            new_folder = resolver.folder_for(str(service), str(post_id))
            if new_folder is None:
                continue  # postref not found in the current library
            new_path = new_folder / rel_name if rel_name else new_folder
            # The **third** writer of the ``path`` column: *resolver* is built by
            # :func:`build_moved_resolver` walking ``root`` with ``os.scandir``,
            # so with a relative ``--root`` every folder it hands back is spelled
            # relatively and this row would store a relative ``path`` next to an
            # absolute ``path_key`` — the exact badge/info-panel split
            # :func:`absolute_spelling` exists to prevent.
            new_display = absolute_spelling(new_path)
            new_key = normalize_entry_key(new_path)
            old_key = normalize_entry_key(old_path)
            if new_key == old_key:
                # Same entry, merely re-spelled — nothing moved.  **This guard
                # is load-bearing, not an optimisation**:
                # delete a starred *file* while leaving its post folder (and
                # post.md) in place and the row qualifies as "gone", yet the
                # resolver hands back that same folder, so ``new_path`` is the
                # old path.  Step 3's "don't clobber a destination the user has
                # already curated" probe would then find the row **itself** and
                # take the DELETE branch — silently and permanently erasing the
                # star (measured: 1 row star=5 → 0 rows).  Without the guard
                # the more spellings fold, the more rows self-destruct.
                continue
            repoints.append((old_key, new_display, new_key))
        if not repoints:
            return 0
        # 3. Apply the repoints under the lock (fast — no stat calls here).
        repointed = 0
        merged = 0
        curation_row_sql = (
            "SELECT star, user_tags, later, service, post_id, rel_name "
            "FROM entries WHERE path_key=?"
        )
        with self._lock:
            if repoints:
                # path_key ごと張り替える経路 — postref の穴の台帳（キーで
                # 持つ）は次の読み込みで取り直す。
                self._gap_keys = None
            for old_key, new_display, new_key in repoints:
                # Each repoint gets its own SAVEPOINT so a failure undoes that
                # repoint and nothing else.  Without one the merge branch's two
                # statements can half-apply: the DELETE succeeds, the
                # destination UPDATE raises, the ``except`` swallows it, and
                # the commit below makes the DELETE permanent — the source
                # row's star and tags are gone for good, counted and logged as
                # a successful merge.  This is the only non-regenerable data
                # the viewer holds and there is no undo.  :meth:`rebind_path`,
                # which folds the same "one object, two rows" collision,
                # already rolls back on error; this path did not.
                try:
                    self._conn.execute("SAVEPOINT resolve_repoint")
                except sqlite3.Error as exc:  # pragma: no cover (defensive)
                    logger.warning(
                        "resolve_moved_entries could not open a savepoint: {}",
                        exc,
                    )
                    break
                row_repointed = 0
                row_merged = 0
                try:
                    # The destination may already carry curation of its own —
                    # fold both rows instead of picking a winner (see the
                    # docstring; both are non-regenerable).
                    dst = self._conn.execute(
                        curation_row_sql, (new_key,)
                    ).fetchone()
                    if dst is not None:
                        src = self._conn.execute(
                            curation_row_sql, (old_key,)
                        ).fetchone()
                        # ``src is None``: another window deleted the source
                        # between the lock-free probe and here — nothing to
                        # merge, and the released savepoint is a no-op.
                        if src is not None:
                            star, tags, later, svc, pid, rel = (
                                _merge_curation_fields(src, dst)
                            )
                            cur = self._conn.execute(
                                "DELETE FROM entries WHERE path_key=?",
                                (old_key,),
                            )
                            row_merged = max(cur.rowcount, 0)
                            self._conn.execute(
                                "UPDATE entries SET path=?, star=?, "
                                "user_tags=?, later=?, service=?, post_id=?, "
                                "rel_name=? WHERE path_key=?",
                                (new_display, star, tags, later, svc, pid,
                                 rel, new_key),
                            )
                    else:
                        cur = self._conn.execute(
                            "UPDATE entries SET path=?, path_key=? "
                            "WHERE path_key=?",
                            (new_display, new_key, old_key),
                        )
                        row_repointed = max(cur.rowcount, 0)
                    self._conn.execute("RELEASE resolve_repoint")
                except sqlite3.Error as exc:
                    logger.warning(
                        "resolve_moved_entries repoint failed: {}", exc,
                    )
                    try:
                        self._conn.execute("ROLLBACK TO resolve_repoint")
                        self._conn.execute("RELEASE resolve_repoint")
                    except sqlite3.Error:  # pragma: no cover (defensive)
                        pass
                    continue
                # Counted only after the repoint is released: these numbers are
                # what the caller repaints from and what the log line claims.
                repointed += row_repointed
                merged += row_merged
            changed = repointed + merged
            if changed:
                self._conn.commit()
            elif self._conn.in_transaction:  # pragma: no cover (defensive)
                # Nothing landed, but a savepoint may have opened a
                # transaction — don't leave it holding the write lock.
                self._conn.rollback()
        if changed:
            logger.info(
                "user_meta: re-pointed {} moved entr(ies), "
                "merged {} into an existing row",
                repointed, merged,
            )
        return changed

    def postrefs(self) -> list[tuple[str, str]]:
        """Distinct ``(service, post_id)`` postrefs recorded in the store.

        Lets the rename-following worker build a resolver only for the postrefs
        that actually have curation, instead of indexing the whole library.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT DISTINCT service, post_id FROM entries "
                    "WHERE service IS NOT NULL AND post_id IS NOT NULL"
                ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover (defensive)
            logger.debug("user_meta postrefs failed: {}", exc)
            return []
        return [(str(s), str(p)) for s, p in rows]

    # ------------------------------------------- postref のバックフィル

    #: postref 列が空の行の ``path_key``。``None`` = 未読み込み（初回の
    #: :meth:`_postref_gap_keys` が 1 回だけ SELECT する）。以後は書き込みが
    #: 増減させるので、穴が無いライブラリでの :meth:`fill_postrefs` は
    #: **SQL を 1 本も撃たない**（メタ着地ごとに呼ばれる経路なのでこれが要る）。
    _gap_keys: "set[str] | None" = None

    def _postref_gap_keys(self) -> set[str]:
        """postref 列が空の行の ``path_key``（呼び出し側が ``_lock`` を保持）。"""
        if self._gap_keys is None:
            try:
                rows = self._conn.execute(
                    "SELECT path_key FROM entries "
                    "WHERE service IS NULL OR service='' "
                    "OR post_id IS NULL OR post_id=''"
                ).fetchall()
            except sqlite3.Error as exc:  # pragma: no cover (defensive)
                logger.debug("user_meta postref gap scan failed: {}", exc)
                return set()
            self._gap_keys = {str(row[0]) for row in rows}
        return self._gap_keys

    def _note_postref_state(self, key: str, service, post_id) -> None:
        """行を書いた / 消したときに穴の台帳を更新する（``_lock`` の中で）。"""
        gaps = self._gap_keys
        if gaps is None:
            return  # まだ読んでいない = 次の読み込みが真の状態を取る
        if service and post_id:
            gaps.discard(key)
        else:
            gaps.add(key)

    def fill_postrefs(
        self, refs: "Iterable[tuple[Path, str, str]]",
    ) -> int:
        """既存行の **postref 列だけ**を埋める。★ / タグ / 「あとで見る」は不変。

        *refs* は ``(post フォルダ, service, post_id)``。フォルダ自身の行と、
        その**直下**のファイル行（``rel_name`` = ファイル名）のうち postref が
        空のものを埋め、埋めた件数を返す。

        これが要るのは、印を打った時点でそのフォルダのメタデータがまだ着地
        していないことがあるため（冷えた共有では ``scan_done`` と
        ``metadata_batch`` の間が秒単位空く）。そのとき ``_postref_columns``
        は ``(None, None, None)`` を返し、:meth:`_merge` はそれを「既存
        postref を保持」と読むので、行は postref を**永久に**持てない — 同じ
        パスへもう一度書き込むまでリネーム追従の対象から外れたままになる。
        ★はこの製品で唯一の再生成不能データで、リネーム追従はその保護機構。

        既に postref を持つ行は触らない（WHERE 句で弾く）ので、リネーム追従が
        張り替えた行を古いフォルダの postref で上書きすることはない。
        """
        pairs = [
            (normalize_entry_key(folder), service, post_id)
            for folder, service, post_id in refs
            if service and post_id
        ]
        if not pairs:
            return 0
        filled = 0
        stale = False
        with self._lock:
            gaps = self._postref_gap_keys()
            if not gaps:
                return 0
            try:
                for key, service, post_id in pairs:
                    prefix = key + os.sep
                    targets = [
                        k for k in gaps
                        if k == key
                        or (
                            k.startswith(prefix)
                            and os.sep not in k[len(prefix):]
                        )
                    ]
                    for target in targets:
                        rel: str | None = None
                        if target != key:
                            row = self._conn.execute(
                                "SELECT path FROM entries WHERE path_key=?",
                                (target,),
                            ).fetchone()
                            if row is None:
                                gaps.discard(target)
                                continue
                            # 表示スペリングから採る（``path_key`` は
                            # normcase 済みで、rel_name は実名を保つ）。
                            rel = Path(str(row[0])).name
                        cur = self._conn.execute(
                            "UPDATE entries SET service=?, post_id=?, rel_name=? "
                            "WHERE path_key=? AND (service IS NULL OR service='' "
                            "OR post_id IS NULL OR post_id='')",
                            (service, post_id, rel, target),
                        )
                        if cur.rowcount:
                            filled += 1
                        else:
                            # 台帳にはあるのに穴ではない = 別インスタンスが
                            # 先に埋めた（台帳はプロセス内の書き込みしか
                            # 映さない）。次回読み直す。
                            stale = True
                        gaps.discard(target)
                # 0 行 UPDATE でも sqlite3 は暗黙の BEGIN を出しているので、
                # 埋めた件数に関わらず必ず閉じる — 開いたままだと write
                # ロックを掴んだまま GUI へ戻り、別インスタンスの★書き込みが
                # busy_timeout いっぱい待たされて失敗する。
                if self._conn.in_transaction:
                    self._conn.commit()
                if stale:
                    self._gap_keys = None
            except sqlite3.Error as exc:  # pragma: no cover (defensive)
                logger.debug("user_meta postref backfill failed: {}", exc)
                self._rollback_quietly()
                # 台帳の状態が疑わしいので次回読み直す。
                self._gap_keys = None
                return 0
        return filled


def _row_to_meta(row) -> UserMeta:
    if row is None:
        return _EMPTY
    star, tags, later = row
    return UserMeta(
        star=clamp_star(star or 0),
        tags=tuple(split_user_tags(str(tags or ""))),
        later=bool(later),
    )


__all__ = [
    "DB_NAME",
    "MAX_STAR",
    "MIN_STAR",
    "SCHEMA_VERSION",
    "USER_TAG_SEPARATOR_RE",
    "CurationMap",
    "CurationResolve",
    "MovedResolver",
    "UserMeta",
    "UserMetaStore",
    "WriteOutcome",
    # 解決層（``user_meta_parts.resolve``）からの re-export。``_`` 付きの 2 つは
    # ``curation_recovery`` とテストが ``user_meta`` 越しに読む私設名で、分割前
    # と同じ綴りで届くようにここへ載せる（公開 API を増やす意図ではない）。
    "_curation_display_name",
    "_is_gone_exc",
    "absolute_spelling",
    "build_curation_entry",
    "build_moved_resolver",
    "clamp_star",
    "join_user_tags",
    "normalize_entry_key",
    "resolve_curation_paths",
    "split_user_tags",
]
