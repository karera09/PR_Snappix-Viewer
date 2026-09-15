"""loguru configuration with optional Qt sink.

**この層の不変条件（issue #132 第 4 ラウンド）**: *``logger`` の呼び出しは、
保存先が何をしていても呼び出しスレッドを塞がない*。

診断のためのログ機構が、診断したい障害そのもの（到達不能になった SMB 共有）で
死ぬ、という構図を解くための宣言。実測（loguru 0.7.3 / Windows）:

* ``enqueue=True`` は「呼び出しスレッドを塞がない」を**保証しない**。loguru の
  enqueue キューは ``multiprocessing.SimpleQueue`` = パイプで、1 レコードごとに
  「整形済み文字列 + record 辞書丸ごと」を pickle して書き込む。既定 8KB の
  パイプバッファは実測 **9 レコード**で埋まり、以後の ``put`` は永久に返らない。
  しかも ``Handler.emit`` はハンドラロックを握ったまま ``put`` するので、
  **そのシンクへの以後の全 emit** が道連れになる。
* 9 レコードは死んだ共有で自動的に積まれる（フリーズ監視の ``[diag]`` 警告 /
  サムネデコード失敗 / フォルダスキャンの警告）ので、「終了時に積むのは 2 行
  だけだから届く」という前提は成立しない。GUI スレッドが 15 行吐こうとすると、
  **吐き終える前に**永久ブロックする（実測）。

そこでファイル / stderr の両シンクを :class:`_BoundedSink` に載せる:
呼び出しスレッドがやるのは有界 ``deque`` への ``append`` だけで、実際の I/O は
専用のデーモンライタースレッドが行う。保存先が応答しなければバッファが満杯に
なり、**古いレコードから捨てる**（＝待たない）。

トレードオフの判断（「ログを失わない」 vs 「死んだ保存先で止まらない」）:
**止まらないことを優先する**。理由は 2 つ — (a) 死んだ保存先では待っても
レコードは保存されない（ブロックは「失う」を「固まる」へ置き換えるだけで、
1 行も救わない）、(b) *遅いが生きている* 保存先の取りこぼしはバッファ
（``_SINK_BUFFER_RECORDS`` 行）が吸収するので、実際に失うのは「保存先が
数千行ぶん応答しない」＝もう保存できない状況だけ。捨てた行数は復帰後の
1 行目に記録するので、欠落は無言にならない。

**この論拠は件数が正確なときにしか成立しない**（R4 レビュー H-2）: 欠落は
「バッファ満杯で押し出した」経路と「ライタースレッドが引き取った後に
``write_lines`` が例外を投げた」経路の 2 つで起き、後者の件数を戻し忘れると
まさに「もう保存できない」状況でだけ数字が実際の欠落から乖離する（＝無言
より悪い誤報。将来「2048 行が吸収する」と判断する根拠まで汚染する）。通知は
原因も取り違えないこと — 例外経路ではバッファは満杯ではない。
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

from .paths import get_paths

_configured = False

#: :class:`_BoundedSink` が保持できる未書き込みレコード数。整形済みの 1 行は
#: 数百バイト程度なので、2048 行でも数 MB に収まる（＝有界メモリ）。死んだ
#: 共有では「フリーズ監視の警告 + サムネ失敗」が毎秒数行なので、数分ぶんの
#: 猶予がある。これを超えるのは「保存先がもう応答しない」ことの実証。
_SINK_BUFFER_RECORDS = 2048

#: シンクを閉じるとき（``logger.remove`` / loguru の atexit）にライター
#: スレッドを待つ上限 (秒)。**ここが有界でないと、終了時に loguru の
#: ``atexit.register(logger.remove)`` が死んだ保存先へ張り付いて
#: 「窓は閉じたのにプロセスが終わらない」へ症状が移る**（issue #132 R2）。
_SINK_STOP_BUDGET_S = 1.0

#: ファイルシンクのローテーション（旧 ``rotation="5 MB"`` / ``retention=5``
#: 相当）。loguru の ``FileSink`` を使わなくなったので自前で持つ。
_LOG_ROTATION_BYTES = 5 * 1024 * 1024
_LOG_RETENTION = 5

#: How many log files from *other* (typically dead) processes are kept when
#: :func:`setup_logging` prunes the log directory.  Files belonging to the
#: current process are never pruned, and files still held open by a live
#: process simply fail to unlink on Windows and are skipped (best effort).
_KEEP_OTHER_PROCESS_LOGS = 10


def _per_process_target(base: Path) -> Path:
    """Derive this process's log file name from the shared *base* name.

    ``viewer.log`` → ``viewer_<pid>.log``.  Multiple simultaneously running
    instances must not share one rotating sink: loguru's rotation renames the
    file, and on Windows renaming a file another process holds open fails
    (sharing violation), so two instances racing the 5 MB rotation would
    lose the rotation or the sink.  A per-process file sidesteps the race
    entirely while staying inside the portable app tree.
    """
    return base.with_name(f"{base.stem}_{os.getpid()}{base.suffix}")


def _prune_stale_logs(base: Path, keep: int = _KEEP_OTHER_PROCESS_LOGS) -> None:
    """Best-effort cleanup of per-process log files from previous runs.

    loguru's ``retention`` only manages rotated copies of the *current*
    sink's file, so files named for dead PIDs would otherwise accumulate
    forever.  Deletes the oldest files matching ``<stem>_<pid>…<suffix>``
    beyond *keep*, never touching the current process's own files.  Unlink
    failures are ignored — on Windows the file a live instance is *currently
    writing to* cannot be deleted.  That protection covers exactly one file
    per live instance: the rotated copies ``<stem>_<pid>.1…5<suffix>`` that
    :class:`_RotatingFile` leaves behind are held open by nobody, so a busy
    concurrent instance can still have its older copies pruned here.
    """
    directory = base.parent
    if not directory.is_dir():
        return
    # Match "<stem>_<digits>." so unrelated files that merely share the stem
    # prefix (e.g. viewer_freeze_trace.log) are never considered.
    pattern = re.compile(rf"^{re.escape(base.stem)}_\d+\.")
    own_prefix = f"{base.stem}_{os.getpid()}."
    try:
        candidates = sorted(
            (
                p
                for p in directory.iterdir()
                if p.is_file()
                and pattern.match(p.name)
                and not p.name.startswith(own_prefix)
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in candidates[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


class _RotatingFile:
    """サイズローテーション付きの追記ファイル（ライタースレッド専用）.

    loguru の ``FileSink``（``rotation`` / ``retention`` kwarg）の代わり。
    ``FileSink`` はパス指定の ``logger.add`` でしか作られず、そのときの
    「呼び出しスレッドを塞がない」手段は ``enqueue=True`` = パイプしか無い
    （＝モジュール docstring の 9 レコード問題）。ここは
    :class:`_BoundedSink` のライタースレッドからしか呼ばれないので、
    **塞いでよい**（塞いでも困らないのが要点）。

    命名は ``viewer_<pid>.log`` → ``viewer_<pid>.1.log`` … ``.5.log``。
    ``_prune_stale_logs`` の正規表現（``^<stem>_\\d+\\.``）にも、自分の
    プロセスを除外する接頭辞（``<stem>_<pid>.``）にも従来どおり一致する。

    **ローテーションの事後条件（issue #140 M-1 / レビュー #145）**: *退避段
    （現行ログ・``.1`` をどかす 2 段）のどちらかが失敗したら、既存の世代は
    1 つも消えず、次の再試行まで後退する*。:meth:`_rotate` の節を参照。
    """

    def __init__(
        self,
        path: Path,
        *,
        rotation_bytes: int = _LOG_ROTATION_BYTES,
        retention: int = _LOG_RETENTION,
    ) -> None:
        self._path = path
        self._rotation_bytes = rotation_bytes
        self._retention = retention
        self._stream = None
        # 次にローテーションを試みるサイズ。成功すれば ``rotation_bytes`` へ
        # 戻り、失敗したら「さらに 1 ローテーション分育つまで再試行しない」
        # （issue #140 M-1）。現行ログを掴まれ続けている間、毎バッチで
        # close + reopen + replace を撃ち続けるのを避けるための素朴な後退。
        self._rotate_at_bytes = rotation_bytes

    def probe(self) -> None:
        """保存先を一度 append open して閉じる（開けなければ送出する）.

        実書き込みは :meth:`write_lines` がライタースレッド上で遅延 open する
        ため、open が最初から失敗する配布（書込不可のボリュームへ丸ごとコピー
        された・ログ名が掴まれている 等）では :class:`_BoundedSink` が例外を
        握って ``dropped_error`` に積むだけになり、**誰にも届かない**
        （windowed 凍結ビルドは ``sys.stderr`` が無く ``_report_death`` も
        no-op）。:func:`setup_logging` は呼び出し元の起動ガード
        （``viewer/app.py`` の ``except OSError`` → 案内モーダル・項目#79）の
        内側でこれを呼び、「フォルダは作れるがファイルは開けない」構成を
        起動時に可視化する（レビュー 2026-09-03 項目 #113）。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.open("a", encoding="utf-8", errors="backslashreplace").close()

    def write_lines(self, lines: list[str]) -> None:
        stream = self._stream
        if stream is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            stream = self._path.open("a", encoding="utf-8", errors="backslashreplace")
            self._stream = stream
        stream.writelines(lines)
        stream.flush()
        size = stream.tell()
        if self._rotation_bytes > 0 and size >= self._rotate_at_bytes:
            self._rotate(size)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def _rotate(self, size: int) -> None:
        """現行ログを ``.1`` へ退避し、既存世代を 1 つずつ後ろへずらす.

        **順序が肝**（issue #140 M-1）。かつては「最古世代 unlink → 世代シフト
        → 最後に本命 ``os.replace(current → .1)``」の順で、しかも各段の失敗を
        握り潰していた。本命だけが失敗し続ける条件（Windows で AV / インデクサ
        が**現行ログ**を掴む — ローテーションのたびに開き直す現行ファイルこそ
        最も掴まれやすい）では、試行ごとに世代が 1 つずつ古い方から消え、
        5 回で全滅する。しかも現行ログは退避できないので無制限に育つ ＝
        「保険（過去ログ）を焼きながら、防ごうとした無制限成長も起きる」。

        そこで**破壊の前に「掴まれている可能性のあるファイル」を全部どかす**。
        掴まれ得るのは 2 つだけ — 現行ログと ``.1``（直前のローテーションが
        作った最新の過去ログ。AV / インデクサ / エディタが次に触るのはここ）。
        両方を退避先（``.rotating`` / ``.rotating1``）へ ``os.replace`` できた
        ときにだけ世代シフトへ進み、どちらかで失敗したら**既存世代に一切
        触れずに戻る**（事後条件: 世代は 1 つも消えない）。

        退避先を直接 ``.1`` にできないのは、``.1`` の中身が生きているうちは
        上書きできないため — 一旦 ``.rotating`` へ逃がし、``.1`` を空けてから
        改名する。**``.1`` を空ける操作そのものを破壊の前のゲートにする**のが
        第 2 段の要点（レビュー #145）。かつてはここに後退が無く、``.1`` を
        宛先に持つ ``os.replace`` だけが失敗する条件（＝上記の AV が最新の
        過去ログを掴む条件）で「シフトだけが毎回 1 段回り、末尾で
        ``_rotate_at_bytes`` がリセットされるので毎バッチ再突入する」となり、
        本命失敗と同じ世代全滅が再現していた（レビュー #145 で実測）。
        いまは ``.1`` が空いた後にしか破壊が始まらず、最後の
        ``os.replace(parking → .1)`` は宛先不在・ソースは誰も開いていない
        ファイルなので構造的にほぼ落ちない（それでも落ちたら後退する）。

        最古世代の明示 unlink は**しない**: ``os.replace`` は宛先を上書きする
        ので ``.<retention-1> → .<retention>`` が兼ねる。「退避段が失敗したら
        何も消えない」を構造的に成立させるため、破壊はシフトの中だけに閉じ
        込める。個々のシフト失敗を握るのは従来どおり（ローテーションが不完全
        でも書き込み自体は次の open で続く）。

        ``retention == 1``（``.1`` だけを保つ縮退）では ``.2`` は保持対象外
        なので、退避した旧 ``.1`` はシフトせず捨てる。
        """
        try:
            self.close()
        except OSError:
            self._stream = None
        parent, stem, suffix = self._path.parent, self._path.stem, self._path.suffix

        def _backup(index: int) -> Path:
            return parent / f"{stem}.{index}{suffix}"

        # 退避先も ``<stem>.…<suffix>`` 命名なので、``_prune_stale_logs`` の
        # 「自分のプロセスのものは消さない」接頭辞判定にそのまま乗る。
        parking = parent / f"{stem}.rotating{suffix}"
        parking1 = parent / f"{stem}.rotating1{suffix}"

        def _abort() -> None:
            """現行ログを元の位置へ戻し、次の再試行まで後退する.

            後退（``_rotate_at_bytes`` を押し上げる）が要る: これが無いと
            掴まれている間じゅう毎バッチで再突入し、成功した段（世代シフト）
            だけが回り続けて世代を焼き尽くす。
            """
            try:
                os.replace(parking, self._path)
            except OSError:
                pass
            self._rotate_at_bytes = size + self._rotation_bytes

        try:
            os.replace(self._path, parking)
        except OSError:
            # 現行ログを手放せない。**ここで戻る**のがこの関数の要点。
            self._rotate_at_bytes = size + self._rotation_bytes
            return
        first = _backup(1)
        # ``.1`` を空けられるか＝破壊を始めてよいかの判定。空けられないなら
        # まだ何も壊していないので、そのまま後退して戻る。
        parked_first = False
        if first.exists():
            try:
                os.replace(first, parking1)
            except OSError:
                _abort()
                return
            parked_first = True
        # ここから先が破壊フェーズ（``.2`` 以降のシフト）。``.1`` は上で
        # 空けたので、シフトの範囲は ``.2 → .3 …`` だけ。
        for index in range(self._retention - 1, 1, -1):
            try:
                os.replace(_backup(index), _backup(index + 1))
            except OSError:
                pass
        if parked_first:
            if self._retention >= 2:
                try:
                    os.replace(parking1, _backup(2))
                except OSError:
                    pass
            else:
                # retention == 1: ``.2`` は保持対象外なので旧 ``.1`` は捨てる。
                try:
                    parking1.unlink()
                except OSError:
                    pass
        # 宛先 ``.1`` は空、ソースはたった今この関数が作った誰も開いていない
        # ファイル。それでも失敗したら現行位置へ戻し、ログ本体を人質に
        # 取らない（後退も忘れずに — 上のシフトは既に 1 段回っている）。
        try:
            os.replace(parking, first)
        except OSError:
            _abort()
            return
        self._rotate_at_bytes = self._rotation_bytes


class _StreamTarget:
    """``sys.stderr`` 等の既存ストリームへ書く（ライタースレッド専用）."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write_lines(self, lines: list[str]) -> None:
        self._stream.writelines(lines)
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        # 借り物のストリームなので閉じない（stderr を閉じると以後の
        # トレースバックまで消える）。
        return None


class _BoundedSink:
    """**絶対に呼び出しスレッドを塞がない** loguru シンク（issue #132 R4）.

    loguru の「ストリームらしきもの」デュックタイプ（``write`` / ``flush`` /
    ``stop``）を実装するので、``logger.add(self, enqueue=False)`` で
    ``StreamSink`` として登録される。``StreamSink`` は ``stop`` を持つ
    オブジェクトに対して ``logger.remove()`` / loguru の
    ``atexit.register(logger.remove)`` から :meth:`stop` を呼ぶ — つまり
    **終了経路の有界化がこのクラスの中で閉じる**（呼び出し側が「予算超過を
    検知したときだけ有界化する」特別な後始末を呼ぶ必要が無い）。

    構造:

    * :meth:`write` — 呼び出しスレッド（GUI スレッドを含む）。有界 ``deque``
      への ``append`` と ``notify`` だけ。満杯なら**最も古い行を捨てて**
      カウントする。ロックを握る時間はマイクロ秒オーダーで、I/O は一切
      しない。loguru は ``Handler.emit`` の中でハンドラロックを握ったまま
      ここを呼ぶので、**ここが塞ぐと以後そのシンクへの全 emit が道連れ**に
      なる — だから塞がないことがこのクラスの唯一の存在理由。
    * ライタースレッド — バッファを丸ごと引き取って ``writer.write_lines``。
      死んだ共有ではここが何十秒でも張り付いてよい（デーモンなので
      プロセス終了も止めない）。

    捨てた行数は、書き込みが再開できたときの 1 行目に記録するので欠落は
    無言にならない。**捨てるのは古い行**（``deque(maxlen=…)``）— 直近の
    レコードほど「なぜ落ちたか」に近く、終了時の警告もそこに含まれるため。

    欠落は **2 つの原因**で起きるので、件数も通知文も原因ごとに分ける
    （R4 レビュー H-2 / L-2）:

    * ``_dropped_full`` — バッファが満杯で :meth:`write` が押し出した行。
      「保存先が遅い / 応答しない」の症状。
    * ``_dropped_error`` — ライタースレッドが引き取った後に
      ``write_lines`` が例外を投げて失った行。**バッファは満杯ではない**
      ので、満杯だと書く通知は原因の切り分けを誤らせる。

    どちらの経路でも「引き取ったが書けなかったバッチ長」を件数へ**戻す**
    こと。戻し忘れると、まさに「もう保存できない」状況でだけ件数が
    実際の欠落から乖離し、無言より悪い（積極的に誤った数字を残す）。
    """

    def __init__(
        self,
        writer,
        *,
        name: str,
        capacity: int = _SINK_BUFFER_RECORDS,
        stop_budget_s: float = _SINK_STOP_BUDGET_S,
    ) -> None:
        self._writer = writer
        # loguru が ``logger.add`` のときに ``getattr(sink, "name", None)`` を
        # 見る（エラーメッセージ用）。
        self.name = name
        self.encoding = "utf-8"
        self._capacity = max(1, capacity)
        self._stop_budget_s = stop_budget_s
        self._buffer: deque[str] = deque(maxlen=self._capacity)
        self._cond = threading.Condition()
        self._dropped_full = 0
        self._dropped_error = 0
        self._stopping = False
        self._dead = False
        self._thread = threading.Thread(
            target=self._run, name=f"log-writer[{name}]", daemon=True,
        )
        self._thread.start()

    # -- loguru の StreamSink デュックタイプ（呼び出しスレッド側）-----------

    def write(self, message) -> None:
        text = str(message)
        with self._cond:
            if self._stopping or self._dead:
                # ライターが死んだ後に積んでも誰も読まない（2048 行を永久に
                # 掴んだままになるだけ）。停止は :meth:`_report_death` が
                # stderr へ 1 回残しているので無言ではない。
                return
            if len(self._buffer) == self._capacity:
                self._dropped_full += 1
            self._buffer.append(text)
            self._cond.notify()

    def flush(self) -> None:
        # ``StreamSink.write`` が毎回呼ぶ。ここでライターを待つと「塞がない」
        # という唯一の契約が壊れるので、意図的に no-op。
        return None

    def stop(self) -> None:
        """``logger.remove()`` / loguru の atexit から呼ばれる（**有界**）."""
        with self._cond:
            self._stopping = True
            self._cond.notify()
        self._thread.join(self._stop_budget_s)

    # -- ライタースレッド側 ------------------------------------------------

    def _run(self) -> None:
        # ループ本体ごと ``BaseException`` で包む（R4 レビュー L-1）。ここが
        # 死ぬと以後の ``append`` は誰にも読まれず、``stop()`` の join は即
        # 返り ``writer.close()`` も呼ばれない（＝全ログが無言で消え、ファイル
        # ハンドルも漏れる）。``Exception`` しか捕まえない ``_write_batch``
        # では素通りする種類（``MemoryError`` / ``SystemExit`` / 保存先が
        # 投げる非 ``Exception``）が対象。
        try:
            self._pump()
        except BaseException:   # スレッドの最後の砦
            with self._cond:
                self._dead = True
            try:
                self._writer.close()
            except BaseException:  # pragma: no cover (defensive)
                pass
            # 通知は**死にゆくライタースレッドから**出す（呼び出しスレッド
            # からではなく）。``write`` の中で stderr へ書くと、詰まった
            # コンソールで GUI スレッドを塞ぐ = このクラスの唯一の契約を
            # 破る。デーモンスレッドがもう 1 行書くほうが安全側。
            self._report_death()

    def _pump(self) -> None:
        while True:
            with self._cond:
                while not self._buffer and not self._stopping:
                    self._cond.wait()
                batch = list(self._buffer)
                self._buffer.clear()
                dropped_full, self._dropped_full = self._dropped_full, 0
                dropped_error, self._dropped_error = self._dropped_error, 0
                stopping = self._stopping
            if batch or dropped_full or dropped_error:
                # 引き取る行が無くても**件数だけは書く**: 保存先が復帰した
                # 直後に ``stop`` が来ると（終了時の典型）、通知を運ぶ後続の
                # バッチが二度と来ないため。
                self._write_batch(batch, dropped_full, dropped_error)
            if stopping:
                try:
                    self._writer.close()
                except Exception:  # pragma: no cover (defensive)
                    pass
                return

    def _drop_notice(self, dropped_full: int, dropped_error: int) -> str:
        """欠落通知の 1 行。**原因を取り違えない**こと（R4 レビュー L-2）.

        件数は行頭の合計で 1 度だけ書く。原因が 1 つしか無いときに括弧内へも
        件数を入れると「17 行のログを捨てました（… 満杯になりました
        （17 行））」と同じ数字を 2 度書くことになる（レビュー L-5）。
        **内訳の件数は原因が 2 つあるときだけ**意味を持つ。
        """
        if dropped_full and dropped_error:
            cause = (
                f"保存先への書き込みが失敗し {dropped_error} 行、"
                f"シンクのバッファ {self._capacity} 行が満杯で"
                f" {dropped_full} 行"
            )
        elif dropped_error:
            cause = "保存先への書き込みが失敗しました"
        else:
            cause = (
                f"保存先が応答せず、シンクのバッファ {self._capacity} 行が"
                "満杯になりました"
            )
        total = dropped_full + dropped_error
        return f"... {total} 行のログを捨てました（{cause}）\n"

    def _write_batch(
        self, batch: list[str], dropped_full: int, dropped_error: int,
    ) -> None:
        held = len(batch)
        if dropped_full or dropped_error:
            batch.insert(0, self._drop_notice(dropped_full, dropped_error))
        try:
            self._writer.write_lines(batch)
        except Exception:
            # 保存先が壊れた / 消えた。次の周回で開き直しを試みる。**この
            # バッチの行も失われた**ので、持ち越し（``dropped_*``）に加えて
            # ``held`` も件数へ戻す — ここを戻し忘れると「500 行失って
            # 『8 行捨てました』」になる（R4 レビュー H-2）。
            try:
                self._writer.close()
            except Exception:  # pragma: no cover (defensive)
                pass
            with self._cond:
                self._dropped_full += dropped_full
                self._dropped_error += dropped_error + held

    def _report_death(self) -> None:
        """ライタースレッドが死んだことを stderr へ 1 回だけ（best-effort）."""
        stream = sys.stderr
        if stream is None:  # pragma: no cover (frozen windowed build)
            return
        try:
            stream.write(
                f"[snappix] ログのライタースレッド {self.name!r} が停止しました"
                "。以後このシンクへのログは保存されません。\n",
            )
            stream.flush()
        except BaseException:  # pragma: no cover (defensive)
            pass


_enqueue_guard_installed = False


def _install_enqueue_guard() -> None:
    """``logger.add(..., enqueue=True)`` を**プロセス全体で**無力化する.

    モジュール docstring の不変条件は「このプロセスで enqueue シンクを
    作るな」というプロセス全体の宣言なのに、機械検証は本体
    ``common/logging.py`` の AST ガード（``tests/`` 側）しか無かった —
    プラグインが足すシンクは誰も見ていない（R4 レビュー L-4）。公開
    リポジトリ分離の原則により、本体のテストからプラグインを検査すること
    はできないので、**実行時**に塞ぐ。

    落とすのではなく ``enqueue=False`` へ**倒して警告する**: 落とすと
    「ログ設定の 1 行でプラグインが起動できない」になり、症状が
    「閉じないビューア」より軽いとは限らない。倒せばプラグインはそのまま
    動き、失うのは（このプロセスでは使い道の無い）プロセス間キューだけ。

    限界: ``logger.bind(...).add(...)`` のように **別の ``Logger`` インスタンス
    経由**で足された場合は素通りする（``_core`` は共有だが ``add`` は
    クラス側のメソッド）。現実のシンク登録はモジュールレベルの ``logger``
    から行われるので、これで実用上の穴は塞がる。
    """
    global _enqueue_guard_installed
    if _enqueue_guard_installed:
        return
    original_add = logger.add

    def _guarded_add(sink, **kwargs):
        if kwargs.get("enqueue"):
            kwargs["enqueue"] = False
            # 既定 8KB のパイプは実測 9 レコードで埋まり、以後 emit は
            # ハンドラロックを握ったまま永久ブロックする（issue #132）。
            logger.warning(
                "logger.add(enqueue=True) を enqueue=False へ倒しました"
                "（sink={}）。loguru の enqueue パイプは満杯になると emit が"
                "呼び出しスレッドごとブロックします。非ブロックが要るなら"
                " common.logging._BoundedSink を使ってください。",
                getattr(sink, "name", sink),
            )
        return original_add(sink, **kwargs)

    logger.add = _guarded_add  # type: ignore[method-assign]
    _enqueue_guard_installed = True


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> Path | None:
    """Configure loguru sinks.

    ``log_file`` lets callers pick the *base* name for the file sink (e.g.
    the viewer passes ``logs/viewer.log``); the actual file is a per-process
    variant of it (``viewer_<pid>.log``) so that several simultaneously
    running instances never share one rotating sink — see
    :func:`_per_process_target` for the Windows rotation race this avoids.
    Stale files from previous runs are pruned best-effort
    (:func:`_prune_stale_logs`).

    Behaviour worth knowing (this is a shared-layer API):

    * **First call wins.** A second call with a different ``level`` /
      ``log_file`` is a no-op (guarded by ``_configured``) — a debug line is
      logged so the ignored re-config is at least visible.  Set up logging
      once, at the entrypoint.
    * ``level`` applies to the **stderr** sink only; the file sink is always
      ``DEBUG`` so the on-disk log keeps full detail regardless of console
      verbosity.
    * The file lives under ``AppPaths`` (``data/logs/…``) by default, honouring
      the portability contract (no writes outside the app tree).
    * **両シンクとも** :class:`_BoundedSink` 越しに登録し、``enqueue`` は
      使わない（モジュール docstring の不変条件）。``enqueue=True`` は
      呼び出しスレッドを守るように見えて、パイプが埋まる 9 レコード目から
      **無条件に永久ブロック**する。ここに ``enqueue=True`` を戻さないこと
      （``tests/test_common_touch_logging.py`` のガードが赤くなる）。

    * **保存先が開けなければ ``OSError`` を送出する**（項目 #113）。フォルダ
      の作成可否だけでなくログファイルの append open まで確かめる
      (:meth:`_RotatingFile.probe`) ので、呼び出し元は「ログを一切残せない
      配置」をこの例外で検出できる（``viewer/app.py`` は ``except OSError``
      で案内モーダルへ倒す）。シンクの差し替えはこの確認の**後**に行うため、
      送出時は既存のロギング構成がそのまま残る。

    Returns the resolved per-process log file path, or ``None`` when the
    call was ignored because logging was already configured.
    """
    global _configured
    if _configured:
        logger.debug(
            "setup_logging ignored (already configured); "
            "requested level={} log_file={}",
            level,
            log_file,
        )
        return None
    # 不変条件をプロセス全体へ広げる実行時ガード（本体の AST ガードは
    # プラグインを見られない — :func:`_install_enqueue_guard` 参照）。
    _install_enqueue_guard()
    paths = get_paths()
    base = log_file if log_file is not None else paths.log_file
    target = _per_process_target(base)
    target.parent.mkdir(parents=True, exist_ok=True)
    _prune_stale_logs(base)
    # ログ**ファイル**まで開けることを、まだ既存シンクを外す前・かつ呼び出し元
    # の起動ガードの内側で確かめる（レビュー 2026-09-03 項目 #113）。フォルダの
    # mkdir は「既に存在すれば書込不可のボリュームでも成功する」ため、これが
    # 無いと open 不能な配布でも setup_logging が成功扱いになり、以後すべての
    # ログが _BoundedSink の中で黙って捨てられる。
    file_target = _RotatingFile(target)
    file_target.probe()
    logger.remove()
    if sys.stderr is not None:
        # 凍結ビューアの stderr は Windows のコンソール既定エンコーディング
        # (cp932) のことがあり、日本語パス等のログが文字化けする。UTF-8 へ
        # 再構成できるストリームなら固定する（reconfigure 非対応や None の
        # ストリームは黙ってスキップ — 副作用を最小化）。
        reconfigure = getattr(sys.stderr, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass
        logger.add(
            _BoundedSink(_StreamTarget(sys.stderr), name="<stderr>"),
            level=level,
            colorize=False,
            enqueue=False,
            # ファイルシンク / GUI シンクと同じ理由で変数値の展開を切る
            # （下の add を参照）。コンソール出力はそのままファイルへ
            # リダイレクトして不具合報告に添付されるので、ディスク側で
            # 根絶した値がこちらから出て行かないよう 3 シンクで揃える。
            backtrace=True,
            diagnose=False,
        )
    logger.add(
        _BoundedSink(file_target, name=str(target)),
        level="DEBUG",
        colorize=False,
        enqueue=False,
        # レビュー項目41 再指摘R2 (b): 例外ログの秘匿情報漏出をディスク側で
        # 根絶する。loguru 既定の ``diagnose=True`` は例外時に各スタック
        # フレームの**ローカル変数値**をトレースバックへ展開する — 投稿処理中
        # の例外なら、その場のローカル(署名付き CDN URL を含む添付リスト等)が
        # そのまま ``data/logs/`` へ平文で書き込まれてしまう。``diagnose=False``
        # で変数値展開を無効化し、この経路を塞ぐ。``backtrace`` は既定(True)の
        # まま残す — フレーム/行番号のスタックは診断に有用で、かつ変数の
        # **実行時値**を含まない(URL 等の秘匿値は変数値であり diagnose 側の
        # 責務)ので、秘匿とのトレードオフは diagnose を切れば足りる。
        backtrace=True,
        diagnose=False,
    )
    _configured = True
    return target


def add_gui_sink(
    emit: Callable[[str], None],
    level: str = "INFO",
    *,
    filter: Callable[[Record], bool] | None = None,  # noqa: A002 (loguru の kwarg 名を写す)
) -> int:
    """Attach a sink that forwards formatted messages to a GUI callable.

    The caller is responsible for thread-safety (e.g. pass a Qt Signal.emit
    so Qt marshals the call to the main thread via a queued connection).

    ``filter`` is loguru's per-record predicate (receives the record dict,
    returns whether to forward it).  A secondary window that only wants its
    own component's logs — e.g. a plugin's log pane, which should not mirror
    the whole process's records — passes a name-prefix filter so unrelated
    records (the viewer core, other plugins) stay out of it.

    Returns the loguru handler id so the caller can remove it on shutdown.
    """
    return logger.add(
        lambda msg: emit(msg.rstrip()),
        level=level,
        format="{time:HH:mm:ss} | {level: <7} | {message}",
        enqueue=False,
        filter=filter,
        # ファイルシンクと同じ理由で変数値の展開を切る（そちらの add を参照）。
        # GUI のログペインは画面に出るうえ選択コピーできるので、例外時に
        # フレームのローカル値（署名付き URL 等）が載ると漏出面はディスクより
        # 広い。``backtrace`` は既定のまま — 値を含まないスタックは診断に要る。
        backtrace=True,
        diagnose=False,
    )
