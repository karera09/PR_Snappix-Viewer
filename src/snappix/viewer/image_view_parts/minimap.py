"""中央プレビューのミニマップオーバーレイ.

拡大中の画像のどこを見ているかを右下の小さな縮小図で示し、クリック /
ドラッグで ``panned(fx, fy)`` を出す部品。表示の可否（``set_eligible``）と
活動の申告（``bump_activity``）だけを外から受け取り、サムネの生成・
スクロール位置の計算は所有者（``image_view.ImageView``）が持つ。

依存の向きは **部品 → ビューは無し**（``image_view`` を import しない）。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ...common.ui import overlay
from ...common.ui.timers import DebounceMode, Debouncer


class _MinimapOverlay(QWidget):
    """Bottom-right minimap showing the visible viewport rect over the image.

    Displayed only while the image is zoomed past the viewport (i.e. the
    scroll area actually has something to scroll) *and* only while the user
    is actively interacting — mirrors the zoom pill's auto-hide pattern
    (``control_bar._ZoomOverlayLabel``).  :meth:`bump_activity` re-shows the
    widget (if the pannable
    precondition holds) and restarts a 1.5s idle timer; hovering or
    left-dragging over the minimap itself also counts as activity so it
    doesn't vanish mid-interaction.  The thumbnail pixmap is provided by the
    owner (:class:`ImageView`) — this widget never decodes or rescales the
    source image itself, it only paints whatever ``QPixmap`` it was handed
    plus a viewport rectangle on top.
    """

    _MAX_EDGE = 180
    _MARGIN = 10
    _AUTO_HIDE_MS = 1500

    panned = Signal(float, float)  # fractional (x, y) center requested

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._view_fx0 = 0.0
        self._view_fy0 = 0.0
        self._view_fx1 = 1.0
        self._view_fy1 = 1.0
        # True once the pannable/enabled preconditions hold — controls
        # whether ``bump_activity`` is allowed to (re)show the widget at
        # all.  Set via :meth:`set_eligible`.
        self._eligible = False
        self._timer = Debouncer(
            self, self._AUTO_HIDE_MS, self.hide, mode=DebounceMode.TRAILING
        )
        # Needed so ``mouseMoveEvent``/hover fire without a button held down
        # — plain hovering over the minimap should also count as activity.
        self.setMouseTracking(True)
        self.hide()

    def set_eligible(self, eligible: bool) -> None:
        """Update whether the minimap is allowed to be shown at all.

        Called whenever pannable/enabled state changes.  Turning eligibility
        off hides immediately and stops the idle timer; turning it on does
        *not* show by itself — the next :meth:`bump_activity` call does.
        """
        self._eligible = eligible
        if not eligible:
            self._timer.stop()
            self.hide()

    def bump_activity(self) -> None:
        """Show (if eligible) and restart the auto-hide idle timer."""
        if not self._eligible:
            return
        self.show()
        self.raise_()
        self._timer.trigger()

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        if pixmap is not None and not pixmap.isNull():
            w = pixmap.width()
            h = pixmap.height()
            scale = min(self._MAX_EDGE / max(1, w), self._MAX_EDGE / max(1, h), 1.0)
            self.resize(max(1, round(w * scale)), max(1, round(h * scale)))
        self.update()

    def set_view_rect(self, fx0: float, fy0: float, fx1: float, fy1: float) -> None:
        """Update the fractional (0..1) viewport rectangle and repaint."""
        self._view_fx0 = fx0
        self._view_fy0 = fy0
        self._view_fx1 = fx1
        self._view_fy1 = fy1
        self.update()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = parent.width() - self.width() - self._MARGIN
        y = parent.height() - self.height() - self._MARGIN
        self.move(max(0, x), max(0, y))

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()
        painter.fillRect(rect, overlay.MINIMAP_BACKDROP)
        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(rect, self._pixmap)
        w = rect.width()
        h = rect.height()
        vr = QRect(
            round(self._view_fx0 * w),
            round(self._view_fy0 * h),
            max(1, round((self._view_fx1 - self._view_fx0) * w)),
            max(1, round((self._view_fy1 - self._view_fy0) * h)),
        )
        pen = painter.pen()
        pen.setColor(overlay.MINIMAP_VIEW_RECT)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(vr.adjusted(1, 1, -1, -1))
        painter.end()

    def _emit_pan_for(self, pos: QPoint) -> None:
        w = max(1, self.width())
        h = max(1, self.height())
        fx = max(0.0, min(1.0, pos.x() / w))
        fy = max(0.0, min(1.0, pos.y() / h))
        self.panned.emit(fx, fy)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.bump_activity()
        if event.button() == Qt.MouseButton.LeftButton:
            self._emit_pan_for(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.bump_activity()
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._emit_pan_for(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Hovering counts as activity so the minimap doesn't fade away while
        # the cursor is sitting on top of it deciding where to click.
        self.bump_activity()
        super().enterEvent(event)
