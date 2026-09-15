"""プラグインのロード・activate/deactivate 実行機構（Qt 非依存）.

:class:`PluginHost` は「どのフォルダにどんなプラグインがあるか」
（:mod:`manifest`）と「ユーザーがどれを許可したか」（:mod:`store`）を束ね、
有効なものだけを importlib でロードして ``activate(ctx)`` を呼ぶ。

``ctx``（プラグインに渡すオブジェクト）は **factory 注入** — GUI 起動時は
:mod:`bootstrap` が :class:`~snappix.viewer.plugin_host.context.PluginContext`
を作る factory を渡し、テストはスタブを渡す。これで本モジュールは Qt に
依存せず、オフスクリーン Qt すら無しでロード機構を検証できる。

失敗の扱い（設計方針: プラグインの失敗で viewer を殺さない）:

* ``activate()`` の例外は捕捉してそのプラグインを自動無効化する（store が
  記録するのは決定だけで、理由はモーダルとログへ）。他のプラグインと
  viewer 本体は続行する。失敗したプラグインが
  途中まで寄稿したもの（context の UI 寄稿・:mod:`..ai_pack` のエンジン
  provider）は回収し、**activate 失敗 = そのプラグインの寄稿は残らない**を
  基盤側で保証する。
* except で捕まらないハードクラッシュ（C 拡張の access violation 等）は
  クラッシュセンチネル（:mod:`store`）が次回起動時に検出して自動無効化する。

import の形: プラグインフォルダを 1 つのパッケージとして
``snappix_plugin__<id>`` の名前でロードする（``submodule_search_locations``
を設定するのでプラグイン内の相対 import ``from . import foo`` が使える）。
``vendor/`` サブフォルダがあれば ``sys.path`` 末尾に追加し、プラグインが
同梱した純 Python / 同一 ABI の C 拡張パッケージ（numpy 等）を import
できるようにする（末尾なので本体のモジュールを影で上書きできない）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from ...common.i18n import t
from .manifest import (
    PLUGIN_API_VERSION,
    BrokenPlugin,
    PluginManifest,
    discover_plugins,
)
from .store import PluginStore, clear_sentinel, write_sentinel
from .trust import TrustDecision, trust_decision

#: プラグインモジュールを sys.modules に登録するときの名前接頭辞。
#: フラットな一意名（ドットなし親パッケージ不要）+ submodule_search_locations
#: でパッケージ性を持たせる。
MODULE_PREFIX = "snappix_plugin__"

#: vendor サブフォルダ名（sys.path へ追加される同梱ライブラリ置き場）。
VENDOR_DIR_NAME = "vendor"


def _ai_provider_state() -> tuple:
    """AI エンジンの単一スロットの現在値 ``(provider, owner)``（:mod:`..ai_pack`）。

    ``ai_pack`` は Qt 非依存なので host の「Qt に依存しない」契約は保たれる
    （import は呼び出し時まで遅延させ、モジュール import の依存も足さない）。
    """
    from .. import ai_pack

    return (ai_pack.provider(), ai_pack.provider_owner())


def _restore_ai_provider(before: tuple) -> None:
    """activate 失敗で残った provider を *before*（``(provider, owner)``）へ戻す（項目#85）。

    ``ai_pack._provider`` はモジュールグローバルの**単一スロット**で、任意の
    プラグインが ``register_provider`` を呼べる。activate が失敗したプラグイン
    の登録をそのまま残すと、後勝ちで正規 AI エンジンを追い出したまま
    （自動無効化されたプラグインの provider で）セッションが続き、AI 検索が
    「索引が壊れています」等の誤案内付きで死ぬ。「activate 失敗 = そのプラグ
    インの provider は残らない」を**基盤側で**保証する（公式プラグインが
    register_provider を最後に呼ぶ自主規律だけに頼らない）。

    巻き戻しは登録/解除 API 経由なので、購読者コールバック（MainWindow の
    インデックス再読込）も発火し、追い出された正規エンジンで UI が復帰する。
    """
    from .. import ai_pack

    provider, owner = before
    cur_provider, cur_owner = _ai_provider_state()
    # 実体の一致は**同一性**で見る（``owner`` は文字列なので ``==`` で可）。
    # タプルの ``==`` は provider の ``__eq__`` を呼ぶので、(a) 値等価を
    # 定義した provider では別実体でも「変わっていない」と読んで巻き戻しが
    # 不発になり、(b) ``__eq__`` が送出する provider では例外が下の try の
    # **外**から伝播して、この関数の「巻き戻しの失敗で activate 経路を
    # 殺さない」保証ごと破れる（呼び出し元は activate / deactivate）。
    if cur_provider is provider and cur_owner == owner:
        return
    try:
        if provider is None:
            ai_pack.unregister_provider()
        else:
            ai_pack.register_provider(provider, owner=owner)
    except BaseException:  # noqa: BLE001 — 巻き戻しの失敗で activate 経路を殺さない
        logger.warning(
            "ai provider rollback failed:\n{}", traceback.format_exc()
        )


def _purge_modules(mod_name: str) -> None:
    """*mod_name* とその全サブモジュールを ``sys.modules`` から除去する。"""
    prefix = mod_name + "."
    for name in [
        n for n in sys.modules if n == mod_name or n.startswith(prefix)
    ]:
        sys.modules.pop(name, None)


def _release_ai_provider(
    pid: str, lp: "LoadedPlugin", loaded: "dict[str, LoadedPlugin]"
) -> None:
    """deactivate 後に、そのプラグインが残した provider を引き揚げる（項目#172）。

    activate 失敗の巻き戻し（:func:`_restore_ai_provider`）と対称の保証を
    deactivate 側にも置く: **無効化したプラグインの provider は残らない**を
    基盤側で保証し、プラグインが ``deactivate()`` で自分の登録を解除する
    自主規律だけに頼らない。``deactivate()`` は duck-typing の任意実装
    （docs/PLUGIN_DEVELOPMENT.md）なので、それを持たない第三者プラグインが
    provider を登録していると、UI 寄稿は ``context.cleanup()`` で回収された
    のにエンジンだけ生き残る — activate 側で塞いだのと同じ「乗っ取られた
    単一スロット」状態になる。

    引き揚げるのは **このプラグインが登録した実体がまだ載っているとき
    だけ**。後から別のプラグインが登録した provider を、無効化の巻き添えで
    落とさないための条件（単一スロットは後勝ちなので、素朴に
    ``provider_before`` へ戻すと他プラグインの登録を消してしまう）。判定の
    材料は 2 つ:

    * **所有者の名乗り**（``register_provider(owner=...)`` — ``ai_pack`` が
      登録時に控える）。名乗りがあればそれだけで決まるので、``activate`` の
      中で登録したか**後から**（メニュー操作・遅延初期化）登録したかに
      依らない。
    * 名乗りが無い provider（owner を渡さない第三者プラグイン）は従来どおり
      activate 直後のスナップショット（``provider_after``）が今もスロットに
      居るかで推定する。この推定では activate 後の登録を拾えない。

    どちらの経路でも、プラグインが自分で ``unregister_provider`` 済みなら
    現在値が一致せず no-op。

    戻す先は原則 ``provider_before``（activate 失敗の巻き戻しと同じ「その
    プラグインが来る前」）だが、それが**既に無効化済みの別プラグインの
    実体**なら ``None`` にする — 引き揚げのついでに、止めたはずのエンジンを
    復活させないため。
    """
    current, current_owner = _ai_provider_state()
    if current is None:
        return  # スロットは空 — 引き揚げるものが無い
    if current_owner is not None:
        if current_owner != pid:
            return  # 別のプラグインが名乗って載せた実体 — 触らない
    else:
        after = lp.provider_after[0]
        if after is None or after is lp.provider_before[0] or current is not after:
            # このプラグインは登録していない / 自分で解除済み / 後から別
            # プラグインが載せた（名乗りが無いので推定に頼る枝）。
            return
    before = lp.provider_before
    if before[0] is not None and any(
        other is not lp
        and not other.active
        and other.provider_after[0] is before[0]
        for other in loaded.values()
    ):
        before = (None, None)  # 既に無効化されたプラグインの実体は復活させない
    logger.info("plugin {!r} left its ai provider registered; releasing it", pid)
    _restore_ai_provider(before)


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    module: Any
    context: Any
    active: bool = True
    #: activate 直前 / 直後に ``ai_pack`` の単一スロットに載っていた
    #: ``(provider, owner)``。deactivate でこのプラグインの登録だけを引き揚げる
    #: ための目印（:func:`_release_ai_provider` — 所有者を名乗らない
    #: プラグイン向けの推定材料）。
    provider_before: tuple = (None, None)
    provider_after: tuple = (None, None)


@dataclass
class PluginHost:
    """検出済みプラグインの目録と、ロード/活性化の実行者。

    ``context_factory(manifest) -> ctx`` は activate 直前に 1 プラグイン
    1 回呼ばれる。GUI では :class:`PluginContext` を返す closure。
    """

    plugins_dir: Path
    store: PluginStore
    context_factory: Callable[[PluginManifest], Any]
    data_dir: Path

    manifests: list[PluginManifest] = field(default_factory=list)
    broken: list[BrokenPlugin] = field(default_factory=list)
    loaded: dict[str, LoadedPlugin] = field(default_factory=dict)
    #: 直近の :meth:`discover` で ``plugins/`` を**読めた**か。読めないとき
    #: （同名ファイルに塞がれている・権限や NAS で ``iterdir`` が落ちる）の
    #: 検出結果は「1 つも置かれていない」ときと同じ空なので、記録を倒す/
    #: 倒さないの判断にはこの区別が要る（:mod:`.bootstrap` の 2.5 節）。
    scanned: bool = False

    # ---------------------------------------------------------- discovery

    def discover(self) -> None:
        """``plugins/`` を（作成してから）走査して目録を更新する。

        フォルダを ensure するのはここ — ユーザーが「プラグインを置く場所」を
        見つけられるよう、初回起動で空の ``plugins/`` が exe の隣にできる。

        走査は呼ばれるたびに行う。呼び出し元は 2 つだけで、どちらも読み直しを
        必要とする: 起動時の bootstrap（起動につき 1 回 = 走査もこの 1 回）と、
        管理ダイアログを開いた瞬間（開いた時点で置かれたばかりのフォルダを
        拾う）。ウィンドウ構築前の AI 可用性ゲートは ``data/plugins.json`` の
        有効化記録しか読まないので、起動経路にもう 1 回の走査は存在しない。
        """
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # read-only 配置でも検出は続行
            logger.warning("plugins dir ensure failed: {}", exc)
        # 「読めた上で空」と「読めなかった」を分ける（discover_plugins はどちらも
        # 空で返す）。open 1 回ぶんの追加で、記録を倒す側が誤判定しなくなる。
        try:
            with os.scandir(self.plugins_dir) as it:
                next(it, None)
            self.scanned = True
        except OSError as exc:
            logger.warning("plugins dir scan failed: {}", exc)
            self.scanned = False
        # 記録済みフォルダを id 重複の解決ヒントに渡す（なりすまし対策 #37）。
        self.manifests, self.broken = discover_plugins(
            self.plugins_dir, preferred_folders=self.store.preferred_folders()
        )

    def new_decisions(self) -> list[TrustDecision]:
        """初回確認（= 明示同意）が要るプラグインの**判定**一覧。

        未確認の新規 id に加え、**記録済みフォルダと実体がずれた**プラグイン
        （id 詐称でフォルダが挿げ替わった等）も再確認へ落とす。判定そのものは
        :func:`~.trust.trust_decision` — 管理ダイアログの有効化と共有する
        唯一の信頼判定。ここはその答えに副作用を足す層で、``trusted`` と出た
        ものだけフォルダ実体を記録し直す（後方互換の旧レコードを埋める
        ``bind_folder``。記録済みと同値なら no-op）。副作用があるので
        bootstrap の初回確認ループから 1 回呼ぶ想定。

        返すのは判定ごと（マニフェストだけではない）— 理由（未確認 / フォルダ
        不一致 / id 重複）は確認モーダルが何を聞くべきかを決める情報で、
        呼び出し側が ``store`` から再導出すると区別が潰れる。
        """
        needs: list[TrustDecision] = []
        for manifest in self.manifests:
            decision = self._trust_decision(manifest)
            if decision.verdict != "trusted":
                needs.append(decision)
            else:
                # 信頼した実体を記録に固定する。folder 記録の無い旧レコードが
                # 埋まるのはここ — 次回以降は「記録一致」の一般セルで trusted。
                self.store.bind_folder(manifest.id, manifest.dir.name)
        return needs

    def _trust_decision(self, manifest: PluginManifest) -> TrustDecision:
        """信頼判定（:func:`~.trust.trust_decision`）への薄い委譲（副作用なし）。"""
        return trust_decision(
            manifest.id,
            manifests=self.manifests,
            broken=self.broken,
            store=self.store,
        )

    def manifest_for(self, pid: str) -> PluginManifest | None:
        for m in self.manifests:
            if m.id == pid:
                return m
        return None

    def is_active(self, pid: str) -> bool:
        lp = self.loaded.get(pid)
        return bool(lp and lp.active)

    # --------------------------------------------------------- activation

    def activate_enabled(self) -> list[tuple[PluginManifest, str]]:
        """有効と記録された全プラグインを activate。失敗一覧を返す。"""
        failures: list[tuple[PluginManifest, str]] = []
        for m in self.manifests:
            if not self.store.is_enabled(m.id):
                continue
            error = self.activate(m)
            if error is not None:
                failures.append((m, error))
        return failures

    def activate(self, manifest: PluginManifest) -> str | None:
        """*manifest* をロードして ``activate(ctx)`` を呼ぶ。

        成功で ``None``、失敗でユーザー向けエラーメッセージを返す。失敗時は
        store に記録して自動無効化する（次回起動で再び走らない）。
        """
        pid = manifest.id
        if self.is_active(pid):
            return None
        if manifest.api != PLUGIN_API_VERSION:
            error = t(
                "viewer.plugins.err_api_mismatch",
                plugin_api=manifest.api,
                host_api=PLUGIN_API_VERSION,
            )
            self.store.disable_after_failure(pid, error)
            return error

        # ここから先はプラグインコードが走る。ハードクラッシュに備えて
        # 「誰を読み込み中だったか」をセンチネルに残す。
        write_sentinel(self.data_dir, pid)
        context: Any = None
        # 失敗時に巻き戻すため、プラグインコードが走る前の provider を控える。
        provider_before = _ai_provider_state()
        try:
            module = self._load_module(manifest)
            activate_fn = getattr(module, "activate", None)
            if not callable(activate_fn):
                raise AttributeError(
                    t("viewer.plugins.err_no_activate")
                )
            context = self.context_factory(manifest)
            activate_fn(context)
        except BaseException as exc:  # noqa: BLE001 — プラグイン隔離が目的
            clear_sentinel(self.data_dir, pid)
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "plugin {!r} activate failed:\n{}", pid, traceback.format_exc()
            )
            self.store.disable_after_failure(pid, detail)
            # activate 途中まで寄稿された UI/コールバックを回収する
            # （deactivate と同じ duck-typing — context 未生成なら no-op）。
            cleanup_fn = getattr(context, "cleanup", None)
            if callable(cleanup_fn):
                try:
                    cleanup_fn()
                except BaseException:  # noqa: BLE001
                    logger.warning(
                        "plugin {!r} context cleanup failed:\n{}",
                        pid, traceback.format_exc(),
                    )
            # 寄稿 UI と同様に、登録された AI エンジン provider も回収する。
            _restore_ai_provider(provider_before)
            _purge_modules(MODULE_PREFIX + pid)
            return detail
        clear_sentinel(self.data_dir, pid)
        self.loaded[pid] = LoadedPlugin(
            manifest=manifest,
            module=module,
            context=context,
            # 無効化のときに「このプラグインが載せた provider」だけを引き揚げる
            # ための目印（項目#172 — deactivate 側の対称な保証）。
            provider_before=provider_before,
            provider_after=_ai_provider_state(),
        )
        logger.info("plugin {!r} v{} activated", pid, manifest.version)
        return None

    def _load_module(self, manifest: PluginManifest) -> Any:
        vendor = manifest.dir / VENDOR_DIR_NAME
        if vendor.is_dir():
            vendor_str = str(vendor)
            # 末尾追加: 本体・stdlib のモジュールをプラグイン同梱物が
            # 意図せず（あるいは悪意で）影に置けないようにする。
            if vendor_str not in sys.path:
                sys.path.append(vendor_str)
        mod_name = MODULE_PREFIX + manifest.id
        # 再有効化（同セッション内の disable → enable）は素の再実行にする。
        # トップモジュールだけでなくサブモジュール（``snappix_plugin__x.helper``
        # 等）も掃除しないと、再実行された ``from . import helper`` が古い
        # キャッシュを返し「素の再実行」契約が破れる。
        _purge_modules(mod_name)
        spec = importlib.util.spec_from_file_location(
            mod_name,
            manifest.entry_path,
            submodule_search_locations=[str(manifest.dir)],
        )
        if spec is None or spec.loader is None:  # pragma: no cover (defensive)
            raise ImportError(f"spec_from_file_location failed: {manifest.entry_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    # ------------------------------------------------------- deactivation

    def deactivate(self, pid: str) -> bool:
        """``deactivate()`` があれば呼ぶ（ベストエフォート）。

        戻り値はクリーンに停止できたか。``deactivate`` を持たない・失敗した
        プラグインは「次回起動から無効」の遅延セマンティクスに落ちる
        （UI 側が再起動を案内する）。

        遅延セマンティクスでも **AI エンジンの provider だけは今すぐ引き揚げる**
        （:func:`_release_ai_provider` — 項目#172）: 単一スロットに居座った
        まま UI 骨組みだけ消えると、無効化したはずのプラグインのエンジンで
        セッションが続く。activate 失敗時の巻き戻しと対称の保証。
        """
        lp = self.loaded.get(pid)
        if lp is None or not lp.active:
            return True
        lp.active = False
        clean = False
        deactivate_fn = getattr(lp.module, "deactivate", None)
        if callable(deactivate_fn):
            try:
                deactivate_fn()
                clean = True
            except BaseException:  # noqa: BLE001 — プラグイン隔離が目的
                logger.warning(
                    "plugin {!r} deactivate failed:\n{}", pid, traceback.format_exc()
                )
        # context が UI 回収を提供していれば呼ぶ（PluginContext.cleanup —
        # host 自身は Qt 非依存を保つため duck-typing で任意呼び出し）。
        cleanup_fn = getattr(lp.context, "cleanup", None)
        if callable(cleanup_fn):
            try:
                cleanup_fn()
            except BaseException:  # noqa: BLE001
                logger.warning(
                    "plugin {!r} context cleanup failed:\n{}",
                    pid, traceback.format_exc(),
                )
        # 寄稿 UI を回収したのと同じ理由で、残った AI エンジン provider も
        # 引き揚げる（プラグインの自主規律に頼らない — 項目#172）。
        _release_ai_provider(pid, lp, self.loaded)
        return clean

    def deactivate_all(self) -> None:
        for pid in list(self.loaded):
            self.deactivate(pid)


__all__ = ["PluginHost", "LoadedPlugin", "MODULE_PREFIX", "VENDOR_DIR_NAME"]
