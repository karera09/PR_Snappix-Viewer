"""可視領域パッチ描画に対応した画像ラベル（``ImageView`` の中身）。"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QWidget


class _ImageCanvasLabel(QLabel):
    """QLabel + 可視領域パッチ描画モード。

    通常モードでは素の QLabel（setPixmap / QMovie / テキスト）。canvas モード
    では QLabel の描画をバイパスし、(a) 低解像度の全体像 ``base`` をラベル
    全面に、(b) 可視領域の高解像度 ``patch`` を ``patch_rect``（ラベル論理
    座標）に、``paintEvent`` で直接描く。ラベルの論理サイズはピクセル予算と
    無関係に大きくできる — 子ウィジェットは親ウィンドウのバッキングストアを
    共有し、描画コストは露出領域（≒ビューポート）に比例するため、巨大な
    ピクスマップの割付は一切発生しない。

    描画先は必ず ``event.rect()``（露出領域）とし、ソース側を矩形比で対応
    部分へ切り出す。ラベル全面（20x ズームの 8000px 幅なら 160000px）を
    ターゲット矩形として渡すと QPainter のラスタエンジンが持つ ±32k 座標
    制限に抵触し得るためで、露出領域基準ならデバイス座標は常にビューポート
    程度に収まる。パッチと base の解像度はどちらも矩形比で伸縮されるので、
    物理サイズがクランプされたパッチ（8K ビューポート等の縮退時）も正しく
    描ける（単に解像度が下がるだけ）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._canvas_active = False
        self._canvas_base: QPixmap | None = None
        self._canvas_patch: QPixmap | None = None
        self._canvas_patch_rect = QRect()

    def canvas_active(self) -> bool:
        return self._canvas_active

    def set_canvas(self, base: QPixmap | None) -> None:
        """canvas モードへ入り（既に入っていれば下敷きだけ更新）再描画する.

        入場時に QLabel 側のピクスマップを空にする — 直前の全体レンダ
        （最大 100MP ≈ 400MB）を保持し続けないため。パッチが届くまでの
        表示は ``base`` が受け持つので空白は出ない。
        """
        if not self._canvas_active:
            self._canvas_active = True
            super().setPixmap(QPixmap())
            self.setText("")
        self._canvas_base = base
        self.update()

    def set_canvas_patch(self, patch: QPixmap | None, rect: QRect) -> None:
        self._canvas_patch = patch
        self._canvas_patch_rect = QRect(rect)
        self.update()

    def clear_canvas(self) -> None:
        """通常の QLabel 描画へ戻る（非 canvas 中は no-op）."""
        if not self._canvas_active:
            return
        self._canvas_active = False
        self._canvas_base = None
        self._canvas_patch = None
        self._canvas_patch_rect = QRect()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if not self._canvas_active:
            super().paintEvent(event)
            return
        exposed = event.rect().intersected(self.rect())
        if exposed.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w = max(1, self.width())
        h = max(1, self.height())
        patch = self._canvas_patch
        prect = self._canvas_patch_rect
        patch_ok = (
            patch is not None and not patch.isNull()
            and prect.width() > 0 and prect.height() > 0
        )
        base = self._canvas_base
        # 下敷き（低解像度の全体像）: パッチが露出領域を覆い切るときは省く。
        if (
            base is not None and not base.isNull()
            and not (patch_ok and prect.contains(exposed))
        ):
            sx = base.width() / w
            sy = base.height() / h
            painter.drawPixmap(
                QRectF(exposed),
                base,
                QRectF(
                    exposed.x() * sx, exposed.y() * sy,
                    exposed.width() * sx, exposed.height() * sy,
                ),
            )
        if patch_ok:
            inter = exposed.intersected(prect)
            if not inter.isEmpty():
                px = patch.width() / prect.width()
                py = patch.height() / prect.height()
                painter.drawPixmap(
                    QRectF(inter),
                    patch,
                    QRectF(
                        (inter.x() - prect.x()) * px,
                        (inter.y() - prect.y()) * py,
                        inter.width() * px,
                        inter.height() * py,
                    ),
                )
        painter.end()


__all__ = ["_ImageCanvasLabel"]
