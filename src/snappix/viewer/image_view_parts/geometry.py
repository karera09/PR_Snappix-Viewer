"""``ImageView`` の幾何（ズーム率・フィット枠・パン範囲・パッチ矩形）。

この層はウィジェットに一切触らない。入力は値（原寸・ビューポート寸・倍率・
DPR・設定フラグ）で、出力も値（``QSize`` / ``QRect`` / 数値）。「いま何倍で
見えているか」「レンダのターゲットは何画素か」「可視領域のパッチはどこを
切り出すか」といった判定を、Qt ウィジェットを組まずに表明できる。

適用（ラベルの resize・スクロールバーの setValue・ピクスマップの差し替え）は
``image_view.ImageView`` が行う。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QRect, QSize, Qt

#: レンダターゲットが要求してよい**物理**画素面積の上限。
#: ホイールズームは 20x まで許すので、大判（例 8000×8000）は素直に計算すると
#: 160000×160000 = 数百 GB の確保を試みて OOM かスラッシングになる。100 MP は
#: ARGB32 で約 400 MB — どんな実ビューポートよりも十分大きく、最悪ケースの
#: 確保量は正気の範囲に収まる。超えるターゲットは縦横比を保って一様に縮む。
MAX_TARGET_PIXELS = 100_000_000

#: 可視領域パッチのマージン（ビューポート寸に対する割合）。パン中に高解像度
#: パッチの外へ出るまでの猶予。巨大ビューポート（8K + 高 DPR 等）でマージン
#: 込みが予算を超える場合は狭いマージンへ縮退し、それでも超えるときは物理
#: サイズ自体をクランプする（描画は矩形比で伸縮するので解像度が下がるだけ）。
PATCH_MARGIN_FRACTIONS = (0.5, 0.15, 0.0)

#: パッチの同期プレビュー（Qt スケール）を Smooth で行う出力画素数の上限。
#: 超えるときは Fast（最近傍）で即描きし、直後の高品位リサンプルに任せる。
PATCH_PREVIEW_SMOOTH_MAX_PIXELS = 8_000_000

#: パッチモードの低解像度全体像（下敷き）の画素予算。パン中にパッチ外へ出た
#: 領域を埋める背景なので、ビューポート級の精細度があれば十分。
PATCH_BASE_MAX_PIXELS = 4_000_000

#: ズーム操作の下限 / 上限（上限は :func:`zoom_ceiling` がさらに絞る）。
ZOOM_MIN = 0.05
ZOOM_MAX = 20.0

#: フィット枠がビューポートから引く余白（左右 / 上下それぞれ 1 px ずつ）。
FIT_MARGIN = QSize(2, 2)


def clamp_pixel_budget(
    size: QSize, max_pixels: int = MAX_TARGET_PIXELS,
) -> QSize:
    """``width * height`` が予算に収まるよう *size* を一様に縮める。"""
    total = size.width() * size.height()
    if total <= max_pixels:
        return size
    factor = (max_pixels / total) ** 0.5
    return QSize(
        max(1, int(size.width() * factor)),
        max(1, int(size.height() * factor)),
    )


def fit_logical_box(
    viewport: QSize, natural: QSize, *, no_upscale: bool,
) -> QSize:
    """フィット表示の論理枠（ビューポート − 余白、必要なら原寸でクランプ）。

    *no_upscale* が真なら小さい画像を等倍超に拡大しない — 枠を原寸へ丸めるので
    ぼやけた拡大ではなく、くっきり中央寄せで出る。
    """
    box = viewport - FIT_MARGIN
    if no_upscale:
        box = QSize(
            min(box.width(), natural.width()),
            min(box.height(), natural.height()),
        )
    return box


def target_phys_box(
    *,
    natural: QSize,
    fit_mode: bool,
    zoom: float,
    viewport: QSize,
    dpr: float,
    no_upscale: bool,
) -> QSize | None:
    """全体レンダの**物理**ターゲット寸。描けないときは ``None``。

    フィット時はビューポート束縛、非フィット時は原寸 × 倍率。どちらも最後に
    :func:`clamp_pixel_budget` を通す（予算超のズーム域はパッチレンダが引き
    受けるので、静止画でこのクランプが実際に効くことは通常ない安全網）。
    """
    if fit_mode:
        logical = fit_logical_box(viewport, natural, no_upscale=no_upscale)
    else:
        logical = QSize(
            max(1, round(natural.width() * zoom)),
            max(1, round(natural.height() * zoom)),
        )
    if logical.width() <= 0 or logical.height() <= 0:
        return None
    return clamp_pixel_budget(QSize(
        max(1, round(logical.width() * dpr)),
        max(1, round(logical.height() * dpr)),
    ))


def zoom_ceiling(*, is_gif: bool, natural: QSize | None) -> float:
    """ズーム操作（ホイール / ±）の上限倍率。

    静止画は、全体レンダが予算を超えるズーム域を可視領域パッチレンダが引き
    受けるため常に :data:`ZOOM_MAX` — 表示は常に実効解像度と一致する。

    ``QMovie``（GIF / アニメ WebP）はパッチ対象外で、フレームごとの全体
    スケールのクランプが実表示の上限のまま。予算クランプが効き始める倍率で
    頭打ちにし、「画面が変わらないのに数値だけ上がる」乖離を防ぐ。QMovie の
    クランプは論理ピクセルに掛かるので dpr は寄与しない。
    """
    if not is_gif or natural is None:
        return ZOOM_MAX
    area = natural.width() * natural.height()
    if area <= 0:
        return ZOOM_MAX
    ceiling = (MAX_TARGET_PIXELS / area) ** 0.5
    return max(ZOOM_MIN, min(ZOOM_MAX, ceiling))


def effective_zoom(
    *,
    natural: QSize | None,
    fit_mode: bool,
    zoom: float,
    viewport: QSize,
    no_upscale: bool,
    ceiling: float,
) -> float:
    """いま画面に出ている倍率（原寸を 1.0 とする）。

    フィット時の *zoom* は古い値のまま（既定 1.0）でユーザーが見ているものを
    表さない — 画像はビューポートへ縮めて描かれている。ホイールズームはこの
    実効倍率から始めないと、1 ノッチ目でフィット表示から飛び離れる。

    非フィット時は *ceiling* で頭打ちにする。静止画はパッチレンダが *zoom*
    どおりの実表示を出すので実質そのまま返り、QMovie では予算クランプ済みの
    実効倍率（画面に出ている倍率）を返す。
    """
    if not fit_mode:
        return min(zoom, ceiling)
    if natural is None:
        return zoom
    box = viewport - FIT_MARGIN
    if box.width() <= 0 or box.height() <= 0:
        return zoom
    ratio = min(
        box.width() / natural.width(), box.height() / natural.height(),
    )
    # 等倍超に拡大しないフィットでは小さい画像が 100% で出るので、実効倍率も
    # 画面に合わせて 1.0 で頭打ちにする。
    if no_upscale:
        ratio = min(ratio, 1.0)
    return ratio


def step_zoom_value(base: float, factor: float, ceiling: float) -> float:
    """1 段ズーム後の倍率（ホイール / ± キー共通のクランプ）。"""
    return max(ZOOM_MIN, min(ceiling, base * factor))


def full_zoom_size(natural: QSize, zoom: float) -> QSize:
    """非フィット時の論理レンダ全寸（= ラベル寸）。"""
    return QSize(
        max(1, round(natural.width() * zoom)),
        max(1, round(natural.height() * zoom)),
    )


def patch_mode_wanted(
    *,
    fit_mode: bool,
    is_gif: bool,
    natural: QSize | None,
    zoom: float,
    dpr: float,
) -> bool:
    """全体レンダが予算を超える静止画ズームか（= パッチ描画へ切り替える）。

    フィット表示はビューポート束縛で予算内、``QMovie`` はフレームごとの全体
    スケールしかできないため対象外。
    """
    if fit_mode or is_gif or natural is None:
        return False
    size = full_zoom_size(natural, zoom)
    scale = max(1.0, dpr or 1.0)
    phys = round(size.width() * scale) * round(size.height() * scale)
    return phys > MAX_TARGET_PIXELS


def visible_label_rect(
    *, label_pos: QPoint, label_size: QSize, viewport: QSize,
) -> QRect:
    """いまビューポートに見えているラベル領域（ラベル論理座標）。

    *label_pos* はスクロールオフセット（と、片軸がビューポートより小さいときの
    センタリング）込みの位置なので、その符号反転が可視域の左上になる。
    """
    visible = QRect(-label_pos, viewport)
    return visible.intersected(QRect(QPoint(0, 0), label_size))


def patch_geometry(
    *,
    natural: QSize,
    zoom: float,
    visible: QRect,
    canvas: QRect,
    viewport: QSize,
    dpr: float,
) -> tuple[QRect, tuple[int, int, int, int], QSize] | None:
    """(パッチ論理矩形, 原寸クロップ box, 物理出力サイズ) を計算する。

    クロップは整数ソース座標へ外側スナップし、パッチ矩形はその box から逆算
    する — ソース画素とラベル座標の対応がサブピクセル精度で一致し、描画時の
    矩形比スケールで位置ズレが出ない。マージンは予算内に収まる最大の割合を
    選ぶ（:data:`PATCH_MARGIN_FRACTIONS`）。
    """
    if zoom <= 0 or visible.isEmpty():
        return None
    scale = max(1.0, dpr or 1.0)
    rect = visible
    for frac in PATCH_MARGIN_FRACTIONS:
        mx = round(viewport.width() * frac)
        my = round(viewport.height() * frac)
        rect = visible.adjusted(-mx, -my, mx, my).intersected(canvas)
        phys = round(rect.width() * scale) * round(rect.height() * scale)
        if phys <= MAX_TARGET_PIXELS:
            break
    src_w, src_h = natural.width(), natural.height()
    x0 = max(0, math.floor(rect.left() / zoom))
    y0 = max(0, math.floor(rect.top() / zoom))
    x1 = min(src_w, math.ceil((rect.left() + rect.width()) / zoom))
    y1 = min(src_h, math.ceil((rect.top() + rect.height()) / zoom))
    if x1 <= x0 or y1 <= y0:
        return None
    tx0 = max(0, round(x0 * zoom))
    ty0 = max(0, round(y0 * zoom))
    tx1 = min(canvas.width(), round(x1 * zoom))
    ty1 = min(canvas.height(), round(y1 * zoom))
    target = QRect(tx0, ty0, tx1 - tx0, ty1 - ty0)
    if target.isEmpty():
        return None
    phys_size = clamp_pixel_budget(QSize(
        max(1, round(target.width() * scale)),
        max(1, round(target.height() * scale)),
    ))
    return target, (x0, y0, x1, y1), phys_size


def gif_scale_target(
    *,
    natural: QSize,
    fit_mode: bool,
    zoom: float,
    viewport: QSize,
    no_upscale: bool,
) -> QSize | None:
    """``QMovie.setScaledSize`` に渡す論理寸。描けないときは ``None``。

    非フィット時は :func:`target_phys_box` と同じ予算クランプを掛ける —
    掛けないと QMovie がフレームごとにズーム後の全面を確保する。
    """
    if natural.width() <= 0 or natural.height() <= 0:
        return None
    if fit_mode:
        box = fit_logical_box(viewport, natural, no_upscale=no_upscale)
        if box.width() <= 0 or box.height() <= 0:
            return None
        target = natural.scaled(box, Qt.AspectRatioMode.KeepAspectRatio)
    else:
        target = clamp_pixel_budget(QSize(
            max(1, round(natural.width() * zoom)),
            max(1, round(natural.height() * zoom)),
        ))
    if target.width() <= 0 or target.height() <= 0:
        return None
    return target


def scroll_fraction(
    *, h_value: int, h_max: int, v_value: int, v_max: int,
) -> tuple[float, float]:
    """現在のスクロール位置を各スクロールバーの割合（0..1）で返す。

    スクロールできない軸は 0.5（中央）— 復元側は最大値 0 の軸を触らないので、
    この値は「位置の情報が無い」ことの印にすぎない。
    """
    fx = h_value / h_max if h_max > 0 else 0.5
    fy = v_value / v_max if v_max > 0 else 0.5
    return fx, fy


def restore_scroll_values(
    fx: float, fy: float, *, h_max: int, v_max: int,
) -> tuple[int | None, int | None]:
    """割合からスクロールバー値へ戻す（スクロール不能な軸は ``None``）。"""
    return (
        round(fx * h_max) if h_max > 0 else None,
        round(fy * v_max) if v_max > 0 else None,
    )


def anchor_fraction(
    *, vp_pos: QPoint, label_top_left: QPoint, label_size: QSize,
) -> tuple[float, float]:
    """カーソル下の点が、いまのラベルのどこ（0..1）かを返す。

    ズーム前に捉えておき、ズーム後に :func:`anchor_scroll_values` で同じ割合を
    カーソル下へ戻すと、ポインタの下の画像の点が動かない。
    """
    old_w = max(1, label_size.width())
    old_h = max(1, label_size.height())
    fx = (vp_pos.x() - label_top_left.x()) / old_w
    fy = (vp_pos.y() - label_top_left.y()) / old_h
    return max(0.0, min(1.0, fx)), max(0.0, min(1.0, fy))


def anchor_scroll_values(
    fx: float, fy: float, *, label_size: QSize, vp_pos: QPoint,
) -> tuple[int, int]:
    """ズーム後のラベル寸で、割合 *(fx, fy)* をカーソル下へ戻す値。"""
    return (
        int(round(fx * label_size.width() - vp_pos.x())),
        int(round(fy * label_size.height() - vp_pos.y())),
    )


def minimap_view_rect(
    *, h_value: int, v_value: int, content: QSize, viewport: QSize,
) -> tuple[float, float, float, float] | None:
    """ミニマップの視野枠（左上・右下の割合）。中身が無ければ ``None``。"""
    content_w = content.width()
    content_h = content.height()
    if content_w <= 0 or content_h <= 0:
        return None
    return (
        h_value / content_w,
        v_value / content_h,
        min(1.0, (h_value + viewport.width()) / content_w),
        min(1.0, (v_value + viewport.height()) / content_h),
    )


def minimap_pan_values(
    fx: float, fy: float, *, content: QSize, viewport: QSize,
) -> tuple[int, int]:
    """ミニマップで指した割合の点を、ビューポート中央へ置くスクロール値。

    クランプはスクロールバーの実際の範囲に依存するので呼び出し側が行う。
    """
    return (
        round(fx * content.width() - viewport.width() / 2),
        round(fy * content.height() - viewport.height() / 2),
    )


__all__ = [
    "FIT_MARGIN",
    "MAX_TARGET_PIXELS",
    "PATCH_BASE_MAX_PIXELS",
    "PATCH_MARGIN_FRACTIONS",
    "PATCH_PREVIEW_SMOOTH_MAX_PIXELS",
    "ZOOM_MAX",
    "ZOOM_MIN",
    "anchor_fraction",
    "anchor_scroll_values",
    "clamp_pixel_budget",
    "effective_zoom",
    "fit_logical_box",
    "full_zoom_size",
    "gif_scale_target",
    "minimap_pan_values",
    "minimap_view_rect",
    "patch_geometry",
    "patch_mode_wanted",
    "restore_scroll_values",
    "scroll_fraction",
    "step_zoom_value",
    "target_phys_box",
    "visible_label_rect",
    "zoom_ceiling",
]
