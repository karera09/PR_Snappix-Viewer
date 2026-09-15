"""First-run consent dialog for the snappix terms (利用規約・免責事項).

Shown once, before the main window, by both GUI entry points via
``common/terms.py::require_consent``.  The gate turns the shipped terms into
a click-through agreement: the 「同意する」 button unlocks only after the user
has scrolled the full text to the bottom, and declining exits the app.

When a *previous* acceptance is on file for an older terms version (a terms
revision invalidated it — see ``terms.py::previously_accepted_version``), the
dialog swaps its introduction for a "the terms changed" framing instead of
the first-run one, so a returning user doesn't read the re-prompt as a bug
(#42, UIレビュー 07-25).

PySide6 is imported at module top here (unlike ``common/terms.py``, which
stays Qt-free so GUI-less entry points can import
``has_accepted`` without pulling in Qt) — this module is only ever imported
lazily from ``require_consent`` inside a GUI process, after ``apply_theme``
has themed the running ``QApplication``.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from .i18n import t
from .terms_text import TERMS_TEXT, TERMS_VERSION
from .ui import demote_close_default, hint_style

# (UIレビュー07-25 追修) 以前はここで ``t()`` を呼んで訳文そのものを定数に
# 焼いていた。import 時に確定してしまうため、``set_locale`` より先にこの
# モジュールが読み込まれると全文言が既定ロケールのまま固まる（今は
# ``require_consent`` が遅延 import するので事故っていないだけ）。他の
# ダイアログと同じくウィジェット構築時に引くようキーだけを持つ。
#: Window title shared by both dialogs — must match the document heading in
#: ``terms_text.py`` (the product name, not the package name).
_WINDOW_TITLE_KEY = "common.terms_dialog.window_title"
_INTRO_KEY = "common.terms_dialog.intro"
_HINT_BEFORE_KEY = "common.terms_dialog.hint_before"
_HINT_AFTER_KEY = "common.terms_dialog.hint_after"
_DECLINED_NOTICE_KEY = "common.terms_dialog.declined_notice"


def _format_version_date(version: str) -> str:
    """``"2026-07-20"`` → ``"2026年7月20日"``; falls back to *version* as-is.

    ``TERMS_VERSION`` is an ISO date by convention (see ``terms_text.py``),
    matching the revision-history footer's own dates written in this same
    Japanese format. A malformed version string (should not happen, but this
    is a consent gate — never let formatting throw) degrades to showing the
    raw string rather than crashing the dialog.
    """
    try:
        d = datetime.strptime(version, "%Y-%m-%d")
    except ValueError:
        return version
    return f"{d.year}年{d.month}月{d.day}日"


def _terms_text_edit(text: str) -> QTextEdit:
    """Read-only ``QTextEdit`` rendering *text* with normal paragraph rhythm.

    ``QPlainTextEdit`` (the previous widget here) renders every block with
    identical zero spacing, so the terms' numbered clauses ran straight into
    each other with no visual paragraph break and wrapped continuation lines
    had no consistent indent (UIレビュー 07-25 #101). A per-block
    ``QTextBlockFormat`` restores normal spacing between paragraphs;
    ``setTextIndent(0)``/``setIndent(0)`` keep wrapped lines flush left. The
    text itself is unchanged plain text (``setPlainText``, no markup) — only
    presentation changes, so ``TERMS_VERSION`` stays untouched.
    """
    view = QTextEdit()
    view.setReadOnly(True)
    view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
    view.setPlainText(text)
    cursor = QTextCursor(view.document())
    cursor.select(QTextCursor.SelectionType.Document)
    block_format = QTextBlockFormat()
    block_format.setTopMargin(6)
    block_format.setTextIndent(0)
    block_format.setIndent(0)
    cursor.mergeBlockFormat(block_format)
    return view


class ConsentDialog(QDialog):
    """Modal terms dialog whose accept button unlocks after a full scroll."""

    def __init__(
        self, parent=None, *, previous_version: str | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t(_WINDOW_TITLE_KEY))
        self.setModal(True)
        self.resize(680, 620)

        # #42: a *previous* acceptance on file for a different version means
        # this is a re-prompt caused by a terms revision, not a first run —
        # ``require_consent`` only reaches the dialog at all when the current
        # version isn't already accepted, so any non-``None`` previous
        # version here is necessarily a mismatch (never re-shows the plain
        # first-run framing for someone who just agreed).
        self._is_revision = previous_version is not None

        layout = QVBoxLayout(self)

        intro_text = (
            t(
                "common.terms_dialog.intro_revised",
                date=_format_version_date(TERMS_VERSION),
            )
            if self._is_revision
            else t(_INTRO_KEY)
        )
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ヘッダに版表示（#42 改善案の後半）: いま提示している規約の版を明示
        # する。改定の再プロンプトでは「何が改定されたか」の手がかりになり、
        # 初回でも「何の版に同意したか」の記録になる — 同意を求める面だけが
        # 版を出さないのは筋が通らない（UIレビュー 2026-09-11 N-112。読み返す
        # 面 ``TermsViewDialog`` は既に無条件で出している）。
        version_label = QLabel(
            t("common.terms_dialog.version_label", version=TERMS_VERSION)
        )
        version_label.setStyleSheet(hint_style())
        layout.addWidget(version_label)

        self._view = _terms_text_edit(TERMS_TEXT)
        layout.addWidget(self._view, 1)

        self._hint = QLabel(t(_HINT_BEFORE_KEY))
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        buttons = QDialogButtonBox()
        self._agree = QPushButton(t("common.terms_dialog.agree_btn"))
        self._decline = QPushButton(t("common.terms_dialog.decline_btn"))
        buttons.addButton(self._agree, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self._decline, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Gate agreement on having scrolled to the bottom (a genuine
        # "read the whole thing" click-through).  Declining is always allowed,
        # but never as a *default*: until 同意する unlocks, a reflexive Enter
        # must be a no-op rather than silently quitting the app (UIレビュー #1
        # — the disabled 同意する soaks up the default role, so Enter does
        # nothing until the scroll gate opens and flips the default onto it).
        self._agree.setEnabled(False)
        self._agree.setDefault(True)
        self._decline.setDefault(False)
        self._decline.setAutoDefault(False)
        bar = self._view.verticalScrollBar()
        bar.valueChanged.connect(self._maybe_enable)
        # rangeChanged covers the no-scroll paths valueChanged never fires on:
        # resizing the dialog tall enough that the whole text fits (maximum
        # drops to 0 without a value change) must unlock 同意する too.
        bar.rangeChanged.connect(self._maybe_enable)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        # After layout, a short document may not need scrolling at all — enable
        # immediately in that case so the user is not stuck.
        self._maybe_enable()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        # A reflexive Esc (the "close this popup" instinct) must not silently
        # quit the app — on first run no window has been shown yet, so the
        # standard QDialog Esc→reject() made the exit look like a crash
        # (UIレビュー #1).  Declining stays an explicit button choice.
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)

    def _maybe_enable(self, *_args) -> None:
        bar = self._view.verticalScrollBar()
        at_bottom = bar.maximum() == 0 or bar.value() >= bar.maximum() - 4
        if at_bottom and not self._agree.isEnabled():
            self._agree.setEnabled(True)
            self._hint.setText(t(_HINT_AFTER_KEY))
            # Now that 同意する is a valid choice, make it the default and move
            # focus onto it so a natural Enter after reading agrees rather than
            # quitting the app (A03).  Until this point 同意しない stayed the
            # default because Enter on a disabled 同意する would be a no-op.
            self._agree.setDefault(True)
            self._decline.setDefault(False)
            self._agree.setFocus()
            return
        if self._agree.isEnabled():
            # ゲートは一方通行 — 解放後に読み返しても進捗表示へ戻さない。
            return
        if bar.maximum() > 0:
            # (UIレビュー 2026-09-11 N-96b) 「最後までスクロールすると押せる」
            # とだけ言われる面には、どれだけ読んだかを示すものが何も無く、
            # 長い規約では「まだなのか / もう終わりなのか」が分からなかった。
            # スクロールバーの位置をそのまま割合として添える（新しい状態は
            # 持たない — 表示はバーの現在値から毎回導く）。
            pct = min(100, max(0, round(bar.value() / bar.maximum() * 100)))
            self._hint.setText(
                t("common.terms_dialog.hint_progress", pct=pct)
            )


class TermsViewDialog(QDialog):
    """Read-only terms viewer for the Help menus — no consent semantics.

    Lets users re-read the 利用規約・免責事項 at any time after the first-run
    agreement (the same text also ships next to the EXE as
    ``利用規約・免責事項.txt``).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t(_WINDOW_TITLE_KEY))
        self.resize(680, 620)

        layout = QVBoxLayout(self)

        # いま表示している規約の版 (UIレビュー 2026-08-28 N-122)。改定の
        # 再プロンプト（``TermsDialog``）にだけ出ていて、あとから読み返す面に
        # 版がどこにも出ていなかった — 同じ i18n キー・同じ体裁で揃える。
        version_label = QLabel(
            t("common.terms_dialog.version_label", version=TERMS_VERSION)
        )
        version_label.setStyleSheet(hint_style())
        layout.addWidget(version_label)

        view = _terms_text_edit(TERMS_TEXT)
        layout.addWidget(view, 1)

        buttons = QDialogButtonBox()
        close_btn = QPushButton(t("common.action.close"))
        buttons.addButton(close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        # UIレビュー #4: 閲覧専用ダイアログの唯一のボタン — Qt は単一ボタンの
        # ダイアログで自動的に default 扱い（アクセント色）にしてしまうため、
        # 「閉じる」が推奨アクションに見えないよう明示的に外す。
        demote_close_default(buttons)
        layout.addWidget(buttons)


def show_terms(parent=None) -> None:
    """Show the read-only terms viewer modally (Help menu entry point).

    The dialog is destroyed after ``exec`` — a parented QDialog outlives the
    local reference (the C++ side stays owned by *parent*), so without this
    every ヘルプ ▸ 利用規約 open would leave another copy of the full terms text
    (a QTextDocument each) under the window until the app quits (#180).
    """
    dlg = TermsViewDialog(parent)
    try:
        dlg.exec()
    finally:
        dlg.deleteLater()


def prompt_consent(parent=None, *, previous_version: str | None = None) -> bool:
    """Show the consent dialog modally; return ``True`` iff the user agreed.

    ``previous_version`` (from ``terms.py::previously_accepted_version``) is
    ``None`` on a genuine first run, or the stale accepted version when this
    prompt was triggered by a terms revision — see ``ConsentDialog`` (#42).
    """
    dialog = ConsentDialog(parent, previous_version=previous_version)
    return dialog.exec() == QDialog.DialogCode.Accepted


def notify_declined(parent=None) -> None:
    """One-line farewell after a declined consent, before the process exits.

    Without it the app just vanishes (no window was ever shown), which reads
    as a crash — especially via the title-bar ✕, the one remaining
    non-button way to decline (UIレビュー #1).
    """
    QMessageBox.information(
        parent, t(_WINDOW_TITLE_KEY), t(_DECLINED_NOTICE_KEY),
    )
