"""``GalleryView`` のタイル描画（1 枚ぶんの矩形と状態の値 → ``QPainter``）。

この層はウィジェットを持たない。描画に要るウィジェット由来の値（パレット・
フォント・計量器・表示形式・選択 / ホバーの添字・キュレーション引き・各種
キャッシュ）は :class:`TileStyle` に 1 フレームぶんスナップショットして渡し、
関数は「そのタイルの箱と状態」だけを見て描く。

* 描画は **GUI スレッド限定**（``QPixmap`` を触る）。呼び出し元は
  ``paintEvent`` のみで、ワーカーからは決して呼ばない。
* 席（バッジ行・◇ ボタン・タイトル帯）の幾何は :func:`icon_overlay_seats` と
  :func:`similar_seat` が唯一の真実源で、当たり判定側（ビュー）も同じ関数を
  通る — 描かれていないボタンが押せる / 押せないボタンが描かれる、という
  対実装の片側欠落を構造的に封じるため。
* 画像が実際に落ちる矩形は :func:`drawn_image_rect` 1 本。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QStyle

from ...common.ui import RADIUS_SM, current_tokens, overlay
from .._indicator import (
    _BADGE_MARGIN,
    _BADGE_SIZE,
    badge_corner_layout,
    badge_row_layout,
    badge_spec,
    badge_trail_layout,
    draw_badge_row,
    paint_file_badge_icon_mode,
    paint_folder_background_list_mode,
    paint_folder_badge_icon_mode,
    paint_similar_button,
    surface_style,
)
from ..folder_scan import IMAGE_SUFFIXES
from .captions import paint_caption, title_rows
from .placeholders import paint_placeholder
from .tiles import IconSeats, Tile, is_image_tile

FALLBACK_BORDER_THICKNESS = 2

# Spinner overlay drawn ON TOP OF thumbnail content.  Fixed dark-scrim +
# white colours are the registered design-system exception (image-anchored
# overlays): overlays anchored to image content must stay
# legible on arbitrary photos, so they deliberately do NOT follow the
# theme tokens.  Values come from common/ui/overlay.py (the single overlay
# palette), same source as the badge/chip colours in _indicator.py.
SPINNER_SCRIM = overlay.SPINNER_SCRIM
SPINNER_TRACK = overlay.SPINNER_TRACK
SPINNER_ARC = overlay.SPINNER_ARC

# Bottom title-band gradient scrim.  Transparent
# at the top fading to a near-opaque dark at the image's bottom edge so the
# on-image title stays legible over any photo.  Fixed dark + white text is the
# registered design-system exception (image-anchored overlays), same rationale
# as the badge / spinner overlays above.
SCRIM_TOP = overlay.TILE_SCRIM_TOP
SCRIM_MID = overlay.TILE_SCRIM_MID
SCRIM_BOTTOM = overlay.TILE_SCRIM_BOTTOM
TITLE_TEXT = overlay.OVERLAY_TEXT_STRONG
TITLE_TEXT_DIM = overlay.OVERLAY_TEXT_DIM
# Run-up (px) the scrim gains ABOVE the text band so the gradient has room to
# reach SCRIM_MID before the first caption line starts.  With a plain 2-stop
# ramp over the text band alone the
# first line only sees α20–100 = 2.7–6.2:1 on bright thumbnails; the band
# is grown by this much and the mid stop pinned at ``SCRIM_RAMP / band_h``,
# so text starts at α≥140 (≈4.7:1 against white) instead.
SCRIM_RAMP = 18
# Title band paddings (px) inside the image, and the gap kept between the
# title's right edge and the badge row's left edge.
BAND_PAD_X = 6
BAND_PAD_TOP = 4
BAND_PAD_BOTTOM = 3
BAND_BADGE_GAP = 6
# Extra px the scrim extends above the badge-row top so the badges never sit on
# the hard gradient edge.
ROW_SCRIM_PAD = 6
# Minimum usable title width (px) — below this the badges own the whole bottom
# and the title is dropped rather than overlapped.
MIN_TITLE_W = 24
# 極細タイル対策 — justified はタイル幅に下限を持たないので、縦長画像
# (webtoon 等の 1:10〜1:30)は数px〜20px 幅のスリバーになり得る。その幅では
# 標準座席が全滅する(タイトルは MIN_TITLE_W ガードで落ち、種別バッジ・◇ は
# 箱からはみ出す)ため:
# * 種別バッジはチップが収まらない幅では描かない(下の MIN_BADGE_TILE_W)。
# * タイトルは標準パディングで最低幅が取れないとき、バッジ行を諦めた全幅の
#   下帯キャプション座席(縮小パディング NARROW_PAD_X)へフォールバックする。
MIN_BADGE_TILE_W = _BADGE_SIZE + 2 * _BADGE_MARGIN
NARROW_PAD_X = 1

#: Similarity-overlay button geometry (image-area corner, item 2-4).
#: 24px = the WCAG 2.5.8 minimum target size; the
#: seat and the hit test both read this constant, so the click area follows.
SIMILAR_BTN_SIZE = 24
SIMILAR_BTN_MARGIN = 4

#: 非フォーカス時に選択強調へ掛ける不透明度の係数。
#: 色そのものは既存トークン（QPalette の highlight = accent）のままで、
#: **弱めるのは alpha だけ** — QAbstractItemView の非アクティブ選択と同じ
#: 見え方を、色のハードコードなしで得るための唯一の定数。
UNFOCUSED_SELECTION_ALPHA = 0.42


def selection_color(
    palette: QPalette, active: bool, alpha: int = 255,
) -> QColor:
    """選択強調色（palette の highlight）を *alpha* で返す。

    非フォーカス時は :data:`UNFOCUSED_SELECTION_ALPHA` を掛けて減光する
    （色は変えない — トークンは 1 つのまま）。
    """
    color = QColor(palette.highlight().color())
    if not active:
        alpha = int(round(alpha * UNFOCUSED_SELECTION_ALPHA))
    color.setAlpha(max(0, min(255, alpha)))
    return color


class ScrimCache:
    """タイトル帯のスクリム勾配の使い回し（高さが変わったときだけ組み直す）。

    1 タイル 1 フレームごとに ``QLinearGradient`` を作ると可視タイル数ぶんの
    確保がフレーム単位で走るので、縦 1 本の勾配を各タイルの帯の下へ平行移動
    して使う。
    """

    def __init__(self) -> None:
        self._gradient: QLinearGradient | None = None
        self._height = -1

    def brush(self, height: int):
        """Return the reused vertical scrim gradient sized to *height* px.

        Three stops: the middle one pins SCRIM_MID at ``SCRIM_RAMP``
        px from the top — the run-up :func:`icon_overlay_seats` adds above the
        text band — so the first caption line starts on α≥140 instead of the
        α20–100 a plain 2-stop ramp left it on.
        """
        if self._gradient is None or self._height != height:
            grad = QLinearGradient(0, 0, 0, max(1, height))
            grad.setColorAt(0.0, SCRIM_TOP)
            # Clamped so a band shorter than the run-up (tiny tiles) still has
            # a strictly increasing stop sequence.
            grad.setColorAt(min(0.9, SCRIM_RAMP / max(1, height)), SCRIM_MID)
            grad.setColorAt(1.0, SCRIM_BOTTOM)
            self._gradient = grad
            self._height = height
        return self._gradient


@dataclass(frozen=True)
class TileStyle:
    """1 フレームぶんの描画環境スナップショット（ウィジェット非依存）。

    ``paintEvent`` が入口で 1 回組み、可視タイル全部の描画がこの同じ値を読む。
    ウィジェットの getter をタイルごとに叩かないので、「同じフレーム内で
    パレットとフォントが食い違う」経路が構造的に無くなる。
    """

    palette: QPalette
    font: QFont
    fm: QFontMetrics
    badge_font: QFont
    badge_fm: QFontMetrics
    view_mode: str
    caption_height: int
    caption_band: bool = False
    show_favorites: bool = False
    selection_active: bool = True
    selected_index: int = -1
    hover_index: int | None = None
    similar_overlay_index: int | None = None
    spinner_check: Callable[[Path], bool] | None = None
    spinner_angle: int = 0
    #: ``tile -> (star, later)``（ホストの in-memory 辞書引き。paint パスで
    #: sqlite / NAS I/O をしないのが不変条件）。
    curation: Callable[[Tile], tuple[int, bool]] | None = None
    dpr: float = 1.0
    qstyle: QStyle | None = None
    elide_cache: dict = field(default_factory=dict)
    placeholder_cache: dict = field(default_factory=dict)
    scrim: ScrimCache = field(default_factory=ScrimCache)

    def seated(self) -> bool:
        """Whether icon tiles use the on-image seating chart.

        Triggered when the layout reserves NO caption strip in icon mode
        (``caption_height == 0``): the caption then rides the image bottom on a
        gradient scrim and the badges consolidate into the bottom-right seat.
        Only the main post grid opts in (it passes ``caption_height=0``); other
        icon views (folder-preview, the right file list in grid mode) keep the
        legacy below-image caption strip, so this stays a purely additive path.
        """
        return self.view_mode == "icon" and self.caption_height == 0

    def selection_color(self, alpha: int = 255) -> QColor:
        return selection_color(self.palette, self.selection_active, alpha)

    def curation_for(self, tile: Tile) -> tuple[int, bool]:
        """Return ``(star, later)`` for *tile* via the provider (or ``(0, False)``)."""
        if self.curation is None:
            return 0, False
        try:
            star, later = self.curation(tile)
        except Exception:  # pragma: no cover (defensive)
            return 0, False
        return int(star or 0), bool(later)


# ------------------------------------------------------------------ geometry


def drawn_image_rect(rect: QRect, tile: Tile) -> QRect:
    """Rect the tile's pixmap is *actually* painted into within *rect*.

    The single source of truth shared by :func:`paint_thumb` (drawing)
    and the icon-cell overlay seats / badges / warning frame:
    a min-clamped justified box can be wider than the KeepAspectRatio
    image it holds, and seats anchored to the box then land in the blank
    letterbox area instead of on the image.  With no decoded pixmap the
    box itself is returned (the placeholder panel fills it).
    """
    pm = tile.pixmap
    if pm is None or pm.isNull():
        return QRect(rect)
    dis = pm.deviceIndependentSize()
    dw = dis.width() or pm.width()
    dh = dis.height() or pm.height()
    scale = min(rect.width() / dw, rect.height() / dh) if dw and dh else 1.0
    w = dw * scale
    h = dh * scale
    tx = rect.x() + (rect.width() - w) / 2.0
    ty = rect.y() + (rect.height() - h) / 2.0
    return QRectF(tx, ty, w, h).toRect()


def folder_seat(img_rect: QRect) -> QRect:
    """Top-left folder-indicator seat (mirrors paint_folder_badge_icon_mode)."""
    return QRect(
        img_rect.left() + _BADGE_MARGIN, img_rect.top() + _BADGE_MARGIN,
        _BADGE_SIZE, _BADGE_SIZE,
    )


def similar_seat(img_rect: QRect, *, seated: bool) -> QRect:
    """◇ similar-hover button rect within *img_rect*'s coordinate space.

    The seating chart seats it TOP-RIGHT (the badge row owns bottom-right); the
    legacy layout keeps it bottom-right (favorites was bottom-left there).
    Single source of truth for both the painter and the hit test.
    """
    size = SIMILAR_BTN_SIZE
    margin = SIMILAR_BTN_MARGIN
    x = img_rect.right() - size - margin + 1
    if seated:
        y = img_rect.top() + margin
    else:
        y = img_rect.bottom() - size - margin + 1
    return QRect(x, y, size, size)


def similar_seat_fits(img_rect: QRect) -> bool:
    """Whether *img_rect* is wide enough to hold the ◇ button.

    On a sliver tile the fixed-size seat would start left of the tile
    itself — painted into the neighbour (now clipped away) and, worse,
    hit-tested over the inter-tile gutter, firing ``similar_requested``
    from a click on empty space.  Gate BOTH the painter and the hit test
    through this single predicate.
    """
    return img_rect.width() >= (SIMILAR_BTN_SIZE + 2 * SIMILAR_BTN_MARGIN)


def icon_overlay_seats(
    img_rect: QRect, tile: Tile, *, style: TileStyle,
) -> IconSeats:
    """Compute the four-corner seating for one icon tile (pure geometry).

    Returns an :class:`IconSeats` whose ``title`` rect is trimmed clear of
    the bottom-right ``badge_bounds`` so text and badges never overlap — the
    contract the unit tests assert.  Coordinates share ``img_rect``'s space.

    極細タイル: 標準の帯パディングで最低タイトル幅すら確保できない
    幅では標準座席が全滅する(タイトルは丸ごと落ち、バッジは箱からはみ出す)
    ため、バッジ行・種別バッジ・◇ を諦めて**全幅の下帯キャプション座席**
    (縮小パディング)へフォールバックする — レガシー座席が保っていた
    「狭くても省略キャプションは必ず出る」を座席モデルでも守る。
    """
    # 極細タイル判定 — 下帯フォールバック時はバッジ行を組まない。
    narrow = img_rect.width() - 2 * BAND_PAD_X < MIN_TITLE_W

    # --- bottom-right badge row --------------------------------------
    if narrow:
        placements: list = []
        badge_bounds = QRect()
    else:
        fav = None
        if style.show_favorites:
            f = getattr(tile.entry, "favorites", None)
            if isinstance(f, int) and f >= 0:
                fav = f
        rel = getattr(tile.entry, "relevance", None)
        star, later = style.curation_for(tile)
        placements, badge_bounds = badge_row_layout(
            style.badge_fm, img_rect,
            favorites=fav, star=star, later=later, relevance=rel,
        )

    # --- top corners (empty when the seat doesn't fit the width) --
    folder = (
        folder_seat(img_rect)
        if tile.is_dir and img_rect.width() >= MIN_BADGE_TILE_W
        else QRect()
    )
    similar = (
        similar_seat(img_rect, seated=style.seated())
        if similar_seat_fits(img_rect) else QRect()
    )

    # --- bottom title band + scrim -----------------------------------
    scrim = QRect()
    title = QRect()
    title_text = ""
    rows_out: list[tuple[QRect, str]] = []
    pad_x = NARROW_PAD_X if narrow else BAND_PAD_X
    title_left = img_rect.left() + pad_x
    # The LAST caption row's right edge is trimmed to the badge row's left
    # edge so the two are structurally disjoint (spec: 文字幅をバッジ占有幅
    # ぶん詰める).  Earlier rows keep the full band width — the badges only
    # ever occupy the bottom row (_indicator.badge_row_layout seats one chip
    # height above the image's bottom margin), so making every row pay for
    # them would throw away the badge row's width on every line.
    if not badge_bounds.isNull():
        right_limit = badge_bounds.left() - BAND_BADGE_GAP
    else:
        right_limit = img_rect.right() - pad_x
    # When the badges leave no usable strip on a NORMAL-width tile, drop
    # the title entirely rather than crush it into / under the badge row;
    # a narrow tile has no badges to defer to, so any positive width
    # draws the (heavily elided) fallback caption instead of nothing.
    min_title_w = 1 if narrow else MIN_TITLE_W
    if tile.caption and right_limit - title_left >= min_title_w:
        fm = style.fm
        line_h = fm.lineSpacing()
        last_w = right_limit - title_left
        full_w = max(last_w, img_rect.right() - pad_x - title_left)
        rows = title_rows(
            style.font, fm, tile.caption, full_w, last_w,
            cache=style.elide_cache,
        )
        # 高さ軸のガード — 幅の ``narrow`` 判定と対になる片割れ。
        # 単独タイル行は min クランプが緩いので、30:1 の画像は行高
        # 18px まで潰れ得る。2 行キャプション(≈32px)はそのタイルに入らず、
        # 下端から積む ``rows_top`` がタイルの**外**に出て
        # ``paint_icon_cell`` の setClipRect に丸ごと切り落とされる
        # （＝ファイル名が完全に消え、スクリムだけがタイル高の 100% を
        # 覆って画像も黒帯になる）。入る行数まで落とし、1 行も入らない
        # なら帯ごと出さない。
        max_rows = (img_rect.height() - BAND_PAD_BOTTOM) // line_h
        if max_rows <= 0:
            rows = ()
        elif len(rows) > max_rows:
            # 1 行しか入らない: 2 行ぶんの文面を 1 行へ省略し直す（上の行
            # だけ残すと後半が黙って消え、省略記号も出ない）。
            rows = (
                fm.elidedText(
                    tile.caption.replace("\n", " "),
                    Qt.ElideRight,
                    last_w,
                ),
            )
        n_lines = len(rows)
        text_h = n_lines * line_h
        # Lift the rows above the last one clear of the badge seat: the
        # chips are a touch taller than a text line, so a bottom-anchored
        # 2-row band would have row 1 clipping the chip tops by a few px.
        # Shifting the whole band up by that overlap keeps the per-row
        # non-intersection contract exact instead of "nearly".
        rows_top = img_rect.bottom() - BAND_PAD_BOTTOM - text_h + 1
        lift = 0
        if n_lines > 1 and not badge_bounds.isNull():
            lift = max(
                0,
                rows_top + (n_lines - 1) * line_h - badge_bounds.top(),
            )
        rows_top -= lift
        # 帯がタイルからはみ出すことはこの時点で無いはずだが、行の起点も
        # ``band_h`` と同じくタイル内へ丸めておく（クリップで文字が消える
        # 事故は、はみ出しを黙って許す設計から生まれた — #F1D-2）。
        rows_top = max(img_rect.top(), rows_top)
        band_h = text_h + lift + BAND_PAD_TOP + BAND_PAD_BOTTOM
        # The scrim must also back the badge-row seat (spec: スクリムは
        # 右下座席の背面にも届く高さ), so grow it to cover the badges.
        if not badge_bounds.isNull():
            band_h = max(
                band_h, img_rect.bottom() - badge_bounds.top() + ROW_SCRIM_PAD
            )
        # Reserve the gradient's run-up above the text band.
        band_h = min(band_h + SCRIM_RAMP, img_rect.height())
        scrim_top = img_rect.bottom() - band_h + 1
        if rows:
            scrim = QRect(
                img_rect.left(), scrim_top, img_rect.width(), band_h,
            )
        for i, line in enumerate(rows):
            if not line:
                continue  # deliberately empty bottom row (see title_rows)
            row_w = last_w if i == n_lines - 1 else full_w
            rect = QRect(title_left, rows_top + i * line_h, row_w, line_h)
            rows_out.append((rect, line))
            title = rect if title.isNull() else title.united(rect)
        title_text = "\n".join(rows).rstrip("\n")
    return IconSeats(
        folder=folder, similar=similar, badge_bounds=badge_bounds,
        scrim=scrim, title=title, title_text=title_text,
        title_rows=rows_out, badges=placements,
    )


def seated_title_color(tile: Tile):
    """On-image title colour — the seated counterpart of ``dimmed``.

    :func:`.captions.paint_caption` (下帯キャプション / リスト表示) drops
    post.md and ``#thumb#`` markers to the Disabled text role so they don't
    read as 本編.  A second title painter (the seating model) must honour
    the same flag, or the
    demotion silently stops applying on the **main post grid** — the one
    surface the decision was made for.  Palette roles are unusable
    over image content, hence the overlay-palette twin.
    """
    return TITLE_TEXT_DIM if tile.dimmed else TITLE_TEXT


# ------------------------------------------------------------------- icon mode


def paint_icon_cell(painter, box, tile: Tile, scroll_y: int, *, style: TileStyle) -> None:
    img_rect = QRect(box.x, box.y - scroll_y, box.w, box.h)
    cap_h = style.caption_height
    cell_rect = QRect(box.x, box.y - scroll_y, box.w, box.h + cap_h)
    # 極細タイル: バッジ・◇ は固定サイズのオーバーレイなので、狭い
    # 箱ではセル外(=隣タイルの領域)へ描画がはみ出し得る。タイル毎にセル
    # 矩形へクリップして構造的に封じる(誤クリック側は座席のゲートで抑止)。
    # try/finally で restore を保証 — paintEvent はタイル単位で例外を隔離
    # するため、ここで clip を残すと以降のタイル全部が巻き添えになる。
    painter.save()
    painter.setClipRect(cell_rect, Qt.IntersectClip)
    try:
        paint_icon_cell_body(
            painter, box, tile, img_rect, cell_rect, cap_h, style=style,
        )
    finally:
        painter.restore()


def paint_icon_cell_body(
    painter, box, tile: Tile,
    img_rect: QRect, cell_rect: QRect, cap_h: int, *, style: TileStyle,
) -> None:
    selected = (box.index == style.selected_index)

    if selected:
        painter.save()
        painter.setPen(Qt.NoPen)
        # 非フォーカス時は減光（``selection_color``）。
        painter.setBrush(style.selection_color(60))
        painter.drawRoundedRect(
            cell_rect.adjusted(0, 0, -1, -1), RADIUS_SM, RADIUS_SM
        )
        painter.restore()
    elif box.index == style.hover_index:
        # Subtle neutral wash (text colour at ~7% alpha works on both
        # themes) so the tile under the cursor reads as interactive.
        painter.save()
        painter.setPen(Qt.NoPen)
        hover = style.palette.text().color()
        hover.setAlpha(18)
        painter.setBrush(hover)
        painter.drawRoundedRect(
            cell_rect.adjusted(0, 0, -1, -1), RADIUS_SM, RADIUS_SM
        )
        painter.restore()

    paint_thumb(painter, img_rect, tile, style=style)

    # Overlays anchor to the rect the image is ACTUALLY drawn into:
    # a min-clamped justified box can be wider than its KeepAspectRatio
    # image, and box-anchored seats would float in the letterbox blank.
    drawn = drawn_image_rect(img_rect, tile)

    if tile.is_fallback:
        paint_fallback_border(painter, drawn)
    # Top-left seat: entry-type indicator (both seating models).  Skipped
    # when the chip doesn't fit the tile width — a clipped sliver
    # of a badge reads as garbage, not as a type cue.
    if drawn.width() >= MIN_BADGE_TILE_W:
        if tile.warn:
            # 到達不能な席では種別（フォルダ / ファイル）は綴りからの推定に
            # すぎないので、左上の座席は警告図像に譲る。
            paint_warn_badge(painter, drawn, tile.warn)
        elif tile.is_dir:
            paint_folder_badge_icon_mode(painter, drawn)
        elif style.seated():
            # ライブラリルート直下では裸ファイルのタイルと
            # フォルダのタイルが同じ「写真カード」に見え、区別はフォルダ側の
            # 図像の**有無**（= 無標）だけになる。ファイル側にも控えめな種別
            # 図像を置いて、種別が常に有標になるようにする。
            # 座席モデル（メインの投稿グリッド）限定 — レガシー座席では★が
            # この左上に来るため、ここに置くと衝突する。
            paint_file_badge_icon_mode(painter, drawn)

    if style.seated():
        paint_icon_seated(painter, drawn, box.index, tile, style=style)
    else:
        paint_icon_badges_legacy(painter, drawn, box.index, tile, style=style)
        # Caption strip below the image (legacy layout — two lines max).
        # In 「画像の下に表示」 name-placement mode the strip is backed by a
        # surface-token band so the label reads off the busy photo content.
        if style.caption_band and tile.caption:
            paint_caption_band(painter, box, img_rect, cap_h, style=style)
        cap_rect = QRect(box.x + 2, img_rect.bottom() + 1, box.w - 4, cap_h - 2)
        paint_caption(
            painter, cap_rect, tile, selected,
            int(Qt.AlignHCenter | Qt.AlignTop),
            palette=style.palette, view_mode=style.view_mode,
            cache=style.elide_cache,
        )

    if selected:
        painter.save()
        pen = QPen(style.selection_color())
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            cell_rect.adjusted(1, 1, -2, -2), RADIUS_SM, RADIUS_SM
        )
        painter.restore()


def paint_icon_badges_legacy(
    painter, img_rect: QRect, index: int, tile: Tile, *, style: TileStyle,
) -> None:
    """Legacy overlay layer: favorites bottom-left, relevance/later
    top-right, star top-left, similar-hover bottom-right.

    席と幅はバッジ語彙レジストリ 1 本（:func:`badge_corner_layout`）を
    通す — 入らないチップはクランプせず落とす（クランプすると
    ``83%`` が ``3%`` に読める別の値になる）。
    """
    painter.save()
    painter.setFont(style.badge_font)
    fm = style.badge_fm
    star, later = style.curation_for(tile)
    rel = getattr(tile.entry, "relevance", None)
    fav = getattr(tile.entry, "favorites", None)
    top_right: list[tuple[str, object]] = []
    if isinstance(rel, (int, float)):
        top_right.append(("relevance", float(rel)))
    if later:
        top_right.append(("later", True))
    seats = badge_corner_layout(fm, img_rect, "topright", top_right)
    if star > 0:
        seats += badge_corner_layout(
            fm, img_rect, "topleft", [("star", star)],
            # フォルダは左上にフォルダ図像が座っているので 1 段下げる。
            start_row=1 if tile.is_dir else 0,
        )
    if style.show_favorites and isinstance(fav, int) and fav >= 0:
        seats += badge_corner_layout(
            fm, img_rect, "bottomleft", [("favorites", fav)],
        )
    draw_badge_row(painter, fm, seats)
    painter.restore()
    if style.similar_overlay_index == index and is_image_tile(tile):
        paint_similar_overlay(painter, img_rect, seated=style.seated())


def paint_icon_seated(
    painter, img_rect: QRect, index: int, tile: Tile, *, style: TileStyle,
) -> None:
    """Draw the on-image seating overlays (scrim → title → badges → ◇)."""
    seats = icon_overlay_seats(img_rect, tile, style=style)
    # 1) gradient scrim (behind both the title and the badge row).
    if not seats.scrim.isNull():
        painter.save()
        painter.translate(seats.scrim.topLeft())
        painter.fillRect(
            0, 0, seats.scrim.width(), seats.scrim.height(),
            style.scrim.brush(seats.scrim.height()),
        )
        painter.restore()
    # 2) title text on the scrim — one drawText per seated row, since the
    #    rows can have different widths and are already shaped /
    #    elided by ``title_rows`` (no TextWordWrap re-flow here).
    if seats.title_rows:
        painter.save()
        painter.setPen(seated_title_color(tile))
        for rect, line in seats.title_rows:
            if rect.width() > 0:
                painter.drawText(rect, int(Qt.AlignLeft | Qt.AlignTop), line)
        painter.restore()
    # 3) bottom-right badge row.
    if seats.badges:
        painter.save()
        painter.setFont(style.badge_font)
        draw_badge_row(painter, style.badge_fm, seats.badges)
        painter.restore()
    # 4) top-right similar-hover button.
    if style.similar_overlay_index == index and is_image_tile(tile):
        paint_similar_overlay(painter, img_rect, seated=style.seated())


def paint_warn_badge(painter, img_rect: QRect, kind: str) -> None:
    """左上座席へ警告バッジ（``Tile.warn`` の kind）を描く。

    図像はバッジ語彙レジストリの ``painter_fn`` — 面ごとの手書きグリフを
    作らない（design.md のバッジ語彙レジストリ）。未知の kind は黙って
    描かない（語彙が縮んでもタイル描画は落ちない）。
    """
    try:
        spec = badge_spec(kind)
    except KeyError:  # pragma: no cover (defensive — 未知の kind)
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    spec.painter(painter, QRectF(folder_seat(img_rect)), "", None)
    painter.restore()


# ------------------------------------------------------------------- list mode


def paint_list_row(painter, box, tile: Tile, scroll_y: int, *, style: TileStyle) -> None:
    row_rect = QRect(box.x, box.y - scroll_y, box.w, box.h)
    # 行の中で描いたものが隣の行やキャプションへ出ない（アイコン表示の
    # ``paint_icon_cell`` と同じ規則 — 片側だけ無いとはみ出しが残る）。
    painter.save()
    painter.setClipRect(row_rect, Qt.IntersectClip)
    try:
        paint_list_row_body(painter, box, tile, row_rect, style=style)
    finally:
        painter.restore()


def paint_list_row_body(
    painter, box, tile: Tile, row_rect: QRect, *, style: TileStyle,
) -> None:
    selected = (box.index == style.selected_index)
    # フォーカスを持たないペインの選択行は減光して
    # 描く。塗りが薄くなると highlightedText（白系）が読めなくなるため、
    # 文字色も通常の text へ戻す（= QAbstractItemView の非アクティブ選択）。
    selected_active = selected and style.selection_active
    if tile.is_dir:
        paint_folder_background_list_mode(painter, row_rect, selected)
    if selected:
        painter.save()
        painter.fillRect(row_rect, style.selection_color())
        painter.restore()
    elif box.index == style.hover_index:
        hover = style.palette.text().color()
        hover.setAlpha(18)
        painter.fillRect(row_rect, hover)
    # Icon at the left.  Clamp the square to the row *width* too, so a
    # large thumb size (row height > pane width) can't push the icon
    # past the right edge of a narrow pane.
    pad = 2
    icon_sz = max(1, min(box.h, box.w) - 2 * pad)
    icon_rect = QRect(row_rect.left() + pad, row_rect.top() + pad, icon_sz, icon_sz)
    paint_thumb(painter, icon_rect, tile, style=style)
    if tile.warn:
        # 一覧表示でも到達不能な行を有標にする（アイコン表示と同じ座席規則）。
        paint_warn_badge(painter, icon_rect, tile.warn)
    elif tile.is_dir:
        # The low-alpha row wash alone is nearly invisible over the light
        # theme's white surface, so a folder read identically to a file in
        # list mode.  Draw the same gold folder pictogram used in icon mode
        # on the row's thumbnail — a theme-independent, unmistakable cue.
        paint_folder_badge_icon_mode(painter, icon_rect)
    if tile.is_fallback:
        paint_fallback_border(painter, icon_rect)
    # Text to the right, vertically centered.
    text_rect = QRect(
        icon_rect.right() + 6, row_rect.top(),
        row_rect.right() - icon_rect.right() - 10, row_rect.height(),
    )
    text_rect = paint_list_trail(
        painter, text_rect, tile, selected_active, style=style,
    )
    paint_caption(
        painter, text_rect, tile, selected_active,
        int(Qt.AlignLeft | Qt.AlignVCenter),
        palette=style.palette, view_mode=style.view_mode,
        cache=style.elide_cache, selected=selected,
    )
    if style.spinner_check is not None:
        maybe_paint_spinner(painter, icon_rect, tile, style=style)


def list_trail_kinds(tile: Tile, *, style: TileStyle) -> list[tuple[str, object]]:
    """一覧行の右端席に出す ``(kind, value)`` 列（無ければ空）。

    席順は :func:`badge_trail_layout` が :data:`_indicator.BADGE_ROW_ORDER`
    で決めるので、ここは「値があるか」だけを見る。
    """
    star, later = style.curation_for(tile)
    rel = getattr(tile.entry, "relevance", None)
    kinds: list[tuple[str, object]] = []
    if star > 0:
        kinds.append(("star", star))
    if later:
        kinds.append(("later", True))
    if isinstance(rel, (int, float)):
        kinds.append(("relevance", float(rel)))
    return kinds


def paint_list_trail(
    painter, text_rect: QRect, tile: Tile, selected: bool, *, style: TileStyle,
) -> QRect:
    """一覧行の右端へ印（★ / 時計 / 関連度）を並べ、キャプション席を返す。

    一覧表示の行が画像として持つのは 24px 前後のアイコン枠だけで、そこには
    チップの自然幅が入らない（クランプすると ``83%`` が ``3%`` に読める別の
    値になるので、四隅の席は正しく**落とす**）。落ちたままだと関連度と
    「あとで見る」が一覧表示から丸ごと消え、★だけが出るという非対称に
    なるので、行の右端 = キャプションと分け合う席へ移す。席と幅は
    :func:`badge_trail_layout`（= 他の席と同じ ``_BADGE_SPECS`` の権威）。

    行は画像コンテンツではなく素のペイン面なので、配色は固定スクリムでは
    なく :func:`surface_style`（design.md 使用ルール 2 の例外は「画像の上」
    限定）。選択中の行だけは面の前景が ``highlightedText`` なので、その色を
    渡して行の他の文字と揃える。戻り値は印のぶん縮めたキャプション矩形で、
    名前は印の手前で省略される（構造的に非重複）。
    """
    kinds = list_trail_kinds(tile, style=style)
    if not kinds:
        return text_rect
    painter.save()
    painter.setFont(style.badge_font)
    fm = style.badge_fm
    placements, caption_rect = badge_trail_layout(fm, text_rect, kinds)
    pal = style.palette
    tone = pal.highlightedText().color() if selected else pal.text().color()
    draw_badge_row(painter, fm, placements, surface_style(tone))
    painter.restore()
    return caption_rect


# ----------------------------------------------------------- shared primitives


def paint_thumb(painter, rect: QRect, tile: Tile, *, style: TileStyle) -> None:
    if tile.pixmap is not None and not tile.pixmap.isNull():
        pm = tile.pixmap
        drawn = drawn_image_rect(rect, tile)
        painter.drawPixmap(
            QRectF(drawn), pm,
            QRectF(0, 0, pm.width(), pm.height()),
        )
        if style.view_mode == "icon" and style.spinner_check is not None:
            maybe_paint_spinner(painter, rect, tile, style=style)
        return
    # No thumbnail yet (or a genuinely image-less folder) → subtle panel.
    paint_placeholder(
        painter, rect, tile,
        palette=style.palette, view_mode=style.view_mode, dpr=style.dpr,
        qstyle=style.qstyle, cache=style.placeholder_cache,
    )
    if style.view_mode == "icon" and style.spinner_check is not None:
        maybe_paint_spinner(painter, rect, tile, style=style)


def paint_caption_band(
    painter, box, img_rect: QRect, cap_h: int, *, style: TileStyle,
) -> None:
    """Fill the below-image caption strip with a surface-token plate.

    Themed (``palette(alternate-base)`` = the ``bg_raised`` card token, per
    the design tokens) so it reads against the ``bg_surface`` grid
    viewport and stays legible in every theme — no hardcoded colour.  Drawn
    under the caption text (which keeps the normal ``text`` foreground) so
    the tile becomes an image + label-plate card in 「画像の下に表示」 mode.
    """
    band = QRect(box.x, img_rect.bottom() + 1, box.w, cap_h - 1)
    if band.width() <= 0 or band.height() <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(style.palette.alternateBase())
    painter.drawRoundedRect(
        QRectF(band).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_SM, RADIUS_SM
    )
    painter.restore()


def paint_similar_overlay(painter, img_rect: QRect, *, seated: bool) -> None:
    """Draw the faint 「類似検索」 overlay button in *img_rect*'s corner.

    Content-anchored overlay (like the spinner / badges — the registered
    design-system exception for image-anchored overlays): fixed
    dark scrim + light glyph so it stays legible on any thumbnail, and kept
    translucent so it never masks the image underneath.

    The artwork comes from the badge vocabulary registry
    (``_indicator.paint_similar_button``) — it used to be a ``drawText`` of
    the literal 「◇」 (U+25C7), which carried the same missing-glyph risk
    the ♡ / ★ badges were vectorised to escape.
    """
    if not similar_seat_fits(img_rect):
        return  # sliver tile — matches similar_seat's hit-test gate
    btn = similar_seat(img_rect, seated=seated)
    painter.save()
    painter.setOpacity(0.78)
    paint_similar_button(painter, btn)
    painter.restore()


def paint_fallback_border(painter, rect: QRect) -> None:
    # Fallback representative (the folder only had its #thumb# creator
    # icon while "クリエイターアイコンを隠す" is on).  A thin warning-toned
    # frame — the old thick ``danger`` red read as a broken/error tile
    # (C04); the tile's tooltip explains what the frame means.
    painter.save()
    pen = QPen(QColor(current_tokens().warning))
    pen.setWidth(FALLBACK_BORDER_THICKNESS)
    pen.setJoinStyle(Qt.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    inset = FALLBACK_BORDER_THICKNESS // 2
    painter.drawRect(rect.adjusted(inset, inset, -inset, -inset))
    painter.restore()


def spinner_eligible(tile: Tile, check: Callable[[Path], bool] | None) -> bool:
    """スピナーを回す資格があるタイルか（描画側と休止判定の**共有述語**）。

    **両者の判定は必ず揃えること** — 片側だけ緩いと「描かないタイルの
    ために 80ms タイマが viewport 全体を回し続ける」。``thumb_failed`` で
    決着したタイルは何も進行していないので除く（見なければ「読み込み中が
    永久に消えない」が壊れ画像で再発する）。
    """
    if check is None or tile.is_dir:
        return False
    if tile.path.suffix.lower() not in IMAGE_SUFFIXES:
        return False
    if tile.thumb_failed:
        return False
    try:
        return not check(tile.path)
    except Exception:  # pragma: no cover (defensive)
        return False


def maybe_paint_spinner(painter, rect: QRect, tile: Tile, *, style: TileStyle) -> None:
    if not spinner_eligible(tile, style.spinner_check):
        return
    size = 16
    margin = 3
    badge = QRect(rect.right() - size - margin, rect.top() + margin, size, size)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(SPINNER_SCRIM)
    painter.drawEllipse(badge)
    arc = badge.adjusted(2, 2, -2, -2)
    track = QPen(SPINNER_TRACK)
    track.setWidth(2)
    painter.setPen(track)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(arc)
    arc_pen = QPen(SPINNER_ARC)
    arc_pen.setWidth(2)
    arc_pen.setCapStyle(Qt.RoundCap)
    painter.setPen(arc_pen)
    painter.drawArc(arc, int(-style.spinner_angle * 16), int(110 * 16))
    painter.restore()


__all__ = [
    "BAND_BADGE_GAP",
    "BAND_PAD_BOTTOM",
    "BAND_PAD_TOP",
    "BAND_PAD_X",
    "FALLBACK_BORDER_THICKNESS",
    "MIN_BADGE_TILE_W",
    "MIN_TITLE_W",
    "NARROW_PAD_X",
    "ROW_SCRIM_PAD",
    "SCRIM_RAMP",
    "SIMILAR_BTN_MARGIN",
    "SIMILAR_BTN_SIZE",
    "UNFOCUSED_SELECTION_ALPHA",
    "ScrimCache",
    "TileStyle",
    "drawn_image_rect",
    "folder_seat",
    "icon_overlay_seats",
    "list_trail_kinds",
    "maybe_paint_spinner",
    "paint_caption_band",
    "paint_fallback_border",
    "paint_icon_badges_legacy",
    "paint_icon_cell",
    "paint_icon_cell_body",
    "paint_icon_seated",
    "paint_list_row",
    "paint_list_row_body",
    "paint_list_trail",
    "paint_similar_overlay",
    "paint_thumb",
    "paint_warn_badge",
    "seated_title_color",
    "selection_color",
    "similar_seat",
    "similar_seat_fits",
    "spinner_eligible",
]
