"""Center pane: audio / video preview using QtMultimedia.

``QtMultimedia`` / ``QtMultimediaWidgets`` are imported lazily inside
:meth:`MediaView.__init__` and the relevant methods (never at module
level) so environments without multimedia plugins don't crash at viewer
startup — :class:`ContentView` defers MediaView construction until media
playback is first needed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import (
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QFontMetrics,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import FONT_SUBTITLE_PT, hint_style, overlay, set_icon
from ._slider import enable_click_jump
from .context_menus import (
    EntryMenuContext,
    append_entry_verbs,
    curation_hooks_from_ancestors,
)
from .folder_scan import VIDEO_SUFFIXES
from .pending import Pending, always

#: 音声ファイル名ラベルの左右の余白（省略幅の計算に使う）。
_AUDIO_LABEL_MARGIN = 16
from .view_prefs import open_with_default

if TYPE_CHECKING:
    from .state import ViewerState


_PLAYBACK_RATES: list[float] = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
_DEFAULT_FRAME_STEP_MS = 33  # ~30fps fallback when frame rate metadata is unavailable


def _format_duration(ms: int) -> str:
    """Convert milliseconds to MM:SS string."""
    s = max(0, ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


class MediaView(QWidget):
    """Audio / video preview using QMediaPlayer + QVideoWidget.

    Layout:
        [QVideoWidget — fills available space; hidden for audio-only files]
        [Control bar: ▶/⏸ | elapsed/total | seek slider | speed | loop |
         vol slider | mute]

    Key bindings (WidgetWithChildrenShortcut so they don't conflict with the
    global ←/→ that step the left-pane PostGrid):
        Space      — play / pause toggle
        ←          — navigate to previous sibling file
        →          — navigate to next sibling file
        ,          — step back one frame (video only; pauses first)
        .          — step forward one frame (video only; pauses first)

    Emits ``navigate_requested(delta, immediate=True)`` for ←/→ so the
    ContentView parent routes the sibling step without the wheel-grace delay.

    Emits ``loop_toggled(bool)`` whenever the user flips the loop button so a
    caller can persist the choice (wiring into ``ViewerState.media_loop`` is
    left to a later phase — this view only owns the in-session behaviour and
    the initial value applied via :meth:`apply_settings`).
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)
    loop_toggled = Signal(bool)
    volume_changed = Signal(int)  # user moved the volume slider (F07, 0–100)
    # 再生速度コンボの変更 — loop / volume と同じ 3 経路に載せ、
    # インスタンス間（分割プレビュー ⇄ 全画面）で値を共有する。
    playback_rate_changed = Signal(float)
    # 現在ソースの再生が**終端に達してもう先へ進まない**ことの正規化通知。
    # EndOfMedia（ループ無効 / スライドショーによる抑止中）と
    # InvalidMedia（破損 / 未対応コーデック — EndOfMedia が永遠に来ない）を
    # MediaView 側で 1 本に正規化する。ライトボックスのスライドショーは
    # private な ``_player`` を購読する代わりにこれを購読する。ループ継続中
    # （ネイティブ / エミュレーションとも）は出さない。
    playback_finished = Signal()
    # プレーヤの durationChanged の再送出（ms）。スライドショーの
    # ウォッチドッグが「残り尺 + 余裕」への張り直しに使う。
    duration_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # QtMultimedia laziness is enforced by ContentView._ensure_media, which
        # defers construction of this widget until media playback is first needed.
        # The imports here are fine — they only execute when MediaView.__init__
        # is actually called (i.e. the first time show_media is invoked).
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer  # noqa: PLC0415
        from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: PLC0415

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- video area ------------------------------------------------
        self._video_widget = QVideoWidget()
        self._video_widget.setStyleSheet(
            f"background: {overlay.MEDIA_LETTERBOX};"
        )
        self._video_widget.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        layout.addWidget(self._video_widget, 1)

        # Audio-only label shown over the black video widget
        self._audio_label = QLabel()
        self._audio_label.setAlignment(Qt.AlignCenter)
        # White-on-black is intentional (video letterbox overlay — see the
        # image-anchored-overlay exception to the theme-token rule).  The
        # colour itself comes from ``common/ui/overlay.py`` like every other
        # image-anchored fixed colour: spelling it here would leave this surface
        # outside the two single sources of truth the design system declares.
        self._audio_label.setStyleSheet(
            f"color: {overlay.MEDIA_LABEL_TEXT};"
            f" font-size: {FONT_SUBTITLE_PT}pt;"
            " background: transparent;"
        )
        self._audio_label.setParent(self._video_widget)
        self._audio_label.hide()
        #: 音声ファイル名の生の値（``_audio_label`` は幅に合わせて省略した
        #: 文字列を持つので、省略の作り直しには元の名前が要る）。
        self._audio_name = ""

        # ---- control bar -----------------------------------------------
        ctrl = QWidget()
        ctrl.setObjectName("mediaview_ctrl")
        ctrl.setStyleSheet(
            "QWidget#mediaview_ctrl { background: palette(window); "
            "border-top: 1px solid palette(mid); }"
        )
        ctrl_box = QHBoxLayout(ctrl)
        ctrl_box.setContentsMargins(8, 4, 8, 4)
        ctrl_box.setSpacing(6)

        self._play_btn = QToolButton()
        set_icon(self._play_btn, "play")
        self._play_btn.setToolTip(t("viewer.media_view.play_pause_tooltip"))
        self._play_btn.clicked.connect(self._on_play_pause)
        ctrl_box.addWidget(self._play_btn)

        self._time_label = QLabel("--:-- / --:--")
        self._time_label.setMinimumWidth(90)
        ctrl_box.addWidget(self._time_label)

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setSingleStep(1000)
        self._seek_slider.setPageStep(5000)
        enable_click_jump(self._seek_slider)
        ctrl_box.addWidget(self._seek_slider, 1)

        self._rate_combo = QComboBox()
        for rate in _PLAYBACK_RATES:
            self._rate_combo.addItem(f"{rate:g}x", rate)
        self._rate_combo.setCurrentIndex(_PLAYBACK_RATES.index(1.0))
        self._rate_combo.setToolTip(t("viewer.media_view.playback_rate_tooltip"))
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        ctrl_box.addWidget(self._rate_combo)

        self._loop_btn = QToolButton()
        set_icon(self._loop_btn, "repeat")
        self._loop_btn.setToolTip(t("viewer.media_view.loop_tooltip"))
        self._loop_btn.setCheckable(True)
        self._loop_btn.toggled.connect(self._on_loop_toggled)
        ctrl_box.addWidget(self._loop_btn)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.setToolTip(t("viewer.media_view.volume_tooltip"))
        enable_click_jump(self._vol_slider)
        ctrl_box.addWidget(self._vol_slider)

        self._mute_btn = QToolButton()
        set_icon(self._mute_btn, "volume")
        self._mute_btn.setToolTip(t("viewer.media_view.mute_tooltip"))
        self._mute_btn.setCheckable(True)
        self._mute_btn.clicked.connect(self._on_mute_toggled)
        ctrl_box.addWidget(self._mute_btn)

        self._open_btn = QPushButton(t("common.action.open_with_default"))
        set_icon(self._open_btn, "external-link")
        self._open_btn.setFixedHeight(24)
        self._open_btn.clicked.connect(self._on_open_default)
        ctrl_box.addWidget(self._open_btn)

        layout.addWidget(ctrl)

        # ---- error label -----------------------------------------------
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(hint_style() + " padding: 4px 8px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        # ---- media player ---------------------------------------------
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.setVideoOutput(self._video_widget)

        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._seek_slider.actionTriggered.connect(self._on_seek_action)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)

        self._seeking = False
        self._pre_mute_vol = 70
        # Guards the volume slot from persisting / mute-unarming when *we*
        # move the slider programmatically (apply_settings, mute toggle,
        # initial value) — only genuine user drags should (F07).
        self._suppress_vol_emit = False
        self._autoplay = True
        self._current_path: Path | None = None
        self._is_video = False
        self._playback_rate = 1.0
        # ``show_media(position_ms=…)`` で受けた再生位置。``LoadedMedia`` が
        # 来た時点で一度だけ当てて降ろす。番兵 0 は使わない——
        # ``0 ms`` は「引き継ぎ無し」ではなく**先頭へ戻す**要求で、別インス
        # タンスを先頭まで巻き戻してから引き継いだときに当てる必要がある。
        self._pending_seek_ms: Pending[int] = Pending()
        # volume の ``_suppress_vol_emit`` と同じ役割 — apply_settings に
        # よる復元をユーザー変更として echo しない。速度・ループもそれぞれ
        # 自前のフラグを持つ。
        self._suppress_rate_emit = False
        self._suppress_loop_emit = False
        self._loop_enabled = False
        # Temporary loop override (slideshow): while True, playback never
        # loops — natively or emulated — regardless of ``_loop_enabled``,
        # so ``EndOfMedia`` fires and the slideshow can advance.  The
        # user's loop preference / button state is left untouched.
        self._loop_suppressed = False
        # True when QMediaPlayer.setLoops is available and used natively;
        # False means we emulate looping via mediaStatusChanged/EndOfMedia.
        self._native_loop_supported = hasattr(QMediaPlayer, "setLoops") and hasattr(
            QMediaPlayer, "Loops"
        )
        # Connected unconditionally: normalizes the playback "end" states
        # (EndOfMedia / InvalidMedia) into ``playback_finished`` and — on the
        # non-native-loop fallback path — performs the manual
        # restart-on-EndOfMedia loop emulation.
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)

        # Apply initial volume (will be overwritten by apply_settings).
        # Suppressed so construction doesn't emit volume_changed / unarm mute.
        self._suppress_vol_emit = True
        self._vol_slider.setValue(70)
        self._suppress_vol_emit = False

        # ---- keyboard shortcuts (WidgetWithChildrenShortcut) -----------
        # このビュー自身がフォーカスを受けられないと、下のショートカットは
        # 配下の子（スライダー等）をクリックするまで一度も発火しない。
        # 最大化のフォーカス移送（ContentView.focus_current_page）は
        # ``Qt.NoFocus`` のページを飛ばして親コンテナへ落とすため、動画ページ
        # を最大化した直後の Space / , / . が全滅していた。←/→ は
        # ウィンドウ側の ←/→ と両マッチになるが、そちらは
        # ``activatedAmbiguously`` → ``_step_or_navigate`` で同じ
        # 「前/次のファイル」へ合流するので意味は変わらない。
        self.setFocusPolicy(Qt.StrongFocus)

        def _shortcut(key: str, cb) -> None:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(cb)

        _shortcut("Space", self._on_play_pause)
        _shortcut("Left",  lambda: self.navigate_requested.emit(-1, True))
        _shortcut("Right", lambda: self.navigate_requested.emit(1, True))
        _shortcut(",", lambda: self._step_frame(-1))
        _shortcut(".", lambda: self._step_frame(1))

    # ------------------------------------------------------------------ API

    def show_media(self, path: Path, position_ms: int = 0) -> None:
        """Open *path*, optionally resuming at *position_ms*.

        ``position_ms`` は「別インスタンスから引き継いだ再生位置」。
        ``setSource`` 直後の ``setPosition`` は無視される（まだ尺が
        分かっていない）ので、``LoadedMedia`` を待ってから当てる。

        ``position_ms=0`` も**保留として積む**（番兵ではない）: 引き継ぎ元が
        先頭まで巻き戻っていた / 再生前だったときの引き継ぎ位置は 0 ms で、
        これを「引き継ぎ無し」と読むと、次のソースの ``LoadedMedia`` 直後に
        自動再生が進めた位置をそのまま引き継いだように見せてしまう。0 ms の
        ``setPosition`` は先頭復帰なので、当てても副作用は無い。
        """
        from PySide6.QtCore import QUrl  # noqa: PLC0415

        self.clear_media()
        self._pending_seek_ms.set(max(0, int(position_ms)))
        self._current_path = path
        self._error_label.hide()
        self._set_controls_enabled(True)

        self._is_video = path.suffix.lower() in VIDEO_SUFFIXES
        if self._is_video:
            self._video_widget.show()
            self._audio_name = ""
            self._audio_label.hide()
        else:
            self._audio_name = path.name
            self._elide_audio_label()
            self._audio_label.show()
            self._video_widget.show()

        self._player.setSource(QUrl.fromLocalFile(str(path)))
        # Playback rate is a session-only preference — re-apply on every
        # file switch since QMediaPlayer resets it per source.
        self._player.setPlaybackRate(self._playback_rate)
        self._apply_native_loop()
        if self._autoplay:
            self._player.play()
        else:
            self._player.pause()

    def playback_position(self) -> int:
        """現在の再生位置 (ms)。別インスタンスへの引き継ぎ用。"""
        return max(0, int(self._player.position()))

    def current_path(self) -> Path | None:
        """いま開いているファイル（未ロードなら ``None``）— 引き継ぎの公開面.

        引き継ぎ 4 点（一時停止 / 位置取得 / シーク / 同一ファイル判定）の
        うち、ホスト側が ``_current_path`` を直読みしないための口。
        Qt 型を返さないので遅延 import に影響しない。
        """
        return self._current_path

    def seek(self, position_ms: int) -> None:
        """再生位置を直接指定する（引き継ぎの復路）。"""
        self._player.setPosition(max(0, int(position_ms)))

    def pause_playback(self) -> bool:
        """再生中なら一時停止する（別ウィンドウが前面に出るとき）.

        ``clear_media`` と違いソースも再生位置も保持するので、閲覧モードから
        戻ったユーザーは同じフレームの続きから再開できる。戻り値は「実際に
        止めたか」（再生していなければ ``False`` の no-op）。
        """
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        playing = QMediaPlayer.PlaybackState.PlayingState
        if self._player.playbackState() != playing:
            return False
        self._player.pause()
        return True

    def clear_media(self) -> None:
        from PySide6.QtCore import QUrl  # noqa: PLC0415

        self._player.stop()
        self._player.setSource(QUrl())
        # 誰も当てないまま残った引き継ぎ位置は、この面と一緒に捨てる。
        self._pending_seek_ms.clear()
        self._current_path = None
        self._is_video = False
        self._seek_slider.setValue(0)
        self._seek_slider.setRange(0, 0)
        self._time_label.setText("--:-- / --:--")
        self._audio_label.hide()
        self._error_label.hide()

    def apply_settings(self, state: "ViewerState") -> None:
        self._autoplay = state.media_autoplay
        vol = max(0, min(100, state.media_volume))
        self._pre_mute_vol = vol
        if not self._mute_btn.isChecked():
            # Programmatic restore — must not echo back as a user change (F07).
            self._suppress_vol_emit = True
            self._vol_slider.setValue(vol)
            self._suppress_vol_emit = False
        self._loop_enabled = state.media_loop
        # Programmatic restore — must not echo back as a user change.
        self._suppress_loop_emit = True
        try:
            self._loop_btn.setChecked(state.media_loop)
        finally:
            self._suppress_loop_emit = False
        self._apply_native_loop()
        self._apply_playback_rate(state.media_playback_rate)

    def _apply_playback_rate(self, rate: float) -> None:
        """Restore a persisted playback rate without echoing it back.

        コンボに無い値（設定ファイルの手書き / 将来の選択肢削除）は
        黙って無視して 1.0 のままにする — 復元で落ちない。
        """
        try:
            index = _PLAYBACK_RATES.index(float(rate))
        except ValueError:
            return
        self._suppress_rate_emit = True
        try:
            self._rate_combo.setCurrentIndex(index)
        finally:
            self._suppress_rate_emit = False

    def loop_enabled(self) -> bool:
        """Return whether loop playback is currently enabled."""
        return self._loop_enabled

    def set_loop_suppressed(self, suppressed: bool) -> None:
        """Temporarily force looping off without touching the user's setting.

        The lightbox slideshow relies on ``EndOfMedia`` to advance past a
        video — with the loop preference ON, native infinite looping never
        reaches end-of-media and the slideshow stalls on the first video
        forever.  While suppressed, both the native ``setLoops`` path
        and the EndOfMedia-emulation fallback play the source once; the
        loop button / ``loop_enabled()`` are unaffected and the preference
        resumes as soon as the suppression is lifted.
        """
        suppressed = bool(suppressed)
        if suppressed == self._loop_suppressed:
            return
        self._loop_suppressed = suppressed
        self._apply_native_loop()

    def is_playing(self) -> bool:
        """現在ソースを実際に再生中か（一時停止 / 停止 / 自動再生 OFF は False）."""
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        return (
            self._player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )

    def expected_remaining_ms(self) -> int | None:
        """実再生中の残り時間（再生速度換算済み・ms）。不明なら ``None``.

        再生していない（一時停止 / 停止 / 自動再生 OFF で置かれたまま）か
        尺が未判明（メタデータ未着）なら ``None`` — その場合、呼び出し側の
        ウォッチドッグは固定間隔のフロアで武装し、後から尺が判明すれば
        :attr:`duration_changed` 経由で張り直せる。
        """
        if not self.is_playing():
            return None
        dur = self._player.duration()
        if dur <= 0:
            return None
        remaining = max(0, dur - self._player.position())
        rate = self._playback_rate if self._playback_rate > 0 else 1.0
        return int(remaining / rate)

    # ------------------------------------------------------------------ slots

    def _on_play_pause(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        state = self._player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_playback_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        if state == QMediaPlayer.PlaybackState.PlayingState:
            set_icon(self._play_btn, "pause")
        else:
            set_icon(self._play_btn, "play")

    def _on_position_changed(self, pos: int) -> None:
        if not self._seeking:
            self._seek_slider.setValue(pos)
        dur = self._player.duration()
        self._time_label.setText(
            f"{_format_duration(pos)} / {_format_duration(dur)}"
        )

    def _on_duration_changed(self, dur: int) -> None:
        self._seek_slider.setRange(0, max(0, dur))
        self.duration_changed.emit(max(0, dur))

    def _on_rate_changed(self, index: int) -> None:
        rate = self._rate_combo.itemData(index)
        if rate is None:
            return
        self._playback_rate = float(rate)
        self._player.setPlaybackRate(self._playback_rate)
        if not self._suppress_rate_emit:
            self.playback_rate_changed.emit(self._playback_rate)

    def _on_loop_toggled(self, checked: bool) -> None:
        self._loop_enabled = checked
        self._apply_native_loop()
        if not self._suppress_loop_emit:
            self.loop_toggled.emit(checked)

    def _apply_native_loop(self) -> None:
        """Reflect ``_loop_enabled`` onto the player when native support exists.

        When ``QMediaPlayer.setLoops`` is unavailable, looping is instead
        emulated in :meth:`_on_media_status_changed` on EndOfMedia.
        """
        if not self._native_loop_supported:
            return
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        looping = self._loop_enabled and not self._loop_suppressed
        loops = (
            QMediaPlayer.Loops.Infinite if looping else QMediaPlayer.Loops.Once
        )
        self._player.setLoops(loops)

    def _on_media_status_changed(self, status) -> None:
        """再生の「終端」を正規化して :attr:`playback_finished` を出す.

        * ``InvalidMedia`` — 破損 / 未対応コーデック。``EndOfMedia`` は永遠に
          来ないので、これも終端として通知する。
        * ``EndOfMedia`` でループ継続する場合（ループ有効かつ非抑止）は終端
          ではない — ネイティブ ``setLoops`` 非対応環境ではここで手動
          リスタート（従来のエミュレーション）。ネイティブ対応環境の無限
          ループでは ``EndOfMedia`` 自体が発生しない。
        * それ以外の ``EndOfMedia``（ループ無効 / スライドショーの抑止中）は
          終端 — 通知する。

        「新しい沈黙経路が見つかるたびにライトボックス側へ条件を 1 つ足す」
        のをやめ、再生状態の知識を持つ MediaView が終端判定を一手に負う。
        """
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            # 引き継いだ再生位置をここで当てる — 尺が分かる
            # 前の setPosition は黙って無視されるため。
            seek_ms = self._pending_seek_ms.take_if(always)
            if seek_ms is not None:
                self._player.setPosition(seek_ms)
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.playback_finished.emit()
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        if self._loop_enabled and not self._loop_suppressed:
            if not self._native_loop_supported:
                self._player.setPosition(0)
                self._player.play()
            return
        self.playback_finished.emit()

    def _step_frame(self, direction: int) -> None:
        """Pause and move one frame back/forward (video only)."""
        if not self._is_video or self._current_path is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: PLC0415

        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        step_ms = self._frame_step_ms()
        new_pos = max(0, self._player.position() + direction * step_ms)
        dur = self._player.duration()
        if dur > 0:
            new_pos = min(new_pos, dur)
        self._player.setPosition(new_pos)

    def _frame_step_ms(self) -> int:
        """Best-effort frame duration in ms from metadata, else a 30fps guess."""
        from PySide6.QtMultimedia import QMediaMetaData  # noqa: PLC0415

        meta = self._player.metaData()
        fps = meta.value(QMediaMetaData.Key.VideoFrameRate)
        try:
            fps_f = float(fps)
        except (TypeError, ValueError):
            fps_f = 0.0
        if fps_f > 0:
            return max(1, round(1000.0 / fps_f))
        return _DEFAULT_FRAME_STEP_MS

    def _on_seek_pressed(self) -> None:
        self._seeking = True

    def _on_seek_released(self) -> None:
        self._seeking = False
        self._player.setPosition(self._seek_slider.value())

    def _on_seek_action(self, action: int) -> None:
        """ホイール / PageUp・PageDown / Home・End による移動を再生位置へ反映する。

        ``sliderPressed`` / ``sliderReleased`` はマウスドラッグでしか発火し
        ない。QSlider はホイールや PageUp/PageDown、Home/End をそれぞれ
        ``triggerAction`` 経由で処理し、そこで飛ぶのは ``actionTriggered``
        だけなので、この経路を無視すると再生位置が一度も動かずつまみだけ
        ずれる。ホイールは ``SliderMove`` として飛ぶ（マウスドラッグの
        ``SliderMove`` と同じ action 値）ため action 種別では区別できず、
        ``_seeking``（ドラッグ中フラグ）で判定する — ドラッグ中の連続移動は
        ``_on_seek_released`` 側でまとめて反映する。
        """
        del action  # action 値では区別できないため未使用（docstring 参照）
        if self._seeking:
            return
        self._player.setPosition(self._seek_slider.sliderPosition())

    def _on_volume_changed(self, value: int) -> None:
        from PySide6.QtMultimedia import QAudio  # noqa: PLC0415

        linear = QAudio.convertVolume(
            value / 100.0,
            QAudio.VolumeScale.LogarithmicVolumeScale,
            QAudio.VolumeScale.LinearVolumeScale,
        )
        self._audio_out.setVolume(linear)
        if self._suppress_vol_emit:
            return  # programmatic change (apply_settings / mute / init)
        # Genuine user drag.  Dragging the slider while muted clears mute so the
        # button state matches what the user now hears (F07) — setChecked does
        # NOT fire the clicked slot, so update the icon by hand.
        if self._mute_btn.isChecked():
            self._mute_btn.setChecked(False)
            set_icon(self._mute_btn, "volume")
        self._pre_mute_vol = value
        self.volume_changed.emit(value)

    def _on_mute_toggled(self, checked: bool) -> None:
        # Slider moves here are programmatic — suppress so they neither persist
        # nor recursively toggle mute (F07).
        self._suppress_vol_emit = True
        try:
            if checked:
                self._pre_mute_vol = self._vol_slider.value()
                self._vol_slider.setValue(0)
                set_icon(self._mute_btn, "volume-x")
            else:
                self._vol_slider.setValue(self._pre_mute_vol)
                set_icon(self._mute_btn, "volume")
        finally:
            self._suppress_vol_emit = False

    def _on_error(self, error, error_string: str) -> None:
        self._error_label.setText(
            t("viewer.media_view.playback_error", error=error_string)
        )
        self._error_label.show()
        self._set_controls_enabled(False)

    def _on_open_default(self) -> None:
        # 共通ヘルパ経由 — 失敗時はステータス通知。
        if self._current_path is not None:
            open_with_default(self._current_path, self)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._play_btn.setEnabled(enabled)
        self._seek_slider.setEnabled(enabled)
        self._vol_slider.setEnabled(enabled)
        self._mute_btn.setEnabled(enabled)
        self._rate_combo.setEnabled(enabled)
        self._loop_btn.setEnabled(enabled)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # F05: unified 「既定アプリで開く / エクスプローラで開く / フルパスをコピー」
        # right-click menu, shared with the other centre-pane leaf views.
        if self._current_path is None:
            return
        menu = QMenu(self)
        append_entry_verbs(
            menu,
            EntryMenuContext(
                path=self._current_path, is_dir=False,
                curation=curation_hooks_from_ancestors(self), host=self,
            ),
        )
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _elide_audio_label(self) -> None:
        """音声ファイル名を幅に合わせて中央省略する（全文はツールチップ）。

        ラベルは ``_video_widget`` 全面に置かれ ``Qt.AlignCenter`` なので、
        省略しないと幅を超えた名前が**中央基準で左右とも**切り落とされ、先頭
        も末尾も読めなくなる（この製品は 250/255 バイトまでのファイル名を
        正当な入力として扱う）。解き方は ``DetailWindow._elide_path`` と同じ
        ``QFontMetrics.elidedText`` + ツールチップ。
        """
        name = self._audio_name
        if not name:
            self._audio_label.setText("")
            self._audio_label.setToolTip("")
            return
        width = max(0, self._audio_label.width() - _AUDIO_LABEL_MARGIN)
        if width <= 0:
            self._audio_label.setText(name)
        else:
            metrics = QFontMetrics(self._audio_label.font())
            self._audio_label.setText(
                metrics.elidedText(name, Qt.ElideMiddle, width)
            )
        self._audio_label.setToolTip(name)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._audio_label.setGeometry(self._video_widget.rect())
        self._elide_audio_label()


#: :func:`build_media_view` の構築失敗トレースバックを 1 プロセス 1 回に
#: 抑えるフラグ。失敗は環境要因（Qt6Multimedia.dll 欠落 / AV 隔離）なので
#: 毎回同じ結果になるが、再試行はユーザーが動画を選ぶたびに起きる。
_BUILD_FAILURE_LOGGED = False


def build_media_view(
    parent: QWidget | None = None,
    *,
    on_navigate: Callable[[int, bool], None],
    on_loop: Callable[[bool], None],
    on_volume: Callable[[int], None],
    on_rate: Callable[[float], None],
    on_finished: Callable[[], None] | None = None,
    on_duration: Callable[[int], None] | None = None,
    pending_state: "ViewerState | None" = None,
) -> "MediaView | None":
    """MediaView を構築し、ホストへの公開シグナルを一括配線する集約点.

    :func:`snappix.viewer.image_view.connect_state_writeback` と同じ趣旨の
    ファンアウト。MediaView のインスタンスは 2 つある（中央プレビュー
    ``ContentView._media`` と全画面 ``LightboxWindow._media``）が、
    「遅延構築 → シグナル配線 → 保留設定の当て込み → 構築失敗の降格」を
    **両ホストが別々に手書き**すると、片側だけに配線が足される事故
    （再生速度の配線、構築失敗の降格が中央ペインだけ、等）が起きる。
    **MediaView とホストの間に増やす配線は必ずこの関数に足すこと**。

    構築失敗（QtMultimedia のプラグイン / DLL 欠落）は例外を送出せず
    ``None`` を返す。「そのプレビューだけ失敗」への降格そのものは各ホストの
    退避先が違う（ContentView はファイル情報ページ、ライトボックスは
    タイトルオーバーレイ + 位置の巻き戻し）ため、``None`` の扱いだけを
    ホスト側に残す。トレースバックは種別ごと初回のみ出す。

    ウィジェットは**ローカル変数で完成させてから**返す: 途中で例外が出ても
    壊れたインスタンスをホストへ渡さない（ホストの ``is None`` 判定が
    次回以降も再試行できる）。
    """
    global _BUILD_FAILURE_LOGGED
    view: MediaView | None = None
    try:
        view = MediaView(parent)
        # ←/→ の 1 ステップナビ（MediaView 内 QShortcut → ホストのナビ）。
        view.navigate_requested.connect(on_navigate)
        # ループ / 音量 / 再生速度のユーザー変更をホストへ再送出し、
        # ViewerState へ永続化させる（遅延構築なので __init__ ではなくここ）。
        view.loop_toggled.connect(on_loop)
        view.volume_changed.connect(on_volume)
        view.playback_rate_changed.connect(on_rate)
        # 終端 / 尺の通知はスライドショーを持つホストだけが購読する。
        if on_finished is not None:
            view.playback_finished.connect(on_finished)
        if on_duration is not None:
            view.duration_changed.connect(on_duration)
        # 構築前に届いていたメディア設定（音量 / 自動再生 / ループ / 速度）。
        # 適用しないとハードコード既定（音量 70 / 自動再生 ON）で再生される。
        if pending_state is not None:
            view.apply_settings(pending_state)
    except Exception:
        if view is not None:
            view.deleteLater()
        if not _BUILD_FAILURE_LOGGED:
            _BUILD_FAILURE_LOGGED = True
            logger.opt(exception=True).error("メディアビューを初期化できません")
        return None
    return view
