"""ペイン席の「いまフォーカスがどこにあるか」を**見出し帯**で示す共通部品.

席の周囲に 2px の accent 枠を出す形は「太く目立ちすぎる」「右辺・下辺が
1px 内側へズレる」（ペン幅 2 の ``drawRect`` は輪郭線の中心にペンを置くので
左上と右下で非対称になる）ため採らない。閉じた枠ではなく、フォーカスを
内包する節の見出し行
（:class:`~.widgets.PanelHeader` / ``StageHeader``）だけを
「地色を一段上げ + 左端に ▸ 印 + 題名を accent」にする。

**なぜ枠でなく見出しか**: 写真を見る道具なので画像の周りに線を立てたく
ない。見出し行は写真に触れず、席の名前と位置を同時に読める。印（▸ と
題名）は不透明のインク :func:`~.tokens.focus_band_ink`（dark 系 = accent、
light 系 = 深い accent_pressed）なので WCAG 1.4.11 の非テキスト 3:1 と
本文 4.5:1 を全テーマで満たす（半透明の accent 混色は 1.0〜1.5:1 にしか
ならず、以前の 1px・α40 枠と同じ失敗になる）。帯の地色 ``bg_hover`` は
輪郭を柔らかく示す補助（``bg_raised`` は light 系で周囲比 1.05:1 と消える
ので、全テーマで 1.12〜1.35:1 の ``bg_hover``）。

**なぜ席ではなく節か**: ナビレールは 4 節、情報パネルは最大 4 節の見出し
を持つので、フォーカスを内包する**最も内側の節**の見出しだけを灯す。
見出しを持たない席（グリッド）は、フォーカスが無いと選択タイルを減光する
``GalleryView._selection_active`` が既に「キーの受け手」を示している。

**レイアウトは 1px も動かない**: 帯の地色は QSS の動的プロパティ
``focusBand``、▸ は見出しの左マージン内に描く、題名の色替えは
スタイルシートの差し替え — どれもジオメトリに触れない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast, runtime_checkable

from PySide6.QtCore import QObject
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget

from .theme import current_tokens
from .tokens import focus_band_ink

#: ▸ 印の幅（px）。見出しの左マージン（8px）の内側に収まる大きさで、高さは
#: ``2 * width - 1``（右向きの二等辺三角形）。
FOCUS_MARK_WIDTH = 5
#: ▸ 印の左端（見出し左端からの px）。
FOCUS_MARK_X = 1


@runtime_checkable
class FocusBandTarget(Protocol):
    """帯を点けられる見出し（:class:`FocusBandMixin` を混ぜた QWidget）。"""

    def set_focus_band(self, on: bool) -> None: ...


class FocusBandMixin:
    """見出し部品に ``set_focus_band`` と ▸ 印の描画を与える混ぜ込み.

    ``class Header(FocusBandMixin, QWidget)`` の順で継承する。題名ラベルは
    objectName ``panelHeaderTitle`` を持ち、帯中の色は qss.py の
    ``QWidget#panelHeader[focusBand="true"] QLabel#panelHeaderTitle``
    （:func:`~.tokens.focus_band_ink`）が与える。混ぜ込む側は
    :meth:`_apply_focus_band_title` で、題名の inline 色（``hint_style``）を
    帯中だけ外す（inline の色は app シートより強く、残すと勝つ）。
    帯の地色も同じ qss の ``[focusBand="true"]`` 規則が担う。
    """

    _focus_band = False

    def set_focus_band(self, on: bool) -> None:
        on = bool(on)
        if on == self._focus_band:
            return
        self._focus_band = on
        widget = cast(QWidget, self)
        widget.setProperty("focusBand", on)
        self._apply_focus_band_title(on)
        # 動的プロパティの変更は QSS セレクタの再評価を起こさないので、
        # 見出しと（祖先セレクタで色を受ける）子ラベルを unpolish → polish で
        # 引き直す。
        style = widget.style()
        for target in (widget, *widget.findChildren(QLabel, "panelHeaderTitle")):
            style.unpolish(target)
            style.polish(target)
        widget.update()

    def focus_band(self) -> bool:
        return self._focus_band

    def _apply_focus_band_title(self, on: bool) -> None:  # pragma: no cover
        raise NotImplementedError

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().paintEvent(event)  # type: ignore[misc]
        if self._focus_band:
            paint_focus_mark(cast(QWidget, self))


def paint_focus_mark(widget: QWidget) -> None:
    """*widget* の左マージンに右向きの ▸ を accent で描く（縦中央）。

    アンチエイリアスに頼らず 1px 幅の縦帯を並べて描く — 5×9 の小さな
    三角形は AA を掛けるとにじんで「点」に見える。1px の下境界線
    （qss の ``#panelHeader``）は避けて中央を取る。
    """
    colour = QColor(focus_band_ink(current_tokens()))
    painter = QPainter(widget)
    height = 2 * FOCUS_MARK_WIDTH - 1
    cy = (widget.height() - 1) // 2  # 下境界線 1px を除いた中央
    top = cy - (height // 2)
    for i in range(FOCUS_MARK_WIDTH):
        painter.fillRect(FOCUS_MARK_X + i, top + i, 1, height - 2 * i, colour)
    painter.end()


class PaneFocusBands(QObject):
    """*targets* のうちフォーカスを内包する最も内側の節の見出しだけを灯す監視役.

    *targets* は ``(scope, header)`` の列 — *scope* はフォーカスを内包するか
    を判定する節（QWidget）、*header* はその節の :class:`FocusBandTarget`。
    ``QApplication.focusChanged`` を 1 本だけ購読し、新しいフォーカス
    ウィジェットの祖先に当たる scope のうち最も深いものの見出しへ帯を
    付け替える。

    **ポップアップ中は据え置く**: メニュー / ``Qt.Popup`` のポップオーバーが
    開くとフォーカスは一時的にそちらへ「重なる」だけで、席を「移った」
    わけではない（``GalleryView._selection_active`` と同じ裁定）。ウィンドウの
    非活性化（``focused=None``）も同様に、最後に灯した帯を残す — 戻った
    ときにフォーカスがどこへ帰るかがそのまま見える。
    """

    def __init__(
        self,
        targets: Sequence[tuple[QWidget, FocusBandTarget]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._targets: list[tuple[QWidget, FocusBandTarget]] = [
            (scope, header) for scope, header in targets if scope is not None
        ]
        self._current: FocusBandTarget | None = None
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        self._sync(QApplication.focusWidget())

    # ------------------------------------------------------------------ API

    def current(self) -> FocusBandTarget | None:
        """いま帯を灯している見出し（無ければ ``None``）。"""
        return self._current

    # ------------------------------------------------------------- internals

    def _on_focus_changed(self, _old, new) -> None:
        self._sync(new)

    def _sync(self, focused: QWidget | None) -> None:
        if focused is None:
            return
        if QApplication.activePopupWidget() is not None or isinstance(focused, QMenu):
            return
        best: tuple[int, FocusBandTarget] | None = None
        for scope, header in self._targets:
            try:
                inside = focused is scope or scope.isAncestorOf(focused)
            except RuntimeError:  # pragma: no cover (defensive — 破棄済み)
                continue
            if not inside:
                continue
            depth = _depth(scope)
            if best is None or depth > best[0]:
                best = (depth, header)
        target = None if best is None else best[1]
        if target is self._current:
            return
        if self._current is not None:
            try:
                self._current.set_focus_band(False)
            except RuntimeError:  # pragma: no cover (defensive — 破棄済み)
                pass
        self._current = target
        if target is not None:
            target.set_focus_band(True)


def _depth(widget: QWidget) -> int:
    depth = 0
    parent = widget.parentWidget()
    while parent is not None:
        depth += 1
        parent = parent.parentWidget()
    return depth


__all__ = [
    "FOCUS_MARK_WIDTH",
    "FOCUS_MARK_X",
    "FocusBandMixin",
    "FocusBandTarget",
    "PaneFocusBands",
    "paint_focus_mark",
]
