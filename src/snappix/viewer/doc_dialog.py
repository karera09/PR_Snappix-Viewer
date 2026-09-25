"""Modeless in-app reader for a shipped Markdown document.

Every other shipped document (利用規約・免責事項 / THIRD_PARTY_LICENSES /
はじめにお読みください) is plain ``.txt``, which Windows always knows how to
open, so :func:`snappix.common.legal_docs.open_shipped_file` hands those to the
OS.  The AI pack's setup guide is the one ``.md`` in the product, and a bare
Windows install has **no** association for that extension — the Help entry
「AIタグ検索のセットアップ…」 would otherwise end at an "how do you want to open
this file?" shell prompt, or at nothing at all.

Rendering it here keeps the heading structure the guide is written with (the
alternative — shipping it as ``.txt`` — flattens it) and costs no new
dependency: ``markdown_it`` is already a viewer dependency.

Deliberately thin next to :mod:`snappix.viewer.markdown_view`: that one exists
for ``post.md`` and carries thumbnail resolution, decode workers and link
rewriting.  A shipped document needs none of it, so this dialog is a
``QTextBrowser`` plus one render — no timers, no threads, nothing for
``closeEvent`` to stop.
"""

from __future__ import annotations

from pathlib import Path

import markdown_it
from loguru import logger
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import current_tokens, localize_buttons, rgba

#: Opening size.  Wide enough that the guide's code lines (model folder paths)
#: do not wrap, tall enough to show a section at a time.
_DEFAULT_WIDTH = 760
_DEFAULT_HEIGHT = 680


def _render(markdown_text: str) -> str:
    """Markdown → HTML for ``QTextBrowser`` (design tokens, no colour literals).

    A fresh ``MarkdownIt`` per call: rendering happens once when the dialog
    opens, so there is nothing to amortise, and no instance is shared across
    threads (``render`` mutates instance state — the reason
    ``markdown_view`` keeps a per-thread one).  ``html=False`` keeps raw HTML in
    the document inert.
    """
    tokens = current_tokens()
    body = markdown_it.MarkdownIt("commonmark", {"html": False}).render(markdown_text)
    return (
        "<style>"
        f"body {{ color: {tokens.text}; }}"
        f"h1, h2, h3 {{ color: {tokens.text}; }}"
        f"a {{ color: {tokens.accent}; }}"
        f"code {{ background-color: {rgba(tokens.text_muted, 0.15)}; }}"
        f"pre {{ background-color: {rgba(tokens.text_muted, 0.10)}; }}"
        "</style>"
        f"{body}"
    )


class DocDialog(QDialog):
    """Read-only Markdown reader, modeless like :class:`ShortcutsDialog`."""

    def __init__(
        self,
        markdown_text: str,
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)

        self._markdown = markdown_text
        outer = QVBoxLayout(self)
        self._view = QTextBrowser(self)
        # 文中のリンクは外部サイト（モデル配布元など）— アプリ内で辿らせず
        # OS の既定ブラウザへ渡す。
        self._view.setOpenExternalLinks(True)
        self._view.setHtml(_render(markdown_text))
        outer.addWidget(self._view, 1)

        buttons = localize_buttons(
            QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        )
        # Close は RejectRole なので rejected() が出る（clicked を重ねない）。
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)

    def document_text(self) -> str:
        """The rendered document as plain text (tests / assistive tooling)."""
        return self._view.toPlainText()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        """テーマ切替で本文を描き直す（:class:`ShortcutsDialog` と同じ手当て）.

        トークン色は :func:`_render` が HTML へ**焼き込む**のに対し、
        ``QTextBrowser`` の地の色はパレット由来なので、モードレスなこの窓を
        開いたまま表示 ▸ テーマを切り替えると地だけが塗り替わり、見出しが
        背景と同化する。描き直しは Markdown 1 本のレンダなので軽い —
        スクロール位置だけ前後で持ち越す。
        """
        super().changeEvent(event)
        if event.type() not in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ThemeChange,
        ):
            return
        # ``__init__`` の途中でも届きうる（QDialog の初期化中）ので存在を確認。
        view = getattr(self, "_view", None)
        if view is None:
            return
        bar = view.verticalScrollBar()
        at = bar.value()
        view.setHtml(_render(self._markdown))
        bar.setValue(at)


def show_markdown_doc(
    path: Path | None,
    parent: QWidget | None,
    *,
    title: str,
    missing_name: str,
) -> DocDialog | None:
    """Open *path* in a :class:`DocDialog`; returns the dialog, or ``None``.

    Degrades exactly like :func:`snappix.common.legal_docs.open_shipped_file`:
    a missing file (mangled install) or an unregistered provider (``path`` is
    ``None``) shows the same 「ファイルが見つかりません」 guidance naming
    *missing_name*, so the two doc openers answer the same way.  An unreadable
    file takes the same branch — from the user's seat it is the same failure.
    """
    text: str | None = None
    if path is not None and path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("could not read the shipped document {}: {}", path, exc)
    if text is None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(
            parent,
            t("common.legal.file_missing_title"),
            t("common.legal.file_missing_body", name=missing_name),
        )
        return None
    dlg = DocDialog(text, title, parent)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


__all__ = ["DocDialog", "show_markdown_doc"]
