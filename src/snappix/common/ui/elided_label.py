"""省略表示ラベル（デザインシステム共有部品 — 項目#195）.

「_full 保持 / minimumSizeHint=(0, fm.height()) / paintEvent で elidedText」
の同型実装がリポジトリに 3 つあった（viewer の ``_ElidedLabel`` / AI プラグ
インの ``_ElidedPathLabel`` / tagger の ``ElidingLabel``）。修正が 1 コピー
にしか当たらない事故（#141: ツールチップ欠落が 1 実装だけ直った）を繰り返さ
ないため、``snappix`` を import できる 2 本（viewer 本体・AI プラグインの
ビューア側）はこの 1 実装へ寄せる。tagger サブツリーは snappix 非依存の
鉄則があるため自前コピーを維持する（tagger/_widgets.py::ElidingLabel）。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QWidget


class ElidedLabel(QLabel):
    """QLabel that elides its text to the available width.

    A plain wordWrap=False QLabel reports ``minimumSizeHint`` equal to the
    full text width, which propagates into the containing layout / splitter
    pane and forces it to widen whenever the shown text (a long path, a file
    name) is long.  We store the full text, report a tiny minimum width, and
    paint an elided form based on the current widget width.

    * フルテキストは**常に**ツールチップに出す（初期テキスト・``setText``
      とも — #141 でコピーごとに直ったり直らなかったりした点を契約にする）。
      呼び出し側が ``setToolTip`` で上書きするのは自由（例: 表示はファイル名・
      ツールチップはフルパス）。
    * ``TextSelectableByMouse`` は立てない（項目#195 — paintEvent を全面
      上書きしているため選択ハイライトが描画されず、「選べるのに見えない」
      振る舞いになっていた。見た目を優先し選択は外す）。
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        elide: Qt.TextElideMode = Qt.ElideMiddle,
    ) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._elide = elide
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        text = text or ""
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self.updateGeometry()
        self.update()

    def text(self) -> str:  # type: ignore[override]
        return self._full_text

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, self.fontMetrics().height())

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, self.fontMetrics().height())

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, self._elide, self.width())
        painter.drawText(self.rect(), int(self.alignment()), elided)


__all__ = ["ElidedLabel"]
