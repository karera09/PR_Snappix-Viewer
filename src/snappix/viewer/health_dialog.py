"""Modeless 「ライブラリ健全性チェック」 dialog for the viewer.

Runs :mod:`snappix.viewer.health_check` over the current root (or a chosen
subtree) off the GUI thread, then groups the detected problems by category in
a tree.  Per-row action: 「エクスプローラで開く」.  Bulk actions, each enabled and
labelled by its live count: 「.part をすべて削除」 / 「0 バイトファイルをすべて
削除」 / 「空フォルダをすべて削除」 (destructive — confirm modal, defaulting to
No, first).  The scan target can be re-pointed from inside the dialog
(「対象フォルダを選択…」 → :meth:`rescan`).

J02: the dialog is **modeless** (a single instance reused via ``show()`` /
:meth:`rescan`, like ``ShortcutsDialog``) — the multi-minute scan of a large
NAS tree runs off-thread with the main window still interactive.  The
destructive bulk-delete confirmation stays a blocking modal, and the window's
``closeEvent`` calls ``close()`` here so the in-flight scan is cancelled.

Design-system rules apply throughout: themed widgets + tokens only, SVG icons
from ``common/ui/icons`` (no emoji), success feedback via ``show_toast``,
destructive actions gated behind a confirm modal (docs/claude/design.md).

走査も一括削除も :class:`~snappix.viewer._runnable.GuardedStream` の
``submit_detached``（**プール外**のデーモンスレッド）で走る: 分オーダーの
NAS 走査を ``QThreadPool`` へ載せると、デストラクタが in-flight を無制限に
待つぶん「窓は閉じたのにプロセスが残る」へ症状が移るだけだから。世代・着地の
選別・協調キャンセル・進捗はストリームが持つ。
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal, assert_never, cast

from loguru import logger
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.format import format_bytes
from ..common.i18n import t
from ..common.paths import get_paths
from ..common.ui import (
    align_header,
    confirm_action,
    demote_all_defaults,
    hint_style,
    icon,
    show_toast,
)
from .cancel_token import CancelToken
from .dialogs import host_picker_places, pick_existing_directory
from .health_check import (
    CATEGORY_EMPTY_FOLDER,
    CATEGORY_MISSING_POST_MD,
    CATEGORY_ORDER,
    CATEGORY_PART,
    CATEGORY_ZERO_BYTE,
    HealthIssue,
    HealthReport,
    PostRef,
    category_explanation,
    category_label,
    deletable_paths,
    dir_has_any_file,
    paths_for_category,
    run_health_check,
)
from .path_probe import probe_path_kind
from ._runnable import GuardedStream, StreamJob, StreamOutcome

#: Progress callback is throttled to this stride so a fast local scan doesn't
#: flood the signal queue (one emit per N folders).
_PROGRESS_STRIDE = 25

#: Append-only record of every path the bulk delete removed, under
#: ``data/logs/`` (UIレビュー 2026-08-28 N-40 — see :func:`log_deleted_paths`).
DELETION_LOG_NAME = "health_deletions.log"

#: Size above which the deletion log is rotated to ``….log.1`` (項目#256).
#: Same house policy as ``common/logging.py`` の ``_LOG_ROTATION_BYTES``.
_DELETION_LOG_ROTATION_BYTES = 5 * 1024 * 1024

#: Lifetime of the completion toast when it carries the 「ログフォルダを開く」
#: button.  The default 3 s is enough to *read* a result but not to notice a
#: button, decide, and hit it — an actionable toast has to outlive the glance.
_ACTION_TOAST_MS = 10_000

#: Role storing the ``HealthIssue`` on a tree row (folder/file rows only).
_ISSUE_ROLE = Qt.UserRole

#: Max rows rendered per category.  A 10 万フォルダ級 NAS ライブラリでは
#: missing_post_md / zero_byte が数万件になり、全件を GUI スレッドで
#: ``QTreeWidgetItem`` 化すると（せっかく走査を off-thread 化したのに）結果
#: 描画で数秒フリーズする。超過分は 1 行の「他 N 件」で畳む — 一括削除は
#: ``report.issues`` 全件を対象にするので、これは**表示だけ**の上限。
_MAX_ROWS_PER_CATEGORY = 500


#: 走査の結末（:class:`StreamOutcome` の kind）。
_ScanKind = Literal["report", "failed"]


def _run_scan(job: StreamJob, root: Path, token: CancelToken) -> StreamOutcome:
    """Run :func:`run_health_check` off-thread with progress + cancel.

    キャンセルは 2 系統を OR で見る: *token* は利用者の「中止」（走査は降りる
    が**部分レポートは着地させる** — 「キャンセルしました (N)」を出すため）、
    ``job.cancel`` はストリーム（ダイアログを閉じた / 再走査に追い越された）で、
    そちらで降りた走査は着地しない。
    """
    last = 0

    def _progress(n: int) -> None:
        nonlocal last
        if n - last >= _PROGRESS_STRIDE:
            last = n
            job.report(n)

    try:
        report = run_health_check(
            root,
            progress=_progress,
            is_cancelled=lambda: token.is_cancelled() or job.cancel.is_cancelled(),
        )
    except Exception as exc:  # noqa: BLE001 — report, never crash the walk
        logger.warning("health check scan failed: {}", exc)
        return StreamOutcome("failed", str(exc))
    return StreamOutcome("report", report)


def _run_bulk_delete(
    job: StreamJob,
    paths: list[Path],
    *,
    is_dir: bool,
    recheck_empty: bool,
    file_snapshots: dict[Path, tuple[int | None, float | None]] | None,
    token: CancelToken,
    label: str,
) -> tuple[list[Path], list[tuple[Path, str]], int, Path | None]:
    """Run a bulk delete off the GUI thread with progress + cancel.

    ``shutil.rmtree`` / ``os.remove`` over hundreds of NAS paths used to run
    synchronously in the confirm handler — freezing the UI for the whole
    batch with no progress and no way out.  The empty-folder re-check
    (``dir_has_any_file``) is I/O too, so it also moved here: the report is a
    scan-time snapshot, and a folder that was empty then may have gained
    files since (the user dropped something in) — re-confirming right before
    ``rmtree`` guarantees live content is never wiped.  ``file_snapshots``
    is the same guarantee for every **file** category (0 バイト / ``.part``):
    it carries the ``(size, mtime)`` the scan captured per path, and
    :func:`_file_changed_since_scan` — which owns the whole policy, including
    how an unverifiable ``stat`` falls — decides skip-vs-delete from it.  One
    snapshot map rather than a per-category flag + a second map, so the two
    categories cannot drift apart.

    Cancel is cooperative (checked between paths); whatever was already
    deleted stays deleted — the rescan afterwards shows the true state.

    Progress is throttled with the same :data:`_PROGRESS_STRIDE` as the scan
    (レビュー 2026-08-27 #173): the receiving ``QProgressDialog`` is
    ``WindowModal``, and ``QProgressDialog::setValue`` spins
    ``processEvents()`` in that state — a per-path emit therefore ran a full
    GUI event-loop turn for **every** deleted path (数万件の .part 一括削除で
    off-thread 化の効果を進捗表示側で相殺し、その再入窓で ``finished`` が
    ``setValue`` の内側に配送され得た)。

    戻り値は ``(削除したパス, エラー, スキップ数, 削除ログのパス)``。削除した
    **パス**（件数ではない — N-40）を運ぶ。
    """
    deleted: list[Path] = []
    skipped = 0
    last = 0
    errors: list[tuple[Path, str]] = []
    for i, path in enumerate(paths):
        if token.is_cancelled() or job.cancel.is_cancelled():
            break
        if recheck_empty and dir_has_any_file(path):
            skipped += 1
        elif file_snapshots is not None and _file_changed_since_scan(
            path, file_snapshots.get(path),
        ):
            skipped += 1
        else:
            done_paths, errs = _delete_paths([path], is_dir=is_dir)
            deleted.extend(done_paths)
            errors.extend(errs)
        # 走査側と同じ間引き（進捗ダイアログは着地側で閉じるので、最後の
        # 端数を報告しそこねても表示は破綻しない）。
        done = i + 1
        if done - last >= _PROGRESS_STRIDE:
            last = done
            job.report(done)
    # 削除ログはこのワーカースレッドで書く（レビュー 2026-09-03 項目
    # #217）。1 パス 1 行の追記は数万件の一括削除で秒単位の同期 I/O に
    # なり、GUI スレッド側（``_on_delete_finished``）で書くと off-thread
    # 化した削除の効果を報告の瞬間に相殺していた。``log_deleted_paths``
    # は Qt 非依存で、``get_paths()`` はキャッシュ済みの読み出しなので
    # ワーカーから呼んで安全。**返す前**に書くのは、窓が閉じられて報告先が
    # 消えても記録は必ず残す、という N-40 の意図。
    log_path = log_deleted_paths(deleted, label=label)
    return (deleted, errors, skipped, log_path)


class _ElideMiddleDelegate(QStyledItemDelegate):
    """Column delegate that truncates with 「…」 in the **middle**.

    ``QAbstractItemView.setTextElideMode`` is view-wide, but only the path
    column wants a middle ellipsis (a middle-elided sentence is harder to read
    than a right-elided one).  Setting the option per column is the smallest
    way to have both in one tree (UIレビュー 08-28 N-23).

    Column 0 also carries **sentences**: the category head row (見出し + 説明,
    N-06) and the 「他 N 件」 row.  Those are told apart by the absence of
    :data:`_ISSUE_ROLE` — the same marker the selection / double-click
    handlers use — and fall back to a trailing ellipsis, because a
    middle-elided sentence loses its own subject.
    """

    def initStyleOption(self, option, index) -> None:  # noqa: N802 (Qt API)
        super().initStyleOption(option, index)
        option.textElideMode = (
            Qt.TextElideMode.ElideMiddle
            if index.data(_ISSUE_ROLE) is not None
            else Qt.TextElideMode.ElideRight
        )


class HealthCheckDialog(QDialog):
    """Modeless dialog scanning the library for integrity problems."""

    def __init__(
        self,
        root: Path,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.health_dialog.title"))
        self.resize(760, 560)
        # Modeless (J02): the caller show()s this and keeps browsing while the
        # off-thread scan runs.  Destructive confirms below stay modal.
        self.setModal(False)
        self._root = root
        self._report: HealthReport | None = None
        # 利用者の「中止」用トークン。走査を降ろすが**部分レポートは着地させる**
        # ので、ストリームのセッション（追い越し / 窓じまい = 着地ごと捨てる）
        # とは別に持つ。
        self._token: CancelToken | None = None

        # Parent the relays to self so a pooled worker mid-``emit`` can't be
        # GC'd during teardown (exit-139), matching image_view/markdown_view (#8).
        # (UIレビュー 07-25 #18) ライブラリ全体に post.md が 1 つでもあるか。
        # 判明するまで（＝走査中）は True＝抑制しない側に倒しておく。
        # 走査完了時に ``HealthReport.library_has_post_md``（走査自身の
        # post-order 畳み込みが計上する値 — 別 walker 無し）で更新される。
        self._has_any_post_md = True
        # 実行中の一括削除が対象にしたパス（完了後に #71 の再スキャン通知へ）。
        self._delete_targets: list[Path] = []

        # 走査 / 一括削除の 2 ストリーム。どちらも :meth:`~._runnable
        # .GuardedStream.submit_detached`（プール外のデーモンスレッド）で走る —
        # 放棄できる FS 仕事をプールへ載せると、``QThreadPool`` のデストラクタ
        # が in-flight を無制限に待つぶん「窓は閉じたのにプロセスが残る」へ
        # 症状が移るだけだから。世代・着地の選別・協調キャンセルは機構が持つ。
        self._scan_stream = GuardedStream(self)
        self._scan_stream.bind(self._on_scan_done)
        self._scan_stream.bind_progress(self._on_progress)
        self._delete_stream = GuardedStream(self)
        self._delete_stream.bind(self._on_delete_finished)
        self._delete_stream.bind_progress(self._on_delete_progress)
        self._delete_token: CancelToken | None = None
        self._delete_progress: QProgressDialog | None = None

        self._build_ui()
        # Kick the scan after the dialog is shown so the progress UI is live.
        self._start_scan()

    def done(self, result: int) -> None:  # noqa: D401 (Qt override)
        """Cancel in-flight background work before closing.

        Every close path (X button, Esc, the 閉じる button) routes through
        ``done`` — without this, ``run_health_check`` kept walking the whole
        tree (and a bulk delete kept grinding) after the dialog was gone.
        """
        if self._token is not None:
            self._token.cancel()
        if self._delete_token is not None:
            self._delete_token.cancel()
        # 走査は結果ごと捨ててよい（次に開くときは必ず走査し直す）ので
        # ストリームごと降ろす。**削除は降ろさない** — 着地は「可視性に
        # 関係なく必ず」行う後始末（``_notify_library_changed`` と
        # ``_delete_targets`` の解放）を持っており、ここで捨てると閉じかけの
        # 一括削除でグリッドが消えたフォルダを描き続ける
        # （:meth:`_apply_delete_result` の ``isVisible`` 分岐がその境目）。
        self._scan_stream.cancel()
        super().done(result)

    def rescan(self, root: Path) -> None:
        """Re-target *root* and restart the scan (modeless reopen, J02).

        The window reuses the single dialog instance across opens; when the
        current library root has changed (or the user just wants a fresh scan)
        this points the dialog at *root* and kicks a new off-thread walk,
        cancelling any still-running one via ``_start_scan``.
        """
        self._root = root
        self._path_label.setText(
            t("viewer.health_dialog.target", path=root)
        )
        self._start_scan()

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self._path_label = QLabel(
            t("viewer.health_dialog.target", path=self._root), self,
        )
        self._path_label.setStyleSheet(hint_style())
        self._path_label.setWordWrap(True)
        outer.addWidget(self._path_label)

        # Scanning row: progress bar + cancel.
        scan_row = QHBoxLayout()
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # indeterminate while scanning
        # (レビュー 2026-08-27 #72) 不確定モード（min == max）の QProgressBar は
        # ``text()`` が常に空 — バーへ書式を載せても走査済みフォルダ数は 1 文字も
        # 出ない（``setFormat`` も ``setValue`` も画面に現れない）。件数は隣の
        # ステータスラベルへ出し、バーは「動いている」ことだけを示す。
        self._progress.setTextVisible(False)
        scan_row.addWidget(self._progress, 1)
        # (UIレビュー 07-25 #117) 走査が終わったあとまでアクセント色で満杯の
        # バーを残すと「まだ動いている」ように読め、しかも満杯バーの上の
        # テキストはコントラストが落ちる — 完了後はバーを隠してこのラベルに
        # 結果テキストだけを出す。走査中はバー（動作中）とこのラベル
        # （走査済みフォルダ数）が並ぶ (#72)。
        self._scan_status = QLabel(
            t("viewer.health_dialog.scanning_fmt", n=0), self,
        )
        scan_row.addWidget(self._scan_status, 1)
        self._cancel_scan_btn = QPushButton(t("common.action.abort"), self)
        self._cancel_scan_btn.clicked.connect(self._on_cancel_scan)
        scan_row.addWidget(self._cancel_scan_btn)
        outer.addLayout(scan_row)

        self._summary_label = QLabel("", self)
        self._summary_label.setWordWrap(True)
        outer.addWidget(self._summary_label)

        # Results tree.
        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels([
            # (UIレビュー 07-25 #117) 「問題」ではなく「検出内容」— この列には
            # 【情報】カテゴリ（old/ 肥大・post.md 欠落）の行も混ざる。
            t("viewer.health_dialog.col_detected"),
            t("common.label.details"),
            t("common.label.size"),
        ])
        # 見出しの揃えは内容の揃えに合わせる (UIレビュー 07-25 #97) —
        # サイズ列（列2）は数値なので内容もろとも右揃えへ。
        align_header(self._tree, right=(2,))
        # (UIレビュー 08-28 N-23) 生の setColumnWidth だけでは
        # ``stretchLastSection``（既定 True）が余白を全部「サイズ」列へ渡し、
        # 「128 B」しか入らない列が約 300px を占める一方で「検出内容」
        # （パス）と「詳細」（カテゴリの平易な説明）が既定幅で一度も読み切れ
        # なかった。姉妹実装 ``shortcuts_dialog``（07-25 #36）と同じ規約へ:
        # 最終列の伸長を切り、余白は最長テキストを持つ列 0 が受け取る。
        hdr = self._tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._tree.setColumnWidth(1, 300)
        # パス列だけは中央省略（頭のドライブとファイル名の両端を残す）。
        # ビュー全体の ``setTextElideMode`` は列 1（説明文）まで中央省略に
        # してしまうため、列 0 専用のデリゲートで指定する。
        self._tree.setItemDelegateForColumn(0, _ElideMiddleDelegate(self._tree))
        self._tree.setAlternatingRowColors(True)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        outer.addWidget(self._tree, 1)

        # Per-row action buttons (enabled by selection).
        row_actions = QHBoxLayout()
        # (UIレビュー 07-25 #56) ラベルは「フォルダを開く」ではなく
        # 「エクスプローラで開く」— 同じ語がルート選択（アプリ内でフォルダを
        # 開く）にも使われており、実際の挙動（OS のファイラ起動）と食い違う。
        # 図像も OS 起動系専用の folder-output へ（folder-open はアプリ内で
        # フォルダを開く動詞 = ルート選択 / ZIP ドリル専用に戻す）。
        self._open_btn = QPushButton(t("common.action.open_in_explorer"), self)
        self._open_btn.setIcon(icon("folder-output"))
        self._open_btn.clicked.connect(self._on_open_selected)
        self._open_btn.setEnabled(False)
        row_actions.addWidget(self._open_btn)
        row_actions.addStretch(1)
        # (UIレビュー 07-25 #18) post.md が 1 つも無いライブラリでは
        # 「post.md 欠落」はライブラリの正常な姿そのもの — 既定では畳み、
        # あえて見たいときだけこのチェックで開く（畳んだときだけ出す）。
        self._show_missing_md_cb = QCheckBox(
            t("viewer.health_dialog.show_missing_post_md"), self,
        )
        self._show_missing_md_cb.setToolTip(
            t("viewer.health_dialog.show_missing_post_md_tooltip")
        )
        self._show_missing_md_cb.setChecked(False)
        self._show_missing_md_cb.setVisible(False)
        self._show_missing_md_cb.toggled.connect(self._on_show_missing_toggled)
        row_actions.addWidget(self._show_missing_md_cb)
        outer.addLayout(row_actions)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)

        # Bulk actions.
        bulk = QHBoxLayout()
        # UIレビュー #13: 一括削除は不可逆な破壊的操作 — danger トークンの
        # 危険信号を付ける objectName マーカー（実際のスタイルは
        # common/ui/qss.py の QPushButton#destructiveButton ルール）。
        # (UIレビュー 07-25 #32) 危険配色のボタンが対象 0 件でも押せる状態で
        # 並んでいると「押したら何か起きる」と読める — 件数はラベルに出し、
        # 有効化もカテゴリごとの件数で行う（_refresh_bulk_actions）。
        # (UIレビュー 07-25 #87) 0 バイトファイルにも .part と同格の一括手段。
        self._del_part_btn = QPushButton(self)
        self._del_part_btn.setObjectName("destructiveButton")
        self._del_part_btn.clicked.connect(self._on_delete_all_parts)
        bulk.addWidget(self._del_part_btn)
        self._del_zero_btn = QPushButton(self)
        self._del_zero_btn.setObjectName("destructiveButton")
        self._del_zero_btn.clicked.connect(self._on_delete_all_zero_byte)
        bulk.addWidget(self._del_zero_btn)
        self._del_empty_btn = QPushButton(self)
        self._del_empty_btn.setObjectName("destructiveButton")
        self._del_empty_btn.clicked.connect(self._on_delete_all_empty)
        bulk.addWidget(self._del_empty_btn)
        bulk.addStretch(1)
        # (UIレビュー 07-25 #86) 走査対象をダイアログ内から変え直せるように
        # する（従来はウィンドウ側のルートを変えて開き直すしかなかった）。
        self._pick_target_btn = QPushButton(
            t("viewer.health_dialog.choose_target"), self,
        )
        self._pick_target_btn.setIcon(icon("folder-open"))
        self._pick_target_btn.clicked.connect(self._on_pick_target)
        bulk.addWidget(self._pick_target_btn)
        self._rescan_btn = QPushButton(t("viewer.health_dialog.rescan"), self)
        self._rescan_btn.setIcon(icon("refresh"))
        self._rescan_btn.clicked.connect(self._start_scan)
        bulk.addWidget(self._rescan_btn)
        self._close_btn = QPushButton(t("common.action.close"), self)
        self._close_btn.clicked.connect(self.accept)
        bulk.addWidget(self._close_btn)
        outer.addLayout(bulk)

        # UIレビュー #4: この閲覧専用ダイアログに主要な肯定アクションは無い。
        # 「閉じる」だけでなく**全ボタン**の default を降ろす — 自前レイアウト
        # のダイアログでは先頭の QPushButton（= 一括削除）が Qt の暗黙 default
        # になり、Enter の着地先が破壊操作になっていた。
        demote_all_defaults(
            self._cancel_scan_btn,
            self._open_btn,
            self._del_part_btn,
            self._del_zero_btn,
            self._del_empty_btn,
            self._pick_target_btn,
            self._rescan_btn,
            self._close_btn,
        )

        self._set_bulk_enabled(False)

    #: 一括削除ボタン → (カテゴリ, ラベル i18n キー)。
    _BULK_BUTTONS = (
        ("_del_part_btn", CATEGORY_PART,
         "viewer.health_dialog.delete_all_parts"),
        ("_del_zero_btn", CATEGORY_ZERO_BYTE,
         "viewer.health_dialog.delete_all_zero_byte"),
        ("_del_empty_btn", CATEGORY_EMPTY_FOLDER,
         "viewer.health_dialog.delete_all_empty"),
    )

    def _set_bulk_enabled(self, enabled: bool) -> None:
        """Gate the bottom-row actions while a scan / delete is in flight.

        再スキャン・対象フォルダ選択は素直に *enabled* をたどるが、削除ボタンは
        さらに「そのカテゴリの対象が 1 件以上あるか」で決まる
        (UIレビュー 07-25 #32) — :meth:`_refresh_bulk_actions` を見ること。

        (レビュー 2026-08-27 #77) 一括削除が実行中のあいだは *enabled* が何で
        あろうと全部落とす。ダイアログはモードレスで :meth:`rescan` は公開 API
        （ウィンドウの診断メニューがいつでも呼べる）なので、削除中に着地した
        再走査の ``_on_finished`` がボタンを復活させると 2 本目の削除が起動し、
        単一スロットの ``_delete_token`` / ``_delete_progress`` /
        ``_delete_targets`` を上書きしてしまう（1 本目の完了が 2 本目の進捗
        ダイアログを閉じ、その ``canceled`` が 2 本目を途中で打ち切る）。
        """
        if self._delete_token is not None:
            enabled = False
        for btn in (self._rescan_btn, self._pick_target_btn):
            btn.setEnabled(enabled)
        self._refresh_bulk_actions(enabled)

    def _refresh_bulk_actions(self, enabled: bool = True) -> None:
        """Label + enable each bulk-delete button from its live count (#32).

        対象 0 件のときは危険配色のまま押せる状態にせず、件数もラベルに出す
        （「.part をすべて削除 (3)」）。レポートが無い間は件数の付かない
        素のラベルに戻す。
        """
        for attr, category, label_key in self._BULK_BUTTONS:
            btn = getattr(self, attr)
            n = len(self._deletable_for(category))
            label = t(label_key)
            if n:
                label += t("viewer.health_dialog.bulk_count_suffix", n=n)
            btn.setText(label)
            btn.setEnabled(bool(enabled and n))

    # -------------------------------------------------------------- scanning

    def _start_scan(self) -> None:
        # Cancel any in-flight scan first (rescan button).
        if self._token is not None:
            self._token.cancel()
        self._report = None
        self._has_any_post_md = True
        self._show_missing_md_cb.setVisible(False)
        self._tree.clear()
        self._summary_label.setText("")
        # (UIレビュー 07-25 #117) 走査中はバー + 件数テキスト、完了後はテキスト
        # だけ（#72 — 不確定バーは自前のテキストを描けない）。
        self._progress.setVisible(True)
        self._scan_status.setVisible(True)
        self._scan_status.setText(t("viewer.health_dialog.scanning_fmt", n=0))
        self._progress.setRange(0, 0)
        self._cancel_scan_btn.setEnabled(True)
        self._set_bulk_enabled(False)
        self._open_btn.setEnabled(False)

        token = self._token = CancelToken()
        # 放棄できる FS 仕事は**プールに載せない**（``submit_detached``）:
        # ``QThreadPool`` はグローバルでも専用でも、デストラクタが in-flight な
        # ``QRunnable`` を無制限に待つ。死んだ共有では 1 回の I/O が 15〜195 秒
        # かかるので、プールに載せると「窓は閉じたのにプロセスが残る」へ症状が
        # 移るだけ。追い越し（再走査）は ``submit_detached`` の世代が、利用者の
        # 「中止」は ``token`` が担う。
        self._scan_stream.submit_detached(
            lambda job, r=self._root, tok=token: _run_scan(job, r, tok),
            label="health-scan",
        )

    def _on_progress(self, n: object) -> None:
        # 走査済みフォルダ数はラベルへ (#72) — 不確定バーは ``setValue`` の値も
        # 書式も描画しないため、ここでバーを触っても画面には何も出ない。
        self._scan_status.setText(
            t("viewer.health_dialog.scanning_fmt", n=int(cast(int, n)))
        )

    def _on_cancel_scan(self) -> None:
        if self._token is not None:
            self._token.cancel()
        self._cancel_scan_btn.setEnabled(False)

    def _on_scan_done(self, payload: object) -> None:
        """走査の結末（レポート / 失敗）— 追い越しの選別は ``bind`` が済み。"""
        if not isinstance(payload, StreamOutcome):
            return  # the worker raised
        kind = cast(_ScanKind, payload.kind)
        match kind:
            case "report":
                self._on_finished(cast(HealthReport, payload.value))
            case "failed":
                self._on_failed(cast(str, payload.value))
            case _:
                assert_never(kind)

    def _on_failed(self, message: str) -> None:
        self._cancel_scan_btn.setEnabled(False)
        # 完了状態はバーではなく ``_scan_status`` が出す（#117 でバーは隠す）
        # ので、ここでバーの range / value / format を書いても画面には一切
        # 現れない（レビュー 2026-09-03 項目 #255）。
        self._show_scan_status(t("viewer.health_dialog.scan_failed_fmt"))
        # No report → the delete buttons stay dark, but 再スキャン must come
        # back so a transient failure (NAS hiccup) can be retried (#30).
        # 対象フォルダの選び直しも同様（消えたルートから抜ける手段として、
        # 失敗時こそ必要 — UIレビュー 07-25 #86）。
        self._rescan_btn.setEnabled(True)
        self._pick_target_btn.setEnabled(True)
        # A failure is modal (design.md 失敗=モーダル).
        QMessageBox.warning(
            self, t("viewer.health_dialog.scan_failed_title"), message,
        )

    def _on_finished(self, report: HealthReport) -> None:
        self._report = report
        # (UIレビュー 07-25 #18) 「post.md 欠落」を畳むかどうかの判定材料は
        # レポート自身が運ぶ（走査の post-order 畳み込みで確定済み）。
        # キャンセルされた部分走査は既定 True = 抑制しない側のまま。
        self._has_any_post_md = bool(report.library_has_post_md)
        self._cancel_scan_btn.setEnabled(False)
        scanned = report.scanned_folders
        if report.cancelled:
            done_text = t("viewer.health_dialog.cancelled_fmt", n=scanned)
        else:
            done_text = t("viewer.health_dialog.done_fmt", n=scanned)
        self._show_scan_status(done_text)  # (UIレビュー 07-25 #117)
        self._sync_missing_md_default(report)
        self._populate_tree(report)
        self._set_bulk_enabled(True)

    def _show_scan_status(self, text: str) -> None:
        """Swap the finished progress bar for a plain status line (#117).

        走査が終わったあともアクセント色で満杯のバーが残ると「まだ動いて
        いる」ように見え、満杯バーの上に描かれる文字はコントラストも落ちる。
        終了状態はテキストだけで足りる。
        """
        self._progress.setVisible(False)
        self._scan_status.setText(text)
        self._scan_status.setVisible(True)

    def _missing_post_md_suppressed(self) -> bool:
        """True when the 「post.md 欠落」 category must stay folded (#18).

        post.md が 1 つも無いライブラリでは全コンテンツフォルダが 1 行ずつ
        並ぶだけ（= このライブラリの正常な姿）なので既定で畳む。混在
        ライブラリ（外部ツール由来の部分木と利用者自身の写真フォルダが同じルートに
        同居する — 本製品が明示的に想定する形）では既定で開くが、チェックを
        外せば畳める: 畳めるかどうかの判定材料を「ツリー全体に post.md が
        1 つでもあるか」の 1 ビットに委ねると、混在では折り畳む手段が 1 つも
        出ないまま利用者の写真フォルダが全部並ぶ。
        """
        return not self._show_missing_md_cb.isChecked()

    def _sync_missing_md_default(self, report: HealthReport) -> None:
        """レポートごとに「post.md 欠落」の既定の開閉を決める。

        post.md が 1 つも無いライブラリでは既定で畳む（全コンテンツフォルダが
        1 行ずつ並ぶだけ = そのライブラリの正常な姿）。それ以外は既定で開く。
        利用者はどちらでもチェックボックスで開閉できる — 可視条件の方は
        :meth:`_populate_tree_inner` が件数だけで決める。
        """
        want = bool(report.library_has_post_md)
        if self._show_missing_md_cb.isChecked() == want:
            return
        # ここでの ``setChecked`` は利用者操作ではないので、再描画を促す
        # ``toggled`` は出さない（この直後に _populate_tree が走る）。
        blocked = self._show_missing_md_cb.blockSignals(True)
        try:
            self._show_missing_md_cb.setChecked(want)
        finally:
            self._show_missing_md_cb.blockSignals(blocked)

    def _category_explanation(self, category: str) -> str:
        """カテゴリの説明文。情報側へ降りた空フォルダだけ別文面を使う。

        「削除して問題ありません」と断言する文面は、そのカテゴリが実際に
        一括削除の対象であることが前提。post.md を 1 つも持たないライブラリ
        では空フォルダは情報カテゴリ（削除手段なし）なので、断言しない版へ
        差し替える。
        """
        if (
            category == CATEGORY_EMPTY_FOLDER
            and self._report is not None
            and CATEGORY_EMPTY_FOLDER in self._report.info_categories()
        ):
            return t("viewer.health_check.explain_empty_folder_plain")
        return category_explanation(category)

    def _on_show_missing_toggled(self, _checked: bool) -> None:
        if self._report is not None:
            self._populate_tree(self._report)

    # ---------------------------------------------------------------- render

    def _populate_tree(self, report: HealthReport) -> None:
        self._tree.setUpdatesEnabled(False)
        try:
            self._populate_tree_inner(report)
        finally:
            self._tree.setUpdatesEnabled(True)

    def _populate_tree_inner(self, report: HealthReport) -> None:
        self._tree.clear()
        counts = report.counts()
        # (UIレビュー 07-25 #18) 畳んだカテゴリは行だけでなく集計からも外す
        # （「post.md 欠落 3000 件」とだけ言って何も出ないのは不可解）。
        # チェックボックスは畳める状況＝抑制の余地があるときだけ出す。
        suppressed = (
            {CATEGORY_MISSING_POST_MD} if self._missing_post_md_suppressed()
            else set()
        )
        # 行が 1 件でもあれば出す（既定の入り / 切りは _sync_missing_md_default
        # がレポートごとに決める）。
        self._show_missing_md_cb.setVisible(
            bool(counts[CATEGORY_MISSING_POST_MD])
        )
        # J04/J05: 情報提示のみの old_bloat を「問題」の集計から分ける。
        info_categories = report.info_categories()
        problem_total = report.problem_count()
        info_total = report.info_count() - sum(
            counts[c] for c in suppressed if c in info_categories
        )
        if problem_total == 0 and info_total == 0:
            self._summary_label.setText(t("viewer.health_dialog.no_problems"))
        else:
            parts = [
                t(
                    "viewer.health_dialog.summary_part",
                    label=category_label(c), n=counts[c],
                )
                for c in CATEGORY_ORDER
                if counts[c] and c not in suppressed
            ]
            if info_total:
                prefix = t(
                    "viewer.health_dialog.summary_counts",
                    problems=problem_total, info=info_total,
                )
            else:
                prefix = t(
                    "viewer.health_dialog.summary_prefix", n=problem_total,
                )
            self._summary_label.setText(prefix + " / ".join(parts))

        for cat in CATEGORY_ORDER:
            if cat in suppressed:
                continue
            issues = report.issues_for(cat)
            if not issues:
                continue
            # J04: informational-only categories carry an 「情報」 badge so a big
            # old/ shelter reads as info, not a problem.
            head_key = (
                "viewer.health_dialog.category_head_info"
                if cat in info_categories
                else "viewer.health_dialog.category_head"
            )
            explanation = self._category_explanation(cat)
            head_text = t(head_key, label=category_label(cat), n=len(issues))
            # (UIレビュー 2026-09-11 N-06) 説明を「詳細」列（幅 300px 固定・
            # パス行と共有）へ入れていたため、カテゴリごとの平易な説明が
            # どのカテゴリでも途中で切れて読めなかった。見出し行だけを全幅に
            # 伸ばし（``setFirstColumnSpanned``）、見出しと説明を 1 つの
            # セルに連結する — 子の issue 行は 3 列のまま。
            head = QTreeWidgetItem(
                self._tree,
                [
                    t(
                        "viewer.health_dialog.category_head_explained",
                        head=head_text, explanation=explanation,
                    ),
                    "",
                    "",
                ],
            )
            # 幅が足りないときの回復手段（全文はツールチップに残す）。
            head.setToolTip(0, explanation)
            head.setFirstColumnSpanned(True)
            head.setExpanded(True)
            # 表示は先頭 _MAX_ROWS_PER_CATEGORY 件まで。超過分は「他 N 件」の
            # 1 行に畳む（数万行の同期生成で GUI が固まるのを防ぐ）。一括削除の
            # 対象は report.issues 全件のままで、上限は表示のみに効く。
            for issue in issues[:_MAX_ROWS_PER_CATEGORY]:
                self._add_issue_row(head, issue)
            hidden = len(issues) - _MAX_ROWS_PER_CATEGORY
            if hidden > 0:
                more_text = t(
                    "viewer.health_dialog.more_rows",
                    n=hidden, shown=_MAX_ROWS_PER_CATEGORY,
                )
                more = QTreeWidgetItem(head, [more_text, "", ""])
                # ``_ISSUE_ROLE`` を持たない行 — 選択しても「開く」は無効の
                # まま、ダブルクリックも無反応（issue が無いので当然）。
                more.setToolTip(0, more_text)

    def _add_issue_row(
        self, parent: QTreeWidgetItem, issue: HealthIssue,
    ) -> None:
        size_text = format_bytes(issue.size) if issue.size not in (None, -1) else ""
        detail = issue.detail
        # docs/formats/post-md.md が公開機能として謳う「文脈付与」— post.md を
        # 持つ投稿の中の問題には、それがどの投稿かを添える。走査は既にこの値を
        # 読んでいる（issue が出たときだけ・ファイル単位でメモ化）ので、出さ
        # ないと読んだ結果が丸ごと捨てられる。
        ref_text = _postref_text(issue.postref)
        if ref_text:
            detail = t(
                "viewer.health_dialog.detail_with_postref",
                detail=detail, ref=ref_text,
            )
        row = QTreeWidgetItem(parent, [str(issue.path), detail, size_text])
        row.setData(0, _ISSUE_ROLE, issue)
        # UIレビュー #7: パス（列0）・詳細（列1）はどちらも幅が固定で長い値が
        # 切り詰められる — 見出し行と同様にツールチップで全文を読めるように
        # する（切れたセルの復旧手段が手動リサイズしかなかった）。
        row.setToolTip(0, str(issue.path))
        row.setToolTip(1, detail)
        # サイズは数値 — 桁が縦に揃うよう右寄せ（見出しも右: #97）。
        row.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)

    # ---------------------------------------------------------- row helpers

    def _selected_issue(self) -> HealthIssue | None:
        items = self._tree.selectedItems()
        if not items:
            return None
        data = items[0].data(0, _ISSUE_ROLE)
        return data if isinstance(data, HealthIssue) else None

    def _on_selection_changed(self) -> None:
        issue = self._selected_issue()
        self._open_btn.setEnabled(issue is not None)

    def _on_item_double_clicked(
        self, item: QTreeWidgetItem, _column: int,
    ) -> None:
        data = item.data(0, _ISSUE_ROLE)
        if isinstance(data, HealthIssue):
            self._open_path(data.path)

    def _on_open_selected(self) -> None:
        issue = self._selected_issue()
        if issue is not None:
            self._open_path(issue.path)

    def _on_pick_target(self) -> None:
        """Re-target the scan from inside the dialog (UIレビュー 07-25 #86).

        走査対象はこれまでウィンドウ側のルートに固定で、サブフォルダだけを
        調べ直すにはビューアのルートごと動かすしかなかった。選び直しは既存の
        :meth:`rescan` に流すだけ（進行中の走査はそこでキャンセルされる）。
        """
        chosen = pick_existing_directory(
            self,
            t("viewer.health_dialog.pick_target_title"),
            str(self._root),
            sidebar=host_picker_places(self),
        )
        if chosen:
            self.rescan(Path(chosen))

    def _open_path(self, path: Path) -> None:
        """エクスプローラで開く（ファイルなら親フォルダ）。

        種別判定は **必ず** :func:`~.path_probe.probe_path_kind` を通す
        （``path_probe`` の規約 — 同期的に見える存在確認はここを通す）。
        健全性チェックの主対象はコールドな NAS ライブラリで、ここへ渡るのは
        走査が「壊れている / 読めない」と判定した行そのもの（``unreadable``
        = オフラインの共有）なので、生の ``Path.is_dir()`` は SMB タイムアウト
        （数十秒）まで GUI スレッドを止める。このダイアログはモードレスなので
        止まるのはダイアログだけでなくアプリ全体（レビュー 2026-09-03 項目
        #105。ブックマーク経路の項目#109 と同一機構の別の呼び出し口）。
        タイムアウト（``None``）はフォルダ扱いにせず、開く相手を確定できない
        ので ``dir`` のときだけフォルダ自身、それ以外は親を開く。
        """
        kind = probe_path_kind(str(path))
        target = path if kind == "dir" else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # --------------------------------------------------------- bulk: delete

    def _on_delete_all_parts(self) -> None:
        self._bulk_delete(CATEGORY_PART, is_dir=False)

    def _on_delete_all_zero_byte(self) -> None:
        # (UIレビュー 07-25 #87) .part と同格の一括手段（確認モーダル・既定 No）。
        self._bulk_delete(CATEGORY_ZERO_BYTE, is_dir=False)

    def _on_delete_all_empty(self) -> None:
        self._bulk_delete(CATEGORY_EMPTY_FOLDER, is_dir=True)

    def _deletable_for(self, category: str) -> list[Path]:
        """Paths a bulk delete for *category* would remove (0 件なら空).

        ``part`` / ``empty_folder`` はホスト側の
        :func:`~snappix.viewer.health_check.deletable_paths` がそのまま答える。
        ``zero_byte`` (UIレビュー 07-25 #87) はそこでは「削除直前に再確認が要る
        カテゴリ」として除外されているため、同じ重複排除・順序維持の規約を
        持つ :func:`~snappix.viewer.health_check.paths_for_category` へ回す
        （UIレビュー07-25 追修: 以前はここでそのループを書き写しており、規約が
        二重実装になっていた）— 削除直前の再確認は ``_run_bulk_delete`` の
        ``recheck_zero_byte``。
        """
        if self._report is None:
            return []
        # 情報カテゴリには一括削除を出さない（危険配色のボタンが 0 件で無効に
        # なる）。集合はレポート依存 — post.md を 1 つも持たないライブラリでは
        # 「空フォルダ」も情報側へ降りる（利用者が整理用に作ったフォルダが
        # rmtree の対象に並ぶのを防ぐ）。
        if category in self._report.info_categories():
            return []
        if category == CATEGORY_ZERO_BYTE:
            return paths_for_category(self._report.issues, CATEGORY_ZERO_BYTE)
        return deletable_paths(self._report.issues, category)

    def _bulk_delete(self, category: str, *, is_dir: bool) -> None:
        if self._report is None:
            return
        # (レビュー 2026-08-27 #77) 削除は単一スロット（token / 進捗ダイアログ /
        # 通知対象）で回っている — 2 本目を起動させない。ボタンは
        # ``_set_bulk_enabled`` が既に落としているが、キーボード操作や
        # ``_on_delete_all_*`` の直接呼び出しでも成立するようここでも弾く。
        if self._delete_token is not None:
            return
        paths = self._deletable_for(category)
        label = category_label(category)
        if not paths:
            show_toast(
                self.window(),
                t("viewer.health_dialog.none_found", label=label),
                kind="info",
            )
            return
        # Destructive → confirm modal listing the count + examples.
        examples = "\n".join(f"  {p}" for p in paths[:5])
        if len(paths) > 5:
            examples += t(
                "viewer.health_dialog.more_examples", n=len(paths) - 5,
            )
        kind_word = (
            t("common.label.folder") if is_dir else t("common.label.file")
        )
        # 動詞ラベル + 「ごみ箱には入りません」の明示（N-01 / N-40）。ごみ箱
        # 経由化は既決の見送り（docs/claude/viewer.md — send2trash 等の新規
        # 依存なし）なので、**取り消せないことを言い切る**のと削除ログを残す
        # のが復旧手段の代わりになる。
        if not confirm_action(
            self,
            title=t("viewer.health_dialog.delete_title", label=label),
            body=t(
                "viewer.health_dialog.delete_confirm",
                n=len(paths), kind=kind_word, examples=examples,
            ),
            informative=t("viewer.health_dialog.delete_no_trash"),
            accept_text=t("common.action.delete_permanently"),
            icon=QMessageBox.Icon.Warning,
            destructive=True,
            # 本文には実ファイルパス（外部由来の文字列）が埋まる。AutoText の
            # まま渡すと ``mightBeRichText`` が真になった瞬間に HTML として
            # 解釈され、``<...>`` を含む名前で文面そのものが化ける
            # （レビュー 2026-09-03 項目 #254。姉妹実装
            # ``curation_recovery`` と同じ理由・同じ指定）。
            plain_text=True,
        ):
            return
        # Delete off the GUI thread (see _run_bulk_delete — the per-path I/O,
        # including the empty-folder re-check, lives there).  A window-modal
        # progress dialog provides per-path progress and a cancel button that
        # flips the shared cooperative token.
        # (項目#172) .part は削除直前に「走査時の (size, mtime) から変わって
        # いないか」を再確認する — 走査時スナップショットを issue から拾って
        # ワーカーへ渡す（空フォルダ / 0 バイトの再確認と同格の安全弁）。
        file_snapshots = None
        if category in (CATEGORY_PART, CATEGORY_ZERO_BYTE):
            file_snapshots = {
                i.path: (i.size, i.mtime)
                for i in self._report.issues
                if i.category == category
            }
        self._set_bulk_enabled(False)
        self._delete_targets = list(paths)
        token = self._delete_token = CancelToken()
        progress = QProgressDialog(
            t("viewer.health_dialog.deleting_label", label=label),
            t("common.action.abort"), 0, len(paths), self,
        )
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(300)
        progress.canceled.connect(token.cancel)
        self._delete_progress = progress
        # Qt が要求する初期化: ``minimumDuration`` の表示タイマーは最初の
        # ``setValue`` で初めて走る。ワーカーの ``progress`` は
        # ``_PROGRESS_STRIDE``(25) で間引かれるので、これが無いと対象 24 件
        # 以下（.part 数件などの典型的な小バッチ = 実運用の常態）では
        # ``setValue`` が一度も呼ばれず、進捗も中止ボタンも最後まで出なかった
        # （レビュー 2026-09-03 項目 #106）。ワーカーを start する**前**に
        # 済ませるので、WindowModal な ``setValue`` の ``processEvents`` が
        # ``finished`` の配送と入れ子になることはない（項目#173）。
        progress.setValue(0)
        # 削除も放棄できる FS 仕事（上の走査と同じ理由でプールに載せない）。
        self._delete_stream.submit_detached(
            lambda job, ps=list(paths), snaps=file_snapshots, tok=token:
                _run_bulk_delete(
                    job, ps,
                    is_dir=is_dir,
                    recheck_empty=(category == CATEGORY_EMPTY_FOLDER),
                    file_snapshots=snaps,
                    token=tok,
                    label=label,
                ),
            label="health-delete",
        )

    def _on_delete_progress(self, n: object) -> None:
        if self._delete_progress is not None:
            self._delete_progress.setValue(int(cast(int, n)))

    def _on_delete_finished(self, payload: object) -> None:
        """一括削除が着地した — ``(deleted, errors, skipped, log_path)``。"""
        if not isinstance(payload, tuple) or len(payload) != 4:
            return  # the worker raised
        deleted, errors, skipped, log_path = payload
        self._apply_delete_result(deleted, errors, int(skipped), log_path)

    def _apply_delete_result(
        self,
        deleted: object,
        errors: object,
        skipped: int,
        log_path: Path | None = None,
    ) -> None:
        # ``deleted`` は削除済みパスの一覧（N-40）。件数だけを渡していた頃の
        # 呼び出し（テスト等）も壊さないよう int を許容する。
        if isinstance(deleted, int):
            deleted_count = deleted
        else:
            deleted_count = len(list(deleted))  # type: ignore[arg-type]
        errs: list[tuple[Path, str]] = list(errors)  # type: ignore[arg-type]
        # 削除ログ（ごみ箱を経由しない以上、消したパスの記録だけが唯一の
        # 事後手段 — N-40）はワーカーが既に書いている（項目#217）。ここは
        # その結果を受け取るだけ。
        #
        # (UIレビュー 2026-09-11 N-72) 中止で抜けたかどうかは token でしか
        # 分からない — ``self._delete_token`` を畳む**前**に読む。開始直後に
        # 中止すると deleted は 0 件で、従来はそれが「0 件を削除しました」の
        # success トーストとして出ていた（押した中止が効いたのか分からない）。
        cancelled = (
            self._delete_token is not None and self._delete_token.is_cancelled()
        )
        remaining = max(
            0, len(self._delete_targets) - deleted_count - skipped,
        )
        if self._delete_progress is not None:
            self._delete_progress.close()
            self._delete_progress.deleteLater()
            self._delete_progress = None
        self._delete_token = None
        # (UIレビュー 07-25 #71) 背後のグリッドは削除を知らないまま古い内容を
        # 描き続ける — ホストの公開 API に「この範囲が変わった」と通知して
        # フォルダプレビューキャッシュの無効化 + 再スキャンを任せる。
        # (UIレビュー07-25 追修) この通知と内部状態（``_delete_targets``）の
        # 後始末は**可視性に関係なく必ず**行う。一括削除の途中でダイアログを
        # 閉じると、既に消えたフォルダのプレビューキャッシュが永久に無効化
        # されず（親フォルダの mtime は変わらない — notify_library_changed の
        # docstring 参照）、グリッドが存在しないフォルダを描き続けていた。
        self._notify_library_changed()
        if not self.isVisible():
            # The dialog was closed mid-delete (done() cancelled the token) —
            # 報告する相手も走査し直す木も無いので UI だけを畳む。
            return
        if errs and not deleted_count:
            QMessageBox.warning(
                self,
                t("viewer.health_dialog.delete_failed_title"),
                t("viewer.health_dialog.delete_failed_body", n=len(errs))
                + "\n".join(
                    f"  {p}: {e}" for p, e in errs[:5]
                ),
            )
        else:
            if cancelled:
                # 中止は「途中まで消えた」状態 — 成功として畳まない（N-72）。
                msg = t(
                    "viewer.health_dialog.delete_cancelled",
                    done=deleted_count, rest=remaining,
                )
            else:
                msg = t("viewer.health_dialog.deleted_count", n=deleted_count)
            if errs:
                msg += t(
                    "viewer.health_dialog.deleted_errors_suffix", n=len(errs),
                )
            if skipped:
                msg += t(
                    "viewer.health_dialog.deleted_skipped_suffix", n=skipped,
                )
            partial = bool(errs or skipped or cancelled)
            if log_path is not None:
                msg += t("viewer.health_dialog.deleted_logged_suffix")
            show_toast(
                self.window(),
                msg,
                kind="warning" if partial else "success",
                # (UIレビュー 07-25 #10) 部分失敗（失敗・スキップ）は
                # クリックされるまで残す — 数分かかる一括削除の「N 件失敗」が
                # 3 秒で自動消滅すると、離席したユーザーは事実ごと失う。
                # 削除ログの導線が付くときは 3 秒では押しに行けないので
                # 伸ばす（N-40）。
                duration_ms=(
                    0
                    if partial
                    else (_ACTION_TOAST_MS if log_path is not None else 3000)
                ),
                # (N-40) ごみ箱に入らない削除の唯一の事後手段 = 記録。トースト
                # から 1 クリックで開けるようにする（消えても data/logs に
                # 残っているので、これは近道であって唯一の入口ではない）。
                action_text=(
                    t("viewer.health_dialog.open_log_folder")
                    if log_path is not None
                    else None
                ),
                on_action=(
                    # ``_open_path`` opens a file's *containing* folder — the
                    # same helper the per-row 「エクスプローラで開く」 uses.
                    (lambda p=log_path: self._open_path(p))
                    if log_path is not None
                    else None
                ),
            )
        # Re-scan so the tree reflects the deletions.
        self._start_scan()

    def _notify_library_changed(self) -> None:
        """Tell the host window that the deleted subtrees changed (#71).

        影響範囲は削除したパスの親フォルダ（ファイルを消してもフォルダ自体の
        エントリは残るため、親を渡さないとプレビューキャッシュが落ちない）。
        ホストが無い / API を持たない（テスト・単体起動）ときは何もしない。
        """
        targets = self._delete_targets
        self._delete_targets = []
        if not targets:
            return
        notify = getattr(self.parent(), "notify_library_changed", None)
        if not callable(notify):
            return
        parents = sorted({p.parent for p in targets})
        notify(parents)


def _postref_text(ref: "PostRef | None") -> str:
    """``service / post_id / creator`` を 1 行に畳む（無い要素は落とす）。

    3 つとも省略可能（docs/formats/post-md.md §3）なので、書かれていた分だけ
    並べる。何も分からない post.md では空文字を返し、呼び出し側は列を元の
    detail のままにする。
    """
    if ref is None:
        return ""
    parts = [v for v in (ref.service, ref.post_id, ref.creator_id) if v]
    return " / ".join(parts)


def _file_changed_since_scan(
    path: Path, snapshot: tuple[int | None, float | None] | None,
) -> bool:
    """True when *path* is no longer the file the scan reported.

    The **one** pre-delete re-check for file categories (``part`` /
    ``zero_byte``), against the ``(size, mtime)`` the scan captured from the
    ``DirEntry.stat`` it already had.  They need the same guard for the same
    reason: a writer plugin may run **in-process** (an external tool may run
    alongside), so a ``.part`` listed as a leftover — or an output file that
    was still 0 bytes when the walk passed — can be the destination of a
    download in flight by the time the delete button is pressed.  Size alone
    cannot see that in the zero-byte case: the file is 0 bytes until the first
    byte lands, so the snapshot's mtime is what tells the two apart (項目#172).

    倒し方は空フォルダ側の
    :func:`~snappix.viewer.health_check.dir_has_any_file` と揃える:

    * 消滅済み（``FileNotFoundError``）は **False** — 既に目的の状態なので
      削除経路へ流し、:func:`_delete_paths` が成功として畳む。
    * ``stat`` 失敗・スナップショット不明（走査時に stat できなかった）は
      **True** — 「変わっていない」と証明できないものは消さない。

    ``stat`` 失敗でスキップした分は「内容が変わった」わけではないので、報告
    文言は 2 つの理由（状態が変わった / 状態を確認できなかった）の両方を言う
    — ``deleted_skipped_suffix``（レビュー 2026-08-27 #176）。
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if snapshot is None:
        return True
    size, mtime = snapshot
    if size is None or size < 0 or mtime is None:
        return True
    return (st.st_size, st.st_mtime) != (size, mtime)


def _delete_paths(
    paths: list[Path], *, is_dir: bool,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Delete *paths* (files or dir trees). Returns ``(deleted, errors)``.

    Pure filesystem side effects (no Qt) so it is independently testable.
    Uses ``shutil.rmtree`` for folders and ``os.remove`` for files — no new
    dependency (send2trash etc.: ごみ箱経由化は既決の見送り —
    docs/claude/viewer.md).

    ``deleted`` は**件数ではなくパスの一覧**（UIレビュー 2026-08-28 N-40）。
    ごみ箱に入らない以上、何を消したかの記録だけが唯一の事後手段なので、
    呼び出し側が data/logs の削除ログへ書き出せるよう一覧で返す — ログ出力
    自体はここでは行わない（この関数を Qt 非依存・副作用がファイル削除だけ
    の純関数のまま保つため。件数は ``len()`` で足りる）。

    レポートは走査時点のスナップショットなので、外部（エクスプローラ・別の
    ツール）で先に消されたパスに当たることがある。``FileNotFoundError`` は
    **失敗ではなく成功**として数える — 求めた状態は既に成立しており、常駐
    warning で「N 件失敗」と報告する筋合いは無い（一覧にも載る: 走査時に
    在ったものが消えたという記録は残す価値がある）。
    """
    deleted: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for path in paths:
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
        except FileNotFoundError:
            deleted.append(path)
        except OSError as exc:
            errors.append((path, str(exc)))
        else:
            deleted.append(path)
    return deleted, errors


def _rotate_deletion_log(log_path: Path) -> None:
    """Move an over-sized deletion log aside so the live file starts fresh.

    (レビュー 2026-09-03 項目 #256) 追記専用で上限が無いと、数万件の一括削除を
    繰り返すライブラリでは 1 パス 1 行がそのまま積み上がる。保持方針は
    ``common/logging.py`` の house policy に合わせて **上限
    :data:`_DELETION_LOG_ROTATION_BYTES` ・退避は ``.1`` の 1 世代**（そこより
    古い記録は落ちる — 直近の削除を思い出すのがこのログの目的で、履歴の
    永久保存ではない）。

    失敗は握って続行する: 退避できなくても追記は続けられるし、削除自体は
    もう起きている。ワーカースレッドから呼ばれる（項目#217）ので複数の
    一括削除が同時に回ると ``os.replace`` が競合し得るが、負け側は例外を
    握って追記へ進むだけなので記録は失われない。
    """
    try:
        if log_path.stat().st_size <= _DELETION_LOG_ROTATION_BYTES:
            return
        os.replace(log_path, log_path.with_suffix(log_path.suffix + ".1"))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("could not rotate the deletion log: {}", exc)


def log_deleted_paths(paths: list[Path], *, label: str) -> Path | None:
    """Append *paths* to the portable deletion log; returns the log file.

    (UIレビュー 2026-08-28 N-40) 一括削除はごみ箱を経由しないので、誤った
    対象で数千件消したときに「何が在ったか」を思い出す手段がゼロだった。
    完全な復旧にはならないが、パスの一覧さえ残れば再取得・再構成の起点には
    なる。

    Portability: the log lives under ``get_paths().logs`` like every other
    runtime artefact — never ``Path.home()`` / ``%APPDATA%``.  Written from
    the **worker thread** by :func:`_run_bulk_delete` (項目#217 — the append is
    per-path I/O and must not land on the GUI thread; the pure
    :func:`_delete_paths` deliberately stays log-free), appended so a session
    never overwrites an earlier one.  Size is bounded by
    :func:`_rotate_deletion_log` (上限 5 MiB・``.1`` の 1 世代).  A failure to
    write is logged and swallowed: the deletion itself already happened and
    reporting it is what matters.
    """
    if not paths:
        return None
    try:
        log_path = get_paths().logs / DELETION_LOG_NAME
        _rotate_deletion_log(log_path)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"# {stamp}\t{label}\t{len(paths)}\n")
            for path in paths:
                fh.write(f"{path}\n")
    except OSError as exc:
        logger.warning("could not write the deletion log: {}", exc)
        return None
    logger.info("deleted {} path(s) [{}] -> {}", len(paths), label, log_path)
    return log_path


__all__ = ["HealthCheckDialog", "log_deleted_paths"]
