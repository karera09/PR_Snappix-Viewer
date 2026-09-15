"""タイルのキャプション整形（純テキスト）と描画。

「幅と文字列 → 描く行」だけを決める層。ウィジェットを持たず、フォント /
計量器 / 省略キャッシュを引数で受け取るので、行分割の規則
（:func:`title_rows` — 最終行だけバッジ行へ幅を譲る、明示改行は行ごとに省略、
入らない副題は丸ごと落とす）はビュー無しで表明できる。

キャッシュは呼び出し側（ビュー）が持つ 1 個の ``dict`` で、フォント / スタイル
変更時にビューがまとめて捨てる — 計量器が変われば行も変わるため。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPalette

from .tiles import Tile

#: Elide-cache size bound — cleared wholesale rather than LRU-evicted
#: (captions and widths are stable within a layout, so overflow is rare).
ELIDE_CACHE_MAX = 4096


def elide_two_lines(font, fm, text: str, width: int, *, cache: dict) -> str:
    """Exact 2-line cap with an ellipsis on overflow, ``\\n``-joined.

    Thin wrapper over :func:`title_rows` with one width for both rows —
    the below-image caption seat has no badge row to yield to.  Kept as
    the single entry point for callers that draw the whole caption with
    one ``drawText(..., TextWordWrap)``.
    """
    return "\n".join(title_rows(font, fm, text, width, width, cache=cache))


def title_rows(
    font, fm, text: str, first_w: int, last_w: int, *, cache: dict,
) -> tuple[str, ...]:
    """Split *text* into at most two drawable rows, top → bottom.

    Replaces the old average-character-width heuristic, which mixed
    CJK/ASCII captions threw far off (over-estimates spilled a clipped
    third line; under-estimates elided half of line two away).

    * ``first_w`` is the width available to every row EXCEPT the last;
      ``last_w`` (≤ ``first_w``) is the last row's, trimmed clear of the
      bottom-right badge seat.  They are equal for the below-image caption.
    * Captions with an explicit ``\\n`` (title + subtitle) elide each
      line independently, so the subtitle (date / size) stays visible
      even under a long title.  When the badges have narrowed the bottom
      row and the subtitle no longer fits there, the subtitle is **dropped
      whole** rather than shown truncated (a half-printed date is noise) —
      the row is returned empty so the title keeps the full-width row above
      the badges.
    * Single-paragraph captions are shaped with ``QTextLayout`` using the
      same word-wrap rules ``drawText`` applies, so the first line
      breaks exactly where the renderer will break it; the remainder is
      elided into line two.

    This runs on the paint hot path, so results are memoised per
    ``(text, first_w, last_w)`` (cleared on font / style changes).
    """
    if last_w <= 0:
        return (text,)
    if "\n" not in text and fm.horizontalAdvance(text) <= last_w:
        return (text,)
    key = (text, first_w, last_w)
    cached = cache.get(key)
    if cached is not None:
        return cached
    first, sep, rest = text.partition("\n")
    if sep:
        head = fm.elidedText(first, Qt.ElideRight, first_w)
        tail = rest.replace("\n", " ")
        if last_w < first_w and fm.horizontalAdvance(tail) > last_w:
            result: tuple[str, ...] = (head, "")
        else:
            result = (head, fm.elidedText(tail, Qt.ElideRight, last_w))
    else:
        result = shape_two_lines(font, fm, text, first_w, last_w)
    if len(cache) >= ELIDE_CACHE_MAX:
        cache.clear()
    cache[key] = result
    return result


def shape_two_lines(
    font, fm, text: str, first_w: int, last_w: int,
) -> tuple[str, ...]:
    """Break *text* into (line 1, elided line 2) via ``QTextLayout``."""
    from PySide6.QtGui import QTextLayout, QTextOption

    tl = QTextLayout(text, font)
    opt = QTextOption()
    # Matches Qt.TextWordWrap used by drawText (word boundaries only; an
    # over-long single word overflows rather than breaking mid-word).
    opt.setWrapMode(QTextOption.WordWrap)
    tl.setTextOption(opt)
    tl.beginLayout()
    line1 = tl.createLine()
    line1.setLineWidth(first_w)
    n1 = line1.textLength()
    line2 = tl.createLine()
    if line2.isValid():
        line2.setLineWidth(last_w)
        n2 = line2.textLength()
    else:
        n2 = 0
    tl.endLayout()
    # ``QTextLine`` が返す長さは UTF-16 コード単位で、Python 文字列の添字
    # （コードポイント）とは単位が違う。非 BMP 文字（絵文字）が 1 つ入る
    # だけで切り出しが後ろへずれ、1 行目が座席幅を超えて省略記号なしに
    # 欠ける。UTF-16 のまま切ってから戻す。
    # 変換は両方向とも ``surrogatepass``: Windows の ``os.scandir`` は不正な
    # 名前を孤立サロゲートのまま復号して返すので、既定のエラーハンドラでは
    # そのファイルのタイルを描く最中に ``UnicodeEncodeError`` が
    # ``paintEvent`` を貫通する（描画ループが途中で止まり QPainter が
    # active のまま残る）。孤立サロゲートは QString がそのまま持てる値で、
    # ``drawText`` は豆腐として描ける。
    u16 = text.encode("utf-16-le", "surrogatepass")
    u16_len = len(u16) // 2

    def _head(units: int) -> str:
        return u16[: max(0, units) * 2].decode("utf-16-le", "surrogatepass")

    def _tail(units: int) -> str:
        return u16[max(0, units) * 2:].decode("utf-16-le", "surrogatepass")

    if n1 >= u16_len:
        if fm.horizontalAdvance(text) > first_w:
            # A single unbreakable run wider than the widest row (WordWrap
            # never splits mid-word) — elide it instead of overflowing.
            return (fm.elidedText(text, Qt.ElideRight, first_w),)
        # The whole caption shaped onto line 1.  It got here because it
        # does NOT fit ``last_w``, so keep it on the wide row and leave the
        # narrow bottom row (the badges' row) empty rather than eliding it.
        return (text, "")
    head = _head(n1).rstrip()
    rest = _tail(n1)
    if n1 + n2 >= u16_len:
        # Fits in exactly two lines — return them as two rows.  Returning
        # the un-split original here (the old early return) made the caller
        # count one line and reserve a one-line rect, so the second line
        # was clipped away with no ellipsis to show for it (N-19).
        return (head, rest)
    # Insert an explicit break at the shaped line-1 boundary so the caller
    # reproduces it, then elide the remainder into a single second line.
    return (head, fm.elidedText(rest, Qt.ElideRight, last_w))


def paint_caption(
    painter, rect: QRect, tile: Tile, selected_active: bool, align: int,
    *, palette: QPalette, view_mode: str, cache: dict,
    selected: bool | None = None,
) -> None:
    """Draw *tile*'s caption into *rect*.

    2 つの選択フラグを取るのは、色の決定が**フォーカスの有無に依らない
    「選択されているか」**と**フォーカスを持つ席かどうか**の 2 段だから:

    * *selected* — この席の現在の選択行か（フォーカス非依存）。
    * *selected_active* — かつフォーカスがこの席にあるか。

    選択判定を ``dimmed`` より**前**に置くのが要点。非フォーカスの選択行は
    ``highlightedText`` ではなく通常 ``text`` で描く（薄い塗りの上では
    白系が読めない — UIレビュー 07-25 #47）が、以前はその分岐が
    ``selected_active`` だけを見ていたため、post.md 等の淡色行は
    「選択しているのに Disabled 文字色のまま」になっていた。
    """
    if rect.width() <= 0 or rect.height() <= 0 or not tile.caption:
        return
    if selected is None:
        selected = selected_active
    painter.save()
    if selected:
        color = (
            palette.highlightedText().color()
            if selected_active and view_mode == "list"
            else palette.text().color()
        )
    elif tile.dimmed:
        # 内部/メタファイル（post.md）は本編と同格に見せない — 無効文字色
        # トークン（QPalette の Disabled ロール）で 1 段落とす
        # (UIレビュー 07-25 #52)。
        color = palette.color(QPalette.Disabled, QPalette.Text)
    else:
        color = palette.text().color()
    painter.setPen(color)
    fm = painter.fontMetrics()
    # Word-wrap to at most two lines for icon mode; single line for list.
    if view_mode == "icon":
        elided = elide_two_lines(
            painter.font(), fm, tile.caption, rect.width(), cache=cache
        )
        painter.drawText(rect, align | int(Qt.TextWordWrap), elided)
    else:
        painter.drawText(rect, align,
                         fm.elidedText(tile.caption, Qt.ElideRight, rect.width()))
    painter.restore()


__all__ = [
    "ELIDE_CACHE_MAX",
    "elide_two_lines",
    "paint_caption",
    "shape_two_lines",
    "title_rows",
]
