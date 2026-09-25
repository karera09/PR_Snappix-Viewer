"""閲覧モードの固定オーバーレイ色と、その色で描くクローム部品.

閲覧モードは任意の写真の上に載る**常時ダークな面**なので、ここのクロームは
テーマパレットに追従しない（design.md の「画像上オーバーレイは固定色可」の
例外規定）。**閲覧モードの固定色はこのモジュールに集約する** — 実値は共通の
単一オーバーレイパレット ``common/ui/overlay.py`` から引き、このモジュールの
:data:`_SCRIM` / :data:`_OVERLAY_TEXT` が閲覧モード側の唯一の別名になる
（下端のサムネイル帯 :mod:`~snappix.viewer.lightbox_parts.filmstrip` も地色は
ここから import する）。

置いてある部品:

* :class:`_CounterOverlay` — 左下の常設カウンタ（n / N ・ファイル名・★N）。
  オートハイド対象外の唯一の面。
* :class:`_HintOverlay` — セッション初回だけ数秒出る操作ヒント。
* :class:`CenterMessageOverlay` — 投稿タイトル / 端到達予告の中央ピル。
  プレビュー最大化側の端到達案内と見た目を共有するため公開名。
* :class:`LightboxTopBar` — 上部情報バー（タイトル + 印ストリップ + 操作）。
* :class:`LightboxControlCapsule` — 下端中央の操作カプセル。
* :class:`EmptyPlaylistView` — 表示できるメディアが 1 つも無いときの常設ページ。

ピル / カプセルの体裁（QSS 生成・``WA_StyledBackground``・親矩形クランプ・
自動消灯タイマー・末尾省略）は共通基底 ``common/ui/overlay_chrome.py`` の
``OverlayPill`` / ``OverlayCapsule`` が持ち、ここに残るのは席ごとの配置方針と
表示内容だけ。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...common.i18n import t
from ...common.ui import (
    FONT_SUBTITLE_PT,
    FONT_TITLE_PT,
    overlay,
    overlay_chrome,
)
from ...common.ui import fixed_icon as shared_fixed_icon
from ...common.ui.overlay_chrome import OverlayCapsule, OverlayPill
from ..curation_strip import MODE_COMPACT, CurationStrip
from ..image_view import format_zoom_readout

# 投稿遷移時のタイトルオーバーレイ表示時間。
_TITLE_OVERLAY_MS = 1500
# セッション初回のみ表示する操作ヒント（Esc/Space/S）の表示時間。
_HINT_OVERLAY_MS = 5000

# 固定オーバーレイ色（画像上で読めることが要件 — design.md の固定色例外）。
# 実値は共通の単一オーバーレイパレット common/ui/overlay.py に集約。
_SCRIM = overlay.SCRIM_HEAVY
_OVERLAY_TEXT = overlay.LABEL_TEXT  # 最暗背景上の明るい文字（トークン由来）




def _fixed_icon(name: str, color: str = _OVERLAY_TEXT, size: int = 20) -> QIcon:
    """固定色（テーマ非追従）の埋め込み SVG アイコン.

    ライトボックスのクロームは固定の暗スクリム上に載るため、テーマ追従の
    ``common.ui.icon()``（ライトテーマでは暗色）ではなく常に明るい色で描く。
    実装は共有ヘルパー ``common.ui.fixed_icon`` に一本化されており（この
    モジュール・``image_view`` の 2 箇所に同じ 1x/2x ループが手書き複製されて
    いた）、グリフも ``icons.py`` の 1 表だけを引く。
    """
    return shared_fixed_icon(name, color, size=size)


def _fit_icon(color: str = _OVERLAY_TEXT, size: int = 18) -> QIcon:
    """「フィット / 実寸」トグル用の固定色アイコン.

    グリフは ``icons.py`` 登録済みの ``fit-frame``（外枠の中の小矩形 = 画像を
    枠に収める）。以前はこのモジュールが ``_FIT_GLYPH`` として四隅の角括弧を
    手書きで持っており、``image_view_parts.control_bar._CTRL_GLYPHS["fit"]``
    と文字列レベルで同一・かつ ``icons.py`` の ``maximize``（この席を広げる）
    とも同型という三重定義だった。
    """
    return shared_fixed_icon("fit-frame", color, size=size)


class _CounterOverlay(OverlayPill):
    """常時表示の控えめな位置カウンタ（n / N ・ファイル名・★N）.

    上部バー（オートハイド対象）とは独立に**常に表示**し、手を止めて眺めて
    いる間・スライドショー中でも「今フォルダ内の何枚目 / 総数」と現在ファイル名
    が分かるようにする。左下隅の半透明ピル（フィルムストリップ帯の上に配置）。

    ここは「位置と評価の常設面」— 位置表示の唯一の常設先（上部バーは投稿
    タイトルのみ）であり、現在画像のスター評価も併記
    する（オートハイド対象外の唯一の面なので、0-5 で評価する瞬間＝バー消灯中
    でも読める）。

    体裁・親矩形クランプ・幅フィットは姉妹の :class:`_HintOverlay` /
    :class:`CenterMessageOverlay` と同じ共通基底 :class:`OverlayPill`。
    以前はここだけ自前 QSS + ``adjustSize`` で、長いファイル名のときに幅の
    上限も末尾省略も無く中央の操作カプセルへ伸びていた。

    **自動消灯は使わない**: 基底の ``present()`` はタイマーを起動するので、
    常設面であるこのピルは :meth:`set_info` で ``show`` / ``raise_`` を自分で
    行う（``present`` を呼ぶと 1 秒台で消えてしまう）。
    """

    #: 左右の画面端から空ける量（:meth:`LightboxWindow._position_counter` と共有）。
    _EDGE_MARGIN = 16
    _PADDING_V = 4
    _PADDING_H = 10
    _RADIUS = 8

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            scrim=overlay.SCRIM_CHIP,
            text_color=_OVERLAY_TEXT,
            font_pt=FONT_SUBTITLE_PT,
            padding_v=self._PADDING_V,
            padding_h=self._PADDING_H,
            radius=self._RADIUS,
            # 常設面なので自動消灯しない。基底はタイマーを作るだけで
            # ``present()`` からしか start しないため、この値は使われない。
            auto_hide_ms=0,
        )
        #: 省略前の原文（幅上限が変わったら省略をやり直すため保持する）。
        self._full_text = ""
        #: ホストが与える追加の幅上限（0 = 親幅いっぱい）。
        self._width_limit = 0

    def set_width_limit(self, limit: int) -> None:
        """ピルが伸びてよい最大幅を与える（0 = 親幅いっぱい）.

        カウンタは左下、操作カプセルは下端中央なので、親幅いっぱいまで許すと
        長いファイル名でカプセルの下へ潜り込む。上限はホスト
        （:meth:`LightboxWindow._position_counter`）がカプセル幅から算出する。
        """
        limit = max(0, int(limit))
        if limit == self._width_limit:
            return
        self._width_limit = limit
        if self._full_text:
            self._apply_text()

    def parent_width_limit(
        self, margin: int = overlay_chrome.PILL_EDGE_MARGIN
    ) -> int:
        """基底の親幅上限に、ホストが与えた上限を重ねる（狭い方が勝つ）."""
        limit = super().parent_width_limit(margin)
        if self._width_limit > 0:
            return max(1, min(limit, self._width_limit))
        return limit

    def full_text(self) -> str:
        """省略前の原文.

        :meth:`QLabel.text` は親幅に合わせて末尾を省略した**表示用**の文字列を
        返すので、状態からの導出が正しいかを見るときはこちらを読む。
        """
        return self._full_text

    def _apply_text(self) -> None:
        self.fit_text_to_parent(self._full_text, margin=self._EDGE_MARGIN)

    def set_info(
        self,
        index: int,
        total: int,
        name: str,
        star: int = 0,
        running: bool = False,
    ) -> None:
        """常設カウンタの表示を更新する.

        *running* はスライドショー実行中フラグ。
        実行中の手掛かりは上部バーの再生 / 一時停止アイコンしか無く、そのバーは
        1.5 秒で自動消灯するため「いま自動送り中か」が画面から読めなくなって
        いた。★の付加と同じ拡張の形で、消えない面にも出す。
        目印は図像文字ではなく**語**（「スライドショー中」）— カタログ値に
        図像を書かない規約（``tests/test_i18n_no_pictographs.py``）に従う。
        """
        if total <= 0:
            self._full_text = ""
            self.hide()
            return
        if star > 0:
            # 未評価（0）のときは何も足さない — 評価済みだけが目に入る
            text = t(
                "viewer.lightbox.counter_starred",
                index=index,
                total=total,
                name=name,
                star=star,
            )
        else:
            text = t(
                "viewer.lightbox.counter", index=index, total=total, name=name
            )
        if running:
            text = t("viewer.lightbox.counter_running", info=text)
        self._full_text = text
        self._apply_text()
        # ``present()`` は呼ばない — 常設面なので自動消灯タイマーを起動しない。
        self.show()
        self.raise_()


class _HintOverlay(OverlayPill):
    """セッション初回のみ数秒表示する操作ヒント（Esc / Space / S）.

    全画面でカーソル・上部バーが消える前に「抜け方・送り方」を一度だけ案内し、
    初見ユーザーが固まるのを防ぐ。中央やや下の半透明ピルで数秒後に自動消灯。
    体裁・自動消灯・親矩形クランプは共通基底 :class:`OverlayPill`。
    """

    _PADDING_V = 10
    _PADDING_H = 20
    _RADIUS = 10

    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            scrim=overlay.SCRIM_HEAVY,
            text_color=_OVERLAY_TEXT,
            font_pt=FONT_SUBTITLE_PT,
            padding_v=self._PADDING_V,
            padding_h=self._PADDING_H,
            radius=self._RADIUS,
            auto_hide_ms=_HINT_OVERLAY_MS,
        )
        # 親幅に収まらないときは折り返す。
        # 姉妹の :class:`CenterMessageOverlay` は基底の
        # ``fit_text_to_parent``（末尾を「…」で省略）に乗っているが、こちらは
        # 「Esc で終了 ・ ←→ で前後 …」という**操作の列挙**なので、末尾を
        # 落とすと案内そのものが欠ける — 省略ではなく折り返しで収める。
        self.setWordWrap(True)
        #: 下端に確保する高さ（フィルムストリップ + 操作カプセル + マージン）。
        #: ホストが ``resizeEvent`` で更新する（``LightboxControlCapsule`` と
        #: 同じ「予約量」の考え方）。
        self._bottom_reserved = 0

    def set_bottom_reserved(self, reserved: int) -> None:
        self._bottom_reserved = max(0, int(reserved))

    def show_hint(self) -> None:
        self.setText(t("viewer.lightbox.hint"))
        self._clamp_width()
        self.present()

    def _clamp_width(self) -> None:
        """親幅（左右マージン込み）を上限にして再レイアウトする."""
        limit = self.parent_width_limit()
        if limit > 0:
            self.setMaximumWidth(limit)
        self.adjustSize()

    def reposition(self) -> None:
        """中央やや下（親高の 2/3）に置く — ただし下端の予約量より上に。

        親高の 2/3 固定だった頃は、常設の操作カプセル（下端からフィルム
        ストリップ高 + カプセル高 + マージン）と必ず重なる画面高があった:
        7 行に育ったこのヒントが ←/→ を覆い、**ヒント自身が案内している
        操作バー**を隠していた（1366x768 / 1280x800 / 1440x900 いずれも該当）。
        予約量の上へ積む（``LightboxControlCapsule.reposition`` /
        ``_position_counter`` と同じ考え方の共有）。
        """
        parent = self.parentWidget()
        if parent is None:
            return
        # 表示中に親がリサイズされても折り返し幅が追随するよう、位置決めの
        # たびに上限を引き直す（``present`` からも resize 経路からも通る）。
        self._clamp_width()
        preferred = parent.height() * 2 // 3
        ceiling = (
            parent.height() - self._bottom_reserved - self.height()
            - overlay_chrome.CAPSULE_MARGIN
        )
        self.move_clamped(
            (parent.width() - self.width()) // 2, min(preferred, ceiling)
        )


class EmptyPlaylistView(QWidget):
    """空プレイリスト（表示できる画像・動画が 1 つも無い）の常設カード.

    画像の無いフォルダで F11 すると、従来は :class:`CenterMessageOverlay` で
    「このフォルダには表示できる画像・動画がありません」を出すだけで、
    ``_TITLE_OVERLAY_MS`` = 1.5 秒後に消えて**自発的な手掛かりがゼロ**になって
    いた（常設カウンタも ``total <= 0`` で自分を隠す）。マウスを動かせば上部
    バーは復帰するので「固まった」わけではないが、動かすまで何も出ない。

    そこで**消えないページ**として別に持つ: 見出し + 一文 + [全画面表示を終了]。
    :class:`CenterMessageOverlay` を「消えないオーバーレイ」に改造すると
    「タイトルオーバーレイは 1.5 秒」という既存契約を壊すので、別ページに
    する。

    **``EmptyStateCard`` を再利用しない理由**: 閲覧モードは
    :data:`DARK_TOKENS` を明示適用した**固定ダーク面**（画像の上に載る）で、
    共通カードはテーマトークン（明テーマでは暗い文字）で描かれる。この面の
    固定オーバーレイ色は design.md の固定色例外として本モジュールが既に
    :data:`_OVERLAY_TEXT` / :data:`_SCRIM` に集約しているので、そちらへ揃える。
    """

    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        heading = QLabel(t("viewer.lightbox.empty_folder"))
        heading.setAlignment(Qt.AlignCenter)
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"QLabel {{ color: {_OVERLAY_TEXT};"
            f" font-size: {FONT_TITLE_PT}pt; font-weight: bold; }}"
        )
        outer.addWidget(heading)

        body = QLabel(t("viewer.lightbox.empty_folder_body"))
        body.setAlignment(Qt.AlignCenter)
        body.setWordWrap(True)
        body.setStyleSheet(
            f"QLabel {{ color: {_OVERLAY_TEXT};"
            f" font-size: {FONT_SUBTITLE_PT}pt; }}"
        )
        outer.addSpacing(6)
        outer.addWidget(body)

        row = QHBoxLayout()
        row.addStretch(1)
        self._close_btn = QPushButton(t("viewer.lightbox.close_tooltip"))
        # 見出し・本文と同じ固定色でボタンも描く。スタイル無指定だと
        # ここだけアプリ全体 QSS のテーマ色が当たり、明テーマでは「暗い地に
        # 明るい文字」の面に明色ボタンが 1 つ浮く。design.md 使用ルール 5 の
        # 「その面固有の 1 調整」で、面そのものは固定色例外（ルール 2）。
        self._close_btn.setStyleSheet(
            "QPushButton {"
            " background: transparent;"
            f" color: {_OVERLAY_TEXT};"
            f" border: 1px solid {overlay.rgba_str(overlay.OVERLAY_TEXT_DIM)};"
            f" border-radius: {overlay_chrome.CAPSULE_BUTTON_RADIUS}px;"
            " padding: 6px 18px;"
            "}"
            "QPushButton:hover {"
            f" background: {overlay.rgba_str(overlay.LIGHTBOX_BTN_HOVER)};"
            "}"
        )
        self._close_btn.setCursor(Qt.PointingHandCursor)
        # 親は QDialog ではない（``LightboxWindow`` は QWidget）ので、Qt が
        # ダイアログ文脈で自動的に付ける autoDefault が付かず、フォーカスが
        # 当たっていても Enter で押せない。キーボードだけで到達できる唯一の
        # 可視アクションなので明示的に付ける（Space は ``QAbstractButton``
        # が元から扱う）。
        self._close_btn.setAutoDefault(True)
        self._close_btn.clicked.connect(self.close_requested.emit)
        row.addWidget(self._close_btn)
        row.addStretch(1)
        outer.addSpacing(16)
        outer.addLayout(row)
        outer.addStretch(2)


class CenterMessageOverlay(OverlayPill):
    """投稿タイトル / 予告メッセージの大きめ中央オーバーレイ（1.5 秒で自動消灯）.

    閲覧モード（全画面）の「もう一度 → で次の投稿へ」等に使う体裁で、
    プレビュー最大化側の端到達案内も**同じ見た目**
    を共有するために公開名にしてある（``ContentView`` が再利用する）。

    表示テキストは**必ず親幅に収める**。投稿タイトルは
    ``common/sanitize.py`` が 250 バイトまで許すので日本語なら 80 文字級が
    普通にあり得るのに対し、このラベルは折り返しも省略も持たない
    ``FONT_TITLE_PT * 2`` の 1 行ピルで、位置のクランプだけでは幅を縮め
    られない — 何もしないと右側が親の外へ出て黙って切れる。省略の実装は
    基底の :meth:`OverlayPill.fit_text_to_parent`（実幅の二分探索）。
    """

    #: 親幅から残す左右マージン（ピルが画面いっぱいに広がらないように）。
    _EDGE_MARGIN = overlay_chrome.PILL_EDGE_MARGIN
    _PADDING_V = 12
    _PADDING_H = 28
    _RADIUS = 12

    def __init__(self, parent: QWidget) -> None:
        # 固定スクリム + 明色文字（画像上オーバーレイの固定色例外）。
        # フォントはトークンのタイトル段の 2 倍（直書き禁止規約に沿って
        # FONT_TITLE_PT から導出）。
        super().__init__(
            parent,
            scrim=overlay.SCRIM_HEAVY,
            text_color=_OVERLAY_TEXT,
            font_pt=FONT_TITLE_PT * 2,
            padding_v=self._PADDING_V,
            padding_h=self._PADDING_H,
            radius=self._RADIUS,
            auto_hide_ms=_TITLE_OVERLAY_MS,
            bold=True,
        )
        # 省略前の原文（親幅が変わったら再フィットするため保持する）。
        self._full_text = ""

    def show_message(self, text: str) -> None:
        self._full_text = text
        self._fit_to_parent()
        self.present()

    def reposition(self) -> None:
        if self.isVisible():
            # 表示中に親幅が変わったら省略をやり直す（隠れている間は不要 —
            # 次の show_message が必ずフィットし直す）。
            self._fit_to_parent()
        super().reposition()

    def _fit_to_parent(self) -> None:
        """原文が親幅に収まらなければ末尾を省略して収める."""
        self.fit_text_to_parent(self._full_text, margin=self._EDGE_MARGIN)


class LightboxTopBar(QWidget):
    """上部情報バー（オートハイド対象）: タイトル + スライドショー + 閉じる."""

    HEIGHT = 44

    slideshow_toggled = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 4, 8, 4)
        self._label = QLabel("")
        self._label.setStyleSheet(
            f"QLabel {{ color: {_OVERLAY_TEXT}; background: transparent; }}"
        )
        lay.addWidget(self._label, 1)
        # 印ストリップ（compact・オーバーレイ配色）— 自動消灯するこのバーに
        # 相乗りするので常時表示の増分は 0。入場直後
        # とマウス移動で必ず現れる面なので、★ / あとで見る / タグが「画面を見れば
        # 分かる」位置にある。読み出し専用の左下カウンタ（★N）はそのまま残す。
        self.strip = CurationStrip(self, mode=MODE_COMPACT, on_scrim=True)
        lay.addWidget(self.strip)
        btn_style = (
            "QToolButton { background: transparent; border: none;"
            " padding: 4px; border-radius: 6px; }"
            f"QToolButton:hover {{ background: {overlay.rgba_str(overlay.LIGHTBOX_BTN_HOVER)}; }}"
        )
        self._btn_slideshow = QToolButton()
        self._btn_slideshow.setStyleSheet(btn_style)
        self._btn_slideshow.setIcon(_fixed_icon("play"))
        self._btn_slideshow.setIconSize(QSize(20, 20))
        self._btn_slideshow.setToolTip(t("viewer.lightbox.slideshow_tooltip"))
        self._btn_slideshow.clicked.connect(self.slideshow_toggled.emit)
        lay.addWidget(self._btn_slideshow)
        self._btn_close = QToolButton()
        self._btn_close.setStyleSheet(btn_style)
        self._btn_close.setIcon(_fixed_icon("x"))
        self._btn_close.setIconSize(QSize(20, 20))
        self._btn_close.setToolTip(t("viewer.lightbox.close_tooltip"))
        self._btn_close.clicked.connect(self.close_requested.emit)
        lay.addWidget(self._btn_close)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def set_slideshow_running(self, running: bool) -> None:
        self._btn_slideshow.setIcon(_fixed_icon("pause" if running else "play"))

    def set_slideshow_enabled(self, enabled: bool) -> None:
        """送る先が無いときは再生ボタンを落とす.

        空プレイリストでは ``toggle_slideshow`` が無音で早期 return するので、
        押せる見た目のまま無反応だった。対は
        :meth:`LightboxControlCapsule.set_step_enabled`。
        """
        self._btn_slideshow.setEnabled(bool(enabled))

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.fillRect(self.rect(), _SCRIM)
        painter.end()
        super().paintEvent(event)


class LightboxControlCapsule(OverlayCapsule):
    """常設のオンスクリーン操作カプセル（前後送り・フィット⇄実寸・ズーム表示）.

    ステージの ``image_view_parts.control_bar._ControlBar`` と役割・見た目・トークンの
    使い方を揃えたライトボックス側のカプセル。**構造は共通基底
    :class:`~snappix.common.ui.overlay_chrome.OverlayCapsule` に集約されて
    いる**:
    固定暗スクリム + 明色文字の QSS 生成・``WA_StyledBackground``（これが
    無いとスクリムが一切描かれない）・親矩形クランプ・ボタン生成は
    基底が持ち、ここに残るのは配置方針（フィルムストリップぶんの下端確保）と
    動画時の表示切替だけ。ホスト（:class:`LightboxWindow`）の既存オート
    ハイド（``_set_chrome_visible``）に連動して表示/非表示する — 独自の
    常時稼働タイマーは持たない。
    """

    prev_clicked = Signal()
    next_clicked = Signal()
    fit_toggle_clicked = Signal()

    _MARGIN = overlay_chrome.CAPSULE_MARGIN

    def __init__(self, parent: QWidget) -> None:
        # 固定色: 任意の写真の上に載るため、テーマパレットには追従しない
        # （design.md の画像オーバーレイ色の例外）。ボタンホバーだけは
        # ステージ側（OVERLAY_BTN_HOVER）と α が 5 違う別定数を使う。
        super().__init__(
            parent,
            object_name="lightboxCtrlCapsule",
            button_hover=overlay.LIGHTBOX_BTN_HOVER,
            text_color=_OVERLAY_TEXT,
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)

        # 図像・ツールチップとも「ファイル送り」の軸を名指す —
        # chevron + 「前へ / 次へ」はステージヘッダーの項目送りと同形・同文言
        # だったため、矢印図像 + 専用ツールチップで弁別する。キーの併記は
        # 分割 / 最大化のカプセル（``control_bar._ControlBar``）と同じく表から
        # 引く（全画面の行 = ``desc_prev_next_image_wrap``）。
        from ..shortcuts_dialog import with_key_hint

        step = "viewer.shortcuts_dialog.desc_prev_next_image_wrap"
        self._btn_prev = self._make_button(
            _fixed_icon("arrow-left"),
            with_key_hint(t("viewer.image_view.ctrl_prev_tooltip"), step),
        )
        self._btn_prev.clicked.connect(self.prev_clicked.emit)
        row.addWidget(self._btn_prev)
        self._btn_next = self._make_button(
            _fixed_icon("arrow-right"),
            with_key_hint(t("viewer.image_view.ctrl_next_tooltip"), step),
        )
        self._btn_next.clicked.connect(self.next_clicked.emit)
        row.addWidget(self._btn_next)

        self._zoom_label = QLabel("100%")
        self._zoom_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self._zoom_label)

        self._btn_fit = self._make_button(
            _fit_icon(),
            with_key_hint(
                t("viewer.image_view.ctrl_fit_actual"),
                "viewer.shortcuts_dialog.desc_middle_click_fit",
            ),
        )
        self._btn_fit.clicked.connect(self.fit_toggle_clicked.emit)
        row.addWidget(self._btn_fit)

        # 直近の reposition で確保した下端量（フィルムストリップの高さ）。
        self._bottom_reserved = 0
        self.hide()

    def _make_button(self, ic: QIcon, tooltip: str) -> QToolButton:
        return self.make_button(ic, tooltip)

    def set_zoom(self, percent: float) -> None:
        self._zoom_label.setText(format_zoom_readout(percent))
        # 桁数の変化で幅が変わるので、その場で再センタリングする
        # （resizeEvent 待ちにすると中央からずれたままになる）。
        self.reposition(self._bottom_reserved)

    def set_step_enabled(self, can_prev: bool, can_next: bool) -> None:
        """送りボタンの活性を歩ける先の有無に同期する.

        ステージヘッダー側の対は ``stage_view.StageHeader.set_step_enabled``
        （同名・同シグネチャ）。全画面では端でも投稿横断・端予告という反応が
        あるので落ちるのは**空プレイリスト**のときだけだが、席ごとに可否の
        算出規則が違うだけで「歩ける先が無ければ落とす」契約は同じなので
        シグネチャを揃える。

        ``setVisible`` には触れない — 可視はオートハイドの専管
        （:meth:`LightboxWindow._sync_chrome` の契約）。
        """
        self._btn_prev.setEnabled(bool(can_prev))
        self._btn_next.setEnabled(bool(can_next))

    def set_fit_controls_visible(self, visible: bool) -> None:
        """動画再生中はフィット⇄実寸・ズーム表示を隠す（前後送りだけ残す）.

        フィット/ズームは静止画（内部 ImageView）専用の概念で、動画ページ
        （MediaView）表示中は意味を持たない — 隠れた ImageView の古い状態を
        誤って読ませないための切替。
        """
        self._btn_fit.setVisible(visible)
        self._zoom_label.setVisible(visible)
        # 動画⇄静止画の切替で幅が変わる — set_zoom と同じく即再センタリング。
        self.reposition(self._bottom_reserved)

    def reposition(self, bottom_reserved: int) -> None:
        """中央寄せで配置する。

        *bottom_reserved* はフィルムストリップの高さ — 表示/非表示に関わらず
        常に確保しておくことで、ストリップが一瞬出てもカプセルと重ならない
        （``_CounterOverlay._position_counter`` と同じ考え方）。値は保持し、
        幅が変わる操作（:meth:`set_zoom` / :meth:`set_fit_controls_visible`）が
        同じ確保量でその場で再センタリングできるようにする。
        """
        self._bottom_reserved = bottom_reserved
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = (parent.width() - self.width()) // 2
        y = parent.height() - bottom_reserved - self.height() - self._MARGIN
        self.move_clamped(x, y)


__all__ = [
    "CenterMessageOverlay",
    "EmptyPlaylistView",
    "LightboxControlCapsule",
    "LightboxTopBar",
]
