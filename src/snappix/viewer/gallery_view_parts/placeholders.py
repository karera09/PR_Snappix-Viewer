"""サムネイルが無いタイルの下敷き（パネル + 待ち / 確定の描き分け）。

待ち（デコードが飛んでいる）と確定（ソースが無い / 失敗した）を**別の絵**に
するのがこの層の仕事 (C03): 待ちは 3 点ドット、確定は淡い種別グリフ。同じ絵に
すると「読み込み中が永久に消えない」ように見える。

種別グリフは 1 枚を作るのに OS のスタイル資源をデコードするので、``(bucket,
論理サイズ, dpr)`` でキャッシュする（呼び出し側の辞書を借りる — テーマ変更で
ビューがまとめて捨てられるように）。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QStyle

from ...common.ui import RADIUS_SM, icon
from ..qimage_decode import _QT_READER_LOCK
from .tiles import (
    BUCKET_ARCHIVE,
    BUCKET_DIR,
    BUCKET_DOC,
    BUCKET_GENERIC,
    BUCKET_IMAGE,
    BUCKET_MEDIA,
    Tile,
    file_icon_bucket,
    tile_awaits_thumb,
)

BUCKET_TO_STYLE = {
    BUCKET_DIR: QStyle.SP_DirIcon,
    BUCKET_IMAGE: QStyle.SP_FileDialogContentsView,
    BUCKET_MEDIA: QStyle.SP_MediaPlay,
    # NOT SP_DirLinkIcon — its blue shortcut arrow reads as an external link
    # on a .zip, which confused users.  Plain file glyph instead.
    BUCKET_ARCHIVE: QStyle.SP_FileIcon,
    BUCKET_DOC: QStyle.SP_FileIcon,
    BUCKET_GENERIC: QStyle.SP_FileIcon,
}


def paint_placeholder(
    painter, rect: QRect, tile: Tile, *,
    palette: QPalette, view_mode: str, dpr: float, qstyle, cache: dict,
) -> None:
    """Draw a calm loading / image-less placeholder for *tile*.

    A subtle neutral panel (mostly the view's base colour), with two
    distinguishable states (C03):

    * still awaiting a thumbnail → three muted dots ("読み込み中" look),
      NO file-type glyph, so a pending tile never masquerades as a
      settled image-less one;
    * settled (no thumbnail source, or the decode failed) → the small,
      dimmed file-type glyph.  Deliberately NOT the big bright OS folder
      icon, which dominated every not-yet-loaded cell.  Folders still
      get their corner badge drawn by the caller on top.
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    base = palette.base().color()
    text = palette.text().color()
    # Nudge the base colour a little toward the text colour so the panel
    # is faintly visible against the viewport background in either theme.
    panel = QColor(
        (base.red() * 5 + text.red()) // 6,
        (base.green() * 5 + text.green()) // 6,
        (base.blue() * 5 + text.blue()) // 6,
    )
    painter.setPen(Qt.NoPen)
    painter.setBrush(panel)
    radius = RADIUS_SM if view_mode == "icon" else 2
    painter.drawRoundedRect(
        QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius
    )
    if tile_awaits_thumb(tile):
        # Pending: three muted dots instead of a glyph, so "loading" and
        # "no image here" read differently at a glance.
        dot_color = palette.mid().color()
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot_color)
        r = max(1, min(3, rect.height() // 24 + 1))
        gap = r * 4
        cy = rect.center().y()
        cx = rect.center().x()
        painter.setRenderHint(QPainter.Antialiasing, True)
        for dx in (-gap, 0, gap):
            painter.drawEllipse(QPoint(cx + dx, cy), r, r)
        painter.restore()
        return
    glyph = placeholder_glyph(
        tile, rect, view_mode=view_mode, dpr=dpr, qstyle=qstyle, cache=cache,
    )
    if glyph is not None and not glyph.isNull():
        dis = glyph.deviceIndependentSize()
        gw = dis.width() or glyph.width()
        gh = dis.height() or glyph.height()
        # Defensive: never let the glyph spill past the panel.
        avail = max(1, min(rect.width(), rect.height()) - 2)
        longest = max(gw, gh)
        if longest > avail:
            s = avail / longest
            gw *= s
            gh *= s
        tx = rect.x() + (rect.width() - gw) / 2.0
        ty = rect.y() + (rect.height() - gh) / 2.0
        painter.setOpacity(0.5)
        painter.drawPixmap(QRectF(tx, ty, gw, gh), glyph,
                           QRectF(0, 0, glyph.width(), glyph.height()))
    painter.restore()


def placeholder_glyph(
    tile: Tile, rect: QRect, *,
    view_mode: str, dpr: float, qstyle, cache: dict,
) -> QPixmap | None:
    bucket = file_icon_bucket(tile.path, tile.is_dir)
    edge = max(1, min(rect.width(), rect.height()))
    if view_mode == "list":
        # Scale to the row icon area, but stay modest: small rows must fit
        # (flooring at a fixed 16 px used to overflow short rows at the
        # minimum thumb size) and tall rows must not balloon into a giant
        # OS folder icon (capped like icon mode).
        target = min(48, max(4, edge - 2))
    else:
        # Icon mode: a modest, dimmed glyph centred in the large panel —
        # deliberately NOT a giant OS folder icon.
        target = max(16, min(48, int(edge * 0.4)))
    # Never exceed the available square, then bucket to a few sizes so the
    # pixmap cache stays tiny.
    target = max(4, (min(target, edge) // 4) * 4)
    dpr = dpr or 1.0
    cache_key = (bucket, target, round(dpr * 4))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    phys = max(1, int(round(target * dpr)))
    if bucket == BUCKET_DIR:
        # Folders use the design-system muted folder glyph (common/ui/
        # icons.py SVG) instead of QStyle's blue 3-D ``SP_DirIcon``, which
        # clashed with the flat token palette and read as an OS chrome
        # element in the grid.  It retints on
        # theme switch because the view clears its ``placeholder_cache``
        # on Palette/Style/Theme changes.  QSvgRenderer (not QImageReader)
        # backs it, so the factory-lock contention that guards the QStyle
        # path below doesn't apply here.
        # Ask for the LOGICAL size *at this dpr*: ``icons.icon`` registers
        # a 1x and a 2x pixmap (both ``target`` logical px), so the engine
        # can hand back the hi-DPI one.  ``pixmap(target, target)`` asked
        # for a dpr-1.0 raster — ``target`` PHYSICAL px — which the shared
        # ``setDevicePixelRatio(dpr)`` below then shrank to ``target/dpr``
        # logical: on a 200% display the folder glyph came out half the
        # size of the file glyph (QStyle branch, which asks in physical
        # px).
        pm = icon("folder", role="muted", size=target).pixmap(
            QSize(target, target), dpr,
        )
    else:
        # QStyle standard icons realise their pixmaps by decoding the
        # style's embedded PNG resources through QImageReader, i.e. the same
        # Qt image-plugin factory the worker-side fallback decoders enter.
        # This runs inside paintEvent on the GUI thread, so without the
        # shared lock a first-paint cache miss can contend the factory lock
        # against a concurrent worker decode and freeze the process
        # permanently (the same crossed GIL/factory-lock order as workaround
        # (3) — see qimage_decode._QT_READER_LOCK).  Cache hits above never
        # get here.
        with _QT_READER_LOCK:
            style_icon = qstyle.standardIcon(BUCKET_TO_STYLE[bucket])
            pm = style_icon.pixmap(QSize(phys, phys))
    if not pm.isNull():
        # Standard icons have discrete native sizes (16/32/48/…) and the
        # engine hands back its *smallest available* pixmap when we ask for
        # something smaller — i.e. a pixmap larger than `phys`.  Drawing it
        # at its own size overflowed the row (the glyph spilled past the
        # correctly-sized panel).  Scale any oversize result down so the
        # logical size is exactly `target`.
        if pm.width() > phys or pm.height() > phys:
            pm = pm.scaled(phys, phys, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
    cache[cache_key] = pm
    return pm


__all__ = ["BUCKET_TO_STYLE", "paint_placeholder", "placeholder_glyph"]
