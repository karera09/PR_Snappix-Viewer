"""Status-bar progress widget for the background cache build.

A compact, horizontal strip — label ("キャッシュ作成中 N / M") + a mini
progress bar + pause/resume + cancel buttons — shown as a permanent status-bar
widget while a :class:`~snappix.viewer.cache_builder.CacheBuilder` runs in the
background (non-modal), so the user keeps browsing while the caches warm.

The widget owns no build logic: it renders progress and forwards button
clicks via signals.  :class:`~snappix.viewer.main_window.ViewerWindow` drives it
(``begin`` / ``update_progress`` / ``finish``) and wires the signals to the
builder.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QToolButton,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import set_icon

# Progress-label / bar updates are coalesced to this window.  The builder emits
# ``progress`` once per processed file (or per node in index-only mode), which
# on a large library is tens of thousands of signals — repainting the label +
# bar every time is wasted work even though (unlike the modal QProgressDialog)
# these updates never call ``processEvents``.  Throttling keeps the status bar
# cheap without the re-entrancy hazard the modal dialog had.
_UPDATE_INTERVAL = 0.1


class CacheBuildStatusWidget(QWidget):
    """Compact background-build progress strip for the status bar.

    Signals:

    * ``pause_toggled(bool paused)`` — user clicked the pause/resume button;
      the argument is the *desired* new paused state.
    * ``cancel_requested()`` — user clicked the cancel button.
    """

    pause_toggled = Signal(bool)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paused = False
        # ``begin()`` sets the real phase; initialising it here too means every
        # entry point works before a build starts, rather than only the ones
        # the update throttle happens to skip.
        self._phase = ""
        self._last_update = 0.0
        self._last_done = 0
        self._last_total = 0

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._label = QLabel("")
        row.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setFixedWidth(120)
        self._bar.setTextVisible(False)
        self._bar.setRange(0, 0)  # busy/indeterminate until the first total
        row.addWidget(self._bar)

        # 絵文字グリフ（⏸ / ▶ / ✕）+ 箱型 QPushButton ではなく、アイコンは
        # icons.py の SVG・ステータスバーの補助操作はフラットなツールボタン
        # という規約に従い QToolButton + set_icon で組む（テーマ切替時の
        # 再着色も set_icon の登録に乗る）。
        self._pause_btn = QToolButton()
        set_icon(self._pause_btn, "pause")
        self._pause_btn.setToolTip(t("viewer.cache_build_status.tooltip_pause"))
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        row.addWidget(self._pause_btn)

        self._cancel_btn = QToolButton()
        set_icon(self._cancel_btn, "x")
        self._cancel_btn.setToolTip(t("common.action.cancel"))
        self._cancel_btn.clicked.connect(self.cancel_requested)
        row.addWidget(self._cancel_btn)

        self.setVisible(False)

    # ------------------------------------------------------------- lifecycle

    def begin(self, phase: str) -> None:
        """Show the widget for a fresh build.  *phase* labels the mode."""
        self._phase = phase
        self._paused = False
        self._last_update = 0.0
        self._last_done = 0
        self._last_total = 0
        set_icon(self._pause_btn, "pause")
        self._pause_btn.setToolTip(t("viewer.cache_build_status.tooltip_pause"))
        self._pause_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        self._bar.setRange(0, 0)
        self._label.setText(t("viewer.cache_build_status.starting", phase=phase))
        self.setVisible(True)

    def update_progress(self, done: int, total: int) -> None:
        """Throttled progress render (100ms).  ``total`` may grow over time.

        Named ``update_progress`` (not ``update``) so it does not shadow
        ``QWidget.update()``, Qt's argument-less repaint slot.
        """
        now = time.monotonic()
        if (now - self._last_update) < _UPDATE_INTERVAL:
            return
        self._last_update = now
        self._render(done, total)

    def _render(self, done: int, total: int) -> None:
        # Remember the most recently rendered figures so a state change that
        # carries no new progress (``set_paused``) can re-render the label.
        self._last_done = done
        self._last_total = total
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(min(done, total))
        suffix = (
            t("viewer.cache_build_status.paused_suffix") if self._paused else ""
        )
        if total > 0:
            self._label.setText(
                t(
                    "viewer.cache_build_status.progress",
                    phase=self._phase,
                    done=done,
                    total=total,
                    suffix=suffix,
                )
            )
        else:
            self._label.setText(
                t(
                    "viewer.cache_build_status.progress_indeterminate",
                    phase=self._phase,
                    suffix=suffix,
                )
            )

    def finish(self, ok: int, failed: int, cancelled: bool) -> None:
        """Hide the strip; the window owns any completion message.

        This widget shows no completion note of its own — on both success and
        cancel it just hides.  The ``ViewerWindow`` posts the transient "完了"
        (or omits it on cancel) via ``statusBar().showMessage``.  ``ok`` /
        ``failed`` / ``cancelled`` are accepted to mirror the builder's
        finished payload (and keep the door open for an in-widget summary
        later) but are deliberately unused here.
        """
        del ok, failed, cancelled  # intentionally unused — see docstring
        self.setVisible(False)

    # ---------------------------------------------------------------- paused

    def set_paused(self, paused: bool) -> None:
        """Reflect the builder's paused state on the button + label."""
        self._paused = bool(paused)
        # 状態表現も SVG グリフの差し替えで行う。
        if self._paused:
            set_icon(self._pause_btn, "play")
            self._pause_btn.setToolTip(t("viewer.cache_build_status.tooltip_resume"))
        else:
            set_icon(self._pause_btn, "pause")
            self._pause_btn.setToolTip(t("viewer.cache_build_status.tooltip_pause"))
        # Render the paused/resumed suffix immediately using the most recent
        # done/total so "(一時停止中)" appears without waiting for the next
        # throttled progress tick.  In index-only mode no ``progress`` is
        # emitted while paused (node batches are buffered), so relying on the
        # next tick would leave the suffix off the label entirely.
        self._last_update = time.monotonic()
        self._render(self._last_done, self._last_total)

    def _on_pause_clicked(self) -> None:
        # Emit the desired new state; the window applies it to the builder and
        # calls back into ``set_paused`` to confirm.
        self.pause_toggled.emit(not self._paused)


__all__ = ["CacheBuildStatusWidget"]
