"""Center pane: scrollable image preview with LANCZOS rendering.

:class:`ImageView` decodes images on a worker thread (two-stage preview →
full), resamples with Pillow's LANCZOS, prefetches neighbouring siblings,
and supports animated GIF / WebP playback via ``QMovie``.

ここに残るのは**ウィジェットの状態機械**だけ — 3 段表示の進行、ストリームへの
投入と着地、オーバーレイ / クロームへの配線。部品は
:mod:`~snappix.viewer.image_view_parts`（幾何 = ``geometry``、入力の意図 =
``input``、先読み台帳 = ``prefetch_ledger``、ワーカー純関数 = ``workers``、
画像ラベル / 失敗カード / ミニマップ / 操作カプセル = ``canvas_label`` /
``error_card`` / ``minimap`` / ``control_bar``）。

このモジュールは ``__all__`` に列挙した名前を re-export するので、テストの
import 文は分割前と変わらない。ビューが直接呼ぶワーカー純関数はここのモジュール
グローバルなので ``monkeypatch.setattr(image_view, "_prefetch_decode", ...)``
は従来どおり効く。ただし**モジュールグローバルを差し替えるテストは移動先
モジュールへ当てること** — ``_MAX_TARGET_PIXELS`` / ``_clamp_pixel_budget`` は
:mod:`~snappix.viewer.image_view_parts.geometry` の値を写した別名で、ここを
差し替えても実体（``geom`` 経由で読む）には届かない。部品どうしの呼び出し
（``workers`` 内の相互参照など）も同じで、当てる先は移動先モジュール。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, assert_never, cast

from loguru import logger
from PIL import Image
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QMimeData,
    QPoint,
    QRect,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QImage,
    QKeySequence,
    QMovie,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QScrollArea,
    QWidget,
)

from . import view_prefs
from ..common.i18n import t
from ..common.touch import enable_touch_scroll
from ..common.ui import show_toast
from ..common.ui.timers import DebounceMode, Debouncer
from .context_menus import (
    EntryMenuContext,
    append_entry_verbs,
    curation_hooks_from_ancestors,
)
from .drag_export import start_file_export_drag
from .edge_nav import route_scroll_or_navigate
from .folder_scan import IMAGE_SUFFIXES
from .image_cache import (
    IMAGEVIEW_CACHE_MAX_BYTES,
    IMAGEVIEW_CACHE_MAX_ENTRIES,
    IMAGEVIEW_CACHE_MAX_SINGLE_BYTES,
    IMAGEVIEW_PREFETCH_RADIUS,
    BoundedImageCache,
)
from .image_scale import pil_to_qimage, pil_to_qpixmap
from .image_view_parts import geometry as geom
from .image_view_parts import input as ivinput
from .image_view_parts.canvas_label import _ImageCanvasLabel
from .image_view_parts.control_bar import (
    ZOOM_READOUT_EMPTY,
    _CTRL_GLYPHS,
    _CTRL_ICON_COLOR,
    _ControlBar,
    _ctrl_icon,
    _ZoomOverlayLabel,
    format_zoom_readout,
)
from .image_view_parts.error_card import _DecodeErrorCard
from .image_view_parts.geometry import (
    MAX_TARGET_PIXELS as _MAX_TARGET_PIXELS,
)
from .image_view_parts.geometry import (
    clamp_pixel_budget as _clamp_pixel_budget,
)
from .image_view_parts.minimap import _MinimapOverlay
from .image_view_parts.movie import MoviePlayback, open_movie, orient_frame, oriented_size, render_frame
from .image_view_parts.prefetch_ledger import PrefetchLedger
from .image_view_parts.workers import (
    _is_animated_bytes,
    _lanczos_full,
    _load_image,
    _LoadKind,
    _patch_resample,
    _pil_sizeof,
    _prefetch_decode,
    _ScaleKind,
    PrefetchMiss,
)
from .pending import MARK, OneShot, Pending, always
from .qimage_decode import read_file_bytes
from ._runnable import GuardedStream, StreamOutcome

if TYPE_CHECKING:
    from .state import ViewerState


class ImageView(QScrollArea):
    """Scrollable image viewer with fit / 1:1 / wheel-zoom.

    Image decoding runs on a :class:`QThreadPool` worker so the GUI thread
    stays responsive while a large image is being read and converted to
    ``QPixmap`` on receipt.  A monotonic *token* is bumped on every
    :meth:`show_image` call so stale results from superseded loads are
    dropped silently.

    Rendering uses Pillow's ``LANCZOS`` resampler for every downscale.
    This is ~10x slower than Qt's built-in bilinear but preserves fine
    lines, dots, and checker patterns that the Qt path aliases into
    block noise.  A short debounce coalesces resize / wheel bursts so
    dragging the window edge doesn't queue up LANCZOS jobs per frame.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)
    # Fires whenever the PIL cache's key set changes.  The file list
    # listens so it can toggle per-item "not cached yet" spinners on
    # prefetch completion / sibling-cache invalidation.
    cache_updated = Signal()
    # Right-click "この画像に類似を検索" on the currently-previewed image
    # (C-10 extension).  Carries the image path and a ~160px scaled QPixmap
    # of the currently displayed image (or None) so the left pane's seed
    # preview has something to show.  Only emitted when similar search is
    # available (VectorIndex present) — see ``set_similar_search_available``.
    similar_search_requested = Signal(Path, object)
    # Emitted when the user toggles either context-menu checkbox.  Wiring
    # these to persisted settings is left to a later phase — ImageView
    # itself only holds the in-memory flag and repaints accordingly.
    zoom_persist_toggled = Signal(bool)
    minimap_toggled = Signal(bool)
    # Context-menu 「全画面で表示 (F11)」 (閲覧モード entry point).  Only
    # offered when ``set_fullscreen_available(True)`` was called — the main
    # window enables it on the centre preview; the lightbox's own internal
    # ImageView keeps the default False (it is already fullscreen).
    fullscreen_requested = Signal()
    # Full (native) image dimensions of the currently-loaded image, emitted
    # on every successful load and reset to (0, 0) when the view is cleared.
    # The status bar shows "W×H" for the previewed image; a non-image page
    # clears it via the (0, 0) emit.
    image_info_changed = Signal(int, int)
    # Digit 0–5 pressed with no modifier while this view has focus — the star
    # rating for the shown image (the stage must accept the same star keys
    # as the grid / lightbox).  The host persists it; the view
    # itself stays user_meta-agnostic.  In the lightbox this signal is simply
    # left unconnected (its own key filter already handles digits first).
    star_key_requested = Signal(int)
    # Split-view redesign 2026-07: double-click on the image while the split
    # view is showing means "make the preview big" (maximise the preview
    # column), not the historical fit⇄actual zoom toggle — that toggle only
    # applies while the preview is already maximised.  The host flips the
    # behaviour with ``set_double_click_maximize``; when enabled the
    # double-click emits this instead of zooming.  The lightbox's internal
    # ImageView keeps the default (zoom toggle).
    maximize_requested = Signal()
    # Effective zoom changed, in percent (100.0 = actual size).  Fired from
    # every path that alters the on-screen scale — wheel-zoom, ± keys, the
    # double-click / control-bar fit⇄actual toggles, display rotation, and
    # the fit scale resolving when an async decode (or GIF setup) lands.
    # Hosts with their own zoom readout (the lightbox capsule) listen here
    # instead of polling ``_effective_zoom``.
    zoom_changed = Signal(float)
    # The decode of this path failed and the error card is now showing.
    # Hosts that picked the path *for* the user (the centre pane's folder
    # representative image) use it to fall back to the next candidate
    # instead of leaving an error card on a folder that has readable
    # images.  Purely informational — the error
    # card is already up when this fires.
    load_failed = Signal(Path)

    # Coalesce resize / wheel bursts.  LANCZOS on a 4K image takes
    # ~40–80 ms; running it per raw resize event would feel laggy while
    # the user drags a window edge.
    _REFRESH_DEBOUNCE_MS = 60

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Initialise the animated-GIF state *before* anything that might
        # trigger our overridden ``eventFilter`` — ``setWidget`` below
        # registers child-event monitoring on the scroll area, which
        # routes events through this filter while ``_movie`` would still
        # be uninitialised if we set it later in ``__init__``.
        self._movie: QMovie | None = None
        # QMovie が読む QBuffer とバイト列（QMovie より長生きさせる — 解放は
        # ``_clear_movie``。理由は ``image_view_parts.movie.open_movie``）。
        self._movie_buffer: QBuffer | None = None
        self._movie_bytes: QByteArray | None = None
        self._is_gif: bool = False
        # 可視領域パッチ描画に対応したラベル。通常は素の
        # QLabel として振る舞い、非フィットの巨大ズーム時のみ canvas モード。
        self._label = _ImageCanvasLabel()
        self._label.setAlignment(Qt.AlignCenter)
        # Mouse tracking so plain hover motion (no button held) reveals the
        # on-screen control bar via ``eventFilter`` — see ``_ControlBar``.
        self._label.setMouseTracking(True)
        self._label.setBackgroundRole(self.backgroundRole())
        self.setWidget(self._label)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignCenter)
        enable_touch_scroll(self)

        self._original: Image.Image | None = None
        # Full-resolution QPixmap cached alongside the PIL source so the
        # fast Qt preview during wheel-zoom scales from the original every
        # time (avoids cascaded-scale blur while LANCZOS is still pending).
        self._original_pixmap: QPixmap | None = None
        # Path of the currently shown image — drives prefetch scheduling.
        self._current_path: Path | None = None
        self._zoom = 1.0
        self._fit_mode = True

        # 「いま何枚目を表示しているか」の通し番号。着地の選別には使わない
        # （それはストリームの仕事）— 画像ごとに 1 回だけ作る派生ピクスマップ
        # （パッチモードの下敷き ``_patch_base`` / ミニマップ）が「自分は今の
        # 画像のものか」を言うための印。
        self._image_serial = 0
        # 表示中の画像のデコード。投入は superseding（``submit``）— 素早い
        # 矢印送りで中間の画像まで全部デコードしないよう、キュー済みは捨て、
        # 走行中も ``job.cancel`` を見て降りる。1 スレッド。
        self._load_stream = GuardedStream(self)
        self._load_stream.bind(self._on_load_landed)
        self._load_stream.bind_progress(self._on_preview_landed)
        # Paths whose worker-side animation probe already classified them as
        # static (single-frame WebP etc.) — revisits skip the probe and take
        # the PIL-cache fast path directly.  Cleared with the sibling cache.
        self._known_static: set[str] = set()

        # LANCZOS リサンプル（全体レンダ）と可視領域パッチレンダの**共有**
        # ストリーム。1 本に載せるのはモード切替を跨いだ相互
        # 無効化のため — どちらの再レンダでも先行が追い越され、切替前の結果は
        # 必ず落ちる。1 スレッド・superseding なので、ズーム連打では最新の
        # ターゲットだけが実際に計算される。
        self._scale_stream = GuardedStream(self)
        self._scale_stream.bind(self._on_scale_landed)

        # --- 可視領域パッチレンダの状態 ------------------------------
        # ここで持つのは「最後にレンダを発行したパッチの論理矩形と DPR」だけ
        # — スクロール / リサイズ時に可視域がパッチから食み出したかの判定
        # （``_patch_stale``）に使う。下敷きピクスマップは画像ごとに 1 回だけ
        # 作ってキャッシュ（ミニマップの ``_minimap_pixmap_source`` と同じ
        # トークン方式）。
        self._patch_rect = QRect()
        self._patch_dpr = 0.0
        self._patch_base: QPixmap | None = None
        self._patch_base_source: int = -1

        self._refresh_timer = Debouncer(
            self,
            self._REFRESH_DEBOUNCE_MS,
            self._refresh,
            mode=DebounceMode.LEADING_WINDOW,
        )

        # Bounded cache of decoded PIL images, keyed by absolute path str.
        # Lets arrow-key / wheel navigation between sibling image files
        # skip the decode pass when the neighbor was pre-fetched below,
        # and survives small zoom bursts without re-reading the file.
        self._pil_cache: BoundedImageCache[Image.Image] = BoundedImageCache(
            max_bytes=IMAGEVIEW_CACHE_MAX_BYTES,
            max_entries=IMAGEVIEW_CACHE_MAX_ENTRIES,
            max_single_bytes=IMAGEVIEW_CACHE_MAX_SINGLE_BYTES,
            sizeof=_pil_sizeof,
        )
        # 現在の件数上限のローカル写し（``reconfigure_cache`` で更新）。
        # ``_schedule_prefetch`` が「構造上キャッシュに載り得ないターゲット」
        # を発行前にふるうのに使う（キャッシュ側の内部値を掘らない）。
        self._cache_max_entries = IMAGEVIEW_CACHE_MAX_ENTRIES
        # Provider returns (image_siblings, index_of_anchor) for the given
        # path.  Injected by :class:`ViewerWindow` so ImageView stays free
        # of file-list coupling; ``None`` means no prefetch is attempted.
        self._siblings_provider: (
            Callable[[Path], tuple[list[Path], int]] | None
        ) = None
        # Provider returns an already-decoded thumbnail ``QPixmap`` for a
        # path (or ``None``).  Injected by :class:`ViewerWindow` so the
        # preview can paint the file-list thumbnail as an instant, blurry
        # placeholder while the full decode runs — no extra I/O, the
        # pixmap is already resident in the right pane.
        self._thumbnail_provider: Callable[[Path], QPixmap | None] | None = None
        # 近傍の先読み。投入は**加算的**（``submit_batch``）— 1 つの近傍集合
        # は複数件をまとめて積むので、後続の投入が先行を捨ててはならない。
        # 近傍集合の入れ替えでもストリームは畳まない（台帳の ``wanted`` と
        # ``inflight`` で振るう — :meth:`_schedule_prefetch`）。畳むのは
        # フォルダ切替 / クリアだけ。1 スレッド: 先読みは best-effort の背景
        # 埋めなので、表示中の画像のデコードと競らせない。
        self._prefetch_stream = GuardedStream(self)
        self._prefetch_stream.bind(self._on_prefetch_landed)
        # Runtime-configurable number of neighbors to prefetch per side.
        self._prefetch_radius = IMAGEVIEW_PREFETCH_RADIUS
        # 先読みの台帳（距離認識の挿入ガード / 引き継ぎラッチ / デコード窓）。
        # ワーカー側の早期降車と着地側の振るいが**同じメソッド**
        # （``PrefetchLedger.wanted``）を引くので、片側だけずれない。
        self._prefetch = PrefetchLedger()

        # Every ImageView shortcut is scoped to this widget subtree.  With
        # the default ``WindowShortcut`` context the Ctrl+0 / Ctrl+1 / Ctrl+C
        # trio would fire from anywhere in the window whenever the image page
        # happens to be visible — Ctrl+C in the grid or nav rail would
        # silently overwrite the clipboard with the preview image, and a
        # grid-side Ctrl+C would collide into an ambiguous-shortcut deadlock
        # (``main_window`` treats Ctrl+C as an ImageView-focus QShortcut).  The bare "+" / "-" / "R" / "F" keys
        # need the same scope so they never fire while the user is typing in
        # the left-pane filter box and never collide with the grid's digit
        # star shortcuts (those are handled in GalleryView.keyPressEvent).
        for seq, slot in (
            (QKeySequence("Ctrl+0"), self._set_actual_size),
            (QKeySequence("Ctrl+1"), self._fit_to_window),
            (QKeySequence("Ctrl+C"), self.copy_image_to_clipboard),
            (QKeySequence("+"), self._zoom_in),
            (QKeySequence("-"), self._zoom_out),
            (QKeySequence("R"), self.rotate_right),
            (QKeySequence("Shift+R"), self.rotate_left),
            (QKeySequence("F"), self.flip_horizontal),
        ):
            sc = QShortcut(seq, self, slot)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

        # Animated GIF support — clicks on the label toggle play/pause
        # via :meth:`eventFilter`.  ``_movie`` / ``_is_gif`` are
        # initialised at the top of ``__init__`` because ``setWidget``
        # may invoke our filter before this point.
        self._label.installEventFilter(self)
        # Drag-to-export: armed on a left press over the label, fires once
        # the cursor passes the platform drag threshold (see eventFilter).
        self._drag_start_pos: QPoint | None = None
        # Drag-to-pan: when the image is zoomed past the viewport a left
        # drag scrolls within it instead of starting an export drag.
        # ``_pan_last`` holds the previous *global* cursor position so the
        # delta is immune to the label shifting under the cursor as we
        # scroll.
        self._panning: bool = False
        self._pan_last: QPoint | None = None

        # --- Zoom-persist toggle -------------------------------------
        # When ON, ``show_image`` restores the previous zoom/fit state and
        # scroll-fraction instead of always resetting to fit-to-window.
        self._zoom_persist: bool = False
        # Snapshot taken just before switching to a new image (see
        # ``show_image``): (fit_mode, zoom, scroll_fx, scroll_fy).
        self._saved_fit_mode: bool = True
        self._saved_zoom: float = 1.0
        self._saved_scroll_fx: float = 0.5
        self._saved_scroll_fy: float = 0.5
        # 立っている間だけ、次に読み終わった画像へ上の snapshot を当てる
        # one-shot（当てた時点で降ろす）。
        self._pending_restore: OneShot = Pending()

        # --- デコード失敗カード ----------------------------------------
        # ビューポートを覆う EmptyStateCard。失敗時だけ現れ、次の表示要求 /
        # 成功で消える。中央プレビューでも閲覧モードでも同じ面が出る。
        self._error_card = _DecodeErrorCard(self.viewport())
        self._error_card.reload_requested.connect(self._on_error_card_reload)
        self._error_card.open_default_requested.connect(
            self._on_error_card_open_default
        )

        # --- Zoom overlay ----------------------------------------------
        self._zoom_overlay = _ZoomOverlayLabel(self.viewport())

        # --- Minimap overlay ---------------------------------------------
        self._minimap_enabled: bool = True
        self._minimap = _MinimapOverlay(self.viewport())
        self._minimap.panned.connect(self._on_minimap_panned)
        # Cached small pixmap used as the minimap's thumbnail — built once
        # per loaded image (scaled from ``_original_pixmap``/preview, never
        # re-decoded from disk) and invalidated whenever the source changes.
        self._minimap_pixmap: QPixmap | None = None
        self._minimap_pixmap_source: int = -1  # token that built it

        # --- On-screen control bar (F02) ---------------------------------
        # Hover-revealed prev / next / fit-actual / fullscreen + zoom label,
        # so the fullscreen entry point isn't F11-only and the fit / actual
        # toggle is discoverable without the context menu.
        # When False, hover never reveals the bar (閲覧モード reuses this
        # ImageView but supplies its own chrome — see set_control_bar_enabled).
        self._control_bar_enabled = True
        self._control_bar = _ControlBar(self.viewport())
        # ツールチップは軸を名指す専用文言 — ラベル「前へ / 次へ」は
        # ステージヘッダーのボタンラベルと共有されているため、そのまま
        # ツールチップに流用すると「同形・同文言で別の軸」になる。
        # キーの併記は表（``shortcuts_dialog.SHORTCUTS``）から引く — 手書きで
        # 併記すると表とずれる。
        from .shortcuts_dialog import with_key_hint

        step = "viewer.shortcuts_dialog.desc_stage_step_image"
        fit = "viewer.shortcuts_dialog.desc_middle_click_fit"
        self._control_bar.set_tooltips(
            with_key_hint(t("viewer.image_view.ctrl_prev_tooltip"), step),
            with_key_hint(t("viewer.image_view.ctrl_next_tooltip"), step),
            with_key_hint(t("viewer.image_view.ctrl_fit_actual"), fit),
            t("viewer.image_view.ctrl_fullscreen"),
        )
        self._control_bar.set_fullscreen_visible(False)
        self._control_bar.prev_clicked.connect(
            lambda: self.navigate_requested.emit(-1, True)
        )
        self._control_bar.next_clicked.connect(
            lambda: self.navigate_requested.emit(1, True)
        )
        self._control_bar.fit_toggle_clicked.connect(self._toggle_fit_actual)
        self._control_bar.fullscreen_clicked.connect(
            self.fullscreen_requested.emit
        )

        # --- Non-destructive orientation (要件 F11) -----------------------
        # 注: この「F11」は要件番号であって**キーの F11（閲覧モード）とは
        # 無関係** — 紛らわしいので以降は「要件 F11」と書く。
        # Display-only rotation (0/90/180/270, clockwise) + horizontal flip.
        # Applied to a pristine ``_base_original`` to rebuild ``_original`` /
        # ``_original_pixmap``; never written to disk.  Reset on every image.
        self._base_original: Image.Image | None = None
        self._rotation = 0
        self._flip_h = False

        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

        # Whether the "この画像に類似を検索" context-menu item is offered
        # (C-10 extension).  ``ViewerWindow`` sets this True only when a
        # VectorIndex is loaded — default False hides the item on installs
        # without semantic vectors, matching FileListView's contract.
        self._similar_search_available = False
        # Whether the 「全画面で表示 (F11)」 context-menu item is offered
        # (see ``fullscreen_requested``).  Default False.
        self._fullscreen_available = False
        # 全画面（ライトボックス）の中の ImageView か。
        # 真なら右クリックに「入口」ではなく**出口**を出す。
        self._fullscreen_exit_mode = False
        # ホバーカプセルの全画面ボタンを出すか —
        # 最大化中はヘッダーの [⛶ 全画面 (F11)] と重複するのでホストが畳む。
        # メニュー側の可否 (``_fullscreen_available``) とは独立。
        self._fullscreen_button_visible = True
        # Whether a still-image double-click emits ``maximize_requested``
        # instead of the fit⇄actual zoom toggle (split-view redesign — see
        # ``set_double_click_maximize``).  Default False (zoom toggle) so
        # the lightbox's internal ImageView keeps its behaviour.
        self._double_click_maximize = False
        # QMovie の再生 / 一時停止と「誰が決めたか」の印（退避の復路・
        # ダブルクリック最大化の打ち消し — ``MoviePlayback`` の docstring）。
        self._playback = MoviePlayback()

    # ------------------------------------------------------------------ API

    @property
    def _prefetch_protect(self) -> dict[str, tuple[str, ...]]:
        """先読み台帳の距離認識ガード（分割前の名前で読み書きする口）."""
        return self._prefetch.protect

    @_prefetch_protect.setter
    def _prefetch_protect(self, value: dict[str, tuple[str, ...]]) -> None:
        self._prefetch.protect = dict(value)

    def set_siblings_provider(
        self,
        provider: Callable[[Path], tuple[list[Path], int]] | None,
    ) -> None:
        self._siblings_provider = provider

    def set_thumbnail_provider(
        self,
        provider: Callable[[Path], QPixmap | None] | None,
    ) -> None:
        """Inject a path→thumbnail-pixmap lookup for placeholder display.

        ``None`` disables placeholders — ``show_image`` then falls back to
        the textual "読み込み中…" notice while decoding.
        """
        self._thumbnail_provider = provider

    def set_zoom_persist(self, on: bool) -> None:
        """Enable/disable carrying zoom + scroll position across images.

        When enabled, the *current* on-screen state (fit/actual/custom
        zoom + scroll fraction) is captured on every ``show_image`` call
        and re-applied once the newly-loaded image lands, instead of
        always resetting to fit-to-window.
        """
        self._zoom_persist = bool(on)

    def zoom_persist(self) -> bool:
        return self._zoom_persist

    def set_minimap_enabled(self, on: bool) -> None:
        self._minimap_enabled = bool(on)
        self._update_minimap_visibility()

    def minimap_enabled(self) -> bool:
        return self._minimap_enabled

    def refresh_fit(self) -> None:
        """フィット表示中なら現在の設定でフィットを描き直す.

        F03「フィット表示: 等倍以上に拡大しない」（``view_prefs`` の
        ``image_fit_no_upscale``）はフィット計算時にしか読まれないモジュール
        変数なので、明示的に描き直さないと設定ダイアログで切り替えても
        **表示中の画像は次のリサイズ / 画像切替まで古いまま**になる（同じ
        設定グループに並ぶミニマップは ``apply_state`` 経由で即時反映される
        ので、隣り合う行で挙動が割れる）。``apply_state`` がこれを呼ぶことで、両 ImageView
        インスタンスへ自動的に届く。

        非フィット時・画像未ロード時は何もしない（ユーザーが決めた倍率を
        設定適用が勝手に動かさないため）。GIF/WebP は ``_apply_movie_scale``
        を通す ``_refresh`` 経路に乗る。
        """
        if not self._fit_mode or not self.has_image():
            return
        self._refresh()
        self._sync_zoom_readout()

    def set_similar_search_available(self, available: bool) -> None:
        """Enable/disable the context-menu "この画像に類似を検索" entry (C-10).

        Wired by :class:`ViewerWindow` to the presence of a VectorIndex —
        with no vectors the similar search would be a no-op, so the item
        is hidden.  Mirrors :meth:`FileListView.set_similar_search_available`.
        """
        self._similar_search_available = bool(available)

    def set_fullscreen_available(self, available: bool) -> None:
        """Offer 「全画面で表示 (F11)」 in the context menu (閲覧モード).

        Wired by :class:`ViewerWindow` (via ``ContentView``) on the centre
        preview only; the fullscreen lightbox's internal ImageView leaves
        it off.  Selecting the item emits :attr:`fullscreen_requested`.
        """
        self._fullscreen_available = bool(available)
        self._control_bar.set_fullscreen_visible(
            self._fullscreen_available and self._fullscreen_button_visible
        )

    def set_fullscreen_exit_mode(self, on: bool) -> None:
        """全画面の中の ImageView として、右クリックに**出口**を出す.

        ライトボックスの出口は Esc / F11 / 上部バーの閉じるだけで、どれも
        右クリックからは見えないため、メニューにも出口を置く。``set_fullscreen_available(True)`` を流用しないのは、
        あちらがホバーカプセルの全画面**ボタン**まで点けてしまい、
        ``set_control_bar_enabled(False)`` による暗黙の無効化に依存する形に
        なるため。行き先は同じ :attr:`fullscreen_requested`（ホストが閉じる）。
        """
        self._fullscreen_exit_mode = bool(on)

    def set_fullscreen_button_visible(self, visible: bool) -> None:
        """ホバーカプセルの全画面ボタンだけを出し入れする.

        プレビュー最大化中はヘッダー右端に ``[⛶ 全画面 (F11)]`` が常設される
        ため、カプセル側の同機能ボタンは重複（隣り合うフィットボタンとの
        取り違えも起きる）。**右クリックメニューの「全画面で表示」は残す**
        ので、入口の総数は減らさずに視覚的な重複だけを消す。
        """
        self._fullscreen_button_visible = bool(visible)
        self._control_bar.set_fullscreen_visible(
            self._fullscreen_available and self._fullscreen_button_visible
        )

    def set_double_click_maximize(self, enabled: bool) -> None:
        """Route a still-image double-click to :attr:`maximize_requested`.

        Split-view redesign 2026-07: while the [grid | preview] split is
        showing, double-clicking the previewed image means "make it big"
        (maximise the preview column).  While the preview is already
        maximised — and in the lightbox's internal ImageView (never
        enabled) — the double-click keeps its historical fit⇄actual zoom
        toggle.  The host flips this on split⇄maximise transitions.
        """
        self._double_click_maximize = bool(enabled)

    def set_control_bar_enabled(self, enabled: bool) -> None:
        """Enable / disable the hover-revealed on-screen control bar (F02).

        閲覧モード (:class:`~snappix.viewer.lightbox.LightboxWindow`) reuses a
        private ImageView but draws its own immersive chrome (prev/next,
        counter, filmstrip).  Leaving F02's ``_ControlBar`` active there makes
        it double up with the lightbox chrome, so the lightbox disables it.
        When disabled the bar is hidden and hover no longer reveals it.
        """
        self._control_bar_enabled = bool(enabled)
        if not self._control_bar_enabled:
            self._control_bar.hide()

    def enable_stage_background(self) -> None:
        """Paint the surround + hairline-frame the image with ``bg_stage``.

        ステージモード: the centre preview shows
        the image on a dedicated backdrop deeper than any chrome surface so it
        reads as *exhibited*.  Tags **this scroll area** and the image label
        with the object names the ``QAbstractScrollArea#stageImageArea`` /
        ``QLabel#stageImageLabel`` QSS rules (qss.py) target.  Only the main
        window's central ImageView opts in; the fullscreen lightbox reuses
        ImageView with its own fixed dark chrome and leaves this off.

        The surround is tagged on the scroll area, not on its viewport: a
        viewport-targeted ``QWidget#id`` rule does NOT paint a QScrollArea
        viewport even with ``WA_StyledBackground``, which would leave every
        theme showing ``bg_window`` behind the image.  The viewport keeps its
        own name + ``WA_StyledBackground`` purely as a "this view is staged"
        marker; the paint comes from the scroll-area rule, which Qt routes to the viewport.
        """
        self.setObjectName("stageImageArea")
        viewport = self.viewport()
        viewport.setObjectName("stageImageViewport")
        viewport.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._label.setObjectName("stageImageLabel")
        # Re-polish so the freshly-set object names pick up the app QSS even
        # when the theme was already applied before this widget was built.
        for w in (self, viewport, self._label):
            w.style().unpolish(w)
            w.style().polish(w)

    def force_show_control_bar(self) -> None:
        """Force the on-screen control capsule fully visible (test / shoot).

        The capsule (:class:`_ControlBar`) normally fades in on hover and
        auto-hides on idle — neither drivable without a real event loop.
        Offscreen tests and screenshot tooling use this to pin it
        on screen.  No-op when the bar is disabled (閲覧モード).
        """
        if self._control_bar_enabled:
            self._control_bar.set_anchor_rect(self._content_anchor_rect())
            self._control_bar.force_show()

    def copy_image_to_clipboard(self) -> None:
        """Copy the current image (pixel data + file URL) to the clipboard.

        Places both an image (``QImage``, full resolution when available)
        and a file URL on the clipboard so paste targets that only accept
        one or the other (e.g. an image editor vs. a file manager/chat
        client) both work from a single Ctrl+C.

        成功トーストは**ここ 1 箇所**で出す: クリップボードは不可視なので
        通知が唯一の成功確認手段であり、3 入口（メニュー / Ctrl+C / 右クリック）
        の全てで揃える必要がある。内側に置くことで、将来入口が増えても
        自動的に一貫する。
        """
        path = self._current_path
        if path is None:
            return
        # デコード失敗中（失敗カード表示中）は ``_current_path`` だけが残る
        # ので、画素が無い状態で URL だけを載せて成功トーストを出しかねない。
        # 画素が無いなら何も載せず、``_on_copy_current_image``
        # と同じ「コピーできる画像がありません」を返す。
        if not self.has_image():
            show_toast(self, t("viewer.main_window.no_copyable_image"), "info")
            return
        image: QImage | None = None
        if self._original is not None:
            try:
                # QPixmap を挟まない: クリップボードに載せる
                # のは QImage なので、原寸ぶんのプラットフォームサーフェス確保
                # と 2 回目の全画素コピーは丸ごと無駄になる。
                image = pil_to_qimage(self._original)
            except Exception as exc:  # pragma: no cover (defensive)
                logger.debug("copy_image_to_clipboard: PIL conversion failed: {}", exc)
                image = None
        if image is None and self._original_pixmap is not None:
            image = self._original_pixmap.toImage()
        if image is None:
            image = self._movie_frame()
        mime = QMimeData()
        if image is not None and not image.isNull():
            mime.setImageData(image)
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        QApplication.clipboard().setMimeData(mime)
        show_toast(self, t("viewer.main_window.image_copied"), "success")

    def is_cached(self, path: Path) -> bool:
        """True when the PIL source for *path* is resident in the LRU."""
        return str(path) in self._pil_cache

    def is_decode_pending(self, path: Path) -> bool:
        """True when *path*'s decode is plausibly in flight right now.

        「LRU に載っているか」(:meth:`is_cached`) との違いが要点: LRU は
        選択中 ± 先読み半径しか保持しないので、それより外の画像は**何も
        起きていない**のに恒久的に「未キャッシュ」になる。右ペインの pending
        スピナーがそれを「読み込み中」として描くと、先読み半径より多い画像を
        持つ普通のフォルダで大半の行が回りっぱなしになり、``_has_spinner_tile``
        のアイドルガードごと無効化される。進行中と言えるのは
        「直近の表示要求 / 先読み発行の窓の中で、まだ載っていない」ものだけ。
        """
        key = str(path)
        return self._prefetch.pending(key) and key not in self._pil_cache

    def _settle_decode(self, path: Path | None) -> None:
        """決着したパスを「デコード進行中」の窓から外す（台帳へ委譲）。"""
        self._prefetch.settle(path)

    def has_image(self) -> bool:
        """True when a (still / animated) image is currently loaded.

        Used by callers that want to know whether ``copy_image_to_clipboard``
        would actually copy something before offering the action — so this
        must track the *pixel data*, not just the requested path.
        ``_apply_failed`` deliberately keeps ``_current_path`` (失敗カードの
        [再読み込み] が使う) while dropping ``_original``; keying off the path
        alone would report "コピーしました" for a failed decode that put
        nothing on the clipboard.
        """
        if self._current_path is None:
            return False
        return (
            self._original is not None
            or self._original_pixmap is not None
            or (self._is_gif and self._movie is not None)
        )

    def invalidate_sibling_cache(self) -> None:
        """Drop all prefetched images — call when the viewer folder changes.

        The active image's PIL source is also gone by design: keeping it
        would be pointless (the user moved to a different folder) and
        confuses memory accounting.  Queued prefetches are dropped and the
        running one's session is tripped, so it bails before caching its
        result.
        """
        self._prefetch_stream.cancel()
        # Cross-folder adoption would resurrect a stale decode — drop the
        # latch and the (path-keyed) insertion guards along with the cache.
        self._prefetch.reset()
        self._pil_cache.clear()
        self._known_static.clear()
        self.cache_updated.emit()

    def reconfigure_cache(
        self,
        *,
        max_bytes: int,
        max_entries: int,
        max_single_bytes: int,
        prefetch_radius: int,
    ) -> None:
        """Apply new cache limits + prefetch radius.

        Evicted entries are simply dropped — ``ImageView`` has no
        resource-cache to sync, so evictions are lossless from the
        viewer's perspective (a revisit will re-decode).  The prefetch
        radius clamps to ``[0, 32]`` to keep the UI behavior sensible;
        0 disables prefetch entirely.
        """
        self._pil_cache.reconfigure(
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_single_bytes=max_single_bytes,
        )
        self._cache_max_entries = max(1, int(max_entries))
        self._prefetch_radius = max(0, min(32, int(prefetch_radius)))
        self.cache_updated.emit()

    def show_image(self, path: Path) -> None:
        # 新しい表示要求 = 失敗カードは畳む（成功すれば出番なし / 再度失敗
        # すれば ``_apply_failed`` が出し直す）。
        self._error_card.hide()
        self._label.show()  # 失敗時に隠したラベルを戻す
        # 前画像のパッチモード痕跡（canvas / 下敷き）は持ち越さない。
        self._reset_patch_state()
        # Snapshot the outgoing image's on-screen state *before* anything
        # below resets ``_fit_mode`` / ``_zoom`` — ``_apply_loaded`` /
        # ``_try_show_gif`` both force fit-to-window unconditionally, so
        # this is the only point where the previous state is still live.
        if self._zoom_persist and (self._original is not None or self._is_gif):
            self._saved_fit_mode = self._fit_mode
            self._saved_zoom = self._zoom
            self._saved_scroll_fx, self._saved_scroll_fy = self._scroll_fraction()
            self._pending_restore.set(MARK)
        self._image_serial += 1
        # Drop the scale work for the outgoing image, and anything queued on
        # the decode stream — rapid scroll through a folder would otherwise
        # pile up decodes for every intermediate file and the target image
        # would sit behind them waiting its turn.  走行中も ``job.cancel``
        # を見て降りる。
        self._scale_stream.cancel()
        self._load_stream.cancel()
        # 先読みは**ここでは畳まない**。新しい近傍集合は着地後の
        # ``_schedule_prefetch`` が決める。代わりに台帳を張り替える: 先読みが「まだ要る」と言えるのは
        # この 1 枚だけ（``_prefetch_protect`` は空 = 前の近傍集合は用済み）で、
        # 走行中のワーカーは ``_prefetch_wanted`` 越しにそれを読んで降りる。
        # 唯一残すのがこのパス — 単一の先読みワーカーがちょうどこのファイルを
        # デコード中なら、それが全解像度表示への最短経路なので殺さない
        # （``_on_prefetch_landed`` が結果を採用して、重複する本デコードを畳む）。
        # 対象は必ずデコード窓の中に入る。近傍は ``_schedule_prefetch`` が
        # 着地後に足す。
        self._prefetch.begin_show(path)
        self._original = None
        self._original_pixmap = None
        self._current_path = path
        # Display-only orientation resets on every new image (要件 F11).
        self._base_original = None
        self._rotation = 0
        self._flip_h = False
        self._refresh_timer.stop()
        # New image → the cached minimap thumbnail (if any) is stale.
        self._minimap_pixmap = None
        self._minimap_pixmap_source = -1
        self._minimap.set_pixmap(None)
        self._minimap.set_eligible(False)
        # Stop and detach any previous animated GIF before routing to a
        # new path — QMovie keeps decoding frames otherwise.
        self._clear_movie()
        # Animated GIFs and animated WebPs render via QMovie so frames
        # actually animate; the static path would only show frame 0.  The
        # byte read *and* the frame-count probe run in the worker task
        # (``animation_probe`` — the old synchronous ``read_file_bytes``
        # here froze the GUI for the full NAS round-trip on cold shares,
        # and a static WebP then re-read the file in the decode task).
        # ``_apply_animated`` assembles the QMovie from the worker's bytes;
        # ``_known_static`` lets already-probed static files keep the
        # instant PIL-cache fast path below.
        #
        # Both the frame-count probe *and* QMovie go through the
        # bytes-first / QBuffer path — a bare ``QImageReader(str(path))`` /
        # ``QMovie(str(path))`` uses QFile, which Qt6 fails to open on some
        # SMB/NAS paths with CJK + full-width punctuation (the whole reason
        # ``qimage_decode`` exists), silently degrading animated media to a
        # single static frame on those shares.
        suffix = path.suffix.lower()
        probe_animation = (
            suffix in (".gif", ".webp")
            and str(path) not in self._known_static
        )
        # Fast path: prefetched by a previous navigation — hand the PIL
        # source directly to ``_apply_loaded`` and skip the decode worker
        # entirely.  This is what makes arrow-key stepping feel instant.
        # Skipped while an animation probe is pending: a prefetched first
        # frame of an animated file must not shadow QMovie playback.
        if not probe_animation:
            cached = self._pil_cache.get(str(path))
            if cached is not None:
                self._label.setText("")
                # ``_apply_loaded`` schedules the prefetch itself (its tail) —
                # a second explicit call here would start a second batch, so
                # the one just dispatched would go stale and a neighbour the
                # worker had already started decoding would be discarded on
                # arrival and decoded twice.
                self._apply_loaded(cached, from_cache=True)
                return
        # Stage 0 — instant thumbnail placeholder.  The right pane already
        # holds a decoded thumbnail for this file; paint it scaled-to-fit
        # right away so the user sees the image immediately (blurry), and
        # let the preview / full decode below sharpen it.  Falls back to a
        # textual notice when no thumbnail is available.
        if not self._show_placeholder(path):
            self._label.setPixmap(QPixmap())
            self._label.setText(t("common.status.loading_name", name=path.name))
            self._label.adjustSize()
        # Ask the worker for a *physical*-pixel-sized preview so the
        # JPEG DCT decode hits the screen's true resolution; on a 4K /
        # 150%-scaled display a logical-sized preview would be
        # subsequently upscaled by Qt and look blurry.
        self._submit_load(path, animation_probe=probe_animation)

    def _submit_load(self, path: Path, *, animation_probe: bool) -> None:
        """Dispatch the two-stage decode of *path* (superseding)."""
        box = self._preview_box()
        self._load_stream.submit_job(
            lambda job: _load_image(job, path, box, animation_probe)
        )

    def _preview_box(self) -> QSize:
        """Viewport size in physical pixels (the stage-1 preview target)."""
        dpr = self.devicePixelRatioF()
        logical = self.viewport().size()
        return QSize(
            max(1, round(logical.width() * dpr)),
            max(1, round(logical.height() * dpr)),
        )

    def _show_placeholder(self, path: Path) -> bool:
        """Paint the file-list thumbnail for *path* scaled-to-fit, instantly.

        Returns ``True`` when a placeholder was shown.  The pixmap is
        already resident in the right pane (no I/O), so this is cheap and
        gives immediate visual feedback; the preview / full decode then
        overwrites it.  ``_original_pixmap`` is deliberately left ``None``
        so ``_apply_preview`` still replaces this with the sharper preview.
        """
        provider = self._thumbnail_provider
        if provider is None:
            return False
        try:
            thumb = provider(path)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("thumbnail_provider failed: {}", exc)
            return False
        if thumb is None or thumb.isNull():
            return False
        logical = self.viewport().size() - QSize(2, 2)
        if logical.width() <= 0 or logical.height() <= 0:
            return False
        dpr = self.devicePixelRatioF()
        phys_w = max(1, round(logical.width() * dpr))
        phys_h = max(1, round(logical.height() * dpr))
        # The thumbnail carries the loader's own devicePixelRatio; strip it
        # so the upscale targets raw physical pixels, then re-stamp the
        # viewport's dpr for correct logical sizing.
        src = thumb
        if src.devicePixelRatio() != 1.0:
            src = QPixmap(thumb)
            src.setDevicePixelRatio(1.0)
        # FastTransformation (nearest-neighbour) is deliberate: this is a
        # throwaway placeholder upscaled from a small thumbnail, overwritten
        # within a few ms by the preview / full decode.  During fast wheel
        # navigation every intermediate file hits this path, so the smooth
        # (bilinear) upscale was pure wasted main-thread cost — the user
        # never sees these frames long enough for the quality to matter.
        pix = src.scaled(
            phys_w, phys_h, Qt.KeepAspectRatio, Qt.FastTransformation,
        )
        if dpr > 1.0:
            pix.setDevicePixelRatio(dpr)
        self._label.setPixmap(pix)
        self._label.resize(pix.deviceIndependentSize().toSize())
        self._label.setText("")
        return True

    def _ensure_original_pixmap(self) -> QPixmap | None:
        """原寸 ``QPixmap`` を返す（:meth:`release_pixels` の後なら作り直す）。

        ページを離れたときに手放した画素を、再表示の要求なしに戻ってきた
        経路（分割 ⇄ 最大化・リサイズ）でも復元するための単一の読み口。
        PIL ソース (``_original``) は LRU と共有で残っているので、ここでの
        復元にファイル読み込みは起きない。
        """
        if self._original_pixmap is None and self._original is not None:
            self._original_pixmap = pil_to_qpixmap(self._original)
        return self._original_pixmap

    def release_pixels(self) -> None:
        """Qt 側の画素ミラーを手放す（画像ページを離れるときの資源返却）。

        落とすのは ``ImageView`` が Qt 側に抱えている画素だけ: 原寸
        ``QPixmap``・パッチモードの下敷き・ミニマップの縮小版。8000×4000 の
        原寸 ``QPixmap`` は 128 MB 級で、``_pil_cache``（バイト予算つき LRU）
        にも入らない**どの予算にも属さない**常駐なので、他の葉ページを見て
        いる間ずっと残っていた。しかも戻ってきたときは ``show_image`` →
        ``_apply_loaded`` が ``pil_to_qpixmap`` で無条件に作り直すため、抱えて
        いても一切再利用されない。

        ``_original``（LRU と共有する PIL ソース）と ``_current_path`` は
        残す — 戻ったときのキャッシュヒットと、失敗カードの再読み込み・
        コピー等の「いま何を見ているか」に必要。
        """
        self.pause_animation()
        self._original_pixmap = None
        self._patch_base = None
        self._patch_base_source = -1
        self._minimap_pixmap = None
        self._minimap_pixmap_source = -1
        self._minimap.set_pixmap(None)

    def clear_image(self) -> None:
        self._error_card.hide()
        self._label.show()           # 失敗表示から復帰
        self._reset_patch_state()    # パッチモードの痕跡も破棄
        self._image_serial += 1
        # 3 本とも畳む（キュー済みを捨て、走行中のセッションを降ろす）。
        self._scale_stream.cancel()
        self._load_stream.cancel()
        self._prefetch_stream.cancel()
        self._prefetch.reset()
        self._original = None
        self._original_pixmap = None
        self._current_path = None
        self._base_original = None
        self._rotation = 0
        self._flip_h = False
        self._pending_restore.clear()
        self._refresh_timer.stop()
        self._clear_movie()
        self._label.clear()
        self._zoom_overlay.hide()
        self._control_bar.hide()
        self._minimap_pixmap = None
        self._minimap_pixmap_source = -1
        self._minimap.set_pixmap(None)
        self._minimap.set_eligible(False)
        self.image_info_changed.emit(0, 0)

    # ------------------------------------------------------------------ slots

    def _on_load_landed(self, payload: object) -> None:
        """画像デコードの着地（``loaded`` / ``failed`` / ``animated``）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_LoadKind, payload.kind)
        match kind:
            case "loaded":
                self._apply_loaded(cast(Image.Image, payload.value))
            case "failed":
                self._apply_failed(cast(str, payload.value))
            case "animated":
                self._apply_animated(cast(bytes, payload.value))
            case _:
                assert_never(kind)

    def _on_preview_landed(self, payload: object) -> None:
        """段 1（プレビュー）の途中報告 — ``progress`` の着地。"""
        if isinstance(payload, QImage):
            self._apply_preview(payload)

    def _apply_animated(self, data: bytes) -> None:
        """Worker probe found an animated file: build the QMovie here.

        Only the QMovie/QBuffer assembly runs on the GUI thread — the file
        bytes were read (and the frame count probed) in the worker, so cold
        NAS latency never blocks the UI.  When QMovie rejects the data
        (malformed file), fall back to a fresh static decode so frame 0
        still shows via the QImageReader path (rare, so the second read is
        acceptable).  その再投入は追い越しではなく**積み足し**
        （``submit_batch``）— 表示要求は同じ 1 枚のままなので、世代を進める
        必要が無い。
        """
        path = self._current_path
        if path is None:
            return
        if self._try_show_gif(path, data):
            # QMovie 経路は PIL の LRU を使わないので、窓に残すと恒久的に
            # 「デコード進行中」になる。
            self._settle_decode(path)
            return
        box = self._preview_box()
        self._load_stream.submit_batch(
            lambda job: _load_image(job, path, box, False)
        )

    def _apply_preview(self, qimage: QImage) -> None:
        if self._original_pixmap is not None:
            return  # full-res already arrived; preview is now obsolete
        logical = self.viewport().size() - QSize(2, 2)
        if logical.width() <= 0 or logical.height() <= 0:
            return
        dpr = self.devicePixelRatioF()
        phys_w = max(1, round(logical.width() * dpr))
        phys_h = max(1, round(logical.height() * dpr))
        pix = QPixmap.fromImage(qimage).scaled(
            phys_w, phys_h,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        if dpr > 1.0:
            pix.setDevicePixelRatio(dpr)
        self._label.setPixmap(pix)
        # QLabel.resize takes *logical* pixels; pix.size() is physical so
        # divide by dpr (deviceIndependentSize handles dpr==1 too).
        self._label.resize(pix.deviceIndependentSize().toSize())
        self._label.setText("")

    def _apply_loaded(
        self, image: Image.Image, *, from_cache: bool = False,
    ) -> None:
        # Keep the pristine decode so display-only rotate / flip (要件 F11) can
        # rebuild ``_original`` from it without re-reading the file.  A fresh
        # image always lands with rotation 0 / no flip (reset in show_image).
        self._base_original = image
        self._original = image
        # Cache a QPixmap version on the GUI thread — this is the source
        # for every fast preview during zoom/resize.
        self._original_pixmap = pil_to_qpixmap(image)
        self.image_info_changed.emit(image.width, image.height)
        if self._pending_restore.armed:
            self._fit_mode = self._saved_fit_mode
            self._zoom = self._saved_zoom
        else:
            self._fit_mode = True
            self._zoom = 1.0
        self._label.setText("")
        # Mirror the just-decoded PIL source into the bounded LRU so the
        # next navigation back to this file skips the decode.  Skip when
        # we got here via a cache hit — the entry is already resident and
        # ``put`` would just shuffle its LRU position (the cache hit in
        # ``show_image`` already did ``move_to_end``).
        if not from_cache and self._current_path is not None:
            self._pil_cache.put(str(self._current_path), image)
            self.cache_updated.emit()
        if self._current_path is not None:
            # A GIF/WebP that landed here was routed static by the worker's
            # animation probe — remember it so the next visit skips the
            # probe and hits the PIL-cache fast path in ``show_image``.
            if self._current_path.suffix.lower() in (".gif", ".webp"):
                self._known_static.add(str(self._current_path))
        self._refresh()
        if self._pending_restore.consume(always):
            self._restore_scroll_fraction(self._saved_scroll_fx, self._saved_scroll_fy)
        self._show_zoom_overlay()
        if self._current_path is not None:
            self._schedule_prefetch(self._current_path)
        # ``_schedule_prefetch`` が窓を張り直した**後**に外す（張り直しには
        # 現在画像も含まれる）。
        self._settle_decode(self._current_path)

    def _schedule_prefetch(self, current: Path) -> None:
        """Queue background decodes for neighboring image files.

        Neighbors are pulled from ``_siblings_provider`` (wired by
        :class:`ViewerWindow` to the right-pane file list).  Only images
        that aren't already cached are queued, so repeated visits to the
        same folder don't re-decode over and over.  Silent no-op when no
        provider is set — the decode-on-demand path still works.
        """
        provider = self._siblings_provider
        if provider is None:
            return
        try:
            siblings, index = provider(current)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("siblings_provider failed: {}", exc)
            return
        if not siblings or index < 0:
            return
        radius = self._prefetch_radius
        if radius <= 0:
            return
        targets: list[Path] = []
        for offset in range(1, radius + 1):
            for delta in (offset, -offset):
                j = index + delta
                if 0 <= j < len(siblings):
                    sibling = siblings[j]
                    # Provider 契約（「返すのは画像のみ」）の消費側担保:
                    # 暗黙契約が破れると隣接**動画**を decode_pil が丸読みしては
                    # 捨てることになる。provider 側のフィルタ（FileListView.image_siblings /
                    # LightboxWindow._sibling_provider）は残した上で、どの
                    # provider を挿しても非画像を読まない二重防御にする。
                    # 注: 非画像が混ざる provider では「半径 N 枚」が「半径 N の
                    # 窓の中の画像だけ」になる（先読み枚数が減る側の変化のみ）。
                    if sibling.suffix.lower() in IMAGE_SUFFIXES:
                        targets.append(sibling)
        # 構造上キャッシュに載り得ないターゲットは最初から発行しない
        # （台帳が予算から件数を言う）。
        targets = targets[
            : self._prefetch.cacheable_target_count(self._cache_max_entries)
        ]
        # 挿入ガードとデコード窓を組み直す（台帳 1 本）。
        self._prefetch.plan(current, targets)
        # 前の近傍集合は**畳まない**: 走行中のデコードが新しい集合でも要るなら
        # （5→6 と送ったときの 7 など）捨てずにそのまま着地させる。要らなく
        # なった残りは ``plan`` が張り替えた ``wanted`` を走り出した直後に見て
        # 降りる。積むのは「キャッシュに無く、まだ投げていない」ものだけ。
        for path in targets:
            self._submit_prefetch(path)

    def _submit_prefetch(self, path: Path) -> None:
        """近傍 *path* の先読みを（要るなら）1 件積む。"""
        key = str(path)
        if key in self._pil_cache or not self._prefetch.needs_submit(key):
            return
        self._prefetch.mark_inflight(key)
        self._prefetch_stream.submit_batch(
            lambda job, p=path: _prefetch_decode(job, p, self._prefetch_wanted)
        )

    def _prefetch_wanted(self, path: Path) -> bool:
        """この先読みの答えがまだ要るか（台帳 :meth:`PrefetchLedger.wanted`）。

        ワーカースレッド（:func:`_prefetch_decode`）と着地側
        （:meth:`_on_prefetch_landed`）が**同じメソッド**を引くので、「ワーカーは
        降りたのに着地は通る」ような片側のずれが起こせない。
        """
        return self._prefetch.wanted(path)

    def _on_prefetch_landed(self, payload: object) -> None:
        # Background fill, plus one display path (adoption, below).
        #
        # ストリームの ``bind`` が世代（フォルダ切替 / クリア）で落とした
        # 残りを、台帳（:meth:`_prefetch_wanted`）でもう一度振るう:
        # 表示要求も近傍集合の入れ替えもセッションを畳まない代わりに台帳を
        # 張り替えるので、前の近傍集合の着地はここで捨てる。捨てないと、
        # 直前の位置の画像が LRU へ入り直し、近い隣接を押し出す。
        if isinstance(payload, PrefetchMiss):
            key = str(payload.path)
            self._prefetch.landed(key)
            if not payload.declined:
                # 読めなかった近傍もデコードは終わっている — 窓から外さないと
                # 右ペインのそのタイルが「読み込み中」のまま 80ms 再描画が続く。
                self._settle_decode(payload.path)
            else:
                # 降りた後で近傍集合が戻ってきた（5→6→5 など）なら投げ直す。
                # 投げた時点では走行中だったので ``_schedule_prefetch`` は
                # 積まなかった。
                self._submit_prefetch(payload.path)
            return
        if not isinstance(payload, tuple):
            return
        path, image = cast("tuple[Path, Image.Image]", payload)
        key = str(path)
        self._prefetch.landed(key)
        if not self._prefetch_wanted(path):
            return
        # 採用 = 「いま表示しようとしているファイルそのもの」の着地。
        adopt = self._prefetch.adopts(key, self._current_path)
        # Current-batch results carry their distance-ordered insertion
        # guard; an adopted result IS the current image, so it inserts
        # unguarded (plain LRU may evict the farthest entries) — 現在画像は
        # 近傍集合の一員ではないので台帳は自然に ``()`` を返す。
        protect = self._prefetch.protect_for(key)
        self._pil_cache.put(key, image, protect=protect)
        # 挿入の可否に関わらずこのパスのデコードは終わっている。
        self._settle_decode(path)
        if key in self._pil_cache:
            self.cache_updated.emit()
        if not adopt:
            return
        if self._original is not None or self._movie is not None:
            return  # full-res (or QMovie) already on screen — nothing to do
        suffix = path.suffix.lower()
        if suffix in (".gif", ".webp") and key not in self._known_static:
            # Possibly animated: the main task's probe must decide — a
            # prefetched static first frame must not shadow QMovie
            # playback (same rule as ``show_image``'s cache fast path).
            return
        # Adopt: display the finished decode and cancel the redundant
        # load (the cancel makes its in-flight stages bail).
        self._load_stream.cancel()
        self._apply_loaded(image, from_cache=True)

    def _on_error_card_reload(self) -> None:
        """失敗カードの [再読み込み] — 同じパスをもう一度デコードする."""
        path = self._current_path
        if path is not None:
            self.show_image(path)

    def _on_error_card_open_default(self) -> None:
        """失敗カードの [既定アプリで開く] — 本体で開けないファイルの逃げ道."""
        path = self._current_path
        if path is not None:
            view_prefs.open_with_default(path, self)

    def _apply_failed(self, message: str) -> None:
        self._original = None
        self._reset_patch_state()  # canvas モードのまま失敗面に入らない
        # ズーム維持のスナップショットはここで捨てる（``clear_image`` と対称）。残したままだと ``_apply_loaded`` /
        # ``_try_show_gif`` に消費されるまで生き続け、失敗を挟んだあとに
        # ユーザーがズーム維持を OFF にしても、次に成功した画像へ失敗前の
        # 倍率・スクロール位置が復元されてしまう。
        self._pending_restore.clear()
        self._label.setPixmap(QPixmap())
        self.image_info_changed.emit(0, 0)
        # 素テキスト 1 行ではなく EmptyStateCard 規格の失敗カード +
        # [再読み込み][既定アプリで開く]。ラベル側の
        # テキストは重複するので消す（カードがビューポートを覆う）。
        self._label.setText("")
        # 中身を空にするだけではラベル自体が残り、qss の
        # ``QLabel#stageImageLabel``（ステージ地色 + 1px ヘアライン枠）が
        # 直前サイズの「空の枠付き矩形」として失敗カード越しに透けて見える
        # （壊れた入力欄のような見た目）。
        # ラベルごと隠し、``show_image`` / ``clear_image`` で出し直す。
        self._label.hide()
        self._error_card.show_error(message)
        # 読み値（カプセルの % と ``zoom_changed`` の購読者）を空にする。
        # ``image_info_changed.emit(0, 0)`` と対で、何も表示していないのに
        # 直前の画像の実効倍率が残るのを防ぐ（``_effective_zoom`` は画像が
        # 無ければ ``_zoom`` の残り値をそのまま返す）。
        self._sync_zoom_readout(0.0)
        self._settle_decode(self._current_path)
        if self._current_path is not None:
            self.load_failed.emit(self._current_path)

    # -------------------------------------------------------------- helpers

    def _original_size(self) -> QSize | None:
        """静止画ソースの原寸（無ければ ``None``）— 幾何への入力。"""
        if self._original is None:
            return None
        return QSize(self._original.width, self._original.height)

    def _target_phys_box(self) -> tuple[QSize, float] | None:
        """Compute the current (physical-pixel) target size and DPR.

        Returns ``None`` when nothing scalable is loaded or the viewport
        has collapsed to zero — callers should bail in that case.
        """
        natural = self._original_size()
        if natural is None:
            return None
        dpr = self.devicePixelRatioF()
        phys_box = geom.target_phys_box(
            natural=natural,
            fit_mode=self._fit_mode,
            zoom=self._zoom,
            viewport=self.viewport().size(),
            dpr=dpr,
            no_upscale=view_prefs.get_image_fit_no_upscale(),
        )
        if phys_box is None:
            return None
        return phys_box, dpr

    def _apply_preview_scale(self) -> None:
        """Synchronous Qt-side rescale of ``_original_pixmap`` only.

        Cheap enough (~5–15 ms for typical sources) to run on every
        resize/zoom event so the displayed image follows splitter drags
        in real time.  No LANCZOS work — that's a separate pass.
        """
        if self._is_gif and self._movie is not None:
            self._apply_movie_scale()
            return
        if self._ensure_original_pixmap() is None:
            return
        target = self._target_phys_box()
        if target is None:
            return
        phys_box, dpr = target
        preview = self._original_pixmap.scaled(
            phys_box.width(), phys_box.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        if dpr > 1.0:
            preview.setDevicePixelRatio(dpr)
        self._label.setPixmap(preview)
        self._label.resize(preview.deviceIndependentSize().toSize())

    def _schedule_lanczos(self) -> None:
        """Queue a LANCZOS resample at the current target size.

        投入は superseding — キュー済みは破棄され、走行中の 1 本の着地も
        追い越されるので、実際に描かれるのは最新のターゲットだけ。
        """
        if self._is_gif:
            return
        pil = self._original
        if pil is None:
            return
        target = self._target_phys_box()
        if target is None:
            return
        phys_box, dpr = target
        self._scale_stream.submit(
            lambda: _lanczos_full(pil, phys_box, dpr)
        )

    def _refresh(self) -> None:
        """Apply a fast Qt-side scale now, schedule LANCZOS in background.

        Two-stage rendering: Qt's ``SmoothTransformation`` (bilinear) is
        cheap enough to feel instantaneous on wheel-zoom / resize, so we
        use it for the immediate frame.  LANCZOS is queued on the
        background pool and replaces the pixmap when done.  If a newer
        refresh comes in while LANCZOS is still running, the token is
        bumped; the queue is cleared so pending jobs never start, and
        an already-running job's result is dropped on token mismatch.

        全体レンダの物理ターゲットがピクセル予算を超えるズーム域
        （静止画・非フィットのみ）は可視領域パッチ描画へ切り替える
        （``_apply_patch_mode`` — 同じ 2 段構成: 同期 Qt スケール → 非同期
        高品位リサンプル）。それ以外は全体レンダ経路で、
        ``_clamp_pixel_budget`` の予算契約もそのまま生きている。
        """
        self._refresh_timer.stop()
        if self._patch_mode_wanted():
            self._apply_patch_mode()
        else:
            # アニメーションは常に canvas で描く（``_apply_movie_scale``）。
            if self._label.canvas_active() and not self._is_gif:
                self._label.clear_canvas()
                self._patch_rect = QRect()
                self._patch_dpr = 0.0
            self._apply_preview_scale()
            self._schedule_lanczos()
        self._update_pan_cursor()
        self._update_overlays()

    # ------------------------------------------------ 可視領域パッチレンダ

    def _full_zoom_size(self) -> QSize:
        """非フィット時の論理レンダ全寸（= ラベル寸）."""
        natural = self._original_size()
        assert natural is not None
        return geom.full_zoom_size(natural, self._zoom)

    def _patch_mode_wanted(self) -> bool:
        """全体レンダが予算を超える静止画ズームか（= パッチ描画に切り替える）.

        アニメーションは ``_original`` を持たないので常に偽 — 原寸フレームを
        canvas へ渡して描画時にスケールする（``_apply_movie_scale``）ので、
        パッチを切り出す必要が無い。
        """
        return geom.patch_mode_wanted(
            fit_mode=self._fit_mode,
            natural=self._original_size(),
            zoom=self._zoom,
            dpr=self.devicePixelRatioF(),
        )

    def _apply_patch_mode(self) -> None:
        """canvas モードへ入り、ラベルを論理全寸へ広げ、可視パッチをレンダする.

        ラベルの resize は同期で走らせる — スクロールレンジ・ホイールズームの
        アンカー計算（``wheelEvent`` が ``_refresh`` 直後にラベル寸を読む）・
        ミニマップの矩形計算は全てラベル寸に乗っているので、全体レンダ経路と
        同じタイミング契約を保つ。
        """
        size = self._full_zoom_size()
        self._label.set_canvas(self._patch_base_pixmap())
        if self._label.size() != size:
            self._label.resize(size)
        self._render_patch()

    def _patch_base_pixmap(self) -> QPixmap | None:
        """低解像度の全体像（canvas の下敷き）。画像ごとに 1 回だけ作る.

        ``_original_pixmap``（デコード済み・GUI スレッド常駐）からのスケール
        のみで、ファイル再読込・再デコードは発生しない（ミニマップと同じ
        方針・同じトークンキャッシュ）。
        """
        if self._patch_base is not None and self._patch_base_source == self._image_serial:
            return self._patch_base
        source = self._ensure_original_pixmap()
        if source is None or source.isNull():
            return None
        target = geom.clamp_pixel_budget(
            source.size(), geom.PATCH_BASE_MAX_PIXELS,
        )
        if target == source.size():
            base = source
        else:
            base = source.scaled(
                target.width(), target.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        self._patch_base = base
        self._patch_base_source = self._image_serial
        return base

    def _visible_label_rect(self) -> QRect:
        """いまビューポートに見えているラベル領域（ラベル論理座標）."""
        return geom.visible_label_rect(
            label_pos=self._label.pos(),
            label_size=self._label.size(),
            viewport=self.viewport().size(),
        )

    def _patch_geometry(
        self,
    ) -> tuple[QRect, tuple[int, int, int, int], QSize] | None:
        """(パッチ論理矩形, 原寸クロップ box, 物理出力サイズ) を計算する."""
        natural = self._original_size()
        if natural is None:
            return None
        return geom.patch_geometry(
            natural=natural,
            zoom=self._zoom,
            visible=self._visible_label_rect(),
            canvas=QRect(QPoint(0, 0), self._label.size()),
            viewport=self.viewport().size(),
            dpr=self.devicePixelRatioF(),
        )

    def _render_patch(self) -> None:
        """可視パッチを 2 段でレンダする（同期 Qt スケール → 非同期高品位）.

        段1 は ``_original_pixmap`` の小さなクロップを Qt でスケールする
        だけなので同期で走らせても軽い（出力が大きいときは Fast へ落とす）。
        段2 は原寸 PIL からのクロップ + リサンプルをワーカーへ
        （:func:`_patch_resample` — GUI スレッドでフル解像度の同期処理は
        しない）。レンダ中は直前のパッチ + 下敷きが表示され続けるので
        ちらつき・空白は出ない。
        """
        plan = self._patch_geometry()
        if plan is None:
            return
        target, box, phys = plan
        self._patch_rect = QRect(target)
        self._patch_dpr = max(1.0, self.devicePixelRatioF() or 1.0)
        source = self._ensure_original_pixmap()
        if source is not None and not source.isNull():
            crop = source.copy(
                QRect(box[0], box[1], box[2] - box[0], box[3] - box[1])
            )
            smooth = (
                phys.width() * phys.height()
                <= geom.PATCH_PREVIEW_SMOOTH_MAX_PIXELS
            )
            mode = (
                Qt.SmoothTransformation if smooth else Qt.FastTransformation
            )
            preview = crop.scaled(
                phys.width(), phys.height(), Qt.IgnoreAspectRatio, mode,
            )
            self._label.set_canvas_patch(preview, target)
        pil = self._original
        if pil is None:
            return
        # 全体レンダと**同じストリーム**へ superseding で積む（モード切替を
        # 跨いだ相互無効化）。
        self._scale_stream.submit(
            lambda: _patch_resample(pil, box, phys, target)
        )

    def _patch_stale(self) -> bool:
        """可視域が最後にレンダしたパッチの外へ出た（or DPR が変わった）か."""
        if not self._label.canvas_active() or self._is_gif:
            return False  # アニメーションの canvas は全面が原寸フレーム
        dpr = max(1.0, self.devicePixelRatioF() or 1.0)
        if dpr != self._patch_dpr:
            return True
        return not self._patch_rect.contains(self._visible_label_rect())

    def _schedule_patch_update(self) -> None:
        """デバウンス付きでパッチ再レンダを予約する（パン / リサイズ中）.

        締切の張り直しはしない（``LEADING_WINDOW``）— 60Hz のスクロールで
        毎回リスタートするとタイマーが永遠に発火せず、長いパンの間ずっと
        下敷きの低解像度のままになる。走っていないときだけ武装することで、
        連続パン中も ``_REFRESH_DEBOUNCE_MS`` 間隔でパッチが追従する。
        """
        self._refresh_timer.trigger()

    def _reset_patch_state(self) -> None:
        """canvas モードと画像単位のパッチキャッシュを破棄する."""
        self._label.clear_canvas()
        self._patch_rect = QRect()
        self._patch_dpr = 0.0
        self._patch_base = None
        self._patch_base_source = -1

    def _on_scale_landed(self, payload: object) -> None:
        """リサンプル結果の着地（全体レンダ / パッチの 1 本口）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_ScaleKind, payload.kind)
        match kind:
            case "full":
                dpr_and_pil = cast("tuple[float, Image.Image]", payload.value)
                self._apply_scaled(*dpr_and_pil)
            case "patch":
                rect_and_pil = cast("tuple[QRect, Image.Image]", payload.value)
                self._apply_patch_scaled(*rect_and_pil)
            case _:
                assert_never(kind)

    def _apply_patch_scaled(self, rect: QRect, pil: Image.Image) -> None:
        if not self._label.canvas_active():
            return  # 全体レンダへ戻った後に届いた残骸
        self._label.set_canvas_patch(pil_to_qpixmap(pil), QRect(rect))

    def _apply_scaled(self, dpr: float, pil: Image.Image) -> None:
        if self._label.canvas_active():
            return  # パッチモードへ切り替わった後に届いた全体レンダ
        pix = pil_to_qpixmap(pil)
        # LANCZOS ran with a physical-pixel target (phys_box passed to the
        # task).  Use the DPR captured at schedule time — re-reading
        # devicePixelRatioF() here would give the wrong value if the window
        # moved monitors while the task was running.
        if dpr > 1.0:
            pix.setDevicePixelRatio(dpr)
        self._label.setPixmap(pix)
        self._label.resize(pix.deviceIndependentSize().toSize())
        self._update_overlays()

    def showEvent(self, event):  # noqa: N802 (Qt API)
        """再表示時にパン可否依存の表示を測り直す.

        非表示のウィジェットに対する ``QLabel.resize`` では ``QScrollArea`` の
        スクロールバー範囲が更新されない（Resize イベントが保留される）ため、
        隠れている間に着地した画像（``_pil_cache`` ヒットの同期ファストパス）
        では ``_is_pannable()`` が**直前の画像**の値を返す。その値で決まる
        オープンハンドカーソルとミニマップ可否は、次に ``_refresh`` が走る
        まで取り違えられたまま固定される（パンできないのにオープンハンド →
        ドラッグするとパンではなくファイルのドラッグ書き出しが始まる）。
        Qt は表示の時点で保留中のジオメトリを適用済みなので、ここで測り直せば
        正しい値が読める。
        """
        super().showEvent(event)
        self._update_pan_cursor()
        self._update_overlays()

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._zoom_overlay.reposition()
        self._minimap.reposition()
        self._sync_overlay_keepout()
        if self._error_card.isVisible():
            self._error_card.setGeometry(self.viewport().rect())
        # 画像矩形基準の再配置。
        self._control_bar.set_anchor_rect(self._content_anchor_rect())
        self._control_bar.reposition()
        # フィット時の実効倍率はビューポート寸法の関数なので、リサイズの
        # たびに読み値が陳腐化する。非フィット分岐でも早期 return
        # の前に通す — 表示は不変でも 1 回の同期は無害で、両分岐の対を
        # 揃えておくほうが次の分岐追加で片側だけ漏れる事故を防げる。
        self._sync_zoom_readout()
        if not self._fit_mode:
            # パッチモード中はビューポート拡大で可視域がパッチ外へ出得る。
            # デバウンス付きで追従レンダを予約する。
            if self._patch_stale():
                self._schedule_patch_update()
            return
        # Splitter drags fire resize events at ~60 Hz.  The 60 ms refresh
        # debounce keeps restarting under that load and never fires until
        # the user pauses, making the image appear "stuck" during drag.
        # Run the cheap Qt bilinear rescale synchronously here so the
        # preview follows the viewport in real time, then defer only the
        # expensive LANCZOS pass to the debounce.
        self._apply_preview_scale()
        # ここだけは作法（LEADING_WINDOW）を意図的に外して締切を張り直す —
        # ドラッグ中の高価な LANCZOS は最後の 1 回だけでよく、追従は上の
        # 同期リスケールが担うため。
        self._refresh_timer.start()

    def _natural_size(self) -> QSize | None:
        """現在表示中コンテンツの元寸。無ければ ``None``。"""
        if self._is_gif and self._movie is not None:
            # フレームは常に原寸でデコードされる（``setScaledSize`` を使わない）
            # ので都度読みでよい。表示用の回転を掛けた寸法を返す（静止画の
            # ``_original`` が回転済みなのと対）。
            natural = self._movie.frameRect().size()
            if not natural.isValid() or natural.isEmpty():
                natural = self._movie.currentImage().size()
            natural = oriented_size(natural, self._rotation)
        elif self._original is not None:
            natural = QSize(self._original.width, self._original.height)
        else:
            return None
        if natural.width() <= 0 or natural.height() <= 0:
            return None
        return natural

    def _effective_zoom(self) -> float:
        """Current on-screen scale relative to the natural image size.

        フィット時の ``self._zoom`` は古い値のままで、ユーザーが見ているもの
        （ビューポートへ縮めた表示）を表さない。ホイールズームがこの実効倍率
        から始まらないと 1 ノッチ目でフィット表示から飛び離れるので、読み値も
        ズームの起点もここを通す。
        """
        return geom.effective_zoom(
            natural=self._natural_size(),
            fit_mode=self._fit_mode,
            zoom=self._zoom,
            viewport=self.viewport().size(),
            no_upscale=view_prefs.get_image_fit_no_upscale(),
        )

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        intent = ivinput.wheel_intent(
            delta=event.angleDelta().y(),
            ctrl=bool(event.modifiers() & Qt.ControlModifier),
            zoomable=self._original is not None or (
                self._is_gif and self._movie is not None
            ),
            wheel_zoom_pref=view_prefs.get_image_wheel_zoom(),
        )
        if isinstance(intent, ivinput.Consume):
            event.accept()
            return
        if isinstance(intent, ivinput.ZoomAtCursor):
            vp_pos = self.viewport().mapFromGlobal(
                event.globalPosition().toPoint()
            )
            if self._zoom_at(vp_pos, intent.factor):
                self._show_zoom_overlay()
            event.accept()
            return
        # When zoomed in past the viewport, the user is panning; defer to
        # the grace period after hitting an edge.  When fit-to-window /
        # actual-size with small images, scrollbars collapse and the event
        # navigates immediately.  Off-edge wheels scroll by the
        # user-tunable ``view_prefs`` pixel step.
        if route_scroll_or_navigate(self, event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)

    def _zoom_at(self, vp_pos: QPoint, factor: float) -> bool:
        """カーソル位置を固定して 1 段ズームする。動かなければ ``False``。"""
        base_zoom = self._effective_zoom()
        new_zoom = geom.step_zoom_value(base_zoom, factor)
        if new_zoom == base_zoom and not self._fit_mode:
            return False
        self._anchor_zoom(vp_pos, new_zoom)
        return True

    def _anchor_zoom(self, vp_pos: QPoint, new_zoom: float) -> None:
        """*vp_pos* の下の画像の点を動かさずに *new_zoom* へ切り替える。

        ズーム前にラベル内の割合を捉え、同期で描き直してから（スクロール範囲が
        新しいラベル寸を反映してからでないとアンカーがずれる）その割合を
        カーソル下へ戻す。ホイールとダブルクリックの共通実装。
        """
        fx, fy = geom.anchor_fraction(
            vp_pos=vp_pos,
            label_top_left=self._label.mapTo(self.viewport(), QPoint(0, 0)),
            label_size=self._label.size(),
        )
        self._fit_mode = False
        self._zoom = new_zoom
        self._refresh_timer.stop()
        self._refresh()
        hx, vy = geom.anchor_scroll_values(
            fx, fy, label_size=self._label.size(), vp_pos=vp_pos,
        )
        self.horizontalScrollBar().setValue(hx)
        self.verticalScrollBar().setValue(vy)

    def _set_actual_size(self) -> None:
        if self._original is None and not self._is_gif:
            return
        self._fit_mode = False
        self._zoom = 1.0
        self._refresh()
        self._show_zoom_overlay()

    def _fit_to_window(self) -> None:
        self._fit_mode = True
        self._refresh()
        self._show_zoom_overlay()

    def _toggle_fit_actual(self) -> None:
        """Control-bar fit/actual toggle (F02): fit-to-window ⇄ actual size.

        Mirrors the double-click behaviour but anchored at the viewport
        centre rather than the cursor, since it's driven by a button.
        """
        if self._original is None and not (self._is_gif and self._movie is not None):
            return
        if self._fit_mode:
            self._set_actual_size()
        else:
            self._fit_to_window()

    def _zoom_in(self) -> None:
        self._step_zoom(ivinput.KEY_ZOOM_FACTOR)

    def _zoom_out(self) -> None:
        self._step_zoom(1 / ivinput.KEY_ZOOM_FACTOR)

    def _step_zoom(self, factor: float) -> None:
        """Keyboard +/- stepwise zoom (F04), centered on the viewport.

        Starts from the current *effective* zoom (so the first + / - after a
        fitted view nudges from what's on screen, not from a stale 1.0) and
        clamps to the same 0.05–20x range as wheel-zoom.
        """
        if self._original is None and not (self._is_gif and self._movie is not None):
            return
        base = self._effective_zoom()
        new_zoom = geom.step_zoom_value(base, factor)  # ホイールと同じクランプ
        if new_zoom == base and not self._fit_mode:
            return
        self._fit_mode = False
        self._zoom = new_zoom
        self._refresh()
        self._show_zoom_overlay()

    def _toggle_zoom_at(self, vp_pos: QPoint) -> None:
        """Double-click zoom toggle: fit-to-window ⇄ actual size (100%).

        When fitted (or at any zoom other than exactly 100%), zoom to actual
        size anchored at *vp_pos* (viewport coords) so the point under the
        cursor stays put; when already at actual size, return to fit-to-window.
        """
        if self._original is None and not (
            self._is_gif and self._movie is not None
        ):
            return
        # "At actual size" = not in fit mode and zoom ~= 1.0.
        at_actual = (not self._fit_mode) and abs(self._zoom - 1.0) < 1e-3
        if at_actual:
            self._fit_to_window()
            return
        self._anchor_zoom(vp_pos, 1.0)
        self._show_zoom_overlay()

    # ------------------------------------------------- animated GIF support

    def _try_show_gif(self, path: Path, data: bytes | None = None) -> bool:
        """Set up :class:`QMovie` playback for *path*.

        Returns ``True`` on success; on failure the caller falls back to
        the static-image path so a broken GIF still shows frame 0 via
        ``QImageReader`` rather than a blank label.

        The movie decodes from the in-memory bytes (read via
        ``read_file_bytes``, or reused from *data*) — why, and why the buffer
        + bytes stay pinned on ``self`` until ``_clear_movie``: see
        :func:`~.image_view_parts.movie.open_movie`.
        """
        if data is None:
            data = read_file_bytes(path)
        if data is None:
            return False
        opened = open_movie(self, data)
        if opened is None:
            return False
        movie, buffer, qbytes = opened
        self._playback.forget()
        self._movie = movie
        # Keep references so the pair isn't GC'd from the Python side; Qt's
        # parent/child chain (buffer→movie→self) owns the teardown order.
        self._movie_buffer = buffer
        self._movie_bytes = qbytes
        self._is_gif = True
        natural = self._natural_size() or QSize()
        self.image_info_changed.emit(natural.width(), natural.height())
        if self._pending_restore.armed:
            self._fit_mode = self._saved_fit_mode
            self._zoom = self._saved_zoom
        else:
            self._fit_mode = True
            self._zoom = 1.0
        self._label.setText("")
        # QLabel.setMovie は使わない — フレームは原寸のまま受け取り、canvas
        # が描画時に拡縮する（``_apply_movie_scale``）。
        movie.frameChanged.connect(self._on_movie_frame)
        self._apply_movie_scale()
        movie.start()
        # Visual affordance: clicking the frame toggles play/pause.
        self._label.setCursor(Qt.CursorShape.PointingHandCursor)
        if self._pending_restore.consume(always):
            self._restore_scroll_fraction(self._saved_scroll_fx, self._saved_scroll_fy)
        self._show_zoom_overlay()
        # アニメ着地も静止画と同じく隣接の先読みを撒く: ``_apply_loaded``
        # だけが ``_schedule_prefetch`` を呼ぶと、GIF / アニメ WebP に着地した
        # 瞬間だけ先読みが止まり、次の画像が毎回コールドデコードになる。provider 未設定なら黙って no-op。
        if self._current_path is not None:
            self._schedule_prefetch(self._current_path)
        return True

    def _apply_movie_scale(self) -> None:
        """ラベルを静止画と同じ計算の寸法へ広げ、現在フレームをその寸で描く.

        拡縮は canvas の描画時なので画素予算の上限は要らない。一時停止中の
        ズーム / リサイズも次のフレームを待たずにここで新寸になる。
        """
        natural = self._natural_size()
        if self._movie is None or natural is None:
            return
        size = geom.movie_label_size(
            natural=natural,
            fit_mode=self._fit_mode,
            zoom=self._zoom,
            viewport=self.viewport().size(),
            no_upscale=view_prefs.get_image_fit_no_upscale(),
        )
        if size is None:
            return
        if self._label.size() != size:
            self._label.resize(size)
        self._on_movie_frame()

    def _on_movie_frame(self, _frame: int = -1) -> None:
        """``frameChanged`` — 現在フレームを表示寸（物理画素）以下にして canvas へ."""
        movie = self._movie
        if movie is None:
            return
        dpr, label = self.devicePixelRatioF() or 1.0, self._label.size()
        target = QSize(round(label.width() * dpr), round(label.height() * dpr))
        pix = render_frame(movie, target=target, rotation=self._rotation, flip_h=self._flip_h)
        if pix is not None:
            self._label.set_canvas(pix)

    def _movie_frame(self) -> QImage | None:
        """表示中の向きを掛けた現在フレーム（アニメーションでなければ ``None``）."""
        if not self._is_gif or self._movie is None:
            return None
        frame = orient_frame(self._movie.currentImage(), self._rotation, self._flip_h)
        return None if frame.isNull() else frame

    def _clear_movie(self) -> None:
        self._playback.forget()
        if self._movie is None:
            return
        self._movie.stop()
        self._movie.frameChanged.disconnect(self._on_movie_frame)
        # deleteLater on the movie also disposes its child QBuffer (parented
        # in _try_show_gif) in the correct order — just drop our Python refs.
        self._movie.deleteLater()
        self._movie = None
        self._movie_buffer = None
        self._movie_bytes = None
        if self._is_gif:
            self._label.clear_canvas()
            self._label.clear()
            self._label.unsetCursor()
        self._is_gif = False

    def pause_animation(self) -> None:
        """Pause a playing QMovie (GIF / animated WebP) without unloading it.

        Called by :class:`ContentView` when the stacked page switches away
        from the image preview — a hidden QMovie keeps decoding frames at
        full rate otherwise, wasting CPU for as long as the user browses
        other content.  Navigating back to an image always goes through
        :meth:`show_image`, which rebuilds and restarts the movie, so no
        resume counterpart is needed here.  別ウィンドウへの退避は選択が
        動かず ``show_image`` を通らないので、そちらは
        :meth:`suspend_animation` / :meth:`resume_animation` の対を使う。
        """
        self._playback.pause(self._movie)

    def suspend_animation(self) -> None:
        """別ウィンドウ（全画面）へ退避する間だけ再生を止める（:meth:`resume_animation` と対）。"""
        self._playback.suspend(self._movie)

    def resume_animation(self) -> None:
        """:meth:`suspend_animation` が止めた再生を再開する（それ以外は no-op）。"""
        self._playback.resume(self._movie)

    def _toggle_movie(self) -> None:
        self._playback.toggle(self._movie)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if obj is not self._label:
            return super().eventFilter(obj, event)
        etype = event.type()
        # Any hover activity over the image reveals the control bar; it
        # fades out again on its own idle timer.  Keyboard-only use never
        # triggers this, so the bar stays out of the way.
        # 種の判定を先に置くのは短絡のため — このフィルタはウィジェット構築
        # 途中の（マウスでない）イベントも受け取るので、``_control_bar_enabled``
        # がまだ無い時点で属性を読んではいけない。
        if etype in ivinput.HOVER_EVENT_TYPES and self._control_bar_enabled:
            self._control_bar.set_anchor_rect(self._content_anchor_rect())
            self._control_bar.reveal()
        if etype not in ivinput.MOUSE_EVENT_TYPES:
            return super().eventFilter(obj, event)
        start = self._drag_start_pos
        intent = ivinput.label_intent(
            etype,
            button=event.button(),
            buttons=event.buttons(),
            armed=start is not None,
            panning=self._panning,
            zoomable=self._original is not None or (
                self._is_gif and self._movie is not None
            ),
            has_movie=self._movie is not None,
            double_click_maximize=self._double_click_maximize,
            has_path=self._current_path is not None,
            pannable=self._is_pannable(),
            drag_reached=start is not None and ivinput.drag_threshold_reached(
                start, event.position().toPoint(),
                QApplication.startDragDistance(),
            ),
        )
        if etype == QEvent.Type.MouseButtonRelease:
            # 離した時点でドラッグ待機は必ず落ちる（意図の判定は上で済み）。
            self._drag_start_pos = None
        if isinstance(intent, ivinput.ArmDrag):
            # 押下は待機を張るだけ。アニメーションの再生 / 一時停止は
            # ドラッグに発展しなかった Release で決める。
            self._drag_start_pos = event.position().toPoint()
            self._playback.arm()
        elif isinstance(intent, ivinput.ToggleFitActual):
            self._toggle_fit_actual()
            return True
        elif isinstance(intent, ivinput.Maximize):
            self._drag_start_pos = None
            # 1 回目の Release が切り替えた再生状態を戻す — 最大化した直後の
            # GIF が止まったまま出ないように。
            self._playback.undo_release_toggle(self._movie)
            self.maximize_requested.emit()
            return True
        elif isinstance(intent, ivinput.ToggleZoomAt):
            self._drag_start_pos = None
            self._toggle_zoom_at(self.viewport().mapFromGlobal(
                event.globalPosition().toPoint()
            ))
            return True
        elif isinstance(intent, ivinput.PanTo):
            self._pan_to(event.globalPosition().toPoint())
            return True
        elif isinstance(intent, ivinput.BeginPan):
            self._panning = True
            self._pan_last = event.globalPosition().toPoint()
            self._label.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        elif isinstance(intent, ivinput.StartExportDrag):
            self._drag_start_pos = None
            self._start_export_drag()
            return True
        elif isinstance(intent, ivinput.EndPan):
            self._panning = False
            self._pan_last = None
            self._update_pan_cursor()
            return True
        elif isinstance(intent, ivinput.ToggleMovie):
            self._playback.toggle_on_release(self._movie)
            return True
        return super().eventFilter(obj, event)

    def _is_pannable(self) -> bool:
        """True when the image overflows the viewport in either axis.

        The threshold is exactly "the scroll area has something to scroll" —
        there is no slop, and a slop band could not help.
        Scrollbars are ``AsNeeded``: a 1 px overflow makes them appear, which
        shrinks the viewport by the scrollbar extent and pushes ``maximum()``
        straight to ~15 px — measured 0 → 15 → 16 for a source 0/1/2 px larger
        than the viewport, so nothing ever lands in a 1–2 px band to filter.
        """
        return (
            self.horizontalScrollBar().maximum() > 0
            or self.verticalScrollBar().maximum() > 0
        )

    def _pan_to(self, global_pos: QPoint) -> None:
        """Scroll by the cursor delta since the last pan sample."""
        if self._pan_last is None:
            self._pan_last = global_pos
            return
        delta = global_pos - self._pan_last
        self._pan_last = global_pos
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setValue(hbar.value() - delta.x())
        vbar.setValue(vbar.value() - delta.y())

    def _update_pan_cursor(self) -> None:
        """Show an open-hand cursor while the image is pannable.

        A GIF that does NOT overflow the viewport keeps its play/pause
        pointing-hand affordance; one zoomed past the viewport gets the same
        open hand as a still image (it pans identically, and without the
        cursor change nothing hints that it can be dragged).  An active pan
        (closed hand) is never overridden.
        """
        if self._panning:
            return
        if self._is_pannable():
            self._label.setCursor(Qt.CursorShape.OpenHandCursor)
        elif self._is_gif:
            self._label.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self._label.unsetCursor()

    def _start_export_drag(self) -> None:
        """Drag the currently shown image file into other applications
        (Explorer, image editors, browsers, chat clients)."""
        path = self._current_path
        if path is None:
            return
        start_file_export_drag(self, path, self._ensure_original_pixmap())

    # --------------------------------------------------- zoom-persist helpers

    def _scroll_fraction(self) -> tuple[float, float]:
        """Current scroll position as a fraction (0..1) of each scrollbar."""
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        return geom.scroll_fraction(
            h_value=hbar.value(), h_max=hbar.maximum(),
            v_value=vbar.value(), v_max=vbar.maximum(),
        )

    def _restore_scroll_fraction(self, fx: float, fy: float) -> None:
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hx, vy = geom.restore_scroll_values(
            fx, fy, h_max=hbar.maximum(), v_max=vbar.maximum(),
        )
        if hx is not None:
            hbar.setValue(hx)
        if vy is not None:
            vbar.setValue(vy)

    # ------------------------------------------------------- zoom overlay

    def _show_zoom_overlay(self) -> None:
        percent = self._effective_zoom() * 100.0
        self._zoom_overlay.show_zoom(percent)
        self._sync_zoom_readout(percent)

    def _sync_zoom_readout(self, percent: float | None = None) -> None:
        """常設の読み値（カプセル % / 外部購読）だけを実効倍率へ同期する.

        ``_show_zoom_overlay`` は「ユーザーがズーム操作をした」ことを前提に
        一時ピルも出すが、**ビューポートのリサイズ**（分割 ⇄ 最大化・スプリッタ
        ドラッグ・全画面遷移）ではフィット時の実効倍率だけが静かに変わる。
        読み値の同期をこのメソッドへ分離し ``resizeEvent`` の両分岐から呼ぶ
        ことで、「52% と出ているのに実際は 100%」という陳腐化を防ぐ。
        一時オーバーレイは出さない。
        """
        if percent is None:
            percent = self._effective_zoom() * 100.0
        # Keep the control bar's always-on zoom readout in sync (F02).
        self._control_bar.set_zoom(percent)
        # Every zoom-affecting path funnels through here, so this is the one
        # emit point external readouts need.
        self.zoom_changed.emit(percent)

    # ---------------------------------------------------------- minimap

    def _on_scrolled(self, _value: int) -> None:
        # Wheel/drag-pan scrolling fires this at up to 60Hz — keep the work
        # here to a rect recompute only (no pixmap rescale, see
        # ``_update_minimap_pixmap``'s caching).  Scrolling is activity, so
        # (re)show the minimap and restart its idle timer, mirroring the
        # zoom overlay's "shown only while interacting" behavior.
        self._update_minimap_rect()
        self._minimap.bump_activity()
        # パン追従: 可視域が高解像度パッチの外へ出たら再レンダを
        # 予約する。マージン内のパンは何もしない（既レンダで覆われている）。
        if self._patch_stale():
            self._schedule_patch_update()

    def _content_anchor_rect(self) -> QRect | None:
        """いま画像が描かれている矩形（viewport 座標）。無ければ ``None``.

        ホバーカプセルの配置基準。``_label`` は
        QScrollArea の中身なので viewport 座標系の geometry を持つ — フィット
        表示ではラベル ≒ 画像、拡大時は viewport との交差が可視領域になる。
        """
        if self._original is None and not (self._is_gif and self._movie):
            return None
        rect = self._label.geometry().intersected(self.viewport().rect())
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        return rect

    def _update_overlays(self) -> None:
        """Refresh both overlays after a zoom/fit/resize change."""
        self._zoom_overlay.reposition()
        self._control_bar.set_anchor_rect(self._content_anchor_rect())
        self._control_bar.reposition()
        self._update_minimap_visibility()

    def _update_minimap_visibility(self) -> None:
        """Recompute eligibility (pannable + enabled) and, if newly eligible
        or already visible, register the triggering change as activity.

        This does *not* keep the minimap permanently visible while pannable
        — it only updates whether :meth:`_MinimapOverlay.bump_activity` is
        allowed to show it.  The actual show + 1.5s auto-hide is driven by
        activity call sites (zoom/fit changes, scrolling, panning, hover).
        """
        has_content = self._original is not None or (
            self._is_gif and self._movie is not None
        )
        eligible = self._minimap_enabled and has_content and self._is_pannable()
        self._minimap.set_eligible(eligible)
        if eligible:
            self._update_minimap_pixmap()
            self._update_minimap_rect()
            self._minimap.reposition()
        # 可否・寸法が決まった後で、下端の浮遊部品に避ける枠を渡し直す。
        self._sync_overlay_keepout()
        if eligible:
            # A zoom/fit/resize change that leaves the image pannable counts
            # as activity — e.g. the user just wheel-zoomed in past the
            # viewport.
            self._minimap.bump_activity()

    def _sync_overlay_keepout(self) -> None:
        """ミニマップの占有枠を唯一の「避ける矩形」としてカプセルとピルへ配る。"""
        keepout = self._minimap.footprint()
        self._control_bar.set_keepout_rect(keepout)
        self._zoom_overlay.set_keepout_rect(keepout)

    def _update_minimap_pixmap(self) -> None:
        """(Re)build the minimap's small preview pixmap, once per image.

        Scaled from ``_original_pixmap`` (already-decoded full-res source)
        — never re-reads the file or triggers a fresh decode.  Cached via
        ``_minimap_pixmap_source`` so panning/zoom changes on the same
        image don't rescale on every call.
        """
        if self._minimap_pixmap_source == self._image_serial and self._minimap_pixmap is not None:
            return
        source: QPixmap | None = self._ensure_original_pixmap()
        if source is None and (frame := self._movie_frame()) is not None:
            source = QPixmap.fromImage(frame)
        if source is None or source.isNull():
            return
        small = source.scaled(
            _MinimapOverlay._MAX_EDGE, _MinimapOverlay._MAX_EDGE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self._minimap_pixmap = small
        self._minimap_pixmap_source = self._image_serial
        self._minimap.set_pixmap(small)

    def _update_minimap_rect(self) -> None:
        # 可視かどうかで早抜けしないこと: 呼び出し元は
        # ``bump_activity()``（= show）より前にここを通るので、オートハイド
        # から復帰する瞬間だけ矩形更新が no-op になり、前回ズームの黄枠のまま
        # 表示されてしまう。数回の除算なので隠れていても計算して構わない。
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        rect = geom.minimap_view_rect(
            h_value=hbar.value(), v_value=vbar.value(),
            content=self._label.size(), viewport=self.viewport().size(),
        )
        if rect is None:
            return
        self._minimap.set_view_rect(*rect)

    def _on_minimap_panned(self, fx: float, fy: float) -> None:
        """Center the viewport on the fractional point clicked in the minimap."""
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        target_x, target_y = geom.minimap_pan_values(
            fx, fy,
            content=self._label.size(), viewport=self.viewport().size(),
        )
        hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), target_x)))
        vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), target_y)))

    # -------------------------------------------- orientation (要件 F11)

    def rotate_right(self) -> None:
        """Rotate the displayed image 90° clockwise (display-only, F11)."""
        if not self._orientable():
            return
        self._rotation = (self._rotation + 90) % 360
        self._rebuild_oriented()

    def rotate_left(self) -> None:
        """Rotate the displayed image 90° counter-clockwise (display-only)."""
        if not self._orientable():
            return
        self._rotation = (self._rotation - 90) % 360
        self._rebuild_oriented()

    def flip_horizontal(self) -> None:
        """Mirror the displayed image left↔right (display-only, F11)."""
        if not self._orientable():
            return
        self._flip_h = not self._flip_h
        self._rebuild_oriented()

    def _orientable(self) -> bool:
        """回転・反転できるものが載っているか（静止画ソース or アニメーション）."""
        return self._base_original is not None or self._movie is not None

    def _rebuild_oriented(self) -> None:
        """Recompute ``_original`` / ``_original_pixmap`` from the pristine
        source under the current rotation + flip, then repaint.

        Nothing is ever written to disk — this only affects what the view
        shows.  90° steps use ``transpose`` (lossless, no resampling).
        アニメーションは原寸フレームを描くたびに向きを掛ける
        （``render_frame``）ので、ここでは寸法の通知と再描画だけ。
        """
        base = self._base_original
        if self._movie is not None:
            natural = self._natural_size() or QSize()
            self.image_info_changed.emit(natural.width(), natural.height())
        elif base is None:
            return
        else:
            img = base
            rot = self._rotation % 360
            if rot == 90:
                img = img.transpose(Image.ROTATE_270)   # PIL rotates CCW; 90° CW
            elif rot == 180:
                img = img.transpose(Image.ROTATE_180)
            elif rot == 270:
                img = img.transpose(Image.ROTATE_90)
            if self._flip_h:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            self._original = img
            self._original_pixmap = pil_to_qpixmap(img)
            self.image_info_changed.emit(img.width, img.height)
        # The cached minimap thumbnail was built from the old orientation.
        self._minimap_pixmap = None
        self._minimap_pixmap_source = -1
        # パッチの下敷きも旧向きのまま — 破棄して次の ``_refresh`` で作り直す
        # （canvas モード自体は維持: 直後の再レンダが新向きで描く）。
        self._patch_base = None
        self._patch_base_source = -1
        self._refresh()
        self._show_zoom_overlay()

    # ------------------------------------------------------------ star keys

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Digit 0–5 sets / clears the user star on the shown image.  Same
        # gating as the grid (``GalleryView.keyPressEvent``); the host decides
        # whether a star store exists.
        intent = ivinput.key_intent(event.key(), event.modifiers())
        if isinstance(intent, ivinput.StarKey):
            self.star_key_requested.emit(intent.value)
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------- context menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        menu = self._build_context_menu()
        menu.exec(event.globalPos())
        # ``QMenu(self)`` is parented to this view, so exec() only hides it —
        # without this every right-click leaves a menu + its QAction set on the
        # view until the whole window is destroyed.
        menu.deleteLater()

    def _build_context_menu(self) -> QMenu:
        """Assemble the preview context menu (split out for testability).

        席固有の表示系（フィット / 実寸 / 全画面 / ズーム維持 / ミニマップ /
        回転 / 反転 / 画像をコピー）が先、続いて 4 席共通のブロック
        （:func:`append_entry_verbs` — 開く / コピー / 探す / 印）。類似検索も
        共通ブロックの「探す」節に置き、4 席で配置を揃える。印の口は祖先
        （``ContentView`` / ``LightboxWindow``）から取るので、分割 / 最大化 /
        全画面の 3 面が 1 実装で揃う。
        """
        menu = QMenu(self)
        fit_act = menu.addAction(t("viewer.image_view.fit_to_window"))
        fit_act.triggered.connect(self._fit_to_window)
        actual_act = menu.addAction(t("viewer.image_view.actual_size"))
        actual_act.triggered.connect(self._set_actual_size)
        if self._fullscreen_available or self._fullscreen_exit_mode:
            # 全画面の中では「入口」ではなく「出口」を名乗る。行き先は
            # 同じシグナルで、ホスト（ライトボックス）が閉じる。
            fs_act = menu.addAction(
                t("viewer.image_view.fullscreen_exit")
                if self._fullscreen_exit_mode
                else t("viewer.image_view.fullscreen")
            )
            fs_act.setEnabled(
                self._fullscreen_exit_mode or self._current_path is not None
            )
            fs_act.triggered.connect(self.fullscreen_requested.emit)
        menu.addSeparator()
        persist_act = menu.addAction(t("viewer.image_view.zoom_persist"))
        persist_act.setCheckable(True)
        persist_act.setChecked(self._zoom_persist)
        persist_act.toggled.connect(self._on_zoom_persist_menu_toggled)
        minimap_act = menu.addAction(t("viewer.image_view.show_minimap"))
        minimap_act.setCheckable(True)
        minimap_act.setChecked(self._minimap_enabled)
        minimap_act.toggled.connect(self._on_minimap_menu_toggled)
        # Display-only rotate / flip (要件 F11) — アニメーションも各フレームを
        # 描くときに向きを掛けるので同じ項目を出す。
        if self._orientable():
            menu.addSeparator()
            rot_r_act = menu.addAction(t("viewer.image_view.rotate_right"))
            rot_r_act.triggered.connect(self.rotate_right)
            rot_l_act = menu.addAction(t("viewer.image_view.rotate_left"))
            rot_l_act.triggered.connect(self.rotate_left)
            flip_act = menu.addAction(t("viewer.image_view.flip_horizontal"))
            flip_act.triggered.connect(self.flip_horizontal)
        menu.addSeparator()
        copy_act = menu.addAction(t("viewer.image_view.copy_image"))
        # 画素が載っているかで判定する。``_apply_failed`` は
        # 失敗カードの [再読み込み] 用に ``_current_path`` を残すので、パスだけ
        # を見ると失敗カード表示中も「画像をコピー」が有効に見えたまま
        # ``copy_image_to_clipboard`` の「コピーできる画像がありません」に
        # 突き当たる。Ctrl+C / 編集メニュー側と同じ述語へ寄せる。
        copy_act.setEnabled(self.has_image())
        copy_act.triggered.connect(self.copy_image_to_clipboard)
        if self._current_path is not None:
            menu.addSeparator()
            append_entry_verbs(
                menu,
                EntryMenuContext(
                    path=self._current_path, is_dir=False,
                    similar_search=(
                        self._on_similar_search_triggered
                        if self._similar_search_available else None
                    ),
                    curation=curation_hooks_from_ancestors(self), host=self,
                ),
            )
        return menu

    def _on_similar_search_triggered(self) -> None:
        path = self._current_path
        if path is None:
            return
        self.similar_search_requested.emit(path, self._seed_pixmap())

    _SEED_PIXMAP_MAX_EDGE = 160

    def _seed_pixmap(self) -> QPixmap | None:
        """Build a ~160px thumbnail of the currently displayed image.

        Prefers the full-resolution source (``_original_pixmap``); falls
        back to the current GIF frame.  Returns ``None`` when nothing is
        loaded — the seed-preview row then falls back to a generic icon.
        """
        source: QPixmap | None = self._ensure_original_pixmap()
        if source is None and (frame := self._movie_frame()) is not None:
            source = QPixmap.fromImage(frame)
        if source is None or source.isNull():
            return None
        edge = self._SEED_PIXMAP_MAX_EDGE
        return source.scaled(
            edge, edge, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )

    def _on_zoom_persist_menu_toggled(self, checked: bool) -> None:
        self.set_zoom_persist(checked)
        self.zoom_persist_toggled.emit(checked)

    def _on_minimap_menu_toggled(self, checked: bool) -> None:
        self.set_minimap_enabled(checked)
        self.minimap_toggled.emit(checked)


def apply_state(view: ImageView, state: "ViewerState") -> None:
    """``ViewerState`` の ImageView 設定を *view* へ一括反映する.

    ImageView のインスタンスは 3 つある（中央プレビュー ``ContentView._image``・
    フォルダプレビューの中央画像・閲覧モード ``LightboxWindow._view``）。「state を ImageView に反映する」
    コードを呼び出し元ごとに手書きすると、設定が 1 項目増えるたびに
    片側だけ配線されて取り残される。ImageView が state
    から受け取る設定は**必ずこの関数に足す**こと — 呼び出し元
    （``ContentView.apply_cache_settings`` / ``apply_view_settings`` と
    ``LightboxWindow.apply_view_state``）は全員ここを通るので、全インスタンス
    へ自動的に届く。

    対象 7 項目: キャッシュ予算 3 種（MiB → バイト換算はここで行う）、
    先読み半径、ズーム維持、ミニマップ表示、フィットの再適用（
    F03「等倍以上に拡大しない」は ``view_prefs`` のモジュール変数で、
    呼び出し元が先にそれを書いてからここへ来る規約）。
    """
    mib = 1024 * 1024
    view.reconfigure_cache(
        max_bytes=max(1, state.imageview_cache_max_mib) * mib,
        max_entries=max(1, state.imageview_cache_max_entries),
        max_single_bytes=max(1, state.imageview_cache_max_single_mib) * mib,
        prefetch_radius=state.imageview_prefetch_radius,
    )
    view.set_zoom_persist(state.image_zoom_persist)
    view.set_minimap_enabled(state.image_minimap_enabled)
    # F03 は state ではなく view_prefs のモジュール変数経由で届くが、その
    # 反映点（＝再フィット）はここに集約する。呼び出し順は
    # ``main_window._apply_settings_live`` が set_image_fit_no_upscale →
    # apply_view_settings → apply_state なので、ここでは新しい値が読まれる。
    view.refresh_fit()


def connect_state_writeback(
    view: ImageView,
    *,
    zoom_persist: Callable[[bool], None],
    minimap: Callable[[bool], None],
) -> None:
    """ImageView の「ビュー内トグル → ホストへの書き戻し」を一括配線する.

    :func:`apply_state`（state → view の**読み**方向）の**対**にあたる
    書き戻し方向の集約点。ImageView のインスタンスは 3 つある（中央プレビュー
    ``ContentView._image``・フォルダプレビュー・全画面）が、右クリック
    メニューは共用のため**どれでも項目は出るし押せる**。
    ``zoom_persist_toggled`` / ``minimap_toggled`` を一部の面にしか配線しないと、
    その他の面でのトグルは黙って捨てられ、次に開いたとき :func:`apply_state`
    が保存値で上書きして変更が消える。

    :func:`apply_state` の「片側だけ配線されて取り残される」事故と同じ形なので、
    読み方向だけでなく書き戻し方向にも集約点を置く。以後、ImageView が
    ホストへ返すビュー設定トグルは**必ずこの関数に足す**こと。
    """
    view.zoom_persist_toggled.connect(zoom_persist)
    view.minimap_toggled.connect(minimap)


__all__ = [
    "ZOOM_READOUT_EMPTY",
    "ImageView",
    "_DecodeErrorCard",
    "_ImageCanvasLabel",
    "_MAX_TARGET_PIXELS",
    "_CTRL_GLYPHS",
    "_CTRL_ICON_COLOR",
    "_ControlBar",
    "_MinimapOverlay",
    "_ZoomOverlayLabel",
    "_clamp_pixel_budget",
    "_ctrl_icon",
    "_is_animated_bytes",
    "apply_state",
    "connect_state_writeback",
    "format_zoom_readout",
]
