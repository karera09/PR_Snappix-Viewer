"""条件チップバーのウィジェット部品（``PostGrid`` から切り出し）。

ツールバー直下の 1 行バー。適用中の条件を 1 軸 1 枚のチップで並べ、チップ本体
クリックで編集面へ、× でその軸だけを中立へ戻す。並びと文言は Qt 非依存の
:mod:`.condition_chips` が決め、ここは**描くことと配線だけ**を持つ:

* チップ列の組み直し（:meth:`ConditionBar.render`）
* 席に入らない分の折り畳み（「他 N 件」チップ）と Resize での組み直し
* 右側の固定要素（件数 / [この検索を保存…] / [すべて解除]）

ホストとの境界は 4 本のコールバック（``on_edit(kind, anchor)`` /
``on_clear(dim_id)`` / ``on_save`` / ``on_clear_all``）だけで、バーはホストの
属性を 1 つも読まない。× は次元 id を返すので、状態を変えるのはホストの
``_clear_dim_*`` 側（``PostGrid._condition_clear_callbacks`` の表）。

**破棄後の更新に耐えること**: バーはウィンドウがマウントする（親は
``PostGrid`` ではない）ので、ペインより先に C++ 側が消える経路がある。
更新は非同期の着地（走査結果 → グリッド再構築 → バー更新）から来るため、
閉じた後に 1 回余分に届き得る。:meth:`ConditionBar.is_live` を全ての公開口の
入口に置き、死んだバーへの更新は**黙って捨てる**（受け取る相手がもう居ない
ので捨てるのが正しい）。:meth:`ConditionBar.release` は閉じる側からの明示の
宣言で、C++ がまだ生きているうちに口を閉じる。

テストは ``tests/test_viewer_condition_bar.py``。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import CONDITION_BAR_HEIGHT, hint_style, set_icon
from . import ai_pack
from .condition_chips import ChipSpec, chips_that_fit

__all__ = [
    "CHIP_DROP_WIDTH",
    "CHIP_MAX_W",
    "CHIP_PADDING_W",
    "CHIP_SPACING",
    "ConditionBar",
    "is_live",
]

#: チップ 1 枚のラベル省略幅（px）。長い AIタグ / シード名は省略し、全文は
#: ツールチップへ残す。
CHIP_MAX_W = 200

#: × ボタンの固定幅（px）— WCAG 2.5.8 の最小ターゲット 24px。素の記号文字
#: ボタンは約 14px しか無い。高さ方向は :data:`CONDITION_BAR_HEIGHT` が確保する。
CHIP_DROP_WIDTH = 24

#: チップ 1 枚の文字以外の実寸（左右マージン + QSS の内側余白）— 折り畳み判定を
#: 実構築と同じ材料で見積もるための定数。
CHIP_PADDING_W = 14

#: チップ列の間隔。見積り（:func:`~.condition_chips.chips_that_fit`）と実構築
#: （:meth:`ConditionBar.render` の ``setSpacing``）が同じ値を読む — 片方だけ
#: 変えると折り畳み位置が実幅とずれる。
CHIP_SPACING = 4


def is_live(widget: QWidget | None) -> bool:
    """*widget* の C++ 実体がまだ生きているか（``None`` / 破棄済みは偽）。

    破棄された ``QWidget`` の Python ラッパは残るので、属性アクセスまでは
    通って C++ を触る 1 行目で ``RuntimeError``（``libshiboken: Internal C++
    object ... already deleted``）になる。判定を 1 か所に持ち、``shiboken6``
    が引けない環境（凍結ビルドの最小構成）では生きている扱いにして、実際に
    触る側の try/except に委ねる。
    """
    if widget is None:
        return False
    try:
        from shiboken6 import isValid
    except Exception:  # pragma: no cover (shiboken は PySide6 に同梱)
        return True
    return bool(isValid(widget))


class ConditionBar(QWidget):
    """適用中の条件を示す 1 行バー（``PostGrid.condition_bar`` の実体）.

    **parentless** に組まれ、``main_window._build_ui`` がツールバーの直下へ
    マウントする。条件が 1 つも無い間はバーごと非表示（高さ 0）。
    """

    def __init__(
        self,
        *,
        on_edit: Callable[[str, QWidget], None],
        on_clear: Callable[[str], None],
        on_save: Callable[[], None],
        on_clear_all: Callable[[], None],
    ) -> None:
        super().__init__()
        self._on_edit = on_edit
        self._on_clear = on_clear
        self._on_save = on_save
        self._on_clear_all = on_clear_all
        self._released = False
        self.setObjectName("conditionBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(CONDITION_BAR_HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(CHIP_SPACING)
        self._bar_layout = lay
        #: チップの席 — 更新のたびに**丸ごと**作り直す（半端に使い回した
        #: チップが新しいチップの上へ描かれる重ね描き崩れを構造的に消す）。
        self._chip_host: QWidget | None = None
        self._chips_layout: QHBoxLayout | None = None
        #: 折り畳みの再計算用: 最後に描いた材料と、そのときのバー幅。幅が
        #: 変わったときだけ組み直す。
        self._last: tuple[tuple[ChipSpec, ...], int, str, bool] | None = None
        self._folded_at = -1
        lay.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: palette(highlight);")
        lay.addWidget(self.count_label)
        # 非表示の集約ラベル — バーのツールチップと、検索条件を 1 本の文字列
        # として読む呼び出し側 / テストのための全文（可視面はチップ列）。
        self.summary_label = QLabel("")
        self.summary_label.setVisible(False)
        lay.addWidget(self.summary_label)
        # 「この検索を保存…」— 作成の導線がメニュー 1 本しか無く、いま効いて
        # いる条件の隣に無かった。バーごと ``setVisible(False)`` に従うのに
        # 加えて、ボタン単体の可視はホストが渡す *save_active* が決める（バーの
        # 可視とは別軸 — 横断一覧だけが条件のときバーは出るが、保存できる検索
        # 条件は無く、押せば必ず撥ねられるボタンになるため）。
        self.save_btn = QPushButton(t("viewer.main_window.save_search"))
        self.save_btn.setToolTip(t("viewer.post_grid.save_search_tooltip"))
        self.save_btn.clicked.connect(self._on_save_clicked)
        lay.addWidget(self.save_btn)
        self.clear_btn = QPushButton(t("viewer.post_grid.clear_all"))
        self.clear_btn.setToolTip(
            t("viewer.post_grid.clear_all_tooltip") if ai_pack.available()
            else t("viewer.post_grid.clear_all_tooltip_free")
        )
        self.clear_btn.clicked.connect(self._on_clear_all_clicked)
        lay.addWidget(self.clear_btn)
        self.setVisible(False)

    # ------------------------------------------------------------- 寿命

    def is_live(self) -> bool:
        """更新を受け付ける状態か（``release()`` 済み / 破棄済みは偽）。"""
        return not self._released and is_live(self)

    def release(self) -> None:
        """閉じる側からの明示の宣言 — 以後の更新を黙って捨てる。

        ``PostGrid.shutdown()`` から呼ぶ。窓の破棄で C++ が消える経路は
        :func:`is_live` が拾うが、ウィジェットが生きているうちに走る後追いの
        着地（走査結果 → 再構築 → バー更新）も同じ口で止めたい。
        """
        self._released = True
        self._last = None

    # ------------------------------------------------------------- 読み口

    @property
    def chip_host(self) -> QWidget | None:
        """いまチップを載せている席（無ければ ``None``）。"""
        return self._chip_host

    @property
    def chips_layout(self) -> QHBoxLayout | None:
        """チップ列のレイアウト（無ければ ``None``）。"""
        return self._chips_layout

    # ------------------------------------------------------------- 更新

    def hide_bar(self) -> None:
        """条件ゼロ — 席を畳んでバーごと隠す（高さ 0）。"""
        if not self.is_live():
            return
        self._drop_chip_host()
        self._last = None
        self.summary_label.setText("")
        self.setToolTip("")
        self.setVisible(False)

    def render(
        self,
        specs: Sequence[ChipSpec],
        *,
        shown: int,
        summary: str,
        save_active: bool,
    ) -> None:
        """チップ列を組み直してバーを出す。

        *specs* が空でも呼ばれ得る — 条件は効いているのに描くチップが無い
        （検索欄が同じ語を見せている絞り込み軸だけだった）場合で、そのときは
        「件数 + すべて解除」だけの**空帯**に見えないよう、何で絞り込み中かを
        示す非クリックの薄いラベルを左端へ置く。条件が 1 つも無いときは
        :meth:`hide_bar` の担当。
        """
        if not self.is_live():
            return
        self._drop_chip_host()
        self._last = (tuple(specs), shown, summary, save_active)
        # 右側の固定要素（件数 / 保存 / すべて解除）の幅は折り畳み予算の材料
        # なので、チップを並べる前に確定させる。
        self.save_btn.setVisible(save_active)
        self.count_label.setText(t("viewer.post_grid.banner_count", n=shown))
        host = QWidget(self)
        chip_lay = QHBoxLayout(host)
        chip_lay.setContentsMargins(0, 0, 0, 0)
        chip_lay.setSpacing(CHIP_SPACING)
        if not specs:
            quiet = QLabel(t("viewer.post_grid.condition_bar_name_filter_only"))
            quiet.setStyleSheet(hint_style())
            chip_lay.addWidget(quiet)
        labels = [spec.label for spec in specs]
        keep = self.chips_that_fit(labels, budget=self.chip_budget())
        for spec in specs[:keep]:
            chip_lay.addWidget(
                self.make_chip(
                    spec.label,
                    spec.kind,
                    lambda key=spec.key: self._on_clear(key),
                )
            )
        if keep < len(specs):
            chip_lay.addWidget(self._make_overflow_chip(labels[keep:]))
        self._chip_host = host
        self._chips_layout = chip_lay
        self._bar_layout.insertWidget(0, host)
        self._folded_at = self.width()
        self.summary_label.setText(summary)
        self.setToolTip(summary)
        self.setVisible(True)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        """幅が変わったら折り畳み位置を引き直す。

        省略はチップ単体の幅にしか効かないので、入り切る枚数は実幅が材料。
        同じ幅での再入は無視 — 組み直しがバー自身の Resize を呼び戻す輪を
        作らない。組み直しは**最後に描いた材料**から行う（状態を読み直さない
        ので、Resize が新しい観測時点を作らない）。
        """
        super().resizeEvent(event)
        if (
            self._last is not None
            and self.isVisible()
            and self.width() != self._folded_at
        ):
            specs, shown, summary, save_active = self._last
            self.render(
                specs, shown=shown, summary=summary, save_active=save_active,
            )

    def _drop_chip_host(self) -> None:
        """チップの席を丸ごと落とす（ウィジェットもレイアウトも作り直す）。"""
        old, self._chip_host = self._chip_host, None
        self._chips_layout = None
        if old is not None:
            self._bar_layout.removeWidget(old)
            old.hide()
            old.deleteLater()

    # ------------------------------------------------------------- チップ

    def make_chip(
        self,
        label: str,
        kind: str,
        on_clear: Callable[[], None] | None = None,
    ) -> QFrame:
        """条件チップ 1 枚（クリックできるラベル + × ボタン）。

        ラベル部はフラットな ``QToolButton`` で、クリックで対応する編集面を
        開く。末尾の × はこの次元だけを落とす。配色は ``#conditionChip`` の
        QSS ブロック（アクセント枠 + 薄いアクセント塗り — デザイントークンのみ）。
        """
        chip = QFrame()
        chip.setObjectName("conditionChip")
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(4, 0, 2, 0)
        lay.setSpacing(0)
        text_btn = QToolButton()
        text_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        fm = text_btn.fontMetrics()
        shown_label = label
        if fm.horizontalAdvance(label) > CHIP_MAX_W:
            shown_label = fm.elidedText(label, Qt.ElideRight, CHIP_MAX_W)
        text_btn.setText(shown_label)
        if kind != "none":
            text_btn.setToolTip(label)
            text_btn.setCursor(Qt.PointingHandCursor)
            text_btn.clicked.connect(
                lambda _=False, k=kind, w=chip: self._on_edit(k, w)
            )
        else:
            # 編集面を持たないチップは編集できるチップと同じ見た目なので、
            # 本体が不活性であることを名乗る — × が「唯一の出口なのに気付け
            # ない操作」にならないように。
            text_btn.setToolTip(
                label + "\n" + t("viewer.post_grid.chip_static_hint")
            )
        lay.addWidget(text_btn)
        drop = QToolButton()
        # 記号文字「×」の素のボタンはクリック幅が約 14px しかなく WCAG 2.5.8
        # （最小ターゲット 24px）未達で、design.md の「記号文字を図像代わりに
        # 使わない」にも反する。SVG の x グリフ + 当たり判定の固定幅で置く。
        set_icon(drop, "x", role="accent", size=12)
        drop.setFixedWidth(CHIP_DROP_WIDTH)
        drop.setCursor(Qt.PointingHandCursor)
        drop.setToolTip(t("viewer.post_grid.chip_remove_tooltip", label=label))
        if on_clear is not None:
            drop.clicked.connect(lambda _=False: on_clear())
        lay.addWidget(drop)
        return chip

    def _make_overflow_chip(self, labels: Sequence[str]) -> QFrame:
        """畳んだ条件をまとめる「他 N 件」チップ（× も編集面も持たない）。

        個別の × を出せない代わりに、何が畳まれているかはツールチップで全部
        名乗る。解除は「すべて解除」かフィルターポップオーバーから行える。
        """
        chip = QFrame()
        chip.setObjectName("conditionChip")
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(0)
        text = QToolButton()
        text.setToolButtonStyle(Qt.ToolButtonTextOnly)
        text.setText(t("viewer.post_grid.condition_overflow", n=len(labels)))
        text.setToolTip(
            t(
                "viewer.post_grid.condition_overflow_tooltip",
                items="\n".join(labels),
            )
        )
        lay.addWidget(text)
        return chip

    # --------------------------------------------------------- 折り畳み

    def chip_width(self, label: str) -> int:
        """*label* のチップ 1 枚が要する幅の見積り（px）。"""
        fm = self.fontMetrics()
        text = min(fm.horizontalAdvance(label), CHIP_MAX_W)
        return text + CHIP_DROP_WIDTH + CHIP_PADDING_W

    def chip_budget(self) -> int:
        """チップ列に使える横幅（右側の固定要素を引いた残り）。

        ``-1`` は「まだレイアウトされていない（幅が未確定）」、``0`` は
        「レイアウト済みだが席が 1 枚ぶんも無い」— 2 値の違いは
        :func:`~.condition_chips.chips_that_fit` の docstring 参照。
        """
        if not self.is_live() or self.width() <= 0:
            return -1
        margins = self._bar_layout.contentsMargins()
        reserved = margins.left() + margins.right()
        for widget in (self.count_label, self.save_btn, self.clear_btn):
            if widget.isHidden():
                continue
            reserved += widget.sizeHint().width() + self._bar_layout.spacing()
        return max(0, self.width() - reserved)

    def chips_that_fit(self, labels: Sequence[str], *, budget: int) -> int:
        """先頭から何枚まで並ぶか（判定は :func:`.condition_chips.chips_that_fit`）."""
        return chips_that_fit(
            [self.chip_width(label) for label in labels],
            budget=budget,
            spacing=CHIP_SPACING,
            overflow_width=self.chip_width(
                t("viewer.post_grid.condition_overflow", n=len(labels))
            ),
        )

    # --------------------------------------------------------- ボタン配線

    def _on_save_clicked(self) -> None:
        self._on_save()

    def _on_clear_all_clicked(self) -> None:
        self._on_clear_all()
