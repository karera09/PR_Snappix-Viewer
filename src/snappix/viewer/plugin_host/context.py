"""プラグインへ渡す安定 API 層 :class:`PluginContext`（Qt 依存）.

プラグインの ``activate(ctx)`` が受け取る ``ctx`` の実体。API は二層構造:

* **安定 API** — このクラスの public メソッド/プロパティ。
  :data:`~snappix.viewer.plugin_host.manifest.PLUGIN_API_VERSION` の範囲で
  後方互換を維持する（壊す変更はバージョンをバンプし、旧 api のプラグインは
  ロード拒否される）。
* **脱出ハッチ** — :attr:`window`（``ViewerWindow`` の生参照）。Qt 的には
  何でもできるが内部構造はバージョン間で予告なく変わる。安定 API に無い
  ことをするための穴で、互換性の保証は無い。

利用例・詳細は docs/PLUGIN_DEVELOPMENT.md（配布フォルダの plugins/ にも同梱）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QWidget

from ...common.ui import show_toast
from ..context_menus import (
    register_context_menu_contributor,
    unregister_context_menu_contributors,
)
from .manifest import PLUGIN_API_VERSION, PluginManifest


class PluginEvents(QObject):
    """プラグインが購読できる viewer 内イベント（安定 API の一部）.

    * ``root_changed(Path)`` — グリッドのルートが変わった（起動時・
      ドリルダウン・履歴移動を含むすべての着地）。
    * ``selection_changed(Path)`` — グリッドでファイル/フォルダが選択された。

    どちらも GUI スレッドで emit される。ハンドラ内で重い I/O をしないこと
    （必要ならワーカースレッドへ — PLUGIN_DEVELOPMENT.md のスレッド節参照）。
    """

    root_changed = Signal(object)
    selection_changed = Signal(object)


class _PluginEventsProxy(PluginEvents):
    """1 プラグイン専用のイベント中継（``PluginContext.events`` が返す実体）。

    共有インスタンス（``bootstrap`` がセッションに 1 つだけ作る
    :class:`PluginEvents`）を購読し、同じ引数で再 emit する。プラグインは常に
    この proxy へ connect するので、``cleanup()`` の :meth:`detach` 一発で
    **そのプラグインの購読だけ**を共有側から切り離せる。

    これが無いと、``activate`` が途中で例外を投げたプラグインの
    ``ctx.events`` 購読を誰も解除できない: 失敗プラグインの ``deactivate()``
    は呼ばれない（``loaded`` に載らない）ので、PLUGIN_DEVELOPMENT.md §4 の
    「シグナル接続は自分で止めること」も発動しようがなく、共有 events は
    ウィンドウ寿命で生きるため、無効化済みと表示されたプラグインのハンドラが
    以後のナビゲーションで走り続ける（``host`` の「activate 失敗 = 寄稿は
    残らない」保証の破れ）。

    転送は **proxy 自身の束縛メソッド slot**（``QObject`` → ``QObject``）で
    つなぐ — ``shared.sig.connect(self.sig.emit)`` と書くと PySide が
    グローバルな受け手経由でつなぎ、proxy を破棄しても接続が残る
    （ワーカーブリッジ → 公開シグナルの転送と同じ規約）。
    """

    def __init__(self, shared: PluginEvents) -> None:
        super().__init__()
        self._shared = shared
        shared.root_changed.connect(self._on_root_changed)
        shared.selection_changed.connect(self._on_selection_changed)

    def _on_root_changed(self, path) -> None:
        self.root_changed.emit(path)

    def _on_selection_changed(self, path) -> None:
        self.selection_changed.emit(path)

    def detach(self) -> None:
        """共有 events からこの proxy の購読を**同期的に**切る（冪等）。"""
        for signal, slot in (
            (self._shared.root_changed, self._on_root_changed),
            (self._shared.selection_changed, self._on_selection_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                # 既に切れている / 共有側が破棄済み（ウィンドウ teardown）。
                pass


class PluginContext:
    """1 プラグインに 1 つ渡される安定 API オブジェクト。"""

    #: このホストが実装するプラグイン API バージョン。
    api_version: int = PLUGIN_API_VERSION

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        window: QWidget,
        events: PluginEvents,
        data_root: Path,
        app_version: str,
    ) -> None:
        self._manifest = manifest
        self._window = window
        # プラグインへ渡すのは共有インスタンスではなく専用 proxy。cleanup() で
        # 購読ごと切れるようにするため（さもないと activate 失敗プラグインの
        # 購読が回収されず全ナビゲーションで走り続ける）。
        self._events = _PluginEventsProxy(events)
        self._data_root = data_root
        self._app_version = app_version
        self._log = logger.bind(plugin=manifest.id)
        # cleanup() で回収するために、このプラグインが追加した UI を記録する。
        self._actions: list[tuple[QMenu, QAction]] = []
        self._top_menus: list[QMenu] = []
        self._status_widgets: list[QWidget] = []
        # data_dir は「初回アクセスで作成」。作成済みを覚えておかないと
        # 参照のたびに mkdir が走り、NAS 上のベースでは 1 参照 = 1 往復になる。
        self._data_dir_ready = False

    # ------------------------------------------------------------ identity

    @property
    def plugin_id(self) -> str:
        return self._manifest.id

    @property
    def plugin_dir(self) -> Path:
        """プラグイン自身のフォルダ（読み取り用。設定の保存は data_dir へ）。"""
        return self._manifest.dir

    @property
    def app_version(self) -> str:
        """Snappix Viewer 本体のバージョン文字列。"""
        return self._app_version

    @property
    def data_dir(self) -> Path:
        """このプラグイン専用の永続データフォルダ（``data/plugins/<id>/``）.

        ポータビリティ規約: プラグインの設定・キャッシュは必ずここへ書く
        （ユーザーホームや %APPDATA% に書かないこと）。初回アクセスで作成。
        """
        path = self._data_root / "plugins" / self._manifest.id
        if not self._data_dir_ready:
            path.mkdir(parents=True, exist_ok=True)
            self._data_dir_ready = True
        return path

    @property
    def log(self):
        """プラグイン id 付きの loguru ロガー（``data/logs/viewer.log`` 行き）。"""
        return self._log

    # -------------------------------------------------------------- events

    @property
    def events(self) -> PluginEvents:
        """このプラグイン専用のイベント中継（``cleanup()`` で購読ごと切れる）。

        セッション共有の :class:`PluginEvents` そのものではなく proxy を返す —
        呼び出しごとに同じインスタンスなので ``connect`` / ``disconnect`` の
        往復はこれまでどおり成立する。
        """
        return self._events

    # ------------------------------------------------------------- UI: menus

    #: :meth:`get_menu` / :meth:`add_menu_action` が受け付けるメニュー id。
    MENU_IDS = (
        "file", "edit", "search", "bookmarks", "view", "diagnostics", "help",
    )

    def get_menu(self, menu_id: str) -> QMenu | None:
        """本体メニューバーの既存トップメニューを返す（無ければ ``None``）。"""
        menus = getattr(self._window, "_menus", None)
        if not isinstance(menus, dict):
            return None
        return menus.get(menu_id)

    def add_menu_action(
        self,
        menu_id: str,
        text: str,
        callback: Callable[[], None],
        *,
        shortcut: str | None = None,
    ) -> QAction:
        """既存メニューの末尾にアクションを 1 つ追加する。

        ``menu_id`` は :data:`MENU_IDS` のいずれか。ショートカットは既存の
        割り当て（ヘルプ → キーボードショートカット一覧）と衝突しないものを
        選ぶこと。
        """
        menu = self.get_menu(menu_id)
        if menu is None:
            raise ValueError(
                f"unknown menu id {menu_id!r} (expected one of {self.MENU_IDS})"
            )
        action = QAction(text, menu)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False: callback())
        menu.addAction(action)
        self._actions.append((menu, action))
        return action

    def add_top_menu(self, title: str) -> QMenu:
        """メニューバーにプラグイン独自のトップメニューを追加する。

        ヘルプメニューの直前に挿入される（ヘルプ位置が特定できない構成では
        末尾）。戻り値の ``QMenu`` には自由に addAction/addMenu してよい。
        """
        bar = self._window.menuBar()  # type: ignore[attr-defined]
        menu = QMenu(title, bar)
        help_menu = self.get_menu("help")
        if help_menu is not None:
            bar.insertMenu(help_menu.menuAction(), menu)
        else:
            bar.addMenu(menu)
        self._top_menus.append(menu)
        return menu

    # ------------------------------------------------------ UI: context menu

    def add_context_menu_entry(
        self, contributor: Callable[[QMenu, Path, bool], None]
    ) -> None:
        """グリッド／情報パネルのタイル右クリックメニューに項目を差し込む。

        ``contributor(menu, path, is_dir)`` はメニュー構築のたびに GUI
        スレッドで呼ばれる。**ファイルシステム I/O をしないこと**（メニュー
        構築は NAS-free が本体の規約 — 判定は拡張子など path 文字列だけで
        行い、実処理は action の triggered ハンドラへ）。例外は隔離され
        その項目が出ないだけで本体メニューは壊れない。
        """
        register_context_menu_contributor(self._manifest.id, contributor)

    # ---------------------------------------------------------- UI: feedback

    def show_toast(
        self, message: str, kind: str = "info", duration_ms: int = 3000,
    ) -> None:
        """非モーダルのトースト通知（成功/情報向け。失敗はモーダルにすること）。

        ``kind`` ∈ {"info", "success", "warning", "error"}。

        ``duration_ms`` は表示時間（既定 3 秒。**0 でクリックまで残す**）。
        「失敗はモーダル」の対象は操作そのものが通らなかったケースで、
        **長時間処理が完走したが一部が失敗した「部分失敗」はここには含まれ
        ない** — 数十分のスキャンの失敗件数が
        既定 3 秒で消えるのを避けるため、そういう通知は
        ``duration_ms=0`` の常駐トーストにする（本体が ``user_meta`` /
        キャッシュ構築の部分失敗に使っているのと同じ様式）。
        """
        show_toast(self._window, message, kind=kind, duration_ms=duration_ms)

    def show_status(self, message: str, timeout_ms: int = 5000) -> None:
        """ステータスバーへ一時メッセージを表示する。"""
        status_bar = getattr(self._window, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(message, timeout_ms)

    def add_status_widget(self, widget: QWidget) -> None:
        """ステータスバー右側（permanent 領域）へ常駐ウィジェットを追加する。

        進捗バー等の常駐 UI 向け（一時テキストは :meth:`show_status`）。
        本体のキャッシュ進捗（``CacheBuildStatusWidget``）と同じ領域に並ぶ —
        普段は ``setVisible(False)`` にしておき、動作中だけ見せる作法を推奨。
        ハンドラ内で GUI スレッドをブロックしないこと。cleanup（無効化・
        終了時）でステータスバーから取り外され ``deleteLater`` される。
        """
        status_bar = getattr(self._window, "statusBar", None)
        if not callable(status_bar):
            raise RuntimeError("this window has no status bar")
        status_bar().addPermanentWidget(widget)
        self._status_widgets.append(widget)

    # ------------------------------------------------------------- library

    def notify_library_changed(self, paths: list[Path]) -> None:
        """*paths* 配下のファイルを外部的に書き換えたことを本体へ通知する。

        コンテンツを書き込むプラグイン（取得・変換・タグ付け等）が
        書き込み完了後に呼ぶ。本体はフォルダプレビューキャッシュの該当
        サブツリーを無効化し、表示中のペインが影響範囲なら選択を保ったまま
        再スキャンする。in-place 上書きは親フォルダの mtime を変えないため、
        この通知なしではキャッシュが古い答えを返し続ける。

        ジョブ完了などのまとまった単位で呼ぶこと（ファイル 1 件ごとに
        呼ばない）。GUI スレッドから呼ぶこと。
        """
        notify = getattr(self._window, "notify_library_changed", None)
        if callable(notify):
            notify(list(paths))

    # -------------------------------------------------------- escape hatch

    @property
    def window(self) -> QWidget:
        """``ViewerWindow`` の生参照（**脱出ハッチ — 互換性の保証なし**）.

        安定 API に無いことはここから何でもできるが、内部の属性・構造は
        本体バージョンで予告なく変わる。使う場合は GitHub のソースコードを
        読み、``hasattr`` ガード等で欠落に耐える書き方を推奨。
        """
        return self._window

    # ----------------------------------------------------------- lifecycle

    def cleanup(self) -> None:
        """このコンテキスト経由で追加された寄稿を回収する（host が呼ぶ）。

        回収するのは 5 面: メニュー項目 / 独自トップメニュー / ステータス
        ウィジェット / 右クリック寄稿 / **``ctx.events`` の購読**（proxy を
        共有 events から切り離す）。プラグイン自身が作ったそれ以外の
        リソース（タイマー・スレッド・本体シグナルへの直接接続）は
        ``deactivate()`` で自前で片付けること。
        """
        for menu, action in self._actions:
            try:
                menu.removeAction(action)
                # QAction は生成時にメニューを親にしているので、removeAction
                # だけでは子オブジェクトとしてメニューの寿命いっぱい残る
                # （top_menus / status_widgets は deleteLater 済み — 対を揃える）。
                action.setParent(None)
                action.deleteLater()
            except RuntimeError:  # menu already deleted (window teardown)
                pass
        self._actions.clear()
        for menu in self._top_menus:
            try:
                bar = self._window.menuBar()  # type: ignore[attr-defined]
                bar.removeAction(menu.menuAction())
                menu.deleteLater()
            except RuntimeError:
                pass
        self._top_menus.clear()
        for widget in self._status_widgets:
            try:
                status_bar = getattr(self._window, "statusBar", None)
                if callable(status_bar):
                    status_bar().removeWidget(widget)
                widget.deleteLater()
            except RuntimeError:  # widget/bar already deleted (teardown)
                pass
        self._status_widgets.clear()
        unregister_context_menu_contributors(self._manifest.id)
        # 5 面目: ctx.events の購読。proxy を共有 events から切り離せば、
        # プラグインが自分で disconnect しなくても（= activate 失敗で
        # deactivate が呼ばれない経路でも）以後のイベントは届かない。
        # proxy 自体は deleteLater しない — プラグイン側に残ったハンドラが
        # 参照していると「C++ object already deleted」を踏むだけで、参照が
        # 尽きれば通常の GC で回収される。
        self._events.detach()


__all__ = ["PluginContext", "PluginEvents"]
