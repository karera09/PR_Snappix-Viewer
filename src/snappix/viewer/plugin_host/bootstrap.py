"""起動時のプラグイン配線（``viewer/app.py`` から 1 回呼ばれる）.

順序（``ViewerWindow`` 構築後・``show()`` 前）:

1. 前回起動のクラッシュセンチネルを確認 — 残っていれば該当プラグインを
   自動無効化してモーダルで通知（design.md「失敗 = モーダル」）。
2. ``plugins/`` を検出。store に記録の無い**新規プラグインごと**に警告付きの
   確認モーダルを出し、ユーザーが有効化を選んだものだけ enabled を記録
   （選ばなければ「確認済み・無効」を記録 = 次回以降は無言）。
3. 有効化済みなのに検出段階で壊れていた（manifest 検証に落ちた）プラグインを
   モーダルで通知 — activate の失敗一覧には現れないため、ここが唯一の告知点。
4. enabled なプラグインを activate。失敗はまとめてモーダル通知し、該当
   プラグインは自動無効化される（host 側）。

セーフモード（``--no-plugins`` / 環境変数 ``SNAPPIX_NO_PLUGINS=1``）では
app.py がこのモジュールを呼ばない — プラグインコードは 1 行も走らない。
"""

from __future__ import annotations

from loguru import logger
from PySide6.QtWidgets import QMessageBox

from ...common.i18n import t
from ...common.paths import AppPaths
from ...common.ui import confirm_action, warn_modal
from ..main_window import ViewerWindow
from .context import PluginContext, PluginEvents
from .host import PluginHost
from .manifest import PluginManifest, clip_untrusted, is_valid_plugin_id
from .store import PluginStore, read_and_clear_sentinel
from .trust import confirm_prompt


def plugins_dir_for(paths: AppPaths):
    """ポータブルベース直下の ``plugins/`` （検出時に ensure される）。"""
    return paths.base / "plugins"


def _is_known_plugin_id(pid: str, store: PluginStore) -> bool:
    """*pid* が id の書式を満たし、かつ store に記録済みか。

    マニフェスト以外の経路（クラッシュセンチネル）から来た文字列を
    ``store.disable_after_failure`` に渡す前のゲート。書式検証は manifest 層の
    単一実装へ委ねる。
    """
    return is_valid_plugin_id(pid) and store.known(pid)


def _disable_vanished(host: PluginHost, pid: str) -> None:
    """実体ごと消えた有効化記録を黙って倒す（告知するものが無いケース）。

    ユーザーが ``plugins/<id>/`` を消しただけの通常操作なので、名指しできる
    破損フォルダは無く、モーダルを出す理由も無い。ただし記録を残してはいけない
    — 起動前ゲートは有効化記録しか読まないので、残すと実体の無い AI UI の
    骨組みが毎起動立ち、管理ダイアログにその id の行が無いので止められない。

    倒してよいのは ``plugins/`` を**読めた上で**居ないときだけ。読めない
    （同名ファイルに塞がれている・権限や NAS で落ちる）ときの検出結果は
    「1 つも置かれていない」と同じ空なので、そこで倒すと**無関係なプラグイン
    まで**無言で無効になり、フォルダが読めるようになっても無効のままになる。
    """
    if not host.scanned:
        return
    host.store.disable_after_failure(pid, t("viewer.plugins.err_load_unresolved"))


def bootstrap_plugins(
    window: ViewerWindow,
    paths: AppPaths,
    *,
    store: PluginStore | None = None,
) -> PluginHost:
    """プラグイン基盤を組み立てて有効なプラグインを activate する。

    ``plugins/`` の走査はここ（``host.discover()``）が起動時の唯一の実施点 —
    ウィンドウ構築前の可用性ゲート（``ai_pack.maybe_enable_from_plugins``）は
    ``data/plugins.json`` の有効化記録しか読まないので、起動 1 回につき走査も
    1 回で、2 つの答えがずれようが無い。

    *store* は可用性ゲートが開いた同じインスタンスを共有する（渡されなければ
    ここで開く）— 記録の書き込みはメモリ上の写しを更新するので、1 プロセスに
    2 つ開くと片方の決定がもう片方から見えない。
    """
    from snappix import __version__

    if store is None:
        store = PluginStore(paths.data / "plugins.json")

    # 1. 前回 activate 中のハードクラッシュ検出（Firefox 式セーフモード判定）。
    crashed_pid = read_and_clear_sentinel(paths.data)
    # センチネルの 1 行目は未検証のファイル内容（別プロセスの書き込み途中・
    # 手編集・文字化け）。そのまま disable_after_failure へ渡すと、実在しない id の
    # レコードが plugins.json に永久に残る（掃除口が無く、管理ダイアログの
    # 一覧にも出ないので気付けない）。書式と記録の有無を確かめてから書く —
    # 記録の無い id はそもそも activate されていないので、無効化する対象が
    # 存在しない（＝告知するべき「自動無効化」も起きていない）。
    if crashed_pid and _is_known_plugin_id(crashed_pid, store):
        store.disable_after_failure(crashed_pid, t("viewer.plugins.err_crash_last_run"))
        # crashed_pid はセンチネル（data/plugin_loading.flag）1 行目由来の値。
        # 書式と記録の有無は上で確かめてあるが、plugins.json 自体もユーザー／
        # 攻撃者が書ける場所なので表示の扱いは未検証文字列と同じに保つ:
        # 長さは本文の断片の上限（``clip_untrusted``）で切り、本文は
        # ``warn_modal`` のプレーンテキスト既定で出す（AutoText は HTML として
        # 解釈され、「プラグインが異常終了した」という最も注意を要する警告の
        # 表示を攻撃者が操作できてしまう）。
        warn_modal(
            window,
            title=t("viewer.plugins.activate_failed_title"),
            body=t(
                "viewer.plugins.crash_disabled_body",
                pid=clip_untrusted(crashed_pid),
            ),
        )
    elif crashed_pid:
        # 記録もしていないし無効化もしていない — 「このプラグインを自動的に
        # 無効化しました」と名乗るモーダルは出さない（何もしていないことを
        # 告げる嘘になる）。痕跡はログだけに残す。
        logger.warning(
            "crash sentinel names an unknown plugin id; not recording it: {!r}",
            crashed_pid[:80],
        )

    events = PluginEvents(window)
    # 安定 API のイベントを既存シグナルへ配線する。選択イベントは左ペインの
    # file/folder 選択の合流。root_changed は ViewerWindow.set_root が
    # ``_plugin_events`` 経由で emit する（main_window 側の 1 行フック）。
    window._plugin_events = events
    window._post_grid.file_selected.connect(events.selection_changed.emit)
    window._post_grid.folder_selected.connect(events.selection_changed.emit)

    def _context_factory(manifest: PluginManifest) -> PluginContext:
        return PluginContext(
            manifest,
            window=window,
            events=events,
            data_root=paths.data,
            app_version=__version__,
        )

    host = PluginHost(
        plugins_dir=plugins_dir_for(paths),
        store=store,
        context_factory=_context_factory,
        data_dir=paths.data,
    )
    host.discover()

    # 2. 初回確認（常に無効で始まり、明示同意でのみ有効化）。新規 id に加え、
    #    記録済みフォルダと実体がずれた（なりすまし疑いの）プラグインもここへ。
    for decision in host.new_decisions():
        manifest = decision.manifest
        if manifest is None:  # pragma: no cover (defensive — 勝者不在は載らない)
            continue
        # 何を聞くかは判定の理由が決める（未確認 / フォルダ挿げ替え / id 重複）。
        # 文言の対応表は trust.py に 1 つだけ置く — ここで store から理由を
        # 再導出すると、id 重複がフォルダ挿げ替えの文面に潰れる。
        title, body = confirm_prompt(decision)
        # 本文には未検証のマニフェスト文字列（name / author / description）が
        # 入る。QMessageBox 既定の AutoText だと HTML として解釈され、
        # 「なりすまし疑い」を伝えるこの警告そのものの表示を攻撃者が
        # 操作できてしまう。管理ダイアログの詳細ラベル
        # （dialog.py）と同じくプレーンテキストを明示する（``plain_text``）。
        #
        # ボタンは動詞。ここで同意しているのは「このフォルダの任意の
        # コードをビューアのプロセス内で実行してよい」ことで、「はい」では
        # 何に同意したのかが残らない — 押すボタン自体が行為を名乗る。
        approved = confirm_action(
            window,
            title=title,
            body=body,
            accept_text=t("viewer.plugins.trust_enable_btn"),
            reject_text=t("viewer.plugins.trust_decline_btn"),
            icon=QMessageBox.Icon.Warning,
            plain_text=True,
        )
        # 有効化する / しない どちらでも「確認済み」を記録し、確認したフォルダ
        # 実体を束ねる（次回以降は同フォルダなら無言・別フォルダなら再び
        # 再確認になる）。断ったときはそのことも残す — 記録が一致するだけで
        # 信頼判定は ``trusted`` を返すので、警告を出したその実体を管理
        # ダイアログのチェック 1 つで無警告に有効化できてしまう。
        if approved:
            store.set_enabled(manifest.id, True, folder=manifest.dir.name)
        else:
            store.record_declined(manifest.id, manifest.dir.name)

    # 2.5. 「有効なのに検出結果に勝者が居ない」プラグインの始末。host.broken は
    #      manifest 検証の前に脱落したフォルダなので activate_enabled() の失敗
    #      一覧には現れない。告知が無いと、ユーザーが有効化したはずの機能が
    #      理由の説明なしに丸ごと消える（管理ダイアログを開かない限り気付けない）。
    #      判定軸は 1 つ（勝者が居ない）で、告知の文面だけが記録の持ち物で変わる:
    #
    #      * フォルダ実体を記録したレコード → そのフォルダが壊れていれば名指しで。
    #      * folder を持たない旧レコード → 実体で突き合わせられないので、読み込め
    #        なかったフォルダ名をまとめて知らせる。
    #      * どちらでもない（フォルダごと消えている）→ 黙る。プラグインを自分で
    #        消しただけの通常操作なので、名指しできるものが何も無い。
    #
    #      **告知したかどうかに関わらず記録は必ず倒す**（activate 失敗 =
    #      host.activate → disable_after_failure と同じ）。告知した口で残すと毎起動
    #      同じモーダルが出続け、管理ダイアログの「読み込めなかった」行には
    #      チェックボックスが無いので止める手段が無い。黙った口で残すと、起動前
    #      ゲート ai_pack.maybe_enable_from_plugins が毎起動その有効化記録だけを
    #      読んで、実体の無い AI UI の骨組みが恒久的に立つ（管理ダイアログには
    #      消えた id の行が無いので、これも止める手段が無い）。どちらも原因を
    #      取り除けば管理ダイアログから再度有効化できる。
    broken_dirs = {b.dir.name: b for b in host.broken}
    all_broken_folders = "\n".join(b.dir.name for b in host.broken)
    live_ids = {m.id for m in host.manifests}
    for pid in store.enabled_ids():
        folder = store.folder(pid)
        if folder is not None:
            broken = broken_dirs.get(folder)
            if broken is None:
                if pid in live_ids:
                    continue
                # 記録したフォルダが壊れてすらいない = 実体ごと消えている。
                _disable_vanished(host, pid)
                continue
            # error は未検証の例外メッセージ（プラグイン側＝攻撃者の制御下）。
            body = t(
                "viewer.plugins.load_failed_body",
                folder=folder,
                error=broken.error,
            )
            reason = broken.error
        else:
            if pid in live_ids:
                continue
            if not host.broken:
                _disable_vanished(host, pid)
                continue
            # フォルダ名は未検証の外部入力（``plugins/`` 直下の実体名）。
            body = t(
                "viewer.plugins.load_failed_unresolved_body",
                pid=pid,
                folders=all_broken_folders,
            )
            reason = t("viewer.plugins.err_load_unresolved")
        warn_modal(
            window, title=t("viewer.plugins.load_failed_title"), body=body,
        )
        store.disable_after_failure(pid, reason)

    # 3. 有効なプラグインを activate（失敗は host が自動無効化済み）。
    failures = host.activate_enabled()
    for manifest, error in failures:
        # name（未検証マニフェスト）と error（プラグインの例外メッセージ）は
        # どちらも攻撃者の制御下にあり、AutoText だと文頭の name が
        # mightBeRichText を真にして本文が HTML 解釈される。
        # ``warn_modal`` のプレーンテキスト既定がそれを塞ぐ。
        warn_modal(
            window,
            title=t("viewer.plugins.activate_failed_title"),
            body=t(
                "viewer.plugins.activate_failed_body",
                name=manifest.name,
                error=error,
            ),
        )

    window.attach_plugin_host(host)
    # 起動フォルダの着地は window 構築時（bootstrap 前）に済んでいるため、
    # activate 内で root_changed を購読したプラグインには届いていない。
    # PluginEvents の契約（起動時を含むすべての着地）を満たすようここで
    # 1 回 emit して現在ルートを配信する。
    current_root = getattr(window, "_root", None)
    if current_root is not None:
        events.root_changed.emit(current_root)
    return host


__all__ = ["bootstrap_plugins", "plugins_dir_for"]
