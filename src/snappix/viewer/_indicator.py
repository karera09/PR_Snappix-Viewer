"""Shared painting helpers that mark folder items in the viewer panes.

The entry-type cues exposed here:

* :func:`paint_folder_badge_icon_mode` — small gold folder pictogram drawn
  in the top-left corner of an item.  Used in icon-mode (thumbnail) views
  to keep folders distinct from images even when a folder has a real
  thumbnail loaded.

* :func:`paint_file_badge_icon_mode` — its muted counterpart for non-folder
  tiles, so "folder or file?" is answered by a *present* mark on both sides
  rather than by the absence of one.

* :func:`paint_folder_background_list_mode` — soft gold wash filling the
  whole row in list-mode views.

Both helpers draw with ``QPainter`` primitives only so they do not depend
on ``QStyle.standardIcon`` — that path produced black squares at small
sizes on some Qt/Windows themes.

Colour policy — deliberate fixed colours: every badge below is an overlay
drawn ON TOP OF thumbnail content, which is the registered design-system
exception (overlays on image content keep fixed colours).  These badges must stay
legible over arbitrary photos in either theme, so they use fixed dark scrims
+ fixed accent hues instead of ``current_tokens()`` — routing e.g. the
relevance blue through the theme ``accent`` would tie readability to the UI
theme rather than the underlying image.  The concrete values live in
``common/ui/overlay.py`` (the single overlay palette); the module-local names
below just alias them so the painting code reads clearly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from ..common.i18n import t
from ..common.ui import current_tokens, fixed_pixmap, overlay

_BADGE_SIZE = 18
_BADGE_MARGIN = 3
_CHIP_BG = overlay.SCRIM_CHIP
_FOLDER_FILL = overlay.FOLDER_FILL
_FOLDER_BORDER = overlay.FOLDER_BORDER
_LIST_BG_TINT = overlay.FOLDER_LIST_TINT

# File (non-folder) counterpart of the gold folder pictogram — a deliberately
# QUIET page silhouette: at a library root a bare file tile and a post-folder
# tile would look identical if only the folder side carried a type mark.  Muted white on the same dark chip keeps the folder
# badge the loud one (folders are the navigable thing) while still answering
# 「これはフォルダ？ファイル？」 at a glance.  Colours come from the overlay
# palette (design.md 使用ルール 2 — drawn over thumbnail content).
_FILE_FILL = overlay.PLACEHOLDER_WHITE
_FILE_BORDER = overlay.OVERLAY_TEXT

# Favorite-count badge (bottom-left heart + count chip).  The heart is drawn
# as a vector path — NOT a font glyph — because the ``♡`` (U+2661) glyph is
# absent from many fonts; relying on it made the measured text advance (from
# the primary font) disagree with the substituted-glyph render width, so
# ``elidedText`` intermittently collapsed ``♡1`` to ``…`` while ``♡16`` drew
# fine.  Same rationale as the folder pictogram above.  The count is plain
# ASCII digits, which every font has, so it never elides spuriously.
_FAV_HEART = overlay.HEART_RED  # soft red, legible on the dark chip
_FAV_HEART_GAP = 3  # px between the heart and the number


def _folder_path(rect: QRectF) -> QPainterPath:
    """Build a tab-on-top folder silhouette inside ``rect``.

    Constructed as the union of two rounded rectangles — a short tab on
    the upper-left and a wider body underneath — then simplified into a
    single outline so the stroke draws around the silhouette rather than
    bisecting the join.
    """
    left = rect.left()
    top = rect.top()
    width = rect.width()
    height = rect.height()
    corner = max(1.0, min(width, height) * 0.14)

    tab_rect = QRectF(left, top + height * 0.05, width * 0.55, height * 0.30)
    body_rect = QRectF(left, top + height * 0.28, width, height * 0.67)

    path = QPainterPath()
    path.addRoundedRect(tab_rect, corner, corner)
    path.addRoundedRect(body_rect, corner, corner)
    return path.simplified()


def paint_folder_badge_icon_mode(painter: QPainter, item_rect: QRect) -> None:
    """Draw a small gold folder pictogram in the top-left corner.

    A dark rounded chip sits behind the pictogram so it stays legible on
    light thumbnails.
    """
    size = _BADGE_SIZE
    margin = _BADGE_MARGIN
    chip = QRect(
        item_rect.left() + margin,
        item_rect.top() + margin,
        size,
        size,
    )
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)

    painter.setPen(Qt.NoPen)
    painter.setBrush(_CHIP_BG)
    painter.drawRoundedRect(chip, 3, 3)

    inner = QRectF(chip.adjusted(3, 3, -3, -3))
    folder = _folder_path(inner)
    painter.fillPath(folder, _FOLDER_FILL)
    pen = QPen(_FOLDER_BORDER)
    pen.setWidthF(0.9)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(folder)
    painter.restore()


def paint_folder_pictogram(painter: QPainter, rect: QRect) -> None:
    """Draw the gold folder silhouette filling *rect* (no chip behind it).

    Same vocabulary as :func:`paint_folder_badge_icon_mode` — the silhouette
    comes from the shared :func:`_folder_path`, so there is exactly one folder
    pictogram in the product — but sized to fill *rect* for surfaces that need
    the mark as the **content** rather than as a corner badge.

    Used by the stage image track: a folder with no representative image must
    not land on the decode-failure「×」glyph (that would claim "読み込み失敗"
    for something merely empty), and the left grid draws the very same object
    with this gold folder mark.
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    folder = _folder_path(QRectF(rect))
    painter.fillPath(folder, _FOLDER_FILL)
    pen = QPen(_FOLDER_BORDER)
    pen.setWidthF(max(0.9, rect.width() / 24.0))
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(folder)
    painter.restore()


def _file_path(rect: QRectF) -> QPainterPath:
    """Build a page-with-folded-corner silhouette inside ``rect``.

    Vector path (not a font glyph) for the same reason as the folder / heart /
    star silhouettes: the corresponding Unicode characters are missing or
    metrically inconsistent across the fonts this ships against.
    """
    left = rect.left()
    top = rect.top()
    w = rect.width()
    h = rect.height()
    fold = min(w, h) * 0.34

    path = QPainterPath()
    path.moveTo(left, top)
    path.lineTo(left + w - fold, top)
    path.lineTo(left + w, top + fold)
    path.lineTo(left + w, top + h)
    path.lineTo(left, top + h)
    path.closeSubpath()
    return path


def paint_file_badge_icon_mode(painter: QPainter, item_rect: QRect) -> None:
    """Draw a muted page pictogram in the top-left corner.

    The non-folder counterpart of :func:`paint_folder_badge_icon_mode`, sharing
    its seat and chip so the two read as one "what kind of thing is this tile"
    channel.  Deliberately quieter than the gold folder mark (translucent white
    instead of an accent hue) — the point is to make a bare file *distinguishable*
    at a library root, not to compete with the folder cue.
    """
    size = _BADGE_SIZE
    margin = _BADGE_MARGIN
    chip = QRect(
        item_rect.left() + margin,
        item_rect.top() + margin,
        size,
        size,
    )
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)

    painter.setPen(Qt.NoPen)
    painter.setBrush(_CHIP_BG)
    painter.drawRoundedRect(chip, 3, 3)

    # Slightly narrower than the folder body so the page reads as a sheet.
    inner = QRectF(chip.adjusted(4, 3, -4, -3))
    page = _file_path(inner)
    painter.fillPath(page, _FILE_FILL)
    pen = QPen(_FILE_BORDER)
    pen.setWidthF(0.9)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPath(page)
    painter.restore()


def _heart_path(rect: QRectF) -> QPainterPath:
    """Build a filled heart silhouette inside *rect* (tip at bottom-centre).

    Drawn as a vector path so it renders identically regardless of which
    fonts are installed — the ``♡`` glyph is missing from many of them.
    """
    x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()

    def pt(nx: float, ny: float) -> tuple[float, float]:
        return (x0 + nx * w, y0 + ny * h)

    path = QPainterPath()
    path.moveTo(*pt(0.5, 1.0))  # bottom tip
    # Left lobe up and over to the centre dip.
    path.cubicTo(*pt(0.5 - 0.55, 0.62), *pt(0.5 - 0.55, 0.22), *pt(0.5 - 0.25, 0.22))
    path.cubicTo(*pt(0.5 - 0.10, 0.22), *pt(0.5, 0.36), *pt(0.5, 0.46))
    # Right lobe (mirror) back down to the tip.
    path.cubicTo(*pt(0.5, 0.36), *pt(0.5 + 0.10, 0.22), *pt(0.5 + 0.25, 0.22))
    path.cubicTo(*pt(0.5 + 0.55, 0.22), *pt(0.5 + 0.55, 0.62), *pt(0.5, 1.0))
    path.closeSubpath()
    return path


# Star / "watch later" curation badges (user_meta.py).  Drawn ON TOP OF
# thumbnail content, so the same fixed-colour policy as the folder / favorites
# / relevance badges above applies: a gold
# star + dark chip that must stay legible over any photo in either theme.  The
# star is a vector path (not a font glyph) for the same reason as the heart:
# ``★`` is missing / metrically inconsistent across fonts.  Placed top-left,
# just BELOW the folder pictogram, so the two never overlap.
_STAR_FILL = overlay.STAR_GOLD      # warm gold
_STAR_GAP = 3

# 「あとで見る」 badge — a small blue CLOCK face in the top-right, below where the
# relevance pill would sit (relevance is search-only, later is a curation flag,
# so they rarely coincide; if they do, later drops one row).
#
# Not a bookmark ribbon (しおり): that would collide head-on with the
# product's *other* しおり — the ブックマーク feature in the menus / nav rail —
# so the same metaphor would name two unrelated things.  A clock says 「あとで」 (time) instead of 「ここに印」 (place), which is what the flag
# actually means.  The legend row in the shortcuts dialog is updated in step.
_LATER_FILL = overlay.LATER_BLUE
_LATER_HAND = overlay.OVERLAY_TEXT
#: Clock diameter.  Square (a face, not a pennant) — the badge-row width maths
#: reads ``_LATER_W``, so both names stay and hold the same value.
_LATER_W = 14
_LATER_H = 14


def _star_path(rect: QRectF) -> QPainterPath:
    """Build a filled 5-point star silhouette inside *rect*."""
    import math

    cx = rect.x() + rect.width() / 2.0
    cy = rect.y() + rect.height() / 2.0
    r_out = min(rect.width(), rect.height()) / 2.0
    r_in = r_out * 0.42
    path = QPainterPath()
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = r_out if i % 2 == 0 else r_in
        x = cx + r * math.cos(ang)
        y = cy + r * math.sin(ang)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


#: 「未取得コンテンツ数」 の南京錠。タイルのバッジ行には載らない（キャプション
#: 側の指標）が、凡例と情報パネルのメタカードが**同じ絵**で説明できるよう語彙
#: レジストリに載せる — i18n 値に 🔒 を直書きしないための置き場。絵文字はテーマにもフォントにも追従しないので、♡ / ★ と同じく
#: ベクタパスで描く。
_LOCK_FILL = overlay.OVERLAY_TEXT

#: キャプション（プレーンテキスト行）で使う南京錠の**文字**形。
#:
#: タイルのバッジ行・凡例・情報パネルは上のベクタパスで描くが、
#: ``post_grid._format_subtitle`` が組み立てるサブタイトルは「日付 · 🔒N ·
#: プラン」を 1 本の文字列として省略（elide）処理へ渡す設計で、そこへチップの
#: ピクスマップを差し込むにはキャプション行の描画そのもの
#: (``GalleryView._paint_caption`` / ``_title_rows``) を作り替える必要がある
#: —— 行描画の変更が大きすぎるので、その範囲には踏み込まない。
#: 図像を完全に 1 実装へ寄せるところまでは行けないが、**文字リテラルの置き場**
#: だけはこのレジストリに 1 つへ寄せておく（``post_grid`` 側に 🔒 を直書き
#: すると、南京錠の姿がレジストリと post_grid の 2 箇所で別々に決まる）。
LOCKED_CAPTION_GLYPH = "🔒"


def _lock_path(rect: QRectF) -> QPainterPath:
    """Build a padlock silhouette (shackle + body) inside *rect*."""
    x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    body = QRectF(x0, y0 + h * 0.42, w, h * 0.58)
    path = QPainterPath()
    path.addRoundedRect(body, w * 0.18, w * 0.18)
    # Shackle: an open ring above the body, thick enough to read at 8px.
    outer = QRectF(x0 + w * 0.18, y0 + h * 0.02, w * 0.64, h * 0.62)
    inner = QRectF(x0 + w * 0.34, y0 + h * 0.18, w * 0.32, h * 0.46)
    ring = QPainterPath()
    ring.addEllipse(outer)
    hole = QPainterPath()
    hole.addEllipse(inner)
    ring = ring.subtracted(hole)
    # Clip the ring's lower half away so it reads as a shackle, not a circle.
    lower = QPainterPath()
    lower.addRect(QRectF(x0, y0 + h * 0.46, w, h * 0.54))
    ring = ring.subtracted(lower)
    return path.united(ring).simplified()


def draw_clock_glyph(
    painter: QPainter, rect: QRectF, outline: QColor | None = None
) -> None:
    """Draw the 「あとで見る」 clock face inside *rect*.

    A filled blue disc with two light hands (12 時 / 3 時), drawn with painter
    primitives so it renders identically regardless of installed fonts — same
    rule as the folder / heart / star silhouettes above.  Shared by the corner
    badge and the badge-row chip so the two can never drift apart.  With
    *outline* it draws an unfilled ring + hands in that colour instead (the
    "not set" state, same grammar as the ★ outline / fill).
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(_LATER_HAND if outline is None else outline)
    pen.setWidthF(max(1.0, rect.width() * 0.11))
    pen.setCapStyle(Qt.RoundCap)
    if outline is None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(_LATER_FILL)
        painter.drawEllipse(rect)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if outline is not None:
        half = pen.widthF() / 2.0
        painter.drawEllipse(rect.adjusted(half, half, -half, -half))
    centre = rect.center()
    radius = rect.width() / 2.0
    painter.drawLine(centre, QPointF(centre.x(), centre.y() - radius * 0.55))
    painter.drawLine(centre, QPointF(centre.x() + radius * 0.42, centre.y()))
    painter.restore()


# ------------------------------------------------------------------ badge row
#
# 四隅の座席表 (tile-overlay seating chart).
# The icon-mode tile overlay layer has a fixed seating chart so overlays never
# collide with each other or with the on-image title band:
#
#     ┌──────────────────────────────────────────┐
#     │ [folder]                        [◇similar] │  top-left / top-right
#     │                                            │
#     │                                            │
#     │▓▓▓▓ title band on gradient scrim ▓▓ [♡][★] │  bottom: title + badge row
#     └──────────────────────────────────────────┘
#
# * top-left     = folder-type indicator (:func:`paint_folder_badge_icon_mode`)
# * top-right    = ◇ similar-image hover button (``GalleryView``)
# * bottom-right = THIS badge row (favorites / star / later / relevance)
# * bottom       = title band on a gradient scrim (``GalleryView``)
#
# The badge row lays present badges out RIGHT-TO-LEFT from the bottom-right
# corner in a fixed seat order, and the title's draw width is trimmed to the
# row's left edge so text and badges are *structurally* non-overlapping (unit
# tested via ``GalleryView._icon_overlay_seats``).  Colours stay fixed
# dark-scrim + light, the registered design-system exception (design.md 使用
# ルール 2), so the row reads on any photo.
_ROW_MARGIN = 4
_ROW_GAP = 4
_ROW_PAD_X = 5
_ROW_PAD_Y = 2
_ROW_CHIP_BG = overlay.SCRIM_CHIP_STRONG
_ROW_TEXT = overlay.OVERLAY_TEXT


@dataclass(frozen=True)
class BadgeStyle:
    """1 つのチップを描くときの**配色セット**（図像そのものは切り替えない）。

    同じバッジが 2 種類の地の上に出る: タイル / ライトボックスの **画像の上**
    （使用ルール 2 の固定スクリム + 白）と、情報パネル・凡例・チートシートの
    **UI 面の上**。後者にスクリムを敷くと、テーマ面の中に黒い札が浮いて
    「画像から切り出した絵」に見え、ライトテーマでは白文字が面と同化する。

    分岐させるのは色だけ — ``painter_fn`` が図像の唯一の実装であるという
    単一情報源（design.md のバッジ語彙レジストリ）を保つため、面ごとの
    グリフ表は作らない。
    """

    #: True = UI 面（スクリムを敷かない）。False = 画像の上（固定スクリム）。
    on_surface: bool
    #: チップに描く文字の色。
    text: QColor
    #: 「スクリム上の白」で塗られるベクタグリフ（🔒 等）の色。
    glyph: QColor
    #: 同じ役割の、SVG アイコン (:func:`fixed_pixmap`) 用の色文字列（◇ 等）。
    icon_hex: str


#: 画像コンテンツの上に載るときの既定配色（従来の見た目そのまま）。
OVERLAY_STYLE = BadgeStyle(
    on_surface=False, text=_ROW_TEXT, glyph=_ROW_TEXT, icon_hex=overlay.CTRL_ICON,
)


def surface_style(tone: QColor | None = None) -> BadgeStyle:
    """UI 面に貼るときの配色（スクリム無し + テーマの ``text`` 色）。

    トークンは呼び出しのたびに読む — 既存のテーマ切替時の貼り直し機構
    （``shortcuts_dialog`` の ``changeEvent`` → ``_populate``）にそのまま乗る。

    *tone* を渡すとその色で貼る。面の地がテーマの ``bg_*`` ではないとき
    （一覧行の選択帯 = ``highlight``）に、その面がすでに使っている前景色
    （``highlightedText``）へ合わせるための口。
    """
    if tone is None:
        name = current_tokens().text
        return BadgeStyle(
            on_surface=True, text=QColor(name), glyph=QColor(name), icon_hex=name,
        )
    return BadgeStyle(
        on_surface=True, text=QColor(tone), glyph=QColor(tone),
        icon_hex=tone.name(),
    )

# Fixed seat order (list order == right-to-left placement): favorites sits
# rightmost, then star, later, relevance to its left.  Never reorder by value
# — a stable seat lets the eye find each badge type in the same place.
BADGE_ROW_ORDER = ("favorites", "star", "later", "relevance")


def badge_font(base_font):
    """Return the shrunk-1pt bold font shared by every badge-row chip.

    Kept a module function so ``GalleryView`` can build the exact same
    ``QFontMetrics`` for its geometry pass (:meth:`_icon_overlay_seats`) that
    the painter uses, guaranteeing measured widths match drawn widths.
    """
    font = type(base_font)(base_font)
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(max(7.0, size - 1.0))
    font.setBold(True)
    return font


def _row_present(favorites, star, later, relevance) -> list[tuple[str, object]]:
    """Filter (kind, value) pairs to the badges actually shown, in seat order."""
    values = {
        "favorites": favorites if isinstance(favorites, int) and favorites >= 0 else None,
        "star": star if isinstance(star, int) and star > 0 else None,
        "later": True if later else None,
        "relevance": float(relevance) if isinstance(relevance, (int, float)) else None,
    }
    return [(k, values[k]) for k in BADGE_ROW_ORDER if values[k] is not None]


def _row_chip_width(fm, kind: str, value) -> int:
    spec = _BADGE_SPECS.get(kind)
    if spec is None:
        return 0
    return round(spec.width(fm, spec.format_value(value)))


def badge_row_layout(
    fm, item_rect: QRect, favorites=None, star=0, later=False, relevance=None,
) -> tuple[list[tuple[str, object, QRect]], QRect]:
    """Pure geometry for the bottom-right badge row (no painting).

    Returns ``(placements, bounds)`` where *placements* is a list of
    ``(kind, value, QRect)`` laid right-to-left in the seat order, and *bounds*
    is their union (an empty ``QRect`` when nothing shows).  A chip that would
    spill past the tile's left margin is dropped, so on a very narrow tile the
    high-priority seats (favorites first) survive.  The same applies on the
    HEIGHT axis: a tile too short to seat one chip row inside its margins gets
    no badges at all rather than a row placed above its own top edge (a 30:1
    image justifies to an ~18 px row — #F1D-2).  Kept side-effect free so it
    is unit-testable and reused by :meth:`GalleryView._icon_overlay_seats`.
    """
    present = _row_present(favorites, star, later, relevance)
    if not present:
        return [], QRect()
    chip_h = fm.height() + 2 * _ROW_PAD_Y
    if chip_h + 2 * _ROW_MARGIN > item_rect.height():
        return [], QRect()
    top = item_rect.bottom() - _ROW_MARGIN - chip_h + 1
    left_limit = item_rect.left() + _ROW_MARGIN
    x = item_rect.right() - _ROW_MARGIN + 1
    placements: list[tuple[str, object, QRect]] = []
    for kind, value in present:
        w = _row_chip_width(fm, kind, value)
        chip_left = x - w
        if chip_left < left_limit:
            break
        placements.append((kind, value, QRect(chip_left, top, w, chip_h)))
        x = chip_left - _ROW_GAP
    if not placements:
        return [], QRect()
    bounds = placements[0][2]
    for _, _, r in placements[1:]:
        bounds = bounds.united(r)
    return placements, bounds


#: 四隅の席（:func:`badge_corner_layout` の *corner*）。
BADGE_CORNERS = ("topleft", "topright", "bottomleft", "bottomright")


def badge_corner_layout(
    fm,
    item_rect: QRect,
    corner: str,
    kinds,
    *,
    start_row: int = 0,
) -> list[tuple[str, object, QRect]]:
    """Pure geometry for a stacked CORNER badge seat (no painting).

    Same rule as :func:`badge_row_layout`: a chip whose **natural** width does
    not fit the seat is dropped, never clamped.  A clamped chip silently reads
    as a different value（``83%`` が ``3%`` に、``♡12345`` が ``♡12`` に見える）
    ので、幅計算の権威はこの 1 本に寄せる。段（``kinds`` の順）は上隅なら下へ、
    下隅なら上へ積み、席から出る段は落とす。*start_row* はその隅に既に置いた
    別図像（フォルダ図像など）のぶんだけ段をずらす。
    """
    placements: list[tuple[str, object, QRect]] = []
    if corner not in BADGE_CORNERS:
        return placements
    chip_h = fm.height() + 2 * _ROW_PAD_Y
    avail = item_rect.width() - 2 * _ROW_MARGIN
    top_anchored = corner.startswith("top")
    left_anchored = corner.endswith("left")
    row = max(0, start_row)
    for kind, value in kinds:
        spec = _BADGE_SPECS.get(kind)
        if spec is None:
            continue
        width = round(spec.width(fm, spec.format_value(value)))
        if width <= 0 or width > avail:
            continue
        step = row * (chip_h + _ROW_GAP)
        if top_anchored:
            top = item_rect.top() + _ROW_MARGIN + step
            if top + chip_h > item_rect.bottom() + 1 - _ROW_MARGIN:
                break
        else:
            top = item_rect.bottom() + 1 - _ROW_MARGIN - chip_h - step
            if top < item_rect.top() + _ROW_MARGIN:
                break
        left = (
            item_rect.left() + _ROW_MARGIN if left_anchored
            else item_rect.right() + 1 - _ROW_MARGIN - width
        )
        placements.append((kind, value, QRect(left, top, width, chip_h)))
        row += 1
    return placements


#: 一覧行のキャプションとその右の印の間に必ず空ける距離（px）。
_TRAIL_GAP = 8


def badge_trail_layout(
    fm, seat_rect: QRect, kinds,
) -> tuple[list[tuple[str, object, QRect]], QRect]:
    """Pure geometry for the **caption-trailing** badge seat (no painting).

    一覧表示（``gallery_view_parts.painter.paint_list_row``）の行は 24px 前後のアイコン枠
    しか画像面を持たないので、四隅の席（:func:`badge_corner_layout`）には
    自然幅のチップが入らない = 落ちる。行の右端にはキャプション行の高さが
    そのまま使えるので、印はそこへ横一列に並べる。

    *seat_rect* は「アイコンの右からの残り全部」（キャプションと印が分け合う
    領域）。返すのは ``(placements, caption_rect)`` で、*caption_rect* は印と
    :data:`_TRAIL_GAP` を差し引いた**キャプションの描画矩形**。こうしておくと
    キャプションは印の下へ潜らず、印の手前で省略される（構造的に非重複）。

    席の規則は :func:`badge_row_layout` / :func:`badge_corner_layout` と同じ
    「自然幅が入らなければクランプせず落とす」。並びは
    :data:`BADGE_ROW_ORDER`（右から favorites → star → later → relevance）を
    そのまま使い、どの面でも同じ位置に同じ印がある状態を保つ。
    """
    placements: list[tuple[str, object, QRect]] = []
    if not kinds or seat_rect.width() <= 0:
        return placements, QRect(seat_rect)
    chip_h = min(fm.height() + 2 * _ROW_PAD_Y, seat_rect.height())
    if chip_h <= 0:
        return placements, QRect(seat_rect)
    top = seat_rect.top() + (seat_rect.height() - chip_h) // 2
    order = {kind: i for i, kind in enumerate(BADGE_ROW_ORDER)}
    ordered = sorted(kinds, key=lambda kv: order.get(kv[0], len(order)))
    right = seat_rect.right() + 1
    for kind, value in ordered:
        spec = _BADGE_SPECS.get(kind)
        if spec is None:
            continue
        width = round(spec.width(fm, spec.format_value(value)))
        if width <= 0:
            continue
        # 印を置いたあともキャプションに間隔ぶんの席が残ること。残らない行
        # では印を落として名前を優先する（名前は行の本体）。
        if right - width - _TRAIL_GAP <= seat_rect.left():
            break
        placements.append((kind, value, QRect(right - width, top, width, chip_h)))
        right -= width + _ROW_GAP
    if not placements:
        return placements, QRect(seat_rect)
    consumed = seat_rect.right() + 1 - (right + _ROW_GAP)
    caption = QRect(seat_rect).adjusted(0, 0, -(consumed + _TRAIL_GAP), 0)
    return placements, caption


def draw_badge_row(
    painter: QPainter, fm, placements: list[tuple[str, object, QRect]],
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    """Paint pre-computed badge-row *placements* (from :func:`badge_row_layout`).

    The caller must have already set the painter's font to :func:`badge_font`
    (the same font *fm* was built from) so widths match.  *style* selects the
    palette (画像の上 = 固定スクリム / UI 面 = :func:`surface_style`).
    """
    if not placements:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    for kind, value, r in placements:
        spec = _BADGE_SPECS.get(kind)
        if spec is None:
            continue
        spec.painter(painter, QRectF(r), spec.format_value(value), fm, style)
    painter.restore()


# =============================================== バッジ語彙レジストリ (提案1)
#
# 図像の単一情報源.
#
# 色には ``common/ui/tokens.py``、語には用語表という単一情報源がある。図像も
# 1 か所に寄せないと、同じ概念が面ごとに別の絵で説明される —— 凡例が実際には
# 描かれない記号を説明する、♡ がタイルではベクタ描画・凡例と文言では生の
# 絵文字という二重表現になる、チップ幅の計算と描画が数字を描く条件を別々に
# 判定する、といったずれが起きる。
#
# ここが唯一の表:
#
# * ``kind``           — バッジの識別子（バッジ行の座席順 ``BADGE_ROW_ORDER``
#                        と同じ語）
# * ``painter_fn``     — チップ 1 個を矩形へ描く**唯一の実装**
# * ``width_fn``       — そのチップの自然幅（描画と幅計算のドリフト防止）
# * ``i18n_name_key``  — 文言用の正式名（:func:`badge_name`）
# * ``tooltip_key``    — 説明文（凡例の 2 列目・ツールチップ）
#
# 生えている API は 3 つ:
#
# * :func:`draw_badge_row`  — タイル描画（バッジ行）
# * :func:`badge_pixmap` / :func:`badge_icon` — 凡例・情報パネル・チートシート
#   用の 18px 実物チップ
# * :func:`badge_name`      — 文言用
#
# **新しいバッジ図像を足すときは必ずここへ 1 行足すこと。** i18n 値へ図像文字
# （♡ / 🔒 / ◆ / ◇ …）を書くのは禁止で、``tests/test_i18n_no_pictographs.py``
# が機械検証する（``test_no_hardcoded_colors.py`` の図像版）。


def _chip_background(
    painter: QPainter, rect: QRectF, brush=_ROW_CHIP_BG,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    """チップの地を敷く（UI 面では敷かない — :class:`BadgeStyle` 参照）。"""
    if style.on_surface:
        return
    painter.setPen(Qt.NoPen)
    painter.setBrush(brush)
    painter.drawRoundedRect(rect, 4, 4)


def _heart_size(fm) -> tuple[float, float]:
    h = max(7.0, fm.ascent() * 0.82)
    return h * 1.05, h


def _star_size(fm) -> tuple[float, float]:
    s = max(8.0, fm.ascent() * 0.9)
    return s, s


def _lock_size(fm) -> tuple[float, float]:
    h = max(8.0, fm.ascent() * 0.9)
    return h * 0.78, h


def _glyph_chip_painter(size_fn, draw_glyph, gap: float, chip_bg=_ROW_CHIP_BG):
    """Build a ``painter_fn`` for a "vector glyph + optional count" chip."""

    def paint(
        painter: QPainter, rect: QRectF, text: str, fm,
        style: BadgeStyle = OVERLAY_STYLE,
    ) -> None:
        _chip_background(painter, rect, chip_bg, style)
        gw, gh = size_fn(fm)
        gx = rect.left() + _ROW_PAD_X
        gy = rect.top() + (rect.height() - gh) / 2.0
        draw_glyph(painter, QRectF(gx, gy, gw, gh), style)
        if not text:
            return
        text_left = gx + gw + gap
        tr = QRectF(
            text_left, rect.top(), rect.right() - text_left - _ROW_PAD_X, rect.height(),
        )
        if tr.width() > 0:
            painter.setPen(style.text)
            painter.drawText(tr, int(Qt.AlignLeft | Qt.AlignVCenter), text)

    return paint


def _glyph_chip_width(size_fn, gap: float):
    """Build the matching ``width_fn`` for :func:`_glyph_chip_painter`."""

    def width(fm, text: str, height: float) -> float:
        gw, _gh = size_fn(fm)
        extra = (gap + fm.horizontalAdvance(text)) if text else 0.0
        return gw + extra + 2 * _ROW_PAD_X

    return width


def _paint_later_chip(
    painter: QPainter, rect: QRectF, text: str, fm,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    # 時計図像 — 直径はチップ高に収まる範囲で座席幅
    # (_LATER_W) と同じにし、正円を保つ。文字盤は青地 + 白針の意味色ペアなので
    # 地が変わっても読める（style は地の有無だけに効く）。
    _chip_background(painter, rect, _ROW_CHIP_BG, style)
    d = min(float(_LATER_W), float(rect.height() - 2))
    px = rect.left() + _ROW_PAD_X + (_LATER_W - d) / 2.0
    py = rect.top() + (rect.height() - d) / 2.0
    draw_clock_glyph(painter, QRectF(px, py, d, d))


def _paint_relevance_chip(
    painter: QPainter, rect: QRectF, text: str, fm,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    # 数字だけ（記号は付けない）— タイルに実際に描かれるのは ``NN%`` のチップ
    # なので、凡例と文言もこの形だけを説明する。
    _chip_background(painter, rect, _ROW_CHIP_BG, style)
    painter.setPen(style.text)
    painter.drawText(rect, int(Qt.AlignCenter), text)


def _paint_thumb_fail_chip(
    painter: QPainter, rect: QRectF, text: str, fm,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    # 代替サムネイル（#thumb# クリエイターアイコンしか無かったフォルダ）の
    # warning 枠。``gallery_view_parts.painter.paint_fallback_border`` と同じトークン色。
    pen = QPen(QColor(current_tokens().warning))
    pen.setWidthF(2.0)
    pen.setJoinStyle(Qt.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(rect.adjusted(1.0, 1.0, -1.0, -1.0))


def _paint_similar_chip(
    painter: QPainter, rect: QRectF, text: str, fm,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    """タイルホバーの「類似画像を検索」ボタン.

    記号文字 ◇ の ``drawText`` をやめ ``icons.py`` の登録済みグリフを固定色で
    描く（♡ / ★ と同じ「フォント欠落に依存しない」規律）。
    """
    _chip_background(painter, rect, _CHIP_BG, style)
    _draw_chip_glyph(painter, rect, "similar", style.icon_hex)


def _draw_chip_glyph(
    painter: QPainter, rect: QRectF, icon_name: str, color: str,
) -> None:
    """``icons.py`` の登録済みグリフをチップの中央へ 1 枚描く（共通実装）。

    ◇（類似検索）とゴーストの ⚠ が同じ経路を通るので、片方だけ dpr の扱いが
    ズレる（= 片側だけボケる）ことが起きない。
    """
    edge = max(8.0, min(rect.width(), rect.height()) - 8.0)
    try:
        # Device DPR *and* any world scale the caller installed (badge_pixmap
        # scales the painter) — otherwise the glyph rasterises at 1x and is
        # upscaled into a blur.
        dpr = float(painter.device().devicePixelRatio()) * abs(
            painter.transform().m11()
        )
    except Exception:  # pragma: no cover (defensive — non-widget paint devices)
        dpr = 1.0
    pm = fixed_pixmap(icon_name, color, size=int(round(edge)), dpr=max(1.0, dpr))
    painter.drawPixmap(
        QRectF(
            rect.left() + (rect.width() - edge) / 2.0,
            rect.top() + (rect.height() - edge) / 2.0,
            edge,
            edge,
        ),
        pm,
        QRectF(pm.rect()),
    )


def _paint_ghost_chip(
    painter: QPainter, rect: QRectF, text: str, fm,
    style: BadgeStyle = OVERLAY_STYLE,
) -> None:
    """到達不能な行（ゴーストタイル）の警告チップ。

    横断一覧の「見つかりません / 読み取れません」の席は、これまでキャプション
    文言だけが通常タイルとの差だった（サムネイルが無いのは未読込のタイルとも
    共通なので、絵としては見分けが付かなかった）。図像は ``warning`` トークン
    の ⚠ — ``_paint_thumb_fail_chip`` と同じ「注意」の色語彙。
    """
    _chip_background(painter, rect, _ROW_CHIP_BG, style)
    _draw_chip_glyph(painter, rect, "alert-triangle", current_tokens().warning)


def _square_width(fm, text: str, height: float) -> float:
    return height


@dataclass(frozen=True)
class BadgeSpec:
    """One entry of the badge vocabulary (see the module section above)."""

    kind: str
    #: チップ 1 個を矩形へ描く**唯一の実装**。第 5 引数は配色セット
    #: (:class:`BadgeStyle`)。既定は画像上のオーバーレイ配色。
    painter_fn: Callable[..., None]
    width_fn: Callable[[object, str, float], float]
    i18n_name_key: str
    tooltip_key: str
    #: Text drawn inside :func:`badge_pixmap`'s specimen chip (``""`` = none).
    sample: str = ""
    #: Badge value → chip text.  The ONLY place a badge decides whether its
    #: count is drawn, so width and paint can never disagree.
    format_value: Callable[[object], str] = lambda value: ""

    def painter(
        self, painter: QPainter, rect: QRectF, text: str, fm=None,
        style: BadgeStyle = OVERLAY_STYLE,
    ) -> None:
        self.painter_fn(painter, rect, text, fm, style)

    def width(self, fm, text: str, height: float | None = None) -> float:
        if height is None:
            height = float(fm.height() + 2 * _ROW_PAD_Y)
        return self.width_fn(fm, text, height)


def _count_text(value) -> str:
    return str(int(value))


_BADGE_SPECS: dict[str, BadgeSpec] = {
    "favorites": BadgeSpec(
        kind="favorites",
        painter_fn=_glyph_chip_painter(
            _heart_size,
            lambda p, r, style: p.fillPath(_heart_path(r), _FAV_HEART),
            _FAV_HEART_GAP,
        ),
        width_fn=_glyph_chip_width(_heart_size, _FAV_HEART_GAP),
        i18n_name_key="viewer.post_grid.prefix_favorites",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_likes",
        sample="N",
        format_value=_count_text,
    ),
    "star": BadgeSpec(
        kind="star",
        painter_fn=_glyph_chip_painter(
            _star_size,
            lambda p, r, style: p.fillPath(_star_path(r), _STAR_FILL),
            _STAR_GAP,
        ),
        width_fn=_glyph_chip_width(_star_size, _STAR_GAP),
        i18n_name_key="viewer.post_grid.star_menu",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_star",
        sample="N",
        # ★1 も数字を描く: 数字の無い★は「壊れて見える」うえ、情報パネルの
        # メタカードは「★ 1」と数字付きで出すので、2 面で表記が割れる。
        format_value=_count_text,
    ),
    "later": BadgeSpec(
        kind="later",
        painter_fn=_paint_later_chip,
        width_fn=lambda fm, text, height: float(_LATER_W + 2 * _ROW_PAD_X),
        i18n_name_key="viewer.post_grid.watch_later",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_later",
    ),
    "relevance": BadgeSpec(
        kind="relevance",
        painter_fn=_paint_relevance_chip,
        width_fn=lambda fm, text, height: fm.horizontalAdvance(text) + 2 * _ROW_PAD_X,
        i18n_name_key="viewer.badge.name_relevance",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_relevance",
        sample="NN%",
        format_value=lambda value: (
            f"{max(0, min(100, round(float(value) * 100)))}%"
        ),
    ),
    "locked": BadgeSpec(
        kind="locked",
        painter_fn=_glyph_chip_painter(
            _lock_size,
            lambda p, r, style: p.fillPath(_lock_path(r), style.glyph),
            _FAV_HEART_GAP,
        ),
        width_fn=_glyph_chip_width(_lock_size, _FAV_HEART_GAP),
        i18n_name_key="viewer.markdown_view.meta_locked",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_locked",
        sample="N",
        format_value=_count_text,
    ),
    "thumb_fail": BadgeSpec(
        kind="thumb_fail",
        painter_fn=_paint_thumb_fail_chip,
        width_fn=_square_width,
        i18n_name_key="viewer.badge.name_thumb_fail",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_thumb_fail",
    ),
    "similar": BadgeSpec(
        kind="similar",
        painter_fn=_paint_similar_chip,
        width_fn=_square_width,
        i18n_name_key="viewer.badge.name_similar",
        tooltip_key="viewer.common.similar_search_image",
    ),
    "ghost": BadgeSpec(
        kind="ghost",
        painter_fn=_paint_ghost_chip,
        width_fn=_square_width,
        i18n_name_key="viewer.badge.name_ghost",
        tooltip_key="viewer.shortcuts_dialog.desc_badge_ghost",
    ),
}

#: Public, read-only view of the vocabulary (tests / callers iterate this).
BADGE_KINDS: tuple[str, ...] = tuple(_BADGE_SPECS)


def badge_spec(kind: str) -> BadgeSpec:
    """The :class:`BadgeSpec` for *kind* (``KeyError`` for an unknown kind)."""
    return _BADGE_SPECS[kind]


def badge_name(kind: str) -> str:
    """The badge's 正式名 for use in prose / tooltips."""
    return t(_BADGE_SPECS[kind].i18n_name_key)


def badge_tooltip(kind: str) -> str:
    """The badge's one-line explanation (legend column 2 / hover help)."""
    return t(_BADGE_SPECS[kind].tooltip_key)


def _pixmap_font(height: int) -> QFont:
    font = QFont()
    font.setPixelSize(max(8, int(round(height * 0.62))))
    font.setBold(True)
    return font


def badge_pixmap(
    kind: str, *, size: int = 18, dpr: float = 1.0, text: str | None = None,
    on_surface: bool = False,
) -> QPixmap:
    """A specimen chip of *kind*, ``size`` px tall — **the real badge artwork**.

    For the surfaces that *explain* badges rather than draw them on a tile:
    the ショートカット一覧のバッジ凡例, the 情報パネル meta card and the search
    cheat-sheet.  Spelling the badge out as a literal character
    (``"♡ N"`` / ``"◆ NN%"``) lets the legend drift into describing a glyph the
    tile no longer draws; going through the same ``painter_fn`` as the tile
    makes that drift impossible.

    Width is the badge's natural width at that height (not square).  ``text``
    overrides what the chip carries: the default is the spec's specimen
    (``"N"`` / ``"NN%"``, right for a legend row that stands alone), while
    ``text=""`` draws the glyph only — what a surface that already prints the
    real number next to it wants (情報パネルのメタカード).

    ``on_surface=True`` swaps the **配色** (not the artwork) to the UI-surface
    set (:func:`surface_style`): no dark scrim, theme ``text`` colour.  The
    explaining surfaces are ordinary chrome, not image content, so the fixed
    scrim that keeps a tile badge legible only reads there as a black card
    pasted into the panel — and the fixed white text vanishes on a light
    theme.  **GUI thread only** —
    this returns a ``QPixmap``; the worker-thread rule (thumbnails decode to
    ``QImage``) is unaffected because no worker calls it.
    """
    spec = _BADGE_SPECS[kind]
    label = spec.sample if text is None else text
    font = _pixmap_font(size)
    fm = QFontMetricsF(font)
    width = max(1.0, spec.width(fm, label, float(size)))
    pm = QPixmap(int(round(width * dpr)), int(round(size * dpr)))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(dpr, dpr)
    painter.setFont(font)
    style = surface_style() if on_surface else OVERLAY_STYLE
    spec.painter(painter, QRectF(0.0, 0.0, width, float(size)), label, fm, style)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def badge_icon(
    kind: str, *, size: int = 18, text: str | None = None,
    on_surface: bool = False,
):
    """1x/2x :class:`QIcon` wrapper around :func:`badge_pixmap` (GUI thread)."""
    from PySide6.QtGui import QIcon

    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(
            badge_pixmap(
                kind, size=size, dpr=dpr, text=text, on_surface=on_surface,
            )
        )
    return ic


def paint_similar_button(painter: QPainter, rect: QRect | QRectF) -> None:
    """Draw the tile-hover 「類似画像を検索」 button into *rect*.

    Thin wrapper so ``gallery_view`` and the badge legend get the identical
    artwork from the one registry entry.
    """
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    _paint_similar_chip(painter, QRectF(rect), "", None, OVERLAY_STYLE)
    painter.restore()


def paint_folder_background_list_mode(
    painter: QPainter, item_rect: QRect, is_selected: bool,
) -> None:
    """Fill the row with a soft gold tint to mark folder items.

    Skipped when the item is selected so the Qt highlight color shows
    cleanly on top.
    """
    if is_selected:
        return
    painter.save()
    painter.fillRect(item_rect, _LIST_BG_TINT)
    painter.restore()
