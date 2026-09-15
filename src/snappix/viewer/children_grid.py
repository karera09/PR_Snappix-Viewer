"""Shared base widget for the viewer's left and right children-list panes.

Both panes display one folder's immediate children (folders + files) in a
custom-painted :class:`gallery_view.GalleryView` supporting three layouts:
list, uniform-square grid, and Eagle-style **justified** grid.  This base
owns all the orchestration — directory scanning, thumbnail loading, the
aspect-ratio probe + cache, slider/size handling, and the public API the
main window depends on — while ``GalleryView`` owns rendering, hit-testing,
selection, and scrolling.

Subclasses (``PostGrid`` / ``FileListView``) add only pane-specific chrome
and signal handling via the hook methods below.

Key behaviours (see ``docs/claude/viewer.md``):

* **Viewport-bounded loading** — thumbnails AND aspect probes are issued
  only for the buffered visible range, off ``GalleryView.visible_range_
  changed``.  Nothing is loaded eagerly on populate.
* **Zero-reflow on revisit** — tiles are seeded with cached aspect ratios
  (``ThumbMetaCache.get_many``) at build time, so a warm folder lays out
  justified immediately with no jitter.  Cold folders settle quickly as the
  fast header probe fills aspects in.
* **Icon flush coalescing** — decoded thumbnails accumulate for 50 ms;
  the per-tick ``set_thumb`` cap adapts to the visible tile count
  (see :func:`_adaptive_flush_limit`).
* **QPixmap on the GUI thread only** — the loader hands back ``QImage``;
  the flush converts and pushes to the view.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import align_form_labels, hint_style
from ..common.ui.timers import DebounceMode, Debouncer
from ._slider import enable_click_jump
from .aspect_probe import AspectProbeScanner
from .empty_state import EmptyAction
from .folder_scan import (
    FolderEntry,
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    VIDEO_SUFFIXES,
    apply_cached_preview,
    is_meta_or_marker_file,
)
from .gallery_view import GalleryView, Tile
from .justified_layout import LayoutParams
from .pending import Pending
from .perf import measure
from .scan_worker import ChildrenScanner
from .thumbnail_loader import ThumbnailLoader, fitted_edge

# Media file tiles the fullscreen lightbox (閲覧モード) can play back — images
# plus videos (see lightbox_parts/scan.py::PLAYLIST_SUFFIXES).  Used by
# ``media_tile_paths`` to build a flat search-result playlist (G07).
_LIGHTBOX_MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES


class SelectRequest(NamedTuple):
    """再構築を跨いで預ける選択の要求（``Pending`` に積む 1 値）。

    *ancestor_ok* が真なら、正確なタイルが（もう）無いとき「一覧に居る最も
    深い祖先」= それを含む表示中フォルダへ落として当てる。呼び出し口ごとの
    opt-in で、履歴復元の検索は非同期結果が対象を再浮上させるまで待つ必要が
    あるので偽のまま（viewer.md）。

    2 値を 1 値に畳んであるのは、パスと祖先フォールバックが別フィールドだと
    「片方だけ積む / 片方だけ消す」が書けてしまうため——祖先フォールバックは
    それ単独では意味を持たない。
    """

    path: Path
    ancestor_ok: bool


#: 表示形式コンボの選択肢 ``(key, i18n ラベルキー)`` — **左右ペイン共通の単一
#: 情報源**。両ペインが手で ``addItem`` していた頃は選択肢の順が完全な逆順
#: (左 justified→square→list / 右 list→square→justified) になっていて、
#: コード上いかなる理由付けも無かった (UIレビュー 2026-08-28 N-75)。
VIEW_LAYOUT_CHOICES: tuple[tuple[str, str], ...] = (
    ("justified", "viewer.common.layout_justified"),
    ("square", "common.view.grid"),
    ("list", "common.view.list"),
)


#: 表示設定の行レイアウトの間隔（ラベル整列はこの値を前提にしない）。
_VIEW_ROW_SPACING = 6


def _labeled_row(label_key: str, widget: QWidget) -> tuple[QHBoxLayout, QLabel]:
    """``ラベル [コントロール]`` の 1 行（表示設定 3 行の共通の型）。

    ラベルも返すのは、3 行ぶんを集めて
    :func:`~snappix.common.ui.align_form_labels` へ一度に渡すため（その場で
    作って捨てるとラベル列が揃えられない — N-39）。
    """
    row = QHBoxLayout()
    row.setSpacing(_VIEW_ROW_SPACING)
    label = QLabel(t(label_key))
    row.addWidget(label)
    row.addWidget(widget, 1)
    return row, label

# Icon-flush batching.  Each flushed icon costs a ``QPixmap.fromImage`` +
# a tile-region repaint on the GUI thread, so one tick must stay well under
# a frame; but a *fixed* 16-cap makes a large viewport (justified layout at
# a low slider value on a wide pane easily shows 60+ tiles) converge in
# visible steps — (visible/16) × 50 ms ≈ 200 ms+ of staggered pop-in.  The
# cap therefore adapts to the visible tile count: floor 16 (small panes keep
# the historical behaviour), half the visible count otherwise, hard-capped
# at 64 to bound the worst-case main-thread cost per tick.
_FLUSH_BATCH_SIZE = 16
_FLUSH_BATCH_MAX = 64


def _adaptive_flush_limit(visible_count: int) -> int:
    """Per-tick ``set_thumb`` cap for *visible_count* on-screen tiles."""
    return min(_FLUSH_BATCH_MAX, max(_FLUSH_BATCH_SIZE, visible_count // 2))

# Grid geometry shared between ``_build_params`` (the LayoutParams handed to
# the view) and the slider-maximum calculations (``_pane_max_icon_size`` /
# ``_current_size_max``).  A single definition keeps the "max ≈ two tiles
# per row" justified contract from silently drifting if the margin changes.
_GRID_MARGIN = 6
_GRID_SPACING = 6

# A tile's box must grow by more than this many physical pixels (longest
# edge) before we re-fetch a higher-resolution thumbnail.  Keeps rounding
# jitter and sub-pixel justify drift from churning the loader, while still
# catching the real aspect-settle / slider growth that causes blur.
_EDGE_UPGRADE_MARGIN = 24

# Default tile aspect ratios for non-measurable sources, so justified rows
# can still pack them sensibly without a probe.
_DEFAULT_ASPECT_PDF = 0.7071  # A4 portrait-ish
_DEFAULT_ASPECT_VIDEO = 16 / 9
_DEFAULT_ASPECT_GENERIC = 1.0


# --------------------------------------------------- entry → Tile の単一写像


def aspect_source(entry: FolderEntry) -> Path | None:
    """Image path whose aspect represents this entry, or ``None``.

    Folders use their resolved representative thumbnail; files use
    themselves when they're images.  Non-image files / unresolved folders
    have no measurable aspect (a default is used instead).
    """
    if entry.is_dir:
        tp = entry.thumbnail_path
        if tp is not None and tp.suffix.lower() in IMAGE_SUFFIXES:
            return tp
        return None
    if entry.path.suffix.lower() in IMAGE_SUFFIXES:
        return entry.path
    return None


def default_aspect(entry: FolderEntry) -> float:
    """計測できないエントリに与える既定のアスペクト比（種別ごと）。"""
    if entry.is_dir:
        return _DEFAULT_ASPECT_GENERIC
    suffix = entry.path.suffix.lower()
    if suffix in PDF_SUFFIXES:
        return _DEFAULT_ASPECT_PDF
    if suffix in VIDEO_SUFFIXES:
        return _DEFAULT_ASPECT_VIDEO
    return _DEFAULT_ASPECT_GENERIC


def build_tile(
    entry: FolderEntry,
    *,
    key: str,
    caption: str,
    tooltip: str = "",
    warn: str = "",
    aspect_of: Callable[[FolderEntry], Path | None] = aspect_source,
) -> Tile:
    """``FolderEntry`` → :class:`~gallery_view.Tile` の**唯一の写像**。

    ``GalleryView`` を置く席は 3 つ（左グリッド / 右一覧 = :class:`ChildrenGrid`
    の 2 つと、フォルダプレビューの子グリッド）。席ごとに違うのはキャプション・
    ツールチップ・キーの名前空間だけで、**エントリから決まる表示属性**（薄く
    描くメタ / マーカー・代替サムネイルの警告枠・アスペクトの既知 / 未知）は
    同じ規則であるべきもの。席ごとに ``Tile(...)`` を手で組んでいたときは、
    そのどれかが片側に無い形（フォルダプレビューだけ post.md が本編と同格に
    描かれる等）が繰り返し生まれた。

    *aspect_of* は「このエントリのアスペクトを測れる画像はどれか」という席
    ごとの方針（既定は :func:`aspect_source`）。``PostGrid`` は到達不能行を
    計測不能へ落とすためにこれを差し替える。
    """
    tile = Tile(
        key=key,
        path=entry.path,
        is_dir=entry.is_dir,
        caption=caption,
        entry=entry,
        is_fallback=entry.is_fallback_thumbnail,
        tooltip=tooltip,
        # post.md / #thumb# は一覧に残すが本編と同格には描かない
        # (UIレビュー 07-25 #52)。
        dimmed=not entry.is_dir and is_meta_or_marker_file(entry.path),
        warn=warn,
    )
    if aspect_of(entry) is not None:
        # Measurable now (an image file, or a folder with a resolved image
        # thumbnail): start square + unknown and let the probe settle it.
        tile.aspect = 1.0
        tile.aspect_known = False
    elif entry.is_dir and not entry.thumbnail_resolved:
        # A not-yet-resolved folder IS measurable once its representative
        # thumbnail loads — keep it unknown so the decoded-thumbnail aspect
        # (the ``set_aspect`` fallback in ``_flush_icon_updates``) settles
        # it.  Marking it a known square here is what left folders stuck in
        # square boxes (never justifying to the real image shape).
        tile.aspect = 1.0
        tile.aspect_known = False
    elif not entry.is_dir and entry.thumbnail_path is not None:
        # Video / PDF: not measurable *up front* (the aspect probe only
        # reads still images), but a real thumbnail IS decoded for these —
        # ``folder_scan.THUMBNAILABLE_SUFFIXES`` covers PDF + video, so the
        # first frame / first page lands like any image and the
        # ``set_aspect`` fallback in ``_flush_icon_updates`` settles the
        # true ratio.  Seed the type default as the pre-arrival shape but
        # keep it UNKNOWN (same treatment as an unresolved folder above):
        # marking it known locked a 9:16 portrait clip inside a 16:9 box
        # for good — the frame was drawn as a narrow strip with ~68% of the
        # tile left blank, and no code path could ever correct it (#F1D-3).
        tile.aspect = default_aspect(entry)
        tile.aspect_known = False
    else:
        # Truly non-measurable (a no-thumbnail file like post.md, or a
        # resolved folder with no image) → fixed default, marked known so
        # it never reflows.  Not expandable: a justified row of only these
        # stays at target height instead of ballooning one tile into a
        # full-width square.
        tile.aspect = default_aspect(entry)
        tile.aspect_known = True
        tile.expandable = False
    return tile


def _button_specs(actions: list[EmptyAction]) -> list[tuple[str, str]]:
    """:class:`EmptyAction` 列 → ``GalleryView`` が要る ``(label, tooltip)`` 列.

    ビューはラベルと添字しか知らない（押下先を持たせると「表示は変わったのに
    押下先が古い」形の二重管理が戻る — 項目 #95）。
    """
    return [(a.label, a.tooltip) for a in actions]


#: 走査失敗カードの理由行の上限（文字）。空白の無い長いパスは折り返せず、
#: これを越えると 1 行のつもりがカード幅を越えて右端で切れる。
_SCAN_REASON_MAX = 80


def _trim_reason(reason: str) -> str:
    """理由行を 1 行に収まる長さへ詰める（末尾は省略記号）."""
    if len(reason) <= _SCAN_REASON_MAX:
        return reason
    return reason[: _SCAN_REASON_MAX - 1].rstrip() + "…"


class ChildrenGrid(QWidget):
    """Base widget for both viewer children-list panes.

    Signals (re-emitted from the hosted :class:`GalleryView`):

    * ``folder_selected(Path)`` / ``folder_activated(Path)``
    * ``file_selected(Path)`` / ``file_activated(Path)``

    Constructor parameters mirror the legacy widget plus ``meta_cache`` (a
    :class:`thumb_meta_cache.ThumbMetaCache` for aspect-ratio seeding;
    ``None`` disables the warm-cache path and the layout still works via the
    live probe + thumbnail-derived aspects).
    """

    folder_selected = Signal(Path)
    folder_activated = Signal(Path)
    file_selected = Signal(Path)
    file_activated = Signal(Path)
    #: Subclass opt-in (see :meth:`set_folder`): a same-folder re-navigation
    #: keeps the current tiles visible while the rescan runs instead of
    #: blanking the pane (stale-while-revalidate).
    _keep_view_on_same_folder = False
    #: Fired after every tile rebuild (`_set_entries_as_tiles` — scan landing,
    #: re-sort, filter).  The stage-mode image track mirrors the right pane's
    #: tile set from this (a cheap no-op while browse mode is showing), the
    #: same way PostGrid's ``counts_changed`` drives the sibling-post track.
    tiles_changed = Signal()
    #: Backspace pressed on the pane's grid (``GalleryView.keyPressEvent``) —
    #: 「上の階層へ」, the same meaning as the ↑ button / Alt+Up.  Declared on
    #: the **base** (UIレビュー 2026-08-28 N-26) because the key is a property
    #: of the shared grid body: it used to exist only on ``PostGrid``, so
    #: Backspace was silently dead whenever the 情報パネル一覧 held focus even
    #: though the shortcut table documents it as a plain 「グリッド」 key.
    #: ``PostGrid`` also emits it from its ↑ chrome button.
    go_up_requested = Signal()

    # Subclass-tunable layout constants (defaults match the left pane).
    _ICON_SIZE_MIN: int = 96
    _LIST_ICON_SIZE_MIN: int = 16
    _CAPTION_PAD: int = 24
    _LIST_MODE_ICON_SIZE: int = 24
    _WITH_METADATA: bool = False

    def __init__(
        self,
        *,
        loader: ThumbnailLoader,
        icon_size: int = 160,
        list_icon_size: int | None = None,
        icon_size_max: int = 320,
        view_mode: str = "icon",
        thumb_layout: str = "justified",
        loader_key_prefix: str = "",
        meta_cache=None,
        folder_cache=None,
        search_index=None,
        probe_parallelism: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._loader = loader
        self._loader.loaded.connect(self._on_thumb_loaded)
        self._loader.failed.connect(self._on_thumb_failed)
        self._meta_cache = meta_cache
        # Folder-preview resolution cache (representative image + post.md).
        # Shared with the scanner + loader pools so a warm folder resolves
        # without a scandir.  ``None`` disables the feature.
        self._folder_cache = folder_cache
        # Search index warmed from the shallow scan (file-name search).
        self._search_index = search_index

        self._icon_size_max = max(self._ICON_SIZE_MIN, int(icon_size_max))
        self._icon_size = max(
            self._ICON_SIZE_MIN, min(self._icon_size_max, int(icon_size))
        )
        # The user's *intended* icon size (icon view).  ``_icon_size`` is the
        # effective value actually laid out — it may be clamped below this
        # when the pane is narrow.  Re-clamping from the desired value each
        # time keeps a transiently-tiny viewport (e.g. during the first show /
        # a splitter drag) from permanently shrinking the thumbnails.
        self._desired_icon_size = self._icon_size
        raw_list = (
            self._LIST_MODE_ICON_SIZE if list_icon_size is None else int(list_icon_size)
        )
        self._list_icon_size = max(
            self._LIST_ICON_SIZE_MIN, min(self._icon_size_max, raw_list)
        )
        self._thumb_render_size = self._icon_size

        self._view_mode = view_mode if view_mode in ("icon", "list") else "icon"
        self._thumb_layout_mode = (
            thumb_layout if thumb_layout in ("square", "justified") else "justified"
        )
        self._key_prefix = loader_key_prefix
        self._root_or_folder: Path | None = None
        # 再構築を跨いで預ける選択（``SelectRequest`` = パス + 祖先フォール
        # バック可否）。積むのは ``queue_pending_select`` /
        # ``set_pending_select``、降ろすのは ``_resolve_pending_select`` が
        # **当てられたときだけ**（``Pending.consume``）。
        self._pending_select: Pending[SelectRequest] = Pending()
        # True only while _resolve_pending_select is applying its selection
        # (the selection-changed emit runs synchronously inside).  Lets hosts
        # distinguish a programmatic pending-select landing from a user click
        # (split-view redesign 2026-07 — see is_resolving_pending_select).
        self._resolving_pending: bool = False
        # Deferred scroll restore (B-13): a scroll offset to re-apply once the
        # tiles are built + laid out.  Set by ``set_pending_scroll`` (from the
        # nav-history restore); applied one event-loop tick after
        # ``_set_entries_as_tiles`` so the layout's scroll range exists and the
        # value wins over ``_resolve_pending_select``'s ensure-visible.  An unarmed
        # slot (the common case) means "no restore pending" — a fresh drill-down
        # keeps the default top-of-list scroll.
        self._pending_scroll: Pending[int] = Pending()

        # Aspect-ratio probe (header reads → real dims for justified layout).
        self._probe = AspectProbeScanner(self)
        self._probe.set_parallelism(probe_parallelism)
        self._probe.probed.connect(self._on_aspect_probed)
        self._last_probe_keys: frozenset[str] = frozenset()
        # Image paths (str) the prober reported as unreadable / unidentified
        # (a (0, 0) row).  Remembered for the current tile set so a corrupt /
        # unsupported file isn't re-probed on every scroll pass (one NAS
        # header-read round-trip per visit otherwise).  Cleared whenever the
        # tile set is rebuilt (paths / mtimes may have changed).
        self._probe_failed: set[str] = set()

        # True between :meth:`shutdown` (= this pane's ``closeEvent``) and the
        # next ``showEvent``.  Gates the self-driving thumbnail work so a
        # stopped timer that somehow re-arms — or a queued signal that lands
        # after the window drained the loader pools — can't submit a fresh
        # decode into an already-closed pane (R2A-4).
        self._shutdown = False

        # Thumbnail icon-swap coalescing.
        self._pending_icons: dict[str, "QImage"] = {}
        self._icon_flush_timer = Debouncer(
            self, 50, self._flush_icon_updates, mode=DebounceMode.LEADING_WINDOW
        )

        # Debounced visible-range request (scroll / resize / relayout).
        self._visible_request_timer = Debouncer(
            self,
            40,
            self._request_visible_thumbnails,
            mode=DebounceMode.LEADING_WINDOW,
        )

        # Debounced size-driven re-decode after the slider settles.
        self._resize_refresh_timer = Debouncer(
            self, 160, self._on_resize_refresh, mode=DebounceMode.TRAILING
        )

        self._scanner = self._make_scanner()
        self._scanner.finished.connect(self._on_scan_finished)
        self._scanner.failed.connect(self._on_scan_failed)
        self._scanner.partial.connect(self._on_scan_partial)
        self._pending_scan_generation = 0
        # 直近の走査で分類できなかった子の件数（0 = 穴なし）。``failed`` と
        # 違って一覧は有効なので、結果は出したうえでこれを添えて告げる。
        self._scan_unclassified = 0
        # Last scan failure message (I01) — ``None`` while healthy.  Set when
        # the shallow scan itself blows up (offline NAS / permission error) so
        # the pane shows an error state + 再試行 instead of "empty"; cleared on
        # the next request / successful result.
        self._scan_error: str | None = None
        # 空状態オーケストレータ（:func:`empty_state.resolve_empty_state`）が
        # 「この席が案内カードを持つ」と裁定しているか（項目#196）。既定 True
        # = 従来どおり自走。ウィンドウが :meth:`refresh_empty_state` の
        # ``allowed`` で更新し、**以後の内部リビルドにも効く**（状態にして
        # あるので、次の裁定まで黙ったカードが勝手に復活しない）。
        self._empty_state_allowed = True
        # Whether a requested scan hasn't reported back yet — drives the
        # delayed "読み込み中…" hint (C02) so a cold-NAS scan doesn't leave the
        # pane a silent blank for seconds.
        self._awaiting_scan = False
        self._loading_hint_timer = Debouncer(
            self, 450, self._show_loading_hint, mode=DebounceMode.ONE_SHOT
        )

        self._build_ui()
        # 空状態カードのボタン（何個でも — UIレビュー 07-25 #26 の主 / 副も
        # この列の先頭 2 つ）。飛んでくるのは添字だけなので、意味は
        # :meth:`_current_empty_actions` の**同じ列**で引き直す（項目 #95）。
        self._view.empty_action_clicked.connect(self._on_empty_action)
        # Backspace = 上の階層へ（N-26）。両ペイン共通なのでここで中継する。
        self._view.go_up_requested.connect(self.go_up_requested.emit)

    # ----------------------------------------------------- hooks (subclass)

    def _make_scanner(self) -> ChildrenScanner:
        return ChildrenScanner(
            with_metadata=self._WITH_METADATA,
            folder_cache=self._folder_cache,
            search_index=self._search_index,
            parent=self,
        )

    def _build_chrome(self, outer_layout: QVBoxLayout) -> None:
        del outer_layout  # subclasses add header rows + size slider

    def _caption_for(self, entry: FolderEntry) -> str:
        return entry.title if entry.is_dir else entry.path.name

    def _tooltip_for(self, entry: FolderEntry) -> str:
        """Hover tooltip for an entry's tile (C01) — full name + path.

        Grid captions elide aggressively, so every tile carries its full
        title/name plus the absolute path.  A fallback representative
        thumbnail (the #thumb# creator icon shown despite the exclude
        toggle) additionally explains its warning frame here.
        """
        head = entry.title if entry.is_dir else entry.path.name
        lines = [head]
        if str(entry.path) != head:
            lines.append(str(entry.path))
        # ♡N / 🔒N バッジは初見で意味が伝わりにくい (UIレビュー #23) —
        # ホバーで言葉に展開する（凡例はショートカット一覧の最下部にもある）。
        if getattr(entry, "favorites", None):
            lines.append(
                t("viewer.common.tooltip_favorites", n=entry.favorites)
            )
        if getattr(entry, "locked_count", 0):
            lines.append(
                t("viewer.common.tooltip_locked", n=entry.locked_count)
            )
        if entry.is_fallback_thumbnail:
            lines.append(t("viewer.common.fallback_thumb_note"))
        return "\n".join(lines)

    def _on_scan_finished_payload(
        self, generation: int, entries: list[FolderEntry],
    ) -> None:
        del generation
        self._set_entries_as_tiles(list(entries))

    def _on_scan_failed_payload(self, message: str) -> None:
        """Hook: show the scan-failure state (I01).  *message* is the raw error.

        The base paints the generic "読み込めませんでした" explanation **plus the
        reason the OS gave**; subclasses may additionally sync their chrome (the
        left pane resets its breadcrumb and notifies the window).
        """
        self._show_scan_error_state(message)

    def _show_scan_error_state(self, message: str = "") -> None:
        self._view.set_empty_state(
            self._scan_error_text(message), icon_name="alert-triangle",
            actions=_button_specs(self._scan_error_actions()),
        )

    @staticmethod
    def _scan_error_text(message: str) -> str:
        """走査失敗カードの本文 — 定型の説明 + OS が返した理由 1 行。

        (UIレビュー 2026-09-11 N-75) 理由は例外メッセージとしてログにだけ
        残り、画面には「ネットワークドライブの接続やアクセス権を確認して
        ください」という総称しか出ていなかった — 「共有名が見つからない」と
        「アクセスが拒否されました」では次の一手が違う。例外文字列にはパスも
        入るが、それは既にカードの文脈そのもの（ログにも出ている）。

        カードを縦に割らないよう **先頭 1 行だけ**を採り、さらに長さでも
        切る: 例外文字列は「…: <絶対パス>」の形で、UNC や深い階層では 1 行が
        数百字になる。空白を含まないパスは折り返せず、カード幅を越えて右端で
        クリップされる（＝宣言どおりの 1 行に収まらない）。
        """
        text = t("viewer.common.scan_error")
        reason = (message or "").strip().splitlines()
        if reason and reason[0].strip():
            text += "\n" + t(
                "viewer.common.scan_error_reason",
                error=_trim_reason(reason[0].strip()),
            )
        return text

    def _scan_error_actions(self) -> list[EmptyAction]:
        """走査失敗カード（⚠ + [再試行]）のボタン列 — 描画と押下で共有する。

        理由を読んでも分からないとき（権限・SMB 方言）に、報告用の
        ``viewer.log`` へ 1 クリックで辿り着けるようにする（N-75）。ホストが
        その導線を持たない構成（単体起動・テスト）では 再試行 だけになる。
        """
        actions = [
            EmptyAction(t("common.action.retry"), self._retry_after_scan_error)
        ]
        open_logs = getattr(self.window(), "open_logs_folder", None)
        if callable(open_logs):
            actions.append(
                EmptyAction(t("viewer.main_window.open_logs_folder"), open_logs)
            )
        return actions

    def has_scan_error(self) -> bool:
        """直近の走査が失敗したまま（I01 のエラーカードを出している）か。

        ウィンドウの空状態オーケストレータ（:mod:`empty_state`）が「案内の主は
        どの席か」を決めるための観測値。エラーは**このペイン自身**のカードが
        持つので、ウィンドウはそれを上書きしない側に倒す。
        """
        return self._scan_error is not None

    def empty_state_kind(self) -> str:
        """0 タイルの分類（タイルがあるときは ``""``）— オーケストレータ入力.

        基底は分類を持たないので、settled-empty のときだけサブクラスの
        :meth:`_empty_state_kind` を通す。「タイルがある」を ``""`` で表すのは
        リゾルバ側の約束（:attr:`empty_state.EmptyStateInput.grid_kind`）。
        """
        if self._view.tile_count() > 0:
            return ""
        return self._empty_state_kind()

    def _empty_state_kind(self) -> str:
        """0 タイルの分類（基底は分類を持たないので ``""``）.

        :meth:`_empty_state_message` / :meth:`_empty_state_icon` /
        :meth:`_empty_state_actions` と同じく、**基底に何もしない既定を置く**
        フックの 1 つ。分類を持つのは左ペイン（``PostGrid._empty_state_kind``
        → :mod:`.grid_empty_state`）だけで、右一覧は自前の 1 文言で足りる。
        """
        return ""

    def refresh_empty_state(self, *, allowed: bool | None = None) -> None:
        """0 タイルのときだけ空状態（文言 / ボタン / アイコン）を再適用する。

        ``_set_entries_as_tiles`` の着地時と、**分類の入力がグリッドの外から
        変わったとき**（ウィンドウの空状態オーケストレータが
        :meth:`post_grid.PostGrid.set_first_run` で初回起動を教えたとき）の
        両方が通る単一の適用点。タイルがある間は何もしない — 空状態はタイル 0
        の面でしか描かれない（``GalleryView`` の塗りも同じ条件でゲートされて
        いる）ので、ここで書くと現れた瞬間に消える文言を作るだけになる。
        走査失敗中は自前のエラーカード（⚠ + [再試行]）が勝つ。

        *allowed* は空状態オーケストレータの裁定（項目#196）:
        :func:`empty_state.resolve_empty_state` が返す ``plan.grid`` の役割が
        ``PRIMARY`` でないとき、この席は**黙る**（案内カードは 1 画面に 1 枚
        — 07-25 #51）。以前 ``plan.grid`` は production では誰も読まず、
        「PRIMARY は多くとも 1 席」はリゾルバ内部とその単体テストの中でしか
        成立していなかった。``None``（既定）は「裁定を更新しない」で、
        グリッド内部からの再適用が最後の裁定を引き継ぐ。走査失敗カードは
        裁定より前に勝つ — 席が黙っていても失敗は伝える必要がある。
        """
        if allowed is not None:
            self._empty_state_allowed = bool(allowed)
        if self._view.tile_count() > 0:
            return
        if self._scan_error is not None:
            self._show_scan_error_state(self._scan_error)
            return
        if not self._empty_state_allowed:
            # 主案内は別の席（プレビュー列）が持つ — ここは黙る。
            self._view.set_empty_state("")
            return
        self._view.set_empty_state(
            self._empty_state_message(),
            icon_name=self._empty_state_icon(),
            actions=_button_specs(self._current_empty_actions()),
        )

    def set_orchestrated_hint(self, text: str | None) -> None:
        """窓の空状態オーケストレータが決めた従属面の 1 行をこの席へ描く。

        :meth:`refresh_empty_state` と同じ 3 条件を同じ順で見る公開入口:
        ``None``（割当 ``NONE``）では**一切書かない** / 走査失敗カード
        （⚠ + [再試行]）は裁定より前に勝つ / タイルがある面には空状態を
        書かない。従属面の 1 行なのでアイコンもボタンも付けない。

        絞り込み 1 打鍵ごとに走る経路なので、ここに I/O や
        ``findChildren`` の再帰を置かないこと。
        """
        if text is None:
            return
        if self._scan_error is not None or self._view.tile_count() > 0:
            return
        self._view.set_empty_state(str(text), "", "")

    def _empty_state_message(self) -> str:
        """Message painted when the pane settles at zero tiles (#5).

        Consulted by ``_set_entries_as_tiles`` whenever a rebuild lands empty
        — i.e. only for *settled* results, never mid-scan (``set_folder``
        clears the view, which also drops any previous message).  The base
        returns ``""`` (no message — the right pane keeps its historical blank
        look); ``PostGrid`` overrides it to distinguish an empty folder from a
        filter that matched nothing.
        """
        return ""

    def _empty_state_actions(self) -> list[EmptyAction]:
        """空状態カードのボタン列（ラベル + 押下先を 1 つの値で — 項目 #95）.

        :meth:`_empty_state_message` と同じ分類から導く。ラベルの分岐
        (``_empty_state_action`` / ``_empty_state_secondary``) と振る舞いの分岐
        (``_on_empty_action`` / ``_on_empty_secondary``) が別々に並走していた
        頃は、片方だけ増えた分類が「押せるのに何も起きないボタン」になっていた
        ので、**1 つの列**に畳んである。基底は何も出さない。
        """
        return []

    def _current_empty_actions(self) -> list[EmptyAction]:
        """今この瞬間に描く / 押下を解決するボタン列（唯一の権威）。

        走査失敗中は自前のエラーカードが勝つので 再試行 1 個だけ
        (:meth:`_show_scan_error_state` と同じ列) — 描画と押下解決が同じ列を
        引くことで、添字だけを運ぶ :attr:`GalleryView.empty_action_clicked`
        が別の意味へずれない。
        """
        if self._scan_error is not None:
            return self._scan_error_actions()
        return self._empty_state_actions()

    def _retry_after_scan_error(self) -> None:
        """走査失敗カードの［再試行］— 同じフォルダをもう一度要求する。"""
        if self._root_or_folder is not None:
            self.set_folder(self._root_or_folder)

    def _empty_state_icon(self) -> str:
        """``common/ui/icons.py`` glyph painted above the settled-empty
        message (redesign 2026-07 Phase 3-4), or ``""`` for none.

        Consulted together with :meth:`_empty_state_message`.  The base
        offers no icon; subclasses pick one that matches their message.
        """
        return ""

    def _on_empty_action(self, index: int) -> None:
        """空状態カードの *index* 番目のボタンが押された（唯一の押下口）.

        ラベルを描いたときと**同じ列**を引き直して押下先を得る — 分類の入力は
        クリックまでの間に変わらない（変われば再構築が走り、ボタンごと差し
        替わる）ので、添字は安全に意味へ戻せる。範囲外（描画とクリックの間に
        列が縮んだ）なら黙って捨てる。
        """
        actions = self._current_empty_actions()
        if 0 <= index < len(actions):
            actions[index].callback()

    def _view_mode_did_change(self) -> None:
        self._reconfigure_view()
        self._schedule_visible_request()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        self._build_chrome(outer)

        # Ctrl+ホイールの受け手は**構築時に渡す**（``GalleryView.__init__``）。
        # 渡すのはこのペインが実際にサイズスライダを持っているときだけ —
        # ``_on_thumb_zoom_requested`` の適用点はスライダなので、持たない
        # ペイン（chrome を組まない基底の直接利用）で受け手のふりをすると
        # ジェスチャを消費して何も起きない死に手が戻る。``_build_chrome`` は
        # この直前に走っているので、ここで見た有無が最終的な有無。
        self._view = GalleryView(
            self,
            zoom_handler=(
                self._on_thumb_zoom_requested
                if getattr(self, "size_slider", None) is not None
                else None
            ),
        )
        self._view.selection_changed.connect(self._on_view_selection_changed)
        self._view.item_activated.connect(self._on_view_item_activated)
        self._view.visible_range_changed.connect(self._schedule_visible_request)
        # 収束適用 (issue #99): 保留スクロール (B-13) はタイル再構築時
        # （``_set_entries_as_tiles``）だけでなく、遅延リレイアウトの収束時にも
        # 適用を試みる。ステージ復帰（席幅 0 → 復元）はタイル再構築を**伴わず**
        # ジオメトリ反映のタイミングも環境依存なので、「幅が戻ればいつかは
        # resizeEvent → コアレサ → このフック」という収束点だけが適用を保証
        # できる（時点適用の 1 tick 再試行は低速 CI で不発だった — #83 追補の
        # 撤去理由）。
        self._view.relayout_converged.connect(self._apply_pending_scroll)
        outer.addWidget(self._view, 1)
        self._reconfigure_view()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_slider_range()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        # Re-arm after a close → show cycle: ``shutdown`` marks the pane
        # closed, and being shown again means it is live once more (the flag
        # is a "closed" state, not a one-way kill switch).
        self._shutdown = False
        self._sync_slider_range()
        self._schedule_visible_request()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Closing the pane cancels its background work (:meth:`shutdown`).

        ``shutdown`` used to be reachable only from ``ViewerWindow.closeEvent``,
        so a grid closed on its own kept every scanner / probe / per-widget
        ``QThreadPool`` running until Python happened to collect the widget.
        The C++ destructors then ran *while those worker threads were still
        live* — on Windows that surfaces as a ``0xC0000005`` access violation at
        an unrelated moment, and the pending ``deleteLater`` flush blocks in
        ``~QThreadPool``'s untimed ``waitForDone``.  The offscreen test suite hit
        both (a crashed xdist worker, then a wedged session teardown) because
        ~240 test call sites close a grid without a window around it.

        ``shutdown`` is pure cancellation and idempotent, so running it here as
        well as from the window costs nothing and can't double-free.
        """
        self.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------ layout params

    def _caption_height(self) -> int:
        if self._view_mode != "icon":
            return 0
        fm = self.fontMetrics()
        return max(36, fm.lineSpacing() * 2 + 8)

    def _caption_overlay(self) -> bool:
        """Whether icon tiles ride the caption on the image (Phase 3-2 seating).

        When True the layout reserves NO caption strip (``caption_height=0``)
        and :class:`~snappix.viewer.gallery_view.GalleryView` draws the title on
        a bottom gradient scrim with badges in the bottom-right seat.  The base
        keeps the legacy below-image strip; :class:`PostGrid` opts in.
        """
        return False

    def _build_params(self) -> LayoutParams:
        if self._view_mode == "icon":
            target = self._icon_size
            return LayoutParams(
                target_size=target,
                spacing=_GRID_SPACING,
                caption_height=0 if self._caption_overlay() else self._caption_height(),
                margin=_GRID_MARGIN,
                min_row_height=max(self._ICON_SIZE_MIN // 2, target // 2),
                max_row_height=max(target * 2, target + 240),
                justify_last_row=False,
            )
        return LayoutParams(
            target_size=self._list_icon_size,
            spacing=2,
            caption_height=0,
            margin=_GRID_MARGIN,
        )

    def _reconfigure_view(self) -> None:
        self._view.configure(
            view_mode=self._view_mode,
            thumb_layout=self._thumb_layout_mode,
            params=self._build_params(),
        )

    # ------------------------------------------------------ tile building

    def _aspect_source(self, entry: FolderEntry) -> Path | None:
        """このペインが「アスペクトを測れる画像」とみなすパス（フック）。

        規則そのものは席をまたぐ 1 実装 :func:`aspect_source`。``PostGrid``
        だけが到達不能行を計測不能へ落とすために override する。
        """
        return aspect_source(entry)

    def _default_aspect(self, entry: FolderEntry) -> float:
        return default_aspect(entry)

    def _tile_warning(self, entry: FolderEntry) -> str:
        """このエントリに付ける警告バッジの kind（``""`` = 無し）のフック。

        基底は常に無し。派生グリッドが「タイルは出るが実体に到達できない」席
        （``PostGrid`` の横断一覧ゴースト）を有標にするための唯一の入口で、
        **in-memory の判定だけ**を行うこと（タイル生成は I/O ゼロの経路）。
        """
        return ""

    def _build_tile(self, entry: FolderEntry) -> Tile:
        """このペインの席名・文言でエントリを 1 枚のタイルへ写す。

        写像そのものは席をまたぐ 1 実装 :func:`build_tile`。ここが足すのは
        ペイン固有の 3 つ（キーの名前空間・キャプション / ツールチップの
        フック・到達不能行の警告 kind）と、アスペクト計測方針の差し替え。
        """
        return build_tile(
            entry,
            key=self._key_prefix + str(entry.path),
            caption=self._caption_for(entry),
            tooltip=self._tooltip_for(entry),
            warn=self._tile_warning(entry),
            aspect_of=self._aspect_source,
        )

    @staticmethod
    def _carry_render_state(old: Tile, new: Tile) -> None:
        """Copy a decoded pixmap + settled aspect from *old* into *new*.

        Keeps a metadata enrichment / re-sort / filter rebuild from flashing
        an already-loaded thumbnail back to its placeholder — and from
        re-probing an aspect we already know.  Only fills fields *new* lacks,
        so a freshly-built tile that already carries its own pixmap / aspect
        still wins.  The loader caches by the (path-derived) tile key, so the
        carried pixmap is exactly what a re-request would have re-emitted.
        """
        if not new.thumb_loaded and old.thumb_loaded and old.pixmap is not None:
            new.pixmap = old.pixmap
            new.thumb_loaded = True
            new.pixmap_size = old.pixmap_size
        # Carry a settled decode *failure* (C03) across the rebuild too —
        # otherwise a failed tile reverts to the "読み込み中" dots on every
        # metadata / re-sort / filter rebuild and flickers back to a glyph
        # only once the loader re-fails.  A freshly-loaded thumbnail (new
        # already has thumb_loaded) supersedes the old failure and wins.
        if old.thumb_failed and not new.thumb_loaded:
            new.thumb_failed = True
            # Keep the recorded failure edge too (#114) — without it a
            # rebuild would reset the gate to 0 and the very next visible
            # pass would re-request (and re-fail) the unreadable file.
            new.thumb_failed_edge = old.thumb_failed_edge
        if not new.aspect_known and old.aspect_known:
            new.aspect = old.aspect
            new.aspect_known = True

    def _seed_folder_resolution(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Resolve warm folders from the preview cache (main-thread seed).

        For panes WITHOUT a metadata pass (the right pane), an unresolved
        folder entry is replaced in place with its cached resolution when the
        folder is warm — so its representative thumbnail AND aspect ratio land
        like an image file's, with no scandir.  Cold folders are left
        unresolved for the loader's lazy ``resolve_in_folder`` path (which
        populates the cache for next time).  Panes that run their own metadata
        pass (the left pane) skip this: the cache-backed pass already resolves
        them, and seeding here would briefly ignore the ``#thumb#`` toggle.
        """
        if self._folder_cache is None or self._WITH_METADATA:
            return entries
        specs = [
            (e.path, e.mtime)
            for e in entries
            if e.is_dir and not e.thumbnail_resolved and e.mtime
        ]
        if not specs:
            return entries
        try:
            hits = self._folder_cache.get_many(specs)
        except Exception:  # pragma: no cover (cache best-effort)
            return entries
        if not hits:
            return entries
        out: list[FolderEntry] = []
        for e in entries:
            if e.is_dir and not e.thumbnail_resolved:
                preview = hits.get(str(e.path))
                if preview is not None:
                    out.append(apply_cached_preview(e, preview))
                    continue
            out.append(e)
        return out

    def _set_entries_as_tiles(
        self, entries: list[FolderEntry], *, seed_aspect: bool = True,
    ) -> None:
        # 再構築前の選択（下の黙った温存で使う — ``set_tiles`` が選択を
        # リセットするより前に取る）。
        prev_selected = self._view.current_path()
        entries = self._seed_folder_resolution(entries)
        with measure("build_tiles", f"{len(entries)} entries"):
            tiles = [self._build_tile(e) for e in entries]
            # ``seed_aspect=False`` (the ``#thumb#`` exclude toggle) means the
            # representative image — and thus the thumbnail and aspect —
            # changed, so we start fresh (no carry-forward, no cache seed) and
            # let the loader + probe repopulate.
            if seed_aspect and entries:
                # Carry decoded pixmaps + settled aspects forward from the
                # tiles currently on screen so a re-sort / filter / metadata
                # rebuild doesn't flash every thumbnail back to a placeholder
                # (nor re-probe an aspect we already know).
                prev: dict[str, Tile] = {}
                for i in range(self._view.tile_count()):
                    t = self._view.tile_at(i)
                    if t is not None:
                        prev[t.key] = t
                if prev:
                    for tile in tiles:
                        old = prev.get(tile.key)
                        if old is not None:
                            self._carry_render_state(old, tile)
                # A tile hidden by a filter and re-shown is NOT on screen at
                # snapshot time, so the carry above misses it.  If the loader
                # still holds its decode, hand the image back through the
                # normal flush path — the loader's source-limited serve stays
                # silent on re-request (it assumes the caller already holds
                # the image), so without this re-seed such a tile would show
                # the "loading" dots forever (#10).  A key the loader has
                # since evicted simply re-decodes via the visible request.
                for tile in tiles:
                    if tile.thumb_loaded or tile.thumb_failed:
                        continue
                    img = self._loader.cached_image(tile.key)
                    if img is not None:
                        self._pending_icons[tile.key] = img
                if self._pending_icons:
                    self._icon_flush_timer.trigger()
                # Warm-seed any still-unknown aspects from the cache so a
                # revisited folder lays out justified with zero reflow.
                if self._meta_cache is not None:
                    # 未解決のタイルだけで specs を組む（レビュー 09-03 #250）。
                    # ``_carry_render_state`` が既知アスペクトを引き継ぐので、
                    # 絞り込み 1 打鍵ごとの再構築では大半が ``aspect_known``
                    # になる — 全件で組むと GUI スレッド上の IN クエリが毎回
                    # 全エントリぶん走り、直後のループでほぼ全部捨てていた。
                    # ``_seed_folder_resolution`` の「未解決分だけ」と同じ流儀。
                    pending = [
                        (tile, e)
                        for tile, e in zip(tiles, entries, strict=True)
                        if not tile.aspect_known
                    ]
                    if pending:
                        specs = [(e.path, e.mtime, e.size) for _t, e in pending]
                        try:
                            hits = self._meta_cache.get_many(specs)
                        except Exception:  # pragma: no cover (cache best-effort)
                            hits = {}
                        for tile, e in pending:
                            wh = hits.get(str(e.path))
                            if wh and wh[1] > 0:
                                tile.aspect = wh[0] / wh[1]
                                tile.aspect_known = True
            self._view.set_tiles(tiles)
        # Empty-state message (#5): a rebuild that lands with zero tiles shows
        # the subclass-provided explanation (plus an optional one-click action
        # button, C06); any non-empty result clears both.  While the last scan
        # FAILED (I01), the error state wins — a follow-up rebuild (e.g. the
        # metadata close-out re-sort) must not overwrite it with "empty".
        if tiles:
            self._view.set_empty_state("")
        else:
            self.refresh_empty_state()
        self._last_probe_keys = frozenset()
        self._probe_failed.clear()
        self._resolve_pending_select()
        if (
            self._keep_view_on_same_folder
            and self._view.current_path() is None
            and prev_selected is not None
        ):
            # ``_keep_view_on_same_folder`` の面（右ペイン）限定 — 左ペインは
            # 「再構築で選択は黙って落ち、残したい経路だけ pending に積む」
            # 契約（_preserve_selection_for_rebuild / overlay_landing テスト群）
            # を既に持っており、無条件の温存はそれを覆す。
            # 再構築前の選択がまだ一覧に居て、pending も何も選ばなかったなら
            # 黙って保つ（emit しない — ユーザーから見て選択は変わっていない
            # ので、file_selected の副作用を再駆動しない）。SWR の温存ビューが
            # 着地の ``set_tiles`` リセットで選択だけ失う穴と、pending を
            # 陳腐化破棄した着地で選択が消える穴の両方を塞ぐ。B-13 の
            # スクロール復元はこの後の ``_apply_pending_scroll`` が上書きする
            # ので位置の主導権は変わらない。
            self._view.select_key(
                self._key_prefix + str(prev_selected), emit=False,
            )
        self._apply_pending_scroll()
        self._schedule_visible_request()
        self.tiles_changed.emit()

    def _resolve_pending_select(self) -> None:
        """預けた選択を当てて降ろす（当てられなければ残す）。

        暫定の再構築（タイル 0 件・対象がまだ現れていない非同期検索の第 1
        フェーズ）では当たらないので、保留は消費されない。
        """
        self._pending_select.consume(self._apply_select_request)

    def _apply_select_request(self, req: SelectRequest) -> bool:
        """*req* を今の一覧へ当て、当たったかを返す（``Pending.consume``）。"""
        idx = self._view.index_of_key(self._key_prefix + str(req.path))
        if idx is None and req.ancestor_ok:
            # The exact item isn't on the grid (deep search hit after the
            # search was cleared, or a multi-level jump up) — select the
            # deepest ancestor that is, i.e. the shown folder containing it.
            for parent in req.path.parents:
                idx = self._view.index_of_key(self._key_prefix + str(parent))
                if idx is not None:
                    break
        if idx is None:
            return False
        # Flag the synchronous selection emit as "pending resolution" so a
        # host can tell a programmatic pending-select landing (e.g. the
        # split view's representative-image auto-pick) apart from a user
        # click / direct select_path call (see
        # ``is_resolving_pending_select``; split-view redesign 2026-07).
        self._resolving_pending = True
        try:
            self._view.select_index(idx, emit=True)
        finally:
            self._resolving_pending = False
        # Re-ensure visibility one tick later: same-tick follow-up
        # relayouts (scrollbar appearing, aspect seeds) can shift the row
        # after select_index's synchronous ensure_visible.  Skipped when a
        # remembered scroll offset is queued — B-13 restore stays the
        # source of truth for the position.
        if not self._pending_scroll.armed:
            # Receiver-context form: the callback is auto-cancelled if the
            # view is destroyed before the timer fires (window closed right
            # after navigating), instead of raising "Internal C++ object
            # already deleted" into the event loop.
            QTimer.singleShot(
                0, self._view, lambda i=idx: self._view.ensure_visible(i)
            )
        return True

    def _apply_pending_scroll(self) -> None:
        """Re-apply a deferred scroll offset (B-13) when the layout can take it.

        Two triggers: after a tiles rebuild (``_set_entries_as_tiles``) and on
        every deferred-relayout convergence (``relayout_converged`` — issue
        #99, the only reliable point for restores that don't rebuild tiles,
        e.g. the stage-return splitter expansion).  Runs one event-loop tick
        later via ``QTimer.singleShot(0)`` so the justified layout's scroll
        range (set in ``GalleryView._do_relayout``) is finalised — ``setValue``
        before the range exists is clamped to 0 and lost.  Applied AFTER
        ``_resolve_pending_select`` so it wins over that selection's
        ensure-visible scroll (the remembered offset is the source of truth
        for the restored position).  The pending value survives an async
        search re-scan and a collapsed-seat (width-0) interim layout (it isn't
        cleared until actually applied), mirroring ``_pending_select``'s
        lifetime.
        """
        value = self._pending_scroll.take_if(self._scroll_restore_ready)
        if value is None:
            return

        def _do() -> None:
            vbar = self._view.verticalScrollBar()
            vbar.setValue(max(0, min(value, vbar.maximum())))
            self._schedule_visible_request()

        # Receiver-context form so the callback is dropped (not fired into a
        # destroyed widget) when the window closes before the tick runs.
        QTimer.singleShot(0, self._view, _do)

    def _scroll_restore_ready(self, _value: int) -> bool:
        """保留スクロールを**今**当ててよいか（``take_if`` の ready 述語）。

        偽の回は値が残る——暫定のレイアウトへ当てるとクランプで復元値が失わ
        れるので、「まだ当てられない」3 つをここ 1 本に畳む。
        """
        # Keep the pending value alive until it lands on a populated view: an
        # async search restore rebuilds tiles several times (scan → ranked
        # results), and an empty interim rebuild must not consume the offset.
        # Mirrors ``_pending_select``'s "resolve only when the target
        # exists" lifetime.
        if self._view.tile_count() == 0:
            return False
        # 現在のスクロールレンジが**縮退レイアウト**（席が畳まれた幅 0 時代の
        # フォールバック幅 1、または未レイアウト — issue #99）由来の間は消費
        # しない: そのレンジへの setValue は maximum=0 への恒久クランプで値を
        # 失う。判定はウィジェットジオメトリではなくレンジの出自
        # （``GalleryView.layout_is_degenerate`` = ``last_layout_viewport_width``
        # の共有述語。可視集合を空にする判定 — issue #160 — と同一）で行う。
        # setSizes の反映遅延中は
        # ジオメトリ（まだ旧幅 > 0）とレンジ（もう幅 0 時代）が逆方向に
        # 食い違う（CI 実測）。実幅レイアウトでレンジが 0 のケース（内容が
        # ちょうど収まる）は従来どおり消費してクランプする — 正しい着地。
        if self._view.layout_is_degenerate():
            return False
        # スキャン未着地（stale-while-revalidate で**旧フォルダ**のタイルを
        # 表示中）の収束でも消費しない: 旧レイアウトの maximum へのクランプで
        # 復元値が縮む（旧フォルダが短い場合）。着地後の再構築
        # （``_set_entries_as_tiles`` → 明示適用）が正しい適用点。
        if self._awaiting_scan:
            return False
        return True

    def scroll_value(self) -> int:
        """Current vertical scroll offset (B-13 nav-history capture)."""
        return self._view.verticalScrollBar().value()

    def set_pending_scroll(self, value: int) -> None:
        """Queue *value* as the scroll offset to restore on the next relayout.

        Used by the nav-history restore so 戻る/進む returns to the exact scroll
        position, not just the selection.  Applied once tiles are built and laid
        out (see ``_apply_pending_scroll``); a value of 0 is a no-op-equivalent
        (the default position) but still queued for symmetry.
        """
        self._pending_scroll.set(int(value))

    # ----------------------------------------------------- public API

    def set_folder(
        self, folder: Path | None, *, pending_select: Path | None = None,
    ) -> None:
        # Same-folder re-navigation keeps the current tiles on screen while
        # the rescan runs (stale-while-revalidate) when the subclass opts in
        # via ``_keep_view_on_same_folder``.  Rationale (CI faulthandler +
        # flake capture 2026-08-30): every left-grid rebuild (metadata
        # enrichment, the #thumb# toggle, keep_mode reloads) re-fires
        # ``folder_selected`` for the folder already shown, and the window
        # re-navigates the right pane — with an unconditional clear the pane
        # blanks and rescans on each re-fire, flashing empty in the UI and
        # racing every reader of its tiles.  The rescan itself still runs
        # unconditionally (freshness is unchanged); only the blank interval
        # is removed — the landing ``_apply_entries``/``set_tiles`` replaces
        # the stale view wholesale.  The left pane deliberately does NOT opt
        # in: its keep_mode machinery owns re-root blanking/restore, and
        # changing that is a separate design decision.
        keep_view = (
            self._keep_view_on_same_folder
            and folder is not None
            and self._root_or_folder == folder
        )
        self._root_or_folder = folder
        self._pending_icons.clear()
        # Drop the loader's in-memory cache.  Navigating rebuilds the view
        # with fresh tiles that hold no pixmap, but the loader keys by path
        # and its size-aware serve path stays SILENT for a *source-limited*
        # entry (an image smaller than its box) on the assumption the caller
        # still holds the decoded image.  Without this, revisiting a folder
        # leaves such thumbnails stuck as placeholders until the size slider
        # moves (which clears the cache).  The disk cache still backs the
        # re-decode, so revisits stay fast.  (``PostGrid.set_root`` already
        # clears for the left pane; this covers the right pane / any subclass.)
        self._loader.clear_cache()
        self._last_probe_keys = frozenset()
        self._probe_failed.clear()
        self._probe.cancel()
        if not keep_view:
            self._view.clear()
        self.queue_pending_select(pending_select)
        # A queued B-13 offset dies with the navigation it belonged to (an
        # unconsumed leftover — e.g. a restore that landed on an empty folder —
        # must not scroll an unrelated folder).  Legitimate restores survive:
        # main_window always calls ``set_pending_scroll`` AFTER set_root.
        self._pending_scroll.clear()
        self._scan_error = None
        self._scan_unclassified = 0
        self._awaiting_scan = False
        self._loading_hint_timer.rearm()
        if folder is None:
            self._scanner.cancel()
            # Invalidate the generation too: cancel() is cooperative, so a
            # worker that already passed its cancel check can still emit
            # ``finished`` for the OLD generation — without this it would
            # match and re-populate the just-cleared pane.
            self._pending_scan_generation = -1
            return
        self._pending_scan_generation = self._scanner.request(folder)
        # Cold-NAS feedback (C02): if nothing lands within the hint delay the
        # pane shows a centred "読み込んでいます…" instead of a blank viewport.
        self._awaiting_scan = True
        self._loading_hint_timer.trigger()

    def clear(self) -> None:
        self.set_folder(None)

    def current_path(self) -> Path | None:
        return self._view.current_path()

    def current_icon(self):
        return self._view.current_pixmap_as_icon()

    def pixmap_for_path(self, path: Path) -> QPixmap | None:
        """Return the decoded thumbnail pixmap for *path*, if one is resident.

        Lets the central image preview reuse this pane's already-loaded
        thumbnail as an instant placeholder while the full-resolution
        decode is in flight.  Returns ``None`` when *path* isn't a tile
        here or its thumbnail hasn't been decoded yet.
        """
        return self._view.pixmap_for_key(self._key_prefix + str(path))

    def tile_paths(self) -> list[Path]:
        """Displayable tiles currently shown (folders + files), in display order.

        Reads the already-built tiles only (no filesystem I/O), so it reflects
        the pane's live sort / filter.  Used by the stage-mode filmstrip
        (layout redesign 2026-07 Phase 2-1) as the sibling-post strip content —
        the same item set the browse grid shows, one row, same order.

        内部ファイル（``post.md`` / ``#thumb#…``）は除く (UIレビュー 07-25 #52 —
        判定は :func:`folder_scan.is_meta_or_marker_file` の 1 か所)。これが
        ‹ › / ←→ の画像送りが**歩く**母集合。**見せる**母集合はこれとは別で、
        ヘッダーの ``n/m`` も最大化中の画像トラックもメディア（画像 + 動画）
        だけを数え・並べる（表示層の分離 — UIレビュー 2026-08-28 N-22② /
        2026-09-11 N-109。``main_window._stage_media_paths`` が唯一の絞り込み）。

        閲覧モードのプレイリスト（:func:`lightbox_parts.scan.list_playlist_sorted`）との
        関係は **包含であって一致ではない** (#137 / UIレビュー07-25 追修):
        あちらは ``PLAYLIST_SUFFIXES``（画像 + 動画）だけを採るのに対し、ここは
        メタ / マーカー以外は種別を問わず残す — フォルダタイルや ``.pdf`` /
        ``.zip`` / ``.txt`` のタイルはここにあってプレイリストには無い。
        成り立つのは ``playlist ⊆ tile_paths`` の一方向で、これにより閲覧モードを
        閉じたときの着地ファイルは必ず右ペインのタイルとして存在し、選択同期が
        黙って失敗しない。右の情報パネルの一覧そのものは ``post.md`` を淡色 +
        末尾で温存する（メタへの導線）。
        """
        out: list[Path] = []
        for i in range(self._view.tile_count()):
            tile = self._view.tile_at(i)
            if tile is None:
                continue
            if not tile.is_dir and is_meta_or_marker_file(tile.path):
                continue
            out.append(tile.path)
        return out

    def folder_paths(self) -> list[Path]:
        """Folder tiles currently shown, in display order (no filesystem I/O).

        Reads the already-built tiles only, so it reflects the pane's live
        sort / filter.  Used by the fullscreen lightbox (閲覧モード) as the
        "next / previous post" candidate list for cross-post navigation.
        """
        out: list[Path] = []
        for i in range(self._view.tile_count()):
            tile = self._view.tile_at(i)
            if tile is not None and tile.is_dir:
                out.append(tile.path)
        return out

    def media_tile_paths(self) -> list[Path]:
        """Image / video file tiles currently shown, in display order (no I/O).

        Reads the already-built tiles only, so it reflects the pane's live
        sort / filter — used by the fullscreen lightbox (閲覧モード) as the
        playlist when a search / filter surfaces individual media files in the
        left pane (G07: browse the filtered results as a flat playlist).
        Returns ``[]`` while the pane shows folders (normal browsing), so the
        caller naturally falls back to the containing folder's own media list.
        """
        out: list[Path] = []
        for i in range(self._view.tile_count()):
            tile = self._view.tile_at(i)
            if tile is None or tile.is_dir:
                continue
            if tile.path.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
                out.append(tile.path)
        return out

    def select_path(self, path: Path) -> bool:
        return self._view.select_key(self._key_prefix + str(path), emit=True)

    def set_pending_select(
        self, path: Path | None, *, ancestor_fallback: bool = False,
    ) -> None:
        """Select *path* now if its tile exists, else once the scan lands.

        Late-arriving callers (e.g. an async preview probe that finishes
        after ``set_folder``) use this instead of re-scanning just to update
        the pending selection.  ``ancestor_fallback=True`` additionally lets
        the resolve fall back to the deepest shown ancestor when the exact
        item never (re)appears — used when a search is cleared or on a
        multi-level jump up, where the old item sits below the new root.
        """
        self.queue_pending_select(path, ancestor_fallback=ancestor_fallback)
        self._resolve_pending_select()

    def queue_pending_select(
        self, path: Path | None, *, ancestor_fallback: bool = False,
    ) -> None:
        """*path* を「再構築を跨いで当てる選択」として預ける（今は解決しない）。

        再構築の**直前**に選択を預ける口（並び替え・``#thumb#`` トグル・
        キャプション切替・検索結果の 2 段着地）はこちらを使う:
        :meth:`set_pending_select` は積んだ直後に解決するので、これから捨てる
        現在のタイル列に当たって保留が空振りで消える。``path=None`` は
        「預けない」（保留があれば捨てる）。
        """
        self._pending_select.set(
            SelectRequest(path, ancestor_fallback) if path is not None else None
        )

    def reselect_same_folder(
        self, folder: Path, *, pending_select: Path | None = None,
    ) -> bool:
        """同一フォルダの純 UI 再発火向け: 再スキャンせず選択適用だけ行う。

        左グリッドはリビルド（メタデータ 2 段リビルド・``#thumb#`` トグル・
        検索 teardown 復元等）のたびに同一フォルダで ``folder_selected`` を
        再発火し、従来は窓が :meth:`set_folder` を呼び直すため
        ``_scanner.request``（フォルダ 1 つぶんの scandir + stat — NAS で
        顕著）がリビルドごとに発行されていた（issue #146 残課題 1）。内容が
        変わっていない再発火では選択の入れ直しだけで十分なので、このメソッドが
        その軽量経路を担う。

        表示中フォルダと一致し、直前のスキャンが失敗していない（= 表示中の
        一覧が信頼できる）ときだけ ``True`` を返して選択適用のみ行う。
        不一致・スキャン失敗（再試行が必要）なら何もせず ``False`` — 呼び出し
        元は従来どおり :meth:`set_folder`（再スキャン）へフォールバックする。
        *pending_select* が ``None`` のときは選択も触らない（進行中の
        pending-select を潰さない）。
        """
        if folder != self._root_or_folder or self._scan_error is not None:
            return False
        if pending_select is not None:
            # 対象が既に選択中なら ``select_index`` は「変化なし」として emit
            # しない — それで正しい。選択が変わらない再発火で右ペイン側の
            # 状態を戻す責務は窓（``_on_folder_selected`` の
            # ``selection_unchanged``）が「そもそも壊さない」形で負う
            # （issue #153: ここで emit を強制すると代表画像の自動選択が
            # 明示選択と区別できずメタカードがファイルカードに化ける）。
            self.set_pending_select(pending_select)
        return True

    def is_resolving_pending_select(self) -> bool:
        """True while a queued pending-select is applying its selection.

        The selection-changed signal fires synchronously inside the resolve,
        so a host slot can call this to tell "the scan/probe landed the
        programmatic pick" apart from a user click / direct ``select_path``
        (split-view redesign 2026-07: the representative-image auto-pick must
        not replace the folder's meta card with a file-detail card).
        """
        return self._resolving_pending

    def select_first(self) -> None:
        self._view.select_first(emit=True)

    def select_last(self) -> None:
        """末尾の項目を選択する（最大化プレビューの End — UIレビュー 07-25 #22）。"""
        self._view.select_last(emit=True)

    def focus_grid(self) -> None:
        """Give keyboard focus to the hosted GalleryView (mode switches)."""
        self._view.setFocus()

    def step_selection(self, delta: int) -> bool:
        return self._view.step_selection(delta)

    def current_view_mode(self) -> str:
        return self._view_mode

    def current_icon_size(self) -> int:
        return self._icon_size

    def current_list_icon_size(self) -> int:
        return self._list_icon_size

    def current_thumb_layout(self) -> str:
        return self._thumb_layout_mode

    def set_curation_provider(self, provider) -> None:
        """Forward a ``path -> (star, later)`` curation lookup to the view.

        Lets a pane render the user-star / watch-later overlays for tiles it
        shows (right pane reuses the left pane's in-memory map so both agree).
        ``None`` disables the overlays.  Pure synchronous lookup — safe on the
        paint path.
        """
        self._view.set_curation_provider(provider)

    def set_tooltip_extra_provider(self, provider) -> None:
        """Forward a ``path -> list[str]`` hover-tooltip supplier to the view.

        The pane's own tile tooltips are baked at build time (:meth:`_tooltip_for`);
        this adds lines resolved when the tooltip is actually shown, for facts
        that change while the tile is on screen — ★ / 「あとで見る」 / ユーザータグ
        (UIレビュー 07-25 #136 / #13).
        """
        self._view.set_tooltip_extra_provider(provider)

    def refresh_curation(self) -> None:
        """Repaint so a just-changed curation badge shows (no rebuild)."""
        self._view.viewport().update()

    # --------------------------------------------------------- scan handler

    def _on_scan_finished(self, generation: int, entries: list) -> None:
        if generation != self._pending_scan_generation:
            return
        self._scan_error = None
        self._awaiting_scan = False
        self._loading_hint_timer.rearm()
        with measure("scan_finished_apply", f"{len(entries)} entries"):
            self._on_scan_finished_payload(generation, entries)

    def _on_scan_partial(self, generation: int, unclassified: int) -> None:
        """走査に穴が空いた（``finished`` の直前に届く）。

        一覧そのものは有効なので結果は捨てない — 「読めなかった」を「無かった」
        とも「全部読めなかった」とも言わない規律（最近追加一覧の
        ``RecentFilesScan.unreadable_dirs`` / 再帰検索の ``walk_incomplete``
        と同じ）。件数を覚えるだけで、告げ方はペインが決める
        (:meth:`_on_scan_partial_payload`)。
        """
        if generation != self._pending_scan_generation:
            return
        self._scan_unclassified = int(unclassified)
        self._on_scan_partial_payload(self._scan_unclassified)

    def _on_scan_partial_payload(self, unclassified: int) -> None:
        """Hook: 穴の告知（既定は何もしない — ペインごとの導線に任せる）。"""
        del unclassified

    def _on_scan_failed(self, generation: int, message: str) -> None:
        """The shallow scan blew up (I01) — surface an error state, not "empty"."""
        if generation != self._pending_scan_generation:
            return
        self._scan_error = message
        self._awaiting_scan = False
        self._loading_hint_timer.rearm()
        if self._view.tile_count():
            # 同じフォルダへの再ナビゲーションでは古い面を温存する
            # (``_keep_view_on_same_folder``) が、空状態カードはタイルが 0 の
            # ときだけ描かれる契約なので、温存したままだと走査失敗が 1 ピクセル
            # も伝わらない（共有が落ちても正常な一覧に見える）。落としてから出す。
            self._drop_stale_view()
        self._on_scan_failed_payload(message)

    def _drop_stale_view(self) -> None:
        """走査失敗時に温存していた古い面を捨てる.

        サブクラスは付随する状態（エントリ表・見出しの件数）も一緒に中立へ
        戻す — 面だけ消して件数が前の値のまま残ると、もっと読みにくい。
        """
        self._view.clear()

    def _show_loading_hint(self) -> None:
        """Delayed mid-scan hint (C02): only if nothing has landed yet."""
        if self._awaiting_scan and self._view.tile_count() == 0:
            self._view.set_empty_state(t("viewer.common.loading_folder"))

    # ----------------------------------------------- selection / activation

    def _on_view_selection_changed(self, index: int) -> None:
        tile = self._view.tile_at(index)
        if tile is None:
            return
        if not self._resolving_pending and self._pending_select.armed:
            # ユーザー（クリック・矢印送り・直接の select_path）が選んだ時点で、
            # 飛行中の再スキャンに積んであった復元/自動選択の pending は
            # 「選ぶ前の状態を戻す」という意図ごと陳腐化する。残すと着地時の
            # ``_resolve_pending_select`` が後からユーザー選択（と、それに
            # 連なる詳細カード/プレビュー）を奪い返す — SWR の温存ビューが
            # 「飛行中も選べる」窓を広げて CI 実写になった (2026-08-31、
            # PR #148 viewer-b)。プローブの自動選択がクリックを上書きする
            # 同族もこの弁で塞がる。resolve 自身の選択適用は
            # ``_resolving_pending`` で除外（自分の pending を消させない）。
            self._pending_select.clear()
        if tile.is_dir:
            self.folder_selected.emit(tile.path)
        else:
            self.file_selected.emit(tile.path)

    def _on_view_item_activated(self, index: int) -> None:
        tile = self._view.tile_at(index)
        if tile is None:
            return
        if tile.is_dir:
            self.folder_activated.emit(tile.path)
        else:
            self.file_activated.emit(tile.path)

    # ----------------------------------------------------- viewport thumbs

    def _schedule_visible_request(self) -> None:
        if self._shutdown:
            return
        self._visible_request_timer.trigger()

    def _request_visible_thumbnails(self) -> None:
        if self._shutdown:
            return
        buffered = self._view.visible_indices(buffer_rows=1)
        if buffered is None:
            # 席が畳まれている（プレビュー最大化でグリッド幅 0）。新規要求を
            # 止めるだけでは、畳む直前に積んだ保留要求が見えない面のために
            # NAS 往復とデコード枠を使い切る — 最大化はプレビューへ集中する
            # 操作なので、その帯域はプレビューへ返す。席が戻れば resizeEvent →
            # コアレサ → 通常経路が全部を要求し直すので取りこぼさない。
            self._loader.discard_pending_outside(set())
            self._loader.mark_visible(set())
            return
        start, end = buffered
        strict = self._view.visible_indices_strict()
        dpr = self.devicePixelRatioF() or 1.0
        target = max(1, self._current_size())
        probe_specs: list[tuple] = []
        buffered_keys: set[str] = set()
        visible_keys: set[str] = set()
        with measure("visible_request", f"{end - start + 1} items"):
            for i in range(start, end + 1):
                tile = self._view.tile_at(i)
                if tile is None:
                    continue
                key = tile.key
                buffered_keys.add(key)
                if strict is not None and strict[0] <= i <= strict[1]:
                    visible_keys.add(key)
                entry: FolderEntry = tile.entry
                if not tile.aspect_known and str(entry.path) not in self._probe_failed:
                    src = self._aspect_source(entry)
                    if src is not None:
                        probe_specs.append((entry.path, entry.mtime, entry.size, src))
                box = self._view.box_size(i)
                if box is not None and box.width() > 0 and box.height() > 0:
                    size = box
                else:
                    size = QSize(target, target)
                # Request the thumbnail when it hasn't loaded yet, OR when the
                # box has since grown past the resolution we actually have
                # (aspect settling / justify row-height growth would otherwise
                # leave an upscaled, blurry thumb).  The loader serves a
                # source-limited key from cache without re-decoding, so a box
                # bigger than the original never churns.
                #
                # A tile whose decode has *settled as failed* (C03) is only
                # re-requested once the box grows PAST the edge the failure
                # was recorded at (``thumb_failed_edge``, #114).  At the same
                # size the loader would just re-fail on every scroll /
                # relayout, hammering an unreadable file forever — but a
                # blanket "never again" gate also froze an already-loaded
                # tile at its stale low-res pixmap when ONE upgrade decode
                # failed transiently (offline NAS blip).  The unconditional
                # retry path remains the explicit cache-clearing refresh
                # (_refresh_thumbnails / reset_thumbnails), which drops the
                # failed flag so the tile gets one fresh attempt.
                need = self._physical_size(size, dpr)
                wants = (
                    not tile.thumb_loaded
                    or self._would_upscale(tile.pixmap_size, need)
                )
                need_edge = max(need.width(), need.height())
                if wants and (
                    not tile.thumb_failed or need_edge > tile.thumb_failed_edge
                ):
                    self._request_thumb(key, entry, size, dpr)
            self._loader.discard_pending_outside(buffered_keys)
            self._loader.mark_visible(visible_keys)
        if probe_specs:
            keys_set = frozenset(str(s[0]) for s in probe_specs)
            if keys_set != self._last_probe_keys:
                self._last_probe_keys = keys_set
                self._probe.request(probe_specs)

    @staticmethod
    def _physical_size(size: QSize, dpr: float) -> QSize:
        """*size* (a laid-out box) in physical pixels at device ratio *dpr*.

        Both axes, deliberately: the decode fits the image inside the box, so
        the SHORT side can be the binding constraint.  A list row's box is
        ~526×24, where a longest-edge ruler would claim 526 px of detail were
        requested when the decode is pinned to 24 (#F1D-1).
        """
        from math import ceil
        ratio = max(1.0, dpr)
        return QSize(
            ceil(size.width() * ratio), ceil(size.height() * ratio),
        )

    @staticmethod
    def _would_upscale(have: QSize, box: QSize) -> bool:
        """True when *have* pixels drawn into *box* stretch past the margin.

        The painter blits the pixmap into the box with ``KeepAspectRatio``, so
        asking the loader for more pixels only helps when the drawn size
        (:func:`~.thumbnail_loader.fitted_edge`, the same ruler the loader
        answers with) lands meaningfully bigger than what we hold.  Comparing
        the box's longest edge against the pixmap's would both miss upgrades
        (an anisotropic box that grew on its constrained axis) and demand
        impossible ones (a list row whose width grows while its ~24 px height
        caps every decode) — #F1D-1.
        """
        hw, hh = have.width(), have.height()
        if hw <= 0 or hh <= 0:
            return True
        return fitted_edge(have, box) > max(hw, hh) + _EDGE_UPGRADE_MARGIN

    def _request_thumb(
        self, key: str, entry: FolderEntry, size: QSize, dpr: float,
    ) -> None:
        if entry.is_dir and not entry.thumbnail_resolved:
            self._loader.request(
                key, entry.path, size, resolve_in_folder=True, dpr=dpr,
                folder_mtime=entry.mtime,
            )
        elif entry.thumbnail_path is not None:
            self._loader.request(key, entry.thumbnail_path, size, dpr=dpr)

    # ------------------------------------------------------- thumb arrival

    def _on_thumb_loaded(self, key: str, image) -> None:
        if self._key_prefix and not key.startswith(self._key_prefix):
            return
        if self._view.index_of_key(key) is None:
            return
        self._pending_icons[key] = image
        self._icon_flush_timer.trigger()

    def _on_thumb_failed(self, key: str) -> None:
        # Settle the tile's placeholder from "loading" dots to the static
        # file-type glyph (C03) — a broken image shouldn't look pending forever.
        if self._key_prefix and not key.startswith(self._key_prefix):
            return
        self._view.mark_thumb_failed(key)

    def _flush_icon_updates(self) -> None:
        if self._shutdown or not self._pending_icons:
            return
        rng = self._view.visible_indices_strict()
        limit = _adaptive_flush_limit(rng[1] - rng[0] + 1 if rng else 0)
        with measure("icon_flush", f"{len(self._pending_icons)} icons"):
            processed = 0
            for key in list(self._pending_icons.keys()):
                if processed >= limit:
                    break
                image = self._pending_icons.pop(key)
                idx = self._view.index_of_key(key)
                if idx is None:
                    continue
                processed += 1
                pixmap = QPixmap.fromImage(image)
                self._view.set_thumb(key, pixmap)
                # Fallback aspect: if the probe hasn't landed yet, establish
                # the tile's aspect from the decoded thumbnail so justified
                # layout settles even without the cache / probe.
                tile = self._view.tile_at(idx)
                if tile is not None and not tile.aspect_known:
                    iw = image.width()
                    ih = image.height()
                    if iw > 0 and ih > 0:
                        self._view.set_aspect(key, iw, ih)
        if self._pending_icons:
            self._icon_flush_timer.trigger()
        # A thumbnail can land smaller than its box if the box grew (aspect
        # settled) while the decode was already in flight.  Re-evaluate the
        # visible range so such tiles get upgraded to a crisp size; this
        # converges (each upgrade raises pixmap_size, and the loader serves
        # source-limited keys from cache without re-decoding).
        self._schedule_visible_request()

    # ------------------------------------------------------- aspect probe

    def _on_aspect_probed(self, generation: int, rows: list) -> None:
        if generation != self._probe.latest_generation():
            return
        if self._meta_cache is not None:
            # One transaction per probe chunk instead of a commit per row —
            # this runs on the GUI thread, so per-row commits add up fast.
            # (put_many skips w<=0 failure rows, so no bad aspect is stored.)
            try:
                self._meta_cache.put_many(list(rows))
            except Exception:  # pragma: no cover (cache best-effort)
                pass
        for path_str, _mtime, _size, w, h in rows:
            if w <= 0 or h <= 0:
                # Unreadable / unidentified: remember it so we don't re-probe
                # this file on the next scroll pass (set_aspect ignores it, so
                # aspect_known stays False and it would otherwise re-queue).
                self._probe_failed.add(path_str)
                continue
            self._view.set_aspect(self._key_prefix + path_str, w, h)

    # ------------------------------------------------------ slider / size

    def _current_size(self) -> int:
        return self._icon_size if self._view_mode == "icon" else self._list_icon_size

    def _set_current_size(self, value: int) -> None:
        if self._view_mode == "icon":
            self._icon_size = value
        else:
            self._list_icon_size = value

    def _current_size_min(self) -> int:
        return (
            self._ICON_SIZE_MIN if self._view_mode == "icon"
            else self._LIST_ICON_SIZE_MIN
        )

    def _pane_max_icon_size(self) -> int:
        vp_w = self._view.viewport().width() if hasattr(self, "_view") else 0
        return max(
            self._ICON_SIZE_MIN, vp_w - 2 * _GRID_MARGIN - self._CAPTION_PAD
        )

    def _current_size_max(self) -> int:
        if self._view_mode != "icon":
            return self._icon_size_max
        pane_max = self._pane_max_icon_size()
        if self._thumb_layout_mode != "justified":
            # Square layout: the slider is a cell edge, so the pane width
            # (one big column) is the natural maximum.
            return pane_max
        # Justified layout: the slider sets the target ROW HEIGHT, not a cell
        # edge.  A target near the pane width makes even a single tile fill
        # the whole row (rows as tall as the pane is wide) — which the user
        # reaches at only ~50 % of a [min, pane_width] slider, so the top
        # half of the travel does almost nothing.  Cap the target so the
        # whole range stays useful and the maximum still fits roughly two
        # average-aspect tiles per row (use square layout for a single big
        # column).  Geometry constants are shared with ``_build_params``.
        vp_w = self._view.viewport().width() if hasattr(self, "_view") else 0
        content_w = max(1, vp_w - 2 * _GRID_MARGIN)
        two_per_row = max(
            self._ICON_SIZE_MIN, (content_w - _GRID_SPACING) // 2
        )
        return min(pane_max, two_per_row)

    def _sync_slider_range(self) -> None:
        slider = getattr(self, "size_slider", None)
        if slider is None:
            return
        if self._view_mode == "icon" and self._view.viewport().width() <= 0:
            return
        new_min = self._current_size_min()
        new_max = max(new_min, self._current_size_max())
        # Clamp from the user's DESIRED size, not the current (possibly already
        # clamped) one, so the size restores when a narrow pane widens again.
        desired = self._desired_icon_size if self._view_mode == "icon" else self._current_size()
        clamped = max(new_min, min(new_max, desired))
        if clamped != self._current_size():
            self._set_current_size(clamped)
            self._reconfigure_view()
        slider.blockSignals(True)
        try:
            slider.setRange(new_min, new_max)
            slider.setValue(clamped)
        finally:
            slider.blockSignals(False)
        self._sync_slider_tooltip(new_min, new_max)

    def _sync_slider_tooltip(self, low: int, high: int) -> None:
        """スライダが上限で止まる理由を、その場（ツールチップ）に出す。

        上限の出どころは表示形式で変わる（リストは設定値 ``_icon_size_max``、
        サムネイル表示はペイン幅由来）ので、**設定への案内はリストのときだけ**
        出す — サムネイル表示で「設定で変えられます」と書くと誤誘導になる
        (UIレビュー 2026-09-11 N-134 / N-62 と整合)。
        """
        slider = getattr(self, "size_slider", None)
        if slider is None:
            return
        key = (
            "viewer.common.thumb_size_tooltip_list"
            if self._view_mode == "list"
            else "viewer.common.thumb_size_tooltip"
        )
        slider.setToolTip(t(key, min=low, max=high))

    def _on_icon_size_changed(self, value: int) -> None:
        value = max(self._current_size_min(), min(self._current_size_max(), int(value)))
        # Record the explicit user intent (icon view) so range re-syncs clamp
        # from it rather than from an earlier transient clamp.
        if self._view_mode == "icon":
            self._desired_icon_size = value
        if value == self._current_size():
            return
        self._set_current_size(value)
        self._reconfigure_view()
        # Re-decode at the new box sizes once the slider settles.
        self._resize_refresh_timer.trigger()

    def _on_thumb_zoom_requested(self, steps: int) -> None:
        """Ctrl+ホイール → サムネサイズを ±1 段（``GalleryView`` の zoom_handler）.

        適用点はスライダのまま — ``setValue`` が ``valueChanged`` →
        :meth:`_on_icon_size_changed` を通すので、**サイズの権威が 1 つ**に
        保たれる（範囲クランプ・再デコードのデバウンスもそのまま効く）。
        ``GalleryView`` は左右両ペインの内部ビューなので、この 1 実装で
        左グリッドと右ファイル一覧の両方に効く。
        """
        slider = getattr(self, "size_slider", None)
        if slider is None or not steps:
            return
        step = slider.singleStep() or 1
        slider.setValue(slider.value() + int(steps) * step)

    def _on_slider_pressed(self) -> None:
        pass

    def _on_slider_released(self) -> None:
        # A plain click on the handle fires sliderReleased without moving the
        # value — skip the (expensive) cache-clear + full re-decode unless the
        # size actually differs from what the thumbnails were rendered at.
        if self._current_size() != self._thumb_render_size:
            self._refresh_thumbnails()

    def _on_resize_refresh(self) -> None:
        if abs(self._current_size() - self._thumb_render_size) >= 8:
            self._refresh_thumbnails()

    def _refresh_thumbnails(self) -> None:
        self._thumb_render_size = self._current_size()
        self._loader.clear_cache()
        self._pending_icons.clear()
        self._view.reset_thumbnails()
        self._schedule_visible_request()

    # ----------------------------------------------------------- view mode

    def _set_view_mode(self, mode: str) -> None:
        if mode not in ("icon", "list") or mode == self._view_mode:
            return
        self._view_mode = mode
        slider = getattr(self, "size_slider", None)
        if slider is not None:
            self._sync_slider_range()
        self._view_mode_did_change()
        self._sync_view_mode_combo()

    # -------------------------------------------- shared 表示設定 3 行 (N-75)

    def _build_view_settings_rows(
        self,
        layout: QVBoxLayout,
        *,
        sort_choices,
        slider_width: int,
    ) -> None:
        """並び順 → 表示形式 → サムネイルサイズ の 3 行を組む（左右共通）。

        同じ 3 設定が、行の並び・ラベルの有無・選択肢の順序・操作方法まで
        左右で違っていた（左で身につけた筋肉記憶を右が裏切る）ため、両ペインの
        ポップオーバーはこの 1 ファクトリから生成する — UIレビュー 2026-08-28
        N-75。行の並びと選択肢順は左（従来）に揃え、右の「並び順」は
        ``QToolButton`` + 入れ子メニューをやめて左と同じインラインコンボにする
        （元の設計コメントは「右は永続的なソートコンボを持たない葉ビュー」を
        理由に挙げていたが、両者ともポップオーバーの中に入った時点で
        "permanent" の前提自体が消えている）。

        並び順コンボは**項目を入れるところまで**で、現在値の選択とシグナル接続
        は呼び出し側が行う（左はランク表示中の差し替え、右は単純な永続化と、
        後段の都合が違うため）。
        """
        labels: list[QLabel] = []
        self.sort_combo = QComboBox()
        for key, label_key in sort_choices:
            self.sort_combo.addItem(t(label_key), key)
        row, label = _labeled_row("viewer.file_list.sort_menu", self.sort_combo)
        layout.addLayout(row)
        labels.append(label)

        # Single 表示形式 combo (C05): the old thumbnail/list pair + the buried
        # ぴったり配置 toggle unified into one three-way choice.
        self.view_mode_combo = QComboBox()
        for key, label_key in VIEW_LAYOUT_CHOICES:
            self.view_mode_combo.addItem(t(label_key), key)
        self.view_mode_combo.setToolTip(t("viewer.common.view_layout_tooltip"))
        idx = self.view_mode_combo.findData(self._view_layout_key())
        self.view_mode_combo.setCurrentIndex(max(0, idx))
        self.view_mode_combo.currentIndexChanged.connect(self._on_view_mode_changed)
        row, label = _labeled_row(
            "viewer.toolbar.layout_label", self.view_mode_combo
        )
        layout.addLayout(row)
        labels.append(label)

        self.size_slider = QSlider(Qt.Horizontal)
        # 縦の当たり判定を 24px へ広げる QSS を引き当てるための名前
        # (UIレビュー 2026-08-28 N-126 — 規則は common/ui/qss.py 側)。
        self.size_slider.setObjectName("thumbSizeSlider")
        # ツールチップ（範囲 + Ctrl+ホイールの案内 — UIレビュー 2026-09-11
        # N-66 / N-134）は _sync_slider_tooltip が表示形式ごとに出す。
        # Range + value finalise in _apply_view_mode (per-mode bounds), but
        # set sensible initial values so the handle isn't at zero before
        # the first paint.
        self.size_slider.setRange(self._current_size_min(), self._icon_size_max)
        self.size_slider.setSingleStep(8)
        self.size_slider.setPageStep(32)
        self.size_slider.setValue(self._current_size())
        self.size_slider.setFixedWidth(slider_width)
        self.size_slider.valueChanged.connect(self._on_icon_size_changed)
        self.size_slider.sliderPressed.connect(self._on_slider_pressed)
        self.size_slider.sliderReleased.connect(self._on_slider_released)
        enable_click_jump(self.size_slider)
        # ``_sync_slider_range`` はビューポート幅が確定するまで早期 return する
        # ので、初期レンジぶんのツールチップはここで 1 回張っておく (N-134)。
        self._sync_slider_tooltip(self.size_slider.minimum(), self.size_slider.maximum())
        row, label = _labeled_row("common.label.thumb", self.size_slider)
        layout.addLayout(row)
        labels.append(label)
        # 3 行のラベル列を共有幅 + 右揃えに（両ペインのポップオーバーが同じ
        # ファクトリを通るので、揃え直しも 1 か所で済む — N-39）。
        align_form_labels(*labels)

        # UIレビュー 2026-09-11 N-135: フィルターポップオーバーには
        # 「※ ここの条件は保存されません」があるのに、**永続する**この 3 設定に
        # 対の注記が無かった。ファクトリが左右共通なので 1 行で 3 席に出る。
        persist_hint = QLabel(t("viewer.common.view_settings_persistent_hint"))
        persist_hint.setWordWrap(True)
        persist_hint.setStyleSheet(hint_style())
        # 1 行に伸びると注記だけでポップオーバーの幅が決まってしまう（右ペインは
        # スライダ 100px なので特に目立つ）。既に組んだ 3 行の自然幅を上限に
        # して折り返させ、横幅の決定権を注記に渡さない。
        persist_hint.setMaximumWidth(max(200, layout.sizeHint().width()))
        layout.addWidget(persist_hint)

    def _on_view_mode_changed(self) -> None:
        """表示形式コンボ → view_mode / thumb_layout（両ペイン共通）。"""
        key = self.view_mode_combo.currentData() or self._view_layout_key()
        self._apply_view_layout_key(key)

    # ------------------------------------------------- unified layout key

    def _view_layout_key(self) -> str:
        """Current combined view/layout key: ``justified`` / ``square`` / ``list``.

        The header's single 表示 combo (C05) exposes the two persisted fields
        (``view_mode`` + ``thumb_layout``) as one three-way choice; this maps
        the pair back to the combo key.
        """
        return "list" if self._view_mode == "list" else self._thumb_layout_mode

    def _apply_view_layout_key(self, key: str) -> None:
        """Apply a 表示 combo key (C05): list, or icon + square/justified."""
        if key == "list":
            self._set_view_mode("list")
            return
        if key in ("square", "justified"):
            self.set_thumb_layout(key)
            self._set_view_mode("icon")

    def _sync_view_mode_combo(self) -> None:
        """Reflect the current view/layout pair in the header combo, if any."""
        combo = getattr(self, "view_mode_combo", None)
        if combo is None:
            return
        idx = combo.findData(self._view_layout_key())
        if idx >= 0 and idx != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    # ----------------------------------------------------- thumb layout

    def set_thumb_layout(self, mode: str) -> None:
        if mode not in ("square", "justified") or mode == self._thumb_layout_mode:
            return
        self._thumb_layout_mode = mode
        # The two layouts give the size slider different meaning (cell edge
        # vs target row height), so re-clamp its range to the new mode before
        # re-laying out — otherwise a value valid for square (≈ pane width)
        # would blow justified rows up to a single full-width tile.
        self._sync_slider_range()
        self._reconfigure_view()
        self._schedule_visible_request()
        self._sync_view_mode_combo()

    # ----------------------------------------------------------- settings

    def apply_settings_icon_size_max(self, new_max: int) -> None:
        new_max = max(self._ICON_SIZE_MIN, int(new_max))
        if new_max == self._icon_size_max:
            return
        self._icon_size_max = new_max
        self._list_icon_size = max(
            self._LIST_ICON_SIZE_MIN, min(self._icon_size_max, self._list_icon_size)
        )
        self._sync_slider_range()
        if self._view_mode == "list":
            self._reconfigure_view()

    def set_probe_parallelism(self, value: int) -> None:
        self._probe.set_parallelism(value)

    def set_scan_metadata_parallelism(self, value: int) -> None:
        """Forward the metadata-pass parallelism to the children scanner.

        Public wrapper so the window's initial propagation of the persisted
        setting doesn't reach into the private ``_scanner`` (#99).  Applies
        from the next scan; ``PostGrid.apply_settings`` covers the runtime
        settings-dialog path.
        """
        self._scanner.set_metadata_parallelism(value)

    # ----------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        """Cancel background scan/probe work before the window closes.

        Without this, QThreadPool teardown waits for in-flight workers —
        closing mid-scan of a huge (NAS) folder would hang the app.
        Subclasses with extra scanners extend this.

        **ペインの下に居るデバウンスを 1 本残らず止める** — ``close()`` は
        ペインを隠すだけなので、武装したままのデバウンスは隠れたウィジェット
        の寿命いっぱい発火し続ける。種類を名指しせず ``findChildren`` で
        **QObject 木そのものから導出する**ので、内側の ``GalleryView`` が
        自分で持つ分（遅延レイアウト / ◇ ドウェル）も、サブクラスが足した分
        も同じ 1 行に載る（新しい経路を足したときに「shutdown への配線を
        書き忘れる」が起こせない — 窓じまいの ``findChildren(QThreadPool)``
        と同じ作法）。  Measured on a 1,200-file folder, the icon-flush /
        visible-request pair fired *after* ``ViewerWindow.closeEvent`` had
        already drained the loader pools and closed every cache store,
        submitting 7 fresh decode tasks into the just-drained pool — which
        leaves the pool running again, so the test harness's "is this window
        idle?" reaper skips the window and it survives across modules
        (R2A-4).  ``_shutdown`` is the second layer: even if a timer re-arms,
        no new decode is submitted.
        """
        self._shutdown = True
        self._scanner.cancel()
        self._probe.cancel()
        for timer in self.findChildren(Debouncer):
            timer.stop()
        # Queued QImages nobody will paint — dropping them also keeps a late
        # flush (were one to sneak in) from touching QPixmap during teardown.
        self._pending_icons.clear()


__all__ = ["ChildrenGrid"]
