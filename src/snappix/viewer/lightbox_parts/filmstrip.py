"""閲覧モード下端のサムネイル帯 :class:`FilmstripView`.

可視セルだけを自前描画する横スクロールの帯。サムネの取得は**ホストに委譲**
する（``request_thumb`` を emit し、ホストが右ペインの常駐 pixmap または
``ThumbnailLoader`` 経由で :meth:`FilmstripView.set_thumb` を返す）ので、
このモジュール自体はスレッドもディスクも触らない。

色フックは固定オーバーレイ色（画像コンテンツの上に載る面なので design.md の
固定色例外）。地色の :data:`~snappix.viewer.lightbox_parts.overlays._SCRIM` は
クローム全体と同じ 1 つの定数を引く — 閲覧モードの固定色は
:mod:`~snappix.viewer.lightbox_parts.overlays` に集約する規約で、ここで別の
スクリムを定義しない。テーマ追従が要る席（``stage_view.StageFilmstrip``）は
色フックを override して派生する。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QEvent, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ...common.ui import DARK_TOKENS, overlay
from .._indicator import badge_pixmap
from .overlays import _SCRIM


class FilmstripView(QWidget):
    """下端の横スクロールサムネイル帯（現在投稿内）.

    表示制御は上部バーのオートハイドとは独立で、ホスト（:class:`LightboxWindow`）
    が「画像移動時 + 下端ホバー時のみ表示、専用タイマーで自動消灯」を担う。

    自前描画（可視セルのみ）。サムネの取得はホストに委譲する —
    ``request_thumb(path)`` を emit し、ホストが右ペインの常駐 pixmap または
    ``ThumbnailLoader`` 経由で :meth:`set_thumb` を返す（可視範囲限定要求）。
    """

    THUMB_EDGE = 64   # 論理 px の正方形セル
    _GAP = 6
    _PAD = 6

    clicked = Signal(int)          # セルクリック → そのインデックスへジャンプ
    request_thumb = Signal(object)  # Path — ホストがサムネを供給する

    # ------------------------------------------------------ colour hooks
    # ライトボックスの帯は「画像コンテンツ上のオーバーレイ」なので固定
    # 暗色スクリム + 固定アクセント（design.md の例外規定）。メインウィンドウ
    # のステージモードで再利用する StageFilmstrip（stage_view.py）はこれらを
    # パレット / トークン参照へオーバーライドしてテーマ追従にする。

    def _bg_color(self) -> QColor:
        return _SCRIM

    def _frame_color(self) -> QColor:
        return QColor(DARK_TOKENS.accent)

    def _placeholder_color(self) -> QColor:
        return overlay.PLACEHOLDER_WHITE

    def _mat_color(self) -> QColor:
        """読み込み済みセルの台紙色.

        セルは正方スロット固定なので、縦長／横長の画像では枠の中に地色が
        残り、選択枠が画像から離れて見えていた。台紙を**常に**敷くことで
        「額装された 1 枚」として読める。読み込み前の
        :meth:`_placeholder_color` はより濃いトーンのまま残す — 台紙を
        常設すると「地色 = まだ読めていない」という暗黙のローディング表現が
        使えなくなるため、2 段のトーンで区別を保つ。
        """
        return overlay.CELL_MAT_WHITE

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._paths: list[Path] = []
        self._path_set: set[Path] = set()
        self._pixmaps: dict[Path, QPixmap] = {}
        self._requested: set[Path] = set()
        # 失敗確定セル（mark_failed）の集合 — reset_failed だけが選択的に
        # 再試行対象へ戻す。
        self._failed: set[Path] = set()
        self._current = -1
        self._offset = 0  # 水平スクロールオフセット px
        # 兄弟セルの評価済みマーク。
        # ``(star, later)`` を返す同期のインメモリ参照だけを受け付ける
        # （描画のたびに呼ぶので sqlite / NAS へは触らないこと）。
        self._curation_provider: Callable[[Path], tuple[int, bool]] | None = None
        # ★マークの描画済みピクスマップ。``badge_pixmap``
        # は内部キャッシュを持たないので、素のままだと可視セルの数だけ毎フレーム
        # SVG を描き直す。キーに dpr（float）を含めるのはモニター間移動で物理
        # サイズが変わるため。インスタンス属性なのは色がテーマトークン由来だから
        # — モジュールレベルの永続辞書にするとテーマ切替で古い色が残る
        # （:meth:`changeEvent` が捨てる）。GUI スレッド専用。
        self._badge_cache: dict[tuple[str, int, float], QPixmap] = {}
        self.setFixedHeight(self.THUMB_EDGE + self._PAD * 2)
        self.setMouseTracking(True)

    # ------------------------------------------------------------- API

    def set_images(self, paths: Sequence[Path]) -> None:
        self._paths = list(paths)
        self._path_set = set(self._paths)
        self._pixmaps.clear()
        self._requested.clear()
        self._failed.clear()
        self._current = -1
        self._offset = 0
        self.update()
        self._request_visible()

    def set_current(self, index: int) -> None:
        self._current = index
        self._ensure_visible(index)
        self.update()
        self._request_visible()

    def set_curation_provider(
        self, provider: "Callable[[Path], tuple[int, bool]] | None",
    ) -> None:
        """兄弟セルの★有無マークのためのプロバイダ注入.

        1 枚ずつ評価していく面（最大化のステージトラック / 全画面の下端
        ストリップ）で「どれを既に評価したか」が一望できなかった。
        **値は描かない**（64px セルに数字は読めないうえ、没入表示を
        汚さないという判断とも整合する）— 印の有無だけ。
        ``StageFilmstrip`` は本クラスの派生なので 1 実装で 2 面に効く。
        未注入なら何も描かない（素の閲覧はそのまま動く）。
        """
        self._curation_provider = provider
        self.update()

    def paths(self) -> list[Path]:
        """The strip's current path list (display order) — host sync helper."""
        return list(self._paths)

    def current_index(self) -> int:
        """Currently highlighted cell index (−1 = none)."""
        return self._current

    def set_thumb(self, path: Path, pixmap: QPixmap) -> None:
        if path not in self._path_set or pixmap is None or pixmap.isNull():
            return
        # セルの物理 px へ 1 回だけ縮小して保持する（paint 毎のスケール回避）。
        # 供給元（右ペイン / ローダー）の pixmap は dpr を持ち得るので剥がして
        # から正確な物理サイズへ縮小し、セルの dpr を焼き直す。
        dpr = self.devicePixelRatioF()
        src = pixmap
        if src.devicePixelRatio() != 1.0:
            src = QPixmap(pixmap)
            src.setDevicePixelRatio(1.0)
        phys = max(1, round(self.THUMB_EDGE * dpr))
        scaled = src.scaled(
            phys, phys, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        if dpr > 1.0:
            scaled.setDevicePixelRatio(dpr)
        self._pixmaps[path] = scaled
        # 成功サムネの供給は失敗確定を上書きする（グリッド由来の常駐 pixmap が
        # 後から届いたケース等）— 失敗マークを残すと次の reset_failed が
        # 正常なサムネを不要に破棄してしまう。
        self._failed.discard(path)
        self.update()

    def mark_failed(self, path: Path, glyph: QPixmap) -> None:
        """デコード失敗セルを *glyph* で確定させる.

        :meth:`set_thumb` と同じく pixmap を常駐させて再要求を抑止しつつ、
        「失敗」として記録する — パス列が不変の再スキャン（F5）では
        :meth:`set_images` が呼ばれない（取得済みサムネの破棄防止）ため、
        失敗セルだけを選択的に再試行へ戻す入口（:meth:`reset_failed`）が
        必要になる。
        """
        if path not in self._path_set:
            return
        self.set_thumb(path, glyph)
        if path in self._pixmaps:  # set_thumb が受理した場合のみ
            self._failed.add(path)

    def reset_failed(self) -> None:
        """失敗確定セルを再デコードの対象へ戻す（F5 再スキャン）.

        失敗グリフの常駐 pixmap と request 済みマークを落とし、可視セルなら
        即座に再要求する — ファイル修復後の F5 でグリフが残り続けないための
        再試行入口。成功サムネのセルには触れない。
        """
        if not self._failed:
            return
        for p in self._failed:
            self._pixmaps.pop(p, None)
            self._requested.discard(p)
        self._failed.clear()
        self.update()
        self._request_visible()

    # -------------------------------------------------------- geometry

    def _cell_span(self) -> int:
        return self.THUMB_EDGE + self._GAP

    def _cell_x(self, index: int) -> int:
        return self._PAD + index * self._cell_span() - self._offset

    def _index_at(self, x: int) -> int:
        rel = x - self._PAD + self._offset
        if rel < 0:
            return -1
        i = rel // self._cell_span()
        if i >= len(self._paths):
            return -1
        # セル間ギャップのクリックは無視
        if rel % self._cell_span() >= self.THUMB_EDGE:
            return -1
        return int(i)

    def _max_offset(self) -> int:
        n = len(self._paths)
        if n == 0:
            return 0
        total = self._PAD * 2 + n * self.THUMB_EDGE + (n - 1) * self._GAP
        return max(0, total - self.width())

    def _ensure_visible(self, index: int) -> None:
        if not (0 <= index < len(self._paths)):
            return
        x0 = self._PAD + index * self._cell_span()
        x1 = x0 + self.THUMB_EDGE
        if x0 - self._offset < self._PAD:
            self._offset = x0 - self._PAD
        elif x1 - self._offset > self.width() - self._PAD:
            self._offset = x1 - (self.width() - self._PAD)
        self._offset = max(0, min(self._max_offset(), self._offset))

    def _visible_range(self) -> tuple[int, int]:
        if not self._paths:
            return 0, -1
        span = self._cell_span()
        first = max(0, (self._offset - self._PAD) // span)
        last = min(
            len(self._paths) - 1,
            (self._offset + self.width()) // span,
        )
        return int(first), int(last)

    def _request_visible(self) -> None:
        first, last = self._visible_range()
        buffer = 4
        lo = max(0, first - buffer)
        hi = min(len(self._paths) - 1, last + buffer)
        for i in range(lo, hi + 1):
            p = self._paths[i]
            if p in self._pixmaps or p in self._requested:
                continue
            self._requested.add(p)
            self.request_thumb.emit(p)

    #: セル隅の★マークの一辺（論理 px）。64px セルに対して主張しすぎない大きさ。
    _CURATION_MARK_PX = 14

    def _paint_curation_mark(self, painter, box: QRect, path: Path) -> None:
        """★を付けてあるセルの右上に小さな印を描く.

        図像は語彙レジストリの実物チップ（``badge_pixmap``）— 凡例・タイル・
        情報パネルと同じ絵になる。``text=""`` で数字を持たないグリフだけを
        描く。プロバイダが無い / 例外を投げる場合は何も描かない（帯の描画は
        キュレーションの有無に依存しない）。

        同じ絵を可視セルの数だけ毎フレーム描き直さないよう
        :attr:`_badge_cache` を通す。
        """
        provider = self._curation_provider
        if provider is None:
            return
        try:
            star, _later = provider(path)
        except Exception:  # pragma: no cover (defensive: paint must not raise)
            return
        if not star:
            return
        pm = self._badge("star", self._CURATION_MARK_PX)
        logical = pm.deviceIndependentSize().toSize()
        painter.drawPixmap(
            box.right() - logical.width() - 2, box.top() + 2, pm,
        )

    def _badge(self, kind: str, size: int) -> QPixmap:
        """``badge_pixmap`` のインスタンス内メモ化."""
        key = (kind, size, self.devicePixelRatioF())
        pm = self._badge_cache.get(key)
        if pm is None:
            pm = badge_pixmap(kind, size=size, dpr=key[2], text="")
            self._badge_cache[key] = pm
        return pm

    # ---------------------------------------------------------- events

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """テーマ / パレットが変わったら描画済みバッジを捨てる.

        バッジの色はテーマトークン由来なので、持ち越すと切替後も古い色の★が
        残る。``StageFilmstrip``（テーマ追従の派生）でも同じ 1 実装で効く。
        """
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ThemeChange,
            QEvent.Type.StyleChange,
        ):
            self._badge_cache.clear()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._bg_color())
        first, last = self._visible_range()
        edge = self.THUMB_EDGE
        for i in range(first, last + 1):
            box = QRect(self._cell_x(i), self._PAD, edge, edge)
            pm = self._pixmaps.get(self._paths[i])
            if pm is not None and not pm.isNull():
                # 台紙を先に敷いてから画像を中央へ — 正方スロットに
                # 対して縦長／横長の画像は必ず余白を残すので、余白が地色の
                # ままだと選択枠だけが画像から離れて浮いて見えた。
                painter.fillRect(box, self._mat_color())
                # set_thumb で既にセルサイズへ縮小済み — 論理サイズで中央配置。
                logical = pm.deviceIndependentSize().toSize()
                target = QRect(0, 0, logical.width(), logical.height())
                target.moveCenter(box.center())
                painter.drawPixmap(target, pm)
            else:
                painter.fillRect(box, self._placeholder_color())
            self._paint_curation_mark(painter, box, self._paths[i])
            if i == self._current:
                pen = painter.pen()
                pen.setColor(self._frame_color())
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(box.adjusted(1, 1, -1, -1))
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._index_at(int(event.position().x()))
            if index >= 0:
                self.clicked.emit(index)
                event.accept()
                return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self._offset = max(
                0, min(self._max_offset(), self._offset - delta)
            )
            self.update()
            self._request_visible()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._offset = max(0, min(self._max_offset(), self._offset))
        self._request_visible()


__all__ = ["FilmstripView"]
