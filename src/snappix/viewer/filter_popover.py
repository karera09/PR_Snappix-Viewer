"""フィルターポップオーバーの**行を条件次元レジストリから生成する** Qt 層.

軸ごとの「ラベル + コロン + コントロール + ツールチップ + 中立値の語」を
手書きせず、台帳 :mod:`.search_dimensions` の列挙 1 本から生成する。
ラベル左端や『無指定』語のばらつきは**軸ごとに手書きする余地そのもの**が
原因なので、書式のばらけを潰すだけで
なくばらけさせる場所を無くす。

``post_grid`` ではなくここに置く理由: ``post_grid.py`` は既に抽出対象の
規模で、この機構は「台帳 → ウィジェット」
の閉じた変換なので独立して読める。台帳（Qt 非依存）に載せられないのは
**シグナルの接続先**だけなので、それを :data:`ROW_HANDLERS` の 1 表として
ここが持つ — 軸を足す作業は「台帳に 1 行 + この表に 1 行」で完結する。

*host* は ``PostGrid``。ウィジェットは
``dim.widget_attr`` が指す**既存の属性名**で host に載る — 永続化・
スナップショット復元・``AdvancedSearchController``・テストは全てその名前で
参照しており、生成化しても参照面は 1 つも変わらない。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import align_form_labels, hint_style
from .search_dimensions import (
    SearchDimension,
    chip_text,
    combo_items,
    filter_rows,
    form_label,
    tooltip,
)

#: 台帳の行 id → host 上の変更ハンドラ名。ハンドラは Qt 依存の実行時配線
#: なので台帳（Qt 非依存）ではなくここに持つ。
ROW_HANDLERS: dict[str, str] = {
    "recursive": "_on_recursive_toggled",
    "media": "_on_filterbar_media_changed",
    "rating": "_on_tag_rating_changed",
    "date": "_on_filterbar_date_changed",
    "star": "_on_filterbar_star_changed",
    "later": "_on_filterbar_later_toggled",
    "usertag": "_on_filterbar_usertag_changed",
    "locked": "_on_locked_filter_toggled",
}

#: 効いている軸のアクセント QSS（エディタ種別ごと）。色は palette 参照なので
#: テーマ切り替えに追従する（色はハードコードしない）。
_ACCENT_QSS: dict[str, str] = {
    "combo": "QComboBox { border: 1px solid palette(highlight); }",
    "check": "QCheckBox { color: palette(highlight); font-weight: bold; }",
}

#: 行レイアウトの間隔（ラベル列の字下げ量にも使う）。
_ROW_SPACING = 6

#: ★ の選択肢（保存値が int の例外形 — 0 = 中立）。
_STAR_FLOORS = (1, 2, 3, 4, 5)


def _build_row(
    host, dim: SearchDimension, ai_on: bool,
) -> tuple[QLabel, QWidget, QHBoxLayout]:
    """台帳 1 行ぶんのエディタを生成し ``(先頭ラベル, エディタ, 行)`` を返す.

    書式は全軸共通（ラベル右揃え + コントロール）。見出しを持たない
    チェックボックス軸にも**空のラベル**を置いて同じラベル列に載せる。
    :func:`~snappix.common.ui.indent_to_form_column` による字下げは
    ``setContentsMargins`` 1 本なので ``QCheckBox`` には効かず（チェック印と
    テキストの配置は Qt のスタイルが決める）、その行だけがフォーム列から
    外れて左端に取り残される。
    """
    row = QHBoxLayout()
    row.setSpacing(_ROW_SPACING)
    tip = tooltip(dim.id, ai=ai_on)
    handler = getattr(host, ROW_HANDLERS[dim.id])
    if dim.editor == "check":
        check = QCheckBox(t(dim.label_key or ""))
        if tip:
            check.setToolTip(tip)
        check.toggled.connect(handler)
        # 見出し付きのチェック行（検索範囲: ☐ サブフォルダも検索）は見出しを、
        # 見出しの無い行は空ラベルを置く — どちらもコンボ行と同じラベル列。
        lbl = QLabel(form_label(dim.id) if dim.form_label_key else "")
        row.addWidget(lbl)
        row.addWidget(check)
        row.addStretch(1)
        setattr(host, dim.widget_attr, check)
        return lbl, check, row
    combo = QComboBox()
    if dim.combo_source == "star":
        # 中立値の語は他軸と同じ「すべて」（独自語を使わない）。
        combo.addItem(t(dim.neutral_key or "common.filter.all"), 0)
        for n in _STAR_FLOORS:
            # ★ の選択肢テキストは条件チップの文言そのもの（意図的な共有）。
            # 台帳のテンプレートを直に引かず :func:`chip_text` を通す —
            # 「全チップが 1 つの書式チョークポイントを通る」の例外にしない。
            combo.addItem(chip_text(dim.id, n=n), n)
    elif dim.combo_source != "user_tags":
        for key, label_key in combo_items(dim.id):
            combo.addItem(t(label_key), key)
    if tip:
        combo.setToolTip(tip)
    lbl = QLabel(form_label(dim.id))
    row.addWidget(lbl)
    row.addWidget(combo, 1)
    setattr(host, dim.widget_attr, combo)
    if dim.combo_source == "user_tags":
        # 候補はストアの現在値 — 構築直後と開くたびに詰め直す（07-25 #13②）。
        # 属性を載せてからでないと host 側が詰め先を見つけられない。
        host._reload_user_tag_choices(cacheable=False)
    # コンボのハンドラは引数を取らない — 可変長の委譲へ index が素通りしないよう捨てる。
    combo.currentIndexChanged.connect(lambda _index: handler())
    return lbl, combo, row


def build(host, *, ai_on: bool, have_user_meta: bool) -> QFrame:
    """台帳の行を列挙してポップオーバーを組み立て、その ``QFrame`` を返す.

    2 つのゲートの**意味が違う**ことに注意:

    * ``needs_user_meta`` の軸（★ / あとで見る / ユーザータグ）は
      **ウィジェットごと作らない** — 下流は ``getattr(..., None)`` の不在で
      挙動を変える（``_parse_curation_control_tokens`` の ``have_star`` 等）。
    * ``owner="ai"`` の軸（年齢区分）は **作るが席を隠す** — 状態往復
      （``state.tag_search_rating``）と ``rating:`` トークンの復元が素の配布
      でも inert に動き続ける必要があるため（AI ポップオーバー全体が同じ
      「構築するが到達させない」契約で建っている）。

    行に追加のインラインコントロールを持つ軸（投稿日の「範囲」エディタ）は
    ``host._build_date_range_editors`` へ委譲する — この形があるために単一の
    ``QFormLayout`` では組めない（``QFormLayout`` 化を採らない理由そのもの）。
    """
    pop = QFrame(host, Qt.Popup)
    pop.setObjectName("toolbarPopover")
    lay = QVBoxLayout(pop)
    lay.setContentsMargins(10, 8, 10, 8)
    lay.setSpacing(_ROW_SPACING)

    visible_ids = {
        dim.id for dim in filter_rows(ai=ai_on, user_meta=have_user_meta)
    }
    labels: list[QLabel] = []
    for dim in filter_rows(ai=True, user_meta=have_user_meta):
        lbl, editor, row = _build_row(host, dim, ai_on)
        if dim.id == "date":
            host._build_date_range_editors(row)
        lay.addLayout(row)
        if dim.id not in visible_ids:
            # 非表示の軸はラベルごと隠す。整列にも渡さない —
            # ``align_form_labels`` は可視性を見ずに全ラベルの文字幅の max を
            # 採るので、渡した時点で「見えない行が列幅を決めない」が成立
            # しなくなる（素の配布だけラベル列が広がる形）。
            lbl.setVisible(False)
            editor.setVisible(False)
            continue
        labels.append(lbl)
    # ラベル列を 1 つの幅へ揃える（ヘルパは common/ui にあり、AI 検索
    # ポップオーバーと共有する）。見出しの無いチェック行も空ラベルで
    # 同じ列に載っているので、ここへ字下げの例外処理は要らない。
    align_form_labels(*labels)
    # 揮発性の注記。このポップオーバーの値は「並び・表示」/「⋯」と違って
    # **保存されない**のに、見た目からはその差が分からない。機序は 2 つあり混ぜると誤案内になるので分けて書く:
    # ① フォルダ移動での解除（``state.search_clear_on_navigate``・設定で無効化可）
    # ② 再起動でのリセット（そもそも永続化しない）
    hint = QLabel(t("viewer.post_grid.filter_popover_volatile_hint"))
    hint.setWordWrap(True)
    hint.setStyleSheet(hint_style())
    lay.addWidget(hint)
    return pop


def sync_accents(host, engaged: dict[str, bool], *, have_user_meta: bool) -> None:
    """効いている軸のコントロールにアクセントを与える（台帳の列挙）.

    エディタ種別でアクセントの QSS が決まるので、軸を足すときにここへ追記
    する必要が無い（= 片側だけアクセントが付かない、が起こせない）。
    """
    for dim in filter_rows(ai=True, user_meta=have_user_meta):
        widget = getattr(host, dim.widget_attr or "", None)
        if widget is None:
            continue
        widget.setStyleSheet(
            _ACCENT_QSS[dim.editor] if engaged.get(dim.id) else ""
        )
