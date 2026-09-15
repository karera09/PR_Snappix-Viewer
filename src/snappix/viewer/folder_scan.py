"""Folder scanning for the viewer.

The viewer is a generic file browser: it lists the immediate children
(sub-directories *and* regular files) of the current root and surfaces
per-folder thumbnails + post.md metadata when present.  The children
listing itself is single-level — drilling down is an explicit user
action (double-click on a folder → that sub-folder becomes the new
root).

Thumbnail *resolution* does descend, however: when a listed sub-folder
contains no images directly but its children/grandchildren do
(gallery-per-subfolder layouts), a bounded BFS picks a preview image
from the shallowest depth that has one.  See ``_RECURSE_MAX_DIRS`` /
``_RECURSE_MAX_DEPTH`` for the limits that stop the search from walking
arbitrarily deep trees.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappush, heappushpop
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from ..common.post_meta import KEY_POST_ID, KEY_SERVICE
from .folder_preview_cache import FolderPreview
from .perf import measure
from ..common.post_meta import HEAD_READ_LIMIT, read_head
from .post_md import ParsedPost, parse_post_md

# Image extensions used both for thumbnail discovery and for the right-pane
# preview routing.
IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
)

# PDF files get first-page thumbnails via QPdfDocument (see
# :mod:`thumbnail_loader`) and a dedicated in-pane preview (see
# :class:`content.pdf_view.PdfView`).  They are also eligible for folder-level
# thumbnail discovery, but only as a fallback: within a single folder a
# non-marker image beats a PDF (see :func:`_scan_dir_images`), so galleries
# with real images keep their image thumbnails.
PDF_SUFFIXES = frozenset({".pdf"})

# Per-file thumbnailable suffixes: used by ``scan_children`` to decide whether
# a file entry gets a ``thumbnail_path``, and by the right pane / grid to
# decide whether to ask the loader for a thumbnail at all.
# Audio / video suffixes — used by the right / left pane icon bucketing and
# ContentView routing.
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v"}
)
AUDIO_SUFFIXES = frozenset(
    {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
)
MEDIA_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES

# Per-file thumbnailable suffixes: used by ``scan_children`` / FileListView to
# decide whether to request a thumbnail for a file entry.  VIDEO_SUFFIXES is
# included so individual video items show a frame preview; AUDIO_SUFFIXES is
# NOT included (no visual content to show).
# NOTE: _scan_dir_images (folder thumbnail discovery) uses IMAGE_SUFFIXES
# directly, so adding VIDEO_SUFFIXES here does NOT cause folder thumbnails to
# pick video files — that invariant is maintained.
THUMBNAILABLE_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES | VIDEO_SUFFIXES

# Archive + document suffix groups, used only by the advanced-search media-type
# filter (PostGrid).  Kept distinct from the thumbnail/preview routing sets
# above so adding a type here never changes how thumbnails are picked.
ARCHIVE_SUFFIXES = frozenset({".zip", ".cbz", ".cbr", ".rar", ".7z"})
DOCUMENT_SUFFIXES = PDF_SUFFIXES | frozenset({".txt", ".md", ".epub"})

# ZIP-format archives the viewer can preview (central-directory listing) and
# drill into (extract + re-root).  ``.cbz`` is a comic archive that is a plain
# ZIP under the hood, so it shares the exact same routing as ``.zip`` (F09).
# ``.cbr`` / ``.rar`` / ``.7z`` are deliberately excluded — they need non-ZIP
# backends the viewer doesn't depend on.
ZIP_DRILL_SUFFIXES = frozenset({".zip", ".cbz"})

# Media-type keys exposed by the advanced-search "検索対象" dropdown, mapped to
# the suffix set used to filter results.  ``"all"`` (and any unknown key) maps
# to ``None`` meaning "no extension filter".
_MEDIA_SUFFIX_SETS = {
    "image": IMAGE_SUFFIXES,
    "video": VIDEO_SUFFIXES,
    "audio": AUDIO_SUFFIXES,
    "document": DOCUMENT_SUFFIXES,
    "archive": ARCHIVE_SUFFIXES,
}


def media_suffixes_for(kind: str) -> frozenset[str] | None:
    """Return the suffix set for a media-type key, or ``None`` for "all".

    Used by the advanced-search media-type filter to keep only files of the
    selected category.  ``None`` means no filtering.
    """
    return _MEDIA_SUFFIX_SETS.get(kind)

# Filename prefix the writing tool uses to mark "this image is the thumbnail".
# Compared case-insensitively against the lowercase filename.
THUMB_MARKER_PREFIX = "#thumb#"

# 投稿メタ文書のファイル名（小文字比較）。
POST_MD_NAME = "post.md"


def is_meta_or_marker_name(name: str) -> bool:
    """:func:`is_meta_or_marker_file` のファイル名だけ版（判定本体）.

    走査ループ内は ``os.DirEntry.name`` しか手元に無い（``Path`` を組むのは
    その 1 件を採用すると決めてから）ので、``Path`` を要求しない形を本体に
    置き、:func:`is_meta_or_marker_file` がこれへ委譲する — 判定の定義は
    あくまで 1 箇所。
    """
    lower = name.lower()
    return lower == POST_MD_NAME or lower.startswith(THUMB_MARKER_PREFIX)


def is_meta_or_marker_file(path: Path) -> bool:
    """*path* が「表示可能メディアではない」内部ファイルか (UIレビュー 07-25 #52).

    対象は ``post.md``（投稿メタ文書 — 中央は Markdown ビュー / 情報パネルの
    メタカードで表現される）と ``#thumb#…``（書き込み側ツールの代表画像マーカー
    — UI のどこにもタイルとして出さない内部マーカー）。

    **母集合統一の単一定義**: ステージの ``n/m``・画像トラック・‹ › / ←→ の
    画像送り（``ChildrenGrid.tile_paths``）と、閲覧モードのプレイリスト
    （``lightbox_parts/scan.py::list_playlist_sorted``）と、「最近追加された
    ファイル」一覧（:func:`walk_recent_files`）が同じ集合＝「表示可能メディア
    のみ」を歩く
    よう、いずれもここを唯一の判定にする。右の情報パネル（ファイル一覧）
    だけは ``post.md`` を淡色 + 末尾で温存する（メタへの導線）。
    """
    return is_meta_or_marker_name(path.name)

# Bounds for the BFS descent used by :func:`find_first_image` /
# :func:`read_folder_preview` when the top-level folder has no images.
# Descent lets a folder whose children/grandchildren hold the actual
# artwork (gallery-per-subfolder layouts) still get a thumbnail, while
# these caps stop the search from walking arbitrarily deep trees on
# NAS shares.  Values are intentionally modest:
#
# * ``MAX_DIRS``: hard cap on how many directories the BFS will scandir,
#   across all depths combined.  Budgets the worst-case NAS round-trips.
# * ``MAX_DEPTH``: how far below the starting folder to descend.  BFS
#   stops at the first depth where any image is found, so this is the
#   cap that applies only when nothing is found closer to the root.
_RECURSE_MAX_DIRS = 32
_RECURSE_MAX_DEPTH = 3


@dataclass
class FolderEntry:
    path: Path
    title: str
    has_post_md: bool
    thumbnail_path: Path | None = None
    # ``False`` means ``thumbnail_path`` hasn't been resolved yet — the
    # thumbnail loader worker should scandir() ``path`` lazily to find the
    # first image.  This keeps scan_children() O(N_children) stat-only on
    # network drives; sub-folder contents are only read on demand when the
    # entry is about to be displayed.
    thumbnail_resolved: bool = True
    # The two candidate images surfaced by ``read_folder_preview``.  Kept
    # so the UI can switch between "show #thumb# marker" and "exclude
    # #thumb# marker" without re-scanning the folder.
    #
    # ``thumb_marker_path``: alphabetically-first image whose filename
    # starts with ``#thumb#`` (the writing tool's marker for "this is the
    # thumbnail"); ``None`` if the folder has no marker file.
    #
    # ``non_thumb_marker_path``: alphabetically-first non-marker
    # thumbnailable file (images preferred over PDFs; a PDF only lands
    # here when the folder has no non-marker image).  ``None`` if every
    # image is a marker and no PDF exists either.
    thumb_marker_path: Path | None = None
    non_thumb_marker_path: Path | None = None
    # True when the currently-shown ``thumbnail_path`` is the ``#thumb#``
    # marker chosen as a *fallback* in exclude-marker mode (because the
    # folder contains no other image).  The grid draws a red border on
    # such items so the user can tell the displayed image is the
    # placeholder thumbnail, not real content.
    is_fallback_thumbnail: bool = False
    posted_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    locked_count: int = 0
    #: Post favorite / like count from ``post.md`` (``None`` when unknown).
    #: Drives the favorites sort modes in PostGrid.
    favorites: int | None = None
    #: Plan / tier name from ``post.md`` (``""`` when unknown).  Searchable via
    #: the ``plan:`` field prefix in the PostGrid filter.
    plan_name: str = ""
    #: Plan price from ``post.md`` kept verbatim with its currency symbol
    #: (e.g. ``¥500``).  Searchable via the ``plan_price:`` field prefix.
    plan_price: str = ""
    mtime: float = 0.0
    is_dir: bool = True
    size: int = 0
    # ``False`` for folders where ``post.md`` hasn't been attempted yet.
    # Files always set this to ``True`` (they have no metadata to load).
    metadata_loaded: bool = True
    # Top-level file names inside this folder (``post.md`` excluded), kept
    # so the PostGrid filter can match by attachment name / extension
    # without a recursive walk.  Populated by the metadata pass — empty on
    # entries that haven't been enriched yet.  Always lower-case for
    # cheap case-insensitive substring matching.
    file_names: list[str] = field(default_factory=list)
    #: ``post.md`` の ``service`` / ``post_id``（不明なら空文字）。表示には
    #: 使わないが、印（★ / あとで見る / ユーザータグ）の書き込みが rename
    #: 追従用の postref 列を**同期 I/O 抜きで**決められるようにするための
    #: 持ち回り。メタデータパス（:func:`apply_metadata` /
    #: :func:`apply_preview`）が埋め、キャッシュヒットも
    #: :func:`_preview_to_read_tuple` 経由で同じ値を再現する。
    service: str = ""
    post_id: str = ""
    #: Normalised relevance score (0..1) for semantic / similar-image search
    #: results, or ``None`` for entries that carry no ranking (direct
    #: children, tag/media search hits, recursive walk).  Set by the AI
    #: plugin's ``VectorSearchScanner`` (``plugins/snappix_ai/engine/
    #: scanners.py``) from the raw query score.  Surfaced in the PostGrid
    #: caption as ``◆NN%``.
    relevance: float | None = None


def select_preview_candidates(
    names: "Iterable[str] | list[str]",
) -> tuple[str | None, str | None]:
    """ファイル名リストから ``(thumb_marker, non_thumb_marker)`` 名を選ぶ純関数.

    フォルダ代表画像の選定規則の**単一定義** (#40): live 読み
    （:func:`_scan_dir_images`）と ``cache_builder._scan_one`` のフォルダ行
    温め（親 scandir で得た名前リストからの選定）が共有する。名前だけで
    完結するので追加の filesystem アクセスは無い。

    規則（:func:`_scan_dir_images` の docstring と同一契約）:

    * 画像（:data:`IMAGE_SUFFIXES`）は ``#thumb#`` 接頭辞で marker /
      non-marker に分かれ、各スロットは小文字名のアルファベット先頭を採る。
    * PDF（:data:`PDF_SUFFIXES`）は non-marker 画像が無いときだけ
      non-marker スロットへ。``#thumb#`` 付き PDF はどちらの候補も無い
      ときだけ marker スロットへ（実画像を絶対に押しのけない）。
    * ``post.md`` は候補にしない。認識できない拡張子は無視。

    Returns the chosen names verbatim (original case); ``None`` when the
    slot has no candidate.
    """
    marker_key: str | None = None
    marker_name: str | None = None
    other_key: str | None = None
    other_name: str | None = None
    pdf_key: str | None = None
    pdf_name: str | None = None
    marker_pdf_key: str | None = None
    marker_pdf_name: str | None = None
    for name in names:
        lower = name.lower()
        if lower == POST_MD_NAME:
            continue
        ext = os.path.splitext(lower)[1]
        if ext in IMAGE_SUFFIXES:
            if lower.startswith(THUMB_MARKER_PREFIX):
                if marker_key is None or lower < marker_key:
                    marker_key = lower
                    marker_name = name
            else:
                if other_key is None or lower < other_key:
                    other_key = lower
                    other_name = name
        elif ext in PDF_SUFFIXES:
            if lower.startswith(THUMB_MARKER_PREFIX):
                # A ``#thumb#`` PDF must never land in the non-marker slot:
                # the exclude toggle would then fail to hide it and the grid
                # would draw it as a real content image (no 「仮画像」
                # border).  Kept in its own slot, used only as a last resort.
                if marker_pdf_key is None or lower < marker_pdf_key:
                    marker_pdf_key = lower
                    marker_pdf_name = name
            elif pdf_key is None or lower < pdf_key:
                pdf_key = lower
                pdf_name = name
    # Fall back to a PDF only when no non-marker image was seen — images
    # are visually more useful thumbnails, so we avoid letting a PDF
    # that sorts earlier alphabetically preempt a perfectly good image.
    if other_name is None and pdf_name is not None:
        other_name = pdf_name
    # Same fallback rule on the marker side, applied last: a ``#thumb#`` PDF
    # only represents the folder when nothing else can (no marker image and
    # no non-marker candidate at all), so it never preempts a real image.
    if marker_name is None and other_name is None and marker_pdf_name is not None:
        marker_name = marker_pdf_name
    return marker_name, other_name


def _scan_dir_images(
    folder: Path,
    *,
    skip: AbstractSet[Path] = frozenset(),
) -> tuple[Path | None, Path | None, list[Path], Path | None, list[str], bool]:
    """Single ``os.scandir`` pass over *folder*.

    Returns ``(thumb_marker, non_thumb_marker, subdirs, post_md_path,
    file_names, ok)``: the alphabetically-first ``#thumb#``-prefixed image,
    the alphabetically-first non-marker thumbnailable file, the list of
    immediate sub-directories (unsorted), the ``post.md`` path if
    present, the lower-cased names of every other file in *folder*, and
    an ``ok`` flag.  On I/O error the three thumbnail/md slots are
    ``None``, *subdirs* is empty, *file_names* is empty and ``ok`` is
    ``False`` — the caller must NOT treat that result as "the folder is
    truly empty" (a transient NAS disconnect looks identical to an empty
    folder otherwise, and persisting it would poison the preview cache).
    An individual entry that could not be classified (``is_dir`` /
    ``is_file`` raising) is skipped but also clears ``ok``: the result is
    then merely *incomplete* rather than empty, which poisons the cache
    just as effectively.

    *file_names* is captured cheaply during the same scandir loop and
    consumed by the PostGrid filter so attachment names / extensions
    are matchable without a recursive walk.  ``post.md`` itself is
    omitted (the filter already considers parsed title / tags).

    Thumbnailable files are images (:data:`IMAGE_SUFFIXES`) plus PDFs
    (:data:`PDF_SUFFIXES`).  Within the non-marker group images are
    preferred over PDFs — a PDF only appears in *non_thumb_marker* when
    no non-marker image exists in this folder.  A ``#thumb#``-prefixed PDF
    is **not** a non-marker candidate either (it is a marker file — the
    exclude toggle and the 「仮画像」 border must both see it as one); it
    only reaches *thumb_marker* when the folder has neither a marker image
    nor any non-marker candidate, so it never preempts a real image.

    *skip* excludes the given paths from the thumbnail/PDF candidate slots
    (they still count towards *file_names*).  Used by the centre-pane
    representative-image fallback (#84) to walk past a candidate that the
    decoder could not read.
    """
    post_md_path: Path | None = None
    subdirs: list[Path] = []
    file_names: list[str] = []
    # Files eligible for the candidate slots (non-``post.md``, not *skip*ped);
    # the actual pick is delegated to :func:`select_preview_candidates` so the
    # rule has one home shared with the cache-build walk (#40).
    candidate_names: list[str] = []
    # Per-entry classification failures degrade the result exactly like a
    # failed scandir: the entry is dropped from *file_names* and from the
    # candidate slots, so persisting the result would bake a folder that
    # looks smaller than it is.  A reparse point whose target lives on a
    # dropped NAS share raises here (``is_dir`` follows the link), which is
    # precisely the transient case ``ok`` exists to keep out of the cache.
    ok = True
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    ok = False
                    continue
                if is_dir:
                    subdirs.append(Path(entry.path))
                    continue
                try:
                    is_file = entry.is_file()
                except OSError:
                    ok = False
                    continue
                if not is_file:
                    continue
                lower = entry.name.lower()
                if lower == POST_MD_NAME:
                    post_md_path = Path(entry.path)
                    continue
                file_names.append(lower)
                if skip and Path(entry.path) in skip:
                    continue
                candidate_names.append(entry.name)
    except OSError:
        return None, None, [], None, [], False
    marker_name, other_name = select_preview_candidates(candidate_names)
    marker_path = folder / marker_name if marker_name is not None else None
    other_path = folder / other_name if other_name is not None else None
    return marker_path, other_path, subdirs, post_md_path, file_names, ok


def _find_images_bfs(
    folder: Path,
    *,
    max_dirs: int = _RECURSE_MAX_DIRS,
    max_depth: int = _RECURSE_MAX_DEPTH,
    skip: AbstractSet[Path] = frozenset(),
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[Path | None, Path | None, list[Path], Path | None, list[str], bool]:
    """BFS descent from *folder* looking for the first depth level that
    contains images.

    Returns ``(marker, other, subdirs, post_md_path, file_names, ok)`` —
    the *subdirs*, *post_md_path* and *file_names* slots always come
    from the initial scan of *folder* itself (callers use them for
    driving the scan of children, reading metadata at the top level,
    and matching against the PostGrid filter haystack respectively).

    The two image paths come from whichever depth first surfaces any
    image.  Ties within a depth are broken alphabetically (case-insensitive)
    across every folder visited at that depth, so the result is stable.
    Descent stops early as soon as a depth yields any candidate.

    If no image is found within *max_dirs* directories or *max_depth*
    levels, both image slots are ``None``.

    Descent goes through :func:`should_descend` — the single definition of
    walk coverage shared with :func:`_iter_tree` and
    ``cache_builder._walk_tree`` — so a junction / symlink pointing back
    at an ancestor (or sideways at a sibling) is visited once instead of
    eating the *max_dirs* budget again under a second name (レビュー
    2026-09-03 項目 #222).  The predicate is applied **immediately before
    each ``os.scandir``**, not when candidates are queued, so its identity
    ``os.stat`` stays inside the *max_dirs* budget.

    ``ok`` is ``False`` when any of the scandir passes hit an I/O error
    (top-level or a descended sub-folder): the result may then be missing
    content that actually exists (e.g. a NAS share that dropped mid-scan),
    so it must not be persisted as "this folder is empty".

    *skip* is forwarded to :func:`_scan_dir_images` at every depth (#84).

    ``should_cancel`` is polled **before the top-level scan too**, not only
    inside the descent loop: a probe that was superseded while it sat in its
    pool's queue would otherwise still pay one full ``os.scandir`` of its own
    folder, and on a half-dead share that single round-trip is the 15〜195 秒
    block ``_runnable`` measures.  With a one-thread pool behind it, every
    such queued-and-cancelled walk delays the folder the user is actually
    looking at by that much.  It is then polled before every descent
    candidate (ahead of the identity ``stat`` as well as the ``os.scandir``
    itself).  A cancelled descent returns what it has with ``ok=False``: the
    answer is
    unproven (an image may well live in a level we never reached), so it must
    never be persisted — the same contract an I/O error gets.  Without this
    the caller's cancellation could not reach a worker that was already
    inside the descent, and up to ``max_dirs`` (32) further round-trips per
    folder kept hitting the NAS after the user had navigated away.
    """
    if should_cancel is not None and should_cancel():
        # Nothing has been looked at yet — the whole answer is unproven.
        return None, None, [], None, [], False
    (
        initial_marker,
        initial_other,
        root_subdirs,
        post_md_path,
        file_names,
        ok,
    ) = _scan_dir_images(folder, skip=skip)
    if initial_marker is not None or initial_other is not None:
        return (
            initial_marker, initial_other, root_subdirs, post_md_path,
            file_names, ok,
        )

    # 訪問可否は走査カバレッジの単一定義 :func:`should_descend` に通す (#44)。
    # ここは 3 本目のツリーウォーカーで、以前は ``visited`` が識別子の集合では
    # なく単なる件数カウンタだったため、祖先や兄弟を指すジャンクション /
    # symlink があると同じ物理ディレクトリを別名で何度も scandir し、
    # ``max_dirs`` の予算をループ側が食い潰して本来届くはずの兄弟サブツリーの
    # 代表画像に到達できなかった（レビュー 2026-09-03 項目 #222）。
    # 2 つの状態は役割が別: *visited* は identity の循環ガード、*scanned* は
    # NAS へのラウンドトリップ予算（実際に scandir した回数）。
    # 判定は「降下候補に積む時点」ではなく **scandir の直前** に置く:
    # ``should_descend`` は ``dir_identity`` 経由で 1 件につき ``os.stat`` を
    # 1 回撃つ（Windows の ``DirEntry.stat()`` は file id を持たないので
    # ``Path`` 渡しと同様に必ず追加の stat が走る）ため、積む時点で判定すると
    # ``max_dirs`` の外側でサブディレクトリ数ぶんの stat が無制限に走り、
    # しかも ``sorted()`` がジェネレータを完全消費するまで ``should_cancel``
    # が 1 度も見られない。降下の直前へ寄せれば identity stat は予算の内側に
    # 収まり、キャンセルも 1 ディレクトリ単位で届く。
    visited: set[object] = set()
    admit_dir_identity(dir_identity(folder), visited)
    scanned = 1  # already scanned *folder* itself
    current_level = sorted(root_subdirs, key=lambda p: p.name.lower())
    depth = 1
    while current_level and depth <= max_depth and scanned < max_dirs:
        marker_key: str | None = None
        marker_path: Path | None = None
        other_key: str | None = None
        other_path: Path | None = None
        next_level: list[Path] = []
        for current in current_level:
            if scanned >= max_dirs:
                break
            if should_cancel is not None and should_cancel():
                # Unproven "no image here" — must not be cached (ok=False).
                return None, None, root_subdirs, post_md_path, file_names, False
            if not should_descend(current, visited):
                continue
            scanned += 1
            m, o, subs, _, _, sub_ok = _scan_dir_images(current, skip=skip)
            if not sub_ok:
                ok = False
            if m is not None:
                key = m.name.lower()
                if marker_key is None or key < marker_key:
                    marker_key = key
                    marker_path = m
            if o is not None:
                key = o.name.lower()
                if other_key is None or key < other_key:
                    other_key = key
                    other_path = o
            next_level.extend(subs)
        if marker_path is not None or other_path is not None:
            return (
                marker_path, other_path, root_subdirs, post_md_path,
                file_names, ok,
            )
        current_level = sorted(next_level, key=lambda p: p.name.lower())
        depth += 1
    return None, None, root_subdirs, post_md_path, file_names, ok


def find_first_image(
    folder: Path,
    *,
    exclude_thumb_marker: bool = False,
    skip: AbstractSet[Path] = frozenset(),
    should_cancel: Callable[[], bool] | None = None,
) -> Path | None:
    """Return the alphabetically-first thumbnailable file in *folder*,
    descending into sub-folders if the folder itself has no candidates.

    Thumbnailable = image files (:data:`IMAGE_SUFFIXES`) plus PDFs
    (:data:`PDF_SUFFIXES`).  Within a single folder, non-marker images
    take priority over PDFs — a PDF is only returned when no image
    exists at that depth.

    Used by the thumbnail loader for lazy resolution when ``scan_children``
    didn't determine a thumbnail up-front (no ``post.md`` thumbnail hint).

    Selection order at the first depth that yields an image:
    * ``exclude_thumb_marker=False`` (default): a ``#thumb#``-prefixed file
      is preferred over any non-marker image; ties within the marker group
      and within the non-marker group are broken alphabetically.  This
      matches the writing tool's "marker = canonical thumbnail" convention.
    * ``exclude_thumb_marker=True``: marker files are skipped entirely.
      Returns the alphabetically-first non-marker image, or ``None`` if
      no non-marker image exists within the search budget.  The caller
      is responsible for deciding what to show as a placeholder when the
      result is ``None`` despite the tree having marker files.

    *skip* excludes the given paths from candidacy at every depth, so a
    caller that found the returned file undecodable can ask for the next
    candidate instead (centre-pane representative-image fallback, #84).

    Descent is bounded by :data:`_RECURSE_MAX_DIRS` and
    :data:`_RECURSE_MAX_DEPTH` to keep NAS load predictable — see the
    constants' comments for the rationale.

    ``should_cancel`` is forwarded to the descent exactly as
    :func:`read_folder_preview_checked` forwards it: a caller that runs this
    on a worker can stop the remaining round-trips once its result is no
    longer wanted.  A cancelled descent returns ``None`` — the answer is
    unproven, so callers must not persist it as "this folder has no image".
    """
    marker_path, other_path, _, _, _, _ = _find_images_bfs(
        folder, skip=skip, should_cancel=should_cancel,
    )
    if exclude_thumb_marker:
        return other_path
    return marker_path if marker_path is not None else other_path


def _read_post_md_checked(md_path: Path) -> tuple[ParsedPost | None, bool]:
    """Read + parse ``post.md``, distinguishing "absent" from "unreadable".

    Returns ``(parsed, ok)``.  ``(None, True)`` means the file genuinely
    does not exist (a folder without post.md — a perfectly cacheable
    fact); ``(None, False)`` means an I/O error prevented reading it
    (NAS disconnect, permission hiccup) — the caller must not persist
    ``has_post_md=False`` for such a folder.

    Only a bounded **head** (:data:`~snappix.common.post_meta.HEAD_READ_LIMIT`)
    is read: this
    path (``read_folder_preview`` → ``apply_preview`` / ``FolderPreviewCache``)
    consumes nothing but the leading title + meta block — ``ParsedPost.body``
    is unused here and the ``thumbnail:`` hint is deliberately ignored (see
    :func:`apply_preview`).  Reading whole multi-MB bodies across six parallel
    metadata workers would burn NAS bandwidth (and memory) for a few hundred
    bytes of meta (#88).  The ``body:`` filter has its own full read
    (``filter_query.parsed_post_cached``).
    """
    with measure("post_md_read", str(md_path)):
        try:
            text = read_head(md_path, HEAD_READ_LIMIT)
        except FileNotFoundError:
            return None, True
        except OSError as exc:
            logger.warning("Failed to read {}: {}", md_path, exc)
            return None, False
    with measure("post_md_parse", str(md_path)):
        return parse_post_md(text), True


def _read_post_md(md_path: Path) -> ParsedPost | None:
    return _read_post_md_checked(md_path)[0]


def read_folder_metadata(folder: Path) -> ParsedPost | None:
    """Read ``post.md`` from *folder* (if present) and return the parsed
    metadata.  Intended for parallel worker-pool execution — NAS round-trips
    dominate the cost, so dozens of concurrent calls are fine.
    """
    return _read_post_md(folder / POST_MD_NAME)


def read_folder_preview(
    folder: Path,
) -> tuple[ParsedPost | None, Path | None, Path | None, list[str]]:
    """Scan *folder* once for ``post.md`` + thumbnail candidates, then
    descend into sub-folders when the top level has no images.

    Returns ``(parsed, thumb_marker_path, non_thumb_marker_path, file_names)``:

    * ``thumb_marker_path``: alphabetically-first ``#thumb#``-prefixed
      image (the writing tool's marker), or ``None`` if absent.
    * ``non_thumb_marker_path``: alphabetically-first non-marker image,
      or ``None`` if every image is a marker / no images at all.
    * ``file_names``: lower-cased names of every non-``post.md`` file
      directly in *folder*, captured during the same scandir pass and
      consumed by the PostGrid filter so attachment names / extensions
      are matchable without a recursive walk.  Files from descended
      sub-folders are deliberately **not** included — the filter would
      otherwise drift into "recursive" behaviour without the explicit
      checkbox.

    ``post.md`` is read only at the top level — metadata never comes
    from a descendant.  The BFS descent is bounded by
    :data:`_RECURSE_MAX_DIRS` and :data:`_RECURSE_MAX_DEPTH`; it stops
    at the first depth that yields any image so a gallery-per-subfolder
    layout still gets a thumbnail without walking the entire tree.
    """
    parsed, marker_path, other_path, file_names, _ok = (
        read_folder_preview_checked(folder)
    )
    return parsed, marker_path, other_path, file_names


def read_folder_preview_checked(
    folder: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[ParsedPost | None, Path | None, Path | None, list[str], bool]:
    """:func:`read_folder_preview` plus an I/O-reliability flag.

    Returns ``(parsed, thumb_marker_path, non_thumb_marker_path,
    file_names, ok)``.  ``ok`` is ``False`` when the scandir pass (top
    level or a descended sub-folder) or the ``post.md`` read failed with
    an I/O error — the empty-looking result is then indistinguishable
    from a transiently-unreadable folder (NAS disconnect), so callers
    that persist previews (:func:`read_folder_preview_cached`) must skip
    caching it.  A genuinely empty folder / genuinely absent ``post.md``
    still reports ``ok=True`` and stays cacheable.

    ``should_cancel`` (polled inside the BFS descent and before the
    ``post.md`` read) lets a caller abandon a folder mid-resolve — a
    cancelled read also reports ``ok=False``, so the partial answer is
    never persisted.
    """
    with measure("folder_preview_scandir", str(folder)):
        (
            marker_path,
            other_path,
            _,
            post_md_path,
            file_names,
            ok,
        ) = _find_images_bfs(folder, should_cancel=should_cancel)

    parsed: ParsedPost | None = None
    if post_md_path is not None:
        if should_cancel is not None and should_cancel():
            return None, marker_path, other_path, file_names, False
        parsed, md_ok = _read_post_md_checked(post_md_path)
        if not md_ok:
            ok = False
    return parsed, marker_path, other_path, file_names, ok


def select_thumbnail(
    thumb_marker: Path | None,
    non_thumb_marker: Path | None,
    *,
    exclude_thumb_marker: bool,
) -> tuple[Path | None, bool]:
    """Pick which of the two preview candidates to actually display.

    Returns ``(path, is_fallback)``:
    * ``path``: the chosen thumbnail path, or ``None`` if no image exists.
    * ``is_fallback``: ``True`` only when *exclude_thumb_marker* was
      requested but the folder contained nothing else, so we had to fall
      back to the marker file.  The caller (grid) uses this flag to draw
      a red border indicating "this is the placeholder thumbnail, not a
      real content image".
    """
    if not exclude_thumb_marker:
        chosen = thumb_marker if thumb_marker is not None else non_thumb_marker
        return chosen, False
    if non_thumb_marker is not None:
        return non_thumb_marker, False
    if thumb_marker is not None:
        return thumb_marker, True
    return None, False


def scan_children(
    root: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> list[FolderEntry]:
    """Return entries for all immediate children of *root* (lossy flavour).

    Thin wrapper over :func:`scan_children_checked` that keeps only the
    entries.  Use it where "unreadable" and "empty" may be treated alike (the
    folder-preview pane already degrades to its own empty state); a consumer
    that must tell the two apart — anything that would otherwise say
    「このフォルダは空です」 — takes the checked flavour instead.
    """
    return scan_children_checked(root, should_cancel=should_cancel).entries


def _entry_is_dir(child_entry: "os.DirEntry[str]") -> bool:
    """``DirEntry.is_dir`` that falls back to the scandir buffer.

    The following form resolves the link target, so a junction / symlink
    pointing at a share that has dropped raises instead of answering.  The
    non-following form answers from the ``WIN32_FIND_DATA`` / ``d_type`` the
    parent's ``os.scandir`` already returned — no round-trip, and an
    unreachable link is still classified as the directory entry it is.
    """
    try:
        return child_entry.is_dir()
    except OSError:
        pass
    try:
        return child_entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _entry_is_file(child_entry: "os.DirEntry[str]") -> bool:
    """``DirEntry.is_file`` with the same buffer fallback as
    :func:`_entry_is_dir`.
    """
    try:
        return child_entry.is_file()
    except OSError:
        pass
    try:
        return child_entry.is_file(follow_symlinks=False)
    except OSError:
        return False


class ChildrenScan(NamedTuple):
    """:func:`scan_children_checked` の答え — 結果と、その穴。

    ``failure`` は **列挙そのものが成立しなかった**ときだけ立つ（``entries``
    は無効）。``unclassified`` は **一覧は立つが一部の子を落とした**件数で、
    ``entries`` は有効なまま（:attr:`incomplete`）。

    2 つを 1 本の ``reason`` にまとめると、消費者は「1 件読めなかった」を
    「フォルダ全体が読めなかった」として扱うしかない — 壊れたリンクが 1 つ
    あるだけで正常な子ごと走査失敗カードへ落ちる。:class:`RecentFilesScan`
    が ``failed`` と ``unreadable_dirs`` を分けているのと同じ理由。

    タプルとしても展開できる（``entries, failure, unclassified = ...``）。
    """

    entries: list[FolderEntry]
    failure: str | None
    unclassified: int = 0

    @property
    def incomplete(self) -> bool:
        """一覧は立つが穴がある（分類できなかった子が居た）。"""
        return self.unclassified > 0


def scan_children_checked(
    root: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> ChildrenScan:
    """Return a :class:`ChildrenScan` for the children of *root*.

    Same ``ok`` convention the rest of this module already uses
    (:func:`_scan_dir_images` / :func:`read_folder_preview_checked` /
    :func:`walk_recent_files`): a caller must be able to tell 「読めなかった」
    from 「空だった」 **from the return value**, because the two look
    identical once the failure has been flattened into an empty list.
    ``failure`` is ``None`` for a complete result (including a genuinely empty
    folder) and a human-readable string when the root could not be listed —
    an unreachable / non-directory root, or an ``os.scandir`` that raised.

    "Complete" is per **entry**, not just per root: a child whose type could
    not be determined at all is left out of *entries*, so the count of such
    children comes back as ``unclassified``.  Dropping them silently would be
    the same 「読めなかった」/「空だった」 confusion one level down — but it is a
    **hole in a valid listing**, not a failed listing, so it travels in its own
    field: a caller that folded it into ``failure`` would throw away the
    children it did read (the whole folder becomes an error card because one
    symlink dangles).  Classification itself falls back to the parent's
    scandir buffer (:func:`_entry_is_dir`), so the common case — a junction
    whose target share has dropped — stays in the listing with no extra I/O.

    Before this existed the consumer re-probed the same root with a second
    ``is_dir()`` + ``scandir`` to *reconstruct* the failure it had just been
    denied; a transient failure (an SMB share that dropped mid-enumeration
    and recovered a moment later) does not reproduce, so a folder with
    contents was announced as 「このフォルダは空です」 — レビュー 2026-09-03
    項目 #216.

    A *cancelled* scan is not a failure: it returns the partial list with
    ``failure=None``, exactly as before, and the caller drops it by
    cancellation.

    This call is deliberately **shallow**: it only issues ``os.scandir`` +
    ``DirEntry.stat`` (cheap on SMB, same ops Explorer uses).  It does NOT
    open ``post.md`` or walk sub-folder contents — those are deferred to
    worker pools so folder navigation stays snappy on network drives with
    hundreds of sub-folders.

    As a consequence, folder entries returned here have ``metadata_loaded
    = False``, ``has_post_md = False``, ``thumbnail_resolved = False`` and
    placeholder title/tags.  Consumers are expected to enrich them by
    calling :func:`read_folder_metadata` in parallel (e.g. via
    :class:`scan_worker.ChildrenScanner`).  File entries are complete as-is.

    When *should_cancel* is supplied it is polled every few hundred entries
    during the ``stat`` loop; if it returns ``True`` the scan aborts early
    and returns whatever has been collected so far.  The caller is expected
    to discard the partial result.  This matters on folders with tens of
    thousands of children where the stat loop itself dominates runtime and
    the user may have already navigated elsewhere.
    """
    entries: list[FolderEntry] = []
    unclassified = 0
    if not root.is_dir():
        # ``Path.is_dir`` swallows the OSError, so an offline share and a
        # deleted folder both land here; the caller only needs "not listable".
        return ChildrenScan(entries, f"フォルダにアクセスできません: {root}")
    with measure("scan_children_scandir", str(root)):
        try:
            with os.scandir(root) as it:
                children = list(it)
        except OSError as exc:
            logger.warning("Failed to enumerate {}: {}", root, exc)
            return ChildrenScan(entries, str(exc))

    for i, child_entry in enumerate(children):
        if should_cancel is not None and (i & 0xFF) == 0 and should_cancel():
            return ChildrenScan(entries, None, unclassified)
        try:
            st = child_entry.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            mtime = 0.0
            size = 0
        is_dir = _entry_is_dir(child_entry)
        is_file = False if is_dir else _entry_is_file(child_entry)
        child = Path(child_entry.path)
        if not is_dir and not is_file:
            # Neither probe could classify the child, not even from the
            # parent's scandir buffer.  Dropping it silently would hand the
            # caller a shorter list that promises a complete one — the very
            # confusion this function's return convention exists to prevent —
            # so it is counted into ``unclassified`` (a hole in a valid
            # listing, NOT a failed listing: the children that *did* classify
            # are still the answer).
            unclassified += 1
            logger.warning("Failed to classify child entry {}", child)
            continue
        if is_dir:
            entries.append(
                FolderEntry(
                    path=child,
                    title=child.name,
                    has_post_md=False,
                    thumbnail_path=None,
                    thumbnail_resolved=False,
                    posted_at=None,
                    tags=[],
                    locked_count=0,
                    mtime=mtime,
                    is_dir=True,
                    size=0,
                    metadata_loaded=False,
                )
            )
        elif is_file:
            thumbable = child.suffix.lower() in THUMBNAILABLE_SUFFIXES
            entries.append(
                FolderEntry(
                    path=child,
                    title=child.name,
                    has_post_md=False,
                    thumbnail_path=child if thumbable else None,
                    thumbnail_resolved=True,
                    posted_at=None,
                    tags=[],
                    locked_count=0,
                    mtime=mtime,
                    is_dir=False,
                    size=size,
                    metadata_loaded=True,
                )
            )
    return ChildrenScan(entries, None, unclassified)


def apply_metadata(entry: FolderEntry, parsed: ParsedPost | None) -> FolderEntry:
    """Return a new :class:`FolderEntry` with *parsed* post.md merged in.

    Used by the metadata worker to produce a replacement entry once
    ``post.md`` is read.  Thumbnail resolution is **always** deferred to
    :func:`find_first_image` — the ``parsed.thumbnail`` hint from
    ``post.md`` is intentionally ignored because the format has varied
    across writer versions and the hint is sometimes stale (file
    renamed / removed after the fact), causing missing thumbnails when
    the loader tries to open a path that no longer exists.
    """
    return FolderEntry(
        path=entry.path,
        title=(parsed.title if parsed and parsed.title else entry.path.name),
        has_post_md=parsed is not None,
        thumbnail_path=None,
        thumbnail_resolved=False,
        thumb_marker_path=None,
        non_thumb_marker_path=None,
        is_fallback_thumbnail=False,
        posted_at=parsed.posted_at if parsed else None,
        tags=list(parsed.tags) if parsed else [],
        locked_count=parsed.locked_count if parsed else 0,
        favorites=parsed.favorites if parsed else None,
        plan_name=parsed.plan_name if parsed else "",
        plan_price=parsed.plan_price if parsed else "",
        service=(parsed.meta.get(KEY_SERVICE, "") if parsed else ""),
        post_id=(parsed.meta.get(KEY_POST_ID, "") if parsed else ""),
        mtime=entry.mtime,
        is_dir=True,
        size=0,
        metadata_loaded=True,
    )


def apply_preview(
    entry: FolderEntry,
    parsed: ParsedPost | None,
    thumb_marker: Path | None,
    non_thumb_marker: Path | None,
    file_names: list[str] | None = None,
) -> FolderEntry:
    """Like :func:`apply_metadata` but also consumes both candidate image
    paths from :func:`read_folder_preview`.

    Both candidates are stored on the entry so the UI can switch between
    "use marker" and "exclude marker" modes without re-scanning.  The
    initial ``thumbnail_path`` / ``is_fallback_thumbnail`` are populated
    via :func:`select_thumbnail` for the default (non-exclude) mode; the
    grid recomputes them on toggle.

    *file_names* (lower-cased) carries the folder's direct file names —
    captured during the same scandir pass as the thumbnail candidates —
    so the PostGrid filter can match by attachment name / extension.

    The ``parsed.thumbnail`` hint from ``post.md`` is **never** used.
    See :func:`apply_metadata` for the long history.
    """
    chosen, is_fallback = select_thumbnail(
        thumb_marker, non_thumb_marker, exclude_thumb_marker=False
    )
    return FolderEntry(
        path=entry.path,
        title=(parsed.title if parsed and parsed.title else entry.path.name),
        has_post_md=parsed is not None,
        thumbnail_path=chosen,
        thumbnail_resolved=True,
        thumb_marker_path=thumb_marker,
        non_thumb_marker_path=non_thumb_marker,
        is_fallback_thumbnail=is_fallback,
        posted_at=parsed.posted_at if parsed else None,
        tags=list(parsed.tags) if parsed else [],
        locked_count=parsed.locked_count if parsed else 0,
        favorites=parsed.favorites if parsed else None,
        plan_name=parsed.plan_name if parsed else "",
        plan_price=parsed.plan_price if parsed else "",
        service=(parsed.meta.get(KEY_SERVICE, "") if parsed else ""),
        post_id=(parsed.meta.get(KEY_POST_ID, "") if parsed else ""),
        mtime=entry.mtime,
        is_dir=True,
        size=0,
        metadata_loaded=True,
        file_names=list(file_names) if file_names else [],
    )


def preview_from_parsed(
    parsed: ParsedPost | None,
    thumb_marker: Path | None,
    non_thumb_marker: Path | None,
    file_names: list[str] | None,
) -> FolderPreview:
    """Build a cacheable :class:`FolderPreview` from a live preview read.

    ``service`` / ``post_id`` are carried across even though nothing
    *displays* them: they are the only part of ``ParsedPost.meta`` a cache
    hit has to reproduce (see :func:`_preview_to_read_tuple`).
    """
    meta = parsed.meta if parsed else {}
    return FolderPreview(
        has_post_md=parsed is not None,
        title=(parsed.title if parsed and parsed.title else ""),
        posted_at=parsed.posted_at if parsed else None,
        tags=list(parsed.tags) if parsed else [],
        locked_count=parsed.locked_count if parsed else 0,
        favorites=parsed.favorites if parsed else None,
        plan_name=parsed.plan_name if parsed else "",
        plan_price=parsed.plan_price if parsed else "",
        thumb_marker_path=thumb_marker,
        non_thumb_marker_path=non_thumb_marker,
        file_names=list(file_names) if file_names else [],
        service=meta.get(KEY_SERVICE, ""),
        post_id=meta.get(KEY_POST_ID, ""),
    )


def _preview_to_read_tuple(
    preview: FolderPreview,
) -> tuple[ParsedPost | None, Path | None, Path | None, list[str]]:
    """Inverse of :func:`preview_from_parsed` for a cached row.

    Reconstructs the ``(parsed, thumb_marker, non_thumb_marker, file_names)``
    shape :func:`read_folder_preview` returns, so cached and live results
    flow through the same :func:`apply_preview` path.  A folder cached
    without a ``post.md`` reconstructs ``parsed=None`` so ``apply_preview``
    falls back to the folder-name title.

    ``meta`` is restored with just the ``service`` / ``post_id`` pair — the
    only keys any consumer of this reconstruction reads (``scan_children``
    feeds it to :func:`~snappix.viewer.search_index.postref_row`).  It used
    to come back empty, which meant a warm folder-preview cache silently
    stopped the ``postref`` index from ever being written for that folder
    (and nothing re-wrote it until the folder's own mtime changed).  The
    ``body`` and the remaining meta keys are deliberately NOT cached: their
    consumers parse the folder's ``post.md`` directly.
    """
    parsed: ParsedPost | None = None
    if preview.has_post_md:
        meta: dict[str, str] = {}
        if preview.service:
            meta[KEY_SERVICE] = preview.service
        if preview.post_id:
            meta[KEY_POST_ID] = preview.post_id
        parsed = ParsedPost(
            title=preview.title,
            posted_at=preview.posted_at,
            tags=list(preview.tags),
            locked_count=preview.locked_count,
            favorites=preview.favorites,
            plan_name=preview.plan_name,
            plan_price=preview.plan_price,
            meta=meta,
        )
    return (
        parsed,
        preview.thumb_marker_path,
        preview.non_thumb_marker_path,
        list(preview.file_names),
    )


def read_folder_preview_cached(
    folder: Path,
    mtime: float,
    cache,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[ParsedPost | None, Path | None, Path | None, list[str]]:
    """:func:`read_folder_preview` with a folder-preview cache front.

    On a cache hit (row present and folder ``mtime`` unchanged) the result
    is reconstructed WITHOUT any filesystem access — the per-folder scandir
    (+ BFS descent + ``post.md`` read) is skipped entirely, which is the NAS
    round-trip that otherwise re-runs on every folder revisit.  On a miss the
    folder is read live and the result stored for next time.

    ``cache`` may be ``None`` (feature disabled) and ``mtime`` may be ``0``
    (unknown / un-stat-able) — both fall back to a plain live read with no
    caching, so a bogus ``mtime`` is never persisted as a key.

    A live read that hit an I/O error (``ok=False`` from
    :func:`read_folder_preview_checked` — e.g. a NAS share that dropped
    mid-scan) is returned as-is for display (empty-looking, same as
    before) but is **never** written to the cache: persisting it would
    bake "this folder is empty / has no post.md" under the folder's
    real mtime, so the wrong answer would keep hitting even after the
    share reconnects.

    ``should_cancel`` is forwarded to the live read (a cache hit needs no
    cancellation — it touches no filesystem).  A cancelled read comes back
    ``ok=False`` and therefore takes the same "return for display, do not
    persist" path as an I/O error.
    """
    use_cache = cache is not None and bool(mtime)
    if use_cache:
        try:
            hit = cache.get(folder, mtime)
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview cache get failed for {}: {}", folder, exc)
            hit = None
        if hit is not None:
            return _preview_to_read_tuple(hit)
    parsed, thumb_marker, non_thumb_marker, file_names, ok = (
        read_folder_preview_checked(folder, should_cancel=should_cancel)
    )
    if use_cache and not ok:
        logger.debug(
            "folder preview read unreliable (I/O error) for {} — not cached",
            folder,
        )
    if use_cache and ok:
        try:
            cache.put(
                folder,
                mtime,
                preview_from_parsed(
                    parsed, thumb_marker, non_thumb_marker, file_names
                ),
            )
        except Exception as exc:  # pragma: no cover (cache best-effort)
            logger.debug("folder preview cache put failed for {}: {}", folder, exc)
    return parsed, thumb_marker, non_thumb_marker, file_names


def apply_cached_preview(entry: FolderEntry, preview: FolderPreview) -> FolderEntry:
    """Merge a cached :class:`FolderPreview` into *entry* (main-thread seed).

    Equivalent to running :func:`apply_preview` with the cached resolution,
    so a warm folder is fully resolved (``thumbnail_resolved=True`` + a
    concrete ``thumbnail_path``) without a scandir — making it lay out by its
    representative image's aspect ratio exactly like an image file.
    """
    parsed, thumb_marker, non_thumb_marker, file_names = _preview_to_read_tuple(
        preview
    )
    return apply_preview(entry, parsed, thumb_marker, non_thumb_marker, file_names)


def sort_children_dir_first(entries: list[FolderEntry]) -> list[FolderEntry]:
    """Directories first (alphabetical), then files with ``post.md`` forced
    to the front of the file group.

    Matches the historical Explorer-style sort contract so the right pane
    (which doesn't expose a user-facing sort UI) gets a deterministic order.
    """
    dirs = sorted(
        [e for e in entries if e.is_dir],
        key=lambda e: e.path.name.lower(),
    )
    files = sorted(
        [e for e in entries if not e.is_dir],
        key=lambda e: (e.path.name.lower() != POST_MD_NAME, e.path.name.lower()),
    )
    return dirs + files


def dir_identity(entry_or_path) -> object | None:
    """Return a stable identity key for a directory, for cycle detection.

    Prefers ``(st_dev, st_ino)`` — on Windows NT ``st_ino`` is the real
    64-bit file ID, so a junction / symlink to an already-visited directory
    resolves to the same identity even though its path differs.  Falls back
    to ``os.path.realpath`` when the stat yields a zero inode (some virtual
    filesystems) so we still detect path-name cycles.  ``None`` on stat
    failure — the caller then descends without dedupe (better to over-scan
    a transiently-unreadable dir than to skip a real one).

    Accepts either an ``os.DirEntry`` (uses its cached stat when that stat
    carries a usable file id) or a ``str`` / ``Path``.  **Both forms must
    yield the same representation for the same directory**: callers seed
    their ``visited`` set from a ``Path`` (the walk root) and then compare
    ``DirEntry`` children against it, so a mixed tuple/str representation
    would make a link back to the root miss the cycle guard.
    """
    # ``os.DirEntry`` carries a ``.path`` attribute; ``Path`` also has ``.stat``
    # so we detect the DirEntry by ``.path`` specifically (a ``Path`` would
    # otherwise wrongly take the DirEntry branch and blow up on ``.path``).
    is_direntry = hasattr(entry_or_path, "path") and hasattr(entry_or_path, "stat")
    try:
        if is_direntry:
            # os.DirEntry: follow the link to the *target* dir's identity.
            st = entry_or_path.stat()
            path = entry_or_path.path
            if not getattr(st, "st_ino", 0):
                # Windows: ``DirEntry.stat()`` is assembled from the parent's
                # scandir buffer (WIN32_FIND_DATA), which carries no file id —
                # ``st_ino`` / ``st_dev`` are 0 for every ordinary directory.
                # A full ``os.stat`` does return the real 64-bit id, so take
                # it rather than dropping to ``realpath``: it keeps the
                # representation identical to the Path branch (above) AND is
                # cheaper than realpath, which opens the target to resolve
                # the final path name (~134µs/dir locally, an extra SMB
                # round-trip per directory on a NAS).
                st = os.stat(path)
        else:
            st = os.stat(entry_or_path)
            path = os.fspath(entry_or_path)
    except OSError:
        return None
    ino = getattr(st, "st_ino", 0)
    dev = getattr(st, "st_dev", 0)
    if ino:
        return (dev, ino)
    # No usable inode → fall back to the resolved path name, case-normalised
    # so two spellings of the same directory (Windows / macOS filesystems are
    # case-insensitive) can't slip past the cycle guard as distinct keys.
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:  # pragma: no cover (defensive)
        return None


def admit_dir_identity(ident: object | None, visited: set[object]) -> bool:
    """Record *ident* in *visited*; return whether its directory may be walked.

    The single admission rule behind :func:`should_descend` (#44), split out
    for callers that already computed the identity elsewhere —
    ``cache_builder._scan_one`` computes identities on its ``scandir`` worker
    threads and ``cache_builder._walk_tree`` admits them on the coordinator
    thread.  ``None`` (stat
    failure) admits without recording: better to over-scan a transiently
    unreadable directory than to skip a real one.
    """
    if ident is None:
        return True
    if ident in visited:
        return False
    visited.add(ident)
    return True


def should_descend(entry_or_path, visited: set[object]) -> bool:
    """訪問可否述語: *entry_or_path* のディレクトリへ降りてよいか (#44)。

    「あるルート配下の全ディレクトリを、リンクによる無限ループを踏まずに
    1 回ずつ訪れる」という走査カバレッジの定義そのもの。リポジトリには
    独立した 3 つのウォーカー — :func:`_iter_tree`（``walk_for_search`` /
    ``walk_recent_files`` の逐次ジェネレータ）、
    ``cache_builder._walk_tree``（並列・ストリーミング）、
    :func:`_find_images_bfs`（フォルダ代表画像の浅い BFS。項目 #222 で合流）
    — があり、カバレッジ
    一致は scanning.md の明文不変条件だが、実際には symlink 除外 (#85) /
    ジャンクション除外 (#38) / identity 表現の不一致 (#43) と同型のズレが
    繰り返し発生した。ウォーカー本体の統合は層分け（Qt 依存 / 同期）上
    妥当でないため、**訪問可否の判定だけ**をこの 1 関数に寄せる:
    symlink / ジャンクションは除外せず（``dir_identity`` がリンク先の
    identity を返すので）*visited* との照合だけで重複降下を防ぐ。

    両ウォーカーの集合一致は
    ``tests/test_viewer_cache_builder.py::test_walk_coverage_matches_iter_tree_with_links``
    の対称性テストが機械検証する。
    """
    return admit_dir_identity(dir_identity(entry_or_path), visited)


def _iter_tree(
    root: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_dir_error: Callable[[Path], None] | None = None,
) -> Iterator[tuple[os.DirEntry[str], bool]]:
    """BFS over every descendant of *root*, yielding ``(entry, is_direct_child)``.

    The traversal body shared by :func:`walk_for_search` and
    :func:`walk_recent_files` — the two differ only in *what they collect*, so
    the queue, the junction / symlink cycle guard, the cancel polling and the
    progress throttling live here once (they used to be duplicated verbatim in
    the same module, where fixing one and forgetting the other is a live risk).

    Sub-directories are enqueued as they are yielded, so a consumer never has
    to drive the descent itself; the second tuple element says whether the
    entry sits directly under *root* (the recursive search excludes those — the
    caller already has them from its shallow pass — while the recent-files
    listing includes them).  Directory entries are yielded too: the search
    surfaces matching folders as tiles.

    Entries whose ``is_dir()`` raises ``OSError`` are skipped entirely (a
    vanished / unreadable child is not classifiable, and both consumers used to
    ``continue`` on it).  A directory whose ``os.scandir`` itself raises is
    skipped too, but ``on_dir_error`` (when given) is told which one — both
    consumers wire it, because 「読めなかった」 must never be shown as
    「増えていない」 (recent files) nor as 「該当なし」 (the recursive search: a
    missed sub-tree only costs hits *while hits remain*, but a zero-hit walk
    with a hole in it is a denial the walk never earned — レビュー 2026-09-03
    項目 #64).  The caller decides what a failure means.

    ``should_cancel`` is polled at each new ``os.scandir`` and every 256
    entries; a cancelled walk simply stops iterating, so the consumer keeps
    whatever it has gathered and decides for itself whether that counts as a
    partial answer.  ``on_progress`` receives the running count of
    examined entries roughly every 1024 (and once at the end).

    ``os.DirEntry`` is yielded rather than ``Path`` because both consumers want
    its cached ``stat`` — building a ``Path`` per entry is the caller's choice,
    made only for the entries it actually keeps.
    """
    queue: deque[Path] = deque([root])
    scanned = 0
    last_reported = 0
    _PROGRESS_STRIDE = 1024
    # Cycle guard: a junction / symlink that points at an ancestor would
    # otherwise make BFS re-enumerate the same subtree under a new path name
    # forever (a result cap bounds *results*, not *scan volume*, so a low-hit
    # query never terminates and hammers the NAS).  Track each visited
    # directory's (st_dev, st_ino) identity and skip already-seen ones.
    visited: set[object] = set()
    root_id = dir_identity(root)
    if root_id is not None:
        visited.add(root_id)
    while queue:
        if should_cancel is not None and should_cancel():
            break
        current = queue.popleft()
        is_root_level = current == root
        try:
            with os.scandir(current) as it:
                children = list(it)
        except OSError:
            if on_dir_error is not None:
                on_dir_error(current)
            continue
        cancelled = False
        for i, child_entry in enumerate(children):
            scanned += 1
            if on_progress is not None and scanned - last_reported >= _PROGRESS_STRIDE:
                last_reported = scanned
                on_progress(scanned)
            if should_cancel is not None and (i & 0xFF) == 0 and should_cancel():
                cancelled = True
                break
            try:
                is_dir = child_entry.is_dir()
            except OSError:
                continue
            if is_dir and should_descend(child_entry, visited):
                queue.append(Path(child_entry.path))
            yield child_entry, is_root_level
        if cancelled:
            break
    if on_progress is not None and scanned != last_reported:
        on_progress(scanned)


def walk_for_search(
    root: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
    on_dir_error: Callable[[Path], None] | None = None,
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    or_terms: list[str] | None = None,
    suffixes: frozenset[str] | None = None,
    max_hits: int = 50000,
    on_truncated: Callable[[], None] | None = None,
) -> list[tuple[FolderEntry, str]]:
    """Walk *root* recursively (BFS) and return matching descendants as
    ``(FolderEntry, relative_path_str)`` tuples.

    Used by the viewer's "サブフォルダも検索" mode in :class:`PostGrid`.
    The immediate children of *root* itself are **excluded** — the caller
    already has them via :func:`scan_children` + the post.md metadata pass
    and applies tag/title matching there.  This function deliberately
    avoids reading ``post.md`` and never sets ``thumb_marker_path`` /
    tags — heavy I/O is reserved for the shallow direct-child pass.

    ``includes`` / ``excludes`` (each a list of already-casefolded terms)
    filter descendants by their root-relative path **on this worker
    thread**, so the caller receives only matches and never runs the
    O(descendants) substring scan on the GUI thread.  ``or_terms`` is the
    Danbooru-style ``~`` pool (already-casefolded): when non-empty, a
    descendant must additionally contain at least ONE of its members (#3 —
    the pool must not be flattened into the AND ``includes``).
    ``includes=None`` with no ``or_terms`` disables filtering (every
    descendant is returned).  Directories that don't themselves match are
    still descended into — a match may live deeper — they're just not added
    to the result list.

    ``suffixes`` (a set of lower-case extensions including the dot, e.g.
    ``{".mp4"}``) restricts **file** results to those extensions — used by the
    advanced-search media-type filter to list e.g. only videos recursively.
    Directories are unaffected by ``suffixes`` (they're always descended and,
    when matched, surfaced).  ``None`` disables the extension filter.

    ``FolderEntry`` shape mirrors :func:`scan_children`: folders get
    ``thumbnail_resolved=False`` so the existing viewport-driven lazy
    loader picks them up, files get ``thumbnail_path=path`` for thumbable
    suffixes.

    ``should_cancel`` is polled every 256 entries during stat loops and
    at each new ``os.scandir`` to abort fast on filter-text churn.
    ``on_progress``, if given, is called with the running count of entries
    examined roughly every 1024 entries (and once at the end) so a caller
    can surface live "N 件走査" progress without per-entry signal spam.
    ``max_hits`` caps the result list so a runaway scan on a deep NAS
    tree doesn't balloon memory; the BFS stops as soon as the cap is hit.
    打ち切ったことは ``on_truncated`` で呼び出し側へ返す（無言で上限に達すると
    「N 件見つかりました」が全件の顔をする — 穴を告げる ``on_dir_error`` と
    同じ規律）。戻り値の型は変えないので、渡さない呼び出し側は従来どおり。

    ``on_dir_error`` (when given) is told about every directory whose own
    ``os.scandir`` failed — the walk still returns whatever the readable part
    of the tree yielded, but the caller can then say 「一部読み取れませんでした」
    instead of presenting a holed answer (or worse, an empty one) as a
    confident 「該当なし」.  Same channel :func:`walk_recent_files` uses; the
    search left it unwired until レビュー 2026-09-03 項目 #64.

    Returns an empty list on I/O errors at the root level.
    """
    if not root.is_dir():
        return []

    def _rel_matches(rel: str) -> bool:
        if includes is None and not or_terms:
            return True
        hay = rel.casefold()
        if excludes and any(exc in hay for exc in excludes):
            return False
        if includes and not all(inc in hay for inc in includes):
            return False
        # ``~`` OR pool: at least one member must match (#3).
        return not or_terms or any(alt in hay for alt in or_terms)

    # Root-relative path of an entry, as a forward-slash string, without
    # ``PurePath.relative_to`` — that is a pure-Python parent-walk and, run on
    # EVERY entry (matching or not), it measured ~42µs/entry = about three
    # quarters of a warm recursive search's CPU, on a worker thread that never
    # yields the GIL.  ``_iter_tree`` only ever descends by appending
    # ``child_entry.path``, so every entry path starts with ``root``'s text;
    # the entry name is kept as the same safety valve the old ``except
    # ValueError`` provided.  (``walk_recent_files`` already avoids this cost
    # by only building ``Path`` objects for the entries it keeps.)
    root_text = str(root)
    root_cut = len(root_text) + (0 if root_text.endswith(("\\", "/")) else 1)

    def _rel_of(entry: os.DirEntry[str]) -> str:
        path = entry.path
        if path.startswith(root_text) and len(path) > root_cut:
            return path[root_cut:].replace("\\", "/")
        return entry.name

    results: list[tuple[FolderEntry, str]] = []
    with measure("walk_for_search", str(root)):
        for child_entry, is_root_level in _iter_tree(
            root, should_cancel=should_cancel, on_progress=on_progress,
            on_dir_error=on_dir_error,
        ):
            # Top-level direct children are excluded — the caller handles them
            # via the regular scan_children path (``_iter_tree`` has already
            # queued the directory ones for descent).
            if is_root_level:
                continue
            if len(results) >= max_hits:
                if on_truncated is not None:
                    on_truncated()
                break
            try:
                is_dir = child_entry.is_dir()
            except OSError:  # pragma: no cover (the walker already classified it)
                continue
            try:
                is_file = child_entry.is_file()
            except OSError:
                is_file = False
            try:
                st = child_entry.stat()
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                mtime = 0.0
                size = 0
            rel = _rel_of(child_entry)
            matched = _rel_matches(rel)
            child = Path(child_entry.path)
            if is_dir:
                # The descent happens regardless of the dir's own match — a hit
                # may live deeper — but the dir itself is only surfaced when it
                # matches the query.  When a media-type ``suffixes`` filter is
                # active the result is a file listing of that type, so
                # directories are descended but never surfaced.
                if matched and suffixes is None:
                    results.append(
                        (
                            FolderEntry(
                                path=child,
                                title=child.name,
                                has_post_md=False,
                                thumbnail_path=None,
                                thumbnail_resolved=False,
                                posted_at=None,
                                tags=[],
                                locked_count=0,
                                mtime=mtime,
                                is_dir=True,
                                size=0,
                                metadata_loaded=False,
                            ),
                            rel,
                        )
                    )
            elif is_file and matched and (
                suffixes is None or child.suffix.lower() in suffixes
            ):
                thumbable = child.suffix.lower() in THUMBNAILABLE_SUFFIXES
                results.append(
                    (
                        FolderEntry(
                            path=child,
                            title=child.name,
                            has_post_md=False,
                            thumbnail_path=child if thumbable else None,
                            thumbnail_resolved=True,
                            posted_at=None,
                            tags=[],
                            locked_count=0,
                            mtime=mtime,
                            is_dir=False,
                            size=size,
                            metadata_loaded=True,
                        ),
                        rel,
                    )
                )
    return results


@dataclass(frozen=True)
class RecentFilesScan:
    """Result of :func:`walk_recent_files`.

    ``files`` are ``(FolderEntry, root-relative path)`` pairs — the same shape
    :func:`walk_for_search` produces, so the grid can consume them verbatim —
    already ordered **newest first**.  ``total`` is how many files the walk
    saw *before* the newest-N cap, so the caller can say 「N 件中 M 件」 instead
    of silently truncating; ``cancelled`` marks a partial result (the caller
    normally drops it via its generation guard).

    ``failed`` separates 「読めなかった」 from 「1 件も無かった」: the walk answers
    both with an empty ``files``, but a disconnected share or a folder deleted
    between the click and the walk must not be reported as 「ファイルはありません」
    (an empty listing invites 「そうか、増えていないのか」 — the opposite of the
    truth).  The root is judged both by an upfront ``is_dir`` probe AND by
    whether its own enumeration succeeded: a list-denied ACL passes ``is_dir``
    (that check is stat-based) and a share can drop between the two, so a root
    that never got enumerated is ``failed`` even though nothing raised out.

    ``unreadable_dirs`` counts the directories whose enumeration failed —
    ``0`` means the answer is complete.  A single unreadable sub-directory deep
    in the tree is still not an error (the rest of the answer stands), but it is
    no longer invisible: the caller surfaces 「一部読み取れませんでした」 so that a
    share dying mid-walk cannot masquerade as a confident 「N 件」.
    """

    files: list[tuple[FolderEntry, str]]
    total: int
    cancelled: bool = False
    failed: bool = False
    unreadable_dirs: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > len(self.files)

    @property
    def incomplete(self) -> bool:
        """走査に穴がある（列挙できなかったディレクトリがあった）。"""
        return self.unreadable_dirs > 0


def walk_recent_files(
    root: Path,
    *,
    limit: int = 2000,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> RecentFilesScan:
    """Walk *root* recursively and return its newest *limit* files by mtime.

    Powers the left pane's 「最近追加されたファイル」 overlay: a flat, newest-first
    listing of **every file** under the selected folder, keyed on the file's own
    ``st_mtime`` — deliberately NOT the post.md 投稿日, which says when the
    creator published, not when the file landed in the library (an incremental
    sync that adds
    files to a two-year-old post is exactly the case the 投稿日 sort hides).

    Unlike :func:`walk_for_search` this **includes the direct children** of
    *root* (there is no separate shallow pass behind this view) and surfaces
    files only — directories are descended but never returned, since a folder's
    own mtime says nothing about the files added inside it.

    ``post.md`` / ``#thumb#…`` (:func:`is_meta_or_marker_name`) are skipped
    outright, and do not count toward ``total`` either.  They are not content
    that 「増えた」: the writing tool **rewrites post.md on every incremental sync**, so its
    mtime is always exactly 「最近」 and it would sit at the top of the very
    listing this view exists to make useful — in a 1 画像/投稿 service that is
    half the listing.  The exclusion is not merely cosmetic: the newest-*limit*
    cap is applied HERE while the view's 種別 / ``#thumb#`` predicates run
    in-memory AFTER it, so anything a metadata file crowds out of the cap can
    never be recovered by narrowing inside the view.  Skipping them before the
    heap also saves one ``stat`` per post.md across the whole tree.

    Only the newest *limit* files are kept, via a bounded min-heap: memory stays
    O(limit) on a library with hundreds of thousands of files, and the survivors
    are the genuinely newest ones (a BFS-order cap like ``walk_for_search``'s
    ``max_hits`` would truncate *before* the ordering is known, which would make
    the answer wrong rather than merely partial).

    ``should_cancel`` is polled per directory and every 256 entries; a cancelled
    walk returns what it has with ``cancelled=True``.  ``on_progress`` is called
    with the running count of examined entries roughly every 1024 (and once at
    the end), matching :func:`walk_for_search`'s contract.

    A *root* that is not a readable directory (share offline, folder deleted
    since the click) comes back as ``failed=True`` rather than as a plain empty
    result, and so does one whose own enumeration fails after the probe passed
    (list-denied ACL, share dropped in between).  Sub-directories that could not
    be enumerated are counted in ``unreadable_dirs`` — see
    :class:`RecentFilesScan`.
    """
    if limit <= 0:
        return RecentFilesScan([], 0)
    if not root.is_dir():
        return RecentFilesScan([], 0, failed=True)

    # (mtime, seq, path_str, size) — ``seq`` keeps the tuples totally ordered so
    # the heap never compares paths, and makes ties deterministic.  The raw
    # ``DirEntry.path`` string is stored rather than a ``Path``: all but *limit*
    # of these are dropped by ``heappushpop``, so parsing a ``Path`` here would
    # pay the (dominant) per-entry cost for every file EXAMINED to answer about
    # the ones KEPT.  The survivors are turned into ``Path`` below —
    # ``_iter_tree`` yields ``DirEntry`` precisely to leave that choice here.
    heap: list[tuple[float, int, str, int]] = []
    seq = 0
    total = 0
    unreadable_dirs = 0
    root_unreadable = False

    def _note_dir_error(failed_dir: Path) -> None:
        nonlocal unreadable_dirs, root_unreadable
        unreadable_dirs += 1
        if failed_dir == root:
            root_unreadable = True

    with measure("walk_recent_files", str(root)):
        for child_entry, _is_root_level in _iter_tree(
            root,
            should_cancel=should_cancel,
            on_progress=on_progress,
            on_dir_error=_note_dir_error,
        ):
            # Unlike the recursive search this listing DOES include the direct
            # children of *root*, so ``_is_root_level`` is ignored here.
            try:
                if not child_entry.is_file():
                    continue  # directories are descended by _iter_tree, never listed
            except OSError:
                continue
            # 内部ファイル（post.md / #thumb#）は上限枠を使わせない — 判定は
            # 名前だけなので stat の手前で落とす（docstring の理由参照）。
            if is_meta_or_marker_name(child_entry.name):
                continue
            try:
                st = child_entry.stat()
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                mtime = 0.0
                size = 0
            total += 1
            item = (mtime, seq, child_entry.path, size)
            seq += 1
            if len(heap) < limit:
                heappush(heap, item)
            else:
                heappushpop(heap, item)  # drops the oldest of the N+1
    # ``_iter_tree`` stops silently on cancel; the token is sticky, so asking it
    # once more here is what distinguishes 「打ち切った部分結果」 from a full walk.
    cancelled = should_cancel is not None and should_cancel()
    if root_unreadable:
        # ルート自体を列挙できなかった — ``is_dir()`` は stat 由来なので走査拒否
        # ACL では通ってしまうし、チェックと走査の間に共有が落ちることもある。
        # 空の成功として返すと 「ファイルはありません」= 「増えていない」 と読まれる。
        return RecentFilesScan(
            [], 0, cancelled, failed=True, unreadable_dirs=unreadable_dirs,
        )
    files: list[tuple[FolderEntry, str]] = []
    for mtime, _seq, path_str, size in sorted(heap, reverse=True):
        path = Path(path_str)
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover (defensive)
            rel = path.name
        thumbable = path.suffix.lower() in THUMBNAILABLE_SUFFIXES
        files.append(
            (
                FolderEntry(
                    path=path,
                    title=path.name,
                    has_post_md=False,
                    thumbnail_path=path if thumbable else None,
                    thumbnail_resolved=True,
                    posted_at=None,
                    tags=[],
                    locked_count=0,
                    mtime=mtime,
                    is_dir=False,
                    size=size,
                    metadata_loaded=True,
                ),
                rel,
            )
        )
    return RecentFilesScan(
        files, total, cancelled, unreadable_dirs=unreadable_dirs,
    )


def list_files(
    folder: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> list[Path]:
    """Return the list of files (not dirs) directly inside *folder*.

    ``post.md`` is forced to the front; the rest are name-sorted.

    When *should_cancel* is supplied it is polled every few hundred entries
    during the scan; a ``True`` return aborts early with an empty list so
    stale scans don't keep hammering a network share after the user has
    navigated away.
    """
    if not folder.is_dir():
        return []
    files: list[Path] = []
    with measure("list_files_scandir", str(folder)):
        try:
            with os.scandir(folder) as it:
                for i, entry in enumerate(it):
                    if (
                        should_cancel is not None
                        and (i & 0xFF) == 0
                        and should_cancel()
                    ):
                        return []
                    try:
                        if entry.is_file():
                            files.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            return []
    files.sort(key=lambda p: (p.name.lower() != POST_MD_NAME, p.name.lower()))
    return files


