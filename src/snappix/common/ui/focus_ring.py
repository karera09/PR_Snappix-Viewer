"""ペイン席の「いまフォーカスがどこにあるか」を細枠で示す共通部品.

UIレビュー 2026-08-28 N-26 案B。ショートカット表の 6 通りの文脈分岐
（グリッド / 一覧 / プレビュー / 入力欄 …）は、文言をどれだけ正確に書いても
**画面のどこにフォーカスがあるかが読めない**限り「効いたり効かなかったり
する」ままになる。QSS の ``:focus`` は QPushButton / QLineEdit / QComboBox の
3 つにしか定義が無く、ペイン席（ナビレール / グリッド / プレビュー列 /
情報パネル）には一切無かった（07-25 #47 の残課題）。

**なぜ QSS ではなくオーバーレイか**: フォーカスを実際に受け取るのは席そのもの
ではなく席の**中の**ウィジェット（``GalleryView`` / ``QListWidget`` /
``ImageView`` …）で、Qt の QSS には ``:focus-within`` に当たるセレクタが無い。
かといって内側のビューごとに ``:focus`` を書くと、席の中のどの部品に入ったか
で枠の出方が変わってしまう（スライダやコンボに入ると枠が消える）。席の上に
枠だけを描く透明な子ウィジェットを載せると、「席の中のどこかにフォーカスが
ある」を 1 つの見え方で表せる。

色は必ずトークン経由（``current_tokens().accent`` — docs/claude/design.md の
「色をハードコードしない」規約）。枠はレイアウトに参加しないオーバーレイ
なので、太さが何 px でも席の内容は 1px も動かない（フォーカス往復で中身が
ガタつかない）。
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from .theme import current_tokens

#: 枠線の太さ（px）と accent のアルファ（0-255）。
#:
#: 当初は「補助情報だから弱く」で 1px・α40 にしていたが、それでは
#: **席地色とのコントラストが 1.1〜1.3:1** にしかならず（accent を席地へ α40
#: で合成した実効色。WCAG 1.4.11 の非テキスト 3:1 を全テーマで下回る）、
#: 枠が出ているかどうかを目で判定できなかった。α を落として弱さを表現する
#: のではなく、**不透明 accent の細枠**（2px）で「見えるが主張しない」を
#: 満たす。枠はレイアウトに参加しない子ウィジェットなので、太さを変えても
#: 席の内容は 1px も動かない。
FOCUS_RING_WIDTH = 2
FOCUS_RING_ALPHA = 255


class _FocusRing(QWidget):
    """席の上に枠だけを描く、当たり判定を持たない子ウィジェット。"""

    def __init__(self, seat: QWidget) -> None:
        super().__init__(seat)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        colour = QColor(current_tokens().accent)
        colour.setAlpha(FOCUS_RING_ALPHA)
        painter = QPainter(self)
        pen = QPen(colour)
        pen.setWidth(FOCUS_RING_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        inset = FOCUS_RING_WIDTH - 1
        painter.drawRect(
            self.rect().adjusted(inset, inset, -FOCUS_RING_WIDTH, -FOCUS_RING_WIDTH)
        )


class PaneFocusRings(QObject):
    """*seats* のうちフォーカスを内包する 1 つにだけ枠を出す監視役。

    ``QApplication.focusChanged`` を 1 本だけ購読し、新しいフォーカス
    ウィジェットの祖先に当たる席を探して枠を付け替える。席が隠れている /
    幅 0 に畳まれている場合は枠も見えないだけで、状態は持たない。

    席のリサイズ・表示は ``eventFilter`` で追う（席は再親付けされない前提 —
    ビューアのレイアウトはウィンドウ構築時の一度きりで席を組む）。
    """

    def __init__(self, seats: Sequence[QWidget], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rings: dict[QWidget, _FocusRing] = {}
        for seat in seats:
            if seat is None:
                continue
            self._rings[seat] = _FocusRing(seat)
            seat.installEventFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        self._sync(QApplication.focusWidget())

    # ------------------------------------------------------------- internals

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt API)
        if event.type() in (QEvent.Resize, QEvent.Show):
            ring = self._rings.get(obj)
            if ring is not None and ring.isVisible():
                self._place(obj, ring)
        return False

    def _on_focus_changed(self, _old, new) -> None:
        self._sync(new)

    def _sync(self, focused: QWidget | None) -> None:
        for seat, ring in self._rings.items():
            try:
                inside = focused is not None and (
                    focused is seat or seat.isAncestorOf(focused)
                )
            except RuntimeError:  # pragma: no cover (defensive — 破棄済み)
                continue
            if inside:
                self._place(seat, ring)
                ring.show()
                ring.raise_()
            else:
                ring.hide()

    @staticmethod
    def _place(seat: QWidget, ring: _FocusRing) -> None:
        ring.setGeometry(seat.rect())


__all__ = ["PaneFocusRings", "FOCUS_RING_ALPHA", "FOCUS_RING_WIDTH"]
