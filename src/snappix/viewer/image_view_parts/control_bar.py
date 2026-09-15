"""中央プレビューのズーム読み値と操作カプセル.

画像の上に浮く「読み値 + 操作」のクローム部品:

* :data:`ZOOM_READOUT_EMPTY` / :func:`format_zoom_readout` — 常設のズーム
  読み値の文字列（プレビューと全画面のカプセルが同じ形で出すための共通化）
* :class:`_ZoomOverlayLabel` — ズーム操作の直後だけ右下に出る一時ピル
* :class:`_ControlBar` — 下端中央にホバーで現れる操作カプセル（前後送り /
  フィット⇄実寸 / 全画面 + ズーム率）

固定の暗スクリム + 明色グリフはテーマに追従しない（画像上オーバーレイの
固定色例外 — docs/claude/design.md）。スクリム QSS・``WA_StyledBackground``・
親矩形クランプ・ボタン生成は :class:`~snappix.common.ui.overlay_chrome.OverlayCapsule`
が持ち、ここにはフェードとアイドル消灯の方針、画像矩形への寄せだけを置く。

依存の向きは **部品 → ビューは無し**（``image_view`` を import しない）。
"""

from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QWidget,
)

from ...common.ui import fixed_icon, overlay, overlay_chrome
from ...common.ui.overlay_chrome import OverlayCapsule
from ...common.ui.timers import DebounceMode, Debouncer


#: ズーム読み値の「値なし」表示（画像が無い / デコード失敗）。数字と単位だけ
#: の読み値なので i18n カタログではなく記号で表す（``%`` と同じ扱い）。
ZOOM_READOUT_EMPTY = "—"


def format_zoom_readout(percent: float | None) -> str:
    """常設のズーム読み値を作る。値が無いときは :data:`ZOOM_READOUT_EMPTY`。

    プレビューと全画面のカプセルが同じ文字列を出すための共通化。0 以下は
    「表示していない」の合図で、``ImageView.zoom_changed`` もその値を流す
    （シグナルは ``float`` なので ``None`` を運べない）。
    """
    if percent is None or percent <= 0:
        return ZOOM_READOUT_EMPTY
    return f"{round(percent)}%"


class _ZoomOverlayLabel(QLabel):
    """Semi-transparent rounded pill showing the current effective zoom.

    Lives as a child of the viewport so it floats above the image without
    participating in the scroll area's layout.  Auto-hides ~1.5s after
    the last :meth:`show_zoom` call via a single-shot timer; repeated
    calls simply restart the timer rather than flashing on/off.
    """

    _AUTO_HIDE_MS = 1500

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            "QLabel {"
            f" background: {overlay.rgba_str(overlay.SCRIM_CHIP)};"
            f" color: {overlay.CTRL_ICON};"
            " padding: 3px 8px;"
            " border-radius: 8px;"
            " font-weight: bold;"
            "}"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._timer = Debouncer(
            self, self._AUTO_HIDE_MS, self.hide, mode=DebounceMode.TRAILING
        )

    def show_zoom(self, percent: float) -> None:
        self.setText(f"{round(percent)}%")
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._timer.trigger()

    def reposition(self) -> None:
        if self.isVisible():
            self._reposition()

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        margin = 10
        x = parent.width() - self.width() - margin
        y = parent.height() - self.height() - margin
        self.move(max(0, x), max(0, y))


# Fixed light colour for the on-screen control bar glyphs.  The bar is an
# image-content overlay (design.md 使用ルール2 exception) so it keeps a fixed
# dark scrim + light chrome regardless of theme, exactly like the lightbox.
# Sourced from the single overlay palette (common/ui/overlay.py).
_CTRL_ICON_COLOR = overlay.CTRL_ICON

# Control-bar button glyph names, resolved against the SINGLE glyph table in
# ``common/ui/icons.py`` — this table holds *names*, never SVG bodies, so the
# capsule and the lightbox cannot drift into two drawings of one verb.
# 「フィット / 実寸」 must stay out of ``maximize``'s vocabulary (四隅の角括弧 =
# この席を広げる): ``fit-frame`` (外枠の中の小矩形 = 画像を枠に収める) keeps the
# toggle in the *zoom* vocabulary, and 全画面 shares one glyph with the stage
# header.  The fixed colour comes from the overlay palette (design.md
# 使用ルール 2) via ``icons.fixed_icon``.
#
# 前後送りは chevron ではなく**軸線付きの矢印**: chevron-left/right はステージ
# ヘッダーの ‹前へ / 次へ›（グリッドの項目を歩む別の軸）が使うので、カプセル =
# ファイル送り、ヘッダー = 項目送り、を図像でも弁別する。
_CTRL_GLYPHS: dict[str, str] = {
    "prev": "arrow-left",
    "next": "arrow-right",
    "fit": "fit-frame",
    "fullscreen": "expand",
}


def _ctrl_icon(name: str, size: int = 18) -> QIcon:
    """Render a fixed-colour control-bar glyph to a crisp 1x/2x QIcon."""
    return fixed_icon(_CTRL_GLYPHS[name], _CTRL_ICON_COLOR, size=size)


class _ControlBar(OverlayCapsule):
    """Hover-revealed on-screen controls for the image preview.

    A semi-transparent rounded bar pinned to the bottom-centre of the
    viewport with prev / next / fit-actual / fullscreen buttons and a live
    zoom-percent label.  It only appears on mouse activity over the image
    (``reveal``) and fades out after an idle timeout, so keyboard-driven use
    is never obstructed.  Fixed dark scrim + light glyphs (image-overlay
    colour exception — see design.md 使用ルール2).

    The scrim / button styling, the ``WA_StyledBackground`` attribute Qt
    needs before it paints that scrim at all and the parent-rect clamp come
    from :class:`~snappix.common.ui.overlay_chrome.OverlayCapsule`, shared
    with the lightbox capsule.  Only the fade + idle-timer reveal policy and
    the image-rect anchoring are local.
    """

    prev_clicked = Signal()
    next_clicked = Signal()
    fit_toggle_clicked = Signal()
    fullscreen_clicked = Signal()

    _MARGIN = overlay_chrome.CAPSULE_MARGIN
    _AUTO_HIDE_MS = 2500
    _FADE_MS = 140

    def __init__(self, parent: QWidget) -> None:
        # Fixed colours: this rides on top of arbitrary photos, so it must not
        # follow the theme palette (design.md exception for image overlays).
        super().__init__(
            parent,
            object_name="imgctrlbar",
            button_hover=overlay.OVERLAY_BTN_HOVER,
            text_color=_CTRL_ICON_COLOR,
        )

        # 配置の基準矩形（``set_anchor_rect``）。None の間は viewport 下端中央。
        self._anchor_rect: QRect | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)

        # 軸線付きの矢印 = 「隣のファイルへ移る」。ステージヘッダーの
        # ‹前へ / 次へ›（chevron = グリッドの項目を歩む）と図像で弁別する
        # （グリフはレジストリ共有で二重定義しない）。
        self._btn_prev = self._make_button("prev")
        self._btn_prev.clicked.connect(self.prev_clicked.emit)
        row.addWidget(self._btn_prev)
        self._btn_next = self._make_button("next")
        self._btn_next.clicked.connect(self.next_clicked.emit)
        row.addWidget(self._btn_next)

        self._zoom_label = QLabel("100%")
        self._zoom_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self._zoom_label)

        self._btn_fit = self._make_button("fit")
        self._btn_fit.clicked.connect(self.fit_toggle_clicked.emit)
        row.addWidget(self._btn_fit)
        self._btn_fullscreen = self._make_button("fullscreen")
        self._btn_fullscreen.clicked.connect(self.fullscreen_clicked.emit)
        row.addWidget(self._btn_fullscreen)

        # Fade via an opacity effect so reveal/hide is smooth, not a pop.
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(self._FADE_MS)
        self._anim.finished.connect(self._on_fade_finished)

        self._hide_timer = overlay_chrome.auto_hide_timer(
            self, self._AUTO_HIDE_MS, self._fade_out
        )

        self.hide()

    def _make_button(self, glyph: str) -> QToolButton:
        return self.make_button(_ctrl_icon(glyph))

    def set_tooltips(self, prev: str, next_: str, fit: str, fullscreen: str) -> None:
        self._btn_prev.setToolTip(prev)
        self._btn_next.setToolTip(next_)
        self._btn_fit.setToolTip(fit)
        self._btn_fullscreen.setToolTip(fullscreen)

    def set_fullscreen_visible(self, visible: bool) -> None:
        self._btn_fullscreen.setVisible(visible)
        self.adjustSize()
        self.reposition()

    def set_zoom(self, percent: float) -> None:
        self._zoom_label.setText(format_zoom_readout(percent))
        self.adjustSize()
        self.reposition()

    def reveal(self) -> None:
        """Show (fading in) and restart the idle auto-hide timer."""
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        if self._effect.opacity() < 1.0:
            self._anim.stop()
            self._anim.setStartValue(self._effect.opacity())
            self._anim.setEndValue(1.0)
            self._anim.start()
        self._hide_timer.start()

    def force_show(self) -> None:
        """Show at full opacity immediately, cancelling fade + idle timer.

        The normal :meth:`reveal` ramps opacity 0→1 over an animation and
        arms an auto-hide timer — neither of which advances without a real
        event loop.  Offscreen tests and the screenshot harness need the
        capsule deterministically on screen, so this pins it fully opaque
        and keeps it there (no auto-hide) until the next ``reveal`` cycle.
        """
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._hide_timer.stop()
        self._anim.stop()
        self._effect.setOpacity(1.0)

    def _fade_out(self) -> None:
        if not self.isVisible():
            return
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_fade_finished(self) -> None:
        if self._effect.opacity() <= 0.01:
            self.hide()

    def set_anchor_rect(self, rect: QRect | None) -> None:
        """カプセルを寄せる基準矩形（viewport 座標の**画像矩形**）を設定する.

        viewport 下端に貼り付けたままだと、分割ビューで縦長のプレビュー列に
        横長画像を出したとき、カプセルが画像から遠く離れた空白に浮いて「何に
        対する操作か」が読めない。画像の下端に寄せると、操作対象との関係が
        ひと目で分かる。``None``（画像なし）なら viewport 基準。
        """
        self._anchor_rect = rect
        if self.isVisible():
            self.reposition()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        anchor = self._anchor_rect
        if anchor is not None and anchor.width() > 0 and anchor.height() > 0:
            x = anchor.center().x() - self.width() // 2
            y = anchor.bottom() - self.height() - self._MARGIN
        else:
            x = (parent.width() - self.width()) // 2
            y = parent.height() - self.height() - self._MARGIN
        # viewport からはみ出さないようクランプ（極小画像・極端な分割幅）。
        self.move_clamped(x, y)

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Cursor resting on the bar keeps it up (and fully opaque) — never
        # fade out from under a user reaching for a button.
        self._hide_timer.stop()
        self._anim.stop()
        self._effect.setOpacity(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._hide_timer.start()
        super().leaveEvent(event)
