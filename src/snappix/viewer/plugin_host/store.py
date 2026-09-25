"""プラグインの有効/無効状態の永続化（Qt 非依存）.

``data/plugins.json`` に「ユーザーがどのプラグインをどう扱うと決めたか」を
記録する。**記録が無い = 未知のプラグイン**であり、未知は常に無効 —
新しく置かれたプラグインは初回確認（bootstrap）を経るまで実行されない。

viewer_state.json とは意図的に分離してある: プラグインの有効状態は
「viewer の表示設定」ではなく実行許可の記録なので、設定リセットや
state スキーマ変更に巻き込まれてはならない。書式::

    {
      "version": 1,
      "plugins": {
        "<id>": {
          "enabled": true,
          "folder": "snappix_ai",   # ユーザーが確認を済ませた manifest フォルダ
          "declined": false         # その確認で「有効化しない」と答えたか
        }
      }
    }

記録するのは**決定**だけで、経過（直近の失敗理由など）は持たない: 失敗は
その場でモーダルに出し、詳細は ``data/logs/viewer.log`` に残る。永続化した
理由文字列は表示用の写しにしかならず、「記録された理由」と「今の実体」が
食い違う面（フォルダを直した後に残る古い理由）を増やす。

なりすまし対策（id + フォルダの束縛）
------------------------------------
記録キーは plugin id だが、``id`` は ``plugin.json`` にユーザーが（あるいは
攻撃者が）書ける自己申告値なので、id だけを信頼境界にすると「有効化済みの
id を騙るフォルダを ``plugins/`` に置く」だけで無確認 activate されうる。
そこで各レコードに**ユーザーが確認を済ませたフォルダ実体**
（``plugins/`` 直下のフォルダ名 = basename）を束ねる。bootstrap は「記録した
フォルダ」と「今回解決されたフォルダ」を照合し、不一致なら known 扱いに
せず初回確認フローへ落とす。``folder`` 欠落（本フィールド導入前の旧記録）は
後方互換として許容する — スキーマ version は据え置き。

記録されるのは**有効化したフォルダとは限らない**: 確認モーダルで「有効化
しない」と答えた実体も ``enabled=False`` と一緒にここへ束ねる（bootstrap
2 節）。「同じフォルダなら次回以降は無言・別フォルダなら再び確認」という
確認済みの単位がフォルダ実体だからで、拒否した実体が優先フォルダになっても
``is_enabled`` が偽である限りそのコードは走らない（``activate_enabled`` は
信頼判定より前に有効化記録で弾く）。拒否した実体を優先から外したいときは
そのフォルダを削除する（正規フォルダとの ``folder_mismatch`` で再確認に戻る）。

ただし「断った」ことは ``declined`` に残す: ``folder`` だけを見る信頼判定は
記録一致＝``trusted`` を返すので、なりすまし警告を出したその実体を、管理
ダイアログのチェック 1 つで無警告のまま有効化できてしまう。有効化の記録
（``set_enabled(enabled=True)``）が立てば消える 1 度きりの印。

クラッシュセンチネル
--------------------
activate 中のハードクラッシュ（access violation 等、except で捕まらない死）
に備え、activate 直前にプラグイン id を ``data/plugin_loading.flag`` へ書き、
成功後に消す。次回起動時にファイルが残っていれば「そのプラグインを
読み込み中に落ちた」と判断して自動無効化する（Firefox のセーフモード検出と
同じ仕組み）。

内容は「plugin id + 書き込んだプロセスの PID」の 2 行。ポータブル配布は
多重起動され得るため、``clear_sentinel(data_dir, pid)`` は**自分が書いた
内容と一致するときだけ**削除する — インスタンス A の activate 完了が
インスタンス B の書いたセンチネルを消して B のクラッシュ検出を潰さない
ための compare-and-delete。同じ理由で ``read_and_clear_sentinel`` は
2 行目の PID を検査し、**自プロセス以外で生存中**ならクラッシュ扱いせず
消さない（並走インスタンスの activate 実行中の生きたセンチネル）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger

_STORE_VERSION = 1

#: クラッシュセンチネルのファイル名（``data/`` 直下）。
SENTINEL_NAME = "plugin_loading.flag"


class PluginStore:
    """``data/plugins.json`` の読み書き。全操作は即時保存（小さいファイル）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        try:
            # ``utf-8-sig``: BOM 付きで書かれた plugins.json（手編集・
            # PowerShell 5.1 の ``Set-Content -Encoding utf8``）を「壊れた記録」
            # として全プラグイン未知に倒さない。BOM 無しはそのまま読める。
            data = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # 壊れた記録ファイルは「全プラグイン未知」に安全側で倒す
            # （未知 = 無効なので、破損で勝手に有効化されることはない）。
            logger.warning("plugins.json load failed ({}); treating as empty", exc)
            return
        records = data.get("plugins") if isinstance(data, dict) else None
        if isinstance(records, dict):
            for pid, rec in records.items():
                if isinstance(pid, str) and isinstance(rec, dict):
                    folder = rec.get("folder")
                    self._records[pid] = {
                        "enabled": bool(rec.get("enabled", False)),
                        # folder 欠落（旧記録）は None として許容する後方互換読み。
                        "folder": folder
                        if isinstance(folder, str) and folder
                        else None,
                        # declined 欠落（旧記録）は「断っていない」で読む。
                        "declined": bool(rec.get("declined", False)),
                    }

    def _save(self) -> None:
        # tmp + os.replace のアトミック書き込み（viewer_state.json /
        # shared_prefs.json と同じリポジトリ規約）。書き込み途中のクラッシュで
        # 「実行許可の記録」が破損 → 全プラグイン未知化 → 同意モーダル再表示、
        # という事故を防ぐ。ヘルパーは state.py の単一情報源を共有する。
        from ..state import _write_json_atomic

        payload = {"version": _STORE_VERSION, "plugins": self._records}
        try:
            _write_json_atomic(self._path, payload)
        except OSError as exc:  # read-only volume 等は警告のみ（UI を壊さない）
            logger.warning("plugins.json save failed: {}", exc)

    # ------------------------------------------------------------- queries

    def known(self, pid: str) -> bool:
        """このプラグインについてユーザーの決定が記録済みか（初回確認済みか）。"""
        return pid in self._records

    def is_enabled(self, pid: str) -> bool:
        rec = self._records.get(pid)
        return bool(rec and rec.get("enabled"))

    def folder(self, pid: str) -> str | None:
        """ユーザーが確認を済ませた manifest フォルダ basename（旧記録は None）。"""
        rec = self._records.get(pid)
        return rec.get("folder") if rec else None

    def declined(self, pid: str) -> bool:
        """記録された ``folder`` が「確認モーダルで断られた実体」か。

        ``folder`` は承認・拒否のどちらでも書かれる（モジュール docstring の
        「なりすまし対策」節）ので、信頼判定はこの記録を ``trusted`` と読む。
        断ったのか許したのかはこのフラグにしか残らない — 断った実体を後から
        有効化しようとする面（管理ダイアログ）はここを見て、そのとき出した
        警告をもう一度出す。
        """
        rec = self._records.get(pid)
        return bool(rec and rec.get("declined"))

    def enabled_ids(self) -> list[str]:
        """有効と記録された id（辞書順）。

        :meth:`preferred_folders` と違い **folder 記録の無い旧レコードも含む** —
        「有効なのに検出結果のどこにも居ない」プラグインを起動時に見つける
        ための走査口（folder で突き合わせられない旧レコードの受け皿）。
        """
        return sorted(pid for pid, rec in self._records.items() if rec.get("enabled"))

    def preferred_folders(self) -> dict[str, str]:
        """``{id: folder}`` — フォルダ実体が記録済みのレコードだけ。

        id 重複の解決で「記録済みフォルダ優先」を実現するため、
        :func:`~snappix.viewer.plugin_host.manifest.discover_plugins` へ渡す
        ヒント。Qt 非依存の manifest 層に store を import させないための橋渡し。
        """
        return {
            pid: rec["folder"]
            for pid, rec in self._records.items()
            if rec.get("folder")
        }

    # ------------------------------------------------------------ mutation

    def _new_record(self) -> dict:
        return {"enabled": False, "folder": None, "declined": False}

    def set_enabled(self, pid: str, enabled: bool, folder: str | None = None) -> None:
        """有効/無効を記録する。*folder* 指定時はフォルダ実体も束ねる。

        初回確認・再確認・ダイアログでの有効化など「ユーザーがそのフォルダに
        ついて決めた」瞬間に *folder*（``plugins/`` 直下の basename）を渡す
        ことで、以後の id 重複解決となりすまし検出の基準になる。**有効化を
        断った決定でも渡す** — 確認済みの単位はフォルダ実体で、次回起動から
        無言にするために記録が要る（モジュール docstring の「なりすまし対策」
        節）。*folder* を省略すると既存の記録を保つ。
        """
        rec = self._records.setdefault(pid, self._new_record())
        rec["enabled"] = bool(enabled)
        if folder is not None:
            rec["folder"] = folder
        if enabled:
            # 有効化した = もう「断った実体」ではない（印は 1 度きり）。
            rec["declined"] = False
        self._save()

    def record_declined(self, pid: str, folder: str) -> None:
        """確認モーダルで「有効化しない」と答えた決定を記録する。

        :meth:`set_enabled` の ``enabled=False`` と同じ「確認済み・無効」に
        加えて、**その実体を断った**ことを残す（:meth:`declined`）。ダイアログ
        でチェックを外す通常の無効化とは別の口にしてある — そちらは断った
        わけではないので、次に有効化するとき警告を出す理由が無い。
        """
        rec = self._records.setdefault(pid, self._new_record())
        rec["enabled"] = False
        rec["folder"] = folder
        rec["declined"] = True
        self._save()

    def bind_folder(self, pid: str, folder: str) -> None:
        """既知レコードにフォルダ実体を書き足す（後方互換の暗黙採用用）。

        folder 記録の無い旧レコードで、その id の manifest フォルダが 1 つだけの
        ときに bootstrap が呼ぶ。未知 id には何もしない（未知 = 無効の不変条件を
        壊さない）。値が変わるときだけ保存する。
        """
        rec = self._records.get(pid)
        if rec is None or rec.get("folder") == folder:
            return
        rec["folder"] = folder
        self._save()

    def disable_after_failure(self, pid: str, message: str) -> None:
        """ロード/activate 失敗でプラグインを自動無効化する（次回起動で走らせない）。

        *message* は残さずログへ流す — ユーザーへの告知は失敗したその場の
        モーダル（bootstrap / 管理ダイアログ）が担い、調査に要る全文
        （トレースバック付き）は ``data/logs/viewer.log`` にある。記録側に
        写しを持つと、原因を取り除いた後も古い理由が残って実体と食い違う。
        """
        logger.info("plugin {!r} disabled after failure: {}", pid, message)
        rec = self._records.setdefault(pid, self._new_record())
        rec["enabled"] = False
        self._save()


# ------------------------------------------------------- crash sentinel


def _sentinel_token(pid: str) -> str:
    return f"{pid}\n{os.getpid()}"


def process_alive(pid: int) -> bool:
    """*pid* のプロセスが生存しているか（新規依存なしの best-effort 判定）。

    判定不能（権限エラー等）は「生存」に倒す — 生存の誤認は「クラッシュ
    検出が次回起動に 1 回遅れる」だけで、他インスタンスの生きたセンチネルを
    誤回収するより安全。OS の PID 再利用で無関係なプロセスを生存と誤認する
    可能性も同じ理由で best-effort として許容する。

    センチネルの判定（多重起動での誤回収防止）のほか、デタッチ起動した子
    プロセスの早期死亡検出も同じ問いなので、判定はこの 1 本を共有する
    （プラグインからも import できる公開関数）。
    """
    if pid <= 0:  # os.kill(0, 0) はプロセスグループ宛てになるため弾く
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        _STILL_ACTIVE = 259
        _ERROR_INVALID_PARAMETER = 87
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # 87 = 存在しない PID。それ以外（アクセス拒否等）は
                # 「存在するが開けない」なので安全側の生存扱い。
                return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return True  # 判定不能 → 生存扱い
                return exit_code.value == _STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return True  # 判定不能 → 生存扱い
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError 含む = 存在する / 判定不能
        return True
    return True


def _unlink_if_content(path: Path, expected: str) -> None:
    """*path* の中身が *expected* のままのときだけ削除（compare-and-delete）。"""
    try:
        if path.read_text(encoding="utf-8") != expected:
            return
        path.unlink(missing_ok=True)
    except OSError:
        pass


def write_sentinel(data_dir: Path, pid: str) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / SENTINEL_NAME).write_text(
            _sentinel_token(pid), encoding="utf-8"
        )
    except OSError:  # ベストエフォート（書けなくても起動は続行）
        pass


def clear_sentinel(data_dir: Path, pid: str | None = None) -> None:
    """センチネルを消す。*pid* 指定時は自分が書いた内容のときだけ消す。

    compare-and-delete: 多重起動時、別インスタンスが書き直したセンチネル
    （＝その activate がまだ生きている証拠）をこちらの完了掃除で潰さない。
    """
    path = data_dir / SENTINEL_NAME
    try:
        if pid is not None:
            _unlink_if_content(path, _sentinel_token(pid))
            return
        path.unlink(missing_ok=True)
    except OSError:
        pass


def read_and_clear_sentinel(data_dir: Path) -> str | None:
    """前回起動の残骸センチネルを読み、消してから中身（plugin id）を返す。

    2 行目の PID が**自プロセス以外で生存中**なら、それは並走する別
    インスタンスが activate 実行中の生きたセンチネル — クラッシュ扱い
    せず・消さずに ``None`` を返す（多重起動での誤回収防止）。PID が
    死んでいる・自 PID・パース不能のときだけ従来どおりクラッシュ報告して
    クリアする。クリアは読んだ内容との compare-and-delete（判定中に別
    インスタンスが書き直したセンチネルを潰さない）。
    """
    path = data_dir / SENTINEL_NAME
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    lines = content.splitlines()
    pid = lines[0].strip() if lines else ""
    writer_pid: int | None = None
    if len(lines) >= 2:
        try:
            writer_pid = int(lines[1].strip())
        except ValueError:
            writer_pid = None
    if (
        writer_pid is not None
        and writer_pid != os.getpid()
        and process_alive(writer_pid)
    ):
        return None  # 別インスタンスの activate が進行中
    _unlink_if_content(path, content)
    return pid or None


__all__ = [
    "PluginStore",
    "SENTINEL_NAME",
    "process_alive",
    "write_sentinel",
    "clear_sentinel",
    "read_and_clear_sentinel",
]
