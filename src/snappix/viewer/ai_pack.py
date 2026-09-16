"""AI 検索機能パックの可用性レジストリ + provider シーム（有償プラグイン連携）.

Snappix Viewer の AI 検索（AIタグ検索 / 意味・類似検索 / NSFW 非表示 /
詳細ウィンドウの AI タグ節など）は、**公式 AI プラグイン
（``plugins/snappix_ai/``）が導入・有効化されているときだけ** UI に現れる。
本モジュールはその二段ゲートを提供する:

1. **可用性フラグ**（:func:`available`） — 起動時に 1 回、**ウィンドウ構築より
   前**に判定する（:func:`maybe_enable_from_plugins` — ``data/plugins.json``
   の有効化記録を読むだけで、``plugins/`` の走査もプラグインコードの実行も
   しない）。AI UI の骨組みを構築するかどうかのゲート。
2. **provider レジストリ**（:func:`register_provider` / :func:`provider`） —
   検索エンジンの実体（TagIndex / VectorIndex / スキャナ工場）。エンジンの
   コードは**ビューア本体に存在せず**、プラグインパッケージ
   （``plugins/snappix_ai/engine/``）に物理的に住む。プラグインの
   ``activate(ctx)``（ウィンドウ構築後）が provider を登録すると、
   :func:`add_provider_callback` で登録されたコールバック（MainWindow の
   インデックス再読込）が発火して UI が点灯する。

設計方針（ビジネス上の合意 2026-07）:

* 素の配布では **AI 関連の UI が一切表示されず、検索エンジンの
  コード（tag_db / vector_index / スキャナ）と numpy が物理的に同梱されない**。
  有効化記録を偽装しても provider が不在なため、tags.db 不在時と同じ
  「骨組みだけ・検索不能」に劣化する。
* ``numpy`` を要する vector_index は provider 経由でのみ遅延 import され、
  プラグインが ``vendor/`` で numpy を持参する（``snappix_viewer.spec`` の
  excludes と併せて素のビルドから排除）。``vendor/`` を ``sys.path`` へ載せる
  のは activate 時の plugin_host（``host._load_module``）だけ — provider の
  遅延 import はそれより後にしか走らないので、起動前のゲートがパスに触る
  必要は無い。
* tags.db 不在時の「表示されるが無効 + 案内」の既存挙動は、パック有効時の
  挙動としてそのまま維持する。provider 未登録もこの劣化シームに乗る。

テストは ``tests/conftest.py`` の autouse フィクスチャが :func:`register` と
:func:`register_provider`（プラグイン実体の provider）を呼び、従来（AI あり）の
挙動で全テストが走る。素モードのテストは両方を外して検証する
（``tests/test_viewer_ai_gating.py``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from loguru import logger

#: 公式 AI プラグインの plugin id（``plugins/<この名前>/plugin.json``）。
OFFICIAL_PLUGIN_ID = "snappix_ai"

#: tags.db のファイル名（``data/`` 直下）。エンジン移動後もビューア側
#: （tags.db watcher 等）が参照する唯一の tags.db 由来定数。
TAGS_DB_NAME = "tags.db"

_available = False

#: 登録済み provider（プラグインの activate が登録）。duck-typed:
#: ``open_tag_index(data_dir)`` / ``open_vector_index(data_dir)`` /
#: ``create_tag_scanner(tag_index=, folder_cache=, parent=)`` /
#: ``create_vector_scanner(vector_index=, folder_cache=, parent=)`` を持つこと。
#: 任意メソッド: ``open_tag_index_status(data_dir)``（開けない理由まで返す）、
#: ``models_doc_path() -> Path | None``（パック同梱のセットアップガイドの
#: 実パス — 本体がパック内部のフォルダ構成を知らずに済むための口。#178）。
_provider = None

#: :func:`register_provider` に渡された所有者トークン（通常は登録した
#: プラグインの id）。名乗らずに登録した provider では ``None``。
#: 「単一スロットに今載っているのは誰の実体か」を**登録時に**確定させる
#: ための目印で、activate 直後のスナップショット推定（host 側）では拾えない
#: 「activate より後に登録された provider」も所有者が分かる。
_provider_owner = None

#: 直近の :func:`open_tag_index` が下した判定（UIレビュー 07-25 #114）:
#: ``"unavailable"``（パック無効 / provider 不在）/ ``"missing"``（tags.db が
#: 無い）/ ``"empty"``（開けたがまだ画像が入っていない）/ ``"error"``
#: （ファイルはあるが開けない = 壊れている疑い）/ ``"ok"``。
#: 従来はこの 4 通りが全て ``None`` に潰れており、破損した tags.db にも
#: 「タガーでスキャンしてください」という直らない案内を出していた。
_TAG_INDEX_STATUSES = ("unavailable", "missing", "empty", "error", "ok")
_tag_index_status = "unavailable"

#: provider 登録時に発火するコールバック（GUI スレッドで呼ばれる —
#: プラグインの activate は GUI スレッドで走る）。MainWindow がインデックス
#: 再読込をぶら下げる。
_provider_callbacks: list[Callable[[], None]] = []

#: 直近の登録/解除コールバック発火で購読者が失敗したか（項目#189）。
#: register_provider は購読者例外を握って続行する（1 購読者の失敗で activate
#: 全体を落とさない）ため、「provider は登録されたのに UI 反映（インデックス
#: 再読込 → バナー/タイトル更新）が途中で死んだ」状態が無記録のまま残り得た。
#: :func:`provider_ui_sync_failed` がその唯一の読み取り口。
_provider_ui_sync_failed = False


def available() -> bool:
    """AI 機能パックが有効か（AI UI を構築・表示してよいか）。"""
    return _available


def register() -> None:
    """AI 機能パックを有効化する（起動時ゲート / テスト用）。"""
    global _available
    _available = True


def unregister() -> None:
    """AI 機能パックを無効化する（テスト用）。

    (UIレビュー07-25 追修) 併せて :func:`tag_index_status` を初期値
    ``"unavailable"`` へ戻す — 無効化後も直前の ``"ok"`` / ``"error"`` が
    残ると、パック不在の状態で「索引が壊れています」等の案内が出得た
    （:func:`open_tag_index` が同じ状況で書く値と一致させる）。
    """
    global _available, _tag_index_status
    _available = False
    _tag_index_status = "unavailable"


# ---------------------------------------------------------------- provider

def provider():
    """登録済みの AI エンジン provider（未登録は ``None``）。"""
    return _provider


def models_doc_path() -> "Path | None":
    """パック同梱のセットアップガイド（tagger-models.md 相当）の実パス。

    provider の任意メソッド ``models_doc_path()`` に委譲する（#178: 本体は
    「どのパックが何というファイルをどこに持つか」を知らない — パスの知識は
    パック側にある）。provider 未登録・メソッド未実装・解決失敗は ``None``
    （ヘルプ側は「ファイルが見つかりません」案内へ劣化する）。
    """
    getter = getattr(_provider, "models_doc_path", None)
    if getter is None:
        return None
    try:
        path = getter()
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("ai_pack: provider models_doc_path() failed: {}", exc)
        return None
    return path if isinstance(path, Path) else None


def provider_owner():
    """今載っている provider を登録した所有者トークン（名乗り無し / 未登録は ``None``）。"""
    return _provider_owner


def register_provider(p, *, owner=None) -> None:
    """AI エンジン provider を登録し、登録コールバックを発火する。

    公式プラグインの ``activate(ctx)`` が呼ぶ（GUI スレッド）。ウィンドウは
    provider 未登録（インデックス None）で構築済みなので、コールバック側
    （MainWindow のインデックス再読込）が既存の tags.db 再読込経路で UI を
    点灯させる。コールバック内の例外は握って続行する — 1 つの購読者の失敗が
    プラグインの activate 全体を失敗（自動無効化）させないため。

    *owner* を渡すと「このスロットに載っているのは誰の実体か」が記録され、
    :func:`unregister_provider` と plugin_host の引き揚げがその名乗りだけで
    判断できる（``activate`` の中で登録したか後から登録したかに依らない）。
    名乗らない登録も従来どおり受け付ける（所有者不明として扱われる）。
    """
    global _provider, _provider_owner
    _provider = p
    _provider_owner = owner
    _fire_provider_callbacks("register")


def provider_ui_sync_failed() -> bool:
    """直近のコールバック発火で購読者（UI 反映）が失敗したか（項目#189）.

    True のとき provider 自体は登録済みだが、MainWindow のインデックス
    再読込〜UI 点灯が途中で例外死しており、AI UI は劣化シーム（表示されるが
    無効 + 案内）のまま残っている可能性がある。診断・テストの読み取り用。
    """
    return _provider_ui_sync_failed


def _fire_provider_callbacks(event: str) -> None:
    """登録/解除コールバックを発火し、失敗を記録する（項目#189）.

    例外は従来どおり握って続行する（1 購読者の失敗でプラグインの activate
    全体を失敗＝自動無効化させないため）が、握り潰しを無記録にしない:
    「provider は登録されたが UI 反映に失敗した」ことをフラグ
    （:func:`provider_ui_sync_failed`）とトレースバック付きログに残す。
    """
    global _provider_ui_sync_failed
    _provider_ui_sync_failed = False
    for cb in list(_provider_callbacks):
        try:
            cb()
        except Exception:
            _provider_ui_sync_failed = True
            logger.exception(
                "ai_pack provider {} callback failed — provider は{}済みだが"
                " UI 反映が未完了のまま（AI UI は劣化シームに残ります）",
                event,
                "登録" if event == "register" else "解除",
            )


def unregister_provider(*, owner=None) -> None:
    """provider の登録を解除する（プラグインの deactivate / テスト用）。

    登録時と対称にコールバックを発火する — 購読者（MainWindow）は
    :func:`open_tag_index` が ``None`` を返すようになった状態で再読込し、
    既存の「tags.db 消失」劣化に落ちる。

    *owner* を渡すと**自分が登録した実体がまだ載っているときだけ**引き揚げる
    （スロットは後勝ちの単一枠なので、名乗らずに解除すると後から別の
    プラグインが登録した provider を巻き添えで落とせる）。所有者が違う／
    既に別の登録に入れ替わっている場合は何もしない。

    (UIレビュー07-25 追修) :func:`tag_index_status` も初期値へ戻す —
    provider 不在は :func:`open_tag_index` が ``"unavailable"`` と判定する
    状態なので、解除の時点で古い判定（``"ok"`` / ``"error"``）を捨てる。
    """
    global _provider, _provider_owner, _tag_index_status
    if owner is not None and _provider_owner != owner:
        logger.info(
            "ai_pack: keeping the registered provider (owner {!r} != {!r})",
            _provider_owner, owner,
        )
        return
    _provider = None
    _provider_owner = None
    _tag_index_status = "unavailable"
    _fire_provider_callbacks("unregister")


def add_provider_callback(cb: Callable[[], None]) -> None:
    """provider の登録/解除時に呼ばれるコールバックを追加する。"""
    if cb not in _provider_callbacks:
        _provider_callbacks.append(cb)


def remove_provider_callback(cb: Callable[[], None]) -> None:
    """コールバックを外す（ウィンドウの closeEvent から）。"""
    try:
        _provider_callbacks.remove(cb)
    except ValueError:
        pass


def maybe_enable_from_plugins(
    paths, *, store=None, no_plugins: bool = False,
) -> None:
    """公式 AI プラグインが有効化済みなら AI パックを有効化する。

    ``ViewerWindow`` 構築より前（``viewer/app.py``）に呼ぶこと — AI UI の
    骨組みの構築可否が可用性フラグに依存するため。

    読むのは ``data/plugins.json`` の**有効化記録 1 個だけ**（``plugins/``
    の走査もマニフェストの読み込みもしない）。この段でプラグインのコードは
    1 行も走らず、``sys.path`` にも触らない。

    なぜ判定材料が有効化記録だけで足りるか: このフラグが立ててよいのは
    **AI UI の骨組み**だけで、エンジンは provider（ウィンドウ構築後に
    プラグインの ``activate(ctx)`` が登録）が無ければ不在のままになる。
    つまりフラグを偽装しても・記録と実体がずれていても、行き着く先は
    tags.db 不在時と同じ「表示されるが無効 + 案内」の劣化シームで、
    そこから先へ進む道は無い。「このフォルダのコードを走らせてよいか」と
    いう**答えを間違えると危険な**問いは、ユーザーに問い直せるウィンドウ
    構築後（:mod:`~snappix.viewer.plugin_host.bootstrap` と管理ダイアログ
    が共有する :func:`~snappix.viewer.plugin_host.trust.trust_decision`）
    だけが扱う — 起動前のここは、その判定の写しを持たない。

    帰結として、プラグインフォルダを手で消した直後の 1 回だけ「骨組みは
    立つが provider が来ない」状態になる（次の起動では bootstrap が記録を
    無効へ倒すので消える）。上記のとおりこれは既定の劣化シームそのもの。
    """
    if no_plugins:
        return
    if store is None:
        from .plugin_host.store import PluginStore

        store = PluginStore(paths.data / "plugins.json")
    if not store.is_enabled(OFFICIAL_PLUGIN_ID):
        return
    register()
    logger.info("AI feature pack enabled (plugin {!r})", OFFICIAL_PLUGIN_ID)


# ------------------------------------------------------------- index opening


def tag_index_status() -> str:
    """直近の :func:`open_tag_index` の判定（:data:`_TAG_INDEX_STATUSES` のいずれか）。

    UI（AI ポップオーバーのバナー・0 件/エラーカード）が「索引が無い」と
    「索引が壊れている」を出し分けるための唯一の情報源（UIレビュー 07-25 #114）。
    ``None`` 返却だけでは両者を区別できないため、開いた側でここへ記録する。
    """
    return _tag_index_status


def open_tag_index(data_dir: Path):
    """``tags.db`` を読み取り専用で開く（パック無効・provider 不在・失敗は ``None``）。

    併せて :func:`tag_index_status` へ「なぜ使えないか」を記録する。provider が
    任意メソッド ``open_tag_index_status(data_dir) -> (index, status)`` を実装
    していればその判定を採用し、無ければ従来どおり ``open_tag_index`` を呼んで
    ``ok`` / ``missing`` の 2 値に落とす（duck-typed 契約の後方互換）。

    provider のメソッドは engine パッケージを**メソッド内で遅延 import** する
    設計なので、パックの部分展開・AV 隔離・MAX_PATH による展開途中失敗では
    tags.db を 1 バイトも読まないうちに :class:`ImportError` が飛ぶ。これを
    ``error``（= ファイルはあるが壊れている疑い）に丸めると、UI が
    ``broken_db_banner``「ファイルが壊れている可能性があります…再スキャン」を
    出して**何をしても直らない**案内になる（項目#118）。import 失敗は provider
    不在と同じエンジンシームの劣化なので ``unavailable`` へ落とし、``error`` は
    ファイルに触れた後の失敗専用に残す（兄弟の :func:`open_vector_index` と同型）。
    """
    global _tag_index_status
    if not _available or _provider is None:
        _tag_index_status = "unavailable"
        return None
    probe = getattr(_provider, "open_tag_index_status", None)
    try:
        if probe is not None:
            index, status = probe(data_dir)
        else:
            index = _provider.open_tag_index(data_dir)
            status = "ok" if index is not None else "missing"
    except ImportError as exc:
        # engine の遅延 import が解決できない = エンジン不在。tags.db の破損では
        # ないので「壊れている」文言へは落とさない（項目#118）。
        logger.warning(
            "tags.db provider unavailable (import of {!r} failed): {}",
            getattr(exc, "name", None) or "?", exc,
        )
        _tag_index_status = "unavailable"
        return None
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("tags.db unavailable: {}", exc)
        # 例外まで来た = ファイルはあるが扱えない（壊れている疑い）扱い。
        _tag_index_status = "error"
        return None
    if status not in _TAG_INDEX_STATUSES:  # pragma: no cover (defensive)
        status = "ok" if index is not None else "missing"
    _tag_index_status = status
    return index


def open_vector_index(data_dir: Path):
    """意味検索ベクトルを開く（パック無効・numpy 不在・失敗は ``None``）。

    vector_index（プラグインの engine パッケージ）の import は provider の
    メソッド内まで遅延される — パック有効なのに numpy が見つからない
    （プラグインの vendor 欠損等）は警告ログ + ``None`` に劣化し、
    AIタグ検索（sqlite のみ）は生きる。
    """
    if not _available or _provider is None:
        return None
    try:
        return _provider.open_vector_index(data_dir)
    except ImportError as exc:
        # 遅延 import の失敗は numpy 欠損とは限らない（パックの engine/ 欠落・
        # vector_index.py の欠損も同じ枝へ落ちる）。原因モジュールを見ずに
        # 「numpy」と決め打ちすると、ログを一次情報にする不具合調査を誤誘導
        # する（項目#192）。
        name = getattr(exc, "name", None) or ""
        if name == "numpy" or name.startswith("numpy."):
            logger.warning(
                "semantic vectors unavailable (numpy import failed): {}", exc
            )
        else:
            logger.warning(
                "semantic vectors unavailable (import of {!r} failed): {}",
                name or "?", exc,
            )
        return None
    except Exception as exc:  # pragma: no cover (defensive)
        logger.warning("semantic vectors unavailable: {}", exc)
        return None


__all__ = [
    "OFFICIAL_PLUGIN_ID",
    "TAGS_DB_NAME",
    "available",
    "register",
    "unregister",
    "provider",
    "provider_owner",
    "provider_ui_sync_failed",
    "register_provider",
    "unregister_provider",
    "add_provider_callback",
    "remove_provider_callback",
    "maybe_enable_from_plugins",
    "open_tag_index",
    "open_vector_index",
    "tag_index_status",
]
