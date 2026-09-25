"""プラグイン管理ダイアログ（ファイル ▸ プラグイン…）.

検出済みプラグインの一覧と有効/無効の切替 UI。モーダル（設定ダイアログと
同格の「アプリ構成を変える」操作なので、閲覧と並走させない）。

適用は OK 時のみ（Cancel は無変更）。有効化はその場で activate を試み、
失敗はモーダルで通知して自動無効化（design.md の「失敗 = モーダル」）。
無効化は ``deactivate()`` + context の UI 回収をベストエフォートで行い、
完全に片付かない場合は「次回起動時に反映」をトーストで案内する。

セーフモード起動（``--no-plugins``）では host が無い（= 何もロードされて
いない）が、このダイアログは store と検出だけで動くので設定変更は可能 —
変更は次回の通常起動から効く。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ...common.i18n import t
from ...common.ui import (
    RADIUS_SM,
    confirm_action,
    current_tokens,
    empty_state_stack,
    hint_style,
    localize_buttons,
    rgba,
    show_toast,
    svg_source,
    warn_modal,
)
from .host import PluginHost
from .manifest import BrokenPlugin, PluginManifest, discover_plugins
from .store import PluginStore
from .trust import confirm_prompt, decline_prompt, declined_before, trust_decision

#: ダイアログからの有効化で**再確認モーダルを挟む**信頼判定の理由
#: （なりすまし疑い = 記録した実体と違うフォルダが検出の勝者になっている）。
#:
#: ``unknown``（まだ一度も確認していないプラグイン）は入れない — この画面で
#: チェックを付けて OK を押す操作そのものが明示同意で、しかも常設の安全注記
#: パネルが「プラグインは任意のコードを実行できる」ことを名乗っている。
#: 起動時の bootstrap は利用者が何も操作していないところへ出るので、そちらは
#: 未確認でも問う（同じ判定・違う帰結）。
_RECONFIRM_REASONS = ("folder_mismatch", "duplicate_id")


def _tinted_pixmap(
    name: str, color: str, size: int, dpr: float = 1.0,
) -> QPixmap:
    """Render themed SVG glyph *name* tinted *color* (no icon-role match).

    ``common.ui.icons.icon()`` only exposes a fixed role→token table (text /
    muted / accent / danger / on-accent) — no "warning" role — so the
    security-note panel renders its own pixmap from the public
    ``svg_source`` template instead of extending that table.

    *size* は**論理**サイズ。``common/ui/icons.py`` の ``_render`` と同じく
    物理ピクセル（``size * dpr``）で描いてから ``setDevicePixelRatio`` を
    打ち込む — 論理サイズのまま描くと 150% / 200% スケールの Windows で ⚠ が
    拡大ボケする。
    """
    renderer = QSvgRenderer(QByteArray(svg_source(name, color).encode("utf-8")))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm

_ID_ROLE = Qt.UserRole


class PluginManagerDialog(QDialog):
    """検出済みプラグインの一覧・有効/無効切替（OK 適用・Cancel 破棄）。"""

    def __init__(
        self,
        parent=None,
        *,
        host: PluginHost | None,
        plugins_dir: Path,
        store: PluginStore | None = None,
        safe_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.plugins.dialog_title"))
        self.resize(560, 420)
        self._host = host
        self._plugins_dir = plugins_dir
        self._safe_mode = safe_mode
        if host is not None:
            # ダイアログを開いた時点で置かれたばかりのフォルダも拾う。
            host.discover()
            self._store = host.store
            self._manifests: list[PluginManifest] = list(host.manifests)
            self._broken: list[BrokenPlugin] = list(host.broken)
        else:
            self._store = store if store is not None else _default_store()
            self._manifests, self._broken = discover_plugins(
                plugins_dir, preferred_folders=self._store.preferred_folders()
            )
        self._build_ui()
        self._populate()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # 「プラグインは任意コード実行できる」という安全警告は、事務的な注記と
        # 同じ hint スタイルでは一番目立たない。warning トークンの枠付き
        # パネル + アイコンで出す（セーフモード注記は hint のまま残し、両者の
        # 見た目に段差を付ける）。
        outer.addWidget(self._build_security_note_panel())

        # 状態列は「未有効化 (新規)」と言うだけで、どう操作すれば有効になるのか
        # を語らない（チェックボックスは行の左端にあるが、それが「有効化」だとは
        # 名乗っていない）。常設の 1 行として、再起動注記と同じ hint の段に置く。
        enable_hint = QLabel(t("viewer.plugins.enable_hint"))
        enable_hint.setWordWrap(True)
        enable_hint.setStyleSheet(hint_style())
        outer.addWidget(enable_hint)

        if self._safe_mode:
            outer.addSpacing(6)
            safe = QLabel(t("viewer.plugins.safe_mode_note"))
            safe.setWordWrap(True)
            safe.setStyleSheet(hint_style())
            outer.addWidget(safe)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(
            [
                t("viewer.plugins.col_name"),
                t("viewer.plugins.col_version"),
                t("viewer.plugins.col_status"),
            ]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._tree.currentItemChanged.connect(lambda *_: self._sync_detail())
        self._tree.itemChanged.connect(self._on_item_changed)
        # 空状態の案内はツリーの行（列幅で切り詰められて読めない）ではなく、
        # 大きな空ツリーの代わりに中央寄せの折り返しラベルへ切り替える（管理系
        # ダイアログの空状態と揃える）。設置場所の案内は有償プラグインの導入
        # 導線でもあるため全文が読めること。図像は sliders ではなく puzzle —
        # sliders はツールバーの検索オプション（つまみを調整する）の席なので、
        # 同じ絵で「プラグイン」を指さない。
        self._stack, self._empty_label = empty_state_stack(
            self._tree, icon_name="puzzle"
        )
        outer.addWidget(self._stack, 1)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        # description / homepage は「まだ有効化していない」プラグインの
        # マニフェスト由来 = 信頼できない文字列。AutoText のままだと
        # HTML マークアップがクリック可能リンク等として描画されるため、
        # 必ずプレーンテキストで表示する（選択・コピーは可）。
        self._detail.setTextFormat(Qt.PlainText)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail.setStyleSheet(hint_style())
        outer.addWidget(self._detail)

        buttons_row = QHBoxLayout()
        open_btn = QPushButton(t("viewer.plugins.open_folder_btn"))
        open_btn.clicked.connect(self._open_plugins_folder)
        # この行が唯一のボタンとして先にレイアウトへ入るため、Qt の暗黙
        # default 付与でアクセント塗りの既定ボタンになり、Enter がフォルダを
        # 開いてしまう（新規購入者が最初に開く画面）。
        # OK/Cancel 側に本来の肯定アクションがあるので、こちらは明示的に外す。
        open_btn.setAutoDefault(False)
        open_btn.setDefault(False)
        buttons_row.addWidget(open_btn)
        buttons_row.addStretch(1)
        outer.addLayout(buttons_row)

        buttons = localize_buttons(
            QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_security_note_panel(self) -> QFrame:
        """Warning-token bordered panel for the code-execution risk notice.

        Not a plain ``hint_style`` label — that is the least visually urgent
        style in the dialog, level with routine caption text, while this warns
        about arbitrary code execution. Rendered from
        ``current_tokens().warning`` (no colour literals) so both themes stay
        correct.
        """
        tok = current_tokens()
        panel = QFrame()
        panel.setObjectName("pluginSecurityNotePanel")
        panel.setStyleSheet(
            "QFrame#pluginSecurityNotePanel {"
            f" background: {rgba(tok.warning, 0.12)};"
            f" border: 1px solid {rgba(tok.warning, 0.55)};"
            f" border-radius: {RADIUS_SM}px;"
            "}"
        )
        row = QHBoxLayout(panel)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)
        icon_label = QLabel()
        icon_label.setPixmap(
            _tinted_pixmap(
                "alert-triangle", tok.warning, 18, self.devicePixelRatioF(),
            )
        )
        row.addWidget(icon_label, 0, Qt.AlignTop)
        note = QLabel(t("viewer.plugins.security_note"))
        note.setWordWrap(True)
        row.addWidget(note, 1)
        return panel

    def _populate(self) -> None:
        # 行生成中の setCheckState / setText は ``itemChanged`` を鳴らす —
        # 生成の途中で状態列を作り直しても無意味なので黙らせる。
        self._tree.blockSignals(True)
        try:
            self._populate_rows()
        finally:
            self._tree.blockSignals(False)
        self._sync_detail()

    def _populate_rows(self) -> None:
        self._tree.clear()
        for m in self._manifests:
            item = QTreeWidgetItem(
                self._tree, [m.name, m.version, self._status_text(m)]
            )
            item.setData(0, _ID_ROLE, m.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.Checked if self._store.is_enabled(m.id) else Qt.Unchecked,
            )
        if self._broken:
            header = QTreeWidgetItem(
                self._tree, [t("viewer.plugins.broken_header"), "", ""]
            )
            header.setFlags(Qt.ItemIsEnabled)
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            for b in self._broken:
                item = QTreeWidgetItem(self._tree, [b.dir.name, "", b.error])
                item.setFlags(Qt.ItemIsEnabled)
        if not self._manifests and not self._broken:
            self._empty_label.setText(t("viewer.plugins.no_plugins_hint"))
            self._stack.setCurrentWidget(self._empty_label)
        else:
            self._stack.setCurrentWidget(self._tree)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Re-render the 状態 column when a row's checkbox is toggled.

        Enable flags only reach the store on OK (``_on_accept``), so a column
        showing the *stored*状態 would contradict the checkbox next to it until
        the dialog was closed and reopened.  ``_status_text`` takes the pending flag and answers
        「…予定 (OK で確定)」 for it, so ``status_enabled_inactive``
        （再起動が要る旨の唯一の告知面）is untouched.
        """
        if column != 0:
            return
        pid = item.data(0, _ID_ROLE)
        manifest = next((m for m in self._manifests if m.id == pid), None)
        if manifest is None:  # 「読み込めなかったプラグイン」節の行
            return
        item.setText(
            2,
            self._status_text(
                manifest, enabled=item.checkState(0) == Qt.Checked
            ),
        )

    def _status_text(
        self, m: PluginManifest, *, enabled: bool | None = None,
    ) -> str:
        """The 状態 column's text; *enabled* overrides the **stored** flag.

        Pass the row's live check state to describe a not-yet-committed change
        (see :meth:`_on_item_changed`); leave it ``None`` for the stored状態.
        """
        if enabled is not None and enabled != self._store.is_enabled(m.id):
            return t(
                "viewer.plugins.status_pending_enable"
                if enabled
                else "viewer.plugins.status_pending_disable"
            )
        if self._host is not None and self._host.is_active(m.id):
            return t("viewer.plugins.status_active")
        if not self._store.known(m.id):
            return t("viewer.plugins.status_new")
        if self._store.is_enabled(m.id):
            return t("viewer.plugins.status_enabled_inactive")
        return t("viewer.plugins.status_disabled")

    def _sync_detail(self) -> None:
        item = self._tree.currentItem()
        pid = item.data(0, _ID_ROLE) if item is not None else None
        manifest = next((m for m in self._manifests if m.id == pid), None)
        if manifest is None:
            self._detail.setText("")
            return
        lines: list[str] = []
        if manifest.description:
            lines.append(manifest.description)
        if manifest.author:
            lines.append(t("viewer.plugins.detail_author", author=manifest.author))
        # フォルダ名は「name / version / 状態」の 3 列にも詳細にも出ていなかった
        # 唯一の実体情報。同じ id を名乗る別フォルダとの区別（なりすまし判断）は
        # これが無いと画面上で一切つかない。
        lines.append(
            t("viewer.plugins.detail_folder", folder=manifest.dir.name)
        )
        if manifest.homepage:
            lines.append(manifest.homepage)
        self._detail.setText("\n".join(lines))

    # ------------------------------------------------------------- actions

    def _open_plugins_folder(self) -> None:
        try:
            self._plugins_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._plugins_dir)))

    def _checked_ids(self) -> set[str]:
        checked: set[str] = set()
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            pid = item.data(0, _ID_ROLE)
            if pid and item.checkState(0) == Qt.Checked:
                checked.add(pid)
        return checked

    def _confirm_trust(self, m: PluginManifest) -> bool:
        """有効化してよいかを信頼判定に通す（``True`` で続行）。

        起動時の確認（:mod:`.bootstrap`）と同じ
        :func:`~.trust.trust_decision` を通す — このダイアログはもう 1 つの
        有効化経路で、以前はここだけが判定の外にあった。記録した実体が消えて
        別フォルダが同じ id を名乗っている状態でも、チェックして OK を押せば
        無警告で ``set_enabled(folder=新フォルダ)`` + ``activate`` が走った。

        判定が ``trusted`` でも、その記録が**起動時の確認を断って**書かれた
        ものなら同じ警告を出す（:func:`~.trust.declined_before`）— 記録は
        承認・拒否のどちらでも書かれるので、判定だけでは「一度断った実体」と
        「許した実体」の区別が付かない。
        """
        decision = trust_decision(
            m.id,
            manifests=self._manifests,
            broken=self._broken,
            store=self._store,
        )
        if decision.reason in _RECONFIRM_REASONS:
            title, body = confirm_prompt(decision)
        elif declined_before(m, self._store):
            # 起動時の確認で［有効化しない］と答えた実体。記録は「確認済み」
            # としてフォルダを束ねるので判定は ``trusted`` に見えるが、断った
            # 相手をこの画面のチェック 1 つで無警告に走らせてよい理由は無い。
            title, body = decline_prompt(m)
        else:
            return True
        # 本文には未検証のマニフェスト文字列が入る（bootstrap 側と同じ理由で
        # プレーンテキスト明示）。ボタンは動詞。
        return confirm_action(
            self,
            title=title,
            body=body,
            accept_text=t("viewer.plugins.trust_enable_btn"),
            reject_text=t("viewer.plugins.trust_decline_btn"),
            icon=QMessageBox.Icon.Warning,
            plain_text=True,
        )

    def _on_accept(self) -> None:
        checked = self._checked_ids()
        needs_restart = False
        for m in self._manifests:
            was_enabled = self._store.is_enabled(m.id)
            now_enabled = m.id in checked
            if was_enabled == now_enabled and self._store.known(m.id):
                continue
            if now_enabled and not was_enabled and not self._confirm_trust(m):
                # 再確認で断られた = 記録も activate もしない（チェックは
                # 次に開いたとき store の値で描き直される）。
                continue
            # 承認したフォルダ実体を束ねる（id 詐称なりすまし対策）。
            self._store.set_enabled(m.id, now_enabled, folder=m.dir.name)
            if now_enabled and not was_enabled:
                if self._host is not None:
                    error = self._host.activate(m)
                    if error is not None:
                        # name / error は未検証の外部文字列なので AutoText の
                        # HTML 解釈を切る（``warn_modal`` の既定 — bootstrap の
                        # 失敗通知と同じ 1 実装）。
                        warn_modal(
                            self,
                            title=t("viewer.plugins.activate_failed_title"),
                            body=t(
                                "viewer.plugins.activate_failed_body",
                                name=m.name,
                                error=error,
                            ),
                        )
                        continue
                    self._toast(
                        t("viewer.plugins.enabled_toast", name=m.name), "success"
                    )
                else:
                    needs_restart = True
            elif was_enabled and not now_enabled:
                if self._host is not None and self._host.is_active(m.id):
                    if not self._host.deactivate(m.id):
                        needs_restart = True
                    self._toast(
                        t("viewer.plugins.disabled_toast", name=m.name), "info"
                    )
                else:
                    needs_restart = True
        if needs_restart:
            self._toast(t("viewer.plugins.restart_note"), "info")
        self.accept()

    def _toast(self, message: str, kind: str) -> None:
        parent = self.parent()
        if parent is not None:
            show_toast(parent, message, kind=kind)


def _default_store() -> PluginStore:
    from ...common.paths import get_paths

    return PluginStore(get_paths().data / "plugins.json")


__all__ = ["PluginManagerDialog"]
