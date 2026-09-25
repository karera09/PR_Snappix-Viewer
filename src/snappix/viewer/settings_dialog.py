"""Modal settings dialog for per-viewer preferences.

Exposes tunables previously hardcoded in the viewer subtree so users can
adjust UX density (thumbnail slider bounds, preview scroll speed), preview
image cache budgets, and I/O concurrency (scan parallelism, thumbnail
decode pool).  Changes are applied live to the running viewer widgets via
``ViewerWindow._apply_settings_live`` and persisted when the window closes
by writing the shared :class:`ViewerState` to ``viewer_state.json``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QDoubleSpinBox,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.paths import get_paths
from ..common.ui import (
    confirm_action,
    hint_style,
    localize_buttons,
    set_icon,
    show_toast,
)
from .markdown_view import MAX_FONT_PT as MARKDOWN_MAX_FONT_PT
from .markdown_view import MIN_FONT_PT as MARKDOWN_MIN_FONT_PT
from .state import ViewerState
from .theme import THEME_CHOICES_EXTRA, THEME_CHOICES_MAIN, extra_theme_label
from .view_prefs import _format_bytes as _fmt_bytes
from .view_prefs import notify_failure

#: Performance presets (パフォーマンスタブ先頭の「プリセット」).  Each maps
#: preset key → {ViewerState field: value} for the I/O tunables on that tab.
#:
#: Rationale for the value sets:
#:
#: * ``standard`` — the ``ViewerState`` class defaults, i.e. the values the
#:   viewer has always shipped with (post.md 6 / thumb decode 4 / probe 2,
#:   256-thumb memory cache).  Local SSDs and healthy gigabit NAS links are
#:   latency-bound, so this level of fan-out amortises round-trips without
#:   the pools competing with each other.
#: * ``nas`` — for SMB shares that throttle aggressively.  The live pools
#:   share one SMB credit window;
#:   at the standard fan-out (6+4+2 ≈ 12 outstanding requests) a slow or
#:   credit-starved server makes every request — including tiny UI
#:   round-trips — crawl, which reads as "the viewer froze".  Halving the
#:   concurrency (3+2+1 ≈ 6) keeps the credit window breathing.  The decoded-
#:   thumbnail memory cache is doubled instead (512): RAM is cheap while
#:   re-fetching over a slow link is not, so trading memory for fewer NAS
#:   round-trips is the right direction on exactly these setups.
#:
#: The presets only WRITE the existing persisted fields on selection — the
#: fields stay the single source of truth, so hand-tuned values (プリセット
#: 「カスタム」) and old state files keep working unchanged.
PERF_PRESETS: dict[str, dict[str, int]] = {
    "standard": {
        "scan_metadata_parallelism": 6,
        "thumbnail_cache_size": 256,
        "thumbnail_max_threads": 4,
        "aspect_probe_parallelism": 2,
    },
    "nas": {
        "scan_metadata_parallelism": 3,
        "thumbnail_cache_size": 512,
        "thumbnail_max_threads": 2,
        "aspect_probe_parallelism": 1,
    },
}

#: The ViewerState fields a preset covers (single definition — the matcher
#: and the apply path iterate this).
_PERF_FIELDS = (
    "scan_metadata_parallelism",
    "thumbnail_cache_size",
    "thumbnail_max_threads",
    "aspect_probe_parallelism",
)


def match_perf_preset(values: dict[str, int]) -> str:
    """Return the preset key whose value set equals *values*, else "custom"."""
    for key, preset in PERF_PRESETS.items():
        if all(values.get(f) == preset[f] for f in _PERF_FIELDS):
            return key
    return "custom"


@runtime_checkable
class CacheController(Protocol):
    """The surface ``SettingsDialog`` needs from its cache controller.

    Formalises what was previously an implicit ``hasattr`` contract against a
    duck-typed object (``ViewerWindow``).  Making it a ``Protocol`` lets a type
    checker flag a renamed/removed method statically, and ``runtime_checkable``
    lets the dialog still guard the optional ``tag_index_stats`` /
    ``cache_build_in_progress`` members (older controllers / test doubles may
    omit them) with ``hasattr`` instead of relying on comments.

    **``viewer/`` は pyright のスコープ外**（``pyrightconfig.json`` の include
    は ``src/snappix/common``）なので、「型検査器が静的に弾く」という上の効能は
    ここでは効かない。実装 ``CacheBuildController`` とのシグネチャ一致は
    ``tests/test_of_d_cache_controller.py`` の parity テストが担保する
    （キーワード専用引数が実装にだけ生えると、2 つが黙って乖離する）。
    """

    def cache_stats(self) -> dict[str, Any]: ...

    def clear_persistent_caches(self, kinds: set[str] | None = None) -> None: ...

    def build_cache_interactive(
        self, parent: QWidget, *,
        confirm_background: Callable[[], bool] | None = None,
        notify: bool = True,
    ) -> dict | None: ...

    def cache_build_in_progress(self) -> bool: ...

    def tag_index_stats(self) -> dict[str, Any] | None: ...


class _CollapsibleGroupBox(QGroupBox):
    """A ``QGroupBox`` that can be closed down to just its title row.

    The dialog grew tall enough (many groups across three tabs) that users
    asked to be able to collapse sections they don't need.  ``QGroupBox``
    already ships a checkable title-click affordance (``setCheckable``); this
    subclass just wires that checkbox to hide/show every direct child of the
    box's own layout instead of the default enable/disable behavior.  Kept
    local to this module rather than reusing ``post_grid.py``'s private
    ``_CollapsibleSection`` (out of scope to touch/copy here) — the two
    widgets solve the same UX problem against different base classes
    (``QGroupBox`` vs. a bespoke header-button + body-widget pair) and don't
    share implementation.

    Usage: construct with a title, then build the group's contents exactly
    like a plain ``QGroupBox`` (``QFormLayout(box)`` / ``QVBoxLayout(box)``
    + ``.addRow``/``.addWidget`` as usual). Starts expanded.
    """

    #: Logical px size of the chevron indicator glyph.
    _CHEVRON_PX = 16

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._plain_title = title
        self.setCheckable(True)
        self.setChecked(True)
        # Hide the native checkbox indicator: a checkbox in a settings
        # dialog reads as "this section is on/off", not "collapsed".  The
        # expand state is conveyed by a themed SVG chevron overlaid on the
        # title row instead of a ▼/▶ text glyph (記号文字をボタングリフに
        # 使わない).  The title
        # text is indented (``::title padding-left``) to leave room for it.
        # ``width/height: 0`` だけでは標識の枠が 4px 残り、見出しの前に用途
        # 不明な点として見える（light テーマで顕著）。``border: none`` を足すと画素差分が 0 になる（``image:none``
        # / ``background:transparent`` でも同値だが、意図が最も読めるのは枠）。
        self.setStyleSheet(
            "QGroupBox::indicator { width: 0px; height: 0px; border: none; }"
            "QGroupBox::title { padding-left: %dpx; }" % (self._CHEVRON_PX + 4)
        )
        # A borderless QToolButton so ``set_icon`` can keep the chevron on the
        # theme-retint registry (a plain QLabel has no ``setIcon``).  It also
        # gives the arrow a real click target; the whole title strip stays
        # clickable via ``mousePressEvent`` below.
        self._chevron = QToolButton(self)
        self._chevron.setAutoRaise(True)
        self._chevron.setFocusPolicy(Qt.NoFocus)
        self._chevron.setCursor(Qt.PointingHandCursor)
        self._chevron.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0px; }"
        )
        self._chevron.clicked.connect(
            lambda: self.setChecked(not self.isChecked())
        )
        self.toggled.connect(self._on_toggled)
        self._sync_title(True)

    def _sync_title(self, expanded: bool) -> None:
        # Title text is plain (no arrow glyph); the chevron icon conveys state.
        self.setTitle(self._plain_title)
        set_icon(
            self._chevron,
            "chevron-down" if expanded else "chevron-right",
            role="muted",
            size=self._CHEVRON_PX,
        )
        self._position_chevron()

    def _position_chevron(self) -> None:
        # Sit the chevron at the top-left, roughly centred against the title
        # baseline (the title is drawn along the top border of the box).
        y = max(0, (self.fontMetrics().height() - self._CHEVRON_PX) // 2)
        self._chevron.setGeometry(2, y, self._CHEVRON_PX, self._CHEVRON_PX)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._position_chevron()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # With the indicator hidden the native toggle hit-area is gone, so
        # make the whole title row (top strip of the box) clickable.
        if event.position().y() <= self.fontMetrics().height() + 6:
            self.setChecked(not self.isChecked())
            event.accept()
            return
        super().mousePressEvent(event)

    def _on_toggled(self, checked: bool) -> None:
        self._sync_title(checked)
        layout = self.layout()
        if layout is not None:
            self._set_layout_children_visible(layout, checked)

    @classmethod
    def _set_layout_children_visible(cls, layout, visible: bool) -> None:
        # ``itemAt`` walks every slot regardless of layout kind, so this
        # covers a ``QFormLayout`` (each row is up to two items: label +
        # field widget) and a plain ``QVBoxLayout`` of stacked widgets.
        # Items that are themselves nested layouts (e.g. a ``QHBoxLayout`` of
        # buttons added via ``addLayout``) have no ``.widget()`` — recurse
        # into them so their child widgets collapse too, instead of being
        # left floating under the title row (the cache-management button row
        # regression).
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item is None:
                continue
            child = item.widget()
            if child is not None:
                child.setVisible(visible)
                continue
            sub = item.layout()
            if sub is not None:
                cls._set_layout_children_visible(sub, visible)


def _mib_spin(maximum: int = 16384) -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(1, maximum)
    sb.setSuffix(" MiB")
    sb.setSingleStep(16)
    sb.setAlignment(Qt.AlignRight)
    return sb


#: 本文フォントサイズスピンの「アプリ既定に従う」値。実装側の下限
#: （:data:`~snappix.viewer.markdown_view.MIN_FONT_PT`）の 1 つ下を占位に使い、
#: ``setSpecialValueText`` で語に置き換える。state 側の 0 と往復させる。
#: 範囲は実装側から**導出**する — 写すと、実装側の上限を上げたときに
#: Ctrl+ホイールで到達した値が「設定を開いて OK」だけで黙って縮む。
_MD_FONT_PT_AUTO = MARKDOWN_MIN_FONT_PT - 1
_MD_FONT_PT_MAX = MARKDOWN_MAX_FONT_PT

#: 全画面表示の操作 UI を隠すまでの時間（秒表示・state は ms）。下限は
#: ``lightbox._AUTO_HIDE_MIN_MS`` より上に置く（一瞬で消える値は選ばせない）。
_CHROME_HIDE_MIN_SEC = 0.5
_CHROME_HIDE_MAX_SEC = 30.0
_CHROME_HIDE_STEP_SEC = 0.5


def _seconds_spin() -> QDoubleSpinBox:
    """ミリ秒の state 値を秒（小数 1 桁）で編集する QDoubleSpinBox.

    初期値は束縛表（``_bind`` → ``_write_widget``）が入れる。``setRange`` 済み
    なので範囲外の古い state 値は自動でクランプされる。
    """
    sb = QDoubleSpinBox()
    sb.setDecimals(1)
    sb.setRange(_CHROME_HIDE_MIN_SEC, _CHROME_HIDE_MAX_SEC)
    sb.setSingleStep(_CHROME_HIDE_STEP_SEC)
    sb.setSuffix(t("viewer.settings_dialog.suffix_seconds"))
    sb.setAlignment(Qt.AlignRight)
    return sb


def _seconds_spin_to_ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _count_spin(maximum: int = 4096, minimum: int = 1,
                step: int = 1, suffix: str = "") -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(minimum, maximum)
    sb.setSingleStep(step)
    if suffix:
        sb.setSuffix(suffix)
    sb.setAlignment(Qt.AlignRight)
    return sb


def _read_widget(w: QWidget) -> Any:
    """入力ウィジェットの現在値を型から決めて読む。"""
    if isinstance(w, QCheckBox):
        return w.isChecked()
    if isinstance(w, QComboBox):
        return w.currentData()
    if isinstance(w, (QSpinBox, QDoubleSpinBox)):
        return w.value()
    raise TypeError(f"unsupported settings widget: {type(w).__name__}")


def _write_widget(w: QWidget, value: Any) -> None:
    """*value* を入力ウィジェットへ書く（読みの逆写像）。

    ``QSpinBox.setValue`` はレンジへ自動クランプするので、範囲外の値を持つ
    古い state ファイルもここで丸まる。``QComboBox`` は安定キー（``userData``）
    で引き、見つからなければ先頭 — 廃止された選択肢が残った state でも
    選択が空にならない。
    """
    if isinstance(w, QCheckBox):
        w.setChecked(bool(value))
        return
    if isinstance(w, QComboBox):
        idx = w.findData(value)
        w.setCurrentIndex(idx if idx >= 0 else 0)
        return
    if isinstance(w, QDoubleSpinBox):
        w.setValue(float(value))
        return
    if isinstance(w, QSpinBox):
        w.setValue(int(value))
        return
    raise TypeError(f"unsupported settings widget: {type(w).__name__}")


@dataclass(frozen=True)
class _FieldSpec:
    """``ViewerState`` の 1 フィールドと、それを編集する入力ウィジェットの束縛.

    ``to_state`` / ``from_state`` はウィジェットの表現と永続値がずれる行だけ
    が持つ写像（本文フォントサイズの「0 = アプリ既定」↔ 占位値など）。
    """

    field: str
    widget: QWidget
    to_state: Callable[[Any], Any] | None = None
    from_state: Callable[[Any], Any] | None = None

    def read(self) -> Any:
        value = _read_widget(self.widget)
        return self.to_state(value) if self.to_state else value

    def write(self, value: Any) -> None:
        _write_widget(
            self.widget, self.from_state(value) if self.from_state else value
        )


#: ``_on_build_cache`` が走らせるビルドが（コントローラ経由で）読む設定
#: フィールド。束縛表の**部分集合**をフィールド名で指すだけなので、accept の
#: 書き戻しと二重に実装されることがない。名前が実在することは
#: ``tests/test_viewer_settings_dialog.py`` が機械検証する。
_CACHE_BUILD_FIELDS = (
    "cache_build_background",
    "thumb_disk_cache_enabled",
    "thumb_disk_cache_max_mib",
    "thumb_disk_cache_max_edge",
    "aspect_cache_max_mib",
    "search_index_max_mib",
    "folder_preview_cache_max_mib",
)


class SettingsDialog(QDialog):
    """Tabbed settings dialog.  Commits to the passed-in ``ViewerState``."""

    def __init__(
        self, state: ViewerState, parent: QWidget | None = None,
        *, cache_controller: CacheController | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.settings_dialog.window_title"))
        self.setModal(True)
        self._state = state
        # Optional controller implementing :class:`CacheController` (the live
        # ``ViewerWindow``).  ``None`` (tests / standalone) just disables the
        # cache-management group.
        self._cache_controller: CacheController | None = cache_controller
        # ``_on_build_cache`` has to push the cache tab's edits into ``state``
        # before accept() — the controller reads them straight off the state
        # object (mode, thumbnail edge, aspect budget …).  These let ``reject``
        # undo that so cancelling really does leave state untouched.
        self._build_touched_state = False
        self._pre_build_state: dict[str, object] = {}
        # 「初期値を読む / OK で書き戻す / 既定値に戻す」の 3 経路が回る唯一の
        # 表。``_bind`` が生成箇所で 1 行ずつ積む（宣言順 = 生成順で、行どうしの
        # 依存 — 件数上限が先読みスピンの上限を絞る、など — がそのまま保たれる）。
        self._fields: list[_FieldSpec] = []
        # プリセットがスピンへ値を書いている間、「手編集 → カスタム」への降格を
        # 抑止する。``_build_performance_tab`` より前に置くのは、表の一括書き戻し
        # （``_restore_defaults``）がタブ構築の前後どちらから来ても読めるように。
        self._applying_preset = False
        # 背景ビルドの在/不在を追う自走タイマー。``_build_cache_tab``
        # が interval / 接続を決めて start する。停止は :meth:`done` の 1 箇所。
        self._cache_status_timer = QTimer(self)

        root = QVBoxLayout(self)

        self._tabs = QTabWidget()
        self._tabs.addTab(
            self._scrollable(self._build_display_tab()),
            t("common.label.display"),
        )
        self._tabs.addTab(
            self._scrollable(self._build_cache_tab()),
            t("viewer.common.cache"),
        )
        self._tabs.addTab(
            self._scrollable(self._build_performance_tab()),
            t("viewer.settings_dialog.tab_performance"),
        )
        root.addWidget(self._tabs)

        btns = localize_buttons(QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults
        ))
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        reset_btn = btns.button(QDialogButtonBox.RestoreDefaults)
        assert isinstance(reset_btn, QPushButton)
        # 「既定値に戻す」は表示・キャッシュ・パフォーマンスの 3 タブ全部
        # （テーマまで）を巻き戻すので、対象範囲をツールチップで示し、確認
        # モーダル（既定 No）+ 成功トーストを付ける（同ダイアログのキャッシュ
        # 削除と同じ方針）。
        reset_btn.setToolTip(t("viewer.settings_dialog.restore_defaults_tooltip"))
        reset_btn.clicked.connect(self._confirm_restore_defaults)
        root.addWidget(btns)

        # The dialog used to grow as tall as every group's combined sizeHint
        # (well past 1000px with all the caption/media/caching groups added
        # over time), forcing users to resize it on smaller screens every
        # time. Each tab now scrolls internally (see ``_scrollable``) and
        # groups collapse, so a modest fixed starting height is enough — the
        # dialog stays freely resizable (no ``setFixedSize``).
        self.resize(560, 560)

    # ---------------------------------------------------------- field table

    def _bind(
        self, field: str, widget: QWidget, *,
        to_state: Callable[[Any], Any] | None = None,
        from_state: Callable[[Any], Any] | None = None,
    ) -> Any:
        """*widget* を ``ViewerState.<field>`` へ束縛し、初期値を入れて返す.

        設定の「編集 → 反映 → 永続化」はかつて 3 本の手書き平行リスト
        （生成箇所が state から読む / :meth:`accept` が 1 行ずつ書き戻す /
        :meth:`_restore_defaults` が 1 行ずつ既定へ戻す）で、約 40 行が 3 回
        並んでいた。新しい設定行を足すときに 1 本落ちても動くので、実際に
        片側欠落が起きている（本文フォントサイズが永続するのにダイアログにも
        「既定値に戻す」にも無かった / キャッシュビルドが読む値の押し込みが
        1 フィールドだけだった）。ここで束縛すると 3 経路すべてが同じ表を
        回るので、新しい設定は**生成箇所の 1 行**だけになる。

        読み書きはウィジェットの型で決まる（:func:`_read_widget` /
        :func:`_write_widget`）。ウィジェットの表現と永続値がずれる行だけが
        *to_state* / *from_state* を持つ。

        戻り値を代入する形（``self._x = self._bind("x", _count_spin(...))``）
        で使うこと — 生成箇所でフィールド名と読み書き先が 1 度しか書かれない
        ため、「読む先と書く先が別フィールド」という取り違えが構造的に起きない。
        """
        spec = _FieldSpec(field, widget, to_state, from_state)
        self._fields.append(spec)
        spec.write(getattr(self._state, field))
        return widget

    @staticmethod
    def _scrollable(content: QWidget) -> QScrollArea:
        """Wrap *content* in a vertically-scrolling area.

        Used for every tab page so a dialog height of ~560px is enough
        regardless of how many groups are expanded — the scrollbar (not the
        dialog) grows to hold the rest.  ``widgetResizable`` lets the inner
        widget reflow to the viewport width so wrapped ``QLabel`` hints and
        form rows still lay out correctly.

        The horizontal bar is ``AsNeeded``, **not** ``AlwaysOff``:
        ``widgetResizable`` reflows the content down to its *minimum* width and
        no further, so a viewport narrower than that silently clips the right
        edge — and the right edge is where the spin boxes put their value, unit
        and up/down buttons (``setAlignment(AlignRight)``).  With the bar off
        there was no way to reach them at all; the dialog has no width floor of
        its own, and a long unbreakable path in the cache tab (the ``data/``
        location row) can push the minimum past the default width on its own.
        In the normal case the content fits and no bar appears, so the "the
        scrollbar grows, not the dialog" intent above is unchanged.
        """
        area = QScrollArea()
        area.setWidget(content)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        area.setFrameShape(QFrame.NoFrame)
        return area

    # ------------------------------------------------------------------ tabs

    def _build_display_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(t("viewer.settings_dialog.display_intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # I02: テーマ選択を表示タブ先頭にも掲載（表示メニューのトグルと同じ状態を
        # 読み書きする）。データは安定キー、表示は既存のテーマ名を流用。
        theme_box = _CollapsibleGroupBox(t("viewer.main_window.theme_menu"))
        theme_v = QVBoxLayout(theme_box)
        self._theme_combo = QComboBox()
        # メニューと同じ選択肢テーブル（viewer/theme.py）を流用 — メイン 4 択 +
        # 「その他」の追加テーマをフラットに列挙する（コンボにサブメニューは
        # 無いため）。ここが独自リストを持つとメニューと必ずドリフトする。
        # 10 テーマがフラットで並ぶと主要 4 択と追加 6 択の
        # 区別が付かないため、区切り線 + 追加テーマの表示名に明暗を付記する
        # （保存値は従来どおり内部キーのまま — 表示のみの変更）。
        for key, label_key in THEME_CHOICES_MAIN:
            self._theme_combo.addItem(t(label_key), key)
        self._theme_combo.insertSeparator(self._theme_combo.count())
        for key, label_key in THEME_CHOICES_EXTRA:
            self._theme_combo.addItem(extra_theme_label(key, label_key), key)
        self._bind("theme", self._theme_combo)
        theme_v.addWidget(self._theme_combo)
        theme_hint = QLabel(t("viewer.settings_dialog.theme_hint"))
        theme_hint.setWordWrap(True)
        theme_hint.setStyleSheet(hint_style())
        theme_v.addWidget(theme_hint)
        layout.addWidget(theme_box)

        form_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_slider_scroll"))
        form = QFormLayout(form_box)
        self._post_grid_icon_max = self._bind("post_grid_icon_size_max", _count_spin(
            minimum=128, maximum=1024, step=16, suffix=" px",
        ))
        self._file_list_icon_max = self._bind("file_list_icon_size_max", _count_spin(
            minimum=128, maximum=1024, step=16, suffix=" px",
        ))
        self._preview_scroll = self._bind("preview_scroll_pixels", _count_spin(
            minimum=20, maximum=800, step=20, suffix=" px",
        ))
        self._wheel_nav_grace = self._bind("wheel_nav_grace_ms", _count_spin(
            minimum=0, maximum=3000, step=50, suffix=" ms",
        ))
        # ヒントは対象グループ**の中**（スパン行）へ入れる — タブ直下の
        # QVBoxLayout に置くと _CollapsibleGroupBox が畳んでも説明文だけが枠外に
        # 残り、折りたたみの動機（縦の節約）が半減する。グループ末尾にまとめて
        # 積むと対象行から離れ、直上の別の行の説明に読めてしまう。
        # ``QFormLayout`` はスパン行を任意位置へ挿入できるので、**各ヒントを
        # 対応する行の直下へ**置く。
        def _hint_row(key: str) -> None:
            lbl = QLabel(t(key))
            lbl.setWordWrap(True)
            lbl.setStyleSheet(hint_style())
            form.addRow(lbl)

        form.addRow(
            t("viewer.settings_dialog.left_pane_list_max"), self._post_grid_icon_max
        )
        form.addRow(
            t("viewer.settings_dialog.right_pane_list_max"), self._file_list_icon_max
        )
        _hint_row("viewer.settings_dialog.display_tab_hint")
        form.addRow(
            t("viewer.settings_dialog.preview_scroll_amount"), self._preview_scroll
        )
        # この値は GalleryView のホイールハンドラ
        # が読むので、プレビュー列だけでなく左右ペインの一覧にも効く。
        _hint_row("viewer.settings_dialog.preview_scroll_hint")
        form.addRow(
            t("viewer.settings_dialog.wheel_nav_grace_label"), self._wheel_nav_grace
        )
        _hint_row("viewer.settings_dialog.wheel_nav_grace_hint")
        layout.addWidget(form_box)

        text_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_text_preview"))
        text_form = QFormLayout(text_box)
        self._text_max_mib = self._bind("text_preview_max_mib", _count_spin(
            minimum=1, maximum=256, step=1, suffix=" MiB",
        ))
        text_form.addRow(t("viewer.settings_dialog.text_body_max"), self._text_max_mib)
        # 「ヒントは対象行の直下」規約: text_hint はこの行（ログ・CSV・
        # JSON の読み込み上限）の説明なので、下に別対象の行が増える前に置く。
        def _text_hint_row(key: str) -> None:
            lbl = QLabel(t(key))
            lbl.setWordWrap(True)
            lbl.setStyleSheet(hint_style())
            text_form.addRow(lbl)  # 畳んだら一緒に消える位置へ

        _text_hint_row("viewer.settings_dialog.text_hint")
        # 本文フォントサイズ。Ctrl+ホイールで
        # 変えられて ``ViewerState.markdown_font_pt`` に**永続**するのに、
        # 設定ダイアログから見えず「既定値に戻す」の対象にも入らないのでは
        # 困るので、ここに置く。
        # 0 = アプリ既定追従という現行の意味は、スピンの最小値の
        # specialValueText で表す（0 を打てるようにすると 0〜7pt という
        # 無効域を作ってしまう）。
        self._markdown_font_pt = self._bind(
            "markdown_font_pt",
            _count_spin(
                minimum=_MD_FONT_PT_AUTO, maximum=_MD_FONT_PT_MAX,
                step=1, suffix=" pt",
            ),
            # state の 0（= アプリ既定追従）と、スピンの占位値の相互写像。
            to_state=lambda pt: 0 if pt <= _MD_FONT_PT_AUTO else pt,
            from_state=lambda pt: pt or _MD_FONT_PT_AUTO,
        )
        self._markdown_font_pt.setSpecialValueText(
            t("viewer.settings_dialog.markdown_font_pt_auto")
        )
        text_form.addRow(
            t("viewer.settings_dialog.markdown_font_pt"), self._markdown_font_pt
        )
        # 同じ群の「本文読み込み上限」と対象が
        # 違う（TextView / MarkdownView）ので、対象と 0 の意味をここで言う。
        _text_hint_row("viewer.settings_dialog.markdown_font_pt_hint")
        layout.addWidget(text_box)

        zip_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_zip_preview"))
        zip_form = QFormLayout(zip_box)
        self._zip_size_limit = self._bind("zip_preview_size_limit_mib", _count_spin(
            minimum=1, maximum=4096, step=10, suffix=" MiB",
        ))
        zip_form.addRow(t("viewer.settings_dialog.zip_read_max"), self._zip_size_limit)
        zip_hint = QLabel(t("viewer.settings_dialog.zip_hint"))
        zip_hint.setWordWrap(True)
        zip_hint.setStyleSheet(hint_style())
        zip_form.addRow(zip_hint)
        layout.addWidget(zip_box)

        # PDF は ZIP / テキストと同じ「重いファイルを読むリーフ」だが上限が
        # 無く、しかも QPdfView はページを遅延レンダするので読んだバイト列は
        # 表示中ずっと常駐する。ZIP 上限の隣で可変にする。
        pdf_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_pdf_preview"))
        pdf_form = QFormLayout(pdf_box)
        self._pdf_size_limit = self._bind("pdf_preview_size_limit_mib", _count_spin(
            minimum=1, maximum=4096, step=10, suffix=" MiB",
        ))
        pdf_form.addRow(t("viewer.settings_dialog.pdf_read_max"), self._pdf_size_limit)
        pdf_hint = QLabel(t("viewer.settings_dialog.pdf_hint"))
        pdf_hint.setWordWrap(True)
        pdf_hint.setStyleSheet(hint_style())
        pdf_form.addRow(pdf_hint)
        layout.addWidget(pdf_box)

        image_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_image_preview"))
        image_form = QFormLayout(image_box)
        self._image_zoom_persist = self._bind("image_zoom_persist", QCheckBox(
            t("viewer.settings_dialog.image_zoom_persist_check")
        ))
        self._image_minimap = self._bind("image_minimap_enabled", QCheckBox(
            t("viewer.settings_dialog.image_minimap_check")
        ))
        image_form.addRow(
            t("viewer.settings_dialog.image_zoom_persist_label"),
            self._image_zoom_persist,
        )
        image_form.addRow(
            t("viewer.settings_dialog.image_minimap_label"), self._image_minimap
        )
        self._image_wheel_zoom = self._bind("image_wheel_zoom", QCheckBox(
            t("viewer.settings_dialog.image_wheel_zoom_check")
        ))
        image_form.addRow(
            t("viewer.settings_dialog.image_wheel_zoom_label"),
            self._image_wheel_zoom,
        )
        self._image_fit_no_upscale = self._bind("image_fit_no_upscale", QCheckBox(
            t("viewer.settings_dialog.image_fit_no_upscale_check")
        ))
        image_form.addRow(
            t("viewer.settings_dialog.image_fit_no_upscale_label"),
            self._image_fit_no_upscale,
        )
        self._slideshow_interval = self._bind("slideshow_interval_sec", _count_spin(
            minimum=1, maximum=600, step=1,
            suffix=t("viewer.settings_dialog.suffix_seconds"),
        ))
        self._slideshow_interval.setToolTip(
            t("viewer.settings_dialog.slideshow_interval_tooltip")
        )
        image_form.addRow(
            t("viewer.settings_dialog.slideshow_interval_label"),
            self._slideshow_interval,
        )
        self._chrome_hide_sec = self._bind(
            "lightbox_chrome_hide_ms", _seconds_spin(),
            # state はミリ秒、スピンは秒（小数 1 桁）— 相互写像。
            to_state=_seconds_spin_to_ms,
            from_state=lambda ms: ms / 1000.0,
        )
        self._chrome_hide_sec.setToolTip(
            t("viewer.settings_dialog.chrome_hide_tooltip")
        )
        image_form.addRow(
            t("viewer.settings_dialog.chrome_hide_label"),
            self._chrome_hide_sec,
        )
        layout.addWidget(image_box)

        media_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_media_preview"))
        media_form = QFormLayout(media_box)
        self._media_autoplay = self._bind("media_autoplay", QCheckBox(
            t("viewer.settings_dialog.media_autoplay_check")
        ))
        self._media_loop = self._bind("media_loop", QCheckBox(
            t("viewer.settings_dialog.media_loop_check")
        ))
        self._media_volume = self._bind("media_volume", _count_spin(
            minimum=0, maximum=100, step=5, suffix=" %",
        ))
        media_form.addRow(
            t("viewer.settings_dialog.media_autoplay_label"), self._media_autoplay
        )
        media_form.addRow(t("viewer.settings_dialog.media_loop_label"), self._media_loop)
        media_form.addRow(
            t("viewer.settings_dialog.media_volume_label"), self._media_volume
        )
        layout.addWidget(media_box)

        # タイル装飾設定（♡ とキャプション）は 1 グループにまとめる。state の
        # キー（show_post_favorites / caption_show_* / tile_name_placement）は
        # 別々のままで、UI の見せ方だけをまとめる。
        caption_box = _CollapsibleGroupBox(
            t("viewer.settings_dialog.group_tile_display_items")
        )
        caption_form = QFormLayout(caption_box)
        self._show_favorites = self._bind("show_post_favorites", QCheckBox(
            t("viewer.settings_dialog.show_favorites_check")
        ))
        caption_form.addRow(
            t("viewer.settings_dialog.favorites_label"), self._show_favorites
        )
        self._caption_show_posted = self._bind("caption_show_posted", QCheckBox(
            t("viewer.settings_dialog.caption_show_posted_check")
        ))
        self._caption_show_locked = self._bind("caption_show_locked", QCheckBox(
            t("viewer.settings_dialog.caption_show_locked_check")
        ))
        self._caption_show_size = self._bind("caption_show_size", QCheckBox(
            t("viewer.settings_dialog.caption_show_size_check")
        ))
        self._caption_show_plan = self._bind("caption_show_plan", QCheckBox(
            t("viewer.settings_dialog.caption_show_plan_check")
        ))
        caption_form.addRow(
            t("common.label.posted_colon"), self._caption_show_posted
        )
        caption_form.addRow(
            t("viewer.settings_dialog.caption_locked_label"), self._caption_show_locked
        )
        caption_form.addRow(
            t("viewer.settings_dialog.caption_size_label"), self._caption_show_size
        )
        caption_form.addRow(
            t("viewer.settings_dialog.caption_plan_label"), self._caption_show_plan
        )
        # タイル名の表示位置（オーナー要望 2026-07 — 視認性）: 画像に重ねる
        # （既定）/ 画像の下に帯を設けて外側に置く。post grid の中央グリッドに
        # 即時反映される（PostGrid.apply_settings）。
        self._tile_name_placement = QComboBox()
        self._tile_name_placement.addItem(
            t("viewer.settings_dialog.tile_name_overlay"), "overlay"
        )
        self._tile_name_placement.addItem(
            t("viewer.settings_dialog.tile_name_below"), "below"
        )
        self._bind("tile_name_placement", self._tile_name_placement)
        self._tile_name_placement.setToolTip(
            t("viewer.settings_dialog.tile_name_placement_tooltip")
        )
        caption_form.addRow(
            t("viewer.settings_dialog.tile_name_placement_label"),
            self._tile_name_placement,
        )
        layout.addWidget(caption_box)

        nav_box = _CollapsibleGroupBox(t("common.category.navigation"))
        nav_form = QFormLayout(nav_box)
        self._search_clear_on_nav = self._bind("search_clear_on_navigate", QCheckBox(
            t("viewer.settings_dialog.search_clear_on_nav_check")
        ))
        self._search_clear_on_nav.setToolTip(
            t("viewer.settings_dialog.search_clear_on_nav_tooltip")
        )
        nav_form.addRow(
            t("viewer.settings_dialog.search_clear_on_nav_label"),
            self._search_clear_on_nav,
        )
        self._restore_selection = self._bind(
            "restore_selection_on_startup",
            QCheckBox(t("viewer.settings_dialog.restore_selection_check")),
        )
        self._restore_selection.setToolTip(
            t("viewer.settings_dialog.restore_selection_tooltip")
        )
        nav_form.addRow(
            t("viewer.settings_dialog.restore_selection_label"),
            self._restore_selection,
        )
        # NOTE (分割ビュー再設計 2026-07): 旧「投稿を開いたとき最初に表示」
        # (post_open_initial) はクリック意味の一様化（フォルダ選択 = 常に
        # 代表画像プレビュー）で存在理由が消えたため廃止。
        layout.addWidget(nav_box)

        layout.addStretch(1)
        return w

    def _build_cache_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(t("viewer.settings_dialog.cache_intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        md_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_md_cache"))
        md_form = QFormLayout(md_box)
        self._md_max_mib = self._bind("markdown_cache_max_mib", _mib_spin())
        self._md_max_entries = self._bind(
            "markdown_cache_max_entries", _count_spin()
        )
        self._md_max_single = self._bind(
            "markdown_cache_max_single_mib", _mib_spin()
        )
        md_form.addRow(t("viewer.settings_dialog.cache_mem_limit"), self._md_max_mib)
        md_form.addRow(t("viewer.settings_dialog.cache_entries_limit"), self._md_max_entries)
        md_form.addRow(t("viewer.settings_dialog.cache_single_limit"), self._md_max_single)
        layout.addWidget(md_box)

        iv_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_iv_cache"))
        iv_form = QFormLayout(iv_box)
        self._iv_max_mib = self._bind("imageview_cache_max_mib", _mib_spin())
        self._iv_max_entries = self._bind(
            "imageview_cache_max_entries", _count_spin(maximum=256)
        )
        self._iv_max_single = self._bind(
            "imageview_cache_max_single_mib", _mib_spin()
        )
        # 束縛の順序に意味がある行: 件数上限を先に束縛しておくと、表を一括で
        # 書き戻す経路（既定値へ戻す）でも件数 → 先読み上限 → 先読み値の順に
        # 落ち着く（下の ``_sync_prefetch_max`` が件数の変更に追随するため）。
        self._iv_prefetch = self._bind("imageview_prefetch_radius", _count_spin(
            minimum=0, maximum=16,
            suffix=t("viewer.settings_dialog.suffix_sheets"),
        ))

        # 先読み k 番目のターゲットは「現在画像 + より近い k 件」
        # の挿入ガード付きで put されるため、件数上限 - 1 を超える先読みは
        # 構造上キャッシュに載らない（毎回フルデコード → 破棄の無駄 I/O に
        # なるだけ）。スピン上限を件数上限へ連動させ、無効な組み合わせを
        # UI から選べなくする（従来の上限 16 は維持）。上限を下げたとき
        # 現在値は QSpinBox が自動でクランプする。
        def _sync_prefetch_max(entries: int) -> None:
            self._iv_prefetch.setMaximum(max(0, min(16, entries - 1)))

        self._iv_max_entries.valueChanged.connect(_sync_prefetch_max)
        _sync_prefetch_max(self._iv_max_entries.value())
        iv_form.addRow(t("viewer.settings_dialog.cache_mem_limit"), self._iv_max_mib)
        iv_form.addRow(t("viewer.settings_dialog.cache_entries_limit"), self._iv_max_entries)
        iv_form.addRow(t("viewer.settings_dialog.cache_single_limit"), self._iv_max_single)
        iv_form.addRow(t("viewer.settings_dialog.iv_prefetch_label"), self._iv_prefetch)
        layout.addWidget(iv_box)

        # このヒントだけは 2 グループ（md / iv）に共通の説明なので、どちらか一方
        # の中へ入れると片方の説明が消える。タブ直下に残す（他のヒントは対応
        # するグループのスパン行にある）。
        hint = QLabel(t("viewer.settings_dialog.cache_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(hint_style())
        layout.addWidget(hint)

        disk_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_disk_cache"))
        disk_form = QFormLayout(disk_box)
        self._thumb_disk_enabled = self._bind("thumb_disk_cache_enabled", QCheckBox(
            t("viewer.settings_dialog.disk_cache_enable_check")
        ))
        self._thumb_disk_max = self._bind(
            "thumb_disk_cache_max_mib", _mib_spin(maximum=65536)
        )
        self._thumb_disk_edge = self._bind("thumb_disk_cache_max_edge", _count_spin(
            minimum=256, maximum=2048, step=128, suffix=" px",
        ))
        self._aspect_cache_max = self._bind(
            "aspect_cache_max_mib", _mib_spin(maximum=4096)
        )
        self._search_index_max = self._bind(
            "search_index_max_mib", _mib_spin(maximum=8192)
        )
        self._folder_preview_max = self._bind(
            "folder_preview_cache_max_mib", _mib_spin(maximum=1024)
        )
        # 反映タイミングは 3 種類あり、行注記が
        # そのまま実態でなければならない —
        #   * 有効/無効 … ディスクキャッシュは起動時に開くので**再起動**が要る
        #   * サムネ長辺 … ``main_window._apply_cache_settings`` が両ローダーへ
        #     即座に押し込む。既に保存済みのサムネは古い長辺のままなので、
        #     「再起動後に反映」は実態としても嘘だった（再起動しても直らない）
        #   * 容量上限 … ``set_max_bytes`` + ``prune()`` で即時
        restart_tip = t("viewer.settings_dialog.timing_restart").strip()
        self._thumb_disk_enabled.setToolTip(restart_tip)
        self._thumb_disk_edge.setToolTip(
            t("viewer.settings_dialog.timing_new_thumbs").strip()
        )
        disk_form.addRow(
            t("viewer.settings_dialog.disk_cache_label")
            + t("viewer.settings_dialog.timing_restart"),
            self._thumb_disk_enabled,
        )
        # 行ラベルの種別名は stat_*_name キー（表示名の唯一の情報源）
        # を埋め込む — 統計行・削除確認と同じ呼称になる。
        def _kind_max_label(name_key: str) -> str:
            return t("viewer.settings_dialog.disk_kind_max", name=t(name_key))

        disk_form.addRow(
            _kind_max_label("viewer.settings_dialog.stat_thumb_images_name"),
            self._thumb_disk_max,
        )
        disk_form.addRow(
            t("viewer.settings_dialog.disk_thumb_edge")
            + t("viewer.settings_dialog.timing_new_thumbs"),
            self._thumb_disk_edge,
        )
        disk_form.addRow(
            _kind_max_label("viewer.settings_dialog.stat_aspect_name"),
            self._aspect_cache_max,
        )
        disk_form.addRow(
            _kind_max_label("viewer.settings_dialog.stat_search_index_name"),
            self._search_index_max,
        )
        disk_form.addRow(
            _kind_max_label("viewer.settings_dialog.stat_folder_name"),
            self._folder_preview_max,
        )
        disk_hint = QLabel(t("viewer.settings_dialog.disk_hint"))
        disk_hint.setWordWrap(True)
        disk_hint.setStyleSheet(hint_style())
        disk_form.addRow(disk_hint)
        layout.addWidget(disk_box)

        layout.addWidget(self._build_cache_manage_box())
        # AI タグ情報グループは AI 機能パック（有償プラグイン）有効時のみ —
        # 素の配布では tags.db という概念自体を設定 UI に出さない。
        from . import ai_pack

        if ai_pack.available():
            layout.addWidget(self._build_tag_info_box())

        layout.addStretch(1)
        return w

    def _build_tag_info_box(self) -> QGroupBox:
        box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_ai_tags"))
        v = QVBoxLayout(box)
        label = QLabel("—")
        label.setWordWrap(True)
        v.addWidget(label)
        hint = QLabel(t("viewer.settings_dialog.tag_info_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet(hint_style())
        v.addWidget(hint)

        stats = None
        if self._cache_controller is not None and hasattr(
            self._cache_controller, "tag_index_stats"
        ):
            try:
                stats = self._cache_controller.tag_index_stats()
            except Exception:  # pragma: no cover (defensive)
                stats = None
        if stats is None:
            label.setText(t("viewer.settings_dialog.tag_db_not_loaded"))
        else:
            mib = stats.get("db_bytes", 0) / (1024 * 1024)
            label.setText(
                t(
                    "viewer.settings_dialog.tag_stats",
                    model=stats.get("model") or "—",
                    floor=stats.get("floor", 0),
                    images=stats.get("images", 0),
                    tags=stats.get("distinct_tags", 0),
                    mib=mib,
                )
            )
        return box

    def _build_cache_manage_box(self) -> QGroupBox:
        box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_cache_manage"))
        v = QVBoxLayout(box)

        # 「無効」の 3 義（設定でオフ / 開けなかった / トグルが UI に無い）を
        # 言い分ける。開けなかった理由は
        # ログにしか無いので、その場からログフォルダへ行ける導線を並べる。
        stats_row = QHBoxLayout()
        self._cache_stats_label = QLabel("—")
        self._cache_stats_label.setWordWrap(True)
        stats_row.addWidget(self._cache_stats_label, 1)
        open_logs_btn = QPushButton(t("viewer.main_window.open_logs_folder"))
        open_logs_btn.clicked.connect(self._on_open_logs_folder)
        stats_row.addWidget(open_logs_btn)
        v.addLayout(stats_row)

        # I04: show WHERE the caches live (portable data/ next to the EXE) and a
        # one-click reveal — reassuring for a portable product and handy for
        # manual cleanup / backup.
        data_dir = get_paths().data
        path_row = QHBoxLayout()
        path_label = QLabel(
            t("viewer.settings_dialog.data_location", path=str(data_dir))
        )
        path_label.setWordWrap(True)
        path_label.setStyleSheet(hint_style())
        path_row.addWidget(path_label, 1)
        open_data_btn = QPushButton(t("viewer.settings_dialog.open_data_folder"))
        open_data_btn.clicked.connect(self._on_open_data_folder)
        path_row.addWidget(open_data_btn)
        v.addLayout(path_row)

        self._cache_build_bg = self._bind("cache_build_background", QCheckBox(
            t("viewer.settings_dialog.cache_build_bg_check")
        ))
        self._cache_build_bg.setToolTip(
            t("viewer.settings_dialog.cache_build_bg_tooltip")
        )
        v.addWidget(self._cache_build_bg)

        btn_row = QHBoxLayout()
        # 診断メニューと同じ 1 キーで名乗る。
        self._build_cache_btn = QPushButton(t("viewer.common.cache_prebuild"))
        self._build_cache_btn.setToolTip(
            t("viewer.settings_dialog.build_cache_tooltip")
        )
        self._build_cache_btn.clicked.connect(self._on_build_cache)
        btn_row.addWidget(self._build_cache_btn)

        self._clear_cache_btn = QPushButton(t("viewer.settings_dialog.clear_cache"))
        self._clear_cache_btn.setToolTip(
            t("viewer.settings_dialog.clear_cache_tooltip")
        )
        self._clear_cache_btn.clicked.connect(lambda: self._on_clear_cache(None))
        btn_row.addWidget(self._clear_cache_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)

        # I04: type-split clearing — free just the thumbnail disk space, or drop
        # only the (cheap-to-rebuild-but-slow) search index, without the
        # all-or-nothing wipe taking the other caches down too.
        # ボタンの種別名も stat_*_name キーを埋め込む（統計行・削除
        # 確認・行ラベルと同じ呼称）。
        #
        # 種別ごとの専用ボタンを並べると ``_CLEAR_KIND_META`` の種別と
        # 食い違いうる（新しい種別を足してもボタンが無い）ので、
        # **コンボ +「選択した種類を削除」**で台帳を回すだけにする（新種別を
        # 足しても片側欠落が構造的に起きない）。
        split_row = QHBoxLayout()
        self._clear_kind_combo = QComboBox()
        for kind in self._CLEAR_ORDER:
            self._clear_kind_combo.addItem(
                t(self._CLEAR_KIND_META[kind][3]), kind
            )
        split_row.addWidget(self._clear_kind_combo)
        self._clear_kind_btn = QPushButton(
            t("viewer.settings_dialog.clear_selected_kind")
        )
        self._clear_kind_btn.setToolTip(
            t("viewer.settings_dialog.clear_selected_kind_tooltip")
        )
        self._clear_kind_btn.clicked.connect(self._on_clear_selected_kind)
        split_row.addWidget(self._clear_kind_btn)
        split_row.addStretch(1)
        v.addLayout(split_row)

        self._clear_buttons = (self._clear_cache_btn, self._clear_kind_btn)
        if self._cache_controller is None:
            self._build_cache_btn.setEnabled(False)
            for btn in self._clear_buttons:
                btn.setEnabled(False)
            self._cache_stats_label.setText(
                t("viewer.settings_dialog.cache_manage_viewer_only")
            )
        else:
            self._refresh_cache_stats()
            self._refresh_cache_build_state()
            # 上の状態はダイアログ構築時の 1 回きりなので、開いたまま背景
            # ビルドが終わっても 3 ボタンが無効のまま残らないよう、1 秒ごとに
            # 同じ関数を回して両方向へ追随させる。``cache_build_in_progress``
            # を呼ぶだけ（I/O 無し・例外は _cache_build_running が吸う）。
            self._cache_status_timer.setInterval(1000)
            self._cache_status_timer.timeout.connect(self._refresh_cache_build_state)
            self._cache_status_timer.start()
        return box

    def _refresh_cache_build_state(self) -> None:
        """背景ビルドの在/不在に合わせて 3 ボタンの活性とラベルを合わせる。

        両方向 — ビルド中は無効 + 「作成中…」、終われば元のラベルで再び有効。
        コントローラ不在の枝は構築時に確定するのでここでは扱わない。
        """
        if self._cache_controller is None:
            return
        running = self._cache_build_running()
        self._build_cache_btn.setEnabled(not running)
        for btn in self._clear_buttons:
            btn.setEnabled(not running)
        self._build_cache_btn.setText(
            t("viewer.settings_dialog.cache_building_bg")
            if running
            else t("viewer.common.cache_prebuild")
        )

    def done(self, r: int) -> None:
        """自走タイマーを 1 つ残らず止めてから閉じる（close 時の規約）。

        ``accept`` / ``reject`` / Esc / × の全経路が ``QDialog.done`` を通るので、
        停止はここ 1 箇所で足りる（``stop`` は冪等）。
        """
        self._cache_status_timer.stop()
        super().done(r)

    def _on_open_data_folder(self) -> None:
        """「データフォルダを開く」 — reveal the portable data/ dir (I04)."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        data_dir = get_paths().data
        data_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir)))

    def _on_open_logs_folder(self) -> None:
        """統計行の隣の「ログフォルダを開く」.

        「開けませんでした（ログを参照）」の行き先をその場に置く。パスは
        ポータビリティ規約どおり ``get_paths()`` 経由（``Path.home()`` /
        ``%APPDATA%`` は使わない）。失敗は窓側と同じ 「開く」失敗ファネルへ。
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        logs = get_paths().logs
        logs.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs))):
            notify_failure(self, t("viewer.main_window.open_logs_failed"))

    def _cache_build_running(self) -> bool:
        ctrl = self._cache_controller
        if ctrl is None or not hasattr(ctrl, "cache_build_in_progress"):
            return False
        try:
            return bool(ctrl.cache_build_in_progress())
        except Exception:  # pragma: no cover (defensive)
            return False

    def _refresh_cache_stats(self) -> None:
        if self._cache_controller is None:
            return
        try:
            s = self._cache_controller.cache_stats()
        except Exception:  # pragma: no cover (defensive)
            self._cache_stats_label.setText(
                t("viewer.settings_dialog.cache_stats_unavailable")
            )
            return
        # キャッシュ種別の表示名は _CLEAR_KIND_META の *_name キーが
        # 唯一の情報源 — 統計行・削除確認・完了トーストが構造的に同じ名前を
        # 使う（種別別の複合テンプレートへは焼き込まない）。
        parts: list[str] = []
        for kind in self._CLEAR_ORDER:
            bkey, ckey, ekey, lkey = self._CLEAR_KIND_META[kind]
            # Folder-preview / search-index stats are optional (older
            # controllers / test doubles omit the keys) — only show the
            # segment when present.
            if kind in ("folder", "search") and ekey not in s:
                continue
            name = t(lkey)
            if s.get(ekey):
                parts.append(
                    t(
                        "viewer.settings_dialog.stat_item",
                        name=name,
                        size=_fmt_bytes(s.get(bkey, 0)),
                        count=s.get(ckey, 0),
                    )
                )
                continue
            # 「無効」は 2 義 — 設定でオフなのか、開こうとして
            # 失敗したのか（``_open_cache`` は best-effort で ``None`` を返す）。
            # 意図は ``<kind>_configured`` がコントローラから来る。キーを
            # 持たない古いコントローラ / テストダブルは従来表示へ落とす
            # （上の「省略キーは出さない」と同じ作法）。
            ckey_cfg = f"{ekey.removesuffix('_enabled')}_configured"
            if ckey_cfg not in s:
                parts.append(
                    t("viewer.settings_dialog.stat_item_disabled", name=name)
                )
            elif s.get(ckey_cfg):
                parts.append(
                    t("viewer.settings_dialog.stat_item_failed", name=name)
                )
            else:
                parts.append(t("viewer.settings_dialog.stat_item_off", name=name))
        self._cache_stats_label.setText(
            t("viewer.settings_dialog.cache_stats_prefix")
            + t("common.sep.middot").join(parts)
        )

    def _on_clear_selected_kind(self) -> None:
        """コンボで選んだ 1 種別だけを削除する.

        ``_on_clear_cache`` は元から集合を受ける設計なので、呼び出し側は
        「選択された 1 つ」を包むだけでよい。
        """
        kind = self._clear_kind_combo.currentData()
        if kind:
            self._on_clear_cache({str(kind)})

    #: Cache kinds + their cache_stats keys and display-name i18n key (I04):
    #: (bytes_key, count_key, enabled_key, label_key).  Insertion order is the
    #: display order — :data:`_CLEAR_ORDER` is derived from it so a new kind
    #: only has to be added here (a separately maintained order tuple could
    #: be forgotten, leaving the new kind invisible in the breakdown).
    #: Iterated for the stats line, the confirm breakdown and the success
    #: message — the *_name key is the ONE definition of each cache's display
    #: name.
    _CLEAR_KIND_META = {
        "thumb": ("disk_bytes", "disk_count", "disk_enabled",
                  "viewer.settings_dialog.stat_thumb_images_name"),
        "aspect": ("aspect_bytes", "aspect_count", "aspect_enabled",
                   "viewer.settings_dialog.stat_aspect_name"),
        "folder": ("folder_bytes", "folder_count", "folder_enabled",
                   "viewer.settings_dialog.stat_folder_name"),
        "search": ("search_bytes", "search_count", "search_enabled",
                   "viewer.settings_dialog.stat_search_index_name"),
    }

    #: Display order for the confirm breakdown / success message.
    _CLEAR_ORDER = tuple(_CLEAR_KIND_META)

    def _on_clear_cache(self, kinds: set[str] | None) -> None:
        """Confirm (with a per-type breakdown) then clear the selected caches.

        ``kinds`` is a subset of :data:`_CLEAR_ORDER`, or ``None`` for all.  The
        confirm lists every targeted cache's current size + count (I04) so the
        user sees exactly what disappears — including the folder-preview cache
        and, when thumbnails are cleared, the in-memory thumbnails.  Success is
        a non-modal toast (design principle 1: 成功=非モーダル).

        「すべて」は :meth:`CacheController.clear_persistent_caches` へ ``None``
        のまま渡す — 全種別の定義はコントローラ側
        （``CacheBuildController.CLEAR_KINDS``）にしか無く、ここで
        ``set(_CLEAR_ORDER)`` を組み直すと種別追加時に片方だけ古いまま「すべて
        削除」が一部を消さなくなる。この表示リストの取りこぼしは parity テスト
        （tests/test_viewer_settings_dialog.py）が検出する。
        """
        if self._cache_controller is None:
            return
        selected = set(self._CLEAR_ORDER) if kinds is None else set(kinds)
        try:
            stats = self._cache_controller.cache_stats()
        except Exception:  # pragma: no cover (defensive)
            stats = {}
        lines = []
        names = []
        for kind in self._CLEAR_ORDER:
            if kind not in selected:
                continue
            bkey, ckey, ekey, lkey = self._CLEAR_KIND_META[kind]
            name = t(lkey)
            names.append(name)
            if not stats.get(ekey, False):
                lines.append(
                    t("viewer.settings_dialog.clear_item_disabled", name=name)
                )
                continue
            lines.append(
                t(
                    "viewer.settings_dialog.clear_item",
                    name=name,
                    size=_fmt_bytes(stats.get(bkey, 0)),
                    count=stats.get(ckey, 0),
                )
            )
        detail = "\n".join(lines) if lines else "—"
        if "thumb" in selected:
            # Flag the side-effect: the in-memory thumbnail LRUs are flushed
            # too when thumbnails are cleared.
            detail += "\n" + t("viewer.settings_dialog.clear_mem_note")
        # 動詞ラベル: 「はい」ではキャッシュ削除に同意したのか
        # ダイアログ全体を閉じることに同意したのか区別が付かない。
        if not confirm_action(
            self,
            title=t("viewer.settings_dialog.clear_cache_title"),
            body=t("viewer.settings_dialog.clear_cache_confirm", detail=detail),
            accept_text=t("common.action.delete_confirm"),
            icon=QMessageBox.Icon.Warning,
            destructive=True,
        ):
            return
        # 「すべて」は None のまま委譲する（docstring 参照）。
        self._cache_controller.clear_persistent_caches(
            None if kinds is None else selected
        )
        self._refresh_cache_stats()
        if kinds is None:
            msg = t("viewer.settings_dialog.cache_cleared")
        else:
            msg = t(
                "viewer.settings_dialog.cache_cleared_kinds",
                names=t("common.sep.comma").join(names),
            )
        show_toast(self, msg, kind="success")

    def _confirm_background_commit(self) -> bool:
        """予告: a background build closes this dialog and commits it.

        The dialog's contract is 「「OK」を押すと適用されます」 — the 「既定値に
        戻す」 toast says so on screen — yet the background path has to
        ``accept()`` (see :meth:`_on_build_cache`), which writes **every** tab's
        edits, theme included.  It cannot simply stay open instead: it is
        application-modal (``main_window`` opens it with ``exec()``), so the
        checkbox's own promise 「閲覧を続けながら作成」 would be false.  So the
        contract break stays, but stops being invisible: this names the
        consequence before anything starts, and 「キャンセル」 leaves both the
        settings and the caches untouched.
        """
        return confirm_action(
            self,
            title=t("viewer.cache_build_controller.build_title"),
            body=t("viewer.settings_dialog.cache_build_bg_commit_body"),
            informative=t(
                "viewer.settings_dialog.cache_build_bg_commit_informative"
            ),
            accept_text=t("viewer.settings_dialog.cache_build_bg_commit_accept"),
        )

    def _cache_build_inputs(self) -> dict[str, object]:
        """ビルドが読む設定の**いまのウィジェット値**（フィールド名 → 値）.

        コントローラは ``ViewerState`` から直接読む（モード / サムネ長辺 /
        アスペクト予算 / 各キャッシュの有効・容量）ので、まだ ``accept`` して
        いないタブの編集はそのままでは届かない。届かせないと、長辺を変えた
        直後に押したビルドが**古い長辺で**ライブラリ全体のマスターを作り、
        その後の ``accept`` が新しい値を保存して「設定値と実物が食い違う」
        状態がセッションを越えて残る（行注記「以後に作成されるサムネから
        反映」の逆）。

        ここは ``accept`` の書き戻しの**部分集合**であることが要件で、単独で
        ``state`` を触るのはこのダイアログでここだけ。だから値の読み方は
        accept と同じ束縛表から取り、ここが持つのは「どのフィールドが対象か」
        （:data:`_CACHE_BUILD_FIELDS`）だけにする。
        """
        return {
            spec.field: spec.read()
            for spec in self._fields
            if spec.field in _CACHE_BUILD_FIELDS
        }

    def _on_build_cache(self) -> None:
        if self._cache_controller is None:
            return
        # Honour the cache tab's edits immediately (the user may not have hit
        # OK yet).  This mutates ``state`` before accept(), breaking the
        # dialog's commit-on-accept contract ("Rejecting leaves state
        # untouched"), so remember the pre-build values and roll them back in
        # ``reject`` — ``accept`` writes all of them anyway.
        inputs = self._cache_build_inputs()
        if not self._build_touched_state:
            self._pre_build_state = {
                field: getattr(self._state, field) for field in inputs
            }
            self._build_touched_state = True
        for field, value in inputs.items():
            setattr(self._state, field, value)
        # ``notify=False``: モーダル経路の通知はこのダイアログが自分で出す。
        # コントローラのトーストはホスト窓へ
        # 親付けされるため、``exec()`` のアプリケーションモーダルなこの
        # ダイアログの**裏**に隠れてしまう。
        result = self._cache_controller.build_cache_interactive(
            self,
            confirm_background=self._confirm_background_commit,
            notify=False,
        )
        if result is None:
            return
        # モーダル prompt でその場に選ばれたモードを、accept が書き戻す
        # チェックボックスへ反映する。これが無いと、prompt で背景を選んで
        # 走らせても ``accept`` (:_write_state) はダイアログ側の未更新値
        # （＝ OFF）を保存し、設定が実際に走ったモードと逆になる。
        # ただし逆方向（背景 ON の設定のまま prompt で「今回は前景」を選ぶ）
        # は「今回だけ」の選択なので書き戻さない — 書き戻すと [OK] で永続設定が
        # 反転する。背景 ON はコントローラが
        # confirm_background で永続化の可否を確認したうえで返している。
        if result.get("background"):
            self._cache_build_bg.setChecked(True)
        # Background builds return a sentinel and keep running after the dialog
        # closes — progress + completion are shown in the viewer's status bar,
        # not a modal summary here.  Close the dialog so the user can watch it.
        if result.get("background"):
            self.accept()
            return
        self._refresh_cache_stats()
        self._refresh_cache_build_state()
        ok = result.get("ok", 0)
        failed = result.get("failed", 0)
        total = result.get("total", 0)
        if result.get("cancelled"):
            # 中断を「完了しました」と言わない（背景経路と同じ文言）。
            show_toast(
                self,
                t(
                    "viewer.cache_build_controller.build_cancelled_toast",
                    done=ok + failed,
                ),
                kind="info",
            )
        elif failed > 0:
            # Partial failure stays modal (design principle 2: 失敗=モーダル) so
            # the failed count is acknowledged, not lost.
            QMessageBox.warning(
                self, t("viewer.settings_dialog.build_cache_done_title"),
                t(
                    "viewer.settings_dialog.build_cache_done_body",
                    ok=ok, failed=failed, total=total,
                ),
            )
        else:
            # Clean success is non-modal (principle 1) to match the background
            # path's toast.
            show_toast(
                self,
                t(
                    "viewer.cache_build_controller.build_done_toast",
                    ok=ok, total=total,
                ),
                kind="success",
            )

    def _build_performance_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        intro = QLabel(t("viewer.settings_dialog.perf_intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Tier 1: preset selector.  A general user picks one of these and
        # never has to parse SMB-credit copy; the individual knobs collapse
        # behind 「詳細設定を表示」 below.
        preset_box = QGroupBox(t("viewer.settings_dialog.group_preset"))
        preset_v = QVBoxLayout(preset_box)
        preset_row = QHBoxLayout()
        self._preset_standard = QRadioButton(t("viewer.settings_dialog.preset_standard"))
        self._preset_standard.setToolTip(
            t("viewer.settings_dialog.preset_standard_tooltip")
        )
        self._preset_nas = QRadioButton(t("viewer.settings_dialog.preset_nas"))
        self._preset_nas.setToolTip(
            t("viewer.settings_dialog.preset_nas_tooltip")
        )
        self._preset_custom = QRadioButton(t("viewer.settings_dialog.preset_custom"))
        self._preset_custom.setToolTip(
            t("viewer.settings_dialog.preset_custom_tooltip")
        )
        for rb in (self._preset_standard, self._preset_nas,
                   self._preset_custom):
            preset_row.addWidget(rb)
        preset_row.addStretch(1)
        preset_v.addLayout(preset_row)
        preset_hint = QLabel(t("viewer.settings_dialog.preset_hint"))
        preset_hint.setWordWrap(True)
        preset_hint.setStyleSheet(hint_style())
        preset_v.addWidget(preset_hint)
        layout.addWidget(preset_box)

        self._show_advanced_check = QCheckBox(t("viewer.settings_dialog.show_advanced_check"))
        layout.addWidget(self._show_advanced_check)

        # --- Tier 2: the individual knobs (unchanged persisted fields).
        self._perf_box = _CollapsibleGroupBox(t("viewer.settings_dialog.group_io_parallelism"))
        form = QFormLayout(self._perf_box)
        self._scan_parallelism = self._bind("scan_metadata_parallelism", _count_spin(
            minimum=1, maximum=32,
        ))
        self._thumb_cache_size = self._bind("thumbnail_cache_size", _count_spin(
            minimum=16, maximum=4096, step=16,
            suffix=t("viewer.settings_dialog.suffix_sheets"),
        ))
        self._thumb_max_threads = self._bind("thumbnail_max_threads", _count_spin(
            minimum=1, maximum=32,
        ))
        self._aspect_probe_parallelism = self._bind(
            "aspect_probe_parallelism", _count_spin(minimum=1, maximum=16)
        )
        # I03: this knob is the one perf value NOT applied live — post_grid picks
        # it up on the next folder switch (see perf_hint) — so annotate the row.
        self._scan_parallelism.setToolTip(
            t("viewer.settings_dialog.timing_next_folder").strip()
        )
        form.addRow(
            t("viewer.settings_dialog.postmd_concurrency")
            + t("viewer.settings_dialog.timing_next_folder"),
            self._scan_parallelism,
        )
        form.addRow(t("viewer.settings_dialog.thumb_cache_max"), self._thumb_cache_size)
        form.addRow(
            t("viewer.settings_dialog.thumb_decode_parallelism"), self._thumb_max_threads
        )
        form.addRow(
            t("viewer.settings_dialog.aspect_probe_parallelism"),
            self._aspect_probe_parallelism,
        )
        # 各つまみの反映タイミングを説明するヒントはグループの中
        # （スパン行）へ。畳んだとき / 詳細チェックで _perf_box ごと隠したとき
        # に説明文だけが残らない。
        self._perf_hint = QLabel(t("viewer.settings_dialog.perf_hint"))
        self._perf_hint.setWordWrap(True)
        self._perf_hint.setStyleSheet(hint_style())
        form.addRow(self._perf_hint)
        layout.addWidget(self._perf_box)
        layout.addStretch(1)

        # Wiring.  ``_applying_preset`` (initialised in ``__init__``) suppresses
        # the spin-edit → カスタム flip while a preset writes its value set.
        # Initial radio: the persisted memo, sanity-checked against the actual
        # values (hand-edited state / an older version wins over the memo).
        current = {f: getattr(self._state, f) for f in _PERF_FIELDS}
        detected = match_perf_preset(current)
        initial = self._state.perf_preset
        if initial not in ("custom", *PERF_PRESETS):
            initial = detected
        if initial != "custom" and detected != initial:
            initial = detected
        self._select_preset_radio(initial)
        self._sync_advanced_visibility(initial)
        self._preset_standard.toggled.connect(
            lambda on: on and self._on_preset_chosen("standard")
        )
        self._preset_nas.toggled.connect(
            lambda on: on and self._on_preset_chosen("nas")
        )
        self._preset_custom.toggled.connect(
            lambda on: on and self._on_preset_chosen("custom")
        )
        for spin in (self._scan_parallelism, self._thumb_cache_size,
                     self._thumb_max_threads, self._aspect_probe_parallelism):
            spin.valueChanged.connect(self._on_perf_value_edited)
        self._show_advanced_check.toggled.connect(self._perf_box.setVisible)
        return w

    # ------------------------------------------------- performance presets

    def _preset_radios(self) -> dict[str, QRadioButton]:
        return {
            "standard": self._preset_standard,
            "nas": self._preset_nas,
            "custom": self._preset_custom,
        }

    def current_perf_preset(self) -> str:
        for key, rb in self._preset_radios().items():
            if rb.isChecked():
                return key
        return "custom"

    def _select_preset_radio(self, key: str) -> None:
        rb = self._preset_radios().get(key, self._preset_custom)
        rb.blockSignals(True)
        rb.setChecked(True)
        rb.blockSignals(False)

    def _sync_advanced_visibility(self, preset: str) -> None:
        """カスタム → auto-expand the knobs; presets start collapsed."""
        show = preset == "custom"
        self._show_advanced_check.blockSignals(True)
        self._show_advanced_check.setChecked(show)
        self._show_advanced_check.blockSignals(False)
        self._perf_box.setVisible(show)

    def _on_preset_chosen(self, key: str) -> None:
        if key == "custom":
            # No values to write — just reveal the knobs for hand-tuning.
            self._sync_advanced_visibility("custom")
            return
        values = PERF_PRESETS[key]
        spins = {
            "scan_metadata_parallelism": self._scan_parallelism,
            "thumbnail_cache_size": self._thumb_cache_size,
            "thumbnail_max_threads": self._thumb_max_threads,
            "aspect_probe_parallelism": self._aspect_probe_parallelism,
        }
        self._applying_preset = True
        try:
            for field, spin in spins.items():
                spin.setValue(values[field])
        finally:
            self._applying_preset = False

    def _on_perf_value_edited(self, _value: int) -> None:
        """Hand-editing any knob demotes the shown preset to カスタム.

        The knobs stay visible (the user is editing them); only the radio
        flips, so the state saved on OK reflects what's actually configured.
        """
        if self._applying_preset:
            return
        if not self._preset_custom.isChecked():
            self._select_preset_radio("custom")

    # --------------------------------------------------------------- actions

    def _confirm_restore_defaults(self) -> None:
        """Confirm (default No) before wiping all 3 tabs, then toast.

        This button rewrites every field across all three tabs (including the
        theme), so like the cache-management group's delete actions in the
        same dialog it gets a confirm + toast instead of acting silently.

        The toast is **info, not success**: ``_restore_defaults`` only rewrites
        the dialog's own widgets — nothing reaches ``ViewerState`` until
        ``accept``. A 成功 toast here was a lie for anyone who then closed with
        キャンセル / ✕ (nothing had changed), so it says what actually happened
        and what still has to be pressed.
        """
        if not confirm_action(
            self,
            title=t("common.action.restore_defaults"),
            body=t("viewer.settings_dialog.restore_defaults_confirm_body"),
            # 動詞ラベル — ボタンが自分の行為を名乗る。
            accept_text=t("common.action.restore_defaults"),
        ):
            return
        self._restore_defaults()
        show_toast(
            self, t("viewer.settings_dialog.restore_defaults_pending_toast"),
            kind="info",
        )

    def _restore_defaults(self) -> None:
        """束縛表を ``ViewerState`` のクラス既定値で埋め直す（ダイアログ内だけ）.

        表を回すので「既定値に戻す」から 1 行落ちることが構造的に起きない。
        表に載らないのはパフォーマンスプリセットのラジオだけで、既定値の集合は
        標準プリセットの値集合と同値（``tests/test_viewer_settings_dialog.py``
        が機械検証）。表がスピンへ書く間は「手編集 → カスタム」への降格を
        プリセット適用と同じ抑止で止め、そのあとラジオと詳細表示を合わせる。
        """
        defaults = ViewerState()
        self._applying_preset = True
        try:
            for spec in self._fields:
                spec.write(getattr(defaults, spec.field))
        finally:
            self._applying_preset = False
        self._select_preset_radio("standard")
        self._sync_advanced_visibility("standard")

    def accept(self) -> None:  # noqa: D401 (Qt API)
        """束縛表をそのまま ``ViewerState`` へ書き戻して閉じる.

        呼び出し元の ``ViewerWindow._apply_settings_live`` が新しい値を読む。
        """
        for spec in self._fields:
            setattr(self._state, spec.field, spec.read())
        # 表の外: 3 つのラジオから導出するメモ（対応する入力ウィジェットが
        # 1 対 1 で存在しないので束縛できない）。
        self._state.perf_preset = self.current_perf_preset()
        super().accept()

    def reject(self) -> None:  # noqa: D401 (Qt API)
        # Restore the pre-build values if a "キャッシュ作成…" run (modal path)
        # pushed the cache tab's edits into ``state`` while the dialog stayed
        # open.  Every other field is only written in ``accept``, so cancelling
        # must leave state exactly as the caller passed it in.
        if self._build_touched_state:
            for field, value in self._pre_build_state.items():
                setattr(self._state, field, value)
        super().reject()
