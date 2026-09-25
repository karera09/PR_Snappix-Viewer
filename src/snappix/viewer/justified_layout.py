"""Pure-logic gallery layout engine (no Qt import).

Computes tile geometry for the viewer's image grid in three modes:

* **Justified** (Eagle / Flickr / Google-Photos style): row-based packing
  where every row's tiles share one height and the row is scaled so its
  tiles + gutters fill the viewport width exactly.  File order preserved;
  both horizontal AND vertical packing (no padding gutters around
  non-square thumbnails).  Needs each tile's aspect ratio up front.
* **Square grid**: uniform square cells, columns grown to fill the
  viewport width (matches the legacy ``"square"`` thumb layout).
* **List rows**: one full-width row per tile, fixed height (icon at left,
  text inline) — used by the ``"list"`` view mode.

Everything here is deliberately Qt-free so it can be unit-tested without a
``QApplication`` (mirrors ``folder_scan.py`` / ``post_md.py``).  The
hosting ``GalleryView`` (Qt) feeds in viewport width + slider size and
renders the returned :class:`TileBox` rectangles.

Coordinate system: logical pixels, origin top-left, y grows downward.
A tile's :class:`TileBox` describes only the **image area**; the caption
(when ``caption_height > 0``) occupies ``(x, y + h, w, caption_height)``
directly beneath it.  Hit-testing treats image + caption as one cell.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TileInput:
    """Per-tile layout input.

    ``aspect`` is width / height of the thumbnail.  When ``aspect_known``
    is ``False`` the tile lays out as a 1:1 square placeholder and the
    view re-runs layout once the real aspect arrives from the probe —
    this is the only time a tile ever moves, and only while its row is
    on/near screen.

    ``expandable`` is ``True`` for tiles backed by a real (or to-be-probed)
    image whose aspect is trustworthy, so the row may be stretched to fill
    the width.  ``False`` for fixed-default placeholders (no-thumbnail files
    like ``post.md``, PDFs, videos, empty folders): a row made only of these
    is laid out at ``target_row_height`` instead of ballooning to a giant
    full-width square when few tiles share the row.
    """

    aspect: float
    aspect_known: bool = True
    expandable: bool = True


@dataclass(frozen=True)
class LayoutParams:
    """Dynamic layout parameters supplied per re-layout.

    ``target_size`` is the slider-controlled size: in justified mode it is
    the *ideal* row height, in square mode the minimum cell edge (cells
    grow to fill width), in list mode the row height.
    """

    target_size: int
    spacing: int = 6
    caption_height: int = 0
    margin: int = 6
    # Justified-only clamps so a sparse / extreme-aspect row can't blow up
    # (one tall image becoming 1800 px) or collapse (many tiles below a
    # legible height).  Ignored by square / list strategies.
    min_row_height: int = 1
    max_row_height: int = 100_000
    # When False the final (partial) justified row is left at target_size,
    # left-aligned, rather than stretched to fill the width.
    justify_last_row: bool = False


@dataclass(frozen=True)
class TileBox:
    """Image-area rectangle for one tile (logical px)."""

    index: int
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class RowBand:
    """A laid-out row: y-range (image top → caption bottom) + tile span.

    ``first``/``last`` are inclusive indices into ``LayoutResult.boxes``.
    ``bottom`` excludes the inter-row spacing so a click landing in the
    gutter below a row hit-tests to nothing (matches the old grid).
    """

    top: int
    bottom: int
    first: int
    last: int


@dataclass(frozen=True)
class LayoutResult:
    boxes: list[TileBox] = field(default_factory=list)
    rows: list[RowBand] = field(default_factory=list)
    content_height: int = 0
    # Precomputed binary-search keys for the hot query paths (paint /
    # scroll / hover hit-testing).  Building ``[band.top for band in rows]``
    # per call allocated a thousands-element list on every mouse move for
    # large grids; a layout is immutable once constructed, so the keys are
    # derived exactly once here.
    row_tops: tuple[int, ...] = field(init=False, default=())
    row_firsts: tuple[int, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "row_tops", tuple(band.top for band in self.rows)
        )
        object.__setattr__(
            self, "row_firsts", tuple(band.first for band in self.rows)
        )


# --------------------------------------------------------------------- core


def compute_justified_layout(
    tiles: Sequence[TileInput],
    *,
    viewport_width: int,
    target_row_height: int,
    spacing: int,
    caption_height: int,
    min_row_height: int,
    max_row_height: int,
    margin: int = 6,
    justify_last_row: bool = False,
) -> LayoutResult:
    """Row-based justified layout.

    Greedy with a one-tile lookahead (Flickr / Google-Photos style): for
    every tile we compare the justified height the row would get *without*
    it against the height *with* it, and close the row on whichever lands
    nearer ``target_row_height``.  Closing a row scales its height so the
    tiles + gutters fill ``content_w`` exactly (rounding drift folded into
    the last tile → zero right gutter).  Unknown-aspect tiles contribute a
    1:1 box.  Clamps keep extreme rows sane (see :class:`LayoutParams`).

    The lookahead is what keeps a row's height near the slider value in a
    narrow pane.  The simpler rule — close the row
    *before* the tile that would overflow — can only ever stretch a row,
    so a narrow pane produces runs of single-tile rows pinned at
    ``max_row_height`` with a wide gap on the right.  Measured over 1200
    tiles at a 538px viewport with ``target_row_height=160``: 556 rows /
    105 single-tile rows / 7 rows with a >5% right gap / tallest row 400px
    with that rule, 410 / 0 / 0 / 208 with the lookahead.

    Rows made only of non-``expandable`` placeholders are the exception:
    they are never scaled (see :func:`_should_close_row`), so they keep the
    old "close before the overflow" rule — overflowing one would shave its
    last tile down to a sliver instead of shrinking the row.
    """
    boxes: list[TileBox] = []
    rows: list[RowBand] = []
    if not tiles:
        return LayoutResult([], [], margin)

    content_w = max(1, viewport_width - 2 * margin)
    x0 = margin
    y = margin
    target_row_height = max(1, target_row_height)
    # Constrain the clamps around the target so they can never break the
    # fill-width invariant: a justified row's height is always close to
    # ``target_row_height`` (the lookahead picks the break that lands
    # nearest it), so a ``min`` above target — or a ``max`` below it —
    # would force an out-of-range height and overflow / leave a gutter on
    # rows that should fill exactly.  ``min`` only ever shortens a sparse
    # row; ``max`` only ever caps a sparse/tall one.
    eff_min = max(1, min(min_row_height, target_row_height))
    eff_max = max(max_row_height, target_row_height)

    # Current row accumulator: (tile_index, aspect).
    row: list[tuple[int, float]] = []
    row_natural_w = 0.0  # Σ aspect*target_h + leading gutters
    row_aspect_sum = 0.0
    # Whether the current row has at least one stretchable (real-image) tile.
    # A row of only fixed-default placeholders is NOT justified — it stays at
    # target height so a lone no-thumbnail file doesn't balloon to a giant
    # full-width square.
    row_expandable = False

    def aspect_of(t: TileInput) -> float:
        if t.aspect_known and t.aspect > 0:
            return t.aspect
        return 1.0

    for i, t in enumerate(tiles):
        a = aspect_of(t)
        tile_w = a * target_row_height
        add = tile_w + (spacing if row else 0)
        if row and _should_close_row(
            row_len=len(row),
            row_aspect_sum=row_aspect_sum,
            row_natural_w=row_natural_w,
            candidate_aspect=a,
            candidate_add=add,
            content_w=content_w,
            spacing=spacing,
            target_h=target_row_height,
            min_h=eff_min,
            justified=_row_will_justify(
                row_expandable or t.expandable,
                last=i == len(tiles) - 1,
                justify_last_row=justify_last_row,
            ),
        ):
            y = _emit_justified_row(
                row, content_w, x0, y, target_row_height, spacing,
                caption_height, eff_min, eff_max,
                boxes, rows, justify=row_expandable,
            )
            row = []
            row_natural_w = 0.0
            row_aspect_sum = 0.0
            row_expandable = False
            add = tile_w  # first tile in the fresh row: no leading gutter
        row.append((i, a))
        row_natural_w += add
        row_aspect_sum += a
        row_expandable = row_expandable or t.expandable

    if row:
        y = _emit_justified_row(
            row, content_w, x0, y, target_row_height, spacing,
            caption_height, eff_min, eff_max,
            boxes, rows, justify=(justify_last_row and row_expandable),
        )

    # ``y`` currently points just past the last row's bottom + spacing;
    # trim that trailing spacing and add the bottom margin.
    content_h = (y - spacing) + margin if rows else margin
    return LayoutResult(boxes, rows, max(margin, content_h))


def _row_will_justify(expandable: bool, *, last: bool, justify_last_row: bool) -> bool:
    """行を閉じるかの先読みが前提にしてよい「その行は justify されるか」.

    最終行は既定で justify しない（``justify_last_row`` = False がグリッドの
    既定）。それを justify される前提で詰めると、行が ``content_w`` を超えた
    まま emit され、はみ出しの後始末で箱の縦横比が画像と食い違う。
    """
    if last and not justify_last_row:
        return False
    return expandable


def _apportion_row(aspects: list[float], avail: int) -> list[int]:
    """``avail`` を縦横比に比例して整数幅へ割る（最大剰余法）.

    丸め残差を最後の 1 枚へ全部折り込むと、その 1 枚だけが自分の縦横比から
    最大十数 % 外れて行のリズムから浮く。1px 単位で行内へ配ると、どの箱も
    ずれが 1px 以内に収まり、行の合計は ``avail`` にちょうど一致する。
    """
    total = sum(aspects) or 1.0
    exact = [avail * a / total for a in aspects]
    widths = [max(1, int(w)) for w in exact]
    rest = avail - sum(widths)
    if rest > 0:
        order = sorted(
            range(len(exact)), key=lambda i: exact[i] - int(exact[i]), reverse=True
        )
        for k in range(rest):
            widths[order[k % len(order)]] += 1
    elif rest < 0:
        _trim_to_width(widths, avail)
    return widths


def _trim_to_width(widths: list[int], avail: int) -> None:
    """丸めで残った超過分を広いタイルから 1px ずつ削る（その場で書き換える）."""
    excess = sum(widths) - avail
    if excess <= 0:
        return
    order = sorted(range(len(widths)), key=lambda i: widths[i], reverse=True)
    k = 0
    guard = excess * len(widths) + len(widths)
    while excess > 0 and guard > 0:
        i = order[k % len(order)]
        if widths[i] > 1:
            widths[i] -= 1
            excess -= 1
        k += 1
        guard -= 1


def _should_close_row(
    *,
    row_len: int,
    row_aspect_sum: float,
    row_natural_w: float,
    candidate_aspect: float,
    candidate_add: float,
    content_w: int,
    spacing: int,
    target_h: int,
    min_h: int,
    justified: bool,
) -> bool:
    """Whether the (non-empty) current row closes before ``candidate``.

    ``justified`` says whether the row *including* the candidate would be
    stretched to fill the width (i.e. it holds at least one expandable
    tile).

    * Justified rows: one-tile lookahead — compare the height the row gets
      as it stands with the height it would get after taking the candidate,
      and keep whichever is nearer ``target_h``.  Both a too-tall sparse row
      and a too-short crowded one are therefore avoided, which is the whole
      point of the rule.
    * ...except that a candidate which would drag the row below its floor
      (``max(min_h, target_h / 2)``) is never taken, however the linear
      distance compares.  An extreme panorama (10:1, 100:1 strip) joining
      a row of ordinary tiles otherwise wins on distance — a 3px row is
      "nearer" 120 than a 300px one — and the emit step's min-clamp →
      overflow → shrink then squeezes the *neighbours* to dots.  Closing
      leaves the ordinary tiles on their own (tall, capped by
      ``max_row_height``) and lets the strip thin only itself.  A ratio
      distance (``|log(h/target)|``) was measured to break the narrow-pane
      balance above, so the guard is a floor, not a new metric.  The
      ``target_h / 2`` half keeps hosts that pass no ``min_row_height``
      (default 1) covered too.
    * Placeholder-only rows are never scaled, so a row that overflows would
      be fixed up by shaving its last tile into a sliver (and, at very
      narrow widths, could still overflow ``content_w``).  Those keep the
      "close before the tile that would overflow" rule.
    """
    if not justified:
        return (row_natural_w + candidate_add) > content_w
    height_without = (content_w - (row_len - 1) * spacing) / max(
        row_aspect_sum, 1e-9
    )
    height_with = (content_w - row_len * spacing) / max(
        row_aspect_sum + candidate_aspect, 1e-9
    )
    if height_with < max(min_h, target_h / 2):
        return True
    return abs(height_without - target_h) <= abs(height_with - target_h)


def _emit_justified_row(
    row: list[tuple[int, float]],
    content_w: int,
    x0: int,
    y: int,
    target_h: int,
    spacing: int,
    caption_height: int,
    min_row_height: int,
    max_row_height: int,
    boxes: list[TileBox],
    rows: list[RowBand],
    *,
    justify: bool,
) -> int:
    """Append one row's :class:`TileBox`es + :class:`RowBand`; return next y."""
    n = len(row)
    aspects = [a for _, a in row]
    sum_aspect = sum(aspects) or 1.0
    gutters = (n - 1) * spacing
    avail = content_w - gutters

    if justify and avail > 0:
        ideal_h = avail / sum_aspect
        row_h = int(round(ideal_h))
    else:
        row_h = target_h
    # Track whether the clamp actually changed the height — comparing the
    # final value against the bounds ("row_h == min_row_height") misfires
    # when an UNclamped ideal height just happens to round to a bound,
    # skipping the drift fold and leaving a right gutter on that row.
    clamped_h = max(min_row_height, min(max_row_height, row_h))
    was_clamped = clamped_h != row_h
    row_h = clamped_h

    if justify and avail > 0 and not was_clamped:
        # Unclamped justify: apportion the width across the whole row so it
        # fills ``content_w`` exactly (no sub-pixel right gutter) *and* every
        # box keeps its own aspect to within a pixel.
        widths = _apportion_row(aspects, avail)
    else:
        widths = [max(1, int(round(a * row_h))) for a in aspects]
        if sum(widths) + gutters > content_w:
            # Overflow (clamped-to-min row, or a non-justified row that was
            # packed one tile too far): scaling the row height down keeps
            # every box matching its image.  Shaving the overflow off the
            # last tile alone would leave that one box with an aspect the
            # KeepAspectRatio paint cannot fill, so the image floats inside
            # it — the legibility floor is worth less than a box that lies
            # about its picture.
            if avail > 0:
                row_h = max(1, int(row_h * (avail / float(sum(widths)))))
                widths = [max(1, int(round(a * row_h))) for a in aspects]
                _trim_to_width(widths, avail)
            else:
                widths = [1] * n
    # else: clamped-to-max or non-justified last row → left-aligned, a
    # trailing gutter is accepted (Flickr's documented behaviour).

    x = x0
    first_idx = len(boxes)
    for (idx, _), w in zip(row, widths, strict=True):
        boxes.append(TileBox(index=idx, x=x, y=y, w=w, h=row_h))
        x += w + spacing
    last_idx = len(boxes) - 1
    bottom = y + row_h + caption_height
    rows.append(RowBand(top=y, bottom=bottom, first=first_idx, last=last_idx))
    return bottom + spacing


def compute_square_grid(
    n_tiles: int,
    *,
    viewport_width: int,
    target_size: int,
    spacing: int,
    caption_height: int,
    margin: int = 6,
) -> LayoutResult:
    """Uniform square cells, columns grown to fill the viewport width.

    ``target_size`` is treated as a *minimum* cell edge: we fit as many
    columns as possible at that size, then grow each cell to consume the
    full width (no trailing right gutter) — matching the legacy
    ``_compute_effective_icon_size`` behaviour.
    """
    if n_tiles <= 0:
        return LayoutResult([], [], margin)
    content_w = max(1, viewport_width - 2 * margin)
    target_size = max(1, target_size)
    stride = target_size + spacing
    cols = max(1, (content_w + spacing) // stride)
    cell = max(1, (content_w - (cols - 1) * spacing) // cols)

    boxes: list[TileBox] = []
    rows: list[RowBand] = []
    x0 = margin
    y = margin
    i = 0
    while i < n_tiles:
        first_idx = len(boxes)
        x = x0
        col = 0
        while col < cols and i < n_tiles:
            boxes.append(TileBox(index=i, x=x, y=y, w=cell, h=cell))
            x += cell + spacing
            col += 1
            i += 1
        bottom = y + cell + caption_height
        rows.append(
            RowBand(top=y, bottom=bottom, first=first_idx, last=len(boxes) - 1)
        )
        y = bottom + spacing
    content_h = (y - spacing) + margin if rows else margin
    return LayoutResult(boxes, rows, max(margin, content_h))


def compute_list_rows(
    n_tiles: int,
    *,
    viewport_width: int,
    row_height: int,
    spacing: int,
    margin: int = 6,
) -> LayoutResult:
    """One full-width row per tile (icon left + inline text).

    The caption is rendered inline (to the right of the icon) by the view,
    so there is no separate caption strip — each box IS the full row.
    """
    if n_tiles <= 0:
        return LayoutResult([], [], margin)
    content_w = max(1, viewport_width - 2 * margin)
    row_height = max(1, row_height)
    boxes: list[TileBox] = []
    rows: list[RowBand] = []
    x0 = margin
    y = margin
    for i in range(n_tiles):
        boxes.append(TileBox(index=i, x=x0, y=y, w=content_w, h=row_height))
        bottom = y + row_height
        rows.append(RowBand(top=y, bottom=bottom, first=i, last=i))
        y = bottom + spacing
    content_h = (y - spacing) + margin if rows else margin
    return LayoutResult(boxes, rows, max(margin, content_h))


# ------------------------------------------------------ strategies (Qt-free)


class LayoutStrategy:
    """Maps (tiles, viewport_width, params) → :class:`LayoutResult`.

    Subclasses implement :meth:`layout` only — hit-testing and visible-
    range are mode-independent (they operate on the geometry in
    :class:`LayoutResult`) and live as module functions below.
    """

    def layout(
        self,
        tiles: Sequence[TileInput],
        *,
        viewport_width: int,
        params: LayoutParams,
    ) -> LayoutResult:  # pragma: no cover - abstract
        raise NotImplementedError


class JustifiedRows(LayoutStrategy):
    def layout(self, tiles, *, viewport_width, params):
        return compute_justified_layout(
            tiles,
            viewport_width=viewport_width,
            target_row_height=params.target_size,
            spacing=params.spacing,
            caption_height=params.caption_height,
            min_row_height=params.min_row_height,
            max_row_height=params.max_row_height,
            margin=params.margin,
            justify_last_row=params.justify_last_row,
        )


class SquareGrid(LayoutStrategy):
    def layout(self, tiles, *, viewport_width, params):
        return compute_square_grid(
            len(tiles),
            viewport_width=viewport_width,
            target_size=params.target_size,
            spacing=params.spacing,
            caption_height=params.caption_height,
            margin=params.margin,
        )


class ListRows(LayoutStrategy):
    def layout(self, tiles, *, viewport_width, params):
        return compute_list_rows(
            len(tiles),
            viewport_width=viewport_width,
            row_height=params.target_size,
            spacing=params.spacing,
            margin=params.margin,
        )


def make_strategy(view_mode: str, thumb_layout: str) -> LayoutStrategy:
    """Pick the strategy for a (view_mode, thumb_layout) pair.

    ``list`` view mode always uses :class:`ListRows` (thumb_layout is
    ignored — list rows are uniform by design).  Icon mode picks
    :class:`JustifiedRows` for ``"justified"`` else :class:`SquareGrid`.
    """
    if view_mode == "list":
        return ListRows()
    if thumb_layout == "justified":
        return JustifiedRows()
    return SquareGrid()


# ------------------------------------------------------------- query helpers


def hit_test(result: LayoutResult, x: int, y: int) -> int | None:
    """Return the tile index at content-coordinate ``(x, y)``, or ``None``.

    Treats each tile's cell as image + caption (``RowBand`` bottom).  A
    point in the inter-row gutter or outside any tile's x-range returns
    ``None``.
    """
    rows = result.rows
    if not rows:
        return None
    r = _row_index_at_y(result, y)
    if r is None:
        return None
    band = rows[r]
    if not (band.top <= y < band.bottom):
        return None
    for i in range(band.first, band.last + 1):
        b = result.boxes[i]
        if b.x <= x < b.x + b.w:
            return i
    return None


def visible_range(
    result: LayoutResult,
    *,
    scroll_y: int,
    viewport_height: int,
    buffer_rows: int = 1,
) -> tuple[int, int] | None:
    """Inclusive ``(start, end)`` tile-index range intersecting the viewport.

    ``buffer_rows`` extends the range by that many rows on each side for
    pre-fetch.  Returns ``None`` when there are no rows.
    """
    rows = result.rows
    if not rows:
        return None
    top = scroll_y
    bottom = scroll_y + viewport_height
    tops = result.row_tops

    # First row whose top is at or above the viewport top.  Row bands are
    # laid out strictly sequentially (every builder sets the next row's top
    # to the previous bottom + spacing), so bands never overlap vertically
    # and no walk-back for "tall neighbours" is needed: the previous row's
    # bottom is always <= this row's top <= the viewport top.
    first_row = bisect.bisect_right(tops, top) - 1
    if first_row < 0:
        first_row = 0
    # Last row whose top is above the viewport bottom.
    last_row = bisect.bisect_left(tops, bottom) - 1
    if last_row < first_row:
        last_row = first_row

    first_row = max(0, first_row - buffer_rows)
    last_row = min(len(rows) - 1, last_row + buffer_rows)
    return rows[first_row].first, rows[last_row].last


def nearest_in_adjacent_row(
    result: LayoutResult, index: int, direction: int,
) -> int | None:
    """Tile geometrically nearest above/below ``index`` for arrow nav.

    ``direction`` is ``-1`` (up) or ``+1`` (down).  Picks the tile in the
    adjacent row whose x-centre is closest to ``index``'s x-centre.
    Returns ``None`` at the top/bottom edge.
    """
    if not (0 <= index < len(result.boxes)):
        return None
    cur = result.boxes[index]
    cur_cx = cur.x + cur.w / 2
    row = _row_of_tile(result, index)
    if row is None:
        return None
    target_row = row + direction
    if not (0 <= target_row < len(result.rows)):
        return None
    band = result.rows[target_row]
    best = band.first
    best_d = None
    for i in range(band.first, band.last + 1):
        b = result.boxes[i]
        cx = b.x + b.w / 2
        d = abs(cx - cur_cx)
        if best_d is None or d < best_d:
            best_d = d
            best = i
    return best


# ------------------------------------------------------------ scroll anchor


@dataclass(frozen=True)
class ScrollAnchor:
    """レイアウトに依存しないスクロール位置 = 「どのタイルのどこを、
    ビューポートの上から何 px に見せているか」.

    スクロール値（px）はレイアウトごとに意味が変わる — タイルサイズ・幅・
    上側の行のアスペクトが変わると全行の y が動くので、同じ値が数百枚先を
    指す。リレイアウトの前にこれを取り、後で :func:`scroll_for_anchor` で
    新しいレイアウトの値へ訳し直す。

    ``index`` はタイル添字、``fraction`` はそのタイルの**行**（画像 +
    キャプション、``RowBand.top``〜``bottom``）の中の位置（0 = 上端、1 =
    下端。行間の余白では 1 を超え、先頭行の上余白では負になり得る）、
    ``view_offset`` はその点をビューポート上端から何 px に置くか。
    同じレイアウトへ訳し直すと元の値に戻る（丸めの範囲で）。
    """

    index: int
    fraction: float
    view_offset: int = 0


def anchor_at_top(result: LayoutResult, *, scroll_y: int) -> ScrollAnchor | None:
    """ビューポート上端の行の先頭タイルに掛けたアンカー。行が無ければ ``None``."""
    rows = result.rows
    if not rows:
        return None
    r = max(0, bisect.bisect_right(result.row_tops, scroll_y) - 1)
    band = rows[r]
    height = max(1, band.bottom - band.top)
    return ScrollAnchor(
        index=band.first, fraction=(scroll_y - band.top) / height, view_offset=0,
    )


def anchor_on_tile(
    result: LayoutResult, index: int, *, scroll_y: int, fraction: float = 0.5,
) -> ScrollAnchor | None:
    """タイル *index* の行内 *fraction* の点を、いまのビューポート位置に留める
    アンカー（既定は行の中央）。タイルが無ければ ``None``."""
    row = _row_of_tile(result, index)
    if row is None:
        return None
    band = result.rows[row]
    point = band.top + fraction * max(1, band.bottom - band.top)
    # :func:`scroll_for_anchor` と同じく点を先に丸める — ``round(point -
    # scroll_y)`` は .5 の点で偶数丸めが逆へ振れ、同じレイアウトへ訳し直すと
    # 1 px ずれる。
    return ScrollAnchor(
        index=index, fraction=fraction, view_offset=int(round(point)) - scroll_y,
    )


def scroll_for_anchor(result: LayoutResult, anchor: ScrollAnchor) -> int | None:
    """*anchor* を *result* でのスクロール値へ訳す（クランプは呼び出し側）.

    アンカーのタイルがこのレイアウトに無ければ ``None``（タイル列が変わった）。
    """
    row = _row_of_tile(result, anchor.index)
    if row is None:
        return None
    band = result.rows[row]
    point = band.top + anchor.fraction * max(1, band.bottom - band.top)
    return int(round(point)) - anchor.view_offset


def _row_index_at_y(result: LayoutResult, y: int) -> int | None:
    """Index of the row whose [top, bottom) contains ``y`` (or the row the
    point falls within the spacing after).  Binary search on the layout's
    precomputed ``row_tops``."""
    if not result.rows:
        return None
    r = bisect.bisect_right(result.row_tops, y) - 1
    if r < 0:
        return None
    return r


def _row_of_tile(result: LayoutResult, index: int) -> int | None:
    rows = result.rows
    if not rows:
        return None
    # ``rows`` are ordered by ``first``; bisect the precomputed keys.
    r = bisect.bisect_right(result.row_firsts, index) - 1
    if r < 0:
        return None
    if rows[r].first <= index <= rows[r].last:
        return r
    return None


__all__ = [
    "TileInput",
    "LayoutParams",
    "TileBox",
    "RowBand",
    "LayoutResult",
    "compute_justified_layout",
    "compute_square_grid",
    "compute_list_rows",
    "LayoutStrategy",
    "JustifiedRows",
    "SquareGrid",
    "ListRows",
    "make_strategy",
    "hit_test",
    "visible_range",
    "nearest_in_adjacent_row",
    "ScrollAnchor",
    "anchor_at_top",
    "anchor_on_tile",
    "scroll_for_anchor",
]
