"""プレビュー列のヘッダーと画像トラック（2026-07 レイアウト再設計・候補A）.

中央 [グリッド | プレビュー] 分割ビューのプレビュー列を構成する 2 部品:

* :class:`StageHeader` — プレビュー列上端の PanelHeader 規格（28px）の常設バー。
  分割時は **[‹] [›]**（グリッド選択を歩む中立の項目送り・アイコンのみ）+
  タイトル + n/m 位置 + 右端の **[⤢ 最大化 (E)]**。プレビュー最大化時は
  左端に **[◧ 分割に戻す (G)]** が出て送りがラベル付きに戻り、右端の最大化
  ボタンが **[⛶ 全画面 (F11)]** に入れ替わる（出口・入口・現在地・位置の可視化）。
* :class:`StageFilmstrip` — **画像トラック（``FILMSTRIP_HEIGHT``=96px）**。
  「現在の投稿（フォルダ）の中身」の列で、表示中ファイルをハイライトし、
  カプセルの ‹ › / ←→ と完全同期する。**プレビュー最大化中のみ表示**
  （分割時は右情報パネルのファイル一覧が同役割）。

旧・投稿トラック（``StagePostStrip`` 56px）は分割ビュー化で完全廃止 —
「他の投稿の同時確認」は常時見えている中央グリッド本体が担う。

ストリップの実体はライトボックスの
:class:`~snappix.viewer.lightbox_parts.filmstrip.FilmstripView`（自前描画・可視セル限定の
サムネ要求・``request_thumb``/``clicked`` シグナル）のサブクラスで、

- **高さ規格** をトークン（``common/ui/tokens.py``）に、
- **配色** を固定暗色スクリム（画像上オーバーレイの例外規定）から
  **パレット参照（テーマ追従）** に

差し替えている。サムネの供給はホスト（main_window）が ``request_thumb`` に
応えて :meth:`set_thumb` を返す契約も不変 — ホスト側はペインの常駐 pixmap を
優先し、無いものだけ専用 ThumbnailLoader（ワーカーは QImage のみ / QPixmap
変換は GUI スレッド）で解決する。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import (
    FILMSTRIP_HEIGHT,
    FONT_CAPTION_PT,
    PANEL_HEADER_HEIGHT,
    ElidedLabel,
    FocusBandMixin,
    current_tokens,
    hint_style,
    set_icon,
)
from .curation_strip import MODE_COMPACT, CurationStrip
from .lightbox import FilmstripView

# Qt の ``QWIDGETSIZE_MAX``（PySide6 は定数を公開していない）= 幅の制約なし。
_UNCONSTRAINED_WIDTH = 16777215


class StageFilmstrip(FilmstripView):
    """テーマ追従・96px 規格の画像トラック（プレビュー最大化中のみ表示）."""

    # FILMSTRIP_HEIGHT = セル edge + 上下 PAD（FilmstripView.__init__ が
    # ``THUMB_EDGE + _PAD * 2`` を setFixedHeight する）。定義を逆算で束ねて
    # おくことで、トークンを変えれば帯全体が追従する。
    THUMB_EDGE = FILMSTRIP_HEIGHT - 2 * FilmstripView._PAD

    # ---------------------------------------------------- colour overrides
    # メインウィンドウの UI 面に常設される帯だが、ステージ演出
    # では中央プレビューと同じ ``bg_stage`` 地色を敷いて「展示台」を中央〜
    # 下端でひと続きに見せる。トークン参照 (current_tokens) なのでテーマに
    # 追従する（色はハードコードしない）。

    def _bg_color(self) -> QColor:
        return QColor(current_tokens().bg_stage)

    def _frame_color(self) -> QColor:
        return self.palette().highlight().color()

    def _placeholder_color(self) -> QColor:
        # 独自描画トーン規約の「ホバー」と同じ text α18 のニュートラル面。
        c = QColor(self.palette().text().color())
        c.setAlpha(18)
        return c

    def _mat_color(self) -> QColor:
        # 読み込み済みセルの台紙。α8 は placeholder（α18）より薄く、
        # 「読めている / まだ読めていない」の 2 段を保ったまま額装できる。
        c = QColor(self.palette().text().color())
        c.setAlpha(8)
        return c

    # ------------------------------------------------- double-click 封じ込め
    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """帯上のダブルクリックは親（``PreviewColumn``）へ渡さない.

        ``FilmstripView.mousePressEvent`` はセルに当たったときだけ accept し、
        外れると ignore して親へ伝播する。``QWidget`` 既定の
        ``mouseDoubleClickEvent`` はその press をそのまま呼ぶだけなので、
        セル間ギャップや最終セルより右の空き（数枚の投稿では帯の大半）を
        ダブルクリックすると ``PreviewColumn.double_clicked`` が飛んで
        プレビュー最大化が解除されていた。帯の役割は「サムネで表示を切り替える」
        ことなので、狙いを外したダブルクリックはここで飲み込む
        （セル上なら 2 回目のクリックとして ``clicked`` を出す = 従来どおり）。
        """
        if event.button() == Qt.MouseButton.LeftButton:
            self.mousePressEvent(event)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class StageHeader(FocusBandMixin, QWidget):
    """プレビュー列上端の常設ヘッダー（PanelHeader 規格 28px）.

    分割時: ``[‹] [›]  タイトル ・ n/m  [⤢ 最大化 (E)]``。
    最大化時: 左端に ``[◧ 分割に戻す (G)]``、送りボタンがラベル付きに戻り、
    右端の最大化ボタンが ``[⛶ 全画面 (F11)]`` に入れ替わる
    （:meth:`set_maximized`）。囲みの髪の毛線は qss の ``#panelHeader``
    （透明背景 + 1px 下境界）をそのまま使い、ボタンはツールバー規約どおり
    フラット QToolButton。色・フォントサイズはトークン経由。

    送りボタンは「投稿」ではなく**グリッドの前後の項目**を歩む中立の導線
    で、歩ける先が無いとき（無選択・端・空グリッド）は
    ホストが :meth:`set_step_enabled` で無効化する。

    フォーカス帯（``FocusBandMixin``）は PanelHeader と同じ文法 — プレビュー列に
    フォーカスがあるとき地色が一段変わり、左マージン内に ▸、題名が帯のインク色。
    左マージンは ▸ の席（``FOCUS_MARK_X + FOCUS_MARK_WIDTH``）を常時空けて
    おく（フォーカスで幅を変えない = 中身が動かない）。
    """

    back_requested = Signal()
    fullscreen_requested = Signal()
    maximize_requested = Signal()
    prev_post_requested = Signal()
    next_post_requested = Signal()
    #: post.md 本文表示中の「‹ 画像に戻る」。ホストがこの項目の
    #: 先頭の画像・動画へ選択を戻す。
    back_to_media_requested = Signal()
    #: 印ストリップの要求 (path, kind, value) — ホストが単一 funnel へ渡す。
    curation_requested = Signal(object, str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panelHeader")
        # QWidget サブクラスは WA_StyledBackground が無いと qss の #panelHeader の
        # 下境界線が描かれない（common/ui/widgets.py::PanelHeader と同じ手当て）。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._maximized = False
        self._store_available = False
        self.setFixedHeight(PANEL_HEADER_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(6)

        # 出口ボタン（最大化時のみ表示）。テキスト付きだがツールバーの検索
        # モードチップと同じフラット文法（QToolButton + TextBesideIcon）—
        # 28px ヘッダー内で箱型 QPushButton は浮くため。ショートカットは
        # ラベルに併記。
        self._back_btn = QToolButton(self)
        set_icon(self._back_btn, "columns")
        self._back_btn.setText(t("viewer.stage_view.back_to_split"))
        self._back_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._back_btn.setToolTip(t("viewer.stage_view.back_to_split_tooltip"))
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._back_btn.clicked.connect(self.back_requested.emit)
        layout.addWidget(self._back_btn)

        # 項目送り（常設）: グリッド選択を前後の項目へ歩む。分割時の
        # 「プレビューだけ見て次へ」の主導線（Ctrl+←/→ は最大化中の
        # ショートカット — ツールチップに併記）。
        # 文言は「前の投稿 / 次の投稿」から中立の
        # 「前へ / 次へ」へ — 実体はグリッド選択の ±1 送りで、post.md の無い
        # フォルダにも裸のファイルにも着地する（「post.md が無い状態が基本」）。
        self._prev_btn = QToolButton(self)
        set_icon(self._prev_btn, "chevron-left")
        # ラベルはライトボックスのカプセルと同じ「前へ / 次へ」を再利用
        # （表記揺れ防止 — test_i18n の重複値ガード）。
        self._prev_btn.setText(t("viewer.image_view.ctrl_prev"))
        self._prev_btn.setToolTip(t("viewer.stage_view.prev_item_tooltip"))
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._prev_btn.clicked.connect(self.prev_post_requested.emit)
        layout.addWidget(self._prev_btn)

        self._next_btn = QToolButton(self)
        set_icon(self._next_btn, "chevron-right")
        self._next_btn.setText(t("viewer.image_view.ctrl_next"))
        # アイコン（›）をテキストの右側に出す — QToolButton は icon-left 固定
        # なので、レイアウト方向の反転で「次へ ›」の並びにする。
        self._next_btn.setLayoutDirection(Qt.RightToLeft)
        self._next_btn.setToolTip(t("viewer.stage_view.next_item_tooltip"))
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._next_btn.clicked.connect(self.next_post_requested.emit)
        layout.addWidget(self._next_btn)

        # 1 行テキストの幅収めは共有部品 1 実装（``common/ui`` の
        # :class:`ElidedLabel`）に寄せる — 席ごとに自前で持つと、同じ直しが
        # 1 コピーにしか当たらない。
        self._title_label = ElidedLabel(parent=self, elide=Qt.ElideRight)
        # フォーカス帯中の色は qss の ``#panelHeader[focusBand] #panelHeaderTitle``
        # （PanelHeader と同じ規則。題名は inline 色を持たないので外す物が無い）。
        self._title_label.setObjectName("panelHeaderTitle")
        layout.addWidget(self._title_label, 1)

        # 印ストリップ（compact）— ★ / あとで見る / タグをその場で付けられる
        # 面。最大化中だけ出す（分割時のヘッダーは
        # 幅 ≈400px で入らず、隣の情報パネルが full 版を常設で持つ）。店が無ければ
        # 隠れる。高さ増は 0（28px ヘッダー内）。
        self._strip = CurationStrip(self, mode=MODE_COMPACT)
        self._strip.curation_requested.connect(self.curation_requested)
        layout.addWidget(self._strip)

        self._pos_label = QLabel(self)
        self._pos_label.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
        # ‹ › は**グリッドの項目**を歩くのに対し、
        # この n/m は**開いている項目の中の画像**の位置 — 隣り合っているので
        # 「次へ」を押せば n が 1 進むと読めてしまう。どちらの軸かを両側の
        # ツールチップで名指しする（「投稿」の語は使わない —
        # post.md 不在が基本なので、軸は「グリッドの項目」と「中の画像」）。
        self._pos_label.setToolTip(t("viewer.stage_view.position_tooltip"))
        layout.addWidget(self._pos_label)

        # post.md 本文を表示中の戻り導線。
        # post.md は歩く母集合（tile_paths）の外なので n/m もトラックの
        # ハイライトも消え、最大化中の現在地が完全に無所属になる。
        # 位置カウンタの席に「投稿本文」を出したうえで、隣に「画像へ戻る」を
        # 常設せず**本文表示中だけ**出す。図像はカプセルと同じ「ファイル移動」
        # 語彙の arrow-left（項目送りの chevron とは別軸）。
        self._back_media_btn = QToolButton(self)
        set_icon(self._back_media_btn, "arrow-left")
        self._back_media_btn.setText(t("viewer.stage_view.back_to_media"))
        self._back_media_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._back_media_btn.setToolTip(t("viewer.stage_view.back_to_media_tooltip"))
        self._back_media_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_media_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._back_media_btn.clicked.connect(self.back_to_media_requested.emit)
        self._back_media_btn.hide()
        layout.addWidget(self._back_media_btn)

        # 最大化への入口（分割時のみ・右端）。出口 [◧ 分割に戻す] と同じ
        # 文法・同じ席に置くことで入口/出口が対称になり、(E) の併記で
        # キーも学べる。
        self._maximize_btn = QToolButton(self)
        set_icon(self._maximize_btn, "maximize")
        self._maximize_btn.setText(t("viewer.stage_view.maximize"))
        self._maximize_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._maximize_btn.setToolTip(t("viewer.stage_view.maximize_tooltip"))
        self._maximize_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._maximize_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._maximize_btn.clicked.connect(self.maximize_requested.emit)
        layout.addWidget(self._maximize_btn)

        self._fullscreen_btn = QToolButton(self)
        # external-link は「既定アプリで開く（アプリ外）」の図像で、アプリ内に
        # 留まる全画面と正反対の意味になるので使わない。expand 系（4 方向に開く
        # 矢印）を使い、ラベルもキー併記の「全画面 (F11)」（既存の ``viewer.image_view.ctrl_fullscreen`` を再利用）
        # にして「どこへ行くのか」をボタン自体が説明するようにする。
        # 「閲覧モード」の語自体はメニュー・ヘルプ側でそのまま維持する。
        set_icon(self._fullscreen_btn, "expand")
        # 文言は既存の「全画面 (F11)」を再利用（カプセルのツールチップと同一 —
        # 表記揺れ防止・test_i18n の重複値ガード）。
        self._fullscreen_btn.setText(t("viewer.image_view.ctrl_fullscreen"))
        self._fullscreen_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._fullscreen_btn.setToolTip(
            t("viewer.stage_view.fullscreen_btn_tooltip")
        )
        self._fullscreen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fullscreen_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        layout.addWidget(self._fullscreen_btn)

        # 既定は分割表示（最大化専用ボタンは畳む）。
        self.set_maximized(False)

    # ------------------------------------------------------------------ API

    def _apply_focus_band_title(self, on: bool) -> None:
        # 題名の色は app シートの規則が祖先の動的プロパティで切り替える
        # （inline 色は無い）ので、ここでは何も差し替えない。
        del on

    def set_maximized(self, maximized: bool) -> None:
        """モードに応じてヘッダーの両端を組み替える.

        * 左端 — 最大化中のみ ``[◧ 分割に戻す (G)]``。
        * 右端 — 分割時は ``[⤢ 最大化 (E)]``、最大化時は同じ席が
          ``[⛶ 全画面 (F11)]`` に入れ替わる。
        * 送りボタン — 分割時はアイコンのみに畳んで幅をタイトルへ返す。
          最大化時はラベル付き（幅に余裕がある）。
        """
        self._back_btn.setVisible(maximized)
        self._fullscreen_btn.setVisible(maximized)
        self._maximize_btn.setVisible(not maximized)
        style = (
            Qt.ToolButtonTextBesideIcon if maximized else Qt.ToolButtonIconOnly
        )
        self._prev_btn.setToolButtonStyle(style)
        self._next_btn.setToolButtonStyle(style)
        self._trim_step_button(self._prev_btn, maximized)
        self._trim_step_button(self._next_btn, maximized)
        self._maximized = bool(maximized)
        self._sync_strip()

    @staticmethod
    def _trim_step_button(btn: QToolButton, maximized: bool) -> None:
        """ラベル付き送りボタンの余幅を落とす.

        ``QToolButton`` の ``TextBesideIcon`` は ``sizeHint`` にラベル幅の
        両脇へ空白 2 つ分を足す。``_next_btn`` は「次へ ›」の並びを作るため
        ``RightToLeft`` で、その余りがラベルとアイコンの**間**に落ちて 2 つが
        離れて見える。Qt が足している分だけを引いて内容幅へ詰める — 定数では
        なくその場のフォントメトリクスから測るので、フォント設定が変わっても
        追従する（``set_maximized`` は最大化トグルのたびに呼ばれる）。
        アイコンのみの分割時は Qt が余白を足さないので制約ごと外す。

        テキストを差し替えるボタン（``_fullscreen_btn`` /
        ``_back_media_btn``）には当てない — 固定幅のまま長いラベルへ替わると
        切れる。
        """
        if not maximized:
            btn.setMinimumWidth(0)
            btn.setMaximumWidth(_UNCONSTRAINED_WIDTH)
            return
        slack = btn.fontMetrics().horizontalAdvance(" ") * 2
        btn.setFixedWidth(max(0, btn.sizeHint().width() - slack))

    def set_fullscreen_folder_mode(self, folder_mode: bool) -> None:
        """全画面ボタンが「フォルダ流し見」へフォールバックする状態かを示す.

        非メディア（``.part`` / ZIP / PDF / テキスト）を表示中の F11 は、
        いま見ているものではなくフォルダの先頭メディアを開く（G05 の意図した
        機能）。挙動は変えず、ボタン自身にそれを予告させる。
        """
        if folder_mode:
            self._fullscreen_btn.setText(
                t("viewer.stage_view.fullscreen_folder")
            )
            self._fullscreen_btn.setToolTip(
                t("viewer.stage_view.fullscreen_folder_tooltip")
            )
        else:
            self._fullscreen_btn.setText(t("viewer.image_view.ctrl_fullscreen"))
            self._fullscreen_btn.setToolTip(
                t("viewer.stage_view.fullscreen_btn_tooltip")
            )
        # ラベルが変われば席の下限も変わる（印ストリップの可否を測り直す）。
        self._sync_strip()

    def fullscreen_text(self) -> str:
        """全画面ボタンの現在ラベル（テスト用）."""
        return self._fullscreen_btn.text()

    def set_step_enabled(self, can_prev: bool, can_next: bool) -> None:
        """送りボタンの活性を歩ける先の有無に同期する.

        無選択・端・空グリッドでは押せる見た目のまま無反応だったため、ホスト
        （``_update_stage_header``）が計算した可否をそのまま反映する。
        """
        self._prev_btn.setEnabled(bool(can_prev))
        self._next_btn.setEnabled(bool(can_next))

    def set_store_available(self, available: bool) -> None:
        """user_meta 店の有無 — 無ければ印ストリップは出ない（右クリックと同じ劣化）."""
        self._store_available = bool(available)
        self._sync_strip()

    def set_curation(
        self,
        path,
        star: int = 0,
        later: bool = False,
        tags: tuple[str, ...] = (),
    ) -> None:
        """現在表示中の項目の印を compact ストリップへ（対象はツールチップで名乗る）.

        分割ビューでは投稿フォルダとプレビュー画像で対象が変わるので、ホスト
        （``_update_stage_header``）が「プレビューが実際に映しているもの」を渡す。
        """
        self._strip.set_target(path, star, later, tags)
        self._sync_strip()

    def curation_strip(self) -> CurationStrip:
        """ヘッダー内の印ストリップ（テスト・レイアウト参照用）."""
        return self._strip

    def _label_fold_steps(self) -> tuple[QToolButton, ...]:
        """幅が足りないときラベルを落とすボタン（先頭から落とす）.

        入口［全画面］が先で、出口［分割に戻す］は最後 — 最大化から戻る道が
        読めなくなるのが一番痛い。どちらもアイコンとツールチップは残る。
        """
        return (self._fullscreen_btn, self._back_btn)

    def _set_chrome_folded(self, folded: int) -> None:
        """ラベル付きボタンを先頭 *folded* 個だけアイコンのみへ落とす."""
        for i, btn in enumerate(self._label_fold_steps()):
            btn.setToolButtonStyle(
                Qt.ToolButtonIconOnly if i < folded
                else Qt.ToolButtonTextBesideIcon
            )

    def _chrome_min_width(self) -> int:
        """印ストリップ以外のヘッダー要素がいま要る幅（タイトルは省略で 0 まで縮む）."""
        layout = self.layout()
        margins = layout.contentsMargins()
        total = margins.left() + margins.right()
        cells = 0
        for btn in (
            self._back_btn, self._prev_btn, self._next_btn, self._pos_label,
            self._back_media_btn, self._maximize_btn, self._fullscreen_btn,
        ):
            if btn.isHidden():
                continue
            total += btn.sizeHint().width()
            cells += 1
        # 間隔はタイトル・印ストリップのぶんも数える（+2）。
        return total + layout.spacing() * (cells + 1)

    def _sync_strip(self, width: int | None = None) -> None:
        """幅が足りないヘッダーは、まずラベルを畳み、それでも足りなければ印を隠す.

        しきい値は定数ではなくその場の ``sizeHint`` から測る: ヘッダーの他の
        要素が要る幅を引いた残りが、★行を保てる最小幅
        （``CurationStrip.minimumSizeHint``）に届かなければ面ごと隠す。
        入る幅での畳み方（ラベル → チップ列 → タグの入口）は部品が自分の
        幅を見て決めるので、席は「最大化中 ∧ 店あり ∧ 席が足りる」だけを渡す。

        席が足りるかは**ヘッダー側が縮んだ後**で測る: 両端のラベル付きボタンは
        省略しないフル幅を席として予約するので、それを丸ごと下限に数えると
        既定ウィンドウ（プレビュー列 ≈710px）でも印ストリップが座れなくなる。
        落とす順は :meth:`_label_fold_steps`、測るのは畳んだ後の実 ``sizeHint``
        （``_trim_step_button`` と同じ「その場から測る」作法）。

        畳む段は**足りるまで畳み、足りなければ最後まで畳む**（戻さない）。
        ラベルを戻すとヘッダー自身のレイアウト最小幅が上がり、席より狭い帯では
        「戻す → 最小幅が上がる → 幅が増える → 畳む → 最小幅が下がる」の
        振動になる（段が幅に対して単調なら、この帰還は必ず止まる）。
        """
        if width is None:
            width = self.width()
        want = self._maximized and self._store_available
        floor = self._strip.minimumSizeHint().width()
        folded = 0
        steps = len(self._label_fold_steps()) if want else 0
        while True:
            self._set_chrome_folded(folded)
            if folded >= steps or width - self._chrome_min_width() >= floor:
                break
            folded += 1
        self._strip.set_store_available(
            want and width - self._chrome_min_width() >= floor
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._sync_strip(event.size().width())

    def set_context(
        self,
        title: str,
        position: str,
        position_tooltip: str = "",
        *,
        back_to_media: bool = False,
    ) -> None:
        """現在地（投稿タイトル / フォルダ名）と位置カウンタを更新する.

        *position* は通常 ``n/m``（画像・動画の位置）だが、表示中が画像・動画
        でないときはホストが種別ラベルや「投稿本文」を渡す。
        軸の説明が変わるので *position_tooltip* も併せて受け取る（空なら
        既定の ``position_tooltip``）。*back_to_media* は post.md 本文表示中の
        戻り導線の表示可否。
        """
        self._title_label.setText(title)
        self._pos_label.setText(position)
        self._pos_label.setVisible(bool(position))
        self._pos_label.setToolTip(
            position_tooltip or t("viewer.stage_view.position_tooltip")
        )
        self._back_media_btn.setVisible(bool(back_to_media))
        # 位置ラベル・戻り導線の出入りも席の下限を動かす（測り直す）。
        self._sync_strip()

    def title_text(self) -> str:
        """表示中のフルタイトル（エリプシス前 — テスト/ツールチップ用）."""
        return self._title_label.text()


class PreviewColumn(QWidget):
    """プレビュー列のコンテナ（ヘッダー + ContentView + 画像トラック）.

    子ビューが消費しなかった左ダブルクリック（余白・非画像ページ等）を
    「分割 ⇄ 最大化のトグル」として emit する。画像上のダブルクリックは
    ImageView 側の契約（分割中=最大化 / 最大化中=ズーム切替）が担う。
    """

    double_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # QSplitter clamps children to ``qSmartMinSize`` — for a widget with a
        # layout that is the layout's minimum (the header's labelled buttons +
        # ContentView's stacked sub-views push it past ~400px), which silently
        # overrides the requested 55:45 split.  The column must be freely
        # shrinkable (down to 0 — the user may collapse it entirely), so the
        # horizontal policy is ``Ignored`` (qSmartMinSize then reports width 0;
        # the splitter's setSizes stays the single width authority).  Mirrors
        # the ``setMinimumWidth(0)`` guards on the outer splitter panes
        # (main_window R2 note).  A Python ``minimumSizeHint`` override would
        # do the same but adds a virtual dispatched during C++ teardown.
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Ignored)
        self.setSizePolicy(policy)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


__all__ = ["PreviewColumn", "StageFilmstrip", "StageHeader"]
