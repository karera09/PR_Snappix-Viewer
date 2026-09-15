"""``GalleryView`` の入力判定（イベント → **意図**）。

この層はウィジェットの状態を一切変えない。キー / ホイール / マウスの生の
値と「いまの観測値（タイル数・選択位置・表示形式・受け手の有無）」だけを見て、
何をしたいかを表す小さな値（:data:`Intent`）を返す。適用（選択の移動・
スクロール・シグナル発火）はビューが行う。

こうしておくと「Ctrl+ホイールの受け手が居ない席では消費しない」「数字キーは
選択がある素の修飾のときだけ★」「Escape はビューで扱わない」といった判定が、
ウィジェットを組まずに表明できる。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt

#: 矢印 / Home / End / PageUp / PageDown — ``ShortcutOverride`` を横取りして
#: 窓のグローバル ←/→ に盗まれないようにするキー集合。**Escape は入れない**
#: （#115: Escape は窓の単一 ``QShortcut`` が消費する）。
NAV_KEYS = frozenset({
    Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
    Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown,
})


@dataclass(frozen=True)
class Unhandled:
    """このビューは関知しない（呼び出し元は基底実装へ委ねる）。"""


@dataclass(frozen=True)
class Zoom:
    """サムネイルを 1 段拡大 / 縮小したい（``steps`` = +1 / -1）。"""

    steps: int


@dataclass(frozen=True)
class ScrollBy:
    """ビューポートを *pixels* ぶん縦スクロールしたい（正 = 下へ）。"""

    pixels: int


@dataclass(frozen=True)
class StepSelection:
    """選択を線形に *delta* 個ずらしたい。"""

    delta: int


@dataclass(frozen=True)
class SelectIndex:
    """*index* のタイルを選びたい（範囲外は呼び出し側で不発）。"""

    index: int


@dataclass(frozen=True)
class MoveRow:
    """隣接行の最寄りタイルへ移りたい（``direction`` = -1 上 / +1 下）。"""

    direction: int


@dataclass(frozen=True)
class PageStep:
    """1 ページぶん選択を送りたい（``direction`` = -1 上 / +1 下）。"""

    direction: int


@dataclass(frozen=True)
class ActivateCurrent:
    """選択中のタイルをアクティベートしたい（ダブルクリックと同じ経路）。"""


@dataclass(frozen=True)
class GoUp:
    """親フォルダへ上がりたい（配線するかは席が決める）。"""


@dataclass(frozen=True)
class StarKey:
    """選択タイルの★を *value*（0 = 解除 / 1–5）にしたい。"""

    value: int


#: イベント 1 つに対する意図。
Intent = (
    Unhandled | Zoom | ScrollBy | StepSelection | SelectIndex | MoveRow
    | PageStep | ActivateCurrent | GoUp | StarKey
)

UNHANDLED = Unhandled()


def wheel_intent(
    *, delta: int, ctrl: bool, has_zoom_handler: bool, scroll_pixels: int,
) -> Intent:
    """ホイール 1 ノッチの意図。

    Ctrl+ホイール = サムネイルの拡大縮小 — image_view / markdown_view と
    同じ慣習。値は自分で持たず、ホストのサイズスライダを 1 段動かして
    もらう。**受け手（構築時の ``zoom_handler``）が居ない席では消費しない** —
    :data:`UNHANDLED` を返して親へ流し、その席の chrome に判断を委ねる。
    """
    if delta and ctrl:
        if not has_zoom_handler:
            return UNHANDLED
        return Zoom(1 if delta > 0 else -1)
    if delta:
        # Pixels per notch follow the user-tunable preview scroll speed
        # (settings dialog → プレビュースクロール速度) so the grid panes
        # and the centre previews feel consistent.
        return ScrollBy(-int(round(delta / 120.0 * scroll_pixels)))
    return UNHANDLED


def key_intent(
    key: int, modifiers, *, tile_count: int, has_selection: bool,
) -> Intent:
    """キー 1 打の意図（順序は従来の分岐と 1 対 1）。

    Escape の枝は**無い**（#115）— 窓の常時 ``QShortcut`` が先に消費するので、
    ここに置くと生きた第 2 実装に読める死にコードになる。
    """
    if key == Qt.Key_Left:
        return StepSelection(-1)
    if key == Qt.Key_Right:
        return StepSelection(1)
    if key == Qt.Key_Home:
        return SelectIndex(0)
    if key == Qt.Key_End:
        return SelectIndex(tile_count - 1)
    if key in (Qt.Key_Up, Qt.Key_Down):
        direction = -1 if key == Qt.Key_Up else 1
        return SelectIndex(0) if not has_selection else MoveRow(direction)
    if key in (Qt.Key_PageUp, Qt.Key_PageDown):
        return PageStep(1 if key == Qt.Key_PageDown else -1)
    if key in (Qt.Key_Return, Qt.Key_Enter):
        # Activate the current tile — same path as a double-click (folder
        # drill-in / file open) so the keyboard mirrors the mouse.
        return ActivateCurrent() if has_selection else UNHANDLED
    if key == Qt.Key_Backspace:
        # Go up one level (host decides whether it applies — only the left
        # pane connects this).
        return GoUp()
    if (
        Qt.Key_0 <= key <= Qt.Key_5
        and has_selection
        and modifiers in (Qt.NoModifier, Qt.KeypadModifier)
    ):
        # Digit 0–5 sets / clears the user star on the selected tile.
        # Guarded by a live selection so a stray digit on an empty grid is
        # inert, and by bare (or numpad-only) modifiers so shortcuts like
        # Ctrl+1 don't silently rewrite stars.
        return StarKey(key - Qt.Key_0)
    return UNHANDLED


@dataclass(frozen=True)
class PressOutcome:
    """左ボタン押下 1 回の意図（どれも「まだ何もしていない」値）。"""

    #: ◇ 類似オーバーレイが押された添字（押されていなければ ``None``）。
    #: 非 ``None`` のときは選択もドラッグ待機も起こさない。
    similar_index: int | None = None
    #: 選択する添字（タイルの無い場所なら ``None``）。
    select_index: int | None = None
    #: ドラッグ待機を張るか（押下位置とともにビューが保持する）。
    arm_drag: bool = False


def press_outcome(
    index_at: int | None,
    *,
    view_mode: str,
    similar_overlay_index: int | None,
    similar_hit: bool,
) -> PressOutcome:
    """左ボタン押下の意図。

    A press on the shown 「◇」 similarity overlay fires the similar-search
    request instead of selecting / arming a drag.  描画は icon モード限定な
    ので、当たり判定も同じゲートを通す（非対称だと描かれていないボタンが
    押せてしまう）。
    """
    if (
        view_mode == "icon"
        and similar_overlay_index is not None
        and similar_hit
    ):
        return PressOutcome(similar_index=similar_overlay_index)
    return PressOutcome(select_index=index_at, arm_drag=True)


def drag_threshold_reached(
    start: QPoint, current: QPoint, threshold: int,
) -> bool:
    """押下位置からプラットフォームのドラッグ開始距離を越えたか。"""
    return (current - start).manhattanLength() >= threshold


@dataclass(frozen=True)
class PageStepPlan:
    """PageUp / PageDown 1 回の着地計画。

    ``QAbstractItemView`` の Page 意味論に合わせ、カーソルはページと**一緒に**
    動く（置き去りにすると次の矢印で視界が古い選択へ戻る）。
    """

    #: ビューを直接スクロールする量（px。0 = スクロールしない）。
    scroll_delta: int = 0
    #: 選ぶ添字（``None`` かつ :attr:`select_from_visible` が偽なら選択を変えない）。
    select_index: int | None = None
    #: スクロール後の可視域の端（下送りなら先頭 / 上送りなら末尾）を選ぶ。
    select_from_visible: bool = False


def page_step_plan(
    layout, *, selected: int, tile_count: int, direction: int, page_h: int,
) -> PageStepPlan:
    """Move the selection ~one viewport page up/down (``direction`` ±1).

    Geometry-based: pick the tile in the row nearest to the current tile's
    centre displaced by one viewport height, preferring the same column.
    With no selection yet, fall back to a plain page scroll and select the
    first/last tile that lands in view; ``select_index``'s ensure-visible
    then performs the actual scrolling in the normal case.
    """
    page = max(1, page_h)
    rows = layout.rows
    if tile_count <= 0 or not rows:
        return PageStepPlan(scroll_delta=direction * page)
    if selected < 0 or selected >= len(layout.boxes):
        return PageStepPlan(
            scroll_delta=direction * page, select_from_visible=True,
        )
    box = layout.boxes[selected]
    cur_cx = box.x + box.w / 2
    # ``box.y`` equals its row's top, so the same bisect resolves both
    # the current row and the row one page away.
    cur_row = max(0, bisect.bisect_right(layout.row_tops, box.y) - 1)
    target_y = box.y + box.h / 2 + direction * page
    target_row = bisect.bisect_right(layout.row_tops, target_y) - 1
    target_row = max(0, min(len(rows) - 1, target_row))
    if target_row == cur_row:
        # Rows taller than the viewport: guarantee at least one row of
        # travel so repeated presses always make progress.
        target_row = cur_row + direction
    if not (0 <= target_row < len(rows)):
        # Past the extremity — clamp to the first/last tile (Home/End).
        return PageStepPlan(
            select_index=tile_count - 1 if direction > 0 else 0
        )
    band = rows[target_row]
    best = band.first
    best_d = None
    for i in range(band.first, band.last + 1):
        b = layout.boxes[i]
        d = abs(b.x + b.w / 2 - cur_cx)
        if best_d is None or d < best_d:
            best_d = d
            best = i
    return PageStepPlan(select_index=best)


__all__ = [
    "NAV_KEYS",
    "UNHANDLED",
    "ActivateCurrent",
    "GoUp",
    "Intent",
    "MoveRow",
    "PageStep",
    "PageStepPlan",
    "PressOutcome",
    "ScrollBy",
    "SelectIndex",
    "StarKey",
    "StepSelection",
    "Unhandled",
    "Zoom",
    "drag_threshold_reached",
    "key_intent",
    "page_step_plan",
    "press_outcome",
    "wheel_intent",
]
