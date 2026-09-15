"""Viewer plugin system (フォルダ投入式プラグイン基盤).

``plugins/``（ポータブルベース直下）に置かれたプラグインフォルダを検出し、
明示的に有効化されたものだけを import して ``activate(ctx)`` を呼ぶ。

モジュール構成（Qt 依存を分離してある — テストは Qt 無しでコアを回せる）:

* :mod:`manifest` — ``plugin.json`` のスキーマ・検出（Qt 非依存・純ロジック）
* :mod:`store` — 有効/無効状態の永続化 ``data/plugins.json`` とクラッシュ
  センチネル（Qt 非依存）
* :mod:`trust` — 「この検出結果を信頼して実行に進めてよいか」の唯一の判定
  （Qt 非依存の純関数。bootstrap の初回確認と管理ダイアログの有効化が
  共有する）
* :mod:`host` — importlib によるロード・activate/deactivate の実行機構
  （Qt 非依存。context は factory 注入なので GUI 無しでテスト可能）
* :mod:`context` — プラグインに渡す :class:`PluginContext`（安定 API 層）と
  :class:`PluginEvents`（Qt シグナル）
* :mod:`bootstrap` — 起動時の配線（検出 → 初回確認 → 有効分の activate）
* :mod:`dialog` — プラグイン管理ダイアログ

セキュリティ方針: プラグインは任意の Python コードとして実行される。
新しく検出されたプラグインは**常に無効**で始まり、ユーザーが警告付きの
確認を経て明示的に有効化するまで一切実行されない。ロード/activate に
失敗したプラグインは自動的に無効へ戻す。``--no-plugins``（または環境変数
``SNAPPIX_NO_PLUGINS=1``）ですべてのプラグインを読み込まないセーフモードで
起動できる。

開発者向けドキュメントはリポジトリ/配布フォルダの
``docs/PLUGIN_DEVELOPMENT.md``（ビルドが dist の plugins/ へ自動配置）。
"""

from __future__ import annotations

from .manifest import (  # noqa: F401 (re-exports)
    PLUGIN_API_VERSION,
    BrokenPlugin,
    ManifestError,
    PluginManifest,
    discover_plugins,
    load_manifest,
)
from .store import PluginStore  # noqa: F401
from .host import PluginHost  # noqa: F401

__all__ = [
    "PLUGIN_API_VERSION",
    "BrokenPlugin",
    "ManifestError",
    "PluginManifest",
    "PluginHost",
    "PluginStore",
    "discover_plugins",
    "load_manifest",
]
