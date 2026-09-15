# Snappix Viewer プラグイン開発ガイド

Snappix Viewer は、この `plugins` フォルダに置いた Python プラグインで機能を拡張できます。
このドキュメント 1 枚で、簡単なプラグインなら本体のソースコードを読まずに作れるように書いてあります。

> **AI にプラグインを作らせる場合**: このファイルを丸ごと AI（Claude 等）に渡し、
> 「このガイドに従って○○するプラグインを作って」と依頼してください。
> 本体の内部に踏み込む高度なプラグインを作る場合は、公開リポジトリ
> （GitHub の Snappix Viewer ソースコード）も併せて参照させてください。

---

## 1. 仕組みの概要

- プラグインは **`plugins/` 直下の 1 フォルダ = 1 プラグイン**。
- フォルダには `plugin.json`（マニフェスト）と `__init__.py`（エントリーモジュール）を置く。
- ビューア起動時に検出され、**初回は必ず無効**。確認ダイアログ（または
  「ファイル ▸ プラグイン…」）でユーザーが明示的に有効化すると、次回から自動で読み込まれる。
- 有効化されたプラグインは起動時に `activate(ctx)` が呼ばれる。`ctx` が本体への窓口。
- 読み込みや `activate()` が失敗したプラグインは**自動的に無効化**され、その場で
  理由付きのモーダルが出る（全文は `data/logs/viewer.log`）。プラグインの失敗で
  ビューアは落ちない。
- `SnappixViewer.exe --no-plugins`（または環境変数 `SNAPPIX_NO_PLUGINS=1`）で
  全プラグインを読み込まない**セーフモード**起動ができる。

> **セキュリティ**: プラグインは通常の Python コードとして実行され、PC 上のあらゆる
> 操作が可能です。配布する側は利用者にその旨を伝え、利用する側は信頼できる配布元の
> プラグインだけを有効化してください。

## 2. クイックスタート（Hello World）

`plugins/hello_world/` を作り、次の 2 ファイルを置くだけです。

**`plugins/hello_world/plugin.json`**

```json
{
  "id": "hello_world",
  "name": "Hello World",
  "version": "1.0.0",
  "api": 1,
  "description": "メニューから挨拶するだけのサンプル",
  "author": "あなたの名前"
}
```

**`plugins/hello_world/__init__.py`**

```python
def activate(ctx):
    # ヘルプメニューの末尾に項目を追加
    ctx.add_menu_action(
        "help",
        "Hello World!",
        lambda: ctx.show_toast("こんにちは！", kind="success"),
    )
    ctx.log.info("hello_world activated")
```

ビューアを起動 → 確認ダイアログで有効化 → ヘルプメニューに項目が現れます。

## 3. plugin.json（マニフェスト）

| フィールド | 必須 | 型 | 説明 |
|---|---|---|---|
| `id` | ✔ | 文字列 | 一意な識別子。`^[a-z0-9][a-z0-9_-]*$`（半角小文字・数字・`-`・`_`） |
| `name` | ✔ | 文字列 | 表示名（プラグイン管理・確認ダイアログに出る） |
| `version` | ✔ | 文字列 | プラグインのバージョン。サードパーティ製プラグインは自由に付けてよい（本体は形式を問わず、他と比較もしない）。**同梱の公式プラグインは製品版数と同値**（ビルドが検査する） |
| `api` | ✔ | 整数 | 対応するプラグイン API バージョン。**現在は `1`** |
| `description` | | 文字列 | 説明（プラグイン管理に表示） |
| `author` | | 文字列 | 作者名 |
| `homepage` | | 文字列 | 配布ページ等の URL |

`api` が本体の対応バージョンと一致しないプラグインは読み込みを拒否されます
（本体の対応バージョンはプラグイン管理ダイアログのエラーメッセージに出ます）。

ファイルの文字コードは **UTF-8**（BOM の有無は問いません）。`name` などの表示用
文字列は長すぎると切り詰められて表示されます。

## 4. ライフサイクル

```python
def activate(ctx):
    """有効化時（起動時 or 管理ダイアログでの有効化直後）に 1 回呼ばれる。必須。"""

def deactivate():
    """無効化時・ビューア終了時に呼ばれる。任意。
    自分で作ったタイマー・スレッド・シグナル接続はここで止めること。
    （ctx.add_menu_action 等で追加した UI は本体が自動回収する）"""
```

- `activate()` で例外を投げると、そのプラグインは自動無効化されます（他への影響なし）。
- `deactivate()` を定義していないプラグインを無効化した場合、完全な反映は次回起動時になります。
- 同フォルダ内に複数の `.py` を置けます。`from . import mymodule` の相対 import が使えます。

## 5. ctx（PluginContext）API リファレンス — api 1

`activate(ctx)` が受け取る `ctx` のすべての安定 API です。
**この節にあるものだけが後方互換を保証されます。**

### 情報・ログ

| API | 説明 |
|---|---|
| `ctx.api_version` | 本体が実装する API バージョン（int） |
| `ctx.plugin_id` | 自分の plugin id |
| `ctx.plugin_dir` | 自分のプラグインフォルダ（`pathlib.Path`。読み取り用） |
| `ctx.data_dir` | プラグイン専用の永続データフォルダ（`data/plugins/<id>/`。初回アクセスで作成） |
| `ctx.app_version` | Snappix Viewer 本体のバージョン文字列 |
| `ctx.log` | ロガー（loguru）。`ctx.log.info("...")` 等。`data/logs/viewer.log` に出る |

> **ポータビリティ規約（重要）**: 設定・キャッシュ等の保存は必ず `ctx.data_dir` 配下へ。
> ユーザーホーム・`%APPDATA%`・レジストリへの書き込みは本体の配布方針違反です。

### メニュー

```python
action = ctx.add_menu_action(menu_id, "表示テキスト", callback, shortcut="Ctrl+Shift+H")
menu = ctx.get_menu(menu_id)      # 既存トップメニューの QMenu（無ければ None）
menu = ctx.add_top_menu("拡張")    # 独自トップメニューを追加（ヘルプの左に挿入）
```

`menu_id` は次のいずれか: `"file"` `"edit"` `"search"` `"bookmarks"` `"view"` `"diagnostics"` `"help"`

ショートカットは既存の割り当て（ヘルプ ▸ キーボードショートカット一覧）と
衝突しないものを選んでください。

### 右クリックメニュー（グリッド／情報パネルのタイル）

```python
def my_entry(menu, path, is_dir):
    # menu: PySide6.QtWidgets.QMenu / path: pathlib.Path / is_dir: bool
    if not is_dir and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        act = menu.addAction("グレースケールで保存")
        act.triggered.connect(lambda: convert(path))

ctx.add_context_menu_entry(my_entry)
```

- メニュー構築のたびに GUI スレッドで呼ばれます。**この関数内でファイルアクセスをしない**こと
  （NAS 上でメニューが固まるため。判定は拡張子など文字列だけで行い、実処理は
  `triggered` ハンドラの中へ）。
- 例外は隔離され、あなたの項目が出ないだけで本体メニューは壊れません。

### イベント

```python
ctx.events.root_changed.connect(on_root)        # グリッドのルート変更（Path）
ctx.events.selection_changed.connect(on_select) # タイル選択（Path。ファイル/フォルダ両方）
```

どちらも Qt シグナルで、GUI スレッドで発火します。ハンドラ内で重い処理・
ネットワーク I/O をしないこと（→ §7 スレッドの節）。

`ctx.events` は**あなたのプラグイン専用の中継**で、無効化時（`activate()` が
例外を投げて自動無効化された場合を含む）に本体が購読ごと切り離します。つまり
ここへの `connect` は「本体が自動回収する寄稿」の側で、`deactivate()` で自分で
`disconnect` する必要はありません（してももちろん構いません）。本体のほかの
シグナル（`ctx.window` 経由で掴んだ内部オブジェクト等）への接続は従来どおり
自分で止めてください。

### 通知

```python
ctx.show_toast("完了しました", kind="success")   # kind: info/success/warning/error
ctx.show_toast(msg, kind="warning", duration_ms=0)  # クリックまで残す常駐トースト
ctx.show_status("処理中…", timeout_ms=5000)      # ステータスバーの一時メッセージ
```

本体の通知規約: **成功・情報 = トースト（非モーダル）／失敗 = モーダル
（`QMessageBox.warning` 等）／破壊的操作 = 実行前に確認モーダル**。

「失敗 = モーダル」の対象は**操作そのものが通らなかった**ケースです。
長時間処理が完走したうえで一部だけ落ちた**部分失敗**（例: スキャン完了・
失敗 N 件）はモーダルにする対象ではなく、`duration_ms=0` の常駐トースト
（`kind="warning"`）にしてください — 既定の 3 秒では、数十分走った処理の
失敗件数が読まれる前に消えます（本体も `user_meta` / キャッシュ構築の部分
失敗に同じ様式を使っています）。

### ステータスバー常駐ウィジェット

```python
ctx.add_status_widget(widget)   # QWidget をステータスバー右側に常駐追加
```

進捗バーなどの常駐 UI 向け（一時テキストは `ctx.show_status`）。本体の
キャッシュ進捗と同じ permanent 領域に並ぶので、普段は
`widget.setVisible(False)` にしておき動作中だけ見せるのが作法です。
無効化・終了時は本体が自動回収します。（api 1 への追加 API —
古い本体にも対応したい場合は `hasattr(ctx, "add_status_widget")` で分岐）

### ライブラリ変更通知

```python
ctx.notify_library_changed([folder1, folder2])   # 書き込んだサブツリーのルート群
```

プラグインがライブラリ内のファイルを**書き込んだ後**に呼びます（ファイル生成・
変換・メタデータ書き換え等）。本体はフォルダプレビューキャッシュの該当
サブツリーを無効化し、表示中のペインが影響範囲なら選択を保ったまま再スキャン
します。in-place 上書き（post.md や画像バイトの差し替え）は親フォルダの
mtime を変えず、この通知なしでは本体キャッシュが古い表示を返し続けます。

ジョブ完了などのまとまった単位で呼ぶこと（ファイル 1 件ごとに呼ばない）。
GUI スレッドから呼ぶこと。（api 1 への追加 API — 古い本体にも対応したい
場合は `hasattr(ctx, "notify_library_changed")` で分岐）

### 翻訳カタログ（i18n）

本体の全 UI 文言はキー方式のカタログ（`snappix.common.i18n`）で管理されて
います。プラグインも同じ仕組みに相乗りできます:

```python
from snappix.common.i18n import register_catalog, t

register_catalog("ja", {"myplugin.menu.run": "実行"})   # import 時に一度
...
action.setText(t("myplugin.menu.run"))
```

キーは必ず `<plugin_id>.` で名前空間化してください（本体・他プラグインの
キーと衝突させない）。`register_catalog` はマージ動作なので既存カタログを
壊しません。ロケール切替は本体起動時に確定済みで、`t()` はそのロケール →
`ja` → キー文字列の順にフォールバックします。

### 脱出ハッチ

```python
win = ctx.window   # ViewerWindow（QMainWindow）の生参照
```

安定 API に無いことは、ここから本体の内部に直接触れて実現できます
（Qt 的にはウィジェット構造の変更を含め何でも可能です）。**ただし内部構造は
本体のバージョンアップで予告なく変わります。** 使う場合は:

- 公開リポジトリ（GitHub）の該当バージョンのソースコードを読むこと
- `getattr(win, "_post_grid", None)` のような防御的な書き方にすること
- 壊れたときはあなたのプラグインが自動無効化されるだけで済むよう、
  `activate()` 内の危険な処理は早めに実行して失敗させること

## 6. 追加ライブラリ（vendor/）

プラグインが import できるのは次の 3 系統です。

1. **Python 標準ライブラリ**（本体は Python 3.12）
2. **本体に同梱済みのライブラリ**: PySide6 (Qt6) / Pillow /
   markdown-it-py / linkify-it-py / pydantic / loguru
   （※ numpy は同梱されていません。必要なら下記の `vendor/` で同梱してください。
   numpy を `vendor/` に同梱した別のプラグインが**先に有効化されている**環境では、
   その vendor の numpy が import できます — `vendor/` が `sys.path` に載るのは
   そのプラグインの `activate()` 直前なので、有効化されていないプラグインの
   `vendor/` は誰からも見えません）
3. **プラグインフォルダの `vendor/` に同梱したパッケージ** —
   `vendor/` フォルダがあると自動で `sys.path` に追加されます（末尾追加なので
   本体のモジュールを上書きすることはできません）

`vendor/` の作り方（例: `requests` を同梱する場合）:

```
pip install --target plugins/my_plugin/vendor requests
```

純 Python パッケージはそのまま動きます。C 拡張（`.pyd`）を含むパッケージは
**Python 3.12 / Windows 64bit（cp312-win_amd64）用の wheel** である必要があります。

> ⚠️ **`vendor/` は全プラグインで共有される `sys.path` 上の先着勝ちです。**
> 各プラグインの `vendor/` は同一プロセスの `sys.path` 末尾に順次追加され、
> **無効化しても除去されません**。したがって 2 つのプラグインが同名パッケージの
> **別バージョン**を vendor すると、先に有効化された側（＝フォルダ名の辞書順で
> 先）のバージョンが**両方に**供給されます。`httpx` / `lxml` / `beautifulsoup4`
> のような一般的なライブラリを vendor するときは、他プラグインとの版衝突が
> 起こりうることを前提にしてください（バージョン下限を緩めに保つ・破壊的 API に
> 依存しない等）。将来的にはプラグインごとの import 隔離を検討しています。

## 7. お作法（本体の設計規約に合わせる）

### スレッド

- Qt のウィジェット操作・`QPixmap` 生成は **GUI スレッド限定**。ワーカースレッドで
  画像を扱うときは `QImage` か Pillow を使う。
- 重い処理（大量ファイルの読み書き・ネットワーク・画像変換）は GUI スレッドでやらない。
  `QThreadPool.globalInstance().start(...)` + シグナルで結果を返すのが本体の流儀。
- NAS（ネットワークフォルダ）上のパスは `os.stat` 1 回でも数秒ブロックし得る前提で書く。

### 見た目

- 色・フォントサイズをハードコードしない。ウィジェットは本体のテーマ
  （パレット + アプリ全体 QSS）を自動で継承するので、**何も指定しないのが正解**。
  独自描画が必要なら `self.palette()` のロール色を使う。
- アイコン付きボタンは `QToolButton`（フラット）、テキストボタンは `QPushButton`（箱型）。

### データ

- 他のプラグインや本体が作成した `data/` 直下の既存ファイルを**書き換えない**こと
  （読み取りは自由）。自分のデータは `ctx.data_dir` へ。
- 表示中のライブラリ（ユーザーの画像フォルダ）内のファイルを勝手に変更・削除しない。
  変更が機能の本質である場合は、実行前に確認ダイアログを出す。

## 8. 実戦サンプル: 選択画像をグレースケール保存

`plugins/grayscale_export/plugin.json`:

```json
{
  "id": "grayscale_export",
  "name": "グレースケール保存",
  "version": "1.0.0",
  "api": 1,
  "description": "右クリックした画像をグレースケール化して隣に保存する",
  "author": "sample"
}
```

`plugins/grayscale_export/__init__.py`:

```python
from pathlib import Path

from PySide6.QtCore import QRunnable, QThreadPool, QObject, Signal
from PySide6.QtWidgets import QMessageBox

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_ctx = None


class _Signals(QObject):
    done = Signal(object)   # 保存先 Path
    failed = Signal(str)


class _ConvertTask(QRunnable):
    """変換はワーカースレッドで（GUI を固めない）。Pillow は同梱済み。"""

    def __init__(self, src: Path, signals: _Signals) -> None:
        super().__init__()
        self._src = src
        self._signals = signals

    def run(self) -> None:
        try:
            from PIL import Image

            dst = self._src.with_stem(self._src.stem + "_gray")
            with Image.open(self._src) as im:
                im.convert("L").save(dst)
            self._signals.done.emit(dst)
        except Exception as exc:
            self._signals.failed.emit(str(exc))


def _on_done(dst: Path) -> None:
    _ctx.show_toast(f"保存しました: {dst.name}", kind="success")


def _on_failed(message: str) -> None:
    # 失敗はモーダル（本体の通知規約）
    QMessageBox.warning(_ctx.window, "グレースケール保存", message)


_signals = None


def _convert(path: Path) -> None:
    QThreadPool.globalInstance().start(_ConvertTask(path, _signals))


def _menu_entry(menu, path: Path, is_dir: bool) -> None:
    # ここは毎回呼ばれる: 判定は拡張子のみ（ファイルアクセス禁止）
    if is_dir or path.suffix.lower() not in _IMAGE_EXTS:
        return
    act = menu.addAction("グレースケールで保存")
    act.triggered.connect(lambda _=False, p=path: _convert(p))


def activate(ctx) -> None:
    global _ctx, _signals
    _ctx = ctx
    _signals = _Signals()
    _signals.done.connect(_on_done)
    _signals.failed.connect(_on_failed)
    ctx.add_context_menu_entry(_menu_entry)


def deactivate() -> None:
    pass  # 右クリック項目は本体が自動回収。タイマー等があればここで止める
```

## 9. 配布とインストール

- **配布**: プラグインフォルダを丸ごと zip にする（`my_plugin.zip` の直下が
  `my_plugin/plugin.json` になる構成を推奨）。
- **インストール**: 利用者は zip を展開して、フォルダごと `plugins/` に置くだけ。
  次回起動時に確認ダイアログが出ます。
- **分割された zip**: 配布サイズの上限で `<名前>.zip.001` / `.002` … に分かれている
  ことがあります。全部を同じフォルダへ置き、7-Zip で `.001` を開く（`.002` 以降は
  自動で読まれます）か、`copy /b a.zip.001 + a.zip.002 a.zip` で 1 本に戻してから
  展開してください。
- **アンインストール**: フォルダを削除するだけ（無効化だけなら「ファイル ▸ プラグイン…」）。
- プラグインの設定は `data/plugins/<id>/` に残るので、フォルダを消しても設定は保持されます。

## 10. トラブルシューティング

| 症状 | 確認すること |
|---|---|
| プラグインが一覧に出ない | フォルダ直下に `plugin.json` と `__init__.py` があるか。zip の二重フォルダ（`plugins/my_plugin/my_plugin/…`）になっていないか |
| 「読み込めないプラグイン」に出る | プラグイン管理ダイアログのエラー列に理由が出ます（JSON 構文・必須フィールド・id 書式）。`plugin.json` は UTF-8 で保存してください（Shift-JIS は読めません） |
| 有効化したのに次の起動で「無効」に戻る | 読み込みか `activate()` が失敗しています。起動時に理由のモーダルが出ており、完全なトレースバックは `data/logs/viewer.log` にあります |
| ビューアが起動しなくなった | `SnappixViewer.exe --no-plugins` でセーフモード起動し、プラグイン管理から無効化 |
| `import` が失敗する | そのライブラリは同梱されていません（§6）。`vendor/` に同梱するか、標準ライブラリで書き直す |

## 11. API バージョンポリシー

- `plugin.json` の `api` はプラグインが対応する API メジャーバージョンです。
- 本体は安定 API（§5）に後方互換を壊す変更を入れるとき `api` をバンプします。
  一致しないプラグインは読み込まれず、エラーとして表示されます。
- 安定 API への**追加**（新メソッド・新イベント）はバンプしません。
  `hasattr(ctx, "new_method")` で新旧両対応にできます。

## 12. AI 検索 provider のスキャナ契約（上級）

AI 検索エンジンを供給するプラグイン（公式 AI プラグイン相当）は、`activate(ctx)` で
`snappix.viewer.ai_pack.register_provider(provider, owner=ctx.plugin_id)` を呼びます。
provider スロットは後勝ちの単一枠なので、**登録も解除も `owner=` で名乗ってください** —
`deactivate(ctx)` 側は `snappix.viewer.ai_pack.unregister_provider(owner=ctx.plugin_id)` を呼び、
自分が載せた実体がまだ載っているときだけ引き揚げます（名乗らない解除は、後から別の
プラグインが登録した provider を巻き添えで落とします）。provider は duck-typed で、
`open_tag_index(data_dir)` / `open_vector_index(data_dir)` /
`create_tag_scanner(tag_index=, folder_cache=, parent=)` /
`create_vector_scanner(vector_index=, folder_cache=, parent=)` を持つこと
（任意メソッド `open_tag_index_status(data_dir) -> (index, status)` を持てば
「無い/壊れている」の判定もプラグイン側で下せます）。

`create_*_scanner` が返すスキャナ（`QObject`）に本体が期待する契約は次のとおりです。
**本体はこの契約だけに依存し、スキャナの内部属性には触れません。**

| メンバー | 契約 |
|---|---|
| `request(root, *, mode=, ...) -> int` | 1 クエリを非 GUI スレッドで開始し、その世代番号（単調増加 int）を返す。直前のクエリは暗黙にキャンセルされる |
| `cancel()` | 進行中のクエリを協調キャンセルし、**世代番号を必ず bump する**（キャンセル済みタスクが emit 済みの結果を、消費側の `generation == latest_generation()` ガードで確実に落とすため。root 変更時の stale 結果対策） |
| `latest_generation() -> int` | 最新の世代番号 |
| `set_index(index)` | 以後の `request` が使う索引リーダー（TagIndex / VectorIndex、`None` 可）を差し替える。tags.db 再読込（K01）時に本体が呼ぶ。進行中タスクは旧リーダーのままでよい（本体が先に `cancel()` する） |
| シグナル `results_ready(int, list)` | `(generation, list[(FolderEntry, rel)])`。同一世代で複数回 emit してよい（即時セット → 存在チェック後の確定セット） |
| シグナル `failed(int)` | `(generation,)`。索引が使えない・クエリ失敗 |

`set_index` を持たないスキャナ（旧契約）でも動作します — 本体は再読込のたびに
スキャナを破棄して `create_*_scanner` で作り直します（性能は落ちますが正しさは同じ）。
