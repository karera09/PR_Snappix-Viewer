"""Viewer entrypoint.

Kept deliberately small so the dispatch in launcher.py can import it without
pulling in PySide6 at module load time.
"""

from __future__ import annotations

import faulthandler
import math
import os
import sys
import traceback
from pathlib import Path
from typing import NamedTuple


#: クラッシュダンプ先のファイルハンドル（プロセス生存中 open のまま保持する
#: — faulthandler はクラッシュ時にこの fd へ直接書くため、閉じると無効になる）。
_crash_dump_file = None

#: 追記式クラッシュログの肥大抑止。セッション見出し + 注記の数行と、まれな
#: ダンプしか書かないので通常は到達しないが、超えたら次回起動で作り直す。
_CRASH_LOG_MAX_BYTES = 1_000_000

#: faulthandler が本物のレコードの先頭に書く綴り。``Windows fatal exception:
#: code 0x…`` / ``… access violation`` / ``Fatal Python error: …`` の 2 系統が
#: あり、クラッシュログのトリアージ（目視でも grep でも）はこの行を数える。
#:
#: **注記・コメント・ログ文言にこの綴りをそのまま書かないこと**。1 回でも書くと、クラッシュが 0 件のセッションでも
#: ``viewer_crash.log`` に必ずこの綴りが載り、``grep`` が常にヒットする＝
#: このファイルの唯一の価値である**信号の純度**が消える。コードを名指し
#: したいときは :func:`_benign_fault_note_lines` のように裸のコード
#: （``0x8001010d``）だけを書く。
FAULT_RECORD_PREFIXES = ("Windows fatal exception", "Fatal Python error")

#: 追記式クラッシュログのセッション見出しの綴り。
#:
#: 書き手（:func:`_enable_crash_dumps`）と読み手（:func:`summarize_crash_log`）
#: の**唯一の接点**。見出しは :func:`_session_header_line` だけが組み立て、
#: パーサはこの定数しか知らない — 書式契約を 1 箇所に閉じておかないと、
#: 見出しの体裁を変えた日に要約が黙って「セッション境界が 1 つも無いログ」
#: として全文集計へ落ち、過去の 1 件が以後ずっと警告され続ける。
CRASH_LOG_SESSION_PREFIX = "=== session start "

#: faulthandler が ``Windows fatal exception: code <hex>`` として記録するが、
#: 実際には発生元（COM ランタイム）の内側で処理され、ビューアはそのまま走り
#: 続ける SEH コード。コード → 良性である理由。
#:
#: CPython の faulthandler は Windows で vectored exception handler を張り、
#: 重大度ビット（0x80000000）が立つコードを一律「fatal exception」として
#: 書き出す。コード単位のフィルタ機構は無い。faulthandler の出力を後段で
#: 濾す（パイプ + フィルタスレッド等）と、本物のクラッシュ時に「書き込み側
#: が生きている」前提が要るため、この機構の唯一の価値である**書き込みの
#: 確実性**を落とす。そこで出力はそのままにし、(a) セッション見出しの直後に
#: 注記を残して人間の誤読を防ぎ、(b) 起動時に既存のログを
#: :func:`summarize_crash_log` で数え直して「良性を除いた件数」をビューア
#: ログへ出す（機械的トリアージの読み取り側）。
#:
#: **この表は原理的に取りこぼす**（VM 実測 2026-08-30）。
#: ``0xE06D7363``(C++ EH) / ``0xE0434352``(CLR) / ``0x40010006`` /
#: ``0x406D1388`` は faulthandler が最初から無視するので**登録してはいけない**
#: （記録されないコードを「良性として除外した」と数えると嘘になる）。一方
#: ``0x8001010e``(RPC_E_WRONG_THREAD) や ``0x80004005`` 等の他の
#: ``0x8001xxxx`` / ``0x8000xxxx`` は記録されるので、実測で良性と確認でき
#: 次第ここへ足す。
BENIGN_FAULT_CODES: dict[str, str] = {
    "0x8001010d": (
        "RPC_E_CANTCALLOUT_ININPUTSYNCCALL - raised and handled inside COM "
        "when a UI Automation client (screen reader, accessibility tool) "
        "walks the desktop; the viewer keeps running"
    ),
}


def _benign_fault_note_lines() -> list[str]:
    """:data:`BENIGN_FAULT_CODES` をクラッシュログへ書く注記行に整形する。

    セッション見出しの直後に置くので、どのセッションのダンプを見ていても
    注記が視界に入る。``#`` 始まりにして faulthandler が書く本物のレコード
    と一目で区別できるようにする。

    注記は良性コードを**裸の数値で**名指しし、
    :data:`FAULT_RECORD_PREFIXES` の綴りは決して含めない — 含めると
    「クラッシュ 0 件のセッションでも grep が必ずヒットする」形になり、
    信号の濁りを自分で作ってしまう。
    """
    return [
        "# note: SEH {} is benign, not a crash - {}".format(code, why)
        for code, why in BENIGN_FAULT_CODES.items()
    ]


def _is_fault_record(line: str) -> bool:
    """*line* が faulthandler の書く本物のレコード見出しか。"""
    return line.startswith(FAULT_RECORD_PREFIXES)


def _session_header_line(pid: int, when: str) -> str:
    """追記式クラッシュログのセッション見出し 1 行を組み立てる。

    書式契約はこの関数**だけ**が持つ（読み手が知るのは
    :data:`CRASH_LOG_SESSION_PREFIX` のみ）。
    """
    return "{}pid={} at {} ===".format(CRASH_LOG_SESSION_PREFIX, pid, when)


def _count_fault_records(text: str) -> tuple[int, int]:
    """*text* 内のレコード見出しを ``(ネイティブ級, 良性として除外)`` に畳む。

    faulthandler のレコード見出し（:data:`FAULT_RECORD_PREFIXES`）を数え、
    :data:`BENIGN_FAULT_CODES` に載る SEH コードを名指しした行だけを良性側へ
    振り分ける。注記行（``#`` 始まり）は :func:`_benign_fault_note_lines` の
    規約により見出しの綴りを含まないので、そもそも数に入らない。
    """
    native = 0
    benign = 0
    for line in text.splitlines():
        if not _is_fault_record(line):
            continue
        if any(code in line for code in BENIGN_FAULT_CODES):
            benign += 1
        else:
            native += 1
    return native, benign


def _last_session_text(text: str) -> str:
    """最後のセッション見出し以降だけを返す（見出しが無ければ全文）。

    見出しが 1 つも無いのは「見出しを書く前のバージョンが残したログ」か
    「手で置かれたファイル」だけ。数えないより安全側（全文を直近扱い）に
    倒す — 見落としよりノイズを選ぶ。
    """
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(CRASH_LOG_SESSION_PREFIX):
            return "\n".join(lines[i:])
    return text


class CrashLogSummary(NamedTuple):
    """:func:`summarize_crash_log` の結果（直近セッション分 + ファイル累計）。"""

    #: 直近セッション（= 前回の起動）のネイティブ級レコード件数。
    native: int
    #: 直近セッションで良性として除外した件数。
    benign: int
    #: ファイル全体のネイティブ級レコード件数。
    total_native: int
    #: ファイル全体で良性として除外した件数。
    total_benign: int


def summarize_crash_log(text: str) -> CrashLogSummary:
    """クラッシュログ本文を**直近セッション分**とファイル累計に畳む。

    ログは追記式で、切り詰めは 1MB 超のときだけ（1 レコードは 1〜3KB なので
    実質ローテートしない）。全文を数えると、本物のクラッシュが 1 回でも
    載った時点で**以後すべての起動で警告が出続ける**＝数か月前の一度きりの
    事故がサポート時に「今落ちている」と読める（累積ラッチ）。集計の単位は
    ログレベルではなくスコープの問題なので、``=== session start …`` の
    見出し（:data:`CRASH_LOG_SESSION_PREFIX`）で直近セッションを切り出す。
    要約は :func:`_report_previous_crash_records` より前に呼ばれ、その時点で
    今回のセッション見出しはまだ書かれていない — つまり「直近」＝前回の起動。

    累計も併せて返す（サポートで「過去に何回あったか」を見たいときのため）
    が、**警告に使ってよいのは直近セッション分だけ**。

    純粋関数（本文 → 件数）にしてあるのは、これがクラッシュログの**読み取り
    側**の実体だからで、置き場所（起動時のログ要約か、将来の診断ダイアログ
    か）に依存しない形で検証できるようにするため。
    """
    total_native, total_benign = _count_fault_records(text)
    native, benign = _count_fault_records(_last_session_text(text))
    return CrashLogSummary(native, benign, total_native, total_benign)


def _report_previous_crash_records(trace_path: Path) -> None:
    """既存の ``viewer_crash.log`` を要約して**ビューアログ**へ 1 行出す。

    クラッシュログの読み取り側を、UI を増やさずに成立させるための
    経路。注記行だけでは「人間が crash log を開いたときの誤読」しか防げず、
    「クラッシュしたのか？」という問いに機械的に答えられない（良性の SEH は
    faulthandler が実際にレコードを書くので、綴りを grep すれば当たる）。
    起動のたびに ``viewer.log`` へ良性除外後の件数を出しておけば、ユーザー
    から届くログ 1 本で切り分けが済む。

    **警告は直近セッション（＝前回の起動）だけで出す**。ログは実質ローテート
    しないので、全文を数えると一度きりのクラッシュが以後ずっと WARNING を
    出し続ける（累積ラッチ — 数か月前の事故が「今落ちている」と読める）。
    累計は同じ行に併記するだけで、格上げの根拠には使わない。

    ``setup_logging`` は :func:`main` でこの呼び出しより前に確立済み。診断
    機構の失敗で起動を落とさないのは :func:`_enable_crash_dumps` と同じ規約
    （読めなければ黙って諦める）。
    """
    from loguru import logger

    try:
        text = trace_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not text.strip():
        return
    s = summarize_crash_log(text)
    if s.native:
        logger.warning(
            "viewer_crash.log: previous session left {} native-level crash "
            "record(s) ({} benign SEH ignored) [whole log: {} native / {} "
            "benign]: {}",
            s.native, s.benign, s.total_native, s.total_benign, trace_path,
        )
    else:
        logger.info(
            "viewer_crash.log: previous session left no native-level crash "
            "record ({} benign SEH ignored) [whole log: {} native / {} "
            "benign]",
            s.benign, s.total_native, s.total_benign,
        )


def _enable_crash_dumps(trace_path: Path) -> None:
    """ネイティブ級クラッシュ（SIGSEGV / SIGABRT 等）の全スレッド Python
    トレースバックを *trace_path* へ常時ダンプできるようにする。

    凍結ビルドは PyInstaller windowed（コンソール無し）で stderr が無く、
    ネイティブ abort（0xc0000409 等）はビューアログに一切
    痕跡を残さず消える。faulthandler をログフォルダのファイルへ向けて
    おけば、abort() / アクセス違反系の少なくとも一部で「どの Python
    フレームに居たか」が残る（__fastfail 直行など捕まらない種別もある）。

    追記モードで開き、起動ごとにセッション見出しを 1 行書く — 前回
    クラッシュのダンプを次回起動で消さないため。診断機構の失敗で起動を
    落とさない（best effort）。``SNAPPIX_VIEWER_TRACE=1`` の freeze trace
    は後段で ``faulthandler.enable`` を自ファイルへ上書きするので併用時は
    そちらへ一本化される。

    このファイルの価値は「何か書かれていたらネイティブ級のクラッシュが
    あった」という信号の純度にあるが、faulthandler は COM が内部で処理する
    良性の SEH（0x8001010d 等）まで拾ってしまう。faulthandler
    側でフィルタはできないので、両側から打ち消す:

    * **書き手側** — 見出しの直後に :data:`BENIGN_FAULT_CODES` の注記を書く
      （人間が開いたときの誤読防止）。注記は
      :data:`FAULT_RECORD_PREFIXES` の綴りを含まないので、grep での
      トリアージを自分で汚さない。
    * **読み手側** — 追記を始める前に、既にあるログを
      :func:`_report_previous_crash_records` が要約してビューアログへ出す
      （良性を除いた件数）。要約は**直近セッションの見出し以降**だけを警告に
      使う（このファイルは実質ローテートしないので、全文集計にすると 1 回の
      クラッシュが以後ずっと警告を出し続ける）。
    """
    global _crash_dump_file
    import datetime

    _report_previous_crash_records(trace_path)
    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a"
        try:
            if trace_path.stat().st_size > _CRASH_LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        f = open(trace_path, mode, encoding="utf-8", buffering=1)
        # 見出しの書式は _session_header_line が単独で持つ（読み手の
        # summarize_crash_log は CRASH_LOG_SESSION_PREFIX しか知らない）。
        f.write(
            _session_header_line(
                os.getpid(),
                datetime.datetime.now().isoformat(timespec="seconds"),
            )
            + "\n"
        )
        for note in _benign_fault_note_lines():
            f.write(note + "\n")
        faulthandler.enable(file=f, all_threads=True)
        _crash_dump_file = f
    except OSError:
        pass


def _enable_freeze_trace(trace_path: Path, interval: float) -> None:
    """Write periodic all-thread tracebacks to *trace_path*.

    Runs on a C-level timer thread spawned by ``faulthandler`` so the
    dumps keep landing even when the Python main thread is stuck inside
    Qt (e.g. a paint / setIcon burst during fast scroll).  The Qt-based
    perf dialog can't report those freezes — its own repaint is blocked
    by the same stall — so we fall back to this always-on filesystem log
    whenever ``SNAPPIX_VIEWER_TRACE=1`` is set.

    The file is opened with ``buffering=1`` (line-buffered) so each
    dump is flushed to disk immediately; otherwise a crash right after
    the freeze would swallow the most useful entry.
    """
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_file = open(trace_path, "w", encoding="utf-8", buffering=1)
    faulthandler.enable(file=trace_file, all_threads=True)
    faulthandler.dump_traceback_later(
        max(1, int(interval)),
        repeat=True,
        file=trace_file,
    )


#: 凍結トレースのダンプ間隔（秒）の既定値。
_TRACE_INTERVAL_DEFAULT = 2.0
#: 同・上限（1 時間）。診断の周期としてこれ以上長い値に意味は無く、有限で
#: あっても ``faulthandler.dump_traceback_later`` が受け取れる範囲を超える
#: 綴り（``2e9`` 等）は ``OverflowError`` になるため、ここで頭打ちにする。
_TRACE_INTERVAL_MAX = 3600.0


def _trace_interval(raw: str | None) -> float:
    """``SNAPPIX_VIEWER_TRACE_INTERVAL`` の値（使えない綴りは既定へ）。

    ``float()`` は ``nan`` / ``inf`` / ``1e400`` を ``ValueError`` 無しで
    通すので、綴りの検査を ``except ValueError`` だけに任せると
    :func:`_enable_freeze_trace` の ``int(interval)`` が ``ValueError`` /
    ``OverflowError`` を送出する。``main`` はこれを捕まえず、windowed の
    凍結ビルドは stderr を持たないので「exe を叩いても何も起きない」に
    なる（診断のための環境変数が、診断そのものを殺す形）。有限かつ正で
    なければ既定へ落とす。

    有限でも大きすぎる綴り（``2e9`` / ``999999999``）は ``int()`` を素通り
    する一方 ``faulthandler.dump_traceback_later`` が ``OverflowError`` を
    送出するので、:data:`_TRACE_INTERVAL_MAX` で頭打ちにする（既定へ落とさ
    ないのは「長い周期が欲しい」という意図をできる限り汲むため）。
    """
    if raw is None:
        return _TRACE_INTERVAL_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _TRACE_INTERVAL_DEFAULT
    if not math.isfinite(value) or value <= 0.0:
        return _TRACE_INTERVAL_DEFAULT
    return min(value, _TRACE_INTERVAL_MAX)


def _env_flag(name: str) -> bool:
    """True when environment variable *name* holds an affirmative value.

    ``bool(os.environ.get(...))`` treats every non-empty string as true, so
    ``SNAPPIX_NO_PLUGINS=0`` — the natural way to write "not disabled" —
    silently entered safe mode and made every plugin surface disappear.  Unset, empty
    and the usual negative spellings mean "off"; anything else means "on"
    (so the documented ``=1`` keeps working, as does bare ``=true``).

    Used for **every** viewer-side ``SNAPPIX_*`` switch, not just one:
    ``SNAPPIX_VIEWER_TRACE=0`` sits on the same trap (it would enable the
    freeze trace), and leaving one caller on a separate spelling is how the
    pair drifts apart.
    """
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _folder_to_report(target: str) -> str:
    """モーダルに出す「書き込めないフォルダ」を ``OSError.filename`` から決める。

    「そのまま出す」と決めたときは *target* の文字列を**無加工で**返す —
    ``Path`` に通して戻すと Windows では区切りが ``\\`` へ正規化され、OS が
    報告した表記（``C:/Program Files/…``）と食い違う。

    文言が「このフォルダ」と言うので、ログ**ファイル**由来（``setup_logging``
    経路: ``…/data/logs/viewer_<pid>.log``）は親フォルダへ寄せ、フォルダ
    由来（``get_paths()`` の mkdir 経路）はそのまま出す。

    判定はまず**既知のパス集合との照合**で行う: ``get_paths(ensure=False)``
    が返す base / data / logs / library（と base の祖先 — ``mkdir(parents=
    True)`` の失敗点になり得る）はフォルダ、logs の直下はログファイル。
    拡張子の有無だけで見るヒューリスティックは「ドットを含み、かつ
    **まだ作られていない**ベースフォルダ」（``D:\\Snappix.v1`` へ初めて展開
    して mkdir 自体が失敗）で ``is_dir()`` が偽になり、親（``D:\\``）を案内
    してしまう。既知集合に無い未知のパスに限り、
    「拡張子あり かつ 実在フォルダでない → 親」へ落とす。
    """

    def _norm(p: Path) -> str:
        return os.path.normcase(os.path.abspath(str(p)))

    candidate = Path(target)
    try:
        from ..common.paths import get_paths

        # ensure=False は mkdir しない純粋なパス解決なので再送出しない。
        known = get_paths(ensure=False)
    except Exception:  # pragma: no cover (defensive)
        known = None
    if known is not None:
        folders = {
            _norm(p) for p in (known.base, known.data, known.logs, known.library)
        }
        folders.update(_norm(p) for p in known.base.parents)
        if _norm(candidate) in folders:
            return target
        if _norm(candidate.parent) == _norm(known.logs):
            return str(candidate.parent)
    if candidate.suffix and not candidate.is_dir():
        return str(candidate.parent)
    return target


def _fail_unwritable_base(exc: OSError) -> int:
    """書き込み不可による起動失敗を可視化して終了する.

    ``get_paths()``（既定 ``ensure=True``）は data / logs / library を
    mkdir する。ポータブル配布を C:\\Program Files 配下へ展開して標準
    ユーザーで起動・書込保護スイッチ付き USB/SD・読み取り専用マウントの
    共有では PermissionError / OSError になるが、凍結ビルドは PyInstaller
    windowed（コンソール無し）で、しかもログ先（``paths.logs``）も同じ
    mkdir に依存して共倒れするため、従来は例外がプロセス最上位まで抜けて
    「exe をダブルクリックしても何も起きない」だけだった。ここで
    QApplication を生成して i18n 済みモーダルを出し、原因パスを明示して
    終了する（stderr のログにも残す — dev 実行 / コンソール付き起動用）。

    **``setup_logging`` の失敗もここへ来る**: ログシンクの確立は同じ
    「書き込めない」失敗クラスで、しかも同じく無言終了になる経路。その場合 ``exc.filename`` はフォルダではなく
    ログ**ファイル**（``…/data/logs/viewer_<pid>.log``）なので、案内には
    親フォルダを出す — モーダル文言が「このフォルダ」と言うため。
    """
    from loguru import logger

    logger.error(
        "startup failed: portable base directory is not writable: {}", exc,
    )
    target = getattr(exc, "filename", None)
    if target:
        # ログファイル由来（setup_logging 経路）なら親フォルダへ寄せる —
        # 文言が「このフォルダ」と言うので、ファイルパスを出すとちぐはぐ
        # になる。フォルダ由来（mkdir 経路）はそのまま（判定の詳細は
        # ``_folder_to_report``）。
        target = _folder_to_report(str(target))
    else:
        try:
            from ..common.paths import get_paths

            # ensure=False は mkdir しない純粋なパス解決なので再送出しない。
            target = str(get_paths(ensure=False).base)
        except Exception:  # pragma: no cover (defensive)
            target = ""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ..common.i18n import t

    # モーダル表示には QApplication が要る — 通常起動の生成点（get_paths
    # 成功後）より前に失敗しているので、ここで生成する（既定ロケール ja の
    # カタログは import 時に登録済みで t() はそのまま使える）。
    # モーダルの間 QApplication を生かしているのは**ローカル束縛 ``app``**
    # （この関数が返るまで参照が残る）。下の ``assert`` はその束縛を「使う」
    # 一行にすぎず、挙動は担っていない — 凍結ビルドは ``optimize=2`` で
    # ``assert`` ごと落とすので、担わせてはならない。
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None
    QMessageBox.critical(
        None,
        t("viewer.app.paths_error_title"),
        t("viewer.app.paths_error_body", path=str(target)),
    )
    return 1


# 起動プローブの本体は path_probe.probe_path_kind（ブックマークジャンプと
# 共有）。この名前は起動経路の呼び出しとテストの互換のため残す。
def _probe_path_kind(path_str: str, timeout: float = 2.0) -> str | None:
    from .path_probe import probe_path_kind

    return probe_path_kind(path_str, timeout)


def _is_resumable_root(last_root: str) -> bool:
    """前回の ``last_root`` を再開位置に使ってよいか.

    ZIP ドリルインの展開先（閉じると消える ``data/tmp`` 配下）は再開位置に
    しない — 旧版が書いた値や掃除に失敗して残った展開先で起動すると、
    ↑ でアプリ自身の ``data/`` が見えてしまう。既定ライブラリで開く。
    """
    from .locations import is_zip_temp_path

    return bool(last_root) and not is_zip_temp_path(last_root)


def main(initial_root: str | None = None, *, no_plugins: bool = False) -> int:
    from loguru import logger
    from PySide6.QtWidgets import QApplication

    from ..common.paths import get_paths
    from ..common.logging import setup_logging
    from ..common.i18n import set_locale
    from ..common.shared_prefs import (
        resolve_startup_language,
        resolve_startup_theme,
    )
    from .state import load_state

    try:
        paths = get_paths()
        # ログシンクの確立も同じ失敗クラス（書き込めないベースフォルダ）に
        # 属するので同じガードの内側で行う。``get_paths()`` が logs/ の mkdir
        # に成功しても、ディスク満杯・AV/バックアップがログを掴んでいる・
        # logs が同名ファイルとして存在する、等でここが OSError になり得る。
        # ガードの外に置いていた間は、そのまま例外がプロセス最上位まで抜けて
        # 「exe をダブルクリックしても何も起きない」に戻っていた（windowed
        # 凍結ビルドは stderr が無い）。
        setup_logging(log_file=paths.logs / "viewer.log")
    except OSError as exc:
        # 書き込めない場所へ展開された（Program Files / 書込保護
        # メディア等）。無言終了ではなくモーダルで案内して終了する。
        return _fail_unwritable_base(exc)
    # ネイティブ級クラッシュの常時ダンプ（windowed 凍結ビルドは
    # stderr が無く、abort 系がログ痕跡ゼロで消える）。ファイル名は logging の
    # _prune_stale_logs 対象外（viewer_<pid>.log 形式でない）なので消されない。
    _enable_crash_dumps(paths.logs / "viewer_crash.log")
    state = load_state()
    # Resolve the UI language and switch the i18n locale BEFORE importing the
    # viewer GUI below, so module-level ``t()`` constants (combo option tables,
    # etc.) evaluate under the right locale.  ``shared_prefs.json`` is the
    # shared source of truth (shared wins, else the local ``viewer_state.json``
    # value is promoted once) — same pattern as the theme.  Fold the winner
    # back into ``state.language`` (the ``state.py`` field contract) so any UI
    # reading the local field reflects the effective locale, mirroring the
    # ``state.theme`` write-back below.
    state.language = resolve_startup_language(state.language)
    set_locale(state.language)
    # ``shared_prefs.json`` is the single source of truth for the theme across
    # both tools.  Reconcile the viewer's legacy local ``viewer_state.json``
    # theme with the shared value (shared wins; otherwise the local value is
    # promoted into shared once) and fold the winner back into ``state.theme``
    # so both ``ViewerWindow``'s ``apply_theme`` call and the 表示→テーマ menu's
    # checked state reflect it.  The local field remains for round-trip
    # compatibility but is no longer authoritative.
    state.theme = resolve_startup_theme(state.theme)  # type: ignore[assignment]

    # AI 機能パック（有償プラグイン）の可用性ゲート。ViewerWindow の import /
    # 構築より前に判定する — AI UI の構築可否とインデックスのオープンが
    # このフラグに依存するため。読むのは data/plugins.json の有効化記録だけで、
    # plugins/ の走査もプラグインコードの実行もしない（「どのフォルダを
    # 走らせてよいか」の判定は、ユーザーに問い直せる bootstrap 側だけが持つ）。
    plugins_disabled = no_plugins or _env_flag("SNAPPIX_NO_PLUGINS")
    plugin_store = None
    if not plugins_disabled:
        from .ai_pack import maybe_enable_from_plugins
        from .plugin_host.store import PluginStore

        # 記録は起動を通して 1 インスタンスを共有する（bootstrap へ渡す）—
        # 2 つ開くと片方の書き込みがもう片方のメモリ上の写しに反映されない。
        plugin_store = PluginStore(paths.data / "plugins.json")
        maybe_enable_from_plugins(paths, store=plugin_store)

    # Imported after the locale is set so any module-level display strings
    # resolve correctly.
    from .main_window import ViewerWindow

    if _env_flag("SNAPPIX_VIEWER_TRACE"):
        interval = _trace_interval(os.environ.get("SNAPPIX_VIEWER_TRACE_INTERVAL"))
        trace_path = paths.logs / "viewer_freeze_trace.log"
        # 診断の失敗で起動を殺さない二重の歯止め（値の検査は
        # ``_trace_interval`` が、それ以外の失敗 — ログ先が書けない・
        # faulthandler が拒む — はここが吸う）。windowed の凍結ビルドは
        # stderr を持たないので、ここを抜けた例外は「何も起きない exe」になる。
        try:
            _enable_freeze_trace(trace_path, interval)
        except BaseException:  # noqa: BLE001
            logger.warning(
                "Freeze tracing could not be enabled:\n{}",
                traceback.format_exc(),
            )
        else:
            logger.info(
                "Freeze tracing enabled: all-thread traceback dumps every {}s -> {}",
                interval, trace_path,
            )

    # A file pre-selected on startup when the launch argument was a file path
    # (Explorer folder-drop of a file / association).  Resolved below and
    # handed to ViewerWindow, which selects the tile once the parent populates.
    initial_select: Path | None = None
    missing_root: str | None = None  # 見つからなかった行き先（show 後に知らせる）
    if initial_root:
        target = Path(initial_root)
        kind = _probe_path_kind(str(target))
        if kind == "file":
            # Open the containing folder and pre-select the file.
            initial_select = target
            root = target.parent
        elif kind == "missing":
            missing_root = str(target)
            root = paths.library
        else:
            # "dir" or a timeout (None): assume a directory and open it; the
            # async scan surfaces any real failure with the 再試行 card.
            root = target
    elif _is_resumable_root(state.last_root):
        # ``is_dir`` on an offline / sleeping NAS share can block for tens of
        # seconds — before the window even shows (B05).  Probe with a hard
        # timeout on a worker thread: a definite "not a directory" falls back
        # to the library; a TIMEOUT (share still waking) keeps the last root
        # and lets the async scan surface the error card + 再試行 instead.
        if _probe_path_kind(state.last_root) in ("file", "missing"):
            missing_root = state.last_root
            root = paths.library
        else:
            root = Path(state.last_root)
    else:
        root = paths.library

    app = QApplication(sys.argv)
    app.setApplicationName("Snappix Viewer")
    app.setOrganizationName("snappix")

    # Qt's own dialog strings (QMessageBox の はい/いいえ, standard buttons,
    # QInputDialog の OK/Cancel) come from Qt's resources,
    # not our catalog — without this they stay English on an otherwise
    # Japanese product.  Installed after
    # set_locale (above) and before any window / dialog is constructed, since
    # Qt resolves translations at widget construction time.  A missing .qm
    # (mangled install) degrades to English rather than blocking startup.
    from ..common.qt_i18n import install_qt_translator

    qt_qm = install_qt_translator(app)
    if qt_qm is None:
        logger.info(
            "Qt translations for locale '{}' not found; "
            "standard dialogs stay in English",
            state.language,
        )

    # First-run consent gate: theme the app first so the terms dialog is
    # styled, then refuse to proceed unless the user accepts the
    # 利用規約・免責事項.
    from ..common.ui import apply_theme
    from ..common.terms import require_consent

    apply_theme(state.theme)
    if not require_consent():
        # Say why the app is exiting — a silent vanish before any window has
        # been shown reads as a crash.
        from ..common.terms_dialog import notify_declined

        notify_declined()
        return 0

    win = ViewerWindow(root=root, state=state, initial_select=initial_select)

    # プラグイン基盤（plugin_host/）。ウィンドウ構築後・show 前に配線する
    # （メニュー等の注入先が揃っていて、かつ初回確認モーダルが最前面に出る
    # タイミング）。--no-plugins / SNAPPIX_NO_PLUGINS=1 のセーフモードでは
    # プラグインコードを 1 行も実行しない。
    if plugins_disabled:
        logger.info("plugins disabled (safe mode)")
        # ログだけでは気づけない — 常時見える
        # タイトルバーにも出す。
        win.set_safe_mode(True)
    else:
        from .plugin_host.bootstrap import bootstrap_plugins

        try:
            # plugins/ の走査はここが唯一の実施点（可用性ゲートは走査しない）。
            bootstrap_plugins(win, paths, store=plugin_store)
        except Exception:  # pragma: no cover (defensive)
            # 基盤自体の想定外エラーで viewer を殺さない（プラグイン無し
            # で起動継続。個々のプラグイン失敗は bootstrap 内で処理済み）。
            logger.exception("plugin bootstrap failed; continuing without plugins")

    win.show()
    if plugins_disabled:
        # タイトルの「（セーフモード）」の説明は show の**後**に出す
        # （可視化前だとトーストの席がずれる）。
        win.notify_safe_mode()
    if missing_root is not None:
        # 無言で初期フォルダに落ちると「開く」が壊れたように見える。
        from ..common.i18n import t

        win._show_toast(
            t("viewer.bookmark_dialog.folder_not_found", path=missing_root),
            "warning",
            duration_ms=0,
            action_text=t("viewer.main_window.folder_not_found_open_other"),
            on_action=win._pick_root,
        )
    logger.info("snappix-viewer started. Root: {}", root)
    code = app.exec()
    # 窓じまいで予算内に空かなかったワーカープール（死んだ共有の I/O で止まって
    # いる）が残っていれば、その I/O タイムアウトを待たずにここで終わる。
    # 残っていなければ何もしない（``pool_teardown`` の docstring 参照）。
    from .pool_teardown import exit_if_pools_stranded

    exit_if_pools_stranded(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
