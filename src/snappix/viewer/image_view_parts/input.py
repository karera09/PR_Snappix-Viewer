"""``ImageView`` の入力判定（イベント → **意図**）。

``gallery_view_parts.input`` と同じ作法: この層はウィジェットの状態を一切
変えない。キー / ホイール / マウスの生の値と「いまの観測値（画像が載って
いるか・パン可能か・アニメーションか・席の設定）」だけを見て、何をしたいかを
表す小さな値（:data:`Intent`）を返す。適用（ズーム・パン・シグナル発火・
ドラッグ書き出し）はビューが行う。

こうしておくと「ドラッグに発展しなかった左クリックだけが再生トグル」
「中クリックは切り替えられるときだけ飲み込む」「ホイール割当は設定で反転する」
といった判定が、ウィジェットを組まずに表明できる。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, Qt

#: :func:`label_intent` が判定するイベント種。ラベルのイベントフィルタには
#: 描画・ジオメトリ・子の追加まで全部来るので、``button()`` / ``position()``
#: を持つマウス系だけをここで切り出してから値を読む。
MOUSE_EVENT_TYPES = frozenset({
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
})

#: 下端の操作カプセルを呼び出すホバー activity のイベント種。画像上でマウスが
#: 動けばカプセルが出る（自分のアイドルタイマーで再び消える）。キーボード
#: だけの操作では決して起きないので、邪魔にならない。
HOVER_EVENT_TYPES = frozenset({
    QEvent.Type.MouseMove, QEvent.Type.Enter,
})


#: ホイール 1 ノッチのズーム倍率（拡大方向）。
WHEEL_ZOOM_FACTOR = 1.1
#: ± キー 1 打のズーム倍率（拡大方向）。
KEY_ZOOM_FACTOR = 1.25


@dataclass(frozen=True)
class Unhandled:
    """このビューは関知しない（呼び出し元は基底実装 / 次の経路へ委ねる）。"""


@dataclass(frozen=True)
class Consume:
    """イベントは飲み込むが、状態は何も変えない。"""


@dataclass(frozen=True)
class ZoomAtCursor:
    """カーソル位置を固定して *factor* 倍したい。"""

    factor: float


@dataclass(frozen=True)
class StarKey:
    """表示中の画像の★を *value*（0 = 解除 / 1–5）にしたい。"""

    value: int


@dataclass(frozen=True)
class ArmDrag:
    """左押下 — ドラッグ待機を張るだけ（まだ何も起こさない）。"""


@dataclass(frozen=True)
class ToggleFitActual:
    """フィット ⇄ 実寸を切り替えたい（ビューポート中央基準）。"""


@dataclass(frozen=True)
class Maximize:
    """プレビュー列を最大化したい（分割ビューでのダブルクリック）。"""


@dataclass(frozen=True)
class ToggleZoomAt:
    """カーソル位置を固定してフィット ⇄ 実寸を切り替えたい。"""


@dataclass(frozen=True)
class BeginPan:
    """ドラッグ距離を越えた & パン可能 — パンを開始したい。"""


@dataclass(frozen=True)
class PanTo:
    """パン中の追従（カーソルの移動量ぶんスクロールしたい）。"""


@dataclass(frozen=True)
class StartExportDrag:
    """ドラッグ距離を越えた & パン不可 — ファイルを外へドラッグしたい。"""


@dataclass(frozen=True)
class EndPan:
    """パンを終える（カーソルを戻す）。"""


@dataclass(frozen=True)
class ToggleMovie:
    """アニメーションの再生 / 一時停止を切り替えたい。"""


#: イベント 1 つに対する意図。
Intent = (
    Unhandled | Consume | ZoomAtCursor | StarKey | ArmDrag | ToggleFitActual
    | Maximize | ToggleZoomAt | BeginPan | PanTo | StartExportDrag | EndPan
    | ToggleMovie
)

UNHANDLED = Unhandled()
CONSUME = Consume()


def wheel_intent(
    *, delta: int, ctrl: bool, zoomable: bool, wheel_zoom_pref: bool,
) -> Intent:
    """ホイール 1 ノッチの意図。

    既定はホイール = 前後送り・Ctrl = ズーム。設定 ``image_wheel_zoom`` が真だと
    二つが入れ替わる — ``ctrl != wheel_zoom_pref`` が、ユーザーが選んだどちらの
    修飾状態でもズームのジェスチャを選ぶ。

    :data:`UNHANDLED` はスクロール / ファイル送りの経路へ流す合図。
    """
    if (ctrl != wheel_zoom_pref) and zoomable:
        if delta == 0:
            return CONSUME
        return ZoomAtCursor(
            WHEEL_ZOOM_FACTOR if delta > 0 else 1 / WHEEL_ZOOM_FACTOR
        )
    return UNHANDLED


def key_intent(key: int, modifiers) -> Intent:
    """キー 1 打の意図。

    数字 0–5 は素の（またはテンキーのみの）修飾のときだけ★ — ``Ctrl+1``
    （フィット）/ ``Ctrl+0``（実寸）はズームの意味を保つ。
    """
    if Qt.Key_0 <= key <= Qt.Key_5 and modifiers in (
        Qt.NoModifier, Qt.KeypadModifier,
    ):
        return StarKey(key - Qt.Key_0)
    return UNHANDLED


def label_intent(
    etype,
    *,
    button,
    buttons,
    armed: bool,
    panning: bool,
    zoomable: bool,
    has_movie: bool,
    double_click_maximize: bool,
    has_path: bool,
    pannable: bool,
    drag_reached: bool,
) -> Intent:
    """画像ラベル上のマウスイベント 1 つの意図（分岐は従来と 1 対 1）。

    *armed* は「左押下でドラッグ待機が張られている」か。押下は待機を張るだけ
    （:class:`ArmDrag`）で、アニメーションの再生 / 一時停止はドラッグに発展
    しなかった Release で決める — 押下で即トグルしていると、ズームした GIF を
    パンするたびに再生状態が必ず反転して「再生させたまま端を見る」「止めたまま
    観察する」のどちらも構造的に不可能になる。
    """
    press = QEvent.Type.MouseButtonPress
    if etype == press and button == Qt.MouseButton.LeftButton:
        return ArmDrag()
    if etype == press and button == Qt.MouseButton.MiddleButton:
        # 中クリック = フィット ⇄ 実寸。画像が載っていないときまで飲み込むと
        # 中クリックがどこにも届かなくなるので、切り替えられるときだけ。
        return ToggleFitActual() if zoomable else UNHANDLED
    if (
        etype == QEvent.Type.MouseButtonDblClick
        and button == Qt.MouseButton.LeftButton
        and (double_click_maximize or not has_movie)
    ):
        # 分割ビュー（ホストが ``double_click_maximize`` を立てた席）では
        # 「プレビューを大きく」。最大化中 / 全画面では従来どおりフィット ⇄
        # 実寸をカーソル基準で切り替える。GIF はダブルクリックが再生トグルに
        # 化けるので**ズーム**の切り替えだけ静止画に限るが、最大化は分割
        # ビューの主要なマウス導線なのでアニメーションでも先に判定する。
        # 1 回目の Release（押下はここへ来る前に DblClick へ置き換わるので
        # トグルはその 1 回だけ）が切り替えた再生状態は、最大化の実行側
        # （``ImageView.eventFilter`` → ``MoviePlayback.undo_release_toggle``）が
        # 打ち消す。
        return Maximize() if double_click_maximize else ToggleZoomAt()
    if (
        etype == QEvent.Type.MouseMove
        and (buttons & Qt.MouseButton.LeftButton)
        and armed
    ):
        if panning:
            return PanTo()
        if drag_reached:
            # ビューポートを超えて拡大されていればドラッグはパン、そうで
            # なければファイルを他アプリへ書き出すドラッグ。
            if pannable:
                return BeginPan()
            if has_path:
                return StartExportDrag()
        return UNHANDLED
    if etype == QEvent.Type.MouseButtonRelease:
        if panning:
            return EndPan()
        if armed and has_movie and button == Qt.MouseButton.LeftButton:
            return ToggleMovie()
    return UNHANDLED


def drag_threshold_reached(
    start: QPoint, current: QPoint, threshold: int,
) -> bool:
    """押下位置からプラットフォームのドラッグ開始距離を越えたか。"""
    return (current - start).manhattanLength() >= threshold


__all__ = [
    "CONSUME",
    "HOVER_EVENT_TYPES",
    "KEY_ZOOM_FACTOR",
    "MOUSE_EVENT_TYPES",
    "UNHANDLED",
    "WHEEL_ZOOM_FACTOR",
    "ArmDrag",
    "BeginPan",
    "Consume",
    "EndPan",
    "Intent",
    "Maximize",
    "PanTo",
    "StarKey",
    "StartExportDrag",
    "ToggleFitActual",
    "ToggleMovie",
    "ToggleZoomAt",
    "Unhandled",
    "ZoomAtCursor",
    "drag_threshold_reached",
    "key_intent",
    "label_intent",
    "wheel_intent",
]
