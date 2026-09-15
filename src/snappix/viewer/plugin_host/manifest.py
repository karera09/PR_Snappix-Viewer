"""``plugin.json`` マニフェストのスキーマと検出（Qt 非依存・純ロジック）.

プラグインは ``plugins/`` 直下の **1 フォルダ = 1 プラグイン**。フォルダには
最低限次の 2 ファイルが要る:

* ``plugin.json`` — このモジュールが検証するマニフェスト
* ``__init__.py`` — ``activate(ctx)`` を定義するエントリーモジュール

``plugin.json`` の書式（詳細は docs/PLUGIN_DEVELOPMENT.md）::

    {
      "id": "my_plugin",          # 必須: ^[a-z0-9][a-z0-9_-]*$ / 全体で一意
      "name": "My Plugin",        # 必須: 表示名
      "version": "1.0.0",         # 必須: プラグイン自身のバージョン文字列
      "api": 1,                   # 必須: 対応するプラグイン API バージョン
      "description": "...",       # 任意
      "author": "...",            # 任意
      "homepage": "https://..."   # 任意
    }

検出（:func:`discover_plugins`）はフォルダ列挙と JSON 読みだけを行い、
プラグインコードは一切 import しない — 「検出されたが無効」のプラグインが
コードを走らせないことはこの分離が保証する。壊れたマニフェストは例外に
せず :class:`BrokenPlugin` として返し、管理ダイアログが理由を表示する。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: 安定 API のバージョン。互換を壊す変更（メソッド削除・意味変更）をしたら
#: バンプする。マニフェストの ``api`` がこれと一致しないプラグインはロードを
#: 拒否される（前方互換の保証はしない — 小さく確実に）。
PLUGIN_API_VERSION = 1

#: マニフェストのファイル名（プラグインフォルダ直下）。
MANIFEST_NAME = "plugin.json"

#: エントリーモジュール（プラグインフォルダ直下）。
ENTRY_NAME = "__init__.py"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: 表示用の文字数上限。``id`` 以外のマニフェスト文字列は**未検証の外部入力**
#: （まだ信頼していないプラグインのフォルダから読む）なので、そのまま
#: 信頼モーダル・管理ダイアログの詳細ラベルへ流すと、数千字の ``description``
#: が本文の危険説明と質問文を画面外へ押し出せる（レビュー 2026-09-11 N-28）。
#: 切り詰めるのは**表示用の値だけ**で、``id`` の検証・``store`` の記録キー・
#: ``api`` の突合には一切影響しない。
_MAX_SHORT_CHARS = 80
_MAX_HOMEPAGE_CHARS = 200
_MAX_DESCRIPTION_CHARS = 300

#: 例外メッセージへ埋める未検証値の上限。``ManifestError`` の本文は
#: :class:`BrokenPlugin` の ``error`` として起動時の「読み込めませんでした」
#: モーダルと管理ダイアログの状態列へそのまま流れるので、表示用フィールドと
#: 同じ理由で上限が要る（5,000 字の不正 ``id`` を書いた ``plugin.json`` は
#: PlainText の QMessageBox を画面幅いっぱいに広げられる）。属性の上限
#: （:data:`_MAX_SHORT_CHARS` 等）とは別に、文面の断片としてより短く切る。
_MAX_ERROR_VALUE_CHARS = 120


_CONTROL_RUN_RE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _clip(value: str, limit: int) -> str:
    """*value* を 1 行に畳んでから *limit* 文字で切り、切ったときだけ 「…」 を付ける。

    改行・制御文字は 1 個の空白へ畳む — 文字数だけを縛ると、改行を詰めた
    300 文字の description が 300 行として描かれ、モーダルが画面の高さを
    超えて危険説明とボタンを押し出せる（文字数上限が狙った N-28 の穴）。
    """
    value = _CONTROL_RUN_RE.sub(" ", value).strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def is_valid_plugin_id(text: str) -> bool:
    """*text* がプラグイン id の書式（:data:`_ID_RE`）を満たすか。

    マニフェスト以外の経路（クラッシュセンチネルのファイル内容など）から来た
    未検証の文字列を、id として扱う前に弾くための公開口。検証規則の複製を
    作らないよう、判定はこの 1 箇所だけが持つ。
    """
    return bool(_ID_RE.match(text))


def clip_untrusted(text: str) -> str:
    """未検証の文字列を「モーダル本文の断片」の上限へ畳む公開口。

    マニフェスト以外の経路（クラッシュセンチネル・``plugins.json`` の記録）
    から来た文字列を画面に出す側が使う。上限も畳み方も :func:`_clip` の
    1 実装で、席ごとに別の上限を手書きしない。
    """
    return _clip(text, _MAX_ERROR_VALUE_CHARS)


class ManifestError(ValueError):
    """``plugin.json`` が欠落・不正なときの検証エラー。"""


@dataclass(frozen=True)
class PluginManifest:
    """検証済みマニフェスト 1 件。``dir`` はプラグインフォルダの絶対パス。"""

    id: str
    name: str
    version: str
    api: int
    dir: Path
    description: str = ""
    author: str = ""
    homepage: str = ""

    @property
    def entry_path(self) -> Path:
        return self.dir / ENTRY_NAME


@dataclass(frozen=True)
class BrokenPlugin:
    """検出はされたがロード対象にできないフォルダ（理由付き）。

    ``dup_id`` は id 重複で敗れたフォルダのときにその id を持つ（それ以外の
    破損理由では ``None``）。呼び出し側が「同一 id が複数フォルダに存在するか」
    を後方互換判定に使えるようにするための識別子。
    """

    dir: Path
    error: str
    dup_id: str | None = None


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """*plugin_dir* の ``plugin.json`` を読んで検証する。

    :raises ManifestError: ファイル欠落・UTF-8 で読めない・JSON 破損・
        必須フィールド不足・id 書式違反・``__init__.py`` 欠落。
    """
    manifest_path = plugin_dir / MANIFEST_NAME
    try:
        # ``utf-8-sig`` は BOM があれば剥ぎ、無ければ素の UTF-8 として読む。
        # BOM 付き UTF-8 は Windows PowerShell 5.1 の ``Set-Content -Encoding
        # utf8`` やメモ帳の「UTF-8 (BOM 付き)」が普通に作る形で、素の ``utf-8``
        # で読むと json.loads が「Unexpected UTF-8 BOM」を投げ、プラグイン作者
        # には「不正な JSON です」としか見えない。
        raw = manifest_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ManifestError(f"{MANIFEST_NAME} がありません") from None
    except UnicodeDecodeError as exc:
        # 非 UTF-8（例: 日本語 Windows のメモ帳が ANSI=Shift-JIS で保存）。
        # UnicodeDecodeError は ValueError であって OSError ではないため、
        # ここで捕まえないと discover_plugins の ManifestError ハンドラを
        # すり抜けて起動全体のプラグイン配線を落とす（レビュー #29）。
        raise ManifestError(
            f"{MANIFEST_NAME} が UTF-8 で読めません: {exc}"
        ) from exc
    except OSError as exc:
        raise ManifestError(f"{MANIFEST_NAME} を読めません: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ManifestError(f"{MANIFEST_NAME} が不正な JSON です: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{MANIFEST_NAME} のトップレベルはオブジェクトが必要です")

    def _req_str(key: str, limit: int | None = None) -> str:
        value = data.get(key)
        if not isinstance(value, str):
            raise ManifestError(f"必須フィールド {key!r} がありません")
        # 空チェックは**切り詰めた後**に行う。``str.strip`` が落とすのは空白
        # だけなので、制御文字だけの ``name`` は strip を素通りし、``_clip``
        # が制御文字を畳んだ結果として "" になっていた（確認モーダルに無名の
        # プラグインが出る）。
        text = value.strip() if limit is None else _clip(value, limit)
        if not text:
            raise ManifestError(f"必須フィールド {key!r} がありません")
        return text

    # ``id`` だけは切り詰めない — 表示ではなく同一性（``_ID_RE`` の検証と
    # ``store`` の記録キー）を担うので、途中で切ると別物になる。例外本文へ
    # 埋めるときだけ表示用に切る（本文は未検証値を含むモーダルへ流れる）。
    pid = _req_str("id")
    if not _ID_RE.match(pid):
        raise ManifestError(
            f"id {_clip(pid, _MAX_ERROR_VALUE_CHARS)!r} が不正です"
            "（半角小文字・数字・'-'・'_' のみ）"
        )
    name = _req_str("name", _MAX_SHORT_CHARS)
    version = _req_str("version", _MAX_SHORT_CHARS)
    api = data.get("api")
    if not isinstance(api, int) or isinstance(api, bool):
        raise ManifestError("必須フィールド 'api' (整数) がありません")

    def _opt_str(key: str, limit: int) -> str:
        value = data.get(key, "")
        return _clip(value.strip(), limit) if isinstance(value, str) else ""

    if not (plugin_dir / ENTRY_NAME).is_file():
        raise ManifestError(f"エントリーモジュール {ENTRY_NAME} がありません")

    return PluginManifest(
        id=pid,
        name=name,
        version=version,
        api=api,
        dir=plugin_dir.resolve(),
        description=_opt_str("description", _MAX_DESCRIPTION_CHARS),
        author=_opt_str("author", _MAX_SHORT_CHARS),
        homepage=_opt_str("homepage", _MAX_HOMEPAGE_CHARS),
    )


def discover_plugins(
    plugins_dir: Path,
    preferred_folders: dict[str, str] | None = None,
) -> tuple[list[PluginManifest], list[BrokenPlugin]]:
    """``plugins/`` を走査してマニフェストを収集する（コードは import しない）.

    * 直下の**フォルダのみ**対象（PLUGIN_DEVELOPMENT.md などのファイルは無視）。
    * ``plugin.json`` を持たないフォルダは黙ってスキップ（ユーザーの作業用
      フォルダ・解凍途中などを壊れ扱いしない）。
    * ``plugin.json`` があるのに検証に失敗したフォルダは :class:`BrokenPlugin`。
    * id 重複は 1 フォルダだけを勝者にし、残りは ``dup_id`` 付きの
      :class:`BrokenPlugin`。勝者の選び方:

      - *preferred_folders* にその id の basename が指定され、候補にそのフォルダが
        あれば**それが勝者**（記録済みフォルダ優先 — なりすまし対策 issue #37）。
      - 指定が無い / 一致しなければ従来どおり**フォルダ名の辞書順で先勝ち**。

    *preferred_folders* は ``{id: フォルダ basename}``。store が記録した「有効化を
    確定したフォルダ」を橋渡しするヒントで、manifest 層は store を import しない
    （Qt 非依存・依存方向の維持）。

    戻り値はどちらもフォルダ名の辞書順で安定。*plugins_dir* が存在しなければ
    空を返す（作成は呼び出し側の責務 — 検出は read-only に保つ）。
    """
    preferred = preferred_folders or {}
    broken: list[BrokenPlugin] = []
    if not plugins_dir.is_dir():
        return [], broken
    try:
        children = sorted(
            (c for c in plugins_dir.iterdir() if c.is_dir()),
            key=lambda p: p.name.casefold(),
        )
    except OSError:
        return [], broken
    # id ごとに候補フォルダをまとめてから勝者を決める（辞書順先勝ちだと記録済み
    # フォルダより先に並ぶ詐称フォルダが勝ってしまうため、二段構えにする）。
    groups: dict[str, list[PluginManifest]] = {}
    for child in children:
        if not (child / MANIFEST_NAME).is_file():
            continue
        try:
            manifest = load_manifest(child)
        except ManifestError as exc:
            broken.append(BrokenPlugin(dir=child, error=str(exc)))
            continue
        groups.setdefault(manifest.id, []).append(manifest)

    manifests: list[PluginManifest] = []
    for pid, candidates in groups.items():
        # candidates は children の辞書順を保持している。
        winner = candidates[0]
        want = preferred.get(pid)
        if want is not None:
            for c in candidates:
                if c.dir.name == want:
                    winner = c
                    break
        manifests.append(winner)
        for c in candidates:
            if c is winner:
                continue
            broken.append(
                BrokenPlugin(
                    dir=c.dir,
                    error=(
                        f"id {_clip(pid, _MAX_ERROR_VALUE_CHARS)!r} が "
                        f"{winner.dir.name}/ と重複しています"
                    ),
                    dup_id=pid,
                )
            )
    manifests.sort(key=lambda m: m.dir.name.casefold())
    broken.sort(key=lambda b: b.dir.name.casefold())
    return manifests, broken


__all__ = [
    "PLUGIN_API_VERSION",
    "MANIFEST_NAME",
    "ENTRY_NAME",
    "ManifestError",
    "clip_untrusted",
    "is_valid_plugin_id",
    "PluginManifest",
    "BrokenPlugin",
    "load_manifest",
    "discover_plugins",
]
