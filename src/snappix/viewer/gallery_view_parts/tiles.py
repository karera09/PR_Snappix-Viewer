"""``GalleryView`` が扱う値型（タイル・座席）と種別バケット。

Qt のウィジェットを一切持たない層: :class:`Tile` は「1 セルぶんの描画状態」、
:class:`IconSeats` は「アイコン表示 1 タイルのオーバーレイ幾何」で、どちらも
描画部品（:mod:`.painter`）と殻（:mod:`..gallery_view`）が共有する。
``gallery_view`` が従来どおり re-export するので、import 元は変わらない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QPixmap

from ..folder_scan import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

ARCHIVE_SUFFIXES = frozenset({".zip", ".cbz", ".rar", ".7z", ".tar", ".gz"})
DOC_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".rtf", ".doc", ".docx"})

BUCKET_DIR = "dir"
BUCKET_IMAGE = "image"
BUCKET_MEDIA = "media"
BUCKET_ARCHIVE = "archive"
BUCKET_DOC = "doc"
BUCKET_GENERIC = "generic"


def file_icon_bucket(path: Path, is_dir: bool) -> str:
    if is_dir:
        return BUCKET_DIR
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return BUCKET_IMAGE
    if suffix in VIDEO_SUFFIXES or suffix in AUDIO_SUFFIXES:
        return BUCKET_MEDIA
    if suffix in ARCHIVE_SUFFIXES:
        return BUCKET_ARCHIVE
    if suffix in DOC_SUFFIXES:
        return BUCKET_DOC
    return BUCKET_GENERIC


@dataclass
class Tile:
    """One renderable cell.

    ``entry`` is the opaque :class:`folder_scan.FolderEntry` the host needs
    in its signal handlers; the view only reads the render fields.
    """

    key: str
    path: Path
    is_dir: bool
    caption: str
    entry: object = None
    aspect: float = 1.0
    aspect_known: bool = False
    # False for fixed-default placeholders (no-thumbnail files / empty
    # folders): their justified row is capped to target height instead of
    # stretching a lone tile into a giant full-width square.
    expandable: bool = True
    pixmap: QPixmap | None = None
    thumb_loaded: bool = False
    is_fallback: bool = False
    # True once the loader reported a decode failure for this tile — the
    # placeholder then settles from the "loading" look (dots) to the static
    # file-type glyph, so a broken image doesn't look forever-pending (C03).
    thumb_failed: bool = False
    # Physical size of ``pixmap`` (BOTH axes).  A tile whose box later grows
    # (aspect settling, justify row-height growth, slider zoom) so that the
    # pixmap would have to be *upscaled* to fill it is re-fetched crisply
    # instead of drawn blurry.  Two axes, not a single longest edge: a list
    # row's box is extremely anisotropic (full row width × ~24 px row height)
    # and the decode is constrained by the SHORT side, so a longest-edge
    # ruler mis-records what the tile actually holds (#F1D-1).
    pixmap_size: QSize = field(default_factory=lambda: QSize(0, 0))
    # Hover tooltip (typically the full path when the caption elides).
    # Empty (the default) shows no tooltip for the tile.  Appended last so
    # positional Tile(...) construction stays source-compatible.
    tooltip: str = ""
    # 「本編ではない」内部/メタファイル（post.md 等）— キャプションを淡色で
    # 描き、本編メディアと同格に見えないようにする (UIレビュー 07-25 #52)。
    # 一覧からは消さない（post.md はメタへの導線として温存する）。
    dimmed: bool = False
    # Physical box edge (longest side × dpr) at which the last decode failure
    # was observed (#114).  The host re-requests a failed tile only once its
    # box grows PAST this — so a one-off failure during a resolution upgrade
    # doesn't pin an already-loaded tile to its blurry low-res pixmap forever,
    # while a settled failure is still never hammered at the same size.
    thumb_failed_edge: int = 0
    # 警告バッジの kind（バッジ語彙レジストリの語。``""`` = 警告なし）。
    # 横断一覧の到達不能行（ゴースト）のように「タイルは出るが実体が無い」席を
    # 図像で有標にするためのフィールド。判定はホスト側の ``_tile_warning``
    # フックが in-memory で行う（追加 I/O ゼロ）。
    warn: str = ""


@dataclass
class IconSeats:
    """Computed overlay geometry for one icon-mode tile (Phase 3-2 seating).

    All rects are in the same coordinate space as the ``img_rect`` passed to
    :func:`.painter.icon_overlay_seats`.  Extracted as a pure geometry
    value object so the four-corner seating (and the structural title↔badge
    non-overlap) is unit-testable without asserting pixels: a test builds a
    view, computes the seats and asserts ``title.intersected(badge_bounds)``
    is empty.
    """

    folder: QRect          # top-left folder-type indicator (dirs only)
    similar: QRect         # top-right ◇ similar-hover button
    badge_bounds: QRect    # bottom-right badge row union (empty if no badges)
    scrim: QRect           # bottom gradient-scrim band (empty if no title)
    title: QRect           # union of ``title_rows`` (empty when no title)
    title_text: str        # elided caption ("\n"-joined rows), for diagnostics
    #: One (rect, line) per DRAWN caption row, top → bottom.  Only the LAST
    #: row yields width to the badge seat (UIレビュー 2026-08-28 N-105 — a
    #: single title rect made every row pay for badges that occupy the bottom
    #: row alone); the rows above it are lifted clear of ``badge_bounds`` so
    #: the "text and badges never overlap" contract still holds per row.
    title_rows: list       # [(QRect, str)]
    badges: list           # [(kind, value, QRect)] for draw_badge_row


def is_image_tile(tile: Tile | None) -> bool:
    """画像ファイルのタイルか（◇ 類似オーバーレイ / スピナーの前提条件）。"""
    return (
        tile is not None
        and not tile.is_dir
        and tile.path.suffix.lower() in IMAGE_SUFFIXES
    )


def tile_awaits_thumb(tile: Tile) -> bool:
    """Whether *tile* still expects a thumbnail to arrive (C03).

    True while a decode is plausibly in flight: the tile has a thumbnail
    source (``entry.thumbnail_path``) or is an unresolved folder whose
    representative image is still being looked up.  A reported decode
    failure (``thumb_failed``) settles it.
    """
    if tile.thumb_loaded or tile.thumb_failed:
        return False
    entry = tile.entry
    if getattr(entry, "thumbnail_path", None) is not None:
        return True
    return tile.is_dir and not getattr(entry, "thumbnail_resolved", True)


__all__ = [
    "ARCHIVE_SUFFIXES",
    "BUCKET_ARCHIVE",
    "BUCKET_DIR",
    "BUCKET_DOC",
    "BUCKET_GENERIC",
    "BUCKET_IMAGE",
    "BUCKET_MEDIA",
    "DOC_SUFFIXES",
    "IconSeats",
    "Tile",
    "file_icon_bucket",
    "is_image_tile",
    "tile_awaits_thumb",
]
