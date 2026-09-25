"""Dialog button helpers: localised labels and verb-labelled confirmations.

Two layers, because Qt's own translations and our catalog answer different
questions:

* **Qt's ``qtbase_ja.qm``** (installed at startup — see
  :mod:`snappix.common.qt_i18n`) translates every string Qt draws itself:
  standard buttons, ``QInputDialog`` の OK/Cancel.
  It *is* shipped, so dialogs do not need to rename Qt's standard buttons
  by hand merely to get Japanese labels.
* **This module** keeps the wording *ours* where the wording matters.
  :func:`localize_buttons` still renames a ``QDialogButtonBox``'s standard
  buttons from the ``t()`` catalog so 「閉じる」/「既定値に戻す」 read the same
  on every screen regardless of Qt's own phrasing (the catalog remains the
  single source of truth for 表記揺れ guarding).  :func:`confirm_action`
  goes further for **irreversible or consequential** modals: 「はい」/「いいえ」
  says nothing about *what* is being agreed to, so those get **verb** labels
  (「完全に削除する」/「有効化する」) built the ``addButton`` way.

Extend :data:`_STANDARD_LABELS` when a dialog starts using a new standard
button; unknown buttons are left untouched (Qt's translated text).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ..i18n import t

#: StandardButton → catalog key for every standard button the app uses.
_STANDARD_LABELS: dict[QDialogButtonBox.StandardButton, str] = {
    QDialogButtonBox.StandardButton.Ok: "common.action.ok",
    QDialogButtonBox.StandardButton.Cancel: "common.action.cancel",
    QDialogButtonBox.StandardButton.Close: "common.action.close",
    QDialogButtonBox.StandardButton.RestoreDefaults: (
        "common.action.restore_defaults"
    ),
}


def localize_buttons(box: QDialogButtonBox) -> QDialogButtonBox:
    """Rename *box*'s standard buttons from the catalog; returns *box*.

    Chainable at construction time::

        btns = localize_buttons(QDialogButtonBox(QDialogButtonBox.Close))
    """
    for standard, key in _STANDARD_LABELS.items():
        btn = box.button(standard)
        if btn is not None:
            btn.setText(t(key))
    return box


def localize_input_dialog(dlg: QInputDialog) -> QInputDialog:
    """Give a ``QInputDialog``'s OK / Cancel our catalog wording; returns *dlg*.

    ``QInputDialog`` builds its own button box internally, so
    :func:`localize_buttons` cannot reach it — Qt exposes the two setters
    used here instead.  Qt's ``qtbase_ja.qm`` already translates them; this
    keeps them worded identically to every other 「OK」/「キャンセル」 in the app
    (and keeps working if a future locale ships no Qt catalog).
    """
    dlg.setOkButtonText(t("common.action.ok"))
    dlg.setCancelButtonText(t("common.action.cancel"))
    return dlg


def confirm_action(
    parent: QWidget | None,
    *,
    title: str,
    body: str,
    accept_text: str,
    reject_text: str | None = None,
    informative: str | None = None,
    icon: QMessageBox.Icon = QMessageBox.Icon.Question,
    destructive: bool = False,
    plain_text: bool = False,
) -> bool:
    """Ask a yes/no question with **verb** buttons; ``True`` when accepted.

    Replaces ``QMessageBox.question(..., Yes | No)`` on every modal whose
    answer is irreversible (削除) or consequential (プラグインの信頼 =
    任意コード実行への同意).  Qt's translated 「はい」/「いいえ」 are grammatical
    but content-free: the user has to hold the question in their head to know
    what 「はい」 does, and the question is exactly what an alarmed user skims
    past.  *accept_text* therefore names the action (「完全に削除する」/
    「有効化する」) — the ``addButton`` idiom already used by
    ``main_window.py`` の folder-not-found modal と
    ``cache_build_controller.py`` の build-mode modal, unified here so new
    call sites stop re-deriving it.

    * *reject_text* defaults to the catalog's 「キャンセル」.
    * The **reject** button is the default (Enter / Escape both decline), so
      a reflexive Enter can never confirm a destructive action.
    * *destructive* files the accept button under ``DestructiveRole`` **and**
      gives it the ``destructiveButton`` objectName, so the danger styling in
      ``qss.py`` lands on every such modal without each call site re-deriving
      the marker.
    * *plain_text* forces ``Qt.PlainText`` — required whenever *body* embeds
      untrusted text (plugin manifest fields), which ``AutoText`` would
      otherwise render as HTML.

    Closing the modal with Escape / ✕ returns ``False`` (no button clicked).
    """
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(body)
    if informative:
        box.setInformativeText(informative)
    if plain_text:
        box.setTextFormat(Qt.TextFormat.PlainText)
    accept_role = (
        QMessageBox.ButtonRole.DestructiveRole
        if destructive
        else QMessageBox.ButtonRole.AcceptRole
    )
    accept_btn = box.addButton(accept_text, accept_role)
    if destructive:
        # 危険スタイル (qss.py の ``QPushButton#destructiveButton``) は
        # objectName マーカーでしか効かない。``destructive=True`` と言った
        # 呼び出しが赤くなるかどうかを各ダイアログの自前 setObjectName に
        # 委ねていると、付け忘れた面だけ「ただの確認」に見える。
        accept_btn.setObjectName("destructiveButton")
    reject_btn = box.addButton(
        reject_text if reject_text is not None else t("common.action.cancel"),
        QMessageBox.ButtonRole.RejectRole,
    )
    box.setDefaultButton(reject_btn)
    box.setEscapeButton(reject_btn)
    box.exec()
    accepted = box.clickedButton() is accept_btn
    # A parented QMessageBox stays owned by its parent after exec() —
    # release it explicitly so repeated confirmations don't pile up.
    box.deleteLater()
    return accepted


def warn_modal(
    parent: QWidget | None,
    *,
    title: str,
    body: str,
    plain_text: bool = True,
) -> None:
    """Show a one-button warning modal whose *body* may embed untrusted text.

    The acknowledge-only sibling of :func:`confirm_action`, for the 「失敗 =
    モーダル」 notices that carry strings the app did not author (plugin
    manifest fields, a plugin's own exception message, a folder name on disk):

    * *plain_text* (default) forces ``Qt.PlainText`` — ``AutoText`` would let
      the embedded string decide it is HTML and re-render the very warning
      that is reporting it.
    * The box is **parented** (so it centres on the window and stays modal to
      it) and released with ``deleteLater`` after ``exec`` returns — a
      parented ``QMessageBox`` otherwise lives on as a hidden child for the
      window's whole lifetime, once per notice.  ``exec`` is not
      wrapped in ``WA_DeleteOnClose``: that frees the C++ object inside the
      close signal's own stack, which aborts the process.

    Callers that need a yes/no answer use :func:`confirm_action` instead.
    """
    box = QMessageBox(
        QMessageBox.Icon.Warning, title, body, QMessageBox.StandardButton.Ok,
        parent,
    )
    if plain_text:
        box.setTextFormat(Qt.TextFormat.PlainText)
    try:
        box.exec()
    finally:
        box.deleteLater()


#: Roles that represent a dialog's primary/affirmative action (the "OK" of an
#: OK/Cancel pair) — the one case where an accent-coloured default button is
#: the correct read, so these are deliberately left untouched.
_AFFIRMATIVE_ROLES = frozenset(
    {
        QDialogButtonBox.ButtonRole.AcceptRole,
        QDialogButtonBox.ButtonRole.YesRole,
    }
)


def demote_close_default(box: QDialogButtonBox) -> QDialogButtonBox:
    """Stop a terminal-only button box from picking up Qt's implicit default.

    Qt makes a lone ``QPushButton`` in a dialog the *default* button (styled
    via ``QPushButton:default`` in ``qss.py`` — accent background) even when
    it is a plain "閉じる" with no primary action to recommend. On a
    view-only dialog (規約表示 / ショートカット一覧 / タグ一覧 / 詳細情報 /
    健全性チェック / パフォーマンス統計, …) that makes 「閉じる」 read as the
    CTA, which contradicts ``accent``'s role (「選択・フォーカス・主要アクショ
    ン」のみ — design.md). Call this right after building a button box that
    carries **no affirmative action** (no ``AcceptRole``/``YesRole`` button) —
    every other button (Close/Reject, Action, Help, …) has its ``default``/
    ``autoDefault`` cleared so none of them can render as the recommended
    action.

    Dialogs with a real OK/Cancel pair must **not** call this — an
    ``AcceptRole``/``YesRole`` button already present in *box* is left alone,
    so its default/accent styling is unaffected either way; this helper is
    only meant for boxes that have no such button in the first place.

    Escape and an explicit button click still close the dialog as normal —
    only the "pressing Enter with no field focused triggers Close" shortcut
    is intentionally given up.
    """
    for btn in box.buttons():
        if box.buttonRole(btn) in _AFFIRMATIVE_ROLES:
            continue
        btn.setAutoDefault(False)
        btn.setDefault(False)
    return box


def demote_all_defaults(*buttons: QPushButton) -> None:
    """The hand-rolled-button sibling of :func:`demote_close_default`.

    A dialog that lays its buttons out itself (no ``QDialogButtonBox``) still
    gets Qt's implicit default: the **first** ``QPushButton`` in the dialog
    becomes the Enter target and renders with ``QPushButton:default``'s accent
    fill.  When that first button is a destructive one (健全性チェックの一括
    削除列), Enter with nothing focused aims at 削除 and the button competes
    with the danger styling for the same pixels — ``#destructiveButton`` wins
    the colour on specificity, so the read is "red button that is also the
    recommended action".

    Pass every ``QPushButton`` of such a dialog; none of them then claims the
    default.  Escape and explicit clicks are unaffected — only "Enter with no
    field focused" is given up, deliberately (design.md の「既定ボタンは常に
    拒否側」).
    """
    for btn in buttons:
        btn.setAutoDefault(False)
        btn.setDefault(False)
