"""AI 検索ポップオーバーの**組み立て**（席と配線だけ）.

``AdvancedSearchController`` から「ウィジェットを作って並べて繋ぐ」部分を
切り出したもの。判断（可否・文言の分岐・クエリ）はコントローラ側に残し、
ここは席の生成と ``connect`` だけを持つ。ウィジェットの属性名はコントローラ
に載る（永続化・スナップショット・テストが参照する面なので不変）。

小さな専用ウィジェット 3 つ（精度スライダ / 補完候補の件数描画 / 類似シードの
ドロップ先）も、ポップオーバー以外から使われないのでここに住む。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...common.i18n import t
from ...common.ui import (
    FONT_CAPTION_PT,
    ElidedLabel,
    align_form_labels,
    hint_style,
    indent_to_form_column,
    popover_position,
    set_icon,
)
from ...common.ui.tokens import RADIUS_SM
from ..folder_scan import IMAGE_SUFFIXES
from ..search_dimensions import cheatsheet_html
from ..search_dimensions import combo_items as _dim_combo_items
from ..tag_chips import TagChipsInput

#: セグメントの鍵 → (ラベル, 役割を語るツールチップ)。2 つの「類似」は
#: 取り違えやすいので、各ボタンが何で並べるのかを明示する。並びは表示順。
AI_MODES = (
    ("and", "viewer.advanced_search.mode_and",
     "viewer.advanced_search.mode_and_tooltip"),
    ("rank", "viewer.advanced_search.mode_rank",
     "viewer.advanced_search.mode_rank_tooltip"),
    ("similar", "viewer.advanced_search.mode_similar",
     "viewer.advanced_search.mode_similar_tooltip"),
)


class TagCountDelegate(QStyledItemDelegate):
    """補完候補の右端に「そのタグの存在件数」を描くデリゲート。

    モデルが持つのは**タグ名だけ**（照合と挿入文字列が常にタグそのものに
    なる）で、件数は ``Qt.UserRole`` に out-of-band で載せてここで描く。
    件数をテキストに混ぜないことが、候補選択時に件数が入力欄へ漏れない理由。
    """

    #: タグ本文が件数の下へ潜り込まないよう確保する右マージン。
    _COUNT_MARGIN = 8

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        cnt = index.data(Qt.UserRole)
        if cnt is None:
            super().paint(painter, option, index)
            return
        # 件数ぶんの溝を先に確保し、**狭めた矩形**へタグ本文を描く。全幅で
        # 描いてから件数を上書きする形だと、長いタグが件数に衝突して読めない。
        count_text = f"{int(cnt):,}"
        fm = QFontMetrics(option.font)
        gutter = fm.horizontalAdvance(count_text) + 2 * self._COUNT_MARGIN
        from PySide6.QtWidgets import QStyleOptionViewItem

        text_opt = QStyleOptionViewItem(option)
        text_opt.rect = option.rect.adjusted(0, 0, -gutter, 0)
        super().paint(painter, text_opt, index)

        selected = bool(option.state & QStyle.State_Selected)
        painter.save()
        if selected:
            painter.setPen(option.palette.color(QPalette.HighlightedText))
        else:
            painter.setPen(option.palette.color(QPalette.Disabled, QPalette.Text))
        rect = option.rect.adjusted(0, 0, -self._COUNT_MARGIN, 0)
        painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, count_text)
        painter.restore()


class PrecisionSlider(QWidget):
    """値表示付きの float スライダ（2 桁刻み）。

    精度の席がスピンボックスからスライダへ移った後も、周辺の配線が読む
    float の API（``minimum`` / ``maximum`` / ``setRange`` / ``value`` /
    ``setValue`` / ``valueChanged``）はそのまま提供する。``blockSignals`` は
    この widget の ``valueChanged`` 再送だけを止める（内側の ``QSlider`` は
    読み出しラベルを更新し続ける）。
    """

    valueChanged = Signal(float)

    #: 整数 ``QSlider`` スケール上の 2 桁精度。
    _SCALE = 100

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, self._SCALE)
        self._slider.setSingleStep(5)   # 0.05
        self._slider.setPageStep(10)
        lay.addWidget(self._slider, 1)
        self._label = QLabel(self._fmt(0.0))
        fm = self._label.fontMetrics()
        self._label.setFixedWidth(fm.horizontalAdvance(self._fmt(0.0)) + 4)
        self._label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self._label)
        self._slider.valueChanged.connect(self._on_slider_changed)

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{value:.2f}"

    def _on_slider_changed(self, raw: int) -> None:
        value = raw / self._SCALE
        self._label.setText(self._fmt(value))
        # この widget の signals が止まっている間は抑止される（上の読み出しは
        # 止まっていない子スライダが駆動するので更新され続ける）。
        self.valueChanged.emit(value)

    def minimum(self) -> float:
        return self._slider.minimum() / self._SCALE

    def maximum(self) -> float:
        return self._slider.maximum() / self._SCALE

    def setRange(self, lo: float, hi: float) -> None:
        self._slider.setRange(round(lo * self._SCALE), round(hi * self._SCALE))
        self._label.setText(self._fmt(self.value()))

    def value(self) -> float:
        return self._slider.value() / self._SCALE

    def setValue(self, value: float) -> None:
        self._slider.setValue(round(float(value) * self._SCALE))
        self._label.setText(self._fmt(self.value()))


class SeedDropArea(QWidget):
    """類似検索の参照画像を受け取る行（画像ファイルのドロップ先）。

    タイルを先に選ばなくても参照画像を指定できるようにするための席で、
    ファイル選択ボタンがもう一方の経路。親への通知は**シグナル**で行う —
    束縛メソッドを属性として持たせると
    PostGrid → ai_popover → tag_seed_row → 束縛メソッド → PostGrid の
    参照循環が閉じる（PySide6 のシグナル接続は受信側 QObject を延命しない）。
    """

    #: ドロップされた画像ファイルのパス（検証済み: 画像拡張子 + 実在）。
    image_dropped = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # dragEnter で解決した「この drag は受け取れるか」— dragMove はドラッグ
        # 中のマウス移動ごとに発火するので、判定を毎回やり直すと NAS 上の
        # ファイルでは GUI スレッドの stat が移動のたびに走る。判定材料
        # （mimeData）はドラッグ中に変わらないので使い回す。
        self._drag_accepted = False
        self.setAcceptDrops(True)

    @staticmethod
    def _dropped_image(event) -> Path | None:
        md = event.mimeData()
        if not md.hasUrls():
            return None
        for url in md.urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file():
                return p
        return None

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        self._drag_accepted = self._dropped_image(event) is not None
        if self._drag_accepted:
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        # dragEnter の判定を使い回す（移動ごとの stat を避ける）。
        if self._drag_accepted:
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[override]
        self._drag_accepted = False
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        self._drag_accepted = False
        p = self._dropped_image(event)
        if p is not None:
            event.acceptProposedAction()
            self.image_dropped.emit(p)


def build_mode_segment(ctl, body: QVBoxLayout) -> None:
    """排他セグメント［AIタグ検索 / AIタグ類似検索 / 類似画像検索］を組む。"""
    row = QHBoxLayout()
    row.setSpacing(0)
    ctl.ai_mode_group = QButtonGroup(ctl)
    ctl.ai_mode_group.setExclusive(True)
    ctl._ai_mode_buttons = {}
    seg_style = (
        "QToolButton { border: 1px solid palette(mid);"
        f" border-radius: {RADIUS_SM}px; padding: 3px 12px;"
        " margin-right: 4px; }"
        " QToolButton:checked { background: palette(highlight);"
        " color: palette(highlighted-text); border-color: palette(highlight); }"
    )
    for i, (key, label_key, tooltip_key) in enumerate(AI_MODES):
        btn = QToolButton()
        btn.setText(t(label_key))
        btn.setToolTip(t(tooltip_key))
        btn.setCheckable(True)
        btn.setStyleSheet(seg_style)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setChecked(key == "and")
        btn.clicked.connect(lambda _=False, k=key: ctl._on_ai_mode_selected(k))
        ctl.ai_mode_group.addButton(btn, i)
        ctl._ai_mode_buttons[key] = btn
        row.addWidget(btn)
    row.addStretch(1)
    body.addLayout(row)
    ctl._sync_ai_segment_enabled()


def build_popover(ctl) -> None:
    """AI 検索ポップオーバー本体を組み、コントローラへ席を載せる。

    AI 機能パック（有償プラグイン）無効の配布でも**構築はする** — 開く経路
    :meth:`AdvancedSearchController.open_ai_popover` が ``ai_pack.available()``
    でゲートされ決して表示されないので、内部配線・状態復元・スナップショット
    往復だけが inert なまま生き続ける（「見えないが壊れない」）。
    """
    host_widget = ctl.host.widget()
    has_index = ctl.host.tag_index is not None
    pop = QFrame(host_widget, Qt.Popup)
    pop.setObjectName("toolbarPopover")
    pop.setMinimumWidth(430)
    ctl.ai_popover = pop
    body = QVBoxLayout(pop)
    body.setContentsMargins(12, 10, 12, 10)
    body.setSpacing(6)
    # 見出し行: タイトル（tags.db が無いときはその理由を名乗る）+ 末尾の
    # チートシートボタン（図像は検索欄の構文ヘルプと同じ help-circle）。
    header = QHBoxLayout()
    ctl.ai_popover_title = QLabel(ctl._panel_title_text(has_index))
    title_font = ctl.ai_popover_title.font()
    title_font.setBold(True)
    ctl.ai_popover_title.setFont(title_font)
    header.addWidget(ctl.ai_popover_title)
    header.addStretch(1)
    ctl.tag_help_btn = QToolButton()
    set_icon(ctl.tag_help_btn, "help-circle")
    ctl.tag_help_btn.setToolTip(t("viewer.advanced_search.help_tooltip"))
    ctl.tag_help_btn.setStyleSheet("QToolButton { border: none; }")
    ctl.tag_help_btn.clicked.connect(ctl._show_search_cheatsheet)
    header.addWidget(ctl.tag_help_btn)
    body.addLayout(header)

    # tags.db が無いときだけ出るインラインバナー: 黙ってコントロールを灰色に
    # するのではなく、AIタグ / 意味検索をどうすれば使えるのかを語る。
    missing_row = QVBoxLayout()
    missing_row.setContentsMargins(0, 0, 0, 0)
    missing_row.setSpacing(2)
    ctl.tag_missing_banner = QLabel(ctl._missing_db_banner_text())
    ctl.tag_missing_banner.setWordWrap(True)
    ctl.tag_missing_banner.setStyleSheet(hint_style())
    missing_row.addWidget(ctl.tag_missing_banner)
    # 「再読み込み」: タガーを回した後、ビューアを再起動せず新しい tags.db を
    # 拾える。ホストの再読み込み要求を出すだけで、索引を開き直すのはホスト。
    reload_btn_row = QHBoxLayout()
    reload_btn_row.setContentsMargins(0, 0, 0, 0)
    ctl.tag_reload_btn = QPushButton(t("viewer.advanced_search.reload_db_btn"))
    ctl.tag_reload_btn.setToolTip(t("viewer.advanced_search.reload_db_tooltip"))
    ctl.tag_reload_btn.clicked.connect(ctl._on_reload_tag_db_clicked)
    reload_btn_row.addWidget(ctl.tag_reload_btn)
    reload_btn_row.addStretch(1)
    missing_row.addLayout(reload_btn_row)
    ctl.tag_missing_widget = QWidget()
    ctl.tag_missing_widget.setLayout(missing_row)
    ctl.tag_missing_widget.setVisible(not has_index)
    # バナーの文言と［再読み込み］の可否は 1 つの判定（エンジン不在なら
    # 押しても回復しない）— 構築直後にもそれを通す。
    ctl._sync_missing_db_banner()
    body.addWidget(ctl.tag_missing_widget)

    # 検索モードのセグメント。暗黙だった ``_query_mode`` の優先順位
    # （semantic > tags）を目に見える選択にし、それが唯一の保持状態を書く。
    build_mode_segment(ctl, body)

    # 種別走査の注記: 種別コンボが「すべて/画像」以外の間、実クエリは拡張子
    # 走査でタグ・シードは一切使われない。走査中はセグメントを中立化し、この
    # muted な 1 行が理由を語る。
    ctl.tag_media_note = QLabel(t("viewer.advanced_search.media_note"))
    ctl.tag_media_note.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
    ctl.tag_media_note.setWordWrap(True)
    ctl.tag_media_note.setVisible(False)
    body.addWidget(ctl.tag_media_note)

    # tags.db はあるがベクトルが無いときの理由を常設で出す: 灰色のボタンと
    # ツールチップだけでは「なぜ押せないのか」が答えられていなかった。
    ctl.tag_no_vectors_hint = QLabel(
        t("viewer.advanced_search.no_vectors_short")
    )
    ctl.tag_no_vectors_hint.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
    ctl.tag_no_vectors_hint.setWordWrap(True)
    ctl.tag_no_vectors_hint.setVisible(False)
    body.addWidget(ctl.tag_no_vectors_hint)

    # Row A: AIタグ入力（tags.db があれば常に使える — 起動はチップが担う）+
    # タグ一覧ボタン。
    row_a = QHBoxLayout()
    ai_tag_lbl = QLabel(t("viewer.advanced_search.ai_tag_label"))
    row_a.addWidget(ai_tag_lbl)
    # チップ式の複数タグ入力。公開 API は文字列ベースで ``QLineEdit`` の
    # 上位互換なので、永続化 / スナップショット / スキャン投入は無変更。
    ctl.tag_input = TagChipsInput()
    ctl.tag_input.setPlaceholderText(ctl._dynamic_tag_placeholder())
    ctl.tag_input.setToolTip(t("viewer.advanced_search.ai_tag_tooltip"))
    ctl.tag_input.textChanged.connect(ctl._on_tag_input_changed)
    if has_index:
        ctl._install_tag_completer()
    row_a.addWidget(ctl.tag_input, 1)
    # 「タグ一覧…」はモードレスのタグブラウザ。tags.db が無ければ無効。
    ctl.tag_browse_btn = QPushButton(t("viewer.advanced_search.tag_browse_btn"))
    ctl.tag_browse_btn.setToolTip(t("viewer.advanced_search.tag_browse_tooltip"))
    ctl.tag_browse_btn.clicked.connect(ctl._open_tag_browser)
    ctl._tag_browser = None  # 遅延生成のモードレスダイアログ
    row_a.addWidget(ctl.tag_browse_btn)
    body.addLayout(row_a)

    # 入力欄の**下**に置く muted な例示 / 構文の 1 行。具体的なタグ例は以前
    # プレースホルダに居たが、ポップオーバーの欄幅で切れて例として読めなく
    # なっていた（「例: no_h…」）。プレースホルダは短く、折返しの効くこの行が
    # 例（または構文の要点）を持つ。
    ctl.tag_examples_hint = QLabel("")
    ctl.tag_examples_hint.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
    ctl.tag_examples_hint.setWordWrap(True)
    body.addWidget(ctl.tag_examples_hint)
    ctl._refresh_tag_examples_hint()

    # Row B: AIタグの精度（値表示付きスライダ）。ランク / 類似モードでは
    # 精度の意味が無いので、セグメントがこの行ごと隠せるよう属性で持つ。
    row_b = QHBoxLayout()
    ctl.tag_precision_label = QLabel(t("viewer.advanced_search.precision_label"))
    row_b.addWidget(ctl.tag_precision_label)
    ctl.tag_threshold_slider = PrecisionSlider()
    floor = 0.0
    if has_index:
        try:
            floor = float(ctl.host.tag_index.recorded_floor())
        except Exception:  # pragma: no cover (best-effort)
            floor = 0.0
    ctl.tag_threshold_slider.setRange(max(0.0, floor), 1.0)
    ctl.tag_threshold_slider.setValue(max(ctl.query.threshold, floor))
    ctl._store_threshold(ctl.tag_threshold_slider.value())
    ctl.tag_threshold_slider.setToolTip(
        t("viewer.advanced_search.precision_tooltip", floor=floor)
    )
    ctl.tag_threshold_slider.valueChanged.connect(ctl._on_tag_threshold_changed)
    row_b.addWidget(ctl.tag_threshold_slider, 1)
    body.addLayout(row_b)

    # Row B2: 種別。年齢区分はここに**居ない** — 条件次元レジストリ第 2 段で
    # フィルターポップオーバーの生成行へ席を移した（属性名・配線・状態往復は
    # 不変で、ここは席が減っただけ）。
    row_b2 = QHBoxLayout()
    # 「種別:」はフィルターポップオーバーにも別に永続化された同名の軸があり、
    # 両者は AND する。統合すると検索の挙動が変わるのでこの 1 軸だけ残し、
    # 「どちらの種別か」は下の共有条件チップで読めるようにする。
    type_lbl = QLabel(t("viewer.advanced_search.media_type_label"))
    row_b2.addWidget(type_lbl)
    ctl.tag_media_combo = QComboBox()
    # 選択肢は条件次元レジストリの台帳から（フィルターバーの種別と共有）。
    for key, label_key in _dim_combo_items("ai_media"):
        ctl.tag_media_combo.addItem(t(label_key), key)
    ctl.tag_media_combo.setToolTip(t("viewer.advanced_search.media_tooltip"))
    ctl.tag_media_combo.currentIndexChanged.connect(ctl._on_tag_media_changed)
    row_b2.addWidget(ctl.tag_media_combo)
    row_b2.addStretch(1)
    body.addLayout(row_b2)

    # Row C: 表示単位コンボ。投稿日はフィルターバー側（平常ブラウズにも効く
    # ビューの絞り込みなので 1 箇所に置く）。器は**縦** — 注記がラベル + コンボ
    # と同じ行に横並びだった頃は、ポップオーバーの幅で先頭 13 字ほどで切れて
    # 理由が読めなかった。注記はこの行の**子のまま**にする（モード切替が行ごと
    # 隠し、``_update_display_unit_combo`` が注記だけを出し入れするため）。
    ctl.tag_display_unit_row = QWidget()
    col_c = QVBoxLayout(ctl.tag_display_unit_row)
    col_c.setContentsMargins(0, 0, 0, 0)
    col_c.setSpacing(2)
    row_c = QHBoxLayout()
    row_c.setContentsMargins(0, 0, 0, 0)
    col_c.addLayout(row_c)
    display_unit_lbl = QLabel(t("viewer.advanced_search.display_unit_label"))
    row_c.addWidget(display_unit_lbl)
    ctl.tag_display_unit_combo = QComboBox()
    # data の鍵は永続化される 2 bool（folder_mode / coverage）へ写る:
    #   file            → folder_mode=False（coverage は無関係）
    #   folder_strict   → folder_mode=True,  coverage=False
    #   folder_coverage → folder_mode=True,  coverage=True
    for key, label_key in _dim_combo_items("ai_unit"):
        ctl.tag_display_unit_combo.addItem(t(label_key), key)
    ctl.tag_display_unit_combo.setToolTip(
        t("viewer.advanced_search.display_unit_tooltip")
    )
    ctl.tag_display_unit_combo.currentIndexChanged.connect(
        ctl._on_tag_display_unit_changed
    )
    row_c.addWidget(ctl.tag_display_unit_combo)
    row_c.addStretch(1)
    # 常設の注記: 既定の「フォルダ（別々の画像で全タグ可）」は include タグが
    # 2 群未満だと自身の無効化ルールに引っかかり、**開くと現在の選択肢が灰色**
    # に見える。結果は folder_strict と完全に同一なので実害は無いが、その理由は
    # 無効項目のツールチップにしか書かれていなかった。
    ctl.tag_display_unit_note = QLabel(
        t("viewer.advanced_search.display_unit_note")
    )
    ctl.tag_display_unit_note.setStyleSheet(hint_style())
    ctl.tag_display_unit_note.setWordWrap(True)
    # 折返しが効かない構成（極端に狭い幅）でも理由を取り戻せるよう、同じ文を
    # ツールチップにも持たせる。
    ctl.tag_display_unit_note.setToolTip(
        t("viewer.advanced_search.display_unit_note")
    )
    col_c.addWidget(ctl.tag_display_unit_note)
    body.addWidget(ctl.tag_display_unit_row)

    # Row C2: 共有条件（読み取り専用）+ 構文での等価表現。AI ポップオーバーと
    # フィルターポップオーバーは同じ検索を 2 枚の面から編集しているのに、片方を
    # 開いている間はもう片方の条件が見えなかった。年齢区分の席がフィルター側へ
    # 移ったので、AI 側は**編集させず読ませる**。
    ctl.tag_shared_conditions = QLabel("")
    ctl.tag_shared_conditions.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
    ctl.tag_shared_conditions.setWordWrap(True)
    ctl.tag_shared_conditions.setToolTip(
        t("viewer.advanced_search.shared_conditions_tooltip")
    )
    body.addWidget(ctl.tag_shared_conditions)
    ctl.tag_shared_syntax = QLabel("")
    ctl.tag_shared_syntax.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
    ctl.tag_shared_syntax.setWordWrap(True)
    ctl.tag_shared_syntax.setTextInteractionFlags(
        Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
    )
    ctl.tag_shared_syntax.setToolTip(
        t("viewer.advanced_search.shared_syntax_tooltip")
    )
    body.addWidget(ctl.tag_shared_syntax)

    # 行 A/B/B2/C の先頭ラベル列を揃える（各行が自分の左端を決めると入力欄の
    # 開始 x がばらける）。右寄せ・共有の固定幅 — 左ペインは可変幅なので、
    # 伸びるのは入力側だけでラベル列は動かない。
    label_col_w = align_form_labels(
        ai_tag_lbl, ctl.tag_precision_label, type_lbl, display_unit_lbl,
    )
    # 例示行はラベルを持たない独立行なので、上の整列だけでは左端に取り残される。
    # ラベル列ぶん字下げして、説明対象の AIタグ入力欄と縦のラインを揃える。
    indent_to_form_column(
        ctl.tag_examples_hint, label_col_w, max(0, row_a.spacing()),
    )
    # 共有条件の 2 行と表示単位の注記も同じ縦のラインに載せる。
    for line in (
        ctl.tag_shared_conditions,
        ctl.tag_shared_syntax,
        ctl.tag_display_unit_note,
    ):
        indent_to_form_column(line, label_col_w, max(0, row_a.spacing()))
    ctl._update_shared_conditions()

    # Row D: モードごとの補助。
    row_d = QHBoxLayout()
    # 「画像を選ぶ…」は類似画像検索セグメントの中身: そのモードでだけ出て、
    # ファイル選択で**任意の**画像を参照にできる（D&D も同じ席が受ける）。
    ctl.tag_similar_btn = QPushButton(t("viewer.advanced_search.pick_image_btn"))
    ctl.tag_similar_btn.setToolTip(t("viewer.advanced_search.pick_image_tooltip"))
    ctl.tag_similar_btn.clicked.connect(ctl._on_pick_similar_image)
    row_d.addWidget(ctl.tag_similar_btn)
    # 設定 / 履歴往復と既存テストのために隠して残す互換ウィジェット。類似
    # モードから出る操作はセグメントが持つ。
    ctl.tag_similar_clear_btn = QPushButton(
        t("viewer.advanced_search.similar_clear_btn")
    )
    ctl.tag_similar_clear_btn.setVisible(False)
    ctl.tag_similar_clear_btn.setEnabled(False)
    ctl.tag_similar_clear_btn.clicked.connect(ctl._on_similar_clear_clicked)
    row_d.addWidget(ctl.tag_similar_clear_btn)
    # ランクモードの注記（チップ = ランキングの種）。表示はモード同期が持つ。
    ctl.tag_rank_note = QLabel(t("viewer.advanced_search.rank_note"))
    ctl.tag_rank_note.setStyleSheet(hint_style())
    ctl.tag_rank_note.setWordWrap(True)
    ctl.tag_rank_note.setVisible(False)
    row_d.addWidget(ctl.tag_rank_note, 1)
    row_d.addStretch(1)
    # ランク結果が画面に出ている間だけ出す ◆ バッジの凡例。
    ctl.tag_relevance_legend = QLabel(
        t("viewer.advanced_search.relevance_legend")
    )
    ctl.tag_relevance_legend.setStyleSheet(hint_style())
    ctl.tag_relevance_legend.setVisible(False)
    row_d.addWidget(ctl.tag_relevance_legend)
    body.addLayout(row_d)

    # Row E: 類似シード行 — 参照画像のドロップ先 + プレビュー。類似画像検索
    # モードの間ずっと見え、シード未設定なら D&D の CTA、設定後はサムネ +
    # 名前 + 「×」。
    ctl.tag_seed_row = SeedDropArea()
    # シグナル接続（束縛メソッドの強参照保持ではなく）— PySide6 は受信側
    # QObject を弱参照で扱うため PostGrid との参照循環が閉じない。
    ctl.tag_seed_row.image_dropped.connect(ctl._seed_from_path)
    seed_layout = QHBoxLayout(ctl.tag_seed_row)
    seed_layout.setContentsMargins(0, 0, 0, 0)
    seed_layout.setSpacing(6)
    seed_layout.addWidget(QLabel(t("viewer.advanced_search.seed_label")))
    ctl.tag_seed_thumb = QLabel()
    ctl.tag_seed_thumb.setFixedSize(QSize(40, 40))
    ctl.tag_seed_thumb.setAlignment(Qt.AlignCenter)
    seed_layout.addWidget(ctl.tag_seed_thumb)
    # 中略表示は共有部品 ElidedLabel（フルテキストのツールチップは部品側の
    # 契約。下の表示更新でフルパスへ上書きされる）。
    ctl.tag_seed_name = ElidedLabel("")
    seed_layout.addWidget(ctl.tag_seed_name, 1)
    # シード未設定のあいだ出す CTA（ドロップ先であることのヒント）。
    ctl.tag_seed_cta = QLabel(t("viewer.advanced_search.seed_cta"))
    ctl.tag_seed_cta.setStyleSheet(hint_style())
    ctl.tag_seed_cta.setWordWrap(True)
    seed_layout.addWidget(ctl.tag_seed_cta, 1)
    # × はシードがあるときだけ。
    ctl.tag_seed_clear = QToolButton()
    ctl.tag_seed_clear.setText("×")
    ctl.tag_seed_clear.setToolTip(t("viewer.advanced_search.seed_clear_tooltip"))
    ctl.tag_seed_clear.setStyleSheet("QToolButton { border: none; }")
    ctl.tag_seed_clear.clicked.connect(ctl._on_similar_clear_clicked)
    seed_layout.addWidget(ctl.tag_seed_clear)
    ctl.tag_seed_row.setVisible(False)
    body.addWidget(ctl.tag_seed_row)

    # 「詳細条件のみリセット」: AI の軸だけを白紙に戻し、絞り込み欄と検索範囲は
    # 残す。席はブロックの**末尾** — かつて Row D と Row E の間にあり、CTA
    # 「画像を選ぶ…」とその対象であるシード行の間に、条件次第で現れたり消えたり
    # するリンクが割り込んで視線の流れを割っていた。
    reset_row = QHBoxLayout()
    reset_row.addStretch(1)
    ctl.tag_reset_link = QPushButton(t("viewer.advanced_search.reset_link"))
    ctl.tag_reset_link.setFlat(True)
    ctl.tag_reset_link.setStyleSheet(
        "QPushButton { color: palette(link); border: none; }"
    )
    ctl.tag_reset_link.setCursor(Qt.PointingHandCursor)
    ctl.tag_reset_link.setToolTip(t("viewer.advanced_search.reset_link_tooltip"))
    ctl.tag_reset_link.clicked.connect(ctl._on_reset_advanced_only)
    ctl.tag_reset_link.setVisible(False)
    reset_row.addWidget(ctl.tag_reset_link)
    body.addLayout(reset_row)

    # 可否の決定点は下の同期関数だけ（それぞれ同じ判定を、Tab チェーンからの
    # 除外まで含めて正しく行う）。構築末尾で素の ``setEnabled`` を先に当てる
    # 「弱い版」を持つと、同じ機構が 2 本になるだけで挙動には何も足さない。
    ctl._update_tag_controls_enabled()
    ctl._update_display_unit_combo()
    ctl._update_similar_button_state()
    ctl._update_ai_mode_ui()
    # セグメントのゲートを全ウィジェット生成後にもう一度: 構築中の呼び出しは
    # ``tag_no_vectors_hint`` の生成前に走るので、インラインの理由行を最初に
    # 出し入れするのはこの呼び出し。
    ctl._sync_ai_segment_enabled()


def show_cheatsheet(ctl) -> None:
    """1 画面の検索チートシートを開く / 閉じる（トグル）。

    絞り込み欄の「?」ヘルプと同じ ``Qt.Popup`` の枠なしフレーム。親は AI
    ポップオーバー（グリッドではない）なので、開いても Qt のポップアップ連鎖
    から外れず元のポップオーバーが閉じない。
    """
    existing = ctl._search_cheatsheet_popup
    if existing is not None:
        if existing.isVisible():
            existing.close()
            return
        # 非可視の旧インスタンスは明示破棄する。``Qt.Popup`` はクリック
        # アウェイで hide されるだけなので、参照を差し替えるだけでは開くたび
        # に ai_popover の子として溜まっていく。
        existing.deleteLater()
        ctl._search_cheatsheet_popup = None
    popup = QFrame(ctl.ai_popover, Qt.Popup)
    popup.setFrameShape(QFrame.StyledPanel)
    layout = QVBoxLayout(popup)
    layout.setContentsMargins(10, 8, 10, 8)
    # 本文は条件次元レジストリが生成（軸の行 = 台帳、語彙対比・モード説明 =
    # i18n の静的断片）。
    label = QLabel(cheatsheet_html())
    label.setTextFormat(Qt.RichText)
    label.setWordWrap(True)
    label.setMaximumWidth(420)
    layout.addWidget(label)
    ctl._search_cheatsheet_popup = popup
    popup.adjustSize()
    popup.move(popover_position(ctl.tag_help_btn, popup.size()))
    popup.show()
