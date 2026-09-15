"""プレビュー列の空状態 / 歓迎カード 4 種。

席の裁定（どの面がどのカードを出すか）は :mod:`..empty_state` が持ち、
ここは描画する部品だけを持つ。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...common.i18n import t
from ...common.ui import EmptyStateCard


class _EmptyView(QWidget):
    """プレビュー列の**静音**プレースホルダ — :class:`EmptyStateCard` の
    ``"hint"`` 形（アイコン + 1 行、ボタンなし）.

    既定は「グリッドから選んでください」の未選択ヒント。*heading* /
    *icon_name* を差し替えることで「空フォルダなので選べる物が無い」側の
    静音プレースホルダにもなる（見出し + CTA を持つ案内カードは 1 画面に
    1 枚だけ = 操作文脈を持つグリッド側に置き、従属するプレビュー列はこの
    静音形へ格下げする）。

    *emphasis* ``"secondary"`` は空状態オーケストレータの ``SECONDARY`` 役
    — **アイコン無し・1 行・1 段小さい**。
    主カード（グリッド側）と同じ大きさのアイコンが 3 面に並ぶのを構造的に
    防ぐため、この形では *icon_name* に ``None`` を渡すこと。
    """

    def __init__(
        self,
        heading: str | None = None,
        *,
        icon_name: str | None = "folder-open",
        emphasis: str = "hint",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = EmptyStateCard(
            heading if heading is not None else t("viewer.content_view.empty_hint"),
            icon_name=icon_name,
            emphasis=emphasis,  # type: ignore[arg-type]
        )
        layout.addWidget(self._card)

    def set_heading(self, text: str) -> None:
        """差し替え可能な 1 行（オーケストレータが割当ごとに文言を渡す）。"""
        self._card.set_heading(text)


class _EmptyFolderView(QWidget):
    """Card for a drilled-into folder that has no entries.

    An empty *sub*folder must not resurface the first-run welcome card —
    its 「フォルダを開く…」 button yanks the user out of the current browse
    flow.  This card states the actual situation and offers 「上の階層へ」
    first; opening another folder stays available as the secondary action.

    Built on :class:`EmptyStateCard` in its ``"onboarding"`` emphasis.
    """

    go_up_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        card = EmptyStateCard(
            t("viewer.content_view.empty_folder_heading"),
            icon_name="folder",
            body=t("viewer.content_view.empty_folder_body"),
            emphasis="onboarding",
        )
        self._card = card
        self._up_btn = card.add_action(
            t("viewer.main_window.go_up"), icon_name="arrow-up",
        )
        self._up_btn.clicked.connect(self.go_up_requested.emit)
        self._open_btn = card.add_action(
            t("viewer.main_window.open_folder"), icon_name="folder-open",
        )
        self._open_btn.clicked.connect(self.open_folder_requested.emit)
        layout.addWidget(card)

    def set_can_go_up(self, can: bool) -> None:
        """「上の階層へ」を出すかどうか。

        ``_on_go_up`` はライブラリ境界（登録ルート直下）で黙って return
        するので、境界で出すとこのボタンは「押せる見た目のまま無反応」に
        なる。ツールバーの ↑ が ``setEnabled(False)`` で応えるのに対し、
        案内カードは**行き先の無い誘いを出さない**方に倒す — ボタンごと
        隠し、本文も「上の階層に戻るか」を含まない版へ差し替える（文面と
        ボタンが食い違わないように）。
        """
        self._up_btn.setEnabled(can)
        self._up_btn.setVisible(can)
        self._card.set_body(
            t("viewer.content_view.empty_folder_body") if can
            else t("viewer.content_view.empty_folder_body_no_up")
        )


class _WelcomeView(QWidget):
    """First-run welcome card shown when the library root has no entries.

    The plain ``_EmptyView`` hint ("select a folder from the grid") is a
    contradiction when the left pane is empty — there is nothing to select.
    This card names the app, offers a one-line orientation, and gives a
    「フォルダを開く…」 button wired to the root picker so the first step is
    always reachable from the centre pane.

    Built on :class:`EmptyStateCard` in its ``"onboarding"`` emphasis.
    """

    open_folder_requested = Signal()
    #: 「操作の基本 (F1)」— 操作ガイド。説明書を読まなくても始められる
    #: ことが目標で、ガイドは 2 つ目の CTA として置く（主要導線は上の
    #: 「フォルダを開く」）。
    help_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        card = EmptyStateCard(
            t("viewer.content_view.welcome_heading"),
            icon_name="folder-open",
            body=t("viewer.content_view.welcome_body"),
            emphasis="onboarding",
        )
        # First-run orientation for the auto-created English 「library」
        # folder: shown only when the current root IS that default library —
        # otherwise a genuinely opened-but-empty folder would carry a note
        # about a folder it isn't.  Hidden until ``set_default_library``.
        self._default_note = card.add_note(
            t("viewer.content_view.welcome_default_library")
        )
        self._default_note.setVisible(False)
        self._open_btn = card.add_action(
            t("viewer.main_window.open_folder"), icon_name="folder-open",
        )
        self._open_btn.clicked.connect(self.open_folder_requested.emit)
        self._help_btn = card.add_action(
            t("viewer.content_view.welcome_help"), icon_name="help-circle",
        )
        self._help_btn.clicked.connect(self.help_requested.emit)
        layout.addWidget(card)

    def set_default_library(self, is_default: bool) -> None:
        """Show the 「library is your starting folder」 note.

        Enabled only when the current root is the auto-created default library,
        so the note never appears over some other empty folder the user opened.
        """
        self._default_note.setVisible(is_default)


class _MaximizedEmptyView(QWidget):
    """最大化中に主案内の行き場が無くなったときのカード.

    プレビュー最大化（またはハンドルを引き切った状態）ではグリッド席が幅 0 に
    なるため、空状態の主案内をグリッドに置くと**見えない面を指す案内**になる。

    空状態オーケストレータ（:mod:`..empty_state`）は「畳まれた席は
    ``PRIMARY`` になれない」規則でこれを構造的に閉じ、代わりに**このカード**
    を中央へ出す: 見出し + [◧ 分割ビューに戻す (G)]。ボタンは G / Esc /
    ヘッダーの同名ボタンと**同じ離脱経路**（``_exit_stage_to_browse``）へ
    配線される。
    """

    restore_split_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = EmptyStateCard(
            t("viewer.content_view.maximized_empty_heading"),
            icon_name="panel-left",
            body=t("viewer.content_view.maximized_empty_body"),
            emphasis="onboarding",
        )
        self._card = card
        self._restore_btn = card.add_action(
            t("viewer.stage_view.back_to_split"), icon_name="panel-preview",
        )
        self._restore_btn.setToolTip(t("viewer.stage_view.back_to_split_tooltip"))
        self._restore_btn.clicked.connect(self.restore_split_requested.emit)
        layout.addWidget(card)

    def set_scan_error(self, failed: bool) -> None:
        """走査が失敗しているときは、そう名乗る（同じカード・同じ出口）.

        主案内がこの席へ退避するのは最大化中だけで、そのとき「表示する項目が
        選ばれていません」と言うと、**失敗した事実**が画面のどこにも残らない
        （グリッドの ⚠ カードは幅 0 の席に居る）。行き先は同じ [分割ビューに
        戻す] — そこにグリッドの再試行ボタンがある。
        """
        if failed:
            self._card.set_heading(
                t("viewer.content_view.maximized_scan_error_heading")
            )
            self._card.set_body(
                t("viewer.content_view.maximized_scan_error_body")
            )
        else:
            self._card.set_heading(
                t("viewer.content_view.maximized_empty_heading")
            )
            self._card.set_body(t("viewer.content_view.maximized_empty_body"))
