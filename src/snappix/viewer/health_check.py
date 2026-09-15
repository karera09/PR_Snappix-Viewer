"""Library health-check scanner (Qt-free pure logic).

Walks a library root (or a chosen subtree) and reports common
integrity problems that build up over many incremental-sync runs.  Kept
GUI-free so the detection logic can be unit-tested without any Qt dependency
(the dialog in :mod:`snappix.viewer.health_dialog` drives this off-thread and
renders the results).

Detected categories (:data:`CATEGORY_ORDER`):

* ``part`` — leftover ``*.part`` files from an interrupted download.
* ``zero_byte`` — zero-length **library** files (images / attachments /
  ``#thumb#`` — and a zero-byte ``post.md`` is reported too, as a real
  anomaly).  The range is :func:`is_library_content_name`, so a marker file
  the user placed on purpose (``.gitkeep`` / ``.nomedia``) is not "a problem"
  and never reaches the bulk delete.
* ``missing_post_md`` — a folder that *looks* like a post folder (it holds
  image/attachment files but no ``post.md``, and no ``post.md``-bearing
  descendant) yet has no ``post.md`` of its own.
* ``old_bloat`` — an incremental-sync ``old/`` shelter whose total size
  exceeds :data:`OLD_BLOAT_THRESHOLD` (informational only — no delete action;
  the user decides whether the shelved history is worth keeping).
* ``empty_folder`` — a folder with no files anywhere beneath it (an empty
  post folder left behind).
* ``unreadable`` — a folder whose one-level scan raised ``OSError`` (access
  denied, an offline NAS share, a disconnected mount…).  Informational only:
  it tells the user that this subtree went **uninspected**, so a clean report
  can be distinguished from "could not look" (#171).

Each problem is a :class:`HealthIssue`.  When the problem sits inside a
post folder whose ``post.md`` carries a postref (service / post_id /
creator_id), that postref is attached as context (which service/post the
problem belongs to).

The scan is a generator (:func:`iter_issues`) that also invokes a progress
callback with the running count of scanned folders, and polls a cancel
predicate at folder boundaries so a large NAS tree can be aborted promptly.
:func:`run_health_check` collects the generator into a :class:`HealthReport`
(list + per-category counts).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

from ..common.i18n import t
from .folder_scan import (
    ARCHIVE_SUFFIXES,
    AUDIO_SUFFIXES,
    DOCUMENT_SUFFIXES,
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    POST_MD_NAME,
    VIDEO_SUFFIXES,
    is_meta_or_marker_name,
)
from .post_md import read_post_ref

#: Category ids in the order the dialog groups them.
CATEGORY_PART = "part"
CATEGORY_ZERO_BYTE = "zero_byte"
CATEGORY_MISSING_POST_MD = "missing_post_md"
CATEGORY_OLD_BLOAT = "old_bloat"
CATEGORY_EMPTY_FOLDER = "empty_folder"
CATEGORY_UNREADABLE = "unreadable"

CATEGORY_ORDER = (
    CATEGORY_PART,
    CATEGORY_ZERO_BYTE,
    CATEGORY_MISSING_POST_MD,
    CATEGORY_OLD_BLOAT,
    CATEGORY_EMPTY_FOLDER,
    CATEGORY_UNREADABLE,
)

#: i18n catalog keys for each category's dialog-header label.
_CATEGORY_LABEL_KEYS = {
    CATEGORY_PART: "viewer.health_check.category_part",
    CATEGORY_ZERO_BYTE: "viewer.health_check.category_zero_byte",
    CATEGORY_MISSING_POST_MD: "viewer.health_check.category_missing_post_md",
    CATEGORY_OLD_BLOAT: "viewer.health_check.category_old_bloat",
    CATEGORY_EMPTY_FOLDER: "viewer.health_check.category_empty_folder",
    CATEGORY_UNREADABLE: "viewer.health_check.category_unreadable",
}

def category_label(category: str) -> str:
    """The dialog-header label for *category*, resolved **at call time**.

    Not a module-level dict of resolved strings: this module is pulled in by
    ``main_window`` at import time (for :data:`PART_SUFFIX`), so an
    import-time ``t()`` would bake in whatever locale was active when the
    window module loaded.  Today the entry point happens to call
    ``set_locale`` first, but that is an ordering accident, not a contract.
    """
    return t(_CATEGORY_LABEL_KEYS[category])

#: i18n keys for each category's plain-language explanation (J04).  These spell
#: out what each writer-side concept means and whether it is safe to
#: delete, so the dialog isn't a wall of jargon (.part / old/ / #thumb# …).
_CATEGORY_EXPLANATION_KEYS = {
    CATEGORY_PART: "viewer.health_check.explain_part",
    CATEGORY_ZERO_BYTE: "viewer.health_check.explain_zero_byte",
    CATEGORY_MISSING_POST_MD: "viewer.health_check.explain_missing_post_md",
    CATEGORY_OLD_BLOAT: "viewer.health_check.explain_old_bloat",
    CATEGORY_EMPTY_FOLDER: "viewer.health_check.explain_empty_folder",
    CATEGORY_UNREADABLE: "viewer.health_check.explain_unreadable",
}

def category_explanation(category: str) -> str:
    """The one-line explanation for *category* (see :func:`category_label`)."""
    return t(_CATEGORY_EXPLANATION_KEYS[category])

#: Categories that are **informational only** — no problem, no delete action
#: (J04/J05).  ``old_bloat`` merely reports the size of an incremental-sync
#: shelter; ``missing_post_md`` merely notes a folder that has no ``post.md``
#: — which is the *normal* state for a plain image library ("post.md が無い状態
#: が基本", per the root CLAUDE.md), so flagging it as a problem would report
#: nearly every folder of an ordinary library as broken.  ``unreadable``
#: (#171) merely reports that a subtree could not be inspected — nothing there
#: is known to be broken, and there is nothing to delete.  The dialog counts
#: these separately from real problems and badges them 「情報」 so neither a big
#: ``old/`` nor a post.md-less folder inflates the problem count.
INFO_CATEGORIES = frozenset(
    {CATEGORY_OLD_BLOAT, CATEGORY_MISSING_POST_MD, CATEGORY_UNREADABLE}
)

#: Categories that are informational only **in a library with no ``post.md``
#: anywhere**.  The reasoning that demoted ``missing_post_md`` applies just as
#: literally to ``empty_folder``: in a tool-written library an empty folder is a
#: post whose files went away, but in a plain image library — the product's
#: default — it is a folder the user made and has not filled yet (「未整理」
#: 「あとで分類する」).  Counting those as problems and offering a
#: danger-styled 「空フォルダをすべて削除」 that ``shutil.rmtree``\\s them is
#: the same mistake, on the one category that actually deletes.
_INFO_CATEGORIES_WITHOUT_POST_MD = frozenset({CATEGORY_EMPTY_FOLDER})

#: An ``old/`` shelter bigger than this is flagged as bloat (informational).
OLD_BLOAT_THRESHOLD = 500 * 1024 * 1024  # 500 MiB

#: Suffix of an interrupted download's partial file.
PART_SUFFIX = ".part"

#: The incremental-sync shelter folder name.
OLD_DIR_NAME = "old"

#: Source / attachment formats a library holds that the viewer does not render
#: itself, so ``folder_scan`` has no routing set for them.  They are still the
#: user's content, which is what this module needs to know.
_ATTACHMENT_SUFFIXES = frozenset({".psd", ".clip", ".doc", ".docx"})

#: Every extension that counts as "a file of the user's library".  Derived
#: from ``folder_scan``'s routing sets rather than written out again: that
#: module already answers "what kind of file is this" for the scan, the recent
#: list and the search filters, and a second hand-maintained table drifted
#: from it in both directions (it lacked ``.avi`` / ``.cbz`` / ``.md`` and had
#: source formats folder_scan never classifies).
_CONTENT_SUFFIXES = (
    IMAGE_SUFFIXES
    | PDF_SUFFIXES
    | VIDEO_SUFFIXES
    | AUDIO_SUFFIXES
    | ARCHIVE_SUFFIXES
    | DOCUMENT_SUFFIXES
    | _ATTACHMENT_SUFFIXES
)


def is_library_content_name(name: str) -> bool:
    """True when *name* is a file of the user's library (or its metadata).

    The one answer to "is this the library's content, or something else that
    happens to sit in the folder".  Two kinds count: a content extension, and
    the metadata / marker names ``post.md`` / ``#thumb#…`` that
    :func:`snappix.viewer.folder_scan.is_meta_or_marker_name` owns (matched
    case-insensitively there — this module used to compare ``#thumb#``
    case-sensitively and missed ``#Thumb#…``).

    Everything else — a ``.gitkeep`` / ``.nomedia`` / a sync tool's marker, a
    stray ``.ini`` — is the user's own bookkeeping, not library content.
    """
    return (
        Path(name).suffix.lower() in _CONTENT_SUFFIXES
        or is_meta_or_marker_name(name)
    )


@dataclass
class PostRef:
    """The ``(service, post_id, creator_id)`` triple from a post folder.

    Every element is optional, exactly as the three meta keys are in
    docs/formats/post-md.md §3 — a ``post.md`` that names its service and
    post id but no creator still identifies the post.
    """

    service: str | None
    post_id: str | None
    creator_id: str | None


@dataclass
class HealthIssue:
    """One detected problem.

    ``path`` is the offending file (``part`` / ``zero_byte``) or folder
    (``missing_post_md`` / ``old_bloat`` / ``empty_folder``).  ``detail`` is a
    short human note.  ``size`` is the relevant byte size (the file's size, or
    the ``old/`` total) or ``None`` when size is not meaningful.  ``postref``
    is set when the problem sits inside a post folder whose ``post.md`` carries
    a postref (context only — which service/post the problem belongs to).
    ``mtime`` is the scan-time ``st_mtime`` of a **file** issue (``part`` /
    ``zero_byte``; ``None`` for folder issues and on stat error), taken from
    the same cached ``DirEntry.stat`` as ``size``: the report is a scan-time
    snapshot, so a bulk delete re-checks ``(size, mtime)`` right before
    ``os.remove`` and skips a file that changed since.  A file being actively
    written (an in-process writer plugin, or an external tool) must never
    be deleted mid-download — and a download's output file is 0 bytes for as
    long as it takes the first byte to arrive, so the zero-byte category needs
    the same snapshot the ``.part`` one does (項目#172).
    """

    category: str
    path: Path
    detail: str = ""
    size: int | None = None
    postref: PostRef | None = None
    #: Scan-time ``st_mtime`` of a file issue (``None`` for folder issues /
    #: on stat error).
    mtime: float | None = None


@dataclass
class HealthReport:
    """A completed scan: all issues plus per-category counts."""

    issues: list[HealthIssue] = field(default_factory=list)
    #: True when the scan stopped early because the cancel predicate fired.
    cancelled: bool = False
    #: Number of folders visited (progress denominator surrogate).
    scanned_folders: int = 0
    #: True when the scanned tree holds at least one ``post.md`` anywhere
    #: (``old/`` shelters excluded).  The dialog uses this to fold the
    #: 「post.md 欠落」 category for post.md-less libraries (UIレビュー 07-25
    #: #18).  Filled from the walk's own post-order fold (no second walker);
    #: defaults to **True** = "do not suppress" so a cancelled / failed scan
    #: never hides the category by accident.
    library_has_post_md: bool = True

    def counts(self) -> dict[str, int]:
        """Issue count per category id (categories with 0 included)."""
        out = {cat: 0 for cat in CATEGORY_ORDER}
        for issue in self.issues:
            out[issue.category] = out.get(issue.category, 0) + 1
        return out

    def issues_for(self, category: str) -> list[HealthIssue]:
        return [i for i in self.issues if i.category == category]

    def info_categories(self) -> frozenset[str]:
        """Which categories are informational **for this report**.

        Always :data:`INFO_CATEGORIES`, plus
        :data:`_INFO_CATEGORIES_WITHOUT_POST_MD` when the scanned tree holds
        no ``post.md`` at all.  The dialog badges 「情報」, excludes from the
        problem tally and refuses bulk deletes by this set, so the demotion
        lands on the summary, the badge and the delete button at once.

        A cancelled / failed scan leaves ``library_has_post_md`` at its
        default ``True``, i.e. demotes nothing — never quietly disable a
        cleanup action on the strength of a partial walk.
        """
        if self.library_has_post_md:
            return INFO_CATEGORIES
        return INFO_CATEGORIES | _INFO_CATEGORIES_WITHOUT_POST_MD

    def problem_count(self) -> int:
        """Number of *actionable* issues (excludes informational categories).

        Backs the dialog's 「問題 N 件」 summary — ``old_bloat`` and any other
        member of :meth:`info_categories` is reported but not counted as a
        problem (J04/J05), so an oversized ``old/`` shelter never inflates the
        tally.
        """
        info = self.info_categories()
        return sum(1 for i in self.issues if i.category not in info)

    def info_count(self) -> int:
        """Number of informational-only issues (``old_bloat`` etc.)."""
        info = self.info_categories()
        return sum(1 for i in self.issues if i.category in info)


def _read_postref(md_path: Path) -> PostRef | None:
    """Resolve a folder's postref from ``post.md`` (bounded head read).

    ``None`` means "this ``post.md`` says nothing about the post's identity",
    which is what makes :class:`_RefScope` fall back to the nearest ancestor.
    Any **one** of the three keys is enough to stop that fallback: requiring
    service *and* creator made a ``post.md`` carrying only ``service`` /
    ``post_id`` defer to an ancestor, i.e. attribute this folder's problems to
    a *different* post.  All three are individually optional per
    docs/formats/post-md.md §3.
    """
    service, post_id, creator_id = read_post_ref(md_path)
    if not service and not post_id and not creator_id:
        return None
    return PostRef(service=service, post_id=post_id, creator_id=creator_id)


class _RefScope:
    """The postref scope of a folder: nearest ``post.md`` + enclosing scopes.

    A lazy stand-in for the eagerly-read ``PostRef`` the walk used to carry
    (項目#75): ``HealthIssue.postref`` is context-only, so reading every
    ``post.md`` up front made even a perfectly clean library pay one
    open+read per post folder (+70% scan time measured on local SSD, worse
    on NAS).  The walk now just remembers *which file would answer* and the
    chain to fall back through (a ``post.md`` head without a valid postref
    defers to the nearest ancestor's, matching the old ``… or inherited_ref``
    behaviour); the read happens only when an issue is actually yielded
    inside the scope, memoised per file by :func:`iter_issues`.
    """

    __slots__ = ("md_path", "parent")

    def __init__(self, md_path: Path, parent: "_RefScope | None") -> None:
        self.md_path = md_path
        self.parent = parent


@dataclass
class _FolderScan:
    """Cheap one-level ``scandir`` snapshot of a folder."""

    #: ``(path, size, mtime)`` per regular file, size and mtime taken from the
    #: ``scandir`` ``DirEntry.stat`` (already cached by the OS enumeration — 0
    #: extra syscalls) so the per-file categories never re-``stat`` each file
    #: with a second ``Path.stat`` (review 2026-08-27 #76).  Size is ``-1`` and
    #: mtime ``None`` when the entry's stat failed (won't match the ``== 0``
    #: zero-byte test).  The mtime rides along for the ``part`` issues' bulk
    #: delete re-check (項目#172).
    files: list[tuple[Path, int, float | None]] = field(default_factory=list)
    subdirs: list[Path] = field(default_factory=list)
    has_post_md: bool = False
    #: Actual ``post.md`` path (real casing) when one exists here.
    post_md_path: Path | None = None
    #: True when the one-level scan raised ``OSError`` (unreadable folder).
    unreadable: bool = False
    #: True when the folder holds an entry that is neither a plain directory nor
    #: a regular file — a symlink / Windows junction (detected explicitly, since
    #: a junction's ``is_dir(follow_symlinks=False)`` is True on Windows), a
    #: fifo, a socket…  Not a file for the per-file categories, but real content
    #: the user placed here, so the folder is never treated as empty.
    has_other_entries: bool = False
    #: Link entries (symlink / junction) that point at a *directory*.  Kept
    #: apart from ``has_other_entries`` so the walk can say "this subtree went
    #: uninspected" instead of passing it off as nothing: the walk deliberately
    #: does not descend through them (#108 — the bulk empty-folder delete
    #: ``rmtree``\\s a junction's **target**), which would otherwise make a
    #: report read "no problems found" for a creator folder the user moved to
    #: another drive.
    link_dirs: list[Path] = field(default_factory=list)


def _is_link_entry(entry: os.DirEntry) -> bool:
    """True when *entry* is a symlink or a Windows directory junction.

    ``DirEntry.is_junction`` exists on Python 3.12+; older interpreters only see
    symlinks (junctions there fall through to the directory branch, matching the
    pre-3.12 behaviour).  Both probes swallow no exceptions of their own — the
    caller's ``try`` handles an unreadable entry.
    """
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction()) if is_junction is not None else False


def _scan_folder(folder: Path) -> _FolderScan:
    out = _FolderScan()
    try:
        entries = list(os.scandir(folder))
    except OSError:
        out.unreadable = True
        return out
    for entry in entries:
        try:
            if _is_link_entry(entry):
                # A symlink or (Windows) junction.  A POSIX symlink fails both
                # ``follow_symlinks=False`` probes below and would land in the
                # ``else`` branch, but a *Windows junction* reports
                # ``is_dir(follow_symlinks=False)`` as **True**, so it must be
                # detected explicitly *before* the directory branch — otherwise a
                # folder holding only link structure is walked as empty subdirs
                # and the bulk 空フォルダ delete rmtree's the user's links away
                # (#108 / review 2026-08-27 #2).  Treat it as real content: not a
                # file for the per-file categories, but the folder is *not* empty.
                out.has_other_entries = True
                # A link to a *directory* also hides a whole subtree from the
                # walk, which the report has to admit to (see ``link_dirs``).
                try:
                    if entry.is_dir():  # follows the link, on purpose
                        out.link_dirs.append(Path(entry.path))
                except OSError:
                    # Broken / unreachable target: nothing to inspect beyond
                    # it, and the entry already counts as content above.
                    pass
            elif entry.is_dir(follow_symlinks=False):
                out.subdirs.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                p = Path(entry.path)
                try:
                    st = entry.stat(follow_symlinks=False)
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    # unknown; won't match the ==0 zero-byte test, and the
                    # part re-check treats an unknown snapshot as "can't
                    # prove unchanged" (skip, never delete — 項目#172).
                    size, mtime = -1, None
                out.files.append((p, size, mtime))
                # Case-insensitive, matching folder_scan's post.md detection.
                if entry.name.lower() == POST_MD_NAME:
                    out.has_post_md = True
                    out.post_md_path = p
            else:
                # Symlink / junction / other entry kind: neither probe matches
                # (both are ``follow_symlinks=False``).  It is not a file for
                # the per-file categories, but the folder is *not* empty —
                # flagging it would let the bulk 空フォルダ delete rmtree the
                # user's link構成 away (#108).
                out.has_other_entries = True
        except OSError:
            # Unknown entry kind → err on the side of "not empty" so we never
            # flag (or auto-delete) a folder we could not fully inspect.
            out.unreadable = True
    return out


def dir_has_any_file(folder: Path) -> bool:
    """True when *folder* has at least one file anywhere beneath it.

    Standalone re-check used by the dialog just before a bulk 空フォルダ delete
    (a folder that was empty at scan time may have gained files since).  It
    reuses the same one-level scandir + child fold-in shape as
    :func:`iter_issues` so the emptiness semantics stay identical: an
    unreadable folder counts as non-empty (we must not rmtree what we cannot
    inspect), a symlink / junction entry likewise counts as non-empty (#108),
    and ``old/`` shelters count toward their parent's emptiness just
    like every other subfolder.

    Already-deleted root: ``False``.  A folder that is gone is already in the
    state the caller wants, so the delete path treats it as a success — the
    same carve-out ``health_dialog``'s two file-level re-checks document.
    Without it the missing folder's ``OSError`` folds into ``unreadable`` and
    the answer comes back "has content", i.e. the delete reports a skip for
    something that no longer exists.

    倒し方は :func:`~snappix.viewer.health_dialog._file_changed_since_scan` と
    同じで、**「消滅」だけを ``False`` へ倒す**: ``Path.exists()`` は
    ``OSError`` を握って ``False`` を返すので、落ちた共有 / 権限を失った
    フォルダまで「もう無い＝消してよい」側へ倒れていた（同じモジュールが
    ``unreadable`` を「消さない」へ倒している規律の逆）。``os.lstat`` を直接
    試し、``FileNotFoundError`` / ``NotADirectoryError`` のときだけ ``False``、
    他の ``OSError`` は ``True``（＝見に行けないものは消さない）。

    No cycle guard: ``_scan_folder`` never puts a link into ``subdirs`` (links
    are content, not a descent path — #108), and a directory cannot be
    hard-linked, so ``subdirs`` cannot reach the same real directory twice.
    Re-admitting links here means restoring the ``dir_identity`` dedupe with
    them.
    """
    try:
        os.lstat(folder)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    stack: list[Path] = [folder]
    while stack:
        scan = _scan_folder(stack.pop())
        if scan.unreadable or scan.files or scan.has_other_entries:
            return True
        stack.extend(scan.subdirs)
    return False


def _looks_like_content_folder(
    files: list[tuple[Path, int, float | None]],
) -> bool:
    """True when *files* include any content-looking file."""
    return any(is_library_content_name(f.name) for f, _size, _mtime in files)


def iter_issues(
    root: Path,
    *,
    progress: Callable[[int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_root_facts: Callable[[bool], None] | None = None,
) -> Iterator[HealthIssue]:
    """Yield every :class:`HealthIssue` under *root* (depth-first).

    ``progress`` is invoked with the running folder count at each folder
    boundary (for a scanning progress display).  ``is_cancelled`` is polled at
    each folder boundary; when it returns True the walk stops (no more issues
    are yielded — the generator simply returns).

    ``old/`` shelters are walked by the same traversal as everything else
    (sharing the cycle guard, the cancel polling and ``_scan_folder``'s entry
    classification — 項目#71), but their contents are intentionally-shelved
    history, not live post content: inside a shelter the per-file categories
    and the folder verdicts (``empty_folder`` / ``missing_post_md``) are
    suppressed, and only the aggregate file size is folded up post-order to
    the shelter root, where the ``old_bloat`` verdict is emitted.  The scan
    **root** is exempt (#109): when the user explicitly points the check at an
    ``old/`` folder they want its leftovers inspected.  A folder named ``old``
    is a shelter only when its **immediate parent carries a ``post.md``** —
    that is the incremental-sync convention this compatibility rule reads
    (the shelter is created as ``<post folder>/old/``).  In a plain image
    library (the product default: post.md is optional) the user's own
    ``写真/old/`` is an ordinary folder and its ``.part`` / zero-byte / empty
    subfolders are reported normally — レビュー 2026-09-03 項目 #104.

    A folder whose one-level scan failed (``OSError``) is reported as an
    informational ``unreadable`` issue (#171) — a clean report must be
    distinguishable from "could not look" — and still counts as non-empty.

    ``on_root_facts`` is invoked once, at the root's post-order visit, with
    "does the tree hold any ``post.md``" (``old/`` excluded) — a fact the fold
    already computes, exported so the dialog needs no second walker (項目#71).
    Not invoked when the walk is cancelled before the root completes.

    Traversal is **post-order** so each directory's ``os.scandir`` runs exactly
    once: a folder's "has any file beneath it" fact is the fold of its own
    one-level scan with its children's already-computed facts, instead of a
    fresh recursive descent per folder (which re-walked every leaf from all its
    ancestors — an O(depth×size) I/O blow-up on cold NAS trees).  File-level
    issues (``part`` / ``zero_byte``) are still yielded eagerly on first visit;
    only the folder-level verdicts wait for the children to report back.
    """
    scanned = 0
    # Lazy postref resolution (項目#75): scopes name the ``post.md`` that
    # *would* answer; the bounded head read runs only when an issue inside the
    # scope is actually yielded, at most once per file (memoised here).
    ref_cache: dict[Path, PostRef | None] = {}

    def _resolve_ref(scope: _RefScope | None) -> PostRef | None:
        while scope is not None:
            if scope.md_path in ref_cache:
                ref = ref_cache[scope.md_path]
            else:
                ref = _read_postref(scope.md_path)
                ref_cache[scope.md_path] = ref
            if ref is not None:
                return ref
            # Head without a valid postref → defer to the nearest ancestor's
            # (the old eager walk's ``… or inherited_ref`` fallback).
            scope = scope.parent
        return None

    # A frame is pushed once with ``entered=False`` (pre-order work: scan, file
    # issues, push children), then revisited with ``entered=True`` (post-order
    # work: fold in the children's facts and emit the folder-level verdicts).
    class _Frame(NamedTuple):
        folder: Path
        #: Nearest postref scope (a post.md at or above *folder*).
        scope: _RefScope | None
        entered: bool
        #: True when a *strict* ancestor carries a ``post.md`` — a content
        #: subfolder beneath a post folder is a legitimate layout
        #: (docs/formats/post-md.md §1: post.md sits at the post root, media may
        #: live in subfolders), so it must not be flagged missing_post_md
        #: (review 2026-08-27 #73).  Distinct from ``scope``, which may resolve
        #: to None when the ancestor's post.md carries no valid postref even
        #: though the file exists.
        ancestor_has_md: bool
        #: True when the *immediate* parent carries a ``post.md`` — the
        #: condition that makes an ``old`` child an actual writer-side shelter
        #: (see the ``old_root`` assignment below).
        parent_has_md: bool
        #: The ``old/`` shelter this folder sits in (itself included), or None
        #: outside any shelter.
        old_root: Path | None

    stack: list[_Frame] = [_Frame(root, None, False, False, False, None)]
    # No cycle guard here: ``_scan_folder`` classifies every symlink / junction
    # as content and never adds it to ``subdirs`` (#108 — the bulk empty-folder
    # delete would rmtree a junction's target), and a directory cannot be
    # hard-linked, so the descent can never reach the same real directory by
    # two names.  Paying a ``dir_identity`` stat per subdirectory to dedupe a
    # set that structurally has no duplicates costs one extra round trip per
    # folder on a cold share.  Descending into links again means restoring the
    # dedupe (``folder_scan.should_descend`` is where that policy lives).
    # folder → True if any file exists anywhere beneath it (filled post-order,
    # consumed by the parent, then discarded to bound memory).
    has_file_below: dict[Path, bool] = {}
    # folder → True if a post.md exists at or below it (same lifecycle).  Folded
    # from children instead of a separate bounded BFS (_has_post_md_descendant),
    # so it is both exact (no 64-dir probe cap) and re-uses the single scandir.
    post_md_below: dict[Path, bool] = {}
    # folder → sum of file sizes at-or-below it.  Tracked only inside ``old/``
    # shelters (same lifecycle as the other fold dicts) and consumed by the
    # shelter root's old_bloat verdict.
    size_below: dict[Path, int] = {}
    # Per-folder pre-order facts kept until the folder's post-order visit.
    own_has_file: dict[Path, bool] = {}
    own_has_post_md: dict[Path, bool] = {}
    own_size: dict[Path, int] = {}
    looks_like_content: dict[Path, bool] = {}
    folder_scopes: dict[Path, _RefScope | None] = {}
    child_dirs: dict[Path, list[Path]] = {}

    while stack:
        (
            folder, inherited_scope, entered, ancestor_has_md, parent_has_md,
            old_root,
        ) = stack.pop()

        if entered:
            # --- post-order: fold children up, emit folder-level verdicts ---
            has_here = own_has_file.pop(folder, False)
            has_md_here = own_has_post_md.pop(folder, False)
            size_here = own_size.pop(folder, 0)
            children = child_dirs.pop(folder, [])
            child_has_md = False
            for sub in children:
                if has_file_below.pop(sub, False):
                    has_here = True
                if post_md_below.pop(sub, False):
                    child_has_md = True
                size_here += size_below.pop(sub, 0)
            has_file_below[folder] = has_here
            post_md_below[folder] = has_md_here or child_has_md
            # Pop unconditionally — inside a short-circuit the entry would
            # survive whenever an earlier operand is falsy (memory leak).
            content_here = looks_like_content.pop(folder, False)
            folder_scope = folder_scopes.pop(folder, inherited_scope)

            if folder == old_root:
                # old/ shelter root: emit the aggregate-size verdict.  The
                # shelter is never a post itself, so it contributes no
                # post.md-below; nor is it ever flagged empty / missing.
                post_md_below[folder] = False
                if size_here >= OLD_BLOAT_THRESHOLD:
                    from ..common.format import format_bytes

                    yield HealthIssue(
                        category=CATEGORY_OLD_BLOAT,
                        path=folder,
                        detail=t(
                            "viewer.health_check.detail_old_bloat",
                            size=format_bytes(size_here),
                        ),
                        size=size_here,
                        postref=_resolve_ref(folder_scope),
                    )
                continue
            if old_root is not None:
                # Inside a shelter: no folder verdicts, just propagate the
                # folded facts (file presence for the parent's emptiness, and
                # the running size for the shelter root's bloat check).
                size_below[folder] = size_here
                continue

            if folder == root and on_root_facts is not None:
                on_root_facts(post_md_below[folder])

            # missing post.md: content-bearing folder with no post.md here, no
            # post.md in any descendant (else it's a container of posts), and no
            # post.md in any ancestor (else it's a media subfolder of a post —
            # a legitimate layout, review 2026-08-27 #73).
            if (
                not has_md_here
                and content_here
                and not child_has_md
                and not ancestor_has_md
            ):
                yield HealthIssue(
                    category=CATEGORY_MISSING_POST_MD,
                    path=folder,
                    detail=t("viewer.health_check.detail_missing_post_md"),
                    size=None,
                    postref=_resolve_ref(folder_scope),
                )

            # Never flag the scan root itself as empty: the empty_folder
            # verdict feeds a bulk 空フォルダ delete (shutil.rmtree), and the
            # root is the folder the user is currently viewing / ran the check
            # on — deleting it would remove the live library root, not clean up
            # a leftover post folder beneath it.
            if not has_here and folder != root:
                yield HealthIssue(
                    category=CATEGORY_EMPTY_FOLDER,
                    path=folder,
                    detail=t("viewer.health_check.detail_empty_folder"),
                    size=None,
                    postref=_resolve_ref(inherited_scope),
                )
            continue

        # --- pre-order: one folder boundary --------------------------------
        if is_cancelled is not None and is_cancelled():
            return
        scanned += 1
        if progress is not None:
            progress(scanned)

        # old/ shelter boundary — case-insensitive, matching post.md detection
        # (on Windows an "Old/" / "OLD/" shelter is the same folder).  The scan
        # root is exempt (#109; see the docstring).  ``parent_has_md`` is what
        # makes this an actual writer-side shelter rather than a folder the user
        # happened to name "old": the incremental-sync convention puts the
        # shelter directly under a **post folder**.
        # Without that condition, a plain image library — the product's default,
        # since post.md is optional — had every ``.part`` / zero-byte /
        # empty-folder issue under the user's own ``写真/old/`` silently dropped
        # (レビュー 2026-09-03 項目 #104).
        if (
            old_root is None
            and folder.name.lower() == OLD_DIR_NAME
            and folder != root
            and parent_has_md
        ):
            old_root = folder
        in_old = old_root is not None

        scan = _scan_folder(folder)

        # The postref scope for problems inside this folder: a post.md here
        # wins, otherwise inherit the nearest ancestor's.  The file is NOT
        # read here (項目#75) — ``_resolve_ref`` reads it lazily at the first
        # issue yielded inside the scope, so a clean library reads nothing.
        folder_scope = inherited_scope
        if scan.post_md_path is not None:
            folder_scope = _RefScope(scan.post_md_path, inherited_scope)

        # A folder we could not (fully) enumerate: surface the fact (#171) —
        # informational, no delete action — instead of silently passing an
        # uninspected subtree off as clean.
        if scan.unreadable:
            yield HealthIssue(
                category=CATEGORY_UNREADABLE,
                path=folder,
                detail=t("viewer.health_check.detail_unreadable"),
                size=None,
                postref=_resolve_ref(folder_scope),
            )

        # A junction / symlink to a directory is content we deliberately do
        # not walk through (#108), so its subtree is uninspected in exactly
        # the sense ``unreadable`` exists to report: without this row a
        # library whose creator folders were offloaded to another drive and
        # linked back reads as 「問題は見つかりませんでした」.  Informational,
        # never deletable — the row names the link, not its target.
        for link in scan.link_dirs:
            yield HealthIssue(
                category=CATEGORY_UNREADABLE,
                path=link,
                detail=t("viewer.health_check.detail_link_not_followed"),
                size=None,
                postref=_resolve_ref(folder_scope),
            )

        if in_old:
            # Shelved history: no per-file issues, only the size (folded up to
            # the shelter root's old_bloat verdict in post-order).  Sizes come
            # from the scandir DirEntry (review 2026-08-27 #76) — no re-stat.
            size = 0
            for _f, fsize, _fmtime in scan.files:
                if fsize > 0:
                    size += fsize
            own_size[folder] = size
        else:
            # --- per-file categories ---------------------------------------
            # Sizes come from the ``scandir`` DirEntry captured in
            # ``_scan_folder`` (already cached by the OS enumeration), so
            # neither the .part nor the zero-byte verdict re-``stat``s the
            # file (review 2026-08-27 #76).
            for f, fsize, fmtime in scan.files:
                if f.suffix.lower() == PART_SUFFIX:
                    yield HealthIssue(
                        category=CATEGORY_PART,
                        path=f,
                        detail=t("viewer.health_check.detail_part"),
                        size=fsize,
                        postref=_resolve_ref(folder_scope),
                        mtime=fmtime,
                    )
                    continue
                # Only the library's own content can be a zero-byte *anomaly*.
                # The category's explanation states outright that these are
                # safe to delete and the dialog offers a one-click bulk delete
                # that bypasses the recycle bin, so a user-placed marker file
                # (``.gitkeep`` / ``.nomedia`` / an empty placeholder note)
                # must not reach that list — it is 0 bytes on purpose and
                # stays 0 bytes, so the pre-delete re-check waves it through.
                if fsize == 0 and is_library_content_name(f.name):
                    yield HealthIssue(
                        category=CATEGORY_ZERO_BYTE,
                        path=f,
                        detail=t("viewer.health_check.detail_zero_byte"),
                        size=0,
                        postref=_resolve_ref(folder_scope),
                        mtime=fmtime,
                    )

        # Stash this folder's pre-order facts for the post-order fold (the
        # folder-level verdicts are emitted there, once the children have
        # reported their post.md-below / file-below facts), then push the
        # post-order frame and descend (children inherit postref scope).
        # An unreadable folder counts as "has file" so it's never flagged empty
        # (we couldn't inspect it), matching the old _dir_has_any_file guard.
        # A symlink / junction likewise keeps its folder non-empty (#108).
        own_has_file[folder] = (
            bool(scan.files) or scan.unreadable or scan.has_other_entries
        )
        own_has_post_md[folder] = scan.has_post_md
        looks_like_content[folder] = (
            False if in_old else _looks_like_content_folder(scan.files)
        )
        folder_scopes[folder] = folder_scope
        stack.append(
            _Frame(
                folder, folder_scope, True, ancestor_has_md, parent_has_md,
                old_root,
            )
        )
        # Children see this folder as an ancestor: a post.md *here* means every
        # descendant is a media subfolder of this post, not a missing-post_md
        # anomaly (review 2026-08-27 #73).
        child_ancestor_has_md = ancestor_has_md or scan.has_post_md
        for sub in scan.subdirs:
            stack.append(
                _Frame(
                    sub, folder_scope, False, child_ancestor_has_md,
                    scan.has_post_md, old_root,
                )
            )
        child_dirs[folder] = list(scan.subdirs)


def run_health_check(
    root: Path,
    *,
    progress: Callable[[int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> HealthReport:
    """Collect :func:`iter_issues` into a :class:`HealthReport`."""
    report = HealthReport()

    def _root_facts(has_post_md: bool) -> None:
        report.library_has_post_md = has_post_md

    for issue in iter_issues(
        root, progress=_wrap_progress(progress, report),
        is_cancelled=is_cancelled, on_root_facts=_root_facts,
    ):
        report.issues.append(issue)
    if is_cancelled is not None and is_cancelled():
        report.cancelled = True
    return report


def _wrap_progress(
    progress: Callable[[int], None] | None, report: HealthReport,
) -> Callable[[int], None]:
    def _cb(n: int) -> None:
        report.scanned_folders = n
        if progress is not None:
            progress(n)

    return _cb


# --------------------------------------------------------------------------
# Pure helpers for the dialog's bulk actions (Qt-free so they are unit-
# testable): which paths a bulk delete targets.
# --------------------------------------------------------------------------


def paths_for_category(issues: list[HealthIssue], category: str) -> list[Path]:
    """*category* の issue が指すパス（重複排除・順序維持）。

    (UIレビュー07-25 追修) 「重複排除して出現順を保つ」という規約の唯一の
    実装。:func:`deletable_paths` と、ダイアログの ``zero_byte`` 一括削除
    （#87 で追加。:func:`deletable_paths` の対象外なので直接こちらを使う）が
    共有する — 以前はダイアログ側が同じループを書き写していた。
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for issue in issues:
        if issue.category != category:
            continue
        if issue.path in seen:
            continue
        seen.add(issue.path)
        out.append(issue.path)
    return out


def deletable_paths(issues: list[HealthIssue], category: str) -> list[Path]:
    """Paths a **カテゴリ丸ごと**の一括削除が消すパス。

    Only ``part`` (files → ``os.remove``) and ``empty_folder`` (folders →
    ``shutil.rmtree``) are answered here; any other category returns an empty
    list.  ``old_bloat`` intentionally has no delete action and
    missing-post_md is a per-row decision.  ``zero_byte`` も一括削除自体は
    持つ（UIレビュー 07-25 #87）が、削除直前に「走査後に中身が書かれたか」を
    再確認する別扱いなので、この関数ではなく :func:`paths_for_category` を
    直接使う（``health_dialog._deletable_for`` が振り分ける）。
    De-duplicated, order-preserving.
    """
    if category not in (CATEGORY_PART, CATEGORY_EMPTY_FOLDER):
        return []
    return paths_for_category(issues, category)


__all__ = [
    "CATEGORY_EMPTY_FOLDER",
    "CATEGORY_MISSING_POST_MD",
    "CATEGORY_OLD_BLOAT",
    "CATEGORY_ORDER",
    "CATEGORY_PART",
    "CATEGORY_UNREADABLE",
    "CATEGORY_ZERO_BYTE",
    "INFO_CATEGORIES",
    "OLD_BLOAT_THRESHOLD",
    "HealthIssue",
    "HealthReport",
    "PostRef",
    "category_explanation",
    "category_label",
    "deletable_paths",
    "dir_has_any_file",
    "is_library_content_name",
    "iter_issues",
    "paths_for_category",
    "run_health_check",
]
