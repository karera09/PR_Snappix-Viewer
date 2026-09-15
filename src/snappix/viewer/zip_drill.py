"""ZIP drill-in pipeline for the viewer window.

Double-clicking a ZIP extracts it into a temp directory **under the
portable base** (``data/tmp`` — see :func:`_zip_temp_base`) on a background
thread (window-modal progress dialog, cooperative cancel) and re-roots the
viewer into the extracted tree; oversized archives fall back to the
central-directory preview.  Split out of ``main_window.py`` (#96) —
behaviour, dialog texts and the temp-dir cleanup contract are unchanged.

The window keeps ownership of the ``temp_dirs`` mapping (extracted dir →
original ZIP path): it needs it for window-title / history-entry labels and
sweeps it on ``closeEvent``.  This controller only registers / unregisters
entries while an extraction is in flight.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Literal, assert_never, cast

from loguru import logger
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from ..common.archive import extract_zip_to_dir
from ..common.i18n import t
from ..common.paths import get_paths
from ..common.ui import show_toast
from ._runnable import GuardedStream, StreamJob, StreamOutcome
from .cancel_token import CancelToken
from .view_prefs import get_zip_preview_size_limit

# The ZIP extraction worker uses the viewer's shared cooperative-cancel flag
# (same contract as the scan workers): the main thread flips it on cancel, the
# worker polls it between members.  Previously re-implemented here as a private
# ``_ZipCancelToken`` (#131); now unified on ``cancel_token.CancelToken``.

# Zip-bomb guard (#6): the drill-in gate checks only the *compressed* archive
# size against the preview limit, but a crafted ZIP can decompress to vastly
# more.  Cap the cumulative *extracted* bytes at a generous multiple of that
# limit — legitimate image/media archives compress to a small fraction of
# this ratio, so only degenerate archives hit it.  Overflow surfaces through
# the existing extraction-failure modal.
_EXTRACT_SIZE_RATIO = 100

# ZIP のサイズ probe 中に受理表示（進捗ダイアログ）を出すまでの遅延（ms）。
# 速い probe で一瞬ダイアログが点滅するのを避けつつ、「押したのに無反応」に
# 感じ始める前に受理を返す — グリッドの 「読み込み中…」 ヒント
# (``children_grid``, 450ms) と同種の閾値（UIレビュー 2026-08-28 N-96）。
_PROBE_ACK_DELAY_MS = 200


#: サイズ probe の結末（:class:`StreamOutcome` の kind）。
_StatKind = Literal["size", "error"]

#: 展開の結末（:class:`StreamOutcome` の kind）。
_ExtractKind = Literal["finished", "failed"]


def _stat_zip_size(zip_path: Path) -> StreamOutcome:
    """Size-probe the archive off the GUI thread (レビュー 2026-07-31 #62).

    The size gate below needs ``st_size`` before it can decide between
    drill-in and the central-directory preview, but a cold NAS ``stat()``
    costs a round-trip and a disconnected share blocks for the full SMB
    timeout — the single-click preview path already moved its probe into
    ``content.zip_view._read_zip_listing`` for exactly this reason, and the
    double-click path must not stay synchronous.
    """
    try:
        return StreamOutcome("size", zip_path.stat().st_size)
    except OSError as exc:
        return StreamOutcome("error", str(exc))


def _extract_zip(
    job: StreamJob,
    generation: int,
    zip_path: Path,
    dest: Path,
    cancel: CancelToken,
    max_total_bytes: int | None,
) -> StreamOutcome:
    """Extract a ZIP into a temp directory off the GUI thread.

    Delegates to :func:`snappix.common.archive.extract_zip_to_dir`, which
    handles cp932 member-name mojibake recovery, zip-slip guarding and
    collision-safe uniquification — identical semantics to the shared
    helper's auto-extraction.  結末は世代タグつきで運ぶので、追い越された展開
    （利用者が既に次へ進んだ）も自分のダイアログと temp を畳んで降りられる。

    キャンセルは 2 系統を OR で見る: *cancel* はこの展開 1 本ぶんの
    :class:`CancelToken`（進捗ダイアログの「キャンセル」・次のドリルインに
    追い越されたとき）、``job.cancel`` はストリーム全体（窓じまい）。前者で
    降りた展開は**結末として着地する**（中途半端な temp を捨てるのは着地側の
    仕事）が、後者で降りたものは着地しない — 受け手がもう居ないため。
    """
    try:
        result = extract_zip_to_dir(
            zip_path,
            dest,
            should_cancel=lambda: cancel.is_cancelled() or job.cancel.is_cancelled(),
            on_progress=lambda done, total: job.report((generation, done, total)),
            max_total_bytes=max_total_bytes,
        )
    except Exception as exc:
        return StreamOutcome("failed", (generation, str(exc)))
    # ``skipped`` (zip-slip 等で展開されなかったエントリ数) も転送する —
    # 欠落を「全件成功」に見せないため受信側が警告を出す（項目#181）。
    return StreamOutcome(
        "finished", (generation, not result.completed, result.skipped),
    )


def _zip_temp_base() -> str:
    """ZIP 展開先の親ディレクトリ（ポータブルベース配下）を返す.

    ``tempfile.mkdtemp()`` を ``dir=`` 無しで呼ぶと Windows では ``%TEMP%``
    ＝ ``%LOCALAPPDATA%\\Temp`` ＝ **ユーザーホーム配下**になる。CLAUDE.md の
    ポータビリティ要件（「実行時に %APPDATA% / レジストリ / ユーザーホームへ
    の書き込みは一切行いません」）に対する唯一の反例だった
    （レビュー 2026-09-03 項目#11）。``state._write_json_atomic`` の
    ``mkstemp(dir=str(path.parent), ...)`` と同じく、明示的にベース配下へ
    スコープする。

    展開物は close / cancel / 失敗で ``shutil.rmtree`` されるが、kill や
    電源断では残るので専用の ``data/tmp`` に集める（``data`` 直下に混ぜない
    ＝ 残骸をユーザーがフォルダごと消せる）。
    """
    base = get_paths().data / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


class _ExtractInFlight:
    """飛んでいる展開 1 本ぶんの実行中レコード（世代で引く）。

    追い越された展開も自分のダイアログ・temp・トークンを保ったまま最後まで
    着地できるように、コントローラは 1 スロットではなく世代キーの表で持つ。
    """

    __slots__ = ("token", "dialog", "temp_dir", "zip_path")

    def __init__(
        self,
        token: CancelToken,
        dialog: QProgressDialog,
        temp_dir: Path,
        zip_path: Path,
    ) -> None:
        self.token = token
        self.dialog = dialog
        self.temp_dir = temp_dir
        self.zip_path = zip_path


class ZipDrillController(QObject):
    """Owns the ZIP drill-in extraction worker lifecycle for the window.

    2 本の :class:`~._runnable.GuardedStream` を持つ:

    * ``_stat_stream`` — サイズ probe。投入の**追い越し**がそのまま「新しい
      ドリルインが古い probe を無効にする」なので、世代の比較はどこにも
      書かない。走る場所はプールの外（``submit_detached``）— 切断中の共有で
      返らない ``stat`` を待つ相手を作らないため。
    * ``_extract_stream`` — 展開。投入は :meth:`~._runnable.GuardedStream.submit_batch`
      （**先行を捨てない**）: 追い越された展開も自分のダイアログを畳み temp を
      捨てるところまで必ず着地させる必要があり、``submit`` の追い越しだと
      その着地がストリームの世代ガードで落ちてしまう。展開 1 本ごとの協調
      停止は :attr:`_inflight` のレコードが持つ :class:`CancelToken` が担う。

    Everything a landing needs — dialog, temp dir, source ZIP, cancel flag —
    lives in :attr:`_inflight` keyed by generation, so every extraction lands
    exactly once.  窓じまいで ``_extract_stream`` を降ろすと、以後の着地は
    ``bind`` の選別で捨てられ（破棄済みダイアログを触らない）、
    :meth:`_on_extract_stream_stopped` が表を空にする。
    """

    def __init__(
        self,
        window: QWidget,
        temp_dirs: dict[Path, Path],
        *,
        show_zip_preview: Callable[[Path], None],
        open_extracted_root: Callable[[Path], None],
        status_message: Callable[[str, int], None],
        parent: QObject | None = None,
    ) -> None:
        """``window`` parents the message boxes / progress dialog;
        ``temp_dirs`` is the window-owned "extracted dir → original ZIP"
        map; ``show_zip_preview`` renders the central-directory fallback;
        ``open_extracted_root`` re-roots the viewer (history push included);
        ``status_message(text, ms)`` posts a transient success notice (the
        host decides the surface — the viewer sends it to a toast, since a
        status-bar message would blank the permanent path label).
        """
        super().__init__(parent)
        self._window = window
        self._temp_dirs = temp_dirs
        self._show_zip_preview = show_zip_preview
        self._open_extracted_root = open_extracted_root
        self._status_message = status_message
        self._generation = 0
        # 飛んでいる展開（世代 → レコード）。``shutdown`` はここを回して全部
        # 止める。1 スレッドのプールなので実際に走るのは 1 本だが、追い越さ
        # れた展開も協調停止して着地するまで表に残る。コントローラ所有の
        # プールこそが ``shutdown`` の有界ドレインを可能にしている —
        # ``QThreadPool.globalInstance().waitForDone()`` では無関係なビューア
        # のタスクまで待つことになる。
        self._inflight: dict[int, _ExtractInFlight] = {}
        self._tearing_down = False
        # 受理表示のダイアログ（サイズ probe 中 — UIレビュー 2026-08-28 N-96）。
        # 1 枚を使い回す（作り捨てにしない — :meth:`_show_probe_dialog`）。
        self._probe_dialog: QProgressDialog | None = None
        #: 表示予約が生きている probe の世代（``None`` = 予約なし / 着地済み）。
        self._probe_generation: int | None = None
        #: 現行のサイズ probe が見ている ZIP（着地の選別はストリームがやるので、
        #: ここに残るのは必ず「いま着地しうる 1 本」の対象）。
        self._stat_path: Path | None = None
        self._stat_stream = GuardedStream(self)
        self._stat_stream.bind(self._on_stat_done)
        # probe が降りた（窓じまい / 受理ダイアログの「キャンセル」）ときは
        # 受理表示を必ず引っ込める — 着地しない probe のぶんを畳む唯一の口。
        self._stat_stream.bind_cancelled(self._dismiss_probe_dialog)
        self._extract_stream = GuardedStream(self)
        self._extract_stream.bind(self._on_extract_done)
        self._extract_stream.bind_progress(self._on_extract_progress)
        self._extract_stream.bind_cancelled(self._on_extract_stream_stopped)

    def open_zip_as_folder(self, zip_path: Path) -> None:
        """Extract a ZIP to a temp directory and treat it as the new root.

        The archive size is probed off the GUI thread (:func:`_stat_zip_size`,
        レビュー 2026-07-31 #62) and the flow continues in
        :meth:`_on_stat_done`; a cold / disconnected NAS would otherwise
        freeze the window on the ``stat()`` for the whole round-trip.

        Extraction then runs on a background thread (:func:`_extract_zip`)
        with a window-modal progress dialog — even small ZIPs can take
        seconds on NAS, and the member loop shares the archive helper's cp932
        name decoding and zip-slip guard instead of raw ``extractall``.
        Cancelling stops the worker cooperatively, deletes the partially
        extracted temp dir and stays on the current root.

        The extracted directory is tracked in the window's temp-dir map and
        removed on ``closeEvent``.  Archives over the ZIP preview size limit
        fall back to the central-directory preview — extracting them would
        waste disk and time for content that's likely too large to browse
        comfortably anyway.
        """
        if self._tearing_down:
            return
        # 展開の世代は**要求時点**で配る — 「新しいドリルインが起きた」
        # という事実そのものが先行の展開を superseded にする。probe の
        # 着地まで待って配ると、遅い共有上の 2 本目をダブルクリックした
        # 後で 1 本目の展開が着地すると ``generation == self._generation`` が
        # 成立し、捨てるはずの展開がルートを差し替えて履歴を積む。
        self._generation += 1
        # 新しい要求が来た時点で先行の展開を止める（レビュー 09-03
        # #194。``main_window._kick_rename_follow`` と同じ形）。追い越された
        # 展開は表に残したまま協調停止させる — 着地でダイアログを畳み
        # temp を捨てるのはその展開自身のレコードなので、取りこぼしが出ない。
        for record in self._inflight.values():
            record.token.cancel()
        # UIレビュー 2026-08-28 N-96: probe 投入から着地までは UI への通知が
        # 一切なく、cold / 切断中の NAS では数秒〜数十秒まるごと無反応だった
        # （:func:`_stat_zip_size` の docstring 自身がその所要時間を認めている）。
        # 受理表示を先に予約する（速い probe では一度も現れない）。
        self._stat_path = zip_path
        # **プールに載せない**（``submit_detached``）: 切断中の共有では 1 回の
        # ``stat`` が SMB タイムアウト（VM 実測 15〜195 秒）までブロックする。
        # ``QThreadPool`` のデストラクタは in-flight を無制限に待つので、載せる
        # と「窓は閉じたのにプロセスが終われない」へ症状が移るだけ — 放棄して
        # よい probe なので待たない場所で走らせる。世代・着地の選別はストリーム
        # が持つので、その点は他の投入形と同じ。
        generation = self._stat_stream.submit_detached(
            lambda _job, p=zip_path: _stat_zip_size(p), label="zip-size-probe",
        )
        self._arm_probe_dialog(zip_path, generation)

    # ----------------------------------------------------- probe acknowledge

    def _arm_probe_dialog(self, zip_path: Path, generation: int) -> None:
        """受理ダイアログを ``_PROBE_ACK_DELAY_MS`` 後に出す予約を入れる (N-96).

        グリッド側の 「読み込み中…」 ヒント（``children_grid`` の 450ms 単発
        タイマー）と同じ作法: 速く終わる probe では何も見せず、体感的に
        「無反応」になる長さを超えたときだけ受理を出す。窓モーダルなので
        その間の連打も届かなくなり、世代破棄に頼らずに済む。

        ``QProgressDialog`` 自前の ``minimumDuration`` タイマーには乗せない —
        あれは最初の ``setValue`` を起点に張られる仕組みで、不定バー
        （range 0,0）で ``setValue`` を呼ばない本ダイアログでは発火しない。
        表示条件を自分で持つほうが挙動が読める。
        """
        self._probe_generation = generation
        QTimer.singleShot(
            _PROBE_ACK_DELAY_MS,
            self,
            lambda: self._show_probe_dialog(zip_path, generation),
        )

    def _show_probe_dialog(self, zip_path: Path, generation: int) -> None:
        """予約が満了した — probe がまだ飛んでいれば受理ダイアログを出す。"""
        if self._tearing_down or generation != self._probe_generation:
            return  # 既に着地 / 上書きされた: 一瞬の点滅を出さない
        dialog = self._probe_dialog
        if dialog is None:
            # コントローラ 1 つにつき 1 枚だけ作って使い回す。``close()`` +
            # ``deleteLater()`` で作り捨てにすると、親ウィンドウが先に壊れた
            # ときに配送待ちの DeferredDelete が死んだオブジェクトへ届く
            # （オフスクリーン QPA でプロセスごと落ちる）。
            dialog = QProgressDialog(
                "", t("common.action.cancel"), 0, 0, self._window,
            )
            dialog.setWindowTitle(t("viewer.zip_drill.extract_dialog_title"))
            dialog.setWindowModality(Qt.WindowModal)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.setMinimumDuration(0)
            dialog.canceled.connect(self._on_probe_cancelled)
            self._probe_dialog = dialog
        dialog.setLabelText(
            t("viewer.zip_drill.probing_progress", name=zip_path.name)
        )
        dialog.show()

    def _dismiss_probe_dialog(self) -> None:
        """受理ダイアログを引っ込める（プログラム都合＝「中止」ではない）。

        ``hide()`` を使うのが要点: ``QProgressDialog`` は ``closeEvent`` から
        ``canceled`` を出すので、``close()`` だと probe の正常着地が利用者の
        中止として跳ね返ってくる。
        """
        self._probe_generation = None
        dialog = self._probe_dialog
        if dialog is None:
            return
        try:
            dialog.hide()
        except RuntimeError:  # pragma: no cover (defensive — 破棄済み)
            self._probe_dialog = None

    def _on_probe_cancelled(self) -> None:
        """受理ダイアログの「キャンセル」— probe を降ろして結果を捨てる。"""
        if self._probe_generation is None:
            return  # 既に着地済み（``hide()`` 経由では発火しない）
        # ``cancelled`` → :meth:`_dismiss_probe_dialog` が予約も表示も畳む。
        self._stat_stream.cancel()

    def _on_stat_done(self, payload: object) -> None:
        """Size probe landed — gate on it and start the extraction.

        追い越し（新しいドリルイン）と窓じまいの選別は ``bind`` が済ませて
        いるので、ここへ来るのは現行の probe の着地だけ。
        """
        if not isinstance(payload, StreamOutcome):
            return  # the worker raised
        # 受理ダイアログは全分岐で必ず引っ込める（サイズ超過の案内モーダル /
        # ``mkdtemp`` 失敗の警告より前 — N-96 の実装注意 ①②）。
        self._dismiss_probe_dialog()
        zip_path = self._stat_path
        if zip_path is None:  # pragma: no cover (defensive)
            return
        kind = cast(_StatKind, payload.kind)
        match kind:
            case "size":
                size = int(cast(int, payload.value))
            case "error":
                QMessageBox.warning(
                    self._window, t("viewer.zip_drill.cannot_open_zip"),
                    cast(str, payload.value),
                )
                return
            case _:
                assert_never(kind)
        limit = get_zip_preview_size_limit()
        if size > limit:
            QMessageBox.information(
                self._window,
                t("viewer.zip_drill.zip_size_limit_title"),
                t(
                    "viewer.zip_drill.zip_size_limit_body",
                    size_mb=size / (1024 * 1024),
                    limit_mb=limit // (1024 * 1024),
                ),
            )
            self._show_zip_preview(zip_path)
            return

        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="snappix-viewer-zip-", dir=_zip_temp_base()))
        except OSError as exc:
            QMessageBox.warning(
                self._window, t("viewer.zip_drill.cannot_create_temp"), str(exc)
            )
            return

        # 世代は :meth:`open_zip_as_folder`（要求時点）で既に進んでいる。
        generation = self._generation
        token = CancelToken()

        # Register up front so an app close during extraction still sweeps
        # the directory in ``closeEvent``; removed again on cancel/failure.
        self._temp_dirs[temp_dir] = zip_path

        dialog = QProgressDialog(
            t("viewer.zip_drill.extracting_progress", name=zip_path.name),
            t("common.action.cancel"),
            0,
            0,
            self._window,
        )
        dialog.setWindowTitle(t("viewer.zip_drill.extract_dialog_title"))
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(200)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        # Delete on close so each drill-in's dialog is reclaimed instead of
        # lingering as a child of the window (they'd otherwise pile up for the
        # whole session, held alive by the parent + the closure below).
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._inflight[generation] = _ExtractInFlight(
            token, dialog, temp_dir, zip_path
        )
        # 「キャンセル」はトークンを直接ではなくコントローラ経由で引く。
        # ``dialog.close()`` は ``closeEvent`` から ``canceled`` を出すので、
        # 着地でレコードを外した後の close が死んだトークンへ届かない。
        dialog.canceled.connect(lambda: self._cancel_extraction(generation))

        # **``submit_batch``**（先行を捨てない）: 追い越された展開も自分の
        # ダイアログ・temp を畳むところまで着地させる必要がある。``submit``
        # にすると世代が進み、その着地がストリームのガードで落ちる。
        self._extract_stream.submit_batch(
            lambda job, g=generation, p=zip_path, d=temp_dir, tok=token,
            cap=limit * _EXTRACT_SIZE_RATIO: _extract_zip(job, g, p, d, tok, cap)
        )

    # -------------------------------------------------------- extract landing

    def _cancel_extraction(self, generation: int) -> None:
        """進捗ダイアログの「キャンセル」— その世代の展開だけを止める。"""
        record = self._inflight.get(generation)
        if record is not None:
            record.token.cancel()

    def _discard_temp_dir(self, temp_dir: Path) -> None:
        # Safe to rmtree here: the worker only signals after it has stopped
        # writing (cooperative cancel between members).
        self._temp_dirs.pop(temp_dir, None)
        shutil.rmtree(temp_dir, ignore_errors=True)

    # ダイアログは ``WA_DeleteOnClose`` で窓の子なので、窓が壊れた後に着地する
    # と死んだ C++ オブジェクトへ ``close()`` を撃つ。その選別は ``bind`` が
    # 担う: :meth:`shutdown` がストリームを降ろした時点で ``accepts`` が偽に
    # なり、遅れて着地した展開はここへ来ない（レビュー 2026-07-31 #81 が
    # ``_tearing_down`` で手当てしていた穴 — 世代タグでは塞げない、teardown が
    # 止めようとしているタスク自身の世代は一致してしまうため）。

    def _on_extract_stream_stopped(self) -> None:
        """展開ストリームが降りた（窓じまい）— 実行中レコードの表を空にする。

        冪等（:meth:`~._runnable.GuardedStream.bind_cancelled` の契約）。
        temp の削除はここでは行わない — 窓の ``closeEvent`` が
        ``_zip_temp_dirs`` を掃引する順序契約のほうが正本で、中途半端な
        クリーンアップを二重に持たない。
        """
        self._inflight.clear()

    def _on_extract_progress(self, payload: object) -> None:
        if not isinstance(payload, tuple):  # pragma: no cover (defensive)
            return
        generation, done, total = payload
        record = self._inflight.get(generation)
        if record is None:
            return
        if record.dialog.maximum() != total:
            record.dialog.setMaximum(total)
        record.dialog.setValue(done)

    def _on_extract_done(self, payload: object) -> None:
        """展開 1 本の結末（完了 / 失敗）を捌く — 世代はペイロードが運ぶ。"""
        if not isinstance(payload, StreamOutcome):
            return  # the worker raised
        kind = cast(_ExtractKind, payload.kind)
        match kind:
            case "finished":
                generation, cancelled, skipped = cast(
                    "tuple[int, bool, int]", payload.value,
                )
                self._on_extract_finished(generation, cancelled, skipped)
            case "failed":
                generation, message = cast(
                    "tuple[int, str]", payload.value,
                )
                self._on_extract_failed(generation, message)
            case _:
                assert_never(kind)

    def _on_extract_finished(
        self, generation: int, cancelled: bool, skipped: int
    ) -> None:
        record = self._inflight.pop(generation, None)
        if record is None:
            return  # 既に着地済み（shutdown で表ごと捨てられた等）
        # ダイアログは世代に関わらず畳む — 畳まないと追い越された展開の
        # 進捗ダイアログが開きっぱなしで残る。
        record.dialog.close()
        if cancelled or generation != self._generation:
            # キャンセル、または新しいドリルインに追い越された展開:
            # temp を捨てて**ルートには触れない**（superseded な展開が
            # ``_open_extracted_root`` で今のルートを差し替えない）。
            self._discard_temp_dir(record.temp_dir)
            return
        self._status_message(
            t("viewer.zip_drill.extracted_status", name=record.zip_path.name),
            3000,
        )
        self._open_extracted_root(record.temp_dir)
        if skipped > 0:
            # 展開されなかったエントリがある（zip-slip / 不正な名前 /
            # 親パスがファイル）— 欠けたフォルダを「全件成功」として
            # 提示しない（項目#181。設計原則: 打ち切りを「全件」に
            # 見せない）。詳細はログに warning 済み。
            show_toast(
                self._window,
                t("viewer.zip_drill.extract_partial_toast", count=skipped),
                kind="warning",
            )

    def _on_extract_failed(self, generation: int, message: str) -> None:
        record = self._inflight.pop(generation, None)
        if record is None:
            return
        record.dialog.close()
        self._discard_temp_dir(record.temp_dir)
        logger.warning("Failed to extract {}: {}", record.zip_path, message)
        if generation != self._generation:
            return  # superseded: 黙って片付ける（新しい操作を遮らない）
        QMessageBox.warning(
            self._window,
            t("viewer.zip_drill.extract_error_title"),
            t(
                "viewer.zip_drill.extract_error_body",
                name=record.zip_path.name,
                message=message,
            ),
        )

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop an in-flight extraction — call BEFORE sweeping the temp dirs.

        ``ViewerWindow.closeEvent`` rmtree's every registered temp dir, but
        the extraction worker used to survive that sweep: it kept writing
        members into (and re-creating) the directory it was just handed,
        leaving a ``snappix-viewer-zip-*`` tree behind in ``data/tmp`` and
        breaking content.md's "temp is always rmtree'd on closeEvent"
        invariant.  Cancelling cooperatively and draining the
        controller-owned pool means the worker has provably stopped writing
        by the time the sweep runs (レビュー 2026-07-31 #81).

        The wait is bounded: a member being written to a stalled NAS share
        must not hang the close.  In that (rare) case the sweep still runs
        and the OS reclaims whatever the worker recreates afterwards.
        """
        self._tearing_down = True  # 新規要求を受け付けない（着地の選別は bind）
        # 走っている probe を降ろす（``cancelled`` が受理ダイアログを畳む）。
        self._stat_stream.cancel()
        for record in self._inflight.values():
            record.token.cancel()
        # ``request_shutdown`` = キュー破棄 + セッション cancel + 有界待ち。
        # ``cancelled`` が :meth:`_on_extract_stream_stopped` を呼んで表を空に
        # し、以後の遅い着地は ``bind`` の選別で捨てられる。
        self._extract_stream.request_shutdown(max(0, timeout_ms))


__all__ = ["ZipDrillController"]
