"""空状態カード（0 タイル時の見出し + 本文 + アイコン + ボタン列）の計測と描画。

`EmptyStateCard` と同じ文字階層を ``GalleryView`` の手描き席にも作る層。
見出しと本文でフォントが違うので、**計測は 1 か所**(:func:`measure`)に寄せ、
描画 (:func:`paint`) とボタン配置 (:func:`plan_button_row`) の両方がその同じ数を
読む — 別々に測ると座席が見出しぶんズレてボタンが文字に重なる。

ウィジェット（``QPushButton``）の生成・表示はビューが持ち、ここは「どこに
置くか / 入るか」だけを返す。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPalette

from ...common.ui import FONT_TITLE_PT, icon

# Empty-state card regularisation (redesign 2026-07 Phase 3-4): glyph size +
# gap above the settled-empty message.  Kept modest (rather than the spec's
# full 32-48px range's top end) so the icon reads as a quiet accent, not a
# dominant graphic, in an otherwise small/typical pane.
EMPTY_ICON_SIZE = 32
EMPTY_ICON_GAP = 10
#: 空状態の見出し行と本文ブロックの間隔（px）。
EMPTY_HEADING_GAP = 4
#: 主 / 副ボタンの間隔（px — UIレビュー 07-25 #26 の 2 ボタン化）。
EMPTY_BUTTON_GAP = 8


@dataclass(frozen=True)
class EmptyTextLayout:
    """空状態テキストの計測結果（:func:`measure` の戻り）。

    見出しと本文でフォントが違うため、描画・テキスト矩形・アクションボタンの
    座席が**同じ 1 回の計測**を読むようにするための値オブジェクト。
    """

    #: 見出し行（改行の無い 1 行メッセージでは ``""``）。
    heading: str
    #: 本文（見出しの後ろ全部。1 行メッセージではメッセージ全体）。
    body: str
    #: 余白を引いたビューポート矩形。
    outer: QRect
    #: 見出しの描画矩形（見出し無しのときは高さ 0 のブロック先頭）。
    heading_rect: QRect
    #: 本文の描画矩形（下端は ``outer`` の下端まで — 折返しの余地）。
    body_rect: QRect
    #: 文字が実際に占める下端 y（アクションボタンはこの下に置く）。
    bottom: int


def split_blocks(message: str) -> tuple[str, str]:
    """``(見出し, 本文)`` — 最初の改行で割る。

    空状態の文言は 1 行目が見出し・以降が説明という書式で書かれている
    （``welcome_heading`` + ``welcome_body`` を ``"\\n".join`` した形）。
    改行の無い 1 行メッセージ（「読み込み中…」等）は見出しを持たず、
    従来どおり本文 1 色で描く。
    """
    heading, sep, body = message.partition("\n")
    if not sep:
        return "", heading
    return heading, body


def heading_font(base: QFont) -> QFont:
    """空状態の見出しフォント（タイトルサイズ + 太字。直書き禁止の規約）。"""
    font = QFont(base)
    font.setPointSize(FONT_TITLE_PT)
    font.setBold(True)
    return font


def measure(
    viewport_rect: QRect,
    message: str,
    *,
    heading_fm: QFontMetrics,
    body_fm: QFontMetrics,
    has_icon: bool,
) -> EmptyTextLayout:
    """空状態テキストの唯一の計測点（描画・座席計算が同じ数を読む）。"""
    outer = viewport_rect.adjusted(24, 24, -24, -24)
    heading, body = split_blocks(message)
    flags = int(Qt.AlignHCenter | Qt.TextWordWrap)
    head_h = 0
    if heading:
        head_h = heading_fm.boundingRect(outer, flags, heading).height()
    body_h = 0
    if body:
        body_h = body_fm.boundingRect(outer, flags, body).height()
    gap = EMPTY_HEADING_GAP if (heading and body) else 0
    icon_h = EMPTY_ICON_SIZE + EMPTY_ICON_GAP if has_icon else 0
    total_h = icon_h + head_h + gap + body_h
    top = max(outer.top(), outer.center().y() - total_h // 2)
    text_top = top + icon_h
    body_top = text_top + head_h + gap
    return EmptyTextLayout(
        heading=heading,
        body=body,
        outer=outer,
        heading_rect=QRect(outer.left(), text_top, outer.width(), head_h),
        body_rect=QRect(
            outer.left(), body_top, outer.width(),
            max(0, outer.bottom() - body_top),
        ),
        bottom=body_top + body_h,
    )


def paint(
    painter,
    layout: EmptyTextLayout,
    *,
    palette: QPalette,
    head_font: QFont,
    body_font: QFont,
    icon_name: str,
) -> None:
    """Paint the settled empty-state icon + heading + body (#5 / redesign #3-4).

    1 行目は見出し（``FONT_TITLE_PT`` + 太字 + ``palette(text)``）、残りは本文
    （既定サイズ + ``palette(mid)`` = ``text_muted`` トークン）。改行の無い
    1 行メッセージは従来どおり本文 1 色。An optional small muted glyph sits
    above the block, following the unified empty-state card grammar.
    """
    outer = layout.outer
    if outer.width() <= 0 or outer.height() <= 0:
        return
    painter.save()
    if icon_name:
        pixmap = icon(icon_name, role="muted", size=EMPTY_ICON_SIZE).pixmap(
            EMPTY_ICON_SIZE, EMPTY_ICON_SIZE
        )
        icon_x = outer.center().x() - EMPTY_ICON_SIZE // 2
        icon_y = layout.heading_rect.top() - EMPTY_ICON_SIZE - EMPTY_ICON_GAP
        painter.drawPixmap(icon_x, icon_y, pixmap)
    flags = int(Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap)
    if layout.heading:
        painter.setFont(head_font)
        painter.setPen(palette.text().color())
        painter.drawText(layout.heading_rect, flags, layout.heading)
        painter.setFont(body_font)
    if layout.body:
        painter.setPen(palette.mid().color())
        painter.drawText(layout.body_rect, flags, layout.body)
    painter.restore()


def plan_button_row(
    sizes: Sequence[tuple[int, int]], viewport_rect: QRect, top: int,
) -> list[QPoint] | None:
    """Centre the action button(s) just below the painted message block.

    2 つまでは 1 行に並べて中央寄せする（主が左 — UIレビュー 07-25 #26）。
    3 つ以上は縦積み（AI 検索の 0 件カードは効いている軸ごとに 1 ボタンで、
    横並びではビューポート幅に収まらない）。

    低いビューポートでボタンを下端へ押し上げると、動かないテキストブロック
    （見出し・本文）の上に載って文字が読めなくなる。文字より上へは決して
    上げず、入らない高さでは ``None`` を返す（= 出さない）。席が広がれば
    ``resizeEvent`` からここへ戻って復帰する。
    """
    count = len(sizes)
    if count == 0:
        return None
    total_w = sum(w for w, _h in sizes) + EMPTY_BUTTON_GAP * (count - 1)
    row_h = max(h for _w, h in sizes)
    # 3 つ以上、または 1 行に収まらない幅（席を細く詰めたとき）は縦積み —
    # 横並びのまま行頭だけを 0 でクランプすると 2 つ目が 1 つ目に重なる。
    stacked = count > 2 or total_w > viewport_rect.width()
    lines = count if stacked else 1
    block_h = lines * row_h + (lines - 1) * EMPTY_BUTTON_GAP
    if top + block_h + 8 > viewport_rect.bottom():
        return None
    points: list[QPoint] = []
    if stacked:
        for index, (w, _h) in enumerate(sizes):
            x = viewport_rect.center().x() - w // 2
            points.append(
                QPoint(max(0, x), top + index * (row_h + EMPTY_BUTTON_GAP))
            )
    else:
        x = viewport_rect.center().x() - total_w // 2
        for w, _h in sizes:
            points.append(QPoint(max(0, x), top))
            x += w + EMPTY_BUTTON_GAP
    return points


__all__ = [
    "EMPTY_BUTTON_GAP",
    "EMPTY_HEADING_GAP",
    "EMPTY_ICON_GAP",
    "EMPTY_ICON_SIZE",
    "EmptyTextLayout",
    "heading_font",
    "measure",
    "paint",
    "plan_button_row",
    "split_blocks",
]
