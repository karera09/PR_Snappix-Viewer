"""ヘルプ + 診断のウィンドウ部品 :class:`WindowHelp`.

``ViewerWindow`` から「ヘルプ ▸」と「診断 ▸」の 2 メニューが起動する面を
まとめて切り出したもの:

* モードレス単一インスタンスの子窓 4 本（計測統計 ``PerfDialog`` / 健全性
  チェック ``HealthCheckDialog`` / 操作ガイド ``ShortcutsDialog`` / AI パックの
  セットアップ手引き ``DocDialog``）— 開く・前面に出す・閉じたら参照を捨てる;
* 同梱文書の口（はじめにお読みください / 利用規約 / サードパーティ
  ライセンス / バージョン情報 / ログフォルダ）;
* 計測トグルとキャッシュ事前作成の起動、エクスプローラ統合ダイアログ。

部品はタイマーもスレッドも持たない（各ダイアログが自分の後始末を持つ）。
子窓はすべて**ウィンドウを親**にして作るので、``closeEvent`` の
「直下の可視トップレベル子を畳む」掃引にそのまま乗る。

この部品がウィンドウ側から読むもの（これで全部）:

* ``_root`` — 健全性チェックの対象ルート;
* ``_cache_ctrl`` — キャッシュ事前作成の起動先;
* ``_act_toggle_perf`` — 診断メニューの計測トグル（ダイアログ側のチェック
  ボックスと鏡写しに保つ相手。メニューはウィンドウが所有する）;
* ``_show_toast`` — 計測トグルの確認通知;
* ``_open_shortcuts_dialog`` — ``F1`` の席判定から操作ガイドを開く口
  （ウィンドウの委譲メソッドを通すのは、入口が 1 本に見えるようにするため）。
"""

from __future__ import annotations

from loguru import logger
from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..common.i18n import t
from ..common.paths import get_paths
from ..common.ui import confirm_action
from .focus_target import SEAT_LIGHTBOX, focused_seat
from .perf import recorder
from .perf_dialog import PerfDialog
from .shortcuts_dialog import TASK_FULLSCREEN, ShortcutsDialog
from .view_prefs import notify_failure


class WindowHelp(QObject):
    """``ViewerWindow`` のヘルプ + 診断の面を持つ部品."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        # Modeless 計測統計ダイアログ（診断メニュー・Ctrl+Shift+P）。
        self._perf_dialog: PerfDialog | None = None
        # Modeless 健全性チェックダイアログ（診断メニュー）。health_dialog は
        # 初回オープンで import する（起動経路に載せない）。
        self._health_dialog = None
        # Modeless 操作ガイド（ヘルプメニュー・F1）。
        self._shortcuts_dialog: ShortcutsDialog | None = None
        # Modeless AI セットアップ手引き（ヘルプメニュー・AI パック有効時のみ）。
        self._tagger_doc_dialog: QDialog | None = None

    # ------------------------------------------------------------ 明示の口

    def _perf_action(self) -> "QAction | None":
        """診断メニューの計測トグル（窓が所有。``_build_menus`` 前は不在）."""
        return getattr(self._window, "_act_toggle_perf", None)

    @property
    def perf_dialog(self) -> "PerfDialog | None":
        return self._perf_dialog

    @property
    def health_dialog(self):
        return self._health_dialog

    @property
    def shortcuts_dialog(self) -> "ShortcutsDialog | None":
        return self._shortcuts_dialog

    @property
    def tagger_doc_dialog(self) -> "QDialog | None":
        return self._tagger_doc_dialog

    # ----------------------------------------------------------- diagnostics

    def toggle_perf(self, enabled: bool) -> None:
        recorder().set_enabled(enabled)
        msg = (
            t("viewer.main_window.perf_measure_on")
            if enabled
            else t("viewer.main_window.perf_measure_off")
        )
        # ``showMessage`` は左端の現在パス表示（常設の「今どこにいるか」）を
        # 3 秒間まるごと潰す。診断トグルの確認に現在地を犠牲にする理由は
        # 無いので、他の成功通知と同じトーストへ寄せる。
        self._window._show_toast(msg, "info")

    def open_perf_dialog(self) -> None:
        # Turn measurement on automatically when the user opens the dialog —
        # the common case is "I want to see what's slow right now", and
        # leaving the menu toggle as a separate step adds a confusing
        # failure mode (open dialog, see empty table, wonder why).
        if not recorder().is_enabled():
            recorder().set_enabled(True)
            action = self._perf_action()
            if action is not None:
                action.setChecked(True)
        if self._perf_dialog is None:
            self._perf_dialog = PerfDialog(self._window)
            self._perf_dialog.destroyed.connect(self._on_perf_dialog_destroyed)
            # Keep the diagnostics-menu toggle in sync when the dialog's own
            # checkbox turns measurement on/off (otherwise the menu keeps its
            # stale checked state and the next menu click is a no-op).
            self._perf_dialog.enabled_toggled.connect(
                self.on_perf_enabled_changed
            )
        self._perf_dialog.show()
        self._perf_dialog.raise_()
        self._perf_dialog.activateWindow()

    def on_perf_enabled_changed(self, enabled: bool) -> None:
        # Mirror the dialog checkbox onto the menu action without re-emitting
        # ``toggled`` (which would recurse into set_enabled needlessly).
        action = self._perf_action()
        if action is None:
            return
        block = action.blockSignals(True)
        action.setChecked(enabled)
        action.blockSignals(block)

    def _on_perf_dialog_destroyed(self) -> None:
        self._perf_dialog = None

    def open_health_dialog(self) -> None:
        """診断メニュー「ライブラリ健全性チェック…」 — modeless integrity scan.

        Scans the current root off-thread (the dialog owns the worker) and
        reports per-post integrity findings.  Modeless single instance (like
        the shortcuts / perf windows) so the multi-minute scan of a large NAS
        tree no longer freezes browsing — the user can keep navigating the
        library while it runs.  Reopening re-targets the current root and
        rescans; destructive bulk deletes stay behind their own modal confirm.
        """
        from .health_dialog import HealthCheckDialog

        root = self._window._root
        if root is None:
            return
        if self._health_dialog is None:
            self._health_dialog = HealthCheckDialog(root, parent=self._window)
            self._health_dialog.destroyed.connect(
                self._on_health_dialog_destroyed
            )
        else:
            # Re-target the (possibly changed) current root and rescan.
            self._health_dialog.rescan(root)
        self._health_dialog.show()
        self._health_dialog.raise_()
        self._health_dialog.activateWindow()

    def _on_health_dialog_destroyed(self) -> None:
        self._health_dialog = None

    def open_cache_prebuild(self) -> None:
        """診断メニュー「キャッシュを事前作成…」.

        Runs the same ``CacheBuildController.build_cache_interactive`` path the
        settings dialog's cache tab uses — folder + mode pick, then a background
        (or modal, per ``cache_build_background``) build.  The controller's
        ``_bg_builder`` guard blocks a second concurrent build, so this needs no
        extra re-entrancy check of its own.
        """
        self._window._cache_ctrl.build_cache_interactive(self._window)

    def open_shell_integration_dialog(self) -> None:
        """診断 ▸ エクスプローラ統合… — register/unregister the right-click verb.

        The only place (with the shipped .bat scripts) the product ever writes
        to the registry, and only on an explicit click.  Registration needs a
        real exe path, so it is offered only in a frozen build; a dev run can
        still *unregister* a stale key.  All keys go through
        :mod:`shell_integration`, which the .bat scripts mirror exactly.
        """
        import sys

        from . import shell_integration as shell

        frozen = bool(getattr(sys, "frozen", False))
        available = shell.is_available()

        dlg = QDialog(self._window)
        dlg.setWindowTitle(t("viewer.shell_integration.title"))
        # 幅を ``sizeHint`` 任せにするとボタン列（登録 / 解除 / 閉じる）が
        # 実効幅を決めてしまい、折り返す説明文が 14 文字前後で 6 行に割れる。
        # 他の説明主体のダイアログと桁を揃える。
        dlg.setMinimumWidth(440)
        layout = QVBoxLayout(dlg)
        status_label = QLabel(dlg)
        status_label.setWordWrap(True)
        layout.addWidget(status_label)
        desc_label = QLabel(dlg)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        button_row = QHBoxLayout()
        register_btn = QPushButton(t("viewer.shell_integration.register"), dlg)
        unregister_btn = QPushButton(t("viewer.shell_integration.unregister"), dlg)
        close_btn = QPushButton(t("common.action.close"), dlg)
        # 既定ボタンを明示する。指定が無いと Qt が先頭の有効なボタン（=
        # [登録]）へ暗黙の default を与え、反射的な Enter がレジストリ書き込みを
        # 確定させる。姉妹実装 ``plugin_host/dialog.py`` と同じ書き方で、既定は
        # [閉じる] に置く。
        for btn in (register_btn, unregister_btn):
            btn.setAutoDefault(False)
            btn.setDefault(False)
        close_btn.setAutoDefault(True)
        close_btn.setDefault(True)
        button_row.addWidget(register_btn)
        button_row.addWidget(unregister_btn)
        button_row.addStretch(1)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

        def refresh() -> None:
            if not available:
                status_label.setText(t("viewer.shell_integration.unavailable"))
                desc_label.setText(t("viewer.shell_integration.description"))
                register_btn.setEnabled(False)
                unregister_btn.setEnabled(False)
                return
            st = shell.status()
            # ポータブル製品はフォルダごと動かされるのが常態で、そのとき
            # 右クリック動詞は古い場所を指したまま無言で壊れる。判定は
            # **読み取りのみ**（``status()`` はレジストリを読むだけ）で、
            # 書き込みは従来どおり [登録] の明示押下でしか起きない。
            stale = bool(
                frozen
                and st.registered
                and st.exe_path
                and not shell.same_exe_path(st.exe_path, sys.executable)
            )
            if st.registered:
                text = t(
                    "viewer.shell_integration.status_registered",
                    exe=st.exe_path or "",
                )
                if stale:
                    text += "\n" + t(
                        "viewer.shell_integration.status_stale",
                        exe=sys.executable,
                    )
                status_label.setText(text)
            else:
                status_label.setText(
                    t("viewer.shell_integration.status_unregistered")
                )
            # 押せば直る（``register`` は registry_entries の全書き込みを冪等に
            # 行うので、同じボタンが「この場所へ貼り直す」動作になる）。
            register_btn.setText(
                t("viewer.shell_integration.register_update")
                if stale
                else t("viewer.shell_integration.register")
            )
            # Registering points the verb at THIS exe — only meaningful for a
            # frozen build (a dev run's sys.executable is python.exe).
            desc_label.setText(
                t("viewer.shell_integration.description")
                if frozen
                else t("viewer.shell_integration.dev_note")
            )
            register_btn.setEnabled(frozen)
            # Unregister stays available whenever something is registered, so a
            # dev session can still clean up a key left by a shipped build.
            unregister_btn.setEnabled(st.registered)

        def do_register() -> None:
            # レジストリ書き込みは「ユーザーの明示操作」に限る、という
            # ポータビリティ規約の唯一の例外。押し間違いがその明示に化けない
            # よう、押下と書き込みの間に確認を 1 枚挟む（``confirm_action`` は
            # reject 側を既定 + Escape ボタンにする）。押下 → 書き込みの経路
            # そのものは変えない。
            if not confirm_action(
                dlg,
                title=t("viewer.shell_integration.title"),
                body=t("viewer.shell_integration.confirm_register_body"),
                accept_text=t("viewer.shell_integration.confirm_register_btn"),
            ):
                return
            try:
                shell.register(sys.executable)
            except OSError as exc:
                QMessageBox.warning(
                    dlg,
                    t("viewer.shell_integration.title"),
                    t("viewer.shell_integration.error", error=str(exc)),
                )
            refresh()

        def do_unregister() -> None:
            if not confirm_action(
                dlg,
                title=t("viewer.shell_integration.title"),
                body=t("viewer.shell_integration.confirm_unregister_body"),
                accept_text=t("viewer.shell_integration.confirm_unregister_btn"),
                destructive=True,
            ):
                return
            try:
                shell.unregister()
            except OSError as exc:
                QMessageBox.warning(
                    dlg,
                    t("viewer.shell_integration.title"),
                    t("viewer.shell_integration.error", error=str(exc)),
                )
            refresh()

        register_btn.clicked.connect(do_register)
        unregister_btn.clicked.connect(do_unregister)
        close_btn.clicked.connect(dlg.accept)
        refresh()
        dlg.exec()
        dlg.deleteLater()  # ラベル/ボタン/クロージャごと親から解放する

    # ------------------------------------------------------------------ help

    def open_shortcuts_dialog(self, task: str | None = None) -> None:
        """操作ガイドを開く。*task* を渡すとその作業ページで開く（作業軸）.

        ガイドはモードレスの単一インスタンスなので、2 回目以降は前回見ていた
        ページのまま上がってくる。入口が既に作業を名指ししている（「はじめに
        （操作の基本と一覧）…」・ようこそカードの [操作の基本 (F1)]・全画面の
        席からの F1）なら、その作業へ揃えてから見せる — 入口の名前と着地が
        食い違わない。名指ししない入口（ヘルプ ▸ ショートカット一覧 / 主窓の
        F1）は ``None`` で、前回のページを尊重する。
        """
        # Modeless single instance (like the perf / detail windows): construct
        # on first use, then just re-raise on subsequent invocations.
        if self._shortcuts_dialog is None:
            self._shortcuts_dialog = ShortcutsDialog(self._window)
            self._shortcuts_dialog.destroyed.connect(
                self._on_shortcuts_dialog_destroyed
            )
        if task is not None:
            # 既にそのページでも絞り込みは解かれる（``select_task`` の契約）—
            # 前回の絞り込みが残ったまま「そのページを出した」ことにしない。
            self._shortcuts_dialog.select_task(task)
        self._shortcuts_dialog.show()
        self._shortcuts_dialog.raise_()
        self._shortcuts_dialog.activateWindow()

    def on_help_shortcut(self) -> None:
        """``F1`` — 操作ガイドを「いま操作している席」に合わせて開く.

        F1 だけはアプリ全体スコープなので全画面（別トップレベル窓）からも届く。
        全画面の席から押されたときは、そこにしか無い操作（スライドショー・投稿
        横断・オートハイド）を探しているのが明らかなので全画面のページを名指し
        する。主窓の席は作業を名指ししないので、前回のページをそのまま出す。
        席の判定は動詞と同じ唯一の判定点（``focused_seat``）を使う。
        """
        seat = focused_seat(self._window)
        self._window._open_shortcuts_dialog(
            TASK_FULLSCREEN if seat == SEAT_LIGHTBOX else None
        )

    def _on_shortcuts_dialog_destroyed(self) -> None:
        self._shortcuts_dialog = None

    def open_logs_folder(self) -> None:
        """Help ▸ ログフォルダを開く — reveal data/logs in the OS file manager.

        Opens the portable ``get_paths().logs`` directory (created on demand) so
        a user reporting a problem can attach ``viewer.log`` without hunting for
        the path.  Failure is logged and surfaced through the shared 「開く」
        失敗ファネル (``view_prefs.notify_failure`` — 直書きの ``showMessage``
        だけが残ると、同じ「開けなかった」が入口ごとに別の面へ出る); it never
        raises.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        logs = get_paths().logs
        logs.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs))):
            logger.warning("could not open logs folder {}", logs)
            notify_failure(self._window, t("viewer.main_window.open_logs_failed"))

    def show_about(self) -> None:
        # 専用ダイアログ（著作権表記 + 規約/ライセンス導線 + 「閉じる」
        # ボタン）。AI 有無での本文出し分けは AboutDialog 側が
        # ai_pack.available() を見て維持する。
        from .about_dialog import show_about

        show_about(self._window)

    def open_shipped_readme(self) -> None:
        """ヘルプ ▸ はじめにお読みください… — open the shipped guide.

        ``legal_docs`` resolves it under ``get_paths().base`` (the dist root in
        a frozen build, the repo root in a dev run) and shows the same
        「ファイルが見つかりません」 guidance as the other doc openers when the
        install has been mangled.
        """
        from ..common.legal_docs import (
            GETTING_STARTED_NAME,
            getting_started_path,
            open_shipped_file,
        )

        open_shipped_file(getting_started_path(), self._window, GETTING_STARTED_NAME)

    def show_terms(self) -> None:
        from ..common.terms_dialog import show_terms

        show_terms(self._window)

    def open_third_party_licenses(self) -> None:
        from ..common.legal_docs import open_third_party_licenses

        open_third_party_licenses(self._window)

    def open_tagger_setup_doc(self) -> None:
        """Help ▸ AIタグ検索のセットアップ… → show the pack's setup guide.

        ガイドの実パスはパック側が知っている（provider の任意メソッド
        ``models_doc_path`` — 本体はパック内部のフォルダ構成を参照しない）。
        provider 未登録（activate 失敗など）はファイル不在と同じ案内に劣化。

        同梱ドキュメントで唯一の ``.md`` なので、OS へ丸投げすると素の
        Windows では関連付けが無く「どれで開きますか」で終わる — アプリ内の
        読み取り専用ダイアログで出す（``.txt`` の同梱文書は関連付けが確実に
        あるので従来どおり OS 委譲）。
        """
        from . import ai_pack
        from .doc_dialog import show_markdown_doc

        if self._tagger_doc_dialog is not None:
            self._tagger_doc_dialog.show()
            self._tagger_doc_dialog.raise_()
            self._tagger_doc_dialog.activateWindow()
            return
        dlg = show_markdown_doc(
            ai_pack.models_doc_path(),
            self._window,
            title=t("viewer.main_window.tagger_setup_title"),
            missing_name="tagger-models.md",
        )
        if dlg is not None:
            self._tagger_doc_dialog = dlg
            dlg.destroyed.connect(self._on_tagger_doc_dialog_destroyed)

    def _on_tagger_doc_dialog_destroyed(self) -> None:
        self._tagger_doc_dialog = None


__all__ = ["WindowHelp"]
