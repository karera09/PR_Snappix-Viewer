"""Cache-build controller: the settings dialog's cache-management backend.

Implements the methods :class:`~snappix.viewer.settings_dialog.SettingsDialog`
calls on its ``cache_controller`` (``cache_stats`` / ``tag_index_stats`` /
``cache_build_in_progress`` / ``clear_persistent_caches`` /
``build_cache_interactive``) plus the modal and background
:class:`~snappix.viewer.cache_builder.CacheBuilder` orchestration that used to
live directly on ``ViewerWindow`` (#96).  Behaviour, dialog texts and the
throttling / teardown contracts are unchanged.

The window constructs one instance with its caches / loaders / status widget
and passes it to the settings dialog as ``cache_controller``; on close it
calls :meth:`shutdown` *before* closing the caches a background build writes
to (see ``ViewerWindow.closeEvent``).
"""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from loguru import logger
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QMessageBox,
    QProgressDialog,
    QWidget,
)

from ..common.i18n import t
from .cache_builder import CacheBuilder
from .dialogs import pick_existing_directory

if TYPE_CHECKING:  # annotations only — keeps runtime imports lean
    from .cache_build_status import CacheBuildStatusWidget
    from .state import ViewerState


class CacheBuildController(QObject):
    """Backs the "キャッシュ管理" group in the settings dialog.

    The dialog is handed this object as ``cache_controller`` and calls the
    public methods by name.  Background-build progress rides the window's
    status-bar :class:`CacheBuildStatusWidget`, whose pause / cancel signals
    are wired here.
    """

    def __init__(
        self,
        *,
        state: "ViewerState",
        disk_cache,
        meta_cache,
        folder_cache,
        search_index,
        tag_index,
        loaders: "Sequence | Callable[[], Sequence]",
        status_widget: "CacheBuildStatusWidget",
        status_message: Callable[[str, int], None],
        current_root: Callable[[], Path | None],
        notify: Callable[..., None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        """``loaders`` are the ThumbnailLoaders whose in-memory LRUs
        ``clear_persistent_caches`` flushes — either a fixed sequence or a
        **callable provider** returning the current set (the window passes
        ``ViewerWindow._all_loaders`` so its lazily-built filmstrip /
        lightbox loaders are included whenever they exist, レビュー
        2026-09-03 項目 #45); ``status_message(text, ms)``
        posts a transient status-bar message; ``current_root`` supplies the
        folder-picker's starting directory; ``notify(message, kind, duration_ms=…)`` (I06)
        raises a non-modal toast on the host window for build completion /
        failure / cancellation (``None`` in tests → the toast is skipped)。
        ホストの単一ファネル（``ViewerWindow._show_toast``）をそのまま渡す
        こと — ここから親ウィンドウの ``show_toast`` を直接呼ばない。
        """
        super().__init__(parent)
        self._state = state
        self._disk_cache = disk_cache
        self._meta_cache = meta_cache
        self._folder_cache = folder_cache
        self._search_index = search_index
        self._tag_index = tag_index
        self._loaders: "Sequence | Callable[[], Sequence]" = (
            loaders if callable(loaders) else tuple(loaders)
        )
        self._status_widget = status_widget
        self._status_message = status_message
        self._current_root = current_root
        self._notify = notify
        # notifier が ``duration_ms`` をどう受けるか（``None`` = 未判定）。
        # 初回の :meth:`_toast` で 1 回だけ署名を調べて覚える。
        self._notify_duration_style: str | None = None

        # Background (non-modal) cache build.  ``None`` when idle; holds the
        # running CacheBuilder + its phase label + a cancel flag while a
        # background build is in flight (used to block a second concurrent
        # build and to tear down cleanly on close).
        self._bg_builder: CacheBuilder | None = None
        self._bg_build_phase = ""
        self._bg_build_cancelled = False
        # True only while ``shutdown`` tears down an in-flight build on window
        # close.  The synchronous ``finished`` from that teardown cancel must
        # NOT raise a toast on the being-destroyed window (C13); a user-driven
        # cancel (``_on_cancel_requested``) leaves this False so its toast
        # still surfaces.
        self._tearing_down = False
        # Builders retired from use but whose worker pools may still hold
        # in-flight runnables (項目16).  QThreadPool's destructor waits
        # *unbounded* for in-flight work, so ``deleteLater()`` right after a
        # user cancel would freeze the GUI on the next event-loop turn until
        # an in-flight NAS decode completes (seconds; tens of seconds on an
        # SMB timeout).  ``_retire_builder`` parks the builder here instead
        # and a 200ms poll deletes it only once ``pools_idle()``.
        self._retiring: list[CacheBuilder] = []
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(200)
        self._retire_timer.timeout.connect(self._poll_retired)

        self._status_widget.pause_toggled.connect(self._on_pause_toggled)
        self._status_widget.cancel_requested.connect(self._on_cancel_requested)

    # ------------------------------------------------ settings-dialog API

    def cache_stats(self) -> dict:
        """Current persistent-cache footprint (bytes + entry counts).

        ``<kind>_enabled`` は「いま読める handle が在るか」で、``None`` は
        **設定でオフ**と**開けなかった**の 2 義になる（``_open_cache`` は
        失敗しても ``None`` を返す best-effort ポリシー）。設定ダイアログが
        その 2 つを言い分けられるよう、設定側の意図を ``<kind>_configured``
        として併せて返す（理由の材料はここが既に持っている）。
        ``aspect`` は設定トグルを持たない常時有効の種別。
        """
        disk_bytes = self._disk_cache.total_bytes() if self._disk_cache else 0
        disk_count = self._disk_cache.count() if self._disk_cache else 0
        aspect_bytes = self._meta_cache.estimated_bytes() if self._meta_cache else 0
        aspect_count = self._meta_cache.count() if self._meta_cache else 0
        folder_bytes = (
            self._folder_cache.estimated_bytes() if self._folder_cache else 0
        )
        folder_count = self._folder_cache.count() if self._folder_cache else 0
        search_bytes = (
            self._search_index.estimated_bytes() if self._search_index else 0
        )
        if self._search_index is not None:
            sc = self._search_index.count()
            search_count = sc.get("node", 0)
        else:
            search_count = 0
        return {
            "disk_enabled": self._disk_cache is not None,
            "disk_configured": bool(self._state.thumb_disk_cache_enabled),
            "disk_bytes": disk_bytes,
            "disk_count": disk_count,
            "aspect_enabled": self._meta_cache is not None,
            "aspect_configured": True,
            "aspect_bytes": aspect_bytes,
            "aspect_count": aspect_count,
            "folder_enabled": self._folder_cache is not None,
            "folder_configured": bool(self._state.folder_preview_cache_enabled),
            "folder_bytes": folder_bytes,
            "folder_count": folder_count,
            "search_enabled": self._search_index is not None,
            "search_configured": bool(self._state.search_index_enabled),
            "search_bytes": search_bytes,
            "search_count": search_count,
        }

    def set_tag_index(self, tag_index) -> None:
        """tags.db 再読込後の新しい索引を受け取る（K01 の再注入の受け口）。

        窓が ``_tag_index`` を直に書いていたのを公開メソッドへ。索引はビューア
        が型を知らない不透明オブジェクト（AI パックの provider が返したもの）
        で、``None``（provider 未登録 / tags.db 不在）は
        :meth:`tag_index_stats` の劣化シームへ落ちる。
        """
        self._tag_index = tag_index

    def tag_index_stats(self) -> dict | None:
        """Read-only summary of the tagger's tags.db, or ``None`` if absent.

        Backs the settings dialog's "AI タグ" info group.  The viewer only
        *reads* tags.db (the tagger writes it), so there are no controls here —
        just model / floor / image + tag counts / file size.
        """
        if self._tag_index is None:
            return None
        try:
            return self._tag_index.stats()
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("tag_index_stats failed: {}", exc)
            return None

    def cache_build_in_progress(self) -> bool:
        """True while a background cache build **or its in-flight writers** run.

        The settings dialog uses this to disable "キャッシュを削除" (clearing a
        DB the builder is writing would corrupt the batched commits) while a
        background build is in flight.

        ``_bg_builder is None`` alone is NOT "nobody is writing" (項目#136):
        :meth:`_on_bg_build_finished` clears ``_bg_builder`` and then parks the
        builder in :attr:`_retiring` while its pools drain (項目16 の deferred
        destruction).  Those parked workers keep writing — ``_ThumbnailTask``
        の ``_produce_cached`` はキャンセルを見ずに ``disk_cache.store`` する —
        so a clear issued in that window reports a success the caches don't
        match (``ThumbDiskCache.clear`` は DELETE 後に store された行/blob を
        意図的に掃かない、#95)。:meth:`shutdown` は既に同じ非対称を手当て
        しているので、ゲート側もリタイア待ちを含めて判定する。
        """
        return self._bg_builder is not None or bool(self._retiring)

    #: The cache kinds ``clear_persistent_caches`` understands (I04 type-split
    #: clearing).  ``None`` (or this whole set) clears everything, matching the
    #: historical wipe-all behaviour.
    CLEAR_KINDS = ("thumb", "aspect", "folder", "search")

    def clear_persistent_caches(self, kinds: "set[str] | None" = None) -> None:
        """Wipe the selected on-disk caches and the relevant in-memory LRUs.

        ``kinds`` (I04) selects which caches to drop — a subset of
        :data:`CLEAR_KINDS` (``"thumb"`` / ``"aspect"`` / ``"folder"`` /
        ``"search"``).  ``None`` clears all of them (the historical behaviour).
        The in-memory thumbnail LRUs are flushed only when the thumbnail disk
        cache is in the set, since they hold decoded thumbnails; clearing just
        the search index leaves painted thumbnails untouched.  Already-painted
        thumbnails stay on screen; subsequent loads re-decode from the
        originals (and re-populate the caches).
        """
        selected = set(self.CLEAR_KINDS) if kinds is None else set(kinds)
        if "thumb" in selected and self._disk_cache is not None:
            self._disk_cache.clear()
        if "aspect" in selected and self._meta_cache is not None:
            self._meta_cache.clear()
        if "folder" in selected and self._folder_cache is not None:
            self._folder_cache.clear()
        if "search" in selected and self._search_index is not None:
            self._search_index.clear()
        if "thumb" in selected:
            for loader in self._thumbnail_loaders():
                loader.clear_cache()

    def _thumbnail_loaders(self) -> Sequence:
        """The loaders to flush *right now*.

        ``loaders`` may be a callable provider (the window's
        ``_all_loaders``); resolve it at use time so lazily-built loaders
        created after construction are included (レビュー 2026-09-03 項目 #45).
        """
        loaders = self._loaders
        return loaders() if callable(loaders) else loaders

    def build_cache_interactive(
        self, parent: QWidget, *,
        confirm_background: Callable[[], bool] | None = None,
        notify: bool = True,
    ) -> dict | None:
        """Prompt for a folder + mode, then warm the caches under it.

        With 「バックグラウンドで実行」 ticked in the mode prompt (seeded from
        ``cache_build_background``) the build runs in the **background** — this
        returns immediately with a sentinel result and the settings dialog
        closes while the build continues, its progress shown in the status bar
        with pause / resume / cancel controls.  Unticked, it falls back to the
        legacy **modal** progress dialog that blocks and returns the final
        ``{ok, failed, total}``.  Returns ``None`` if the user cancels the
        folder / mode prompt.

        *confirm_background* (UIレビュー 08-28 N-82) lets a caller whose own
        contract is broken by the background path veto it **before anything
        starts**: the settings dialog promises 「「OK」を押すと適用されます」 yet
        has to ``accept()`` itself (committing every edited tab) so the user can
        watch a background build it cannot supervise from behind an application
        -modal dialog.  It passes a callback that spells that consequence out;
        returning ``False`` aborts without starting a build.  The 診断メニュー
        path commits nothing and passes nothing.

        *notify* は**モーダル経路の完了通知**をここから出すかどうか（UIレビュー
        2026-09-11 N-11）。背景経路は元から cancel / 部分失敗 / 成功の 3 分岐を
        通知するのに、モーダル経路は結果を返すだけで無言だった — 戻り値を捨てる
        診断メニュー経路では「押したのに何も起きない」に見える。既定 ``True``
        でここから出し、**設定ダイアログだけ ``False``** を渡して自前の通知を
        残す: トーストは ``window.window()`` へ親付けされるので、``exec()`` の
        アプリケーションモーダルな設定ダイアログの**裏**に出てしまう。
        """
        # A background build already in flight: don't let a second one start
        # (both would fight for the shared caches' bulk-write batching).
        if self._bg_builder is not None:
            QMessageBox.information(
                parent, t("viewer.cache_build_controller.build_title"),
                t("viewer.cache_build_controller.build_running_body"),
            )
            return None
        prepared = self._prepare_cache_build(parent)
        if prepared is None:
            return None
        chosen, aspect_only, index_only, background = prepared

        if background:
            if confirm_background is not None and not confirm_background():
                return None
            self._start_background_build(chosen, aspect_only, index_only)
            # Sentinel: the settings dialog treats a non-None result as "done"
            # and shows a summary — suppress that for the background path.
            return {"background": True}

        return self._run_modal_build(
            parent, chosen, aspect_only, index_only, notify=notify,
        )

    # ----------------------------------------------------------- internals

    def _prepare_cache_build(
        self, parent,
    ) -> tuple[str, bool, bool, bool] | None:
        """Shared folder-pick + mode-select + validation for both build paths.

        Returns ``(chosen_folder, aspect_only, index_only, background)`` or
        ``None`` if the user cancelled or no cache can take the work.
        """
        if (
            self._disk_cache is None
            and self._meta_cache is None
            and self._search_index is None
        ):
            QMessageBox.information(
                parent, t("viewer.common.cache"),
                t("viewer.cache_build_controller.all_caches_disabled"),
            )
            return None
        root = self._current_root()
        start = str(root) if root else ""
        chosen = pick_existing_directory(
            parent, t("viewer.cache_build_controller.pick_folder_title"), start
        )
        if not chosen:
            return None

        answer = self._ask_cache_build_mode(parent, chosen)
        if answer is None:
            return None
        mode, background = answer
        aspect_only = mode == "aspect"
        index_only = mode == "index"
        # md-only mode warms just the search index — it's meaningless without
        # one.
        if index_only and self._search_index is None:
            QMessageBox.information(
                parent, t("viewer.common.cache"),
                t("viewer.cache_build_controller.index_disabled"),
            )
            return None
        # The search index builds in either mode, so only block when neither
        # the aspect cache nor the search index can take the work.
        if aspect_only and self._meta_cache is None and self._search_index is None:
            QMessageBox.information(
                parent, t("viewer.common.cache"),
                t("viewer.cache_build_controller.aspect_disabled"),
            )
            return None
        if not aspect_only and not index_only and self._disk_cache is None:
            extra = (
                t("viewer.cache_build_controller.and_search_index")
                if self._search_index else ""
            )
            QMessageBox.information(
                parent, t("viewer.common.cache"),
                t("viewer.cache_build_controller.disk_disabled", extra=extra),
            )
            aspect_only = True
        return chosen, aspect_only, index_only, background

    @staticmethod
    def _cache_build_phase(aspect_only: bool, index_only: bool) -> str:
        if index_only:
            return t("viewer.cache_build_controller.phase_index")
        if aspect_only:
            return t("viewer.cache_build_controller.phase_aspect")
        return t("viewer.cache_build_controller.phase_full")

    def _run_modal_build(
        self, parent, chosen: str, aspect_only: bool, index_only: bool,
        *, notify: bool = True,
    ) -> dict:
        """Legacy blocking build on a modal progress dialog (returns totals)."""
        builder = CacheBuilder(
            self._disk_cache, self._meta_cache,
            cache_edge=self._state.thumb_disk_cache_max_edge,
            full_parallelism=self._state.cache_build_full_parallelism,
            aspect_parallelism=self._state.cache_build_aspect_parallelism,
            walk_parallelism=self._state.cache_build_walk_parallelism,
            search_index=self._search_index,
            index_parallelism=self._state.search_index_build_parallelism,
            folder_cache=self._folder_cache,
            parent=self,
        )
        progress = QProgressDialog(
            t("viewer.cache_build_controller.scanning_folder"),
            t("common.action.cancel"), 0, 0, parent,
        )
        progress.setWindowTitle(t("viewer.cache_build_controller.build_title"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        result: dict = {}
        # キャンセル判定は「builder の ``finished`` より先にダイアログが閉じた
        # か」という **1 つの弁**に集約する（#139）。``QProgressDialog`` の
        # ``wasCanceled()`` は両方向に嘘をつくので使わない:
        #   ① Esc / タイトルバーの × / 明示的な ``reject()`` は
        #      ``QDialog::reject`` を通るだけで ``canceled()`` を出さない
        #      → ``wasCanceled()`` は False のまま。ビルドは UI を失ったまま
        #      走り続け、しかも「完了」扱いで走行中の builder と並行して
        #      ``_prune_caches_async`` が走っていた。
        #   ② 逆に正常完了で ``close()`` すると ``QProgressDialog::closeEvent``
        #      が ``canceled()`` を出す（コンストラクタが ``cancel()`` へ自動
        #      接続している）ので、以後 ``wasCanceled()`` は True を返す。
        # ``build_finished`` は ``on_finished`` が来たことだけを記録し、
        # ``user_cancelled`` は「未完了のまま exec を抜けた／未完了のまま
        # canceled が来た」で立つ。閉じ方が何であれ、この 2 値で決まる。
        build_finished = False
        user_cancelled = False
        phase = self._cache_build_phase(aspect_only, index_only)
        # Throttle dialog updates to ~50ms.  The builder emits ``progress`` once
        # per processed file; updating the *modal* dialog every time is fatal,
        # because ``QProgressDialog.setValue`` calls ``processEvents()`` on a
        # modal dialog, which re-enters this slot for the next queued worker
        # signal → ``setValue`` → ``processEvents`` → … Under the high-
        # parallelism build pools the queue never drains, so the re-entrancy
        # nests unboundedly and the UI freezes (and ``setLabelText`` relayouts
        # every file are pure overhead).  Coalescing to 50ms breaks the chain:
        # a re-entrant call inside the throttle window does no GUI work, so it
        # cannot recurse.
        #
        # The throttle is **purely time-based** — it must NOT special-case
        # ``done == total`` as an always-pass "terminal tick".  In the
        # index-only build node rows are counted synchronously, so ``done``
        # equals ``total`` on *every* emit; a ``done == total`` bypass would
        # therefore defeat the throttle entirely and reintroduce the freeze.
        # The bar reaching exactly 100% is handled by ``on_finished`` (which
        # closes the dialog), so no per-emit terminal pass is needed.
        last_update = 0.0

        def on_progress(done: int, total: int) -> None:
            nonlocal last_update
            now = time.monotonic()
            if (now - last_update) < 0.05:
                return
            last_update = now
            if total > 0:
                progress.setMaximum(total)
                progress.setLabelText(
                    t(
                        "viewer.cache_build_controller.modal_progress",
                        phase=phase, done=done, total=total,
                    )
                )
            progress.setValue(done)

        def on_finished(ok: int, failed: int, total: int) -> None:
            nonlocal build_finished
            build_finished = True
            result.update(ok=ok, failed=failed, total=total)
            progress.close()

        def note_cancelled() -> None:
            # [キャンセル] ボタン経路。完了後の ``close()`` も ``canceled()``
            # を出す（上記②）ので、``finished`` 済みの分は無視する。
            nonlocal user_cancelled
            if not build_finished:
                user_cancelled = True

        builder.progress.connect(on_progress)
        builder.finished.connect(on_finished)
        # 接続順が意味を持つ: ``builder.cancel`` は同期で ``finished`` を回して
        # ``build_finished`` を立てるので、``note_cancelled`` を先に通しておか
        # ないと自分自身のキャンセルを取りこぼす。
        progress.canceled.connect(note_cancelled)
        progress.canceled.connect(builder.cancel)
        builder.build(Path(chosen), aspect_only=aspect_only, index_only=index_only)
        try:
            progress.exec()
        finally:
            if not build_finished:
                # Esc / タイトルバーの × / 明示的な ``reject()``: ``canceled()``
                # が出ない閉じ方はここだけが入り口（#139）。ビルドは走行中な
                # ので、UI を失ったまま走り続けさせない。``cancel`` は同期で
                # ``finished`` を回すため、下の disconnect より **先に** 呼んで
                # 途中経過を ``result`` へ載せる = キャンセルボタン経路と同じ
                # 戻り値になる（空のままだと「0 件完了」と誤報告される）。
                user_cancelled = True
                builder.cancel()
            # The builder OUTLIVES this dialog (a cancel leaves in-flight
            # decodes emitting ``progress`` until the pools drain), and the
            # slots are plain closures — Qt cannot auto-disconnect them when
            # the dialog dies.  Cut the connections INTO the dialog before
            # destroying it, or a late emit calls setValue() on a freed C++
            # object.
            builder.progress.disconnect(on_progress)
            builder.finished.disconnect(on_finished)
            # A parented QProgressDialog survives ``exec`` — the parent owns
            # the C++ side — so without this every 診断 ▸ キャッシュを事前作成…
            # run would leave another hidden dialog under the window (#125 と
            # 同型).  ``deleteLater``, never ``WA_DeleteOnClose``: the latter
            # frees the C++ object from inside the close signal's emission
            # stack, which is the #111 crash shape.
            progress.deleteLater()
        # NOT a bare ``deleteLater()``: a user cancel leaves in-flight decodes
        # running, and destroying the builder's child QThreadPools then blocks
        # the GUI unbounded (項目16) — defer until the pools drain.
        self._retire_builder(builder)
        if not user_cancelled:
            # A full build can overshoot the disk-cache byte budget by a wide
            # margin (prune otherwise only runs at startup / settings-apply);
            # enforce it now that the write burst is over.
            self._prune_caches_async()
        totals = result or {"ok": 0, "failed": 0, "total": 0}
        # 背景経路 (:_on_bg_build_finished) と同じ 3 分岐・同じキー・同じ
        # ``duration_ms`` で通知する（モーダル経路だけが無言だった — N-11）。
        # teardown 中は出さない不変条件も背景経路と揃える。
        if notify and not self._tearing_down:
            ok = totals.get("ok", 0)
            failed = totals.get("failed", 0)
            if user_cancelled:
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_cancelled_toast",
                        done=ok + failed,
                    ),
                    "info",
                )
            elif failed > 0:
                # 部分失敗だけ duration_ms=0（数分かかるビルドの「N 件失敗」を
                # 3 秒で消さない）。
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_done_warn_toast",
                        ok=ok, failed=failed, total=totals.get("total", 0),
                    ),
                    "warning",
                    duration_ms=0,
                )
            else:
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_done_toast",
                        ok=ok, total=totals.get("total", 0),
                    ),
                    "success",
                )
        # N-29: 実際に走ったモード（モーダル）を呼び出し側へ返す。設定
        # ダイアログはこれを見て自分のチェックボックスを実態へ合わせる。
        totals.setdefault("background", False)
        # N-11: ``notify=False`` で自前通知する呼び出し側（設定ダイアログ）も
        # キャンセルを「完了」と誤報告しないよう、判定結果を載せる。
        totals["cancelled"] = user_cancelled
        return totals

    # ------------------------------------------------- background cache build

    def _start_background_build(
        self, chosen: str, aspect_only: bool, index_only: bool,
    ) -> None:
        """Kick off a non-modal build whose progress rides the status bar.

        Uses the **background-specific** low-parallelism knobs: the build now
        shares the NAS SMB-credit window with the live thumb / metadata /
        probe pools, so it must stay well below the exclusive-modal knobs.
        """
        phase = self._cache_build_phase(aspect_only, index_only)
        builder = CacheBuilder(
            self._disk_cache, self._meta_cache,
            cache_edge=self._state.thumb_disk_cache_max_edge,
            full_parallelism=self._state.cache_build_bg_full_parallelism,
            aspect_parallelism=self._state.cache_build_bg_aspect_parallelism,
            walk_parallelism=self._state.cache_build_bg_walk_parallelism,
            search_index=self._search_index,
            index_parallelism=self._state.cache_build_bg_index_parallelism,
            folder_cache=self._folder_cache,
            parent=self,
        )
        self._bg_builder = builder
        self._bg_build_phase = phase
        self._bg_build_cancelled = False
        builder.progress.connect(self._on_bg_build_progress)
        builder.finished.connect(self._on_bg_build_finished)
        self._status_widget.begin(phase)
        builder.build(Path(chosen), aspect_only=aspect_only, index_only=index_only)

    def _on_bg_build_progress(self, done: int, total: int) -> None:
        # The status widget self-throttles (100ms); no processEvents here.
        self._status_widget.update_progress(done, total)

    def _on_bg_build_finished(self, ok: int, failed: int, total: int) -> None:
        cancelled = self._bg_build_cancelled
        self._status_widget.finish(ok, failed, cancelled)
        if cancelled:
            # I06: a cancel used to vanish silently — surface the partial result
            # so the user knows the cancel took and how far it got.  C13: but a
            # cancel that came from ``shutdown`` teardown (window closing) stays
            # silent — the shutdown docstring's "teardown 中はメッセージを
            # 出さない" invariant must hold, and the host window is being
            # destroyed anyway.  (Either way this is NOT the completion branch,
            # so the success toast + post-build prune below are correctly
            # skipped.)
            if not self._tearing_down:
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_cancelled_toast",
                        done=ok + failed,
                    ),
                    "info",
                )
        else:
            # I06: promote the completion note from a 5s status line to a toast
            # (right-corner, harder to miss).  A partial failure (failed>0) is a
            # warning so the count stands out and points at the log.
            # (UIレビュー 07-25 #10) その部分失敗だけは duration_ms=0 —
            # 数分〜数十分かかるビルドの「N 件失敗」が 3 秒で自動消滅すると、
            # 離席したユーザーは失敗の事実ごと失う（クリックするまで残す）。
            if failed > 0:
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_done_warn_toast",
                        ok=ok, failed=failed, total=total,
                    ),
                    "warning",
                    duration_ms=0,
                )
            else:
                self._toast(
                    t(
                        "viewer.cache_build_controller.build_done_toast",
                        ok=ok, total=total,
                    ),
                    "success",
                )
            # Enforce the byte budgets now that the build's write burst is
            # over — a full build can exceed the disk-cache budget severalfold
            # and would otherwise stay bloated until the next startup prune.
            # Skipped on cancellation: the shutdown path cancels the build
            # right before closing the caches, and pruning would race that
            # close (a user cancel just defers the prune to the next start).
            self._prune_caches_async()
        builder = self._bg_builder
        self._bg_builder = None
        self._bg_build_phase = ""
        self._bg_build_cancelled = False
        if builder is not None:
            # Deferred destruction (項目16): a user cancel reaches here with
            # decodes still in flight, and QThreadPool's destructor would
            # block the GUI unbounded on them (NAS read + WebP encode; SMB
            # timeout if the NAS hangs).  Delete only once the pools drain.
            self._retire_builder(builder)

    def _retire_builder(self, builder: CacheBuilder) -> None:
        """``deleteLater`` *builder* only once its worker pools are idle.

        QThreadPool のデストラクタは in-flight QRunnable を**無制限**に待つ
        （``waitForDone`` 相当）。キャンセル直後は in-flight のデコード/
        プローブ/post 読みが残っているため、即 ``deleteLater()`` すると次の
        イベントループ周回で GUI がその完了までフリーズする（項目16）。
        ここでは非ブロッキングの :meth:`CacheBuilder.pools_idle` を 200ms
        ポーリングし、掃けてから破棄する。GUI スレッドは一切待たない。
        """
        if builder.pools_idle():
            builder.deleteLater()
            return
        self._retiring.append(builder)
        self._retire_timer.start()

    def _poll_retired(self) -> None:
        still: list[CacheBuilder] = []
        for b in self._retiring:
            if b.pools_idle():
                b.deleteLater()
            else:
                still.append(b)
        self._retiring = still
        if not still:
            self._retire_timer.stop()

    def _prune_caches_async(self) -> None:
        """LRU-prune the build-written caches back under budget, off-thread.

        A build writes the thumbnail disk cache (full mode) and the aspect
        cache without ever checking their byte budgets; ``prune`` is
        otherwise only called at startup and on settings-apply, so a big
        build could leave the caches far over budget for the rest of the
        session.  Runs on a daemon thread because pruning a multi-GiB
        overshoot on the disk cache unlinks thousands of blob files.  Both
        stores are opened ``check_same_thread=False`` and guard every access
        with their shared ``RLock`` (ThumbMetaCache was aligned to this in
        item 12 — it was previously ``check_same_thread=True`` and its
        off-thread ``prune`` silently raised ``ProgrammingError`` and did
        nothing), so this daemon call is legal; a race against an app-close
        ``close()`` merely aborts the prune (cache data is re-generatable,
        and the next startup prune finishes the job).
        """
        disk = self._disk_cache
        meta = self._meta_cache
        # ビルドは FolderPreviewCache も温めるようになった（#40）ので、
        # 同じ後始末でバイト枠へ戻す（枠は cache 自身が保持）。
        folder = self._folder_cache
        # ThumbDiskCache carries its own budget; ThumbMetaCache.prune takes
        # the budget explicitly — snapshot it here (GUI thread) so the worker
        # never touches ViewerState.
        aspect_budget = int(self._state.aspect_cache_max_mib) * 1024 * 1024

        def _prune() -> None:
            try:
                if disk is not None:
                    disk.prune()
                if meta is not None:
                    meta.prune(aspect_budget)
                if folder is not None:
                    folder.prune()
            except Exception as exc:  # pragma: no cover (best-effort cleanup)
                logger.debug("post-build cache prune failed: {}", exc)

        threading.Thread(
            target=_prune, name="cache-build-prune", daemon=True,
        ).start()

    def _toast(
        self, message: str, kind: str, *, duration_ms: int | None = None,
    ) -> None:
        """Raise a non-modal toast on the host window, if a notifier is wired.

        ``None`` notifier (tests / standalone) makes this a no-op so the build
        path stays headless-friendly.

        ``duration_ms`` (UIレビュー 07-25 #10) pins the toast's lifetime — ``0``
        keeps it up until the user clicks it, which a multi-minute build's
        partial-failure notice needs (a 3 秒 toast is lost outright when the
        user stepped away).  ホスト側の notifier が ``duration_ms`` を受ける
        ようになった（``ViewerWindow._show_toast`` — UIレビュー 07-25 の
        フォローアップ）ので、**常に notifier 1 本を通す**: 以前はここだけが
        親ウィンドウへ ``show_toast`` を直接呼ぶ回避実装を持っており、
        「トーストの単一ファネル」という設計上の約束を崩していた。古い
        2 引数 notifier（外部テストのスタブ等）も引き続き通す。

        (UIレビュー07-25 追修) 旧 notifier の判別は ``try: 3 引数呼び出し /
        except TypeError: 2 引数で再呼び出し`` だったが、これは **3 引数
        notifier の内部で起きた TypeError** まで飲み込み、その上でトーストを
        二重に出していた。呼び分けは署名（arity）で先に決め、notifier 自身の
        例外はそのまま外へ抜けさせる。
        """
        if self._notify is None:
            return
        style = "none"
        if duration_ms is not None:
            style = self._notifier_duration_style()
        if style == "kw":
            self._notify(message, kind, duration_ms=duration_ms)
        elif style == "pos":
            self._notify(message, kind, duration_ms)
        else:
            self._notify(message, kind)

    def _notifier_duration_style(self) -> str:
        """How the notifier takes ``duration_ms``: ``kw`` / ``pos`` / ``none``。

        署名の照会は 1 回だけ（結果をインスタンスに覚える）。イントロスペクト
        できない呼び出し可能物（C 実装等）は旧 2 引数契約とみなす — 無音より
        「既定寿命で 1 回通知」へ倒す方が安全側。

        受け取り方まで見るのが要点。以前は位置 3 引数（``bind("", "", 0)``）
        だけを試しており、``def notify(msg, kind, *, duration_ms=None)`` の
        ような**キーワード専用**の notifier が旧 2 引数扱いに落ちて、常駐指定
        （``duration_ms=0``）が黙って捨てられていた。キーワードを先に試し、
        通らなければ位置（``duration_ms`` という名前でない古い 3 引数）を試す。
        """
        if self._notify_duration_style is None:
            self._notify_duration_style = self._probe_duration_style()
        return self._notify_duration_style

    def _probe_duration_style(self) -> str:
        """One-shot signature probe behind :meth:`_notifier_duration_style`."""
        try:
            sig = inspect.signature(self._notify)
        except (TypeError, ValueError):  # pragma: no cover (署名不明の callable)
            return "none"
        for style, args, kwargs in (
            ("kw", ("", ""), {"duration_ms": 0}),
            ("pos", ("", "", 0), {}),
        ):
            try:
                sig.bind(*args, **kwargs)
            except TypeError:
                continue
            return style
        return "none"

    def _on_pause_toggled(self, paused: bool) -> None:
        builder = self._bg_builder
        if builder is None:
            return
        if paused:
            builder.pause()
        else:
            builder.resume()
        # Confirm the actual state back onto the widget.
        self._status_widget.set_paused(builder.is_paused())

    def _on_cancel_requested(self) -> None:
        builder = self._bg_builder
        if builder is None:
            return
        self._bg_build_cancelled = True
        builder.cancel()  # synchronously emits ``finished`` → cleanup runs

    def _ask_cache_build_mode(self, parent, chosen: str) -> tuple[str, bool] | None:
        """Prompt for the build mode.  Returns ``(mode, background)`` or ``None``.

        *mode* is ``'full'``/``'aspect'``/``'index'``; *background* is the
        「バックグラウンドで実行」 answer.  (UIレビュー 08-28 N-80) That choice
        used to live **only** in the settings dialog's cache tab, so whoever
        started a build from 診断 ▸ キャッシュを事前作成… could not decide, in
        the moment, whether the build would seize the window in a modal — a
        per-run decision parked in a persistent setting.  ``QMessageBox``
        carries a checkbox natively, so it rides along with the mode buttons
        here; the persisted ``cache_build_background`` seeds it.

        *chosen* は直前に選ばれた対象フォルダ（UIレビュー 2026-09-11 N-50）。
        「どこに対して作るのか」がこのモーダルから読めず、直前のフォルダ
        ピッカーの記憶だけが頼りだった。パスは省略せずそのまま出す
        （対象を誤認させないため — 折り返しは ``QMessageBox`` に任せる）。

        チェックボックスの文言は**このモーダル専用のキー**を使う（N-29）:
        設定タブと同じ文言を貼っていたため「永続設定を編集している」と読め、
        実際には今回のビルドにしか効かなかった。実際に走ったモードは
        :meth:`build_cache_interactive` の戻り値の ``background`` に載るので、
        設定ダイアログはそれを見て自分のチェックボックスを合わせる
        （このプロンプト自身は永続値を書き戻さない）。
        """
        box = QMessageBox(parent)
        box.setWindowTitle(t("viewer.cache_build_controller.build_title"))
        box.setIcon(QMessageBox.Question)
        box.setText(
            t("viewer.cache_build_controller.mode_prompt_with_target", path=chosen)
        )
        box.setInformativeText(t("viewer.cache_build_controller.mode_info"))
        bg_check = QCheckBox(t("viewer.cache_build_controller.bg_check_once"))
        bg_check.setChecked(bool(self._state.cache_build_background))
        bg_check.setToolTip(t("viewer.cache_build_controller.bg_tooltip_once"))
        box.setCheckBox(bg_check)
        index_btn = box.addButton(
            t("viewer.cache_build_controller.mode_btn_index"),
            QMessageBox.AcceptRole,
        )
        aspect_btn = box.addButton(
            t("viewer.cache_build_controller.mode_btn_aspect"),
            QMessageBox.AcceptRole,
        )
        full_btn = box.addButton(
            t("viewer.cache_build_controller.mode_btn_full"),
            QMessageBox.AcceptRole,
        )
        box.addButton(t("common.action.cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(aspect_btn)
        # Read the answers INSIDE the try, before the dialog is destroyed:
        # ``clickedButton`` / the checkbox live on the box (#125 と同型 —
        # 親付きなので exec を抜けてもウィンドウの隠れ子として残る).
        # ``deleteLater``, never ``WA_DeleteOnClose`` (#111 型の即時解放を作る).
        try:
            box.exec()
            clicked = box.clickedButton()
            background = bg_check.isChecked()
        finally:
            box.deleteLater()
        if clicked is index_btn:
            return "index", background
        if clicked is aspect_btn:
            return "aspect", background
        if clicked is full_btn:
            return "full", background
        return None

    # ------------------------------------------------------------- teardown

    def shutdown(self, timeout_ms: int) -> None:
        """Tear down an in-flight background build (``closeEvent`` contract).

        Must run BEFORE the window closes the caches the builder writes to,
        or a still-running decode worker would call ``disk_cache.store``
        after ``close``.  ``cancel`` flushes the batched commits (via
        ``set_bulk_writes(False)`` in ``_emit_finished``); ``wait_for_pools``
        then drains the worker pools with a bounded wait so no writer
        survives past the caches' close.  ``_bg_build_cancelled`` is set
        first so the (synchronous) ``finished`` from cancel is treated as a
        cancellation and doesn't post a completion status message during
        teardown; ``_tearing_down`` is raised alongside it so that same
        ``finished`` also suppresses the cancel toast (C13) — teardown must
        stay silent on the being-destroyed window.

        **リタイア待ちの builder も必ずドレインする**: ユーザーがキャンセル
        したビルドは in-flight が残る間 :attr:`_retiring` に退避され（項目16
        の deferred destruction）、``_bg_builder`` は既に ``None`` になって
        いる。ここを素通りすると、その窓でウィンドウを閉じたときにパーク中
        のワーカーが close 済みの disk_cache / search_index へ書き込む
        （scanning.md の「close 前に writer を残さない」順序契約が破れる）。
        残り時間を共有デッドラインとして按分し、GUI を無制限に待たせない
        （項目#41）: ``wait_for_pools`` は受け取った ms を「今から」の持ち時間
        として自前で絶対デッドラインへ変換するので、各 builder に
        ``timeout_ms`` を満額で渡すと ``1 + len(_retiring)`` 本ぶんが直列に
        積み上がる。ここで 1 本の絶対デッドラインを持ち、残り時間だけを渡す。
        """
        end = time.monotonic() + max(0, timeout_ms) / 1000.0

        def _remaining_ms() -> int:
            return max(0, int((end - time.monotonic()) * 1000))

        builder = self._bg_builder
        if builder is not None:
            self._tearing_down = True
            self._bg_build_cancelled = True
            builder.cancel()
            # ここは closeEvent 限定なので one-way の decode シャットダウンで
            # よい: パーク中の動画デコードを解放しないと下の bounded 待ちが
            # タイムアウトし、close 済みキャッシュへの書き込み・プロセス
            # 常駐（#69）につながる。
            builder.request_decode_shutdown()
            if not builder.wait_for_pools(_remaining_ms()):
                logger.warning(
                    "キャッシュ構築のワーカーが close の予算内に掃けませんでした"
                    "（走行中の書き込みが残っています）"
                )
            self._bg_builder = None
        # 先に全 builder の decode を解放してから待つ（順に「解放→満了待ち」を
        # 繰り返すと、後続ぶんは解放が遅れたぶんだけ余計に残る）。
        for retired in self._retiring:
            # 既に cancel 済み（リタイアは cancel/完了の後にしか起きない）
            # なので、ここは残 in-flight の解放と完了待ちだけでよい。
            retired.request_decode_shutdown()
        # 掃けたものだけ手放す。掃けなかった builder を無条件に捨てると、
        # 参照が消えた時点で C++ の ``~QThreadPool`` が**無制限に**走行中の
        # ``QRunnable`` を待つ（有界待ちで諦めた意味が無くなる）。
        stuck: list[CacheBuilder] = []
        for retired in self._retiring:
            if retired.wait_for_pools(_remaining_ms()):
                retired.deleteLater()
            else:
                stuck.append(retired)
        if stuck:
            logger.warning(
                "リタイア中のキャッシュ構築 {} 本が close の予算内に掃けません"
                "でした（掃けるまで保持します）", len(stuck),
            )
        self._retiring = stuck
        self._retire_timer.stop()


__all__ = ["CacheBuildController"]
