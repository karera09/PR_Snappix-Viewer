"""印ストリップ — ★ / あとで見る / ユーザータグ を対象に束ねた 1 部品を 3 席に置く。

UIレビュー 2026-09-11 のリデザイン E2。印を付ける操作面が右クリックと数字キーに
散り、情報パネルの印の行は post.md の有無に従属して消えていた（N-53）。
ユーザータグには常設の「付ける」面が無かった（N-44）。この部品を

* **分割ビュー**: 情報パネル最上段（full 変種 — 常設・post.md 非依存）
* **プレビュー最大化**: ステージヘッダーの旧 ★N ラベルの席（compact 変種・高さ増 0）
* **全画面**: 上部バー（compact 変種・オーバーレイ配色 — 自動消灯に相乗りするので
  常時表示の増分 0）

に置く。3 席とも同じ絵・同じ対象規則で、対象は席の外（``ViewerWindow`` /
``LightboxWindow``）が :meth:`CurationStrip.set_target` で渡す（ダムビュー）。
書き込みは :attr:`CurationStrip.curation_requested` を単一書き手
``PostGrid.request_curation`` へつなぐだけ。

不変:

* **全子が ``NoFocus``** — フォーカスを取ると ``focus_target.focused_seat`` の
  席判定が変わり、``L`` / 0-5 が別の項目へ書く（N-16 の再発）。マウス専用の面で、
  キーボードは既存の 0-5 / ``L`` / 右クリックが担う。
* 店（``user_meta``）が無ければ :meth:`set_store_available` で丸ごと隠す
  （右クリックの印の節が出ない劣化と同じ）。
* 星の図像は ``_indicator._star_path`` / ``overlay.STAR_GOLD``、あとで見るは
  ``_indicator.draw_clock_glyph`` — タイルのバッジと同じ絵（バッジ語彙レジストリ。
  チップ背景は付けない: 16px に縮めると背景ごと青い点にしか見えない）。
  色は全てトークン / オーバーレイ定数経由（design.md）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from ..common.i18n import t
from ..common.ui import (
    FONT_CAPTION_PT,
    RADIUS_SM,
    chip_style,
    current_tokens,
    fixed_icon,
    overlay,
    set_icon,
)
from ._indicator import _star_path, draw_clock_glyph

MODE_FULL = "full"
MODE_COMPACT = "compact"

#: ★ 1 個の当たり判定（20 × 24）— 28px ヘッダー内でも「★3 のつもりが ★2」を
#: 起こさない下限（印はユーザーデータで再生成不能）。
STAR_CELL_W = 20
STAR_CELL_H = 24
#: ★行の固定幅（``_StarRow`` は素の QWidget なので sizeHint を持たない）。
_STAR_ROW_W = STAR_CELL_W * 5
_STAR_GLYPH = 14
_MAX_CHIPS = 2
#: タグチップはピル型（チップ高 ≈18px の半分 — レイアウト寸法）。
_CHIP_RADIUS = 9
#: チップ列の内部間隔（レイアウト寸法）。
_CHIP_SPACING = 4
#: チップ 1 個の表示幅の下限。これを割ると省略記号だけの札になり「別名のタグ」に
#: 読めるので、席が確保できないチップは出さない（幅の足りない席では落とす）。
_CHIP_MIN_W = 56


class _StarRow(QWidget):
    """★×5 — クリックで設定、同じ星をもう一度で解除。ホバーで仮点灯。"""

    star_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None, *, on_scrim: bool = False) -> None:
        super().__init__(parent)
        self._on_scrim = on_scrim
        self._value = 0
        self._hover: int | None = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_STAR_ROW_W, STAR_CELL_H)
        self.setToolTip(t("viewer.curation_strip.star_tooltip"))

    def value(self) -> int:
        return self._value

    def set_value(self, star: int) -> None:
        star = max(0, min(5, int(star or 0)))
        if star != self._value:
            self._value = star
            self.update()

    def index_at(self, x: int) -> int | None:
        """x 座標（ウィジェット内）に当たる星の番号 1〜5（外なら ``None``）."""
        idx = int(x) // STAR_CELL_W + 1
        return idx if 1 <= idx <= 5 else None

    def _outline_color(self) -> QColor:
        if self._on_scrim:
            return QColor(overlay.OVERLAY_TEXT_DIM)
        return QColor(current_tokens().text_muted)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        shown = self._hover if self._hover is not None else self._value
        fill = QColor(overlay.STAR_GOLD)
        preview = QColor(overlay.STAR_GOLD)
        preview.setAlpha(140)
        pen = QPen(self._outline_color())
        pen.setWidthF(1.2)
        for i in range(1, 6):
            cell_x = (i - 1) * STAR_CELL_W
            rect = QRectF(
                cell_x + (STAR_CELL_W - _STAR_GLYPH) / 2.0,
                (STAR_CELL_H - _STAR_GLYPH) / 2.0,
                _STAR_GLYPH, _STAR_GLYPH,
            )
            path = _star_path(rect)
            if i <= shown:
                painter.setPen(Qt.PenStyle.NoPen)
                # 確定値は濃く、ホバーの仮点灯は薄く — 押す前に効果が読める。
                painter.setBrush(fill if i <= self._value else preview)
            else:
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        idx = self.index_at(int(event.position().x()))
        if idx != self._hover:
            self._hover = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # 押下も自分で受ける（親へ渡すと席のクリック処理が動く）。
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # 「同じ星をもう一度で解除」は素早い 2 クリックになりやすく、Qt は 2 回目の
        # 押下を DblClick として届ける。既定実装は ignore して親へ抜けるので、
        # ステージヘッダーの席では ``PreviewColumn`` のダブルクリック（最大化⇄分割）
        # まで発火した（PR #184 レビュー。``StageFilmstrip`` と同じ封じ込め）。
        # 2 回目の release は通常どおり :meth:`mouseReleaseEvent` へ届く。
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.index_at(int(event.position().x()))
            if idx is not None:
                # 同じ星をもう一度 = 解除（0）。数字キーの 0 と同じ意味。
                self.star_clicked.emit(0 if idx == self._value else idx)
            event.accept()
            return
        super().mouseReleaseEvent(event)


_LATER_ICON_PX = 16


def _clock_icon(widget: QWidget) -> QIcon:
    """「あとで見る」の時計グリフだけを描いたアイコン（DPR 対応）."""
    dpr = max(1.0, float(widget.devicePixelRatioF()))
    px = int(round(_LATER_ICON_PX * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.scale(dpr, dpr)
    draw_clock_glyph(painter, QRectF(1.0, 1.0, _LATER_ICON_PX - 2.0, _LATER_ICON_PX - 2.0))
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


def _scrim_button_style() -> str:
    return (
        "QToolButton { background: transparent; border: none;"
        f" padding: 2px 4px; border-radius: {RADIUS_SM}px;"
        f" color: {overlay.rgba_str(overlay.OVERLAY_TEXT)}; }}"
        "QToolButton:hover, QToolButton:checked {"
        f" background: {overlay.rgba_str(overlay.LIGHTBOX_BTN_HOVER)}; }}"
    )


def _chip_style(on_scrim: bool) -> str:
    if on_scrim:
        # 画像上のスクリムはテーマに依らない固定配色（design.md）。
        return (
            "QLabel {"
            f" border: 1px solid {overlay.rgba_str(overlay.OVERLAY_TEXT_DIM)};"
            f" border-radius: {_CHIP_RADIUS}px; padding: 0 6px;"
            f" color: {overlay.rgba_str(overlay.OVERLAY_TEXT)};"
            f" background: transparent; font-size: {FONT_CAPTION_PT}pt; }}"
        )
    # 土台は共有ヘルパ ``common.ui.chip_style``（枠は ``palette(midlight)`` =
    # 髪の毛線の ``border`` ロール、角丸と点サイズもここから）。この席だけの
    # 差分 — 地色を敷かない・文字は控えめな ``palette(mid)`` — を後追いの宣言で
    # 重ねる。どちらもロール参照なので、テーマ切替が QPalette を差し替えるだけで
    # 追随する（値を 16 進で焼くとチップだけ旧テーマの色で残る）。
    return (
        "QLabel {"
        f" {chip_style(font_pt=FONT_CAPTION_PT, radius=_CHIP_RADIUS)}"
        " background: transparent; color: palette(mid); padding: 0 6px; }"
    )


class CurationStrip(QWidget):
    """[★★★★★][あとで見る][タグ… +] を 1 対象に束ねた操作面。

    ``mode="full"`` は情報パネル用（あとで見るに文字、タグはチップ列、右端に
    対象名）。``mode="compact"`` はヘッダー / 全画面の上部バー用（アイコンだけ、
    タグは件数付きボタン 1 個）。``on_scrim=True`` で画像上のオーバーレイ配色。
    """

    #: (path, kind, value) — kind ∈ {"star", "later", "edit_tags"}。
    curation_requested = Signal(object, str, object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        mode: str = MODE_FULL,
        on_scrim: bool = False,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._on_scrim = on_scrim
        self._target: Path | None = None
        self._tags: tuple[str, ...] = ()
        self._later = False
        self._store_available = False
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        if mode == MODE_FULL:
            layout.setContentsMargins(10, 4, 10, 4)
        else:
            layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._stars = _StarRow(self, on_scrim=on_scrim)
        self._stars.star_clicked.connect(self._on_star)
        layout.addWidget(self._stars)

        self._later_btn = QToolButton(self)
        self._later_btn.setCheckable(True)
        self._later_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._later_btn.setIcon(_clock_icon(self))
        self._later_btn.setIconSize(QSize(_LATER_ICON_PX, _LATER_ICON_PX))
        self._later_btn.setToolTip(t("viewer.post_grid.watch_later_menu"))
        if mode == MODE_FULL:
            self._later_btn.setText(t("viewer.post_grid.watch_later"))
            self._later_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        else:
            self._later_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._later_btn.clicked.connect(self._on_later)
        layout.addWidget(self._later_btn)

        # タグ: full = チップ列 + [+]、compact = 件数付きの 1 ボタン。どちらも
        # 押下は「ユーザータグを編集…」（補完つきダイアログは左ペインの 1 実装）。
        self._chips: list[QLabel] = []
        #: チップの全文 / 自然幅 / 文字以外の幅（枠 + 余白）— 畳みの計算に使う。
        self._chip_full: list[str] = []
        self._chip_nat: list[int] = []
        self._chip_chrome: list[int] = []
        self._chip_host: QWidget | None = None
        if mode == MODE_FULL:
            self._chip_host = QWidget(self)
            chips = QHBoxLayout(self._chip_host)
            chips.setContentsMargins(0, 0, 0, 0)
            chips.setSpacing(_CHIP_SPACING)
            layout.addWidget(self._chip_host)
        self._tags_btn = QToolButton(self)
        self._tags_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tags_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tag_glyph = "plus" if mode == MODE_FULL else "tag"
        if on_scrim:
            # 画像上のクロームはテーマ追従の ``icon()`` を使わない（design.md —
            # ライトテーマで暗色になり暗スクリム上で読めない）。上部バーの
            # 再生 / 閉じると同じ固定色。
            self._tags_btn.setIcon(fixed_icon(tag_glyph, overlay.LABEL_TEXT, size=16))
        else:
            set_icon(self._tags_btn, tag_glyph)
        self._tags_btn.setToolTip(t("viewer.post_grid.edit_user_tags"))
        self._tags_btn.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonIconOnly if mode == MODE_FULL
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._tags_btn.clicked.connect(self._on_edit_tags)
        layout.addWidget(self._tags_btn)

        layout.addStretch(1)

        if on_scrim:
            style = _scrim_button_style()
            self._later_btn.setStyleSheet(style)
            self._tags_btn.setStyleSheet(style)

        # 「あとで見る」の 2 形（ラベル付き / アイコンのみ）の幅をここで 1 度だけ
        # 測る。畳みの判定のたびに測り直すとレイアウト無効化が resizeEvent の中で
        # 連鎖するため、値は構築時に取っておく。
        self._later_text_w = self._later_btn.sizeHint().width()
        if mode == MODE_FULL:
            self._later_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self._later_icon_w = self._later_btn.sizeHint().width()
            self._later_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        else:
            self._later_icon_w = self._later_text_w
        self._later_compact = mode != MODE_FULL

        self._sync_visibility()

    # ------------------------------------------------------------------ API

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # 帯の余白でも親（ステージヘッダー → PreviewColumn の分割⇄最大化）へ
        # 抜けさせない — 操作面の中で起きたことは操作面で閉じる。
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mode(self) -> str:
        return self._mode

    def target(self) -> Path | None:
        return self._target

    def set_store_available(self, available: bool) -> None:
        """店（user_meta）の有無。無ければ面ごと隠す（右クリックの節と同じ劣化）."""
        self._store_available = bool(available)
        self._sync_visibility()

    def set_target(
        self,
        path: Path | None,
        star: int = 0,
        later: bool = False,
        tags: tuple[str, ...] | list[str] = (),
    ) -> None:
        """対象と現在の印を差し替える（同期・I/O なし — 値は呼び出し側の map）."""
        self._target = path
        self._tags = tuple(tags or ())
        self._stars.set_value(star)
        self._later = bool(later)
        self._later_btn.blockSignals(True)
        self._later_btn.setChecked(self._later)
        self._later_btn.blockSignals(False)
        self._rebuild_tags()
        # 「この印はどれに付くか」は面自身が名乗る（N-16 / N-88 の教訓）—
        # 席の見出し側（情報パネルの節見出し）が名前を出し、こちらはツールチップ。
        tip = t("viewer.curation_strip.target_tooltip", path=path) if path else ""

        def _with_target(base: str) -> str:
            return f"{base}\n{tip}" if tip else base

        self._stars.setToolTip(_with_target(t("viewer.curation_strip.star_tooltip")))
        self._later_btn.setToolTip(_with_target(t("viewer.post_grid.watch_later_menu")))
        n = len(self._tags)
        tags_tip = (
            t("viewer.curation_strip.tags_tooltip", n=n) if n
            else t("viewer.post_grid.edit_user_tags")
        )
        self._tags_btn.setToolTip(_with_target(tags_tip))
        self._sync_visibility()

    def star_value(self) -> int:
        return self._stars.value()

    def later_checked(self) -> bool:
        return self._later_btn.isChecked()

    def tag_texts(self) -> list[str]:
        """チップが表す文字列（全文 — テスト用）."""
        return list(self._chip_full)

    def tag_display_texts(self) -> list[str]:
        """いま実際に描かれているチップ文字列（省略後・非表示は除く — テスト用）."""
        return [c.text() for c in self._chips if not c.isHidden()]

    def star_row(self) -> _StarRow:
        """★行（テスト・座標計算用）."""
        return self._stars

    # ------------------------------------------------------- 幅に合わせて畳む

    def natural_width(self) -> int:
        """全要素を自然幅で並べたときに要る幅（席が確保を試みる幅）."""
        parts = self._natural_parts()
        margins = self.layout().contentsMargins()
        return (
            margins.left() + margins.right()
            + sum(parts) + self.layout().spacing() * len(parts)
        )

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        """自然幅は畳んでいる最中も変えない.

        畳むと ``QHBoxLayout`` の合計は縮むので、素の ``sizeHint`` を返すと
        「狭いから畳む → 要求幅が下がる → 席がその幅しか渡さない → 二度と
        戻らない」という片道の劣化になる。要求は常に自然幅で出し、実際に
        渡された幅だけを見て畳む。
        """
        hint = super().sizeHint()
        return QSize(max(hint.width(), self.natural_width()), hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        """最小は★行だけ.

        素の最小（★ + あとで見る + タグ）を名乗ると、席の ``QHBoxLayout`` が
        空間不足でそれを割り込ませるときに子の矩形が重なり、後から置いた
        「あとで見る」が★のクリックを奪う。★行 1 本まで縮めると宣言して
        おけば、席は重ねる前にこちらを縮め、畳みが効く。
        """
        hint = super().minimumSizeHint()
        margins = self.layout().contentsMargins()
        return QSize(margins.left() + margins.right() + _STAR_ROW_W, hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._apply_fold(event.size().width())

    # ------------------------------------------------------------ internals

    def _chips_natural_width(self) -> int:
        """チップ列を自然幅で並べたときの幅（チップが無ければ 0）."""
        if not self._chips:
            return 0
        return sum(self._chip_nat) + _CHIP_SPACING * (len(self._chips) - 1)

    def _natural_parts(self) -> list[int]:
        parts = [_STAR_ROW_W]
        chips_w = self._chips_natural_width()
        if chips_w:
            parts.append(chips_w)
        parts.append(self._later_text_w)
        parts.append(self._tags_btn.sizeHint().width())
        return parts

    def _apply_fold(self, width: int | None = None) -> None:
        """幅に入らない要素を重ねずに落とす（★行の当たり判定は必ず残す）.

        落とす順は 「あとで見る」のラベル → チップ列（後ろから）→ タグの入口
        → 「あとで見る」自体。★×5 は印がユーザーデータで再生成できないため
        最後まで残す（当たり判定 20px/個 を削らない）。

        ラベルを先に落とすのは、同じ意味がアイコン + ツールチップで読めるのに
        対しチップはタグ名そのもの（他に出ない情報）だから。順序を逆にすると
        「席を広げたらチップが消える」という幅に対して非単調な劣化になる
        （各要素の出現しきい値が、自分より優先度の高い要素の幅の総和に
        なっている限り、畳みは幅に対して単調）。
        """
        layout = self.layout()
        if layout is None:
            return
        if width is None:
            width = self.width()
        if width <= 0:
            # まだ席が決まっていない（構築直後）— 畳む根拠が無いので触らない。
            return
        margins = layout.contentsMargins()
        gap = layout.spacing()
        avail = width - margins.left() - margins.right()

        def fits(*parts: int) -> bool:
            used = [p for p in parts if p > 0]
            return sum(used) + gap * len(used) <= avail

        stars_w = _STAR_ROW_W
        tags_w = self._tags_btn.sizeHint().width()
        # 「あとで見る」はラベルを落としてでも残す（アイコンのみ + ツールチップ）。
        # ラベルを出すのはチップ列が自然幅で座れるときだけ — チップの席を
        # 削って出すと、席を広げた側でチップが消える。
        compact_later = not fits(
            stars_w, self._later_text_w, tags_w, self._chips_natural_width(),
        )
        later_w = self._later_icon_w if compact_later else self._later_text_w
        show_later = fits(stars_w, later_w)
        show_tags = show_later and fits(stars_w, later_w, tags_w)
        if self._mode == MODE_FULL and compact_later != self._later_compact:
            self._later_compact = compact_later
            self._later_btn.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonIconOnly if compact_later
                else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
        self._later_btn.setVisible(show_later)
        self._tags_btn.setVisible(show_tags)
        if self._chip_host is not None:
            used = [p for p in (stars_w, later_w if show_later else 0,
                                tags_w if show_tags else 0) if p > 0]
            # チップ列ぶんの間隔を 1 つ余分に引く（列を出すなら必ず 1 つ増える）。
            room = avail - sum(used) - gap * (len(used) + 1)
            self._fit_chips(room)

    def _fit_chips(self, room: int) -> None:
        """残り幅をチップへ配り、入り切らない分は省略記号／席ごと落とす."""
        host = self._chip_host
        if host is None:
            return
        chips = self._chips
        if not chips:
            host.setVisible(False)
            return
        left = max(0, room)
        shown = 0
        for chip, full, nat, chrome in zip(
            chips, self._chip_full, self._chip_nat, self._chip_chrome, strict=False,
        ):
            need = min(nat, _CHIP_MIN_W)
            extra = _CHIP_SPACING if shown else 0
            if left - extra < need:
                chip.setVisible(False)
                continue
            left -= extra
            alloc = min(nat, left)
            left -= alloc
            shown += 1
            chip.setFixedWidth(alloc)
            chip.setText(
                chip.fontMetrics().elidedText(
                    full, Qt.TextElideMode.ElideRight, max(0, alloc - chrome),
                )
            )
            chip.setVisible(True)
        host.setVisible(shown > 0)

    def _sync_visibility(self) -> None:
        self.setVisible(self._store_available and self._target is not None)
        self._apply_fold()

    def _rebuild_tags(self) -> None:
        if self._mode == MODE_COMPACT:
            n = len(self._tags)
            self._tags_btn.setText(str(n) if n else "")
            return
        host = self._chip_host
        if host is None:
            return
        for chip in self._chips:
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []
        self._chip_full = []
        self._chip_nat = []
        self._chip_chrome = []
        chips_layout = host.layout()
        style = _chip_style(self._on_scrim)
        shown = self._tags[:_MAX_CHIPS]
        labels: list[tuple[str, str]] = [(text, text) for text in shown]
        rest = len(self._tags) - len(shown)
        if rest > 0:
            labels.append((
                t("viewer.curation_strip.more_tags", n=rest),
                t("common.sep.comma").join(self._tags[_MAX_CHIPS:]),
            ))
        for text, tip in labels:
            chip = QLabel(text, host)
            chip.setStyleSheet(style)
            chip.setToolTip(tip)
            chips_layout.addWidget(chip)
            self._chips.append(chip)
            self._chip_full.append(text)
            # 自然幅と「文字以外」の幅（枠 + 左右余白）を全文のうちに測る。
            # 省略後の文字を入れてからでは自然幅が分からなくなる。
            nat = chip.sizeHint().width()
            self._chip_nat.append(nat)
            self._chip_chrome.append(
                max(0, nat - chip.fontMetrics().horizontalAdvance(text))
            )

    def _on_star(self, star: int) -> None:
        if self._target is not None:
            self.curation_requested.emit(self._target, "star", int(star))

    def _on_later(self, checked: bool = False) -> None:
        # ダムビュー: ボタンの自己トグルは戻し、要求だけを出す。書けたときは
        # ホストが ``curation_changed`` → ``set_target`` で新しい値を配る。
        # 書けなかったとき（読み取り専用ボリューム等）に画面だけ ON で固まり、
        # 次のクリックが「外す」要求になる事故を防ぐ（PR #184 レビュー）。
        self._later_btn.blockSignals(True)
        self._later_btn.setChecked(self._later)
        self._later_btn.blockSignals(False)
        if self._target is not None:
            self.curation_requested.emit(self._target, "later", not self._later)

    def _on_edit_tags(self) -> None:
        if self._target is not None:
            self.curation_requested.emit(self._target, "edit_tags", None)


__all__ = ["MODE_COMPACT", "MODE_FULL", "STAR_CELL_H", "STAR_CELL_W", "CurationStrip"]
