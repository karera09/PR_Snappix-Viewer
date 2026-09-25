"""Cross-tool shared preferences persisted as ``data/shared_prefs.json``.

The viewer (and any companion snappix tool) reads and writes this file.  It exists
because a few settings belong to the *product family* rather than to one tool:
the two GUIs should look alike (shared UI theme) and they operate on the same
folder tree (a tool's output root == the viewer's browse root, here called
a "library root").  Keeping those in each tool's own ``config.json`` /
``viewer_state.json`` let them drift; this module is the single shared source.

Read / write contract
----------------------
* **Read on startup.** Each tool loads shared prefs once when it launches and
  applies them (e.g. resolve the effective theme, seed the library-root list).
* **Write on change.** When the user changes one of these settings in either
  tool (theme menu / dialog, adding a library root), that tool writes the
  change back via :func:`update_shared_prefs` so the *other* tool picks it up
  on its next launch.
* **No real-time cross-process sync.** A running instance is NOT notified when
  the other process rewrites the file; the value only propagates on the next
  startup.  This keeps the design a plain file with no watcher / IPC.  If a
  live sync is ever wanted it should be layered on top (e.g. the viewer's
  ``QFileSystemWatcher``), not baked into this module.

Concurrency
-----------
Two processes may write near-simultaneously.  :func:`save_shared_prefs` writes
atomically (tmp file + :func:`os.replace`), so a reader never sees a truncated
file, and :func:`update_shared_prefs` re-reads immediately before writing and
merges its changes onto the freshest on-disk copy.  This is a deliberately
simple *last-writer-wins* scheme: no file locking.  The read→modify→write
window is tiny and these settings change rarely and interactively, so the
worst case (two edits within the same few milliseconds, the later fully
overwriting the earlier field) is acceptable.  Do not add lock files here —
that would reintroduce a portable-ness / stale-lock hazard for a benign race.

Resilience
----------
Degraded-load protection: if the file
exists but cannot be *read* (lock / permissions / NAS hiccup), the session runs
on defaults and that stand-in refuses to overwrite the (possibly perfectly
fine) on-disk file.  Invalid *values* degrade field-by-field (a mistyped or
junk ``theme`` falls back to its default while ``library_roots`` etc. survive
— see the ``mode="before"`` coercers on :class:`SharedPrefs`); only a file
that fails to parse as a JSON object at all is genuinely corrupt, and
overwriting *that* loses nothing.  Unknown keys round-trip (``extra="allow"``)
so a field written by a newer build survives an older build's load+save.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from .fsutil import reap_stale_tmps
from .paths import get_paths

# Registered theme names: "system" (OS-following) plus every built-in theme
# in ``common/ui/tokens.py`` (main dark / light / standard + the extra themes).
# Kept as a literal tuple (importing ``common.ui`` would pull Qt into this
# Qt-free module); sync with the theme registry is machine-checked by
# ``tests/test_viewer_theme_menu.py``.
_VALID_THEMES = (
    "system",
    "light",
    "dark",
    "standard",
    "extra_astro",
    "extra_obsidian",
    "extra_washi",
    "extra_linen",
    "extra_brass",
    "extra_dusk",
)

#: The theme value that means "not explicitly chosen".  Both tools default
#: their *local* theme field to this too, so it doubles as the sentinel the
#: one-time migration below keys on ("shared still at default").
_DEFAULT_THEME = "system"

#: Registered UI languages.  "ja" is the only bundled locale today; adding a
#: language means creating ``common/i18n/locales/<code>/`` and appending the
#: code here (mirrors how ``_VALID_THEMES`` gates the theme field).
_VALID_LANGUAGES = ("ja",)

#: The default / fallback language.  Matches ``common.i18n.DEFAULT_LOCALE`` and
#: doubles as the "not explicitly chosen" sentinel for the promote-once
#: migration in :func:`decide_startup_language`.
_DEFAULT_LANGUAGE = "ja"


class SharedPrefs(BaseModel):
    """Settings shared across the snappix tools.

    Kept intentionally minimal — only genuinely cross-tool concerns belong
    here.  Tool-private settings stay in
    ``viewer_state.json`` (viewer).

    ``extra="allow"`` guarantees the unknown-key round-trip: a key written by
    a newer build survives being loaded and saved by a build that doesn't know
    the field, instead of being silently dropped by ``model_dump()``.
    """

    model_config = ConfigDict(extra="allow")

    #: Session-only marker set by :func:`load_shared_prefs` when
    #: shared_prefs.json exists but could not be *read* (lock / permissions /
    #: NAS hiccup) and this object is therefore a default stand-in, NOT the
    #: user's real settings.  :func:`save_shared_prefs` refuses to overwrite an
    #: intact file with such a stand-in.  Private attr → never serialised.
    _degraded_load: bool = PrivateAttr(default=False)

    #: UI theme shared by both tools: "system" follows the OS colour scheme,
    #: any other :data:`_VALID_THEMES` name pins the corresponding built-in
    #: token palette (see ``common/ui``).  The default is the empty string =
    #: "not yet chosen"
    #: (the unset sentinel), kept DISTINCT from the explicit "system" choice so
    #: that an explicit reset-to-system is authoritative and durable rather than
    #: being re-promoted from a stale local value (see
    #: :func:`decide_startup_theme`).  Plain str (not Literal) so an unknown
    #: on-disk value degrades to unset in the validator instead of failing the
    #: whole load — same field-level-drop philosophy as ``viewer/state.py``.
    theme: str = ""

    #: UI language shared by both tools (the active i18n locale).  Empty string
    #: default = "not yet chosen" (unset sentinel), distinct from an explicit
    #: locale choice for the same durability reason as ``theme``.  Plain str
    #: (not Literal) for the degrade-not-fail reason.  The single source of
    #: truth is this shared file; each tool's local ``language`` field is kept
    #: only for round-trip / promote-once.
    language: str = ""

    #: Registered library roots — folders that are a tool's output root and
    #: a viewer browse root at once.  Duplicate-free and order-preserving
    #: (first occurrence wins).  Empty on a fresh install.
    library_roots: list[str] = Field(default_factory=list)

    # Field-level type tolerance: a hand-edited / half-written file with ONE
    # mistyped field (``"theme": 123`` / ``"library_roots": "C:/x"``) must not
    # fail whole-model validation — that would make load_shared_prefs treat
    # the entire file as corrupt and reset every other (perfectly fine)
    # setting.  These ``mode="before"`` coercers drop just the bad value, so
    # the after-validator below only ever sees well-typed data.
    @field_validator("theme", "language", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        return value if isinstance(value, str) else ""

    @field_validator("library_roots", mode="before")
    @classmethod
    def _coerce_roots(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        # Drop non-str entries so a hand-edited file can't smuggle in junk
        # paths (empty strings and duplicates are handled in ``_normalise``).
        return [item for item in value if isinstance(item, str)]

    @model_validator(mode="after")
    def _normalise(self) -> "SharedPrefs":
        # "" is the unset sentinel and is preserved; any OTHER value that is
        # not a registered theme/language degrades to unset (so a junk field
        # can neither win nor be promoted, and an explicit valid choice — incl.
        # "system" — round-trips intact and stays authoritative).
        if self.theme and self.theme not in _VALID_THEMES:
            self.theme = ""
        if self.language and self.language not in _VALID_LANGUAGES:
            self.language = ""
        # Dedupe while preserving order; drop empty entries (non-str entries
        # were already removed by the before-validator above).  The dedupe key
        # is ``os.path.normcase``-folded, the kept value is the raw spelling:
        # everywhere else paths are matched case-insensitively on Windows
        # (``fsutil.relative_parts``, the folder-preview cache's COLLATE
        # NOCASE, the nav rail's subtree overlap test), so matching exactly
        # here is what let "C:/a" and "C:/A" both register as separate roots
        # while every consumer treated them as one.  ``normcase`` is the
        # identity on POSIX, so case-sensitive volumes keep both entries.
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in self.library_roots:
            if not raw:
                continue
            key = os.path.normcase(raw)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(raw)
        self.library_roots = cleaned
        return self


def _prefs_path() -> Path:
    return get_paths().data / "shared_prefs.json"


def load_shared_prefs() -> SharedPrefs:
    """Load shared prefs, degrading gracefully on read / parse failure.

    Called once on startup by each tool.  Missing file → defaults (and the
    file is created so the other tool can discover it).  Unreadable file →
    defaults marked ``_degraded_load`` so a later save won't clobber it.
    Corrupt file → backed up and replaced with fresh defaults.

    **Never raises for a damaged file.**  This runs before the main window
    exists (``viewer/app.py`` resolves the startup language/theme through it,
    outside the ``except OSError`` startup guard), so anything escaping here
    ends the process without a window — and a windowed frozen build has no
    stderr to say why.  Non-UTF-8 content counts as unreadable, not corrupt.
    """
    path = _prefs_path()
    if not path.exists():
        prefs = SharedPrefs()
        try:
            save_shared_prefs(prefs)
        except OSError as exc:
            logger.warning("could not write initial shared_prefs.json: {}", exc)
        return prefs
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # ``UnicodeDecodeError`` is a ``ValueError``, *not* an ``OSError``:
        # a file saved as ANSI (cp932) by 日本語 Windows のメモ帳 — or a
        # backup restored with the wrong encoding — would otherwise escape
        # this whole function (``viewer/plugin_host/manifest.py`` guards the
        # same way).  Its content is very likely
        # intact user data in another encoding, so it takes the *unreadable*
        # branch (file left untouched) rather than the corrupt one.
        # Unreadable is NOT corruption — the file may be perfectly fine.
        # Run this session on defaults and leave the file untouched.
        logger.warning(
            "shared_prefs.json could not be read ({}); starting with defaults "
            "for this session (file left untouched)", exc,
        )
        prefs = SharedPrefs()
        prefs._degraded_load = True
        return prefs
    try:
        return SharedPrefs.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "shared_prefs.json is corrupted; backing it up and starting fresh"
        )
        try:
            path.replace(path.with_suffix(".json.bak"))
        except OSError as exc:
            logger.warning("could not back up corrupted shared_prefs.json: {}", exc)
        prefs = SharedPrefs()
        try:
            save_shared_prefs(prefs)
        except OSError as exc:
            logger.warning("could not write fresh shared_prefs.json: {}", exc)
        return prefs


def _existing_prefs_intact(path: Path) -> bool:
    """Whether *path* currently holds a loadable shared-prefs file.

    Used only on the degraded-load path: ``True`` means "there is (or may be)
    real user data here — do not overwrite it with defaults".  A file that is
    still unreadable counts as intact (we cannot rule out that it's fine);
    only a file that reads but fails to parse counts as lost.

    Non-UTF-8 bytes are unreadable, not lost — and ``UnicodeDecodeError`` is
    a ``ValueError``, so it must be named explicitly or it escapes the guard
    it is part of.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True  # still unreadable — assume the data is fine
    try:
        SharedPrefs.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return False  # genuinely corrupt — overwriting loses nothing
    return True


def save_shared_prefs(prefs: SharedPrefs) -> None:
    """Persist *prefs* atomically (tmp file + :func:`os.replace`).

    Honours the degraded-load guard: a default stand-in created because the
    file couldn't be read at startup will not overwrite an intact on-disk
    file.  Raises ``OSError`` on write failure (callers that want best-effort
    behaviour should catch it — :func:`load_shared_prefs` does).
    """
    path = _prefs_path()
    if prefs._degraded_load and path.exists() and _existing_prefs_intact(path):
        logger.warning(
            "shared_prefs.json save skipped: this session started on default "
            "settings because the file could not be read, and overwriting the "
            "existing file would discard the stored settings"
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-CALL unique temp name (``mkstemp``), not per-process: a fixed ".tmp"
    # would let two writers interleave truncate/write/replace on the same
    # file, and a PID-based name is exactly as shared whenever the two writers
    # live in the SAME process — which they do here (the viewer flushes the
    # theme from a worker thread while an in-process plugin can write a
    # library root from the GUI thread).  Distinct temp files keep each
    # replace atomic; the outcome is plain last-writer-wins, as documented
    # above.  Same scheme as ``viewer/state.py::_write_json_atomic`` and
    # ``common/fsutil.py::write_text_atomic``; the TTL sweep of abandoned
    # temps is shared with the latter (``fsutil.reap_stale_tmps``).
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(prefs.model_dump(), ensure_ascii=False, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        # Best-effort cleanup so a failed save doesn't strand a temp file next
        # to the prefs (the original error is re-raised for callers).  Unique
        # names are never overwritten by the next write, so debris would
        # otherwise only accumulate.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    reap_stale_tmps(path)
    prefs._degraded_load = False


def update_shared_prefs(**changes: Any) -> SharedPrefs:
    """Read-modify-write helper: apply *changes* onto the freshest on-disk copy.

    Re-reads the file immediately before writing (rather than trusting an
    in-memory copy the caller may have loaded seconds ago) and merges the
    given fields on top, so a concurrent write by the *other* tool to a
    *different* field is preserved.  This is the simple last-writer-wins
    scheme documented at the module level: only the fields in *changes* are
    overwritten; everything else on disk survives.  No locking.

    Returns the merged :class:`SharedPrefs` that was written.  If the file is
    in a degraded (unreadable-at-startup) state the write is skipped by
    :func:`save_shared_prefs` and the freshly loaded (default) object is
    returned unchanged on disk.
    """
    prefs = load_shared_prefs()
    degraded = prefs._degraded_load
    for key, value in changes.items():
        setattr(prefs, key, value)
    # Re-run the after-validator so, e.g., an out-of-range theme or a
    # duplicate library root is normalised before persisting.  ``model_dump``
    # drops the PrivateAttr degraded flag, so carry it across manually — else
    # a degraded stand-in would lose its guard and clobber the on-disk file.
    prefs = SharedPrefs.model_validate(prefs.model_dump())
    prefs._degraded_load = degraded
    save_shared_prefs(prefs)
    return prefs


def _decide_startup_scalar(
    shared_value: str, local_value: str, *, valid: tuple[str, ...], default: str
) -> tuple[str, str | None]:
    """Shared-wins / promote-once rule for one shared scalar preference.

    The single implementation behind :func:`decide_startup_theme` and
    :func:`decide_startup_language` (and any future shared scalar): a pure
    decision function (no I/O — unit-testable) parameterised only by the
    field's *valid* value tuple and its tool *default*.

    * **shared wins** whenever it holds a real choice — the other tool set it
      and this tool should match on its next launch.
    * **local is promoted** only when shared is still unset *and* this tool
      carries a non-default local value.  That is the one-time migration from
      the pre-shared world where each tool stored the value in its own
      ``config.json`` / ``viewer_state.json``: the first tool to launch after
      the upgrade seeds ``shared_prefs.json`` from its own value so the
      *other* tool adopts it next time.
    * otherwise both are at the default and nothing needs writing.

    Returns ``(effective_value, value_to_write)`` where ``value_to_write`` is
    the value to persist back to shared prefs (via
    :func:`update_shared_prefs`) or ``None`` when no write is needed.

    "Not yet chosen" is the empty-string sentinel, kept DISTINCT from any
    explicit choice that happens to equal *default* (e.g. theme "system"): an
    explicit value in shared is authoritative and wins, so a reset-to-default
    is durable and never re-promoted from a stale local value.  A shared
    value that is empty or outside *valid* is unset; a local value outside
    *valid* is treated as *default* so a junk local field can never win or be
    promoted.
    """
    if shared_value not in valid:
        shared_value = ""  # unset (empty or junk)
    if local_value not in valid:
        local_value = default
    if shared_value:
        # Shared holds an explicit choice (incl. one equal to the default) —
        # authoritative.
        return shared_value, None
    if local_value != default:
        # One-time migration: seed shared from this tool's legacy non-default
        # local value (a real pre-shared choice).  A local that is merely the
        # default is NOT promoted, so shared stays unset until someone chooses.
        return local_value, local_value
    return default, None


def _resolve_startup_scalar(
    local_value: str, *, field: str, valid: tuple[str, ...], default: str
) -> str:
    """Load shared prefs, reconcile *local_value*, and return the winner.

    The single implementation behind :func:`resolve_startup_theme` and
    :func:`resolve_startup_language`: reads the shared file once, applies
    :func:`_decide_startup_scalar` against ``getattr(shared, field)`` and — if
    a promotion is due — writes the promoted value back via
    ``update_shared_prefs(**{field: ...})`` (best effort; an ``OSError`` is
    logged and swallowed so a read-only data dir can never block startup).

    **Degraded load = no promotion.**  When the shared file exists but could
    not be read this session (``_degraded_load``), its default field value is
    a stand-in, not evidence that shared is unset — promoting the local value
    off it could overwrite the *other* tool's real choice (the later
    ``update_shared_prefs`` re-reads the file, so if it has become readable
    again the degraded guard would no longer protect it).  In that case the
    session simply runs on the local value and writes nothing.
    """
    shared = load_shared_prefs()
    if shared._degraded_load:
        logger.warning(
            "shared_prefs.json unreadable this session — using the local "
            "{} without promoting it into shared prefs",
            field,
        )
        return local_value if local_value in valid else default
    effective, to_write = _decide_startup_scalar(
        getattr(shared, field), local_value, valid=valid, default=default
    )
    if to_write is not None:
        try:
            update_shared_prefs(**{field: to_write})
        except OSError as exc:
            logger.warning(
                "could not migrate {} into shared_prefs.json: {}", field, exc
            )
    return effective


def decide_startup_theme(shared_theme: str, local_theme: str) -> tuple[str, str | None]:
    """Reconcile a tool's local theme with the shared one at startup.

    Pure decision function (no I/O — unit-testable) implementing the theme
    single-source-of-truth rule:

    * **shared wins** whenever it holds a real (non-default) choice — the
      other tool set it and this tool should match on its next launch.
    * **local is promoted** only when shared is still at its default *and*
      this tool carries a non-default local value.  That is the one-time
      migration from the pre-shared world where each tool stored theme in
      its own ``config.json`` / ``viewer_state.json``: the first tool to
      launch after the upgrade seeds ``shared_prefs.json`` from its own
      value so the *other* tool adopts it next time.
    * otherwise both are at the default and nothing needs writing.

    Returns ``(effective_theme, theme_to_write)`` where ``theme_to_write``
    is the value to persist back to shared prefs (via
    :func:`update_shared_prefs`) or ``None`` when no write is needed.

    "Not yet chosen" is the empty-string sentinel, kept DISTINCT from the
    explicit "system" choice: an explicit "system" in shared is authoritative
    and wins (so a reset-to-system is durable and never re-promoted from a
    stale local value).  A shared value that is empty or junk is unset;
    a local value outside :data:`_VALID_THEMES` is treated as the tool default
    so a junk local field can never win or be promoted.

    Thin wrapper over :func:`_decide_startup_scalar` — the rule itself lives
    there so a third shared scalar needs no new logic.
    """
    return _decide_startup_scalar(
        shared_theme, local_theme, valid=_VALID_THEMES, default=_DEFAULT_THEME
    )


def resolve_startup_theme(local_theme: str) -> str:
    """Load shared prefs, reconcile with *local_theme*, and return the winner.

    Convenience wrapper around :func:`decide_startup_theme` for the two
    entrypoints (viewer ``app.py``): reads the shared file once,
    applies the shared-wins / local-promote rule, and — if a promotion is due
    — writes the promoted value back via :func:`update_shared_prefs` (best
    effort; an ``OSError`` is logged and swallowed so a read-only data dir can
    never block startup).  The caller passes the result to ``apply_theme``.

    **Degraded load = no promotion.**  When the shared file exists but could
    not be read this session (``_degraded_load``), its default ``theme`` is a
    stand-in, not evidence that shared is unset — promoting the local value
    off it could overwrite the *other* tool's real choice (the later
    ``update_shared_prefs`` re-reads the file, so if it has become readable
    again the degraded guard would no longer protect it).  In that case the
    session simply runs on the local theme and writes nothing.

    Thin wrapper over :func:`_resolve_startup_scalar`.
    """
    return _resolve_startup_scalar(
        local_theme, field="theme", valid=_VALID_THEMES, default=_DEFAULT_THEME
    )


def decide_startup_language(
    shared_language: str, local_language: str
) -> tuple[str, str | None]:
    """Reconcile a tool's local language with the shared one at startup.

    Exact analogue of :func:`decide_startup_theme` (shared wins when it holds
    a real non-default choice; otherwise a non-default local value is promoted
    into shared once; else nothing is written).  See that function for the
    full rationale.  Values outside :data:`_VALID_LANGUAGES` are treated as the
    default so a junk field can never win or be promoted.

    Returns ``(effective_language, language_to_write)`` where the second item
    is the value to persist back to shared prefs, or ``None`` when no write is
    needed.  As with :func:`decide_startup_theme`, "" is the unset sentinel and
    an explicit shared choice is authoritative (durable across resets).

    Thin wrapper over :func:`_decide_startup_scalar` — the same single
    implementation the theme decider uses, so the two can never drift.
    """
    return _decide_startup_scalar(
        shared_language,
        local_language,
        valid=_VALID_LANGUAGES,
        default=_DEFAULT_LANGUAGE,
    )


def resolve_startup_language(local_language: str) -> str:
    """Load shared prefs, reconcile with *local_language*, return the winner.

    Language analogue of :func:`resolve_startup_theme`: shared wins if set,
    else the tool's legacy local value is promoted into shared once (best
    effort — an ``OSError`` is logged and swallowed).  The caller passes the
    result to ``common.i18n.set_locale``.

    **Degraded load = no promotion**, for the same reason as the theme
    resolver: when the shared file could not be read this session its default
    ``language`` is a stand-in, not evidence that shared is unset, so the
    session runs on the local language and writes nothing.

    Thin wrapper over :func:`_resolve_startup_scalar`.
    """
    return _resolve_startup_scalar(
        local_language,
        field="language",
        valid=_VALID_LANGUAGES,
        default=_DEFAULT_LANGUAGE,
    )


def apply_library_roots(
    *,
    added: list[str],
    removed: list[str],
    order: list[str] | None = None,
) -> list[str]:
    """Apply a library-root **diff** onto the freshest on-disk list.

    ライブラリルートの書き込みを「セッション開始時に読んだリスト全体の置換」に
    すると、:func:`update_shared_prefs` が docstring で約束する「直前に読み
    直した最新のディスク内容へ変更を載せる」契約から外れる。degraded フラグは
    *書込時点* の読み取り可否しか表現できないため、「起動時に読めず →
    書込時にはファイルが復帰」の経路で
    起動時スナップショット由来の全置換が実データを消せてしまう。
    書込を「値の置換」ではなく「差分の適用」にすることで、スナップショット
    の古さが構造的に無害になる: 他プロセス（書き込み側のプラグイン等）が途中で足した
    ルートも保存される。

    手順: :func:`load_shared_prefs` の**最新**リストから *removed* を除き、
    *added* のうち未登録のものを（*added* の順で）末尾へ足す。*order*
    （ライブラリ管理ダイアログが提示した並び）を渡すと、結果をその順序で
    **安定ソート**する — *order* に無い未知のエントリ（並行書き込みの追加分）
    は相対順を保って末尾に残る。

    Degraded load（このセッションでファイルを読めていない）のときは書かず、
    計算したマージ結果だけを返す（呼び出し元の UI 表示用）。Write failures
    (``OSError``) are logged and swallowed — the shared list is a convenience,
    never load-bearing.  Returns the list actually written (normalised by the
    :class:`SharedPrefs` validator) on success.
    """
    # 照合キーは ``os.path.normcase`` 済み・保存する値は生の綴り
    # （:meth:`SharedPrefs._normalise` の去重と同じ規則）。綴り違いの
    # 「C:/a」を渡した削除が「C:/A」の行を落とせない / 追加が 2 件目を生む、
    # という非対称をここで閉じる。POSIX では normcase が恒等なので無影響。
    added = [p for p in added if p]
    removed_set = {os.path.normcase(p) for p in removed if p}
    prefs = load_shared_prefs()
    latest = list(prefs.library_roots)
    merged = [r for r in latest if os.path.normcase(r) not in removed_set]
    present = {os.path.normcase(r) for r in merged}
    for p in added:
        key = os.path.normcase(p)
        if key not in present:
            merged.append(p)
            present.add(key)
    if order is not None:
        pos = {os.path.normcase(p): i for i, p in enumerate(order)}
        merged.sort(  # 安定 — 未知は末尾
            key=lambda r: pos.get(os.path.normcase(r), len(pos))
        )
    if prefs._degraded_load:
        # Degraded stand-in: its root list is NOT the real on-disk list, and
        # ``update_shared_prefs`` re-reads the file — if it has become
        # readable again by then, a write derived from the stand-in would
        # replace the real list.  Same no-write rule as
        # :func:`resolve_startup_theme`.
        logger.warning(
            "shared_prefs.json unreadable this session — library roots not "
            "written to shared prefs"
        )
        return merged
    if merged == latest:
        return merged  # no change — avoid a pointless read-modify-write
    try:
        return update_shared_prefs(library_roots=merged).library_roots
    except OSError as exc:
        logger.warning("could not write library roots to shared_prefs.json: {}", exc)
        return merged


def add_library_root(path: str) -> None:
    """Append *path* to the shared library-root list (best effort, deduped).

    Called when a tool establishes an effective library root — the viewer's
    browse root, or a plugin's output root.  The 1-element form of :func:`apply_library_roots`,
    so it applies onto the freshest on-disk list; re-adding an
    existing root is a harmless no-op and a blank path is ignored.  Write
    failures are logged and swallowed inside :func:`apply_library_roots`.
    """
    if not path:
        return
    apply_library_roots(added=[path], removed=[])


def remove_library_root(path: str) -> None:
    """Drop *path* from the shared library-root list (best effort).

    The mirror of :func:`add_library_root` for the library-management UI
    — the 1-element removal form of :func:`apply_library_roots`.
    A no-op when *path* is blank or not currently registered.
    """
    if not path:
        return
    apply_library_roots(added=[], removed=[path])
