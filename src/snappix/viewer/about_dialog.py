"""Dedicated "Snappix Viewer について" dialog.

Why not ``QMessageBox.about``: a plain message box has no room for anything
beyond a title + one text blob, so About would be the only read-only dialog
in the app whose sole button reads "OK" instead of the 「閉じる」 every other
view-only dialog uses, and it would offer no way to reach the 利用規約 /
サードパーティライセンス without hunting the Help menu.

Product decision: a dedicated ``QDialog``, **no logo** —
the large app-name label (``FONT_TITLE_PT``) carries the "brand" weight
instead of an icon.  The copyright line is deliberately just the product
name (``viewer.about_dialog.copyright``): ``terms_text.py`` documents that
the author's identity is intentionally kept out of every shipped document
(communicated through the sales channel instead), so this dialog follows the
same convention rather than inventing a personal name to display.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .. import __version__
from ..common.i18n import t
from ..common.legal_docs import open_third_party_licenses
from ..common.terms_dialog import show_terms
from ..common.ui import (
    FONT_TITLE_PT,
    demote_close_default,
    hint_style,
    localize_buttons,
)
from . import ai_pack


class AboutDialog(QDialog):
    """Read-only "Snappix Viewer について" dialog."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.main_window.about_title"))
        # 既定サイズは本文の出し分け（下の ``body_key``）に合わせる。
        # 固定 460x380 では素の配布で約 175px、AI パック有効でも約 100px の
        # 空白が下半分に残る。``detail_window.py`` の
        # ``resize(420, 720 if self._ai_ui else 260)`` と同じく「素の配布は
        # 内容が半分程度なので既定サイズも詰める」。
        self.resize(460, 340 if ai_pack.available() else 280)

        layout = QVBoxLayout(self)

        # Reuses the window-title string (「Snappix Viewer」) rather than a new
        # key — same wording, no catalog duplicate (tests/test_i18n.py guards
        # against re-minted duplicate values).
        name_label = QLabel(t("viewer.main_window.window_title"), self)
        name_label.setStyleSheet(
            f"font-size: {FONT_TITLE_PT}pt; font-weight: bold;"
        )
        layout.addWidget(name_label)

        version_label = QLabel(
            t("viewer.about_dialog.version", version=__version__), self
        )
        version_label.setStyleSheet(hint_style())
        layout.addWidget(version_label)

        copyright_label = QLabel(t("viewer.about_dialog.copyright"), self)
        copyright_label.setStyleSheet(hint_style())
        layout.addWidget(copyright_label)

        # 素の配布（AI パック無効）では tagger / AI 検索に触れない本文を出す —
        # 旧 QMessageBox.about が持っていた出し分けロジックをそのまま維持。
        body_key = (
            "viewer.main_window.about_body"
            if ai_pack.available()
            else "viewer.main_window.about_body_free"
        )
        # The header rows above already carry the app name + version, so the
        # body strings start straight at the product description (a leading
        # "Snappix Viewer バージョン …" line here would duplicate them).
        body_label = QLabel(t(body_key), self)
        body_label.setWordWrap(True)
        layout.addWidget(body_label)

        layout.addStretch(1)

        link_row = QHBoxLayout()
        terms_btn = QPushButton(t("viewer.about_dialog.show_terms"), self)
        terms_btn.clicked.connect(lambda: show_terms(self))
        # 閲覧専用ダイアログに主要アクションは無い。
        # Qt は最初の QPushButton を implicit default（アクセント色）にするため
        # 2 つのリンクボタンとも明示的に外す。
        terms_btn.setAutoDefault(False)
        terms_btn.setDefault(False)
        link_row.addWidget(terms_btn)

        # Reuses the Help-menu entry's own wording (same action, same label —
        # no catalog duplicate; see the app-name label above for the same
        # reasoning).
        licenses_btn = QPushButton(t("common.legal.third_party_menu"), self)
        licenses_btn.clicked.connect(lambda: open_third_party_licenses(self))
        licenses_btn.setAutoDefault(False)
        licenses_btn.setDefault(False)
        link_row.addWidget(licenses_btn)
        link_row.addStretch(1)
        layout.addLayout(link_row)

        buttons = localize_buttons(QDialogButtonBox(QDialogButtonBox.Close, self))
        buttons.rejected.connect(self.reject)
        # 閲覧専用ダイアログの唯一のボタン — 単一ボタンだと
        # Qt が implicit default（アクセント色）にしてしまうため明示的に外す。
        demote_close_default(buttons)
        layout.addWidget(buttons)


def show_about(parent=None) -> None:
    """Show the About dialog modally (Help menu entry point).

    ``exec()`` だけでは親（ViewerWindow）が C++ 側でダイアログを所有し続け、
    Python 参照が落ちても破棄されない — ヘルプ ▸ バージョン情報… を開くたびに
    ラベル・ボタン一式が終了までリークする。呼び出し側の他ダイアログと同じく
    exec 後に deleteLater する。
    """
    dlg = AboutDialog(parent)
    dlg.exec()
    dlg.deleteLater()
