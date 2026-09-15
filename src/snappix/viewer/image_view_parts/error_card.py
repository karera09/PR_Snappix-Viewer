"""デコード失敗の面（``ImageView`` のビューポートを覆うカード）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...common.i18n import t
from ...common.ui import EmptyStateCard


class _DecodeErrorCard(QWidget):
    """デコード失敗の EmptyStateCard 面。

    以前は中央のラベルに「画像の読み込みに失敗: 名前」の素テキストが 1 行
    出るだけで、他の失敗面（スキャン失敗・空フォルダ・検索 0 件）が使っている
    カード規格から外れているうえに**再試行の手段が無かった** — 一時的な NAS
    断や書き込み途中のファイルでも、選び直すまで復帰できない。ビューポートを
    覆うカードにして [再読み込み] と [既定アプリで開く] を添える。

    ImageView の中に置くので、中央プレビューでも閲覧モードでも同じ面が出る。
    """

    reload_requested = Signal()
    open_default_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = EmptyStateCard(
            t("viewer.content_view.image_error_heading"),
            icon_name="alert-triangle",
            emphasis="onboarding",
        )
        reload_btn = self._card.add_action(
            # このボタンは「この 1 枚をもう一度デコードする」— F5（フォルダの
            # 再読み込み / ランダム並びの再シャッフル）の説明文をラベルへ
            # 流用すると、押した結果と読める文が食い違う。
            t("viewer.image_view.reload_image"), icon_name="refresh",
        )
        reload_btn.clicked.connect(self.reload_requested.emit)
        open_btn = self._card.add_action(
            t("common.action.open_with_default"), icon_name="external-link",
        )
        open_btn.clicked.connect(self.open_default_requested.emit)
        layout.addWidget(self._card)
        self.hide()

    def show_error(self, message: str) -> None:
        """理由（ファイル名 / 例外文言）を本文に出してカードを見せる。"""
        self._card.set_body(message)
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()


__all__ = ["_DecodeErrorCard"]
