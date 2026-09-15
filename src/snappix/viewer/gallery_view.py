"""Custom-painted gallery view (``QAbstractScrollArea``) — the Qt shell.

Replaces the ``QListWidget`` that the viewer's children panes used to wrap.
A custom scroll area + ``paintEvent`` is the only way to render a true
justified (Eagle-style) layout — ``QListView``'s flow engine can't justify
rows or place non-uniform cells — and it also gives pixel-perfect control
of thumbnail blitting (no ``_pad_to_square`` compositing, no letterboxing
in justified mode).

The view is deliberately *dumb about data*: the host builds :class:`Tile`
objects (caption, aspect, pixmap, the opaque ``FolderEntry``) through the
shared mapping :func:`children_grid.build_tile` and drives all scanning /
thumbnail / aspect loading.  The view only lays out, paints, hit-tests, and
emits index-based signals.

Dumb about data is NOT the same as「ホストが後から配線する前提の生部品」:
what the view needs from its host in order to decide whether a *gesture* is
live — the Ctrl+wheel zoom receiver — is taken at construction time
(``zoom_handler``), not as a signal somebody may or may not connect.  A
signal cannot tell the view whether anyone is listening, so a seat that
forgot to connect got a silently-consumed, dead gesture.  See
:meth:`GalleryView.__init__`.

**この殻が持つもの**は「Qt のイベントを受け、意図を得て、自分の状態を変え、
1 フレームぶんの描画環境を組んで描画を頼む」だけ。中身は 3 つの層に分かれる:

* 幾何 — :mod:`justified_layout`（Qt 非依存の純レイアウト）。
* 描画 — :mod:`gallery_view_parts.painter` / :mod:`~gallery_view_parts.captions`
  / :mod:`~gallery_view_parts.empty_card`（``QPainter`` と値だけを受ける）。
* 入力 — :mod:`gallery_view_parts.input`（イベント → 意図）と
  :mod:`gallery_view_parts.selection`（単一選択モデル）。

従来の公開名（``Tile`` / ``IconSeats`` / ``file_icon_bucket``）はここから
re-export するので、ホストとテストの import 元は変わらない。

Invariants preserved from the old ``children_grid``:

* Only viewport-visible tiles are painted (visible-range is computed from
  the layout, far more precisely than the old scrollbar math).
* The host still drives viewport-bounded thumbnail / aspect requests off
  ``visible_range_changed`` — the view never eagerly loads.
* Single-selection, click = select, double-click = activate, arrow keys
  move the cursor (``ShortcutOverride`` accepted so the window's ←/→
  shortcut doesn't steal them while this pane is focused — matching
  ``QAbstractItemView``).
* ``QPixmap`` only ever touched on the GUI thread (the loader hands back
  ``QImage``; the host converts and calls :meth:`set_thumb`).
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Sequence
from math import ceil
from pathlib import Path
from types import MethodType

from loguru import logger
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QContextMenuEvent,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QMenu,
    QPushButton,
    QStyle,
    QToolTip,
    QWidget,
)

from ..common.touch import enable_touch_scroll
from ..common.ui.timers import DebounceMode, Debouncer
from . import view_prefs
from ._indicator import badge_font, badge_tooltip
from .drag_export import start_file_export_drag
from .gallery_view_parts import captions, empty_card
from .gallery_view_parts import input as gvinput
from .gallery_view_parts import painter as gvpaint
from .gallery_view_parts import placeholders
from .gallery_view_parts.selection import SelectionState
from .gallery_view_parts.tiles import (
    IconSeats,
    Tile,
    file_icon_bucket,
    is_image_tile,
    tile_awaits_thumb,
)
from .justified_layout import (
    LayoutParams,
    LayoutResult,
    LayoutStrategy,
    SquareGrid,
    TileInput,
    hit_test,
    make_strategy,
    nearest_in_adjacent_row,
    visible_range,
)
from .perf import measure


def _weak_host_callable(
    handler: Callable[[int], None] | None,
) -> Callable[[int], None] | None:
    """ホストの束縛メソッドを**弱参照**で持ち直す（子 → 親の閉路を作らない）。

    :class:`GalleryView` はホストの子ウィジェットなので、ホストの束縛メソッドを
    そのまま属性に置くと 子 → 親 の Python 参照ができ、Qt の親 → 子（PySide が
    張る）と合わせて参照の閉路になる。閉路は Python の GC が解くしかなく、
    GC は 2 つの**ラッパー**を任意の順で落としに行くので、C++ の親子関係と
    食い違って落ちる — 破棄をまとめて後で行う面（テストハーネスの一括破棄）で
    access violation として実測した。

    弱参照にしておけば閉路にならず、ホストが先に消えたあとの呼び出しは黙って
    捨てられる（受け手がもう居ないのだから、それが正しい）。束縛メソッド以外
    （自由関数・ラムダ）はそのまま持つ — ホストを捕捉したクロージャを渡すと
    同じ閉路が戻るので、ホストは束縛メソッドを渡すこと。
    """
    if handler is None or not isinstance(handler, MethodType):
        return handler
    ref = weakref.WeakMethod(handler)

    def call(steps: int) -> None:
        live = ref()
        if live is not None:
            live(steps)

    return call


class GalleryView(QAbstractScrollArea):
    selection_changed = Signal(int)        # selected tile index, -1 = none
    item_activated = Signal(int)           # double-clicked / Enter index
    context_menu_requested = Signal(int, QPoint)  # (index|-1, global pos)
    visible_range_changed = Signal()       # scroll / relayout — host re-requests
    #: A **deferred** relayout (60ms コアレサ発火 / ``flush_pending_relayout``)
    #: が完了し、スクロールレンジが現在のビューポートジオメトリを反映した。
    #: ``set_tiles`` / ``clear`` の同期リレイアウトでは発火しない — ホストの
    #: 収束適用（``ChildrenGrid._apply_pending_scroll``、issue #99）が
    #: ``_resolve_pending_select`` より前に保留値を消費してしまわないため。
    relayout_converged = Signal()
    # Keyboard-only navigation completion (host wires it where meaningful):
    # Backspace requests moving to the parent folder.  Only the left pane
    # connects it (the right pane leaves it unhandled), so the view stays
    # pane-agnostic.
    go_up_requested = Signal()
    # Request to clear any active filter / search.  NOT emitted by this
    # view's own key handling any more (#115): Escape is consumed by the
    # window-level single QShortcut (main_window ``_sc_escape`` →
    # ``_on_escape`` → ``PostGrid._on_escape_clear``, UIレビュー 07-25 #20)
    # before it could reach ``keyPressEvent``, so the old Escape branch here
    # was dead code.  The signal itself stays as a host-level hook (the left
    # pane keeps it wired; non-keyboard emitters remain possible).
    clear_filter_requested = Signal()
    # A number key 0–5 was pressed while a tile is selected — the host sets
    # (0 = clear) / (1–5 = set) the user star on the current entry.  Only the
    # left pane connects it; the digit keys never reach here while a text field
    # (filter box) has focus, so there's no PDF-page-input style clash.
    star_key_requested = Signal(int)
    # The hover 「◇類似」 overlay on an image tile was clicked (item 2-4) — the
    # host seeds an image→image similarity search from that tile's path.  Only
    # the left pane (with a vector index) enables + connects it.
    similar_requested = Signal(int)
    # 空状態カードのボタンが押された — **何番目か**だけをホストへ伝える
    # (retry / 絞り込みを解除 / フォルダを開く… の意味はホストが決める。
    # :meth:`set_empty_state` 参照)。
    #
    # 引数付きの 1 本にしたのはレビュー 2026-09-03 項目 #95: 従来は主
    # (``empty_action_clicked``) と副 (``empty_secondary_clicked``) の 2 本
    # 立てで、ボタンが 3 つ以上ある空状態（AI 検索の 0 件緩和カード — 効いて
    # いる軸ごとに 1 ボタン）を表せず、その面だけ別のオーバーレイ実装を持つ
    # 二重管理になっていた。0 = 主、1 = 副、以降は並び順。
    empty_action_clicked = Signal(int)

    #: 部品側の定数の別名（席の幾何・キャッシュ上限は部品が権威）。
    _UNFOCUSED_SELECTION_ALPHA = gvpaint.UNFOCUSED_SELECTION_ALPHA
    _SIMILAR_BTN_SIZE = gvpaint.SIMILAR_BTN_SIZE
    _SIMILAR_BTN_MARGIN = gvpaint.SIMILAR_BTN_MARGIN
    _ELIDE_CACHE_MAX = captions.ELIDE_CACHE_MAX
    _EMPTY_BUTTON_GAP = empty_card.EMPTY_BUTTON_GAP

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        zoom_handler: Callable[[int], None] | None = None,
    ) -> None:
        """*zoom_handler* = Ctrl+ホイールの受け手（**構築時のホスト契約**）.

        Ctrl+ホイールでサムネイルの大きさを 1 段変えるのは、同じウィンドウの
        他の面（画像 / Markdown）と揃えたアプリ全体の慣習。値の適用点は
        ホスト側のサイズスライダのままにし（``ChildrenGrid`` がこの符号を
        ±1 段へ翻訳して ``size_slider`` を動かす）、「スライダが唯一の権威」
        を保つ。``+1`` = 拡大 / ``-1`` = 縮小。

        **不変**: 受け手を構築時に受け取るのは、シグナルだと「誰も繋いで
        いないこと」をビューが知れないため。知れなければ Ctrl+ホイールを
        無条件に消費するしかなく、繋いでいない席（フォルダプレビューの子
        グリッド）ではジェスチャが死ぬ。``None`` のときは
        :meth:`wheelEvent` が ``event.ignore()`` で親へ流すので、その席の
        chrome（例: フォルダプレビューの「ホイール = ファイル送り」）が
        代わりに受け取る。

        ホストは**束縛メソッド**を渡すこと — 受け手は
        :func:`_weak_host_callable` が弱参照で持ち直す（理由はそちらの
        docstring: 子 → 親の参照閉路は PySide で落ちる）。
        """
        super().__init__(parent)
        self._zoom_handler = _weak_host_callable(zoom_handler)
        self._tiles: list[Tile] = []
        self._by_key: dict[str, int] = {}
        self._strategy: LayoutStrategy = SquareGrid()
        self._params = LayoutParams(target_size=160, spacing=6, caption_height=40)
        self._view_mode = "icon"
        # When True, ``paintEvent`` draws a ``♡N`` favorite-count badge on each
        # icon-mode tile whose entry carries a non-negative ``favorites`` value.
        # Toggled via :meth:`set_show_favorites`; off by default so the overlay
        # only appears when a caller opts in.
        self._show_favorites = False
        # When True (and NOT in the on-image seating mode) the legacy
        # below-image caption strip is backed by a surface-token band, turning
        # the tile into an image + label-plate card.  Opt-in per host: only the
        # main post grid's 「画像の下に表示」 name-placement mode turns it on
        # (:meth:`set_caption_band`); folder-preview / file-list keep the plain
        # bandless caption.  Ignored while the on-image scrim caption is active.
        self._caption_band = False
        # Curation-badge provider: ``path -> (star:int, later:bool)``.  Set by
        # the host (:meth:`set_curation_provider`) from an in-memory
        # ``user_meta`` dict so painting stays a pure lookup — no per-tile
        # sqlite / NAS I/O on the paint hot path.  ``None`` disables the star /
        # later overlays entirely (no curation store).
        self._curation_provider = None
        # Hover-tooltip extra-line provider (``path -> list[str]``), resolved at
        # tooltip time so mutable facts (★ / あとで見る / ユーザータグ) stay fresh
        # without a tile rebuild — UIレビュー 07-25 #136 / #13.
        self._tooltip_extra_provider = None
        self._layout = LayoutResult()
        # 直近の ``_do_relayout`` がレイアウト計算に使ったビューポート幅
        # (#99)。0 = まだ一度もレイアウトしていない / 1 = 幅 0 席の縮退
        # フォールバック（``_do_relayout`` の ``max(1, ...)``）。スクロール
        # レンジの「出自」なので、レンジに依存する消費判断（保留スクロール）
        # はウィジェットジオメトリではなくこちらを見る — ジオメトリは
        # setSizes 反映遅延中レンジと逆方向に古くなり得る（CI 実測）。
        self._last_layout_vp_w = 0
        #: 単一選択モデル（添字と遷移規則）— :mod:`gallery_view_parts.selection`。
        self._selection = SelectionState()
        # Empty-state message (#5): painted centred in the viewport when the
        # tile set has *settled* at zero.  The view stays dumb about data — the
        # host decides WHAT to say (empty folder vs. filter matched nothing)
        # and sets it via :meth:`set_empty_message`; an empty string (the
        # default, and the state during a scan) paints nothing, preserving the
        # historical blank viewport while results are still pending.
        self._empty_message = ""
        # Optional icon name (common/ui/icons.py glyph) painted above the
        # settled-empty message (redesign 2026-07 Phase 3-4's empty-state
        # card grammar) — "" (the default) paints no icon, matching the
        # historical text-only look.  Set alongside the message via
        # :meth:`set_empty_state`.
        self._empty_icon_name = ""
        # 空状態メッセージの下に並ぶ操作ボタン（"再試行" / "絞り込みを解除" /
        # "精度を 0.35 まで下げる" …）の ``(label, tooltip)`` 列。ボタン本体は
        # 遅延生成し、タイルが 0 のあいだだけ出す。クリックは
        # :attr:`empty_action_clicked` に**添字**として乗る — 意味はホスト持ち。
        # 主 / 副の 2 本立てを 1 本のリストへ畳んだ経緯は項目 #95（同上）。
        self._empty_actions: list[tuple[str, str]] = []
        self._empty_action_btns: list[QPushButton] = []
        self._placeholder_cache: dict[tuple[str, int], QPixmap] = {}
        # Reused title-band scrim gradient (Phase 3-2) — 1 本を各タイルの帯の
        # 下へ平行移動して使う（タイル毎に確保しない）。
        self._scrim = gvpaint.ScrimCache()
        # Memoised 2-line caption elision keyed by (text, width) — see
        # ``captions.title_rows``.  Cleared on font / style changes.
        # キーは (キャプション, 1 行目の幅, 最終行の幅) — バッジ行ぶん詰めた
        # 最終行の幅が別キーになる。値は行ごとの文字列。
        self._elide_cache: dict[tuple[str, int, int], tuple[str, ...]] = {}
        # バッジ行のフォントと計量器（``_view_badge_font`` / ``_view_badge_fm``）。
        # 1 タイル 1 フレームあたり QFont 3 個 + QFontMetrics 2 個を作り直して
        # いたので、席計算と描画で使い回す。無効化は ``changeEvent`` の 1 か所。
        self._badge_font: QFont | None = None
        self._badge_fm: QFontMetrics | None = None
        self._spinner_check = None
        self._spinner_angle = 0
        # Drag-to-export state: armed on left press, fires once the cursor
        # moves past the platform drag threshold (see mouseMoveEvent).
        self._drag_start_pos: QPoint | None = None
        self._drag_index: int | None = None
        #: Tile under the cursor (hover highlight).  Updated on unbuttoned
        #: mouse moves only; repaints are limited to the two affected tiles
        #: so tracking stays cheap even on large grids.
        self._hover_index: int | None = None
        #: Image-similarity hover overlay (item 2-4).  ``_similar_overlay_enabled``
        #: is host-gated (only the left pane with a vector index turns it on).
        #: The 「◇」 button appears on the hovered *image* tile only after a short
        #: dwell (``_similar_dwell_timer``) so it never flickers during a
        #: scan-past and stays faint until the pointer settles.
        self._similar_overlay_enabled = False
        self._similar_overlay_index: int | None = None
        self._similar_dwell_timer = Debouncer(
            self, 350, self._on_similar_dwell, mode=DebounceMode.TRAILING
        )

        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.StrongFocus)
        # Needed for the hover highlight (move events without a pressed
        # button); the handler is a single binary-search hit test per move.
        self.viewport().setMouseTracking(True)
        enable_touch_scroll(self.viewport())
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Coalesce geometry-driven relayouts (#13): a burst of aspect-probe
        # results, a size-slider drag (configure per valueChanged), or a
        # window/splitter resize each schedule ONE deferred re-layout instead
        # of running the O(n) full layout per event — on a 5000-tile flat
        # folder a slider drag otherwise spends 16ms+ of GUI time per step.
        # ``set_tiles`` / ``clear`` stay synchronous: the host's
        # ``_resolve_pending_select`` → ``ensure_visible`` reads the fresh
        # ``_layout.boxes`` immediately after populating.
        self._relayout_timer = Debouncer(
            self, 60, self._run_deferred_relayout, mode=DebounceMode.LEADING_WINDOW
        )

    # ----------------------------------------------------------- configuration

    def configure(
        self, *, view_mode: str, thumb_layout: str, params: LayoutParams,
    ) -> None:
        """Set the view mode, layout strategy, and geometry params at once.

        The re-layout itself is deferred through :meth:`_schedule_relayout`
        (#13) so a slider drag — one ``configure`` per ``valueChanged`` —
        coalesces into at most ~16 full layouts per second instead of one
        per event.
        """
        self._view_mode = view_mode if view_mode in ("icon", "list") else "icon"
        self._strategy = make_strategy(self._view_mode, thumb_layout)
        self._params = params
        # 表示形式の切替はタイルを作り直さないので ``_reset_pointer_state`` を
        # 通らない。ホバーで出した「◇」の添字と待機タイマーが残ると、◇ が
        # 描かれない一覧表示でも行の右端に見えない当たり判定ができる。
        self._similar_dwell_timer.stop()
        self._similar_overlay_index = None
        self._schedule_relayout()

    def set_params(self, params: LayoutParams) -> None:
        self._params = params
        self._schedule_relayout()

    # ----------------------------------------------------------- population

    def clear(self) -> None:
        self._tiles = []
        self._by_key.clear()
        self._selection.reset()
        self._reset_pointer_state()
        self._hover_index = None
        # A clear precedes a fresh scan (or an intentional blank pane) — drop
        # any settled empty-state message so nothing shows while the new
        # results are still pending (the host re-sets it once they land).
        self._empty_message = ""
        self._empty_icon_name = ""
        self._empty_actions = []
        self._sync_empty_action_button()
        self._layout = LayoutResult()
        self.verticalScrollBar().setValue(0)
        self.verticalScrollBar().setRange(0, 0)
        self.viewport().update()

    def set_tiles(self, tiles: list[Tile]) -> None:
        self._tiles = list(tiles)
        self._by_key = {t.key: i for i, t in enumerate(self._tiles)}
        self._selection.reset()
        self._reset_pointer_state()
        self._sync_empty_action_button()
        self._do_relayout()

    def _reset_pointer_state(self) -> None:
        """Disarm an in-progress drag when the tile array is replaced.

        ``_drag_index`` is an index into ``self._tiles``; an async rebuild
        (metadata batch, search results landing, filter change) while the
        mouse button is held would otherwise let the drag fire against a
        DIFFERENT tile at the same index — exporting the wrong file's URL
        to Explorer.

        The similar-image hover overlay holds an index too: a stale
        ``_similar_overlay_index`` (or a dwell timer still pending) would
        make the 「◇」 button show — and fire ``similar_requested`` — on
        whatever tile now sits at that index, so both are reset as well.
        """
        self._drag_start_pos = None
        self._drag_index = None
        self._similar_dwell_timer.stop()
        self._similar_overlay_index = None

    # ----------------------------------------------------------- empty state

    def set_empty_message(self, text: str) -> None:
        """Backwards-compatible alias for :meth:`set_empty_state` (no action)."""
        self.set_empty_state(text)

    def set_empty_state(
        self,
        text: str,
        action_text: str = "",
        icon_name: str = "",
        secondary_text: str = "",
        *,
        actions: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        """Set the message painted when the tile set has settled at zero (#5).

        The host calls this after a rebuild lands: a non-empty *text* is drawn
        centred in the viewport while there are no tiles; ``""`` (also the
        state after :meth:`clear`, i.e. mid-scan) paints nothing.  A stale
        message can never show over tiles — painting is gated on the tile
        list being empty.

        A non-empty *action_text* additionally shows a real button under the
        message (再試行 / 絞り込みを解除 / …); clicking it emits
        :attr:`empty_action_clicked` — the host decides what it does.  The
        button follows the same lifetime as the message (hidden the moment
        tiles exist or the state is cleared).

        *icon_name* (a ``common/ui/icons.py`` glyph, e.g. ``"folder"`` /
        ``"search"``) paints a small muted icon above the message — the
        empty-state card grammar (redesign 2026-07 Phase 3-4).  ``""`` (the
        default) paints no icon, matching the historical text-only look.

        *secondary_text* は**第 2 ボタン**（副アクション）で、主ボタンの右隣に
        並ぶ (UIレビュー 07-25 #26)。回復手段が 2 つある空状態（例: 「サブ
        フォルダも検索して再試行」/「検索条件をすべて解除」）で、片方をもう
        片方に差し替えるしかなかった制約を外すために追加した。``""`` なら
        従来どおり主ボタンのみ。

        *actions* を渡すと ``action_text`` / ``secondary_text`` の代わりに
        ``(label, tooltip)`` の**任意個**のボタンを並べる（3 つ以上は縦積み）。
        AI 検索の 0 件カードが「効いている軸ごとに 1 つ緩和ボタン」を出すため
        (レビュー 2026-09-03 項目 #95) — 以前はそれ専用の別オーバーレイ実装が
        あり、同じグリッドに空状態のオーナーが 2 つ居た。
        """
        text = str(text or "")
        icon_name = str(icon_name or "")
        if actions is None:
            actions = [
                (str(label), "")
                for label in (action_text, secondary_text)
                if str(label or "")
            ]
        norm = [(str(label), str(tip or "")) for label, tip in actions]
        if (
            text == self._empty_message
            and icon_name == self._empty_icon_name
            and norm == self._empty_actions
        ):
            return
        self._empty_message = text
        self._empty_icon_name = icon_name
        self._empty_actions = norm
        self._sync_empty_action_button()
        if not self._tiles:
            self.viewport().update()

    def _sync_empty_action_button(self) -> None:
        """Create/show/hide + position the empty-state action buttons.

        ボタン列 (:attr:`_empty_actions`) の全要素を同じ寿命で扱う — タイルが
        現れた瞬間・状態クリア時にすべて消える。ウィジェットは使い回し、余った
        分は隠すだけ（0 件カードの緩和ボタンは条件を触るたび個数が変わる）。
        """
        want = self._empty_actions if not self._tiles else []
        for index, (label, tooltip) in enumerate(want):
            if index >= len(self._empty_action_btns):
                btn = QPushButton(self.viewport())
                btn.setCursor(Qt.PointingHandCursor)
                btn.clicked.connect(self._on_empty_action_button)
                self._empty_action_btns.append(btn)
            btn = self._empty_action_btns[index]
            btn.setText(label)
            btn.setToolTip(tooltip)
            btn.adjustSize()
        for btn in self._empty_action_btns[len(want):]:
            btn.hide()
        if not want:
            return
        # 表示の可否は配置側が決める（テキストに重なる高さでは出さない）。
        self._position_empty_action_button()

    def empty_action_labels(self) -> list[str]:
        """空状態カードに今載っているボタンのラベル列（観測点）.

        「どのボタンが出ているか」を外から見る唯一の公開口 — 内部表現
        (:attr:`_empty_actions`) を直接読む必要をなくす。空リスト = ボタン無し。
        """
        return [label for label, _tooltip in self._empty_actions]

    def _on_empty_action_button(self) -> None:
        """どのボタンが押されたかを添字にして中継する（唯一の押下口）。"""
        sender = self.sender()
        for index, btn in enumerate(self._empty_action_btns):
            if btn is sender:
                self.empty_action_clicked.emit(index)
                return

    def _empty_text_blocks(self) -> tuple[str, str]:
        """``(見出し, 本文)`` — 最初の改行で割る（:func:`empty_card.split_blocks`）。"""
        return empty_card.split_blocks(self._empty_message)

    def _empty_heading_font(self) -> QFont:
        """空状態の見出しフォント（タイトルサイズ + 太字。直書き禁止の規約）。"""
        return empty_card.heading_font(self.font())

    def _empty_text_layout(self) -> empty_card.EmptyTextLayout:
        """空状態テキストの唯一の計測点（描画・座席計算が同じ数を読む）.

        見出しと本文でフォントが違うので、単一フォントで全文を測ると
        アイコンとアクションボタンの座席が見出しぶんズレる。描画
        (:meth:`_paint_empty_message`) とボタン配置
        (:meth:`_position_empty_action_button`) の 2 経路をここへ寄せている。
        """
        return empty_card.measure(
            self.viewport().rect(),
            self._empty_message,
            heading_fm=QFontMetrics(self._empty_heading_font()),
            body_fm=self.fontMetrics(),
            has_icon=bool(self._empty_icon_name),
        )

    def _position_empty_action_button(self) -> None:
        """Centre the action button(s) just below the painted message block."""
        if self._tiles:
            # 表示の可否は :meth:`_sync_empty_action_button` と同じ述語 1 つ
            # （タイルが 1 枚でもあれば空状態のボタンは出ない）。``resizeEvent``
            # はここを直接呼ぶので、ここで所有権を取ると着地済みのタイルの上に
            # ボタンが戻る。
            for btn in self._empty_action_btns:
                btn.setVisible(False)
            return
        count = len(self._empty_actions)
        if count == 0 or not self._empty_action_btns:
            return
        btns = self._empty_action_btns[:count]
        # UIレビュー07-25 追修: ``resizeEvent`` からもここへ来るため、フォント /
        # テーマが変わった直後は古い幅・高さのままだった。位置決めの前に
        # サイズヒントを取り直す（テキストは既に set 済みなので再計算だけ）。
        for btn in btns:
            btn.adjustSize()
        # 見出し / 本文でフォントが違うので、下端は共通の計測点から取る
        # （単一フォントで全文を測ると見出しぶん低く出てボタンが文字に重なる）。
        points = empty_card.plan_button_row(
            [(btn.width(), btn.height()) for btn in btns],
            self.viewport().rect(),
            self._empty_text_layout().bottom + 16,
        )
        if points is None:
            for btn in btns:
                btn.setVisible(False)
            return
        for btn, point in zip(btns, points, strict=True):
            btn.move(point)
            btn.setVisible(True)
            btn.raise_()

    # NOTE (#70): there is deliberately no ``append_tiles``.  The removed
    # implementation had no callers and skipped ``_sync_empty_action_button``
    # / ``_reset_pointer_state`` — a batched-append entry point must perform
    # the same synchronisation ``set_tiles`` does, or it re-introduces the
    # "empty-state button left on top of tiles" / stale drag-index bugs.

    # ----------------------------------------------------------- tile access

    def tile_count(self) -> int:
        return len(self._tiles)

    def tile_at(self, index: int) -> Tile | None:
        if 0 <= index < len(self._tiles):
            return self._tiles[index]
        return None

    def index_of_key(self, key: str) -> int | None:
        return self._by_key.get(key)

    def replace_tile(self, index: int, tile: Tile) -> None:
        """Swap a tile in place (metadata enrichment); preserves its pixmap.

        The caller is responsible for carrying forward any already-decoded
        pixmap if desired — typically the host re-sets the thumbnail after.
        """
        if not (0 <= index < len(self._tiles)):
            return
        old_key = self._tiles[index].key
        if old_key in self._by_key:
            del self._by_key[old_key]
        self._tiles[index] = tile
        self._by_key[tile.key] = index
        self._update_tile_region(index)

    # ----------------------------------------------------- thumbnail / aspect

    def set_thumb(
        self, key: str, pixmap: QPixmap, is_fallback: bool | None = None,
    ) -> None:
        idx = self._by_key.get(key)
        if idx is None:
            return
        tile = self._tiles[idx]
        tile.pixmap = pixmap
        tile.thumb_loaded = True
        tile.pixmap_size = (
            QSize(pixmap.width(), pixmap.height()) if pixmap else QSize(0, 0)
        )
        # A successful decode supersedes any settled failure (#114): the
        # failure gate exists to stop retry storms, and once a fresh decode
        # lands the tile is healthy again — future upgrades gate on
        # ``pixmap_size`` alone.
        tile.thumb_failed = False
        tile.thumb_failed_edge = 0
        if is_fallback is not None:
            tile.is_fallback = is_fallback
        self._update_tile_region(idx)

    def reset_thumbnails(self) -> None:
        """Drop all decoded pixmaps (e.g. on size change → re-decode).

        Also clears the settled decode-failure flag so this cache-clearing
        refresh (the deliberate retry path) gives a previously-failed tile one
        fresh attempt — otherwise a transient failure (offline NAS / locked
        file) would stay stuck on its glyph until the folder is left and
        revisited.
        """
        for t in self._tiles:
            t.pixmap = None
            t.thumb_loaded = False
            t.thumb_failed = False
            t.thumb_failed_edge = 0
            t.pixmap_size = QSize(0, 0)
        self.viewport().update()

    def set_aspect(self, key: str, width: int, height: int) -> None:
        idx = self._by_key.get(key)
        if idx is None or height <= 0 or width <= 0:
            return
        tile = self._tiles[idx]
        new_aspect = width / height
        if tile.aspect_known and abs(tile.aspect - new_aspect) < 1e-3:
            return
        tile.aspect = new_aspect
        tile.aspect_known = True
        # Debounced re-justify so a batch of probe results coalesces.
        self._schedule_relayout()

    def set_tile_fallback(self, key: str, is_fallback: bool) -> None:
        idx = self._by_key.get(key)
        if idx is None:
            return
        self._tiles[idx].is_fallback = is_fallback
        self._update_tile_region(idx)

    def mark_thumb_failed(self, key: str) -> None:
        """Settle *key*'s placeholder from "loading" to the static glyph (C03).

        Called by the host when the thumbnail loader reports a decode failure,
        so a broken / unreadable image stops looking forever-pending.

        Also records the tile's current physical box edge as
        ``thumb_failed_edge`` (#114): the host skips re-requests at (or
        below) that size — no retry storm on an unreadable file — but a
        later box growth past it earns one fresh attempt, so a one-off
        failure during a resolution upgrade can't pin an already-loaded
        tile to its blurry low-res pixmap forever.  A repeated failure at a
        larger size ratchets the recorded edge up (never down).
        """
        idx = self._by_key.get(key)
        if idx is None:
            return
        tile = self._tiles[idx]
        edge = self._box_physical_edge(idx)
        if edge > tile.thumb_failed_edge:
            tile.thumb_failed_edge = edge
        if tile.thumb_failed:
            return
        tile.thumb_failed = True
        self._update_tile_region(idx)

    def _box_physical_edge(self, index: int) -> int:
        """Longest edge of tile *index*'s laid-out box in physical px (#114).

        Mirrors the host's request-size computation
        (``ChildrenGrid._physical_size`` over ``box_size``), falling back to
        the slider target when the box is missing — the same fallback the
        request path uses, so the two sides always compare like for like.
        """
        size = self.box_size(index)
        if size is None or size.width() <= 0 or size.height() <= 0:
            size = QSize(self._params.target_size, self._params.target_size)
        dpr = self.devicePixelRatioF() or 1.0
        return ceil(max(size.width(), size.height()) * max(1.0, dpr))

    # ----------------------------------------------------------- selection

    @property
    def _selected(self) -> int:
        """現在の選択添字（``-1`` = 無し）。実体は :attr:`_selection`。"""
        return self._selection.index

    def selected_index(self) -> int:
        return self._selection.index

    def current_tile(self) -> Tile | None:
        return self.tile_at(self._selection.index)

    def current_path(self) -> Path | None:
        t = self.current_tile()
        return t.path if t is not None else None

    def current_pixmap_as_icon(self) -> QIcon | None:
        t = self.current_tile()
        if t is None or t.pixmap is None or t.pixmap.isNull():
            return None
        return QIcon(t.pixmap)

    def pixmap_for_key(self, key: str) -> QPixmap | None:
        """Return the already-decoded thumbnail pixmap for *key*, if any.

        Used as a zero-cost placeholder source for the central image
        preview: the tile's thumbnail is already resident in memory, so
        the preview can paint it instantly while the full-resolution
        decode runs.  Returns ``None`` when the tile is unknown or its
        thumbnail hasn't decoded yet.
        """
        idx = self._by_key.get(key)
        if idx is None:
            return None
        pixmap = self._tiles[idx].pixmap
        if pixmap is None or pixmap.isNull():
            return None
        return pixmap

    def select_index(self, index: int, *, emit: bool = True, ensure: bool = True) -> bool:
        move = self._selection.select(index, len(self._tiles))
        if move is None:
            return False
        if move.changed:
            self._update_tile_region(move.previous)
            self._update_tile_region(move.current)
        if ensure:
            self.ensure_visible(index)
        if move.changed and emit:
            self.selection_changed.emit(index)
        return True

    def select_key(self, key: str, *, emit: bool = True) -> bool:
        idx = self._by_key.get(key)
        if idx is None:
            return False
        return self.select_index(idx, emit=emit)

    def select_first(self, *, emit: bool = True) -> bool:
        if self._tiles:
            return self.select_index(0, emit=emit)
        return False

    def select_last(self, *, emit: bool = True) -> bool:
        """末尾タイルを選択（Home/End の End 側 — UIレビュー 07-25 #22）。"""
        if self._tiles:
            return self.select_index(len(self._tiles) - 1, emit=emit)
        return False

    def step_selection(self, delta: int) -> bool:
        target = self._selection.step_target(delta, len(self._tiles))
        if target is None:
            return False
        return self.select_index(target, emit=True)

    def clear_selection(self) -> None:
        self._update_tile_region(self._selection.reset())

    # ----------------------------------------------------------- spinner

    def set_spinner_check(self, predicate) -> None:
        self._spinner_check = predicate
        self.viewport().update()

    def advance_spinner(self) -> None:
        if self._spinner_check is None:
            return
        # The host drives this from an unconditional 80ms timer, so without
        # this guard an idle pane repainted every visible tile 12.5x per
        # second with no spinner to animate (#71).
        if not self._has_spinner_tile():
            return
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self.viewport().update()

    def _has_spinner_tile(self) -> bool:
        """Whether any *visible* tile currently draws a pending spinner.

        描画側 (:func:`painter.maybe_paint_spinner`) と**同じ述語**
        (:func:`painter.spinner_eligible`) を可視域だけに掛ける — 片側だけ
        緩いと「描かないタイルのために 80ms タイマが viewport 全体を回し
        続ける」（項目#60）。述語は純粋なメモリ引きで paint パスと同じ。
        """
        check = self._spinner_check
        if check is None:
            return False
        rng = self.visible_indices(buffer_rows=0)
        if rng is None:
            return False
        first, last = rng
        for i in range(first, last + 1):
            tile = self.tile_at(i)
            if tile is not None and gvpaint.spinner_eligible(tile, check):
                return True
        return False

    def notify_cache_changed(self) -> None:
        self.viewport().update()

    # ----------------------------------------------------------- layout

    def last_layout_viewport_width(self) -> int:
        """直近のレイアウト計算に使われたビューポート幅 (#99).

        現在のスクロールレンジの「出自」。``<= 1`` は縮退レイアウト
        （幅 0 席のフォールバック幅 1、または未レイアウト = 0）を意味し、
        そのレンジへの ``setValue`` は復元値の恒久クランプになる — レンジ
        依存の消費判断（``ChildrenGrid._apply_pending_scroll``）はウィジェット
        ジオメトリではなくこれを見ること（setSizes の反映遅延中、ジオメトリと
        レンジは互いに逆方向へ古くなり得る）。
        """
        return self._last_layout_vp_w

    def _stable_viewport_width(self) -> int:
        """Viewport width invariant to scrollbar visibility (anti-oscillation)."""
        vbar = self.verticalScrollBar()
        vp_w = self.viewport().width()
        if vbar.isVisible():
            return vp_w
        extent = self.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, vbar)
        return max(0, vp_w - extent)

    def _tile_inputs(self) -> list[TileInput]:
        return [
            TileInput(
                aspect=t.aspect, aspect_known=t.aspect_known,
                expandable=t.expandable,
            )
            for t in self._tiles
        ]

    def _schedule_relayout(self) -> None:
        """Coalesce geometry changes into one deferred :meth:`_do_relayout`.

        Shared by ``configure`` / ``set_params`` / ``resizeEvent`` and the
        aspect-probe settles (#13): every path rides the same 60ms
        single-shot timer, so an event burst (slider drag, splitter drag,
        probe batch) costs one O(n) layout instead of one per event.  A
        stale layout can be read for at most one timer interval; the
        follow-up ``visible_range_changed`` from the deferred layout
        re-drives the host's visible-request pass, so thumbnail requests
        self-heal.
        """
        self._relayout_timer.trigger()

    def flush_pending_relayout(self) -> None:
        """保留中の遅延リレイアウトを今すぐ実行する（#13 追補）。

        コアレサ(60ms)の唯一の観測可能な弱点は「スクロールレンジを直後に
        読むホスト操作」: 分割復帰のスクロール復元（項目#10）は setSizes 直後
        に ``verticalScrollBar().maximum()`` を読むが、resizeEvent 経由の
        レイアウトが保留のままだと幅 0 時代の maximum=0 が見え、復元値が 0 に
        クランプされてしまう（値はタイマー発火後も自然回復しない）。レンジに
        依存する操作の直前に 1 回呼べば、保留が無いときは何もしない。
        """
        self._relayout_timer.flush_now()

    def _run_deferred_relayout(self) -> None:
        """遅延リレイアウト（コアレサ発火 / flush）の実体 + 収束通知 (#99).

        レンジ確定**後**に ``relayout_converged`` を発火する — ホストはこれを
        「保留スクロールを適用してよいレンジが揃った」合図として使う。
        ``set_tiles`` の同期リレイアウトはこのラッパーを通らない（シグナルの
        クラスコメント参照）。
        """
        self._do_relayout()
        self.relayout_converged.emit()

    def _do_relayout(self) -> None:
        # Tiles are about to move; a stale hover index would wash the wrong
        # cell.  The relayout repaints the whole viewport anyway, and the
        # next mouse move re-establishes the highlight.
        self._hover_index = None
        vp_w = self._stable_viewport_width()
        if vp_w <= 0:
            vp_w = max(1, self.viewport().width())
        self._last_layout_vp_w = vp_w
        with measure("gallery_relayout", f"{len(self._tiles)} tiles"):
            self._layout = self._strategy.layout(
                self._tile_inputs(), viewport_width=vp_w, params=self._params,
            )
        vp_h = self.viewport().height()
        max_scroll = max(0, self._layout.content_height - vp_h)
        vbar = self.verticalScrollBar()
        vbar.setRange(0, max_scroll)
        vbar.setPageStep(vp_h)
        vbar.setSingleStep(max(1, vp_h // 10))
        self.viewport().update()
        if self._layout.rows:
            self.visible_range_changed.emit()

    def layout_is_degenerate(self) -> bool:
        """現在のレイアウトが**畳まれた席**由来か (issue #160).

        判定は ``last_layout_viewport_width() <= 1`` — ウィジェットジオメトリ
        ではなく**レンジの出自**を見る（保留スクロールの消費判断
        ``ChildrenGrid._apply_pending_scroll`` と同じ裁定。setSizes の反映
        遅延中はジオメトリとレンジが互いに逆方向へ古くなり得る）。
        """
        return self._last_layout_vp_w <= 1

    def visible_indices(self, *, buffer_rows: int = 1) -> tuple[int, int] | None:
        """可視（+ *buffer_rows* 行）のタイル添字域。畳まれた席では ``None``.

        プレビュー最大化は分割比プリセットでグリッド席の幅を 0 に畳む
        (issue #160)。幅 0 のレイアウト（``_do_relayout`` のフォールバック幅
        1）では 1 行 1 タイルの細い帯が縦に並び、**1 画面も表示していない**の
        にビューポート高さいっぱいのタイルが「可視」と出る — ホストはそれを
        信じてサムネイル / アスペクトプローブを要求し、アスペクトが着地する
        たびに行高が変わって次の帯を要求する連鎖になる（数千枚規模で顕在化）。
        席が畳まれている間は**見えているタイルは 1 枚も無い**のが正しいので、
        可視集合を空（``None``）にして要求経路ごと止める。幅が戻れば
        ``resizeEvent`` → コアレサ → ``visible_range_changed`` で通常の可視
        タイル要求が再開する（レイアウト・スクロール復元の経路は不変）。
        """
        if self.layout_is_degenerate():
            return None
        return visible_range(
            self._layout,
            scroll_y=self.verticalScrollBar().value(),
            viewport_height=self.viewport().height(),
            buffer_rows=buffer_rows,
        )

    def visible_indices_strict(self) -> tuple[int, int] | None:
        """バッファ行を含まない可視域。畳まれた席では ``None``（同上）。"""
        if self.layout_is_degenerate():
            return None
        return visible_range(
            self._layout,
            scroll_y=self.verticalScrollBar().value(),
            viewport_height=self.viewport().height(),
            buffer_rows=0,
        )

    def box_size(self, index: int) -> QSize | None:
        """Image-area size (logical px) of tile *index*, or ``None``.

        Used by the host to request a thumbnail at the tile's exact display
        size so the disk-cache master downscales to a crisp box.
        """
        if 0 <= index < len(self._layout.boxes):
            box = self._layout.boxes[index]
            return QSize(box.w, box.h)
        return None

    def _tile_viewport_rect(self, index: int) -> QRect | None:
        """Cell rect (image + caption) in viewport coords, or ``None``."""
        if not (0 <= index < len(self._layout.boxes)):
            return None
        box = self._layout.boxes[index]
        scroll_y = self.verticalScrollBar().value()
        if self._view_mode == "icon":
            h = box.h + self._params.caption_height
        else:
            h = box.h
        return QRect(box.x, box.y - scroll_y, box.w, h)

    def _update_tile_region(self, index: int) -> None:
        rect = self._tile_viewport_rect(index)
        if rect is not None:
            # Pad a little for the folder badge / selection border overdraw.
            self.viewport().update(rect.adjusted(-2, -2, 2, 2))

    def ensure_visible(self, index: int) -> None:
        if not (0 <= index < len(self._layout.boxes)):
            return
        box = self._layout.boxes[index]
        cell_h = box.h + (self._params.caption_height if self._view_mode == "icon" else 0)
        vbar = self.verticalScrollBar()
        vp_h = self.viewport().height()
        top = box.y
        bottom = box.y + cell_h
        cur = vbar.value()
        if top < cur:
            vbar.setValue(top)
        elif bottom > cur + vp_h:
            vbar.setValue(min(vbar.maximum(), bottom - vp_h))

    # ----------------------------------------------------------- events

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_empty_action_button()
        # Deferred (#13): a window/splitter drag fires resize continuously;
        # the shared coalescer caps the O(n) relayouts at the timer rate.
        self._schedule_relayout()

    def _on_scroll(self, _value: int) -> None:
        # スクロールするとカーソルの下のタイルは入れ替わる。取り直さないと
        # ホバーの淡色ウォッシュと「◇」がカーソルの下にないタイルに残る
        # （Qt の項目ビューはスクロール時にホバーを引き直す）。
        self._resolve_hover_under_cursor()
        self.viewport().update()
        self.visible_range_changed.emit()

    def _resolve_hover_under_cursor(self) -> None:
        """いまカーソルが載っているタイルへホバーを付け替える（外なら外す）."""
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        inside = self.viewport().rect().contains(pos)
        self._update_hover(self._index_at_viewport_pos(pos) if inside else None)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        intent = gvinput.wheel_intent(
            delta=delta,
            ctrl=bool(event.modifiers() & Qt.ControlModifier),
            has_zoom_handler=self._zoom_handler is not None,
            # Read via the live module attribute — a from-import would freeze
            # the value at import time (see the view_prefs module docstring).
            scroll_pixels=view_prefs._PREVIEW_SCROLL_PIXELS,
        )
        if isinstance(intent, gvinput.Zoom):
            self._zoom_handler(intent.steps)
            event.accept()
            return
        if isinstance(intent, gvinput.ScrollBy):
            vbar = self.verticalScrollBar()
            vbar.setValue(vbar.value() + intent.pixels)
            event.accept()
            return
        if delta:
            # Ctrl+ホイールだが受け手の居ない席 — 消費せず親へ流し、その席の
            # chrome（フォルダプレビューの「ホイール = ファイル送り」）に委ねる。
            event.ignore()
            return
        super().wheelEvent(event)

    def event(self, ev) -> bool:  # type: ignore[override]
        # Accept ShortcutOverride for navigation keys so the window's global
        # ←/→ shortcut doesn't steal them while this pane has focus — Qt
        # then delivers them as normal key presses to keyPressEvent.
        if ev.type() == QEvent.ShortcutOverride:
            if ev.key() in gvinput.NAV_KEYS:
                ev.accept()
                return True
        return super().event(ev)

    def viewportEvent(self, ev) -> bool:  # type: ignore[override]
        # Per-tile hover tooltips (``Tile.tooltip``, opt-in per tile).  A
        # custom-painted view has no item widgets for Qt to attach tooltips
        # to, so the QHelpEvent is resolved through the layout hit test.
        if ev.type() == QEvent.ToolTip:
            idx = self._index_at_viewport_pos(ev.pos())
            tile = self.tile_at(idx) if idx is not None else None
            # The 類似検索 overlay is a *button* inside the tile: name it rather
            # than repeating the tile's own tooltip (UIレビュー 08-28 N-63).
            seat = (
                self._similar_button_rect(idx)
                if idx is not None and self._similar_overlay_index == idx
                else None
            )
            if seat is not None and seat.contains(ev.pos()):
                text = badge_tooltip("similar")
            else:
                text = self._tooltip_text_for(tile) if tile is not None else ""
            if text:
                QToolTip.showText(ev.globalPos(), text, self.viewport())
            else:
                QToolTip.hideText()
                ev.ignore()
            return True
        return super().viewportEvent(ev)

    def _tooltip_text_for(self, tile: "Tile") -> str:
        """``Tile.tooltip`` plus any display-time extra lines (UIレビュー #136).

        ``Tile.tooltip`` is baked when the tile is built, so anything that can
        change *while the tile is on screen* (the user's ★ / 「あとで見る」 /
        ユーザータグ) has to be resolved here instead — otherwise a star set with
        the digit keys wouldn't reach the tooltip until the next rebuild.  The
        provider is an in-memory lookup (:meth:`set_tooltip_extra_provider`),
        and this runs only on an actual hover, never on the paint path.
        """
        lines = [tile.tooltip] if tile.tooltip else []
        if self._tooltip_extra_provider is not None:
            try:
                extra = self._tooltip_extra_provider(tile.path) or ()
            except Exception:  # pragma: no cover (defensive)
                extra = ()
            lines.extend(str(line) for line in extra if line)
        return "\n".join(lines)

    def set_tooltip_extra_provider(self, provider) -> None:
        """Install a ``path -> list[str]`` hover-tooltip line supplier (#136).

        Resolved at tooltip time (see :meth:`_tooltip_text_for`), so the host can
        surface mutable per-entry facts — ★N / 「あとで見る」 / ユーザータグ — that
        the badges only draw as pictograms.  ``None`` removes the extra lines.
        """
        self._tooltip_extra_provider = provider

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """フォーカスを得た: 選択枠を通常の強さで描き直す (UIレビュー 07-25 #47)."""
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """フォーカスを失った: 選択枠を減光して描き直す (UIレビュー 07-25 #47).

        レール・グリッド・情報パネルの一覧が同時に同じアクセントで光ると、
        キーがどのペインへ届くのかを画面から読み取れない。QAbstractItemView
        と同じく、フォーカスを持つペインだけが満照度の選択枠を持つ。
        """
        super().focusOutEvent(event)
        self.viewport().update()

    def _selection_active(self) -> bool:
        """選択強調を満照度で描くか（= このビューがキー入力を受ける側か）.

        UIレビュー07-25 追修: 判定が ``hasFocus()`` だけだったため、右クリック
        メニュー（``QMenu.exec()``）やツールバーのポップオーバーを開いた瞬間に
        フォーカスがポップアップへ移り、**いま操作している当のタイル**が減光
        されていた。Qt 本体の非アクティブ選択描画がフォーカスウィジェットでは
        なくウィンドウのアクティブ状態 (``State_Active``) を見ているのと同じ
        理由で、ポップアップ表示中はこのビューがキーの受け手のままとみなす。

        本来の目的（どのペインがキーを持つかを見せる）は残す — フォーカスが
        隣のペインや検索欄など**実在の別ウィジェット**へ移ったときは従来どおり
        減光する。
        """
        if self.hasFocus():
            return True
        app = QApplication.instance()
        if app is None:  # pragma: no cover (GUI アプリ外)
            return False
        if QApplication.activePopupWidget() is not None:
            # メニュー / Qt.Popup のポップオーバーが開いている間は、フォーカスが
            # 別ペインへ「移った」のではなく一時的に「重なっている」だけ。
            return True
        # メニューがフォーカスを持つ経路（ポップアップとして数えられない
        # 派生ケース）も同じ扱い。
        return isinstance(app.focusWidget(), QMenu)

    def _selection_color(self, alpha: int = 255):
        """選択強調色（palette の highlight）を *alpha* で返す (#47)。"""
        return gvpaint.selection_color(
            self.palette(), self._selection_active(), alpha,
        )

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        """キーを**意図**へ翻訳して適用する（分岐表は
        :func:`gallery_view_parts.input.key_intent`）。

        Escape の枝はここにも意図側にも**無い** (#115): 窓の常時 ``QShortcut``
        (main_window ``_sc_escape`` → ``_on_escape``) が先に消費する（Escape は
        意図的に ``NAV_KEYS`` に入れていないので ``ShortcutOverride`` でも
        取り返さない）— 枝を置くと生きた第 2 実装に読める死にコードになる。
        """
        intent = gvinput.key_intent(
            event.key(), event.modifiers(),
            tile_count=len(self._tiles),
            has_selection=self._selection.is_set,
        )
        if isinstance(intent, gvinput.Unhandled):
            super().keyPressEvent(event)
            return
        self._apply_key_intent(intent)
        event.accept()

    def _apply_key_intent(self, intent) -> None:
        """:func:`key_intent` の戻りを実際の状態変更へ落とす（唯一の適用点）。"""
        if isinstance(intent, gvinput.StepSelection):
            self.step_selection(intent.delta)
        elif isinstance(intent, gvinput.SelectIndex):
            self.select_index(intent.index)
        elif isinstance(intent, gvinput.MoveRow):
            nxt = nearest_in_adjacent_row(
                self._layout, self._selection.index, intent.direction,
            )
            if nxt is not None:
                self.select_index(nxt)
        elif isinstance(intent, gvinput.PageStep):
            self._page_step_selection(intent.direction)
        elif isinstance(intent, gvinput.ActivateCurrent):
            self.item_activated.emit(self._selection.index)
        elif isinstance(intent, gvinput.GoUp):
            self.go_up_requested.emit()
        elif isinstance(intent, gvinput.StarKey):
            self.star_key_requested.emit(intent.value)

    def _page_step_selection(self, direction: int) -> None:
        """Move the selection ~one viewport page up/down (``direction`` ±1).

        Matches ``QAbstractItemView``'s Page semantics: the cursor moves
        *with* the page instead of being left behind off-screen (which made
        the next arrow press snap the viewport back to the stale selection).
        計画は純関数 :func:`gallery_view_parts.input.page_step_plan` が組み、
        ここは適用だけを行う。
        """
        plan = gvinput.page_step_plan(
            self._layout,
            selected=self._selection.index,
            tile_count=len(self._tiles),
            direction=direction,
            page_h=self.viewport().height(),
        )
        vbar = self.verticalScrollBar()
        if plan.scroll_delta:
            vbar.setValue(vbar.value() + plan.scroll_delta)
        if plan.select_from_visible:
            rng = self.visible_indices_strict()
            if rng is not None:
                self.select_index(rng[0] if direction > 0 else rng[1])
            return
        if plan.select_index is not None:
            self.select_index(plan.select_index)

    def _index_at_viewport_pos(self, pos: QPoint) -> int | None:
        scroll_y = self.verticalScrollBar().value()
        return hit_test(self._layout, pos.x(), pos.y() + scroll_y)

    # ------------------------------------------------------ hover highlight

    def _update_hover(self, index: int | None) -> None:
        if index == self._hover_index:
            return
        old, self._hover_index = self._hover_index, index
        # Similarity overlay (item 2-4): a hover change hides any shown 「◇」
        # button and, over an image tile, arms the dwell timer so it fades in
        # only once the pointer settles.
        self._similar_dwell_timer.stop()
        if self._similar_overlay_index is not None:
            prev = self._similar_overlay_index
            self._similar_overlay_index = None
            self._update_tile_region(prev)
        if (
            self._similar_overlay_enabled
            and index is not None
            and self._is_image_tile(self.tile_at(index))
        ):
            self._similar_dwell_timer.trigger()
        for i in (old, index):
            if i is None:
                continue
            rect = self._tile_viewport_rect(i)
            if rect is not None:
                self.viewport().update(rect)

    def set_similar_overlay_enabled(self, on: bool) -> None:
        """Host gate for the hover 「◇類似」 overlay (item 2-4).

        Only the left pane (with a ``VectorIndex``) enables it; the right pane
        leaves it off.  Disabling drops any shown button.
        """
        on = bool(on)
        if on == self._similar_overlay_enabled:
            return
        self._similar_overlay_enabled = on
        if not on:
            self._similar_dwell_timer.stop()
            if self._similar_overlay_index is not None:
                prev = self._similar_overlay_index
                self._similar_overlay_index = None
                self._update_tile_region(prev)

    @staticmethod
    def _is_image_tile(tile: "Tile | None") -> bool:
        return is_image_tile(tile)

    def _on_similar_dwell(self) -> None:
        """Reveal the 「◇」 button on the settled image tile (icon mode only)."""
        idx = self._hover_index
        if (
            self._view_mode != "icon"
            or idx is None
            or not self._is_image_tile(self.tile_at(idx))
        ):
            return
        self._similar_overlay_index = idx
        self._update_tile_region(idx)

    def _similar_button_rect(self, index: int) -> QRect | None:
        """Viewport rect of the 「◇」 overlay button on tile *index*, or ``None``.

        Delegates to :func:`painter.similar_seat` so the hit test always matches
        where the button is painted — including the drawn-image anchoring (#12):
        the painter seats the button on :func:`painter.drawn_image_rect`, so the
        hit test must use the same rect.  ``None`` when the tile has no
        laid-out box, or when the seat doesn't fit (#116 — a sliver tile's
        fixed-size seat would hit-test over the inter-tile gutter).
        """
        if not (0 <= index < len(self._layout.boxes)):
            return None
        box = self._layout.boxes[index]
        scroll_y = self.verticalScrollBar().value()
        img_rect = QRect(box.x, box.y - scroll_y, box.w, box.h)
        tile = self.tile_at(index)
        if tile is not None:
            img_rect = gvpaint.drawn_image_rect(img_rect, tile)
        if not gvpaint.similar_seat_fits(img_rect):
            return None  # sliver tile — no seat, no gutter misclicks (#116)
        return gvpaint.similar_seat(
            img_rect, seated=self._caption_overlay_active(),
        )

    # ----------------------------------------------------------- mouse

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._update_hover(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            # 描画は icon モード限定なので、当たり判定も同じゲートを通す
            # （非対称だと描かれていないボタンが押せてしまう）。
            seat = (
                self._similar_button_rect(self._similar_overlay_index)
                if self._view_mode == "icon"
                and self._similar_overlay_index is not None
                else None
            )
            outcome = gvinput.press_outcome(
                self._index_at_viewport_pos(pos),
                view_mode=self._view_mode,
                similar_overlay_index=self._similar_overlay_index,
                similar_hit=seat is not None and seat.contains(pos),
            )
            if outcome.similar_index is not None:
                self.similar_requested.emit(outcome.similar_index)
                event.accept()
                return
            if outcome.select_index is not None:
                self.select_index(outcome.select_index, emit=True, ensure=False)
            self._drag_start_pos = pos
            self._drag_index = outcome.select_index
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if not event.buttons():
            self._update_hover(
                self._index_at_viewport_pos(event.position().toPoint())
            )
        if (
            (event.buttons() & Qt.LeftButton)
            and self._drag_start_pos is not None
            and self._drag_index is not None
            and gvinput.drag_threshold_reached(
                self._drag_start_pos,
                event.position().toPoint(),
                QApplication.startDragDistance(),
            )
        ):
            index = self._drag_index
            self._drag_start_pos = None
            self._drag_index = None
            self._start_export_drag(index)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = None
            self._drag_index = None
        super().mouseReleaseEvent(event)

    def _start_export_drag(self, index: int) -> None:
        """Begin a file drag so the selected item can be dropped into other
        applications (Explorer, image editors, browsers, chat clients)."""
        tile = self.tile_at(index)
        if tile is None or tile.warn:
            # 実体の無い行（横断一覧の到達不能タイル）からは始めない —
            # 存在しないパスの file:// URL がドロップ先でエラーになる。
            return
        start_file_export_drag(self, tile.path, tile.pixmap)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            idx = self._index_at_viewport_pos(event.position().toPoint())
            if idx is not None:
                self.select_index(idx, emit=True, ensure=False)
                self.item_activated.emit(idx)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.reason() == QContextMenuEvent.Reason.Keyboard
            and self._selection.is_set
        ):
            # Shift+F10 / メニューキー: Qt はキーボード起動の pos に**フォーカス
            # ウィジェットの中央**を載せるので、当たり判定に掛けると「見えている
            # 中央のタイル」に効いてしまい、選択中の別タイルへ★が書かれる
            # （UIレビュー 2026-09-11 N-88 — 実機で誤爆した）。キーボードの対象は
            # 選択タイルで、メニューはそのタイルの上に出す。
            selected = self._selection.index
            self.ensure_visible(selected)
            rect = self._tile_viewport_rect(selected)
            global_pos = (
                self.viewport().mapToGlobal(rect.center()) if rect is not None
                else self.mapToGlobal(event.pos())  # event.pos() はウィジェット座標
            )
            self.context_menu_requested.emit(selected, global_pos)
            return
        # Unlike mouse events (delivered to the viewport), contextMenuEvent
        # arrives on the scroll area itself, so ``event.pos()`` is in WIDGET
        # coordinates — offset from the viewport by the frame width.  Map
        # through global → viewport so the hit test can't land one tile off
        # at cell boundaries (and stays correct if the frame ever widens).
        idx = self._index_at_viewport_pos(
            self.viewport().mapFromGlobal(event.globalPos())
        )
        self.context_menu_requested.emit(
            idx if idx is not None else -1, event.globalPos()
        )

    # ----------------------------------------------------------- providers

    def set_show_favorites(self, on: bool) -> None:
        """Toggle the bottom-right ``♡N`` favorite-count overlay (icon mode)."""
        on = bool(on)
        if on == self._show_favorites:
            return
        self._show_favorites = on
        self.viewport().update()

    def set_caption_band(self, on: bool) -> None:
        """Back the below-image caption strip with a surface-token band.

        Only meaningful in the legacy below-image caption mode (icon view with
        a reserved ``caption_height`` strip); the on-image scrim caption path
        ignores it.  Turned on by the post grid's 「画像の下に表示」 name
        placement so the tile reads as an image + label-plate card.
        """
        on = bool(on)
        if on == self._caption_band:
            return
        self._caption_band = on
        self.viewport().update()

    def set_curation_provider(self, provider) -> None:
        """Install a ``path -> (star, later)`` lookup for the curation badges.

        *provider* is a zero-cost synchronous callable (an in-memory dict
        lookup), so it is safe to call on the paint path.  ``None`` disables the
        star / "watch later" overlays.  Repaints the viewport so a just-changed
        star shows immediately.
        """
        self._curation_provider = provider
        self.viewport().update()

    def _curation_for(self, tile: "Tile") -> tuple[int, bool]:
        """Return ``(star, later)`` for *tile* via the provider (or ``(0, False)``)."""
        if self._curation_provider is None:
            return 0, False
        try:
            star, later = self._curation_provider(tile.path)
        except Exception:  # pragma: no cover (defensive)
            return 0, False
        return int(star or 0), bool(later)

    # ----------------------------------------------------------- painting

    def _caption_overlay_active(self) -> bool:
        """Whether icon tiles use the on-image seating chart (Phase 3-2).

        Triggered when the layout reserves NO caption strip in icon mode
        (``caption_height == 0``): the caption then rides the image bottom on a
        gradient scrim and the badges consolidate into the bottom-right seat.
        Only the main post grid opts in (it passes ``caption_height=0``); other
        icon views (folder-preview, the right file list in grid mode) keep the
        legacy below-image caption strip, so this stays a purely additive path.
        """
        return self._view_mode == "icon" and self._params.caption_height == 0

    def _paint_style(self) -> gvpaint.TileStyle:
        """1 フレームぶんの描画環境スナップショット（**唯一の組み立て点**）。

        パレット / フォント / 表示形式 / 選択・ホバーの添字 / 各種キャッシュを
        ここで 1 回だけ読む。タイルごとにウィジェットの getter を叩かないので、
        同じフレーム内で環境が食い違う経路が構造的に無くなる。
        """
        return gvpaint.TileStyle(
            palette=self.palette(),
            font=self.font(),
            fm=self.fontMetrics(),
            badge_font=self._view_badge_font(),
            badge_fm=self._view_badge_fm(),
            view_mode=self._view_mode,
            caption_height=self._params.caption_height,
            caption_band=self._caption_band,
            show_favorites=self._show_favorites,
            selection_active=self._selection_active(),
            selected_index=self._selection.index,
            hover_index=self._hover_index,
            similar_overlay_index=self._similar_overlay_index,
            spinner_check=self._spinner_check,
            spinner_angle=self._spinner_angle,
            curation=self._curation_for,
            dpr=self.devicePixelRatioF() or 1.0,
            qstyle=self.style(),
            elide_cache=self._elide_cache,
            placeholder_cache=self._placeholder_cache,
            scrim=self._scrim,
        )

    def paintEvent(self, event) -> None:  # type: ignore[override]
        # ``end()`` MUST run on every exit path: an early ``return`` (or an
        # exception from a per-tile painter) that leaves the QPainter active
        # makes Qt spam ``QBackingStore::endPaint() called with active
        # painter`` every frame AND aborts the paint mid-loop, so only the
        # tiles drawn before the fault appear.  ``try/finally`` guarantees the
        # painter is released; per-tile faults are isolated so one bad tile
        # can't blank the rest of the grid.
        painter = QPainter(self.viewport())
        try:
            painter.fillRect(event.rect(), self.viewport().palette().base())
            rng = self.visible_indices(buffer_rows=0)
            if rng is None:
                if not self._tiles and self._empty_message:
                    self._paint_empty_message(painter)
                return
            start, end = rng
            scroll_y = self.verticalScrollBar().value()
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            style = self._paint_style()
            paint_one = (
                gvpaint.paint_list_row if self._view_mode == "list"
                else gvpaint.paint_icon_cell
            )
            with measure("gallery_paint", f"{end - start + 1} tiles"):
                for i in range(start, end + 1):
                    if i >= len(self._layout.boxes):
                        break
                    box = self._layout.boxes[i]
                    tile = self._tiles[box.index]
                    try:
                        paint_one(painter, box, tile, scroll_y, style=style)
                    except Exception:  # pragma: no cover (defensive)
                        logger.exception(
                            "gallery tile paint failed (index={})", box.index
                        )
        finally:
            painter.end()

    def _paint_empty_message(self, painter) -> None:
        """Paint the settled empty-state icon + heading + body (#5 / redesign #3-4)."""
        empty_card.paint(
            painter,
            self._empty_text_layout(),
            palette=self.palette(),
            head_font=self._empty_heading_font(),
            body_font=self.font(),
            icon_name=self._empty_icon_name,
        )

    def _view_badge_font(self) -> QFont:
        """The shared badge-row font (shrunk 1pt, bold) for the current font.

        Cached: 席計算 (:func:`painter.icon_overlay_seats`) と描画
        (:func:`painter.paint_icon_seated`) が 1 タイル 1 フレームごとに使うので、
        毎回 :func:`badge_font` を作ると可視タイル数 × 3 個の ``QFont`` が
        フレーム単位で生まれていた。無効化は :meth:`changeEvent` の 1 か所。
        """
        if self._badge_font is None:
            self._badge_font = badge_font(self.font())
        return self._badge_font

    def _view_badge_fm(self) -> QFontMetrics:
        """The ``QFontMetrics`` of :meth:`_view_badge_font` (same cache rule).

        幅計算と描画が同じ計量器を通ることを構造的に保証する（別々に作ると
        測った幅と描いた幅がズレ得る）。
        """
        if self._badge_fm is None:
            self._badge_fm = QFontMetrics(self._view_badge_font())
        return self._badge_fm

    # -- 部品への薄い委譲（外から観測される従来名を保つための口） -------------

    def _icon_overlay_seats(self, img_rect: QRect, tile: Tile) -> IconSeats:
        """アイコン 1 タイルの四隅座席（:func:`painter.icon_overlay_seats`）。"""
        return gvpaint.icon_overlay_seats(img_rect, tile, style=self._paint_style())

    def _paint_icon_seated(
        self, painter, img_rect: QRect, index: int, tile: Tile,
    ) -> None:
        """座席モデルのオーバーレイ（:func:`painter.paint_icon_seated`）。"""
        gvpaint.paint_icon_seated(
            painter, img_rect, index, tile, style=self._paint_style(),
        )

    def _paint_list_trail(
        self, painter, text_rect: QRect, tile: Tile, selected: bool,
    ) -> QRect:
        """一覧行の右端席（:func:`painter.paint_list_trail`）。"""
        return gvpaint.paint_list_trail(
            painter, text_rect, tile, selected, style=self._paint_style(),
        )

    def _list_trail_kinds(self, tile: Tile) -> list[tuple[str, object]]:
        """一覧行の右端席に出す ``(kind, value)`` 列（無ければ空）。"""
        return gvpaint.list_trail_kinds(tile, style=self._paint_style())

    def _scrim_brush(self, height: int):
        """タイトル帯のスクリム勾配（:class:`painter.ScrimCache`）。"""
        return self._scrim.brush(height)

    def _placeholder_glyph(self, tile: Tile, rect: QRect) -> QPixmap | None:
        """プレースホルダの種別グリフ（:func:`placeholders.placeholder_glyph`）。"""
        return placeholders.placeholder_glyph(
            tile, rect,
            view_mode=self._view_mode,
            dpr=self.devicePixelRatioF() or 1.0,
            qstyle=self.style(),
            cache=self._placeholder_cache,
        )

    def _paint_caption(self, painter, rect: QRect, tile: Tile,
                       selected_active: bool, align: int,
                       *, selected: bool | None = None) -> None:
        """キャプション描画（:func:`captions.paint_caption`）。"""
        captions.paint_caption(
            painter, rect, tile, selected_active, align,
            palette=self.palette(), view_mode=self._view_mode,
            cache=self._elide_cache, selected=selected,
        )

    def _elide_two_lines(self, font, fm, text: str, width: int) -> str:
        """2 行上限の省略（:func:`captions.elide_two_lines`）。"""
        return captions.elide_two_lines(
            font, fm, text, width, cache=self._elide_cache,
        )

    def _title_rows(
        self, font, fm, text: str, first_w: int, last_w: int,
    ) -> tuple[str, ...]:
        """キャプションの行分割（:func:`captions.title_rows`）。"""
        return captions.title_rows(
            font, fm, text, first_w, last_w, cache=self._elide_cache,
        )

    _shape_two_lines = staticmethod(captions.shape_two_lines)
    _seated_title_color = staticmethod(gvpaint.seated_title_color)
    _drawn_image_rect = staticmethod(gvpaint.drawn_image_rect)
    _tile_awaits_thumb = staticmethod(tile_awaits_thumb)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange, QEvent.ThemeChange):
            self._placeholder_cache.clear()
            self._elide_cache.clear()
            # スタイル由来でウィジェットフォントが変わることがあるので、
            # FontChange と同じ無効化をここでも行う。
            self._invalidate_badge_font()
            self.viewport().update()
        elif event.type() == QEvent.FontChange:
            # Elision widths depend on the font metrics.
            self._elide_cache.clear()
            self._invalidate_badge_font()
            self.viewport().update()
        super().changeEvent(event)

    def _invalidate_badge_font(self) -> None:
        """バッジ行のフォント / 計量器キャッシュを捨てる（唯一の無効化点）。"""
        self._badge_font = None
        self._badge_fm = None


__all__ = ["GalleryView", "IconSeats", "Tile", "file_icon_bucket"]
