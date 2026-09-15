"""Left pane — navigation + children thumbnail grid for the viewer.

Immediate children of the current root (sub-folders AND files) are listed
as a thumbnail grid.  Selection / activation signals are split by kind:

* ``folder_selected`` / ``folder_activated`` — sub-folder row
* ``file_selected`` / ``file_activated``     — file row

Folders always sort ahead of files regardless of the chosen sort mode
(Explorer-style); the chosen sort then orders within each group.

:class:`PostGrid` composes three layers (the former single-module god class
was split — see the sibling modules for the moved halves):

* :class:`~.children_grid.ChildrenGrid` (base) — chunked populate, viewport
  thumb requests, icon flush, slider behaviour.
* :class:`~.advanced_search.AdvancedSearchController` — AI 検索（AIタグ /
  精度 / 種別 / 年齢区分 / 投稿日 / 意味 / 類似画像）のポップオーバーと、
  タグ / ベクトルスキャナの駆動。継承ではなく ``self._ai`` が**所有する**
  部品で、境界は :class:`~.advanced_search_parts.host.SearchHost`、旧名の
  委譲表は :mod:`.advanced_search_parts.facade`。
* :mod:`.filter_query` — the pure (Qt-free) filter-box syntax engine and
  sort-key specs, re-exported here for backwards compatibility.

What remains in this module: the unified-toolbar chrome (nav buttons /
breadcrumb / global search field / 表示 popover / ⋯ options — built here,
mounted window-level by ``main_window``), the post.md metadata-batch handler, the
recursive ("サブフォルダも検索") walk, the async ``body:`` filter worker
(single-thread pool so ``filter_query._BODY_CACHE`` stays single-owner),
the search-state snapshot for the navigation history, and a
``loading_changed`` signal driven by scanner progress.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QPoint,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui.timers import DebounceMode, Debouncer
from ..common.ui import (
    hint_style,
    localize_input_dialog,
    popover_position,
    show_toast,
)
from . import (
    ai_pack,
    condition_chips,
    filter_popover,
    filter_predicates,
    grid_chrome,
    grid_empty_state,
    grid_overlays,
    nsfw_filter,
)
from ._runnable import GuardedStream
from .advanced_search import AdvancedSearchController
from .advanced_search_parts import facade as _search_facade
from .breadcrumb import BreadcrumbBar
from .children_grid import ChildrenGrid, SelectRequest
from .condition_bar import CHIP_SPACING, ConditionBar
from .context_menus import (
    CurationHooks,
    EntryMenuContext,
    append_entry_verbs,
)
from .curation_list import TAG_PREFIX, CurationList
from .curation_recovery import (
    confirm_prefix_rebind,
    confirm_rebind_merge,
    plan_prefix_rebind,
    prompt_current_location,
    prompt_rebind_prefix,
    rebind_and_patch,
)
from .dialogs import host_picker_places, pick_existing_directory
from .empty_state import EmptyAction
from .focus_target import SEAT_GRID, curation_subject

# Re-exported for backwards compatibility: the filter-syntax engine moved to
# ``filter_query.py`` (pure logic, Qt-free) but tests and callers historically
# import these names from here.
from .filter_query import (  # noqa: F401  (re-exports)
    _BODY_CACHE,
    _CONTROL_FIELDS,
    _FILTER_FIELD_GETTERS,
    _FilterTerm,
    _entry_body_text,
    _match_filter_terms,
    _parse_filter_query,
    _parse_query,
    _sort_spec,
    parse_control_tokens,
    strip_control_tokens,
    strip_owned_control_tokens,
)
from .overlay_list import OverlayList
from .grid_tasks import (
    body_filter_matches,
    curation_metadata,
    nsfw_ratings,
    recent_files_walk,
)
from .folder_scan import (
    POST_MD_NAME,
    THUMB_MARKER_PREFIX,
    FolderEntry,
    select_thumbnail,
)
from .keyed_resolver import KeyedResolver
from .pending import Pending, always
from .perf import measure
from .scan_worker import RecursiveSearchScanner
from .search_dimensions import (
    token_fields,
)
from .search_dimensions import get as _dim_get
from .state import ViewerState
from .thumbnail_loader import ThumbnailLoader


# 値型・台帳定数・純関数ヘルパーは ``post_grid_types`` へ移した（移動のみ）。
# 歴史的に ``post_grid`` から import されてきた名前なので、ここから再輸出する。
from .post_grid_types import (  # noqa: F401  (re-exports)
    FILTER_HELP_POPUP_NAME,
    SUBTITLE_SEP,
    SearchSnapshot,
    _CURATION_FAIL_TOAST_MS,
    _CURATION_TOAST_MS,
    _OVERLAY_FIELD_TOKENS,
    _POST_MD_TOKEN_FIELDS,
    _SAVED_SEARCH_TAG_FIELDS,
    _SCOPE_ADV,
    _SCOPE_ALL,
    _SCOPE_PLAIN,
    _SCOPE_PLAIN_ADV,
    _SORT_LABELS,
    _TAG_INPUT_SEP,
    _UserTagCompleter,
    _ViewDim,
    _format_size,
    _format_subtitle,
    _park_cursor_at_end,
    _payload_num,
    _user_tag_token_split,
    describe_search_payload,
    deserialize_search_snapshot,
    saved_search_tooltip,
    serialize_search_snapshot,
    split_rel_caption,
)


class PostGrid(ChildrenGrid):
    """Left pane: navigation + filter / sort / locked / ``#thumb#`` chrome.

    Additional signals on top of :class:`ChildrenGrid`:

    * ``go_back_requested`` — ← button clicked (history pop / drill-down
      trail).
    * ``go_forward_requested`` — → button clicked; re-does a navigation
      that ``go_back_requested`` undid (forward stack).
    * ``go_up_requested`` — ↑ button clicked (Alt+Up in main_window);
      always navigates to the filesystem parent regardless of history.
    * ``help_requested`` — 初回起動カードの [操作の基本 (F1)]（中央の
      ``_WelcomeView`` と同じ 2 つ目の一手。カードは 2 席で対になっている）。
    * ``reload_requested`` — ↻ button clicked; re-scans the current root.
    * ``root_change_requested(Path)`` — user picked a new root via dialog.
    * ``loading_changed(bool)`` — True at scan start, False after the
      metadata pass finishes (drives the status-bar indicator).
    * ``search_status_changed(str)`` — recursive ("サブフォルダも検索") walk
      status / result summary, routed to the bottom-left status bar.  Empty
      string means "no recursive search active".
    """

    go_back_requested = Signal()
    go_forward_requested = Signal()
    help_requested = Signal()
    # ``go_up_requested`` は基底 :class:`ChildrenGrid` に移した（N-26）— 右一覧
    # でも Backspace が効くようにするため。ここでは ↑ ボタンからも emit する。
    reload_requested = Signal()
    root_change_requested = Signal(Path)
    loading_changed = Signal(bool)
    #: The shallow scan of the current root failed (offline NAS, permission
    #: error…).  Carries the raw error text; the window switches its status
    #: label to a failure message instead of 「読み込み完了」 (I01).
    scan_failed = Signal(str)
    #: 一覧は立ったが一部の子を分類できなかった（件数）。走査失敗とは別の
    #: チャネルなので結果は捨てない — 窓は完了ステータスへ「一部の項目を
    #: 読み取れませんでした」を添えるだけ。
    scan_partial = Signal(int)
    search_status_changed = Signal(str)
    #: (folder_count, file_count) of the tiles currently shown in the grid —
    #: emitted after every rebuild (including advanced / recursive search
    #: results, which reflect the displayed hits).  Drives the status-bar
    #: count widget.
    counts_changed = Signal(int, int)
    #: 右クリック「このファイルの場所を開く」(UI08-28 N-64 / 2026-09-11 N-40)。
    #: 横断一覧・検索ヒットの行は実体がどこにあるかを名前でしか示せず、そこへ
    #: 戻る手段がエクスプローラ経由しか無かった。このペインは窓の ``set_root``
    #: を知らないので意図だけを報告し、窓が親フォルダ + 当の項目の選択へ
    #: 着地させる（``recent_files_requested`` と同じ形）。
    reveal_in_app_requested = Signal(Path)
    #: A breadcrumb segment was clicked — the window should navigate to *Path*.
    #: PostGrid itself does nothing with it (wired in a follow-up phase).
    breadcrumb_navigate = Signal(Path)
    #: A user-curation value (star / user tags / watch-later) changed.  The
    #: window repaints the OTHER surfaces that render the same badges (right
    #: pane, an open lightbox) so they stay in sync with the left pane's map.
    curation_changed = Signal()
    #: 母集合を**全面入れ替え**する入口（横断キュレーション一覧 / 最近追加され
    #: たファイル一覧 / 保存した検索の適用）に入る直前。入れ替えは検索状態の
    #: 破棄・並び順の切替・パンくずの置換・全パスの off-thread 解決を伴うのに、
    #: プレビュー最大化中はグリッド席が幅 0 でそれが 1px も見えない。窓は
    #: これを受けて分割ビューへ戻す（``curation_changed`` と同じ grid → window
    #: 方向の通知型）。入口ごとに窓側で前置きを書くと、新しい入口 — プラグイン
    #: API・新メニュー — が増えたときに片側だけ欠ける。
    population_replacing = Signal()
    #: グリッドの母集合が検索 / 全面占有オーバーレイで入れ替わり、その結果に
    #: 選択が 1 つも残らなかった（UIレビュー 2026-08-28 N-13 / N-58）。横断
    #: キュレーション一覧・最近追加されたファイル一覧・AI 検索結果・平常の検索
    #: 着地の 4 経路が :meth:`_rebuild_grid` の 1 点でここへ合流する。窓は
    #: ``_reset_preview_panes``（set_root と同じリセット規約）でプレビュー列と
    #: 右情報パネルを未選択へ落とす — 入場前フォルダを映し続けて「現在地が
    #: 3 つ」になるのを防ぐ。先頭タイルの自動選択は**しない**（横断項目は別
    #: ボリュームにありうるので入場と同時に NAS 読み出しを起こさない）。
    preview_context_lost = Signal()
    #: The 「再読み込み」 button in the AI-panel's missing-DB banner was clicked
    #: (item K01).  The window re-opens tags.db and re-injects the fresh indexes
    #: via :meth:`set_tag_indexes`.
    reload_tag_db_requested = Signal()
    #: 条件バーの「この検索を保存…」が押された（UIレビュー 09-11 N-25）。
    #: ウィンドウ側の既存スロット ``_on_save_current_search`` へ届く。
    save_search_requested = Signal()

    # Class config overrides for the left pane.
    _ICON_SIZE_MIN = 96
    _CAPTION_PAD = 24
    _LIST_MODE_ICON_SIZE = 24
    _WITH_METADATA = True

    def __init__(
        self,
        loader: ThumbnailLoader,
        icon_size: int = 160,
        list_icon_size: int | None = None,
        sort_mode: str = "name_asc",
        view_mode: str = "icon",
        exclude_thumb_marker: bool = False,
        icon_size_max: int = 320,
        thumb_layout: str = "justified",
        meta_cache=None,
        folder_cache=None,
        search_index=None,
        tag_index=None,
        vector_index=None,
        user_meta=None,
        probe_parallelism: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        self._sort_mode = sort_mode
        # Whether the sort combo is currently showing the disabled 「関連度順」
        # placeholder (ranked semantic / similar results override the sort).
        self._sort_combo_ranked = False
        # Seed for the 「ランダム」 sort — stable within a session so filter
        # tweaks / metadata batches don't reshuffle under the cursor; F5
        # regenerates it (reshuffle_random_sort, called by the window's
        # reload handler).
        self._random_sort_seed = random.randrange(1 << 30)
        # Filter-syntax cheatsheet auto-popup: shown once per session, on the
        # first focus of the filter box (see eventFilter / _show_filter_help).
        self._filter_help_autoshown = False
        # クリック規約ヒント（N-146）— 平常ブラウズに初めてタイルが並んだ
        # とき 1 度だけ出す（``_maybe_show_click_hint``）。
        self._click_hint_shown = False
        self._click_hint: QFrame | None = None
        #: 絞り込み欄の実効値（``filter_edit.text().strip()``）。
        #:
        #: **不変条件**: この属性は ``filter_edit`` と**同時に**書く — 代入の
        #: 5 か所（``restore_search_state`` / ``clear_search`` /
        #: ``_on_filter_changed`` / ``_sync_filter_control_tokens`` /
        #: ``_drop_owned_control_tokens``）はどれも ``setText`` か
        #: ``textChanged`` と同じスタックで更新している。条件チップの重複
        #: 排除（``_render_condition_chips``）はこの不変条件に依存するので、
        #: 片方だけ書く経路を新設しないこと。
        self._filter_text = ""
        self._filter_locked_only = False
        # ★キーの確認トースト（UIレビュー #20）— 連打時に差し替えるため保持。
        self._star_toast = None
        # Last tile selected while NO search was engaged.  Typing a filter
        # rebuilds the grid and silently drops the selection, so this is the
        # only record of "what was selected before the search" — the
        # search-teardown restore (filter cleared / search toggles off) falls
        # back to it when nothing is selected among the results.
        self._presearch_selection: Path | None = None
        # 現在地そのものの ``(service, post_id)``（投稿フォルダで無ければ None）。
        # ``set_root`` が「これから離れる母集合」から取り、取れなければフォルダ
        # プレビューキャッシュ（ローカル sqlite）へ 1 回だけ聞く。中に立って
        # いる投稿フォルダは ``_entries`` に居ないので、ファイル行の postref は
        # ここを見ないと解決できない（:meth:`_postref_columns`）。
        self._root_postref: tuple[str, str] | None = None
        self._exclude_thumb_marker = exclude_thumb_marker
        # Caption-field visibility toggles (E-3), taken from ViewerState via
        # ``apply_settings``.  Defaults mirror ViewerState so the initial look
        # is unchanged (plan off, the rest on).
        self._caption_show_posted = True
        self._caption_show_locked = True
        self._caption_show_size = True
        self._caption_show_plan = False
        # Tile name placement (owner request 2026-07): "overlay" (default) rides
        # the name on a bottom scrim ON the thumbnail; "below" drops it out of
        # the image onto a surface-token band under the thumbnail.  Drives
        # ``_caption_overlay`` (read during the base ``__init__`` →
        # ``_build_params``), so it must be set BEFORE ``super().__init__``.
        # Taken from ViewerState at runtime via ``apply_settings``.
        self._caption_placement = "overlay"
        self._search_index = search_index
        self._entries: list[FolderEntry] = []
        # Recursive-search state: results from a descendant walk of the
        # current root.  ``None`` means "no recursive scan active" — the
        # grid falls back to shallow filtering on ``_entries`` alone.
        # ``_rel_paths`` maps str(path) → root-relative path string so
        # ``_caption_for`` can display the relative path for recursive
        # hits.  Direct children aren't keyed here so the caption falls
        # back to the existing title / basename behaviour.
        self._recursive_search = False
        self._recursive_results: list[tuple[FolderEntry, str]] | None = None
        self._rel_paths: dict[str, str] = {}
        # Query (includes, excludes, ~ OR pool as tuples) the worker filtered
        # ``_recursive_results`` against — guards _apply_filter_and_sort from
        # showing a stale needle's matches before the new walk lands.
        self._recursive_results_query: (
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
        ) = None
        self._pending_query: (
            tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
        ) = None
        # Recursive-search status bookkeeping (feeds search_status_changed):
        # whether a live walk is in flight, how many entries it has scanned
        # so far, how many rows the cache seed produced, and how many
        # descendants matched the current query at the last grid rebuild.
        self._recursive_scanning = False
        self._recursive_scanned = 0
        self._recursive_seed_count = 0
        # 走査が列挙できなかったディレクトリ数（0 = 答えは完全）。
        self._recursive_unreadable = 0
        # 上限に達して走査を打ち切った件数（0 = 打ち切っていない）。
        self._recursive_truncated = 0
        self._descendant_match_count = 0
        # AI 検索（AIタグ / 意味 / 類似 / 種別走査）は
        # :class:`~.advanced_search.AdvancedSearchController` が**所有する**
        # 部品で、生成はこの ``__init__`` の末尾 1 回（席もスキャナもそこで
        # 揃う）。ここで用意するのは 2 つだけ:
        #
        # * 索引ハンドル — **ホストの持ち物**。走査・NSFW 解決・詳細窓など
        #   AI 検索以外の面も読むので、コントローラより先に要る。
        # * ``_ai = None`` — chrome の構築（``super().__init__`` の中）は
        #   コントローラより先に走るので、委譲プロパティが「まだ中立」を
        #   答えられるようにしておく。
        self._tag_index = tag_index
        self._vector_index = vector_index
        self._ai: AdvancedSearchController | None = None
        # ``body:`` filter-field worker state (the post.md body reads run on
        # a single-thread pool — ``_body_stream`` / ``grid_tasks.body_filter_
        # matches``).  ``_body_filter_
        # matches`` is the landed ``(and_matched, or_matched)`` verdict pair
        # for ``_body_filter_sig``; ``None`` means no verdict yet (body-gated
        # entries stay hidden until the worker reports).  Invalidated whenever
        # the entry set / post.md metadata changes.  (The Qt pool / signal
        # objects are created after ``super().__init__`` below — QObject
        # construction needs the base.)
        self._body_filter_sig: tuple | None = None
        self._body_filter_matches: tuple[set[str], set[str]] | None = None
        self._pending_body_sig: Pending[tuple] = Pending()
        # Scan-lifecycle flag mirroring ``loading_changed``: True from
        # ``set_root`` until the scanner's ``metadata_finished`` closes the
        # pass.  The empty-state hook (``_empty_state_kind``) consults it so a
        # filter that matches nothing *yet* — post.md titles / tags still being
        # read — shows 「検索中…」 instead of a premature 「一致なし」.
        self._scan_loading = False
        # User curation layer (user_meta.db).  ``None`` disables stars / user
        # tags / watch-later entirely (no store).  ``_user_meta_map`` is an
        # in-memory ``{path: UserMeta}`` snapshot loaded once so paint / filter /
        # sort never touch sqlite — the store is written through on mutation and
        # the map patched in lockstep.
        self._user_meta = user_meta
        self._user_meta_map: dict = (
            user_meta.load_all() if user_meta is not None else {}
        )
        # UIレビュー 07-25 #135: 「スター (高い順)」 は user_meta が開けない環境では
        # 常に全件同点＝実質壊れた選択肢なのに、フィルタ側（★コンボ）だけが
        # ゲートされていた。並びも同じゲートに揃え、永続値がそれを指していた
        # ときは既定へ落とす（選べない値が選択状態のまま残らないように）。
        if user_meta is None and self._sort_mode == "star_desc":
            self._sort_mode = _SORT_LABELS[0][0]
        # NSFW view suppression (item 2-1) — a persistent VIEW setting, not a
        # search dimension.  ``_hide_nsfw`` is the band key ("off" /
        # "questionable" / "explicit"); ``_nsfw_rating_map`` caches known
        # representative ratings so the paint / rebuild path never touches
        # sqlite (the lookups run on ``_nsfw_stream`` —
        # ``grid_tasks.nsfw_ratings``).
        # Unknown tiles stay visible until their rating lands ("show until
        # known → hide once determined").  Plain attrs before super().__init__
        # because ``_build_chrome`` builds the ⋯-menu toggle during it.
        self._hide_nsfw = "off"
        self._nsfw_rating_map: dict[str, str] = {}
        # 受付済みのパス（一度渡したら再照会しない）は ``_nsfw_resolver`` が
        # 持つ: 判った区分は ``_nsfw_rating_map`` へ、答えに無かったパスは
        # 「未タグと確定」として受付済みのまま残り、毎再構築で投げ直されない。
        # ナビゲーションごとに投入を追い越さない（キャンセルされたタスクは
        # 答えないので、そのパスが未解決のまま固定される — A→B→A で explicit
        # なタイルがセッション中見え続けた形）。
        # How many entries the last ``_apply_nsfw_filter`` pass actually hid.
        # Lets the empty-state hook attribute an all-hidden grid to the NSFW
        # view setting (with a matching un-hide action) instead of blaming the
        # — possibly untouched — 絞り込み controls, whose 解除 button would
        # then do nothing.
        self._nsfw_hidden_count = 0
        # 初回起動（空ライブラリ + ナビ履歴なし）を空フォルダと区別するフラグ
        # (N-02 / 空状態オーケストレータ)。**判定はグリッドではできない** —
        # 履歴も既定ライブラリもウィンドウが持つので、ウィンドウが
        # :meth:`set_first_run` で教える。既定 False = 従来どおり
        # ``"empty_folder"``（単体でグリッドを使うテスト・部品利用は無影響）。
        self._first_run_empty = False
        self._first_run_default_library = False
        # Filter-bar (item 2-2) local in-place media predicate — VOLATILE
        # (never persisted; starts inactive).  Restricts whatever the grid
        # currently shows (direct children / recursive / advanced hits) to one
        # media kind by extension, in place — distinct from the AI panel's
        # recursive media walk.  "all" = no restriction.
        self._filterbar_media = "all"
        # Filter-bar curation predicates (H02) — VOLATILE in-place restrictions
        # over the current grid: ``_filterbar_star_min`` keeps only tiles whose
        # user star is ≥ N (0 = no restriction), ``_filterbar_later`` keeps only
        # 「あとで見る」 tiles.  Both resolve against the in-memory ``_user_meta_map``
        # (synchronous dict lookup, never sqlite/NAS on the paint path).  The
        # widgets exist only when a user_meta store is wired.
        self._filterbar_star_min = 0
        self._filterbar_later = False
        # ユーザータグの絞り込み (UIレビュー 07-25 #13②) — ★/あとで見ると同じ
        # 揮発の in-place predicate。``""`` = 「すべて」= 無制限。
        self._filterbar_user_tag = ""
        # ユーザータグ候補のキャッシュ (UIレビュー07-25 追修)。``None`` = dirty。
        # 実体は ``user_meta.db`` の全行走査 (``all_tags``) なので、フィルタ
        # ポップオーバーを開くたび・履歴を 1 歩戻るたびに GUI スレッドで
        # フルスキャンさせない。無効化はタグ編集とストア再読込の 2 か所だけ。
        self._user_tags_cache: list[str] | None = None
        # **グリッド全面を占有している一覧**（横断キュレーション H01 /
        # 最近追加されたファイル N-49）の状態。``None`` = 平常ブラウズ。
        # 2 つの一覧は「グリッド全体を奪う / 母集合をメモリに持つ / 入場前の
        # 並び順を退避する / 単一クラムに差し替える / 世代ガードで stale 着地を
        # 捨てる / 相互排他」という**同一の形**なので、13 + 9 個の平置き
        # フィールドと 4 経路 × 2 の片付けを 1 つの値オブジェクトへ寄せた
        # （:mod:`.overlay_list` — レビュー 2026-09-03 項目 #97）。種別は
        # :class:`~.curation_list.CurationList`（横断一覧）か ``Path``
        # （最近追加一覧）。永続化・履歴・チップの識別子は今までどおり
        # ``CurationList.key`` の文字列 / ``Path`` のまま。
        self._overlay: OverlayList | None = None
        # ``OverlayList`` が持つのは: 母集合 (``entries`` / ``rel_paths``)、
        # 読めずに落ちた行のプレースホルダ (#133 項目 3 —
        # ``curation_recovery.build_ghost_entries``。母集合とは別に持ち、
        # 並べ替え・絞り込みの外で末尾へ付く)、消えた / 読めなかった行の
        # 件数 (N-09 — 分けて数える)、全滅フラグ ``failed``、解決 / 走査が
        # in-flight であることを示す ``pending``、走査中の 「N 件走査」 用の
        # ``scanned``、そして入場前の並び順 ``saved_sort`` (07-25 #59)。
        # 可視タイル限定の post.md 後追い解決 (N-49 後半) の受付済み集合は
        # ``_curation_meta_resolver`` が持つ（世代と中断トークンは
        # ``_curation_meta_stream``）。一覧の母集合はスナップショットなので、
        # 解決結果は**その場更新**のみ（並べ替え・絞り込みの再適用はしない）。
        super().__init__(
            loader=loader,
            icon_size=icon_size,
            list_icon_size=list_icon_size,
            icon_size_max=icon_size_max,
            view_mode=view_mode,
            thumb_layout=thumb_layout,
            loader_key_prefix="",
            meta_cache=meta_cache,
            folder_cache=folder_cache,
            search_index=search_index,
            probe_parallelism=probe_parallelism,
            parent=parent,
        )
        # Folder-tile context menu (plugin-provided actions — see _on_context_menu).
        self._view.context_menu_requested.connect(self._on_context_menu)
        # Keyboard-completion signals from the gallery view (B-item 1):
        # Escape clears the active filter / search (left pane only — the右一覧
        # has no filter of its own).  Backspace（上の階層へ）は基底
        # ``ChildrenGrid`` が両ペイン分を中継する（N-26）。
        self._view.clear_filter_requested.connect(self._on_escape_clear)
        # Image-origin similarity (item 2-4): the hover 「◇」 overlay is only
        # meaningful with a vector index; enable + wire it for the left pane.
        # ``_similar_overlay_wired`` guards against a double-connect when a
        # later tags.db reload (item K01) re-enables the overlay.
        self._similar_overlay_wired = False
        if self._vector_index is not None:
            self._view.set_similar_overlay_enabled(True)
            self._view.similar_requested.connect(self._on_view_similar_requested)
            self._similar_overlay_wired = True
        # Curation overlays: paint-time star / watch-later lookup + digit-key
        # star setting.  Only wired when a store is present.
        if self._user_meta is not None:
            self._view.set_curation_provider(self._curation_badge_for)
            self._view.star_key_requested.connect(self._on_star_key)
            # バッジは絵のままでは意味が伝わらない (UIレビュー 07-25 #136) —
            # ♡/🔒 と同じくホバーで言葉に展開する。ユーザータグはバッジすら
            # 無い唯一のキュレーション次元だったので、ここが初めての表示面
            # になる (#13)。
            self._view.set_tooltip_extra_provider(self.curation_tooltip_lines)
        # Metadata batch + final-pass signals are PostGrid-specific.
        self._scanner.metadata_ready.connect(self._on_metadata_batch)
        self._scanner.metadata_finished.connect(self._on_metadata_finished)
        # Recursive search runs on its own worker pool so filter-text
        # churn doesn't queue overlapping walks of a deep NAS tree.
        self._recursive_scanner = RecursiveSearchScanner(
            search_index=search_index, parent=self,
        )
        self._recursive_scanner.results_ready.connect(self._on_recursive_results)
        self._recursive_scanner.progress.connect(self._on_recursive_progress)
        self._recursive_scanner.walk_incomplete.connect(
            self._on_recursive_walk_incomplete
        )
        self._recursive_scanner.walk_truncated.connect(
            self._on_recursive_walk_truncated
        )
        # Debounce filter typing — a 250 ms idle window before kicking off
        # a recursive scan keeps us from issuing one walk per keystroke
        # while the user is still editing the needle.
        self._recursive_debounce = Debouncer(
            self, 250, self._kick_recursive_scan, mode=DebounceMode.TRAILING
        )
        # 左ペインの off-thread 仕事は 4 本 + 1 本の ``GuardedStream``
        # （レビュー 2026-09-03 項目 #56 / #215）。以前はこの 5 経路がそれぞれ
        # 「世代カウンタ + CancelToken + 無親ブリッジ + 専用プール + shutdown
        # 配線」の 5 点セットを手書きしており、curation-meta だけが 2 点を
        # 落として在庫していた。ストリームは self に親付けするので
        # ``ViewerWindow._drain_loader_pools`` の ``findChildren`` が窓じまいの
        # 有界ドレインへ自動的に載せる = 配線漏れが原理的に起こせない。
        # ワーカー本体（何を計算するか）は Qt 非依存の ``grid_tasks.py``。
        #
        # ``body:`` 判定は単一スレッド（``filter_query._BODY_CACHE`` を単一
        # 所有者に保つ）。最新の署名だけが要るので投入は superseding。
        self._body_stream = GuardedStream(self)
        self._body_stream.bind(self._on_body_filter_results)
        # NSFW 代表レーティング解決（tags.db リーダは内部で直列化するので
        # 1 スレッドで足りる）。投入は**加算的** — 受付済みに入れたパスの
        # 答えを後続が捨ててはならない。受付済み集合と失敗時の解放は
        # ``KeyedResolver`` が持つ（横断一覧の後追い解決と同じ 1 実装）。
        # 「答えに無い鍵」はここでは**未タグと確定**なので解放しない。
        self._nsfw_stream = GuardedStream(self)
        self._nsfw_resolver = KeyedResolver(
            self._nsfw_stream,
            lambda items, job: nsfw_ratings(
                self._tag_index,
                [path for is_dir, path in items if is_dir],
                [path for is_dir, path in items if not is_dir],
                job.cancel,
            ),
        )
        self._nsfw_resolver.landed.connect(self._on_nsfw_ratings)
        # 横断一覧の解決 (H01)。キュレーション全件の per-path ``os.stat`` は
        # 秒単位で詰まり得るので、グローバルプール（短命 probe と共用）では
        # なく専用の単一スレッドへ出す（項目 #215 — ``_runnable`` の docstring
        # が名指ししていた「per-item stat loops」の移行漏れ）。
        self._curation_stream = GuardedStream(self)
        self._curation_stream.bind(self._on_curation_entries)
        # 横断一覧の可視タイル限定 post.md 後追い解決 (N-49 後半)。サムネ /
        # アスペクトと同じ「ビューポート内だけ」の規律に載せるため、可視範囲が
        # 動くたびに 80ms デバウンスで再評価する。投入は**加算的**で、1 tick の
        # 投入量の上限とその続きの投入は ``KeyedResolver`` が持つ。読めなかった
        # フォルダ（答えに無い鍵）は受付から解放して再要求できるようにする。
        self._curation_meta_stream = GuardedStream(self)
        self._curation_meta_resolver = KeyedResolver(
            self._curation_meta_stream,
            lambda entries, job: curation_metadata(
                entries, self._folder_cache, job.cancel
            ),
            batch=self._CURATION_META_BATCH,
            answered=lambda landed: [str(e.path) for e in landed],
        )
        self._curation_meta_resolver.landed.connect(self._on_curation_metadata)
        self._curation_meta_timer = Debouncer(
            self, 80, self._request_curation_metadata, mode=DebounceMode.TRAILING
        )
        self._view.visible_range_changed.connect(
            lambda *_a: self._schedule_curation_meta()
        )
        # 「最近追加されたファイル」 の走査。コールドな NAS ツリーの再帰走査は
        # スレッドを分オーダーで掴むので、専用プール（= ストリーム既定）に載せて
        # 短命な stat / decode probe を飢えさせない。進捗は ``progress``
        # （2 本目のシグナル）で「N 件走査」を出す。
        self._recent_stream = GuardedStream(self)
        self._recent_stream.bind(self._on_recent_files)
        self._recent_stream.bind_progress(self._on_recent_progress)
        # AI 検索の部品はここで 1 回だけ生成する（ポップオーバーの席・タグ /
        # 意味スキャナ・デバウンスが揃う）。``__init__`` を跨ぐ分割契約を
        # 持たないのが要点 — 旧 mixin は「状態は ``super().__init__`` の前 /
        # QObject は後」という 2 段の呼び出し規約をホストに強いていた。
        self._ai = AdvancedSearchController(host=self, folder_cache=folder_cache)

    # ------------------------------------------------------------ chrome

    def _build_chrome(self, outer_layout: QVBoxLayout) -> None:
        # Unified toolbar (layout redesign 2026-07, Phase 1-1): the old Row 1
        # (nav + breadcrumb), Row 2 (filter / recursive / sort) and Row 3
        # (⋯ options / 表示 / size slider) now live on ONE window-level 40px
        # bar that ``main_window`` mounts ABOVE the splitter.  PostGrid still
        # OWNS the widgets and their state (filter text, sort, view mode,
        # thumbnail size, …) — the toolbar is only their seat — so every
        # existing signal path, persistence round-trip and programmatic
        # restore (nav history / saved searches) updates the toolbar display
        # for free (two-way sync by construction).
        self.toolbar = self._build_toolbar()

        # Adaptive filter controls (種別 / 投稿日 / ★ / あとで見る — item 2-2)
        # moved into a small popover behind the toolbar's フィルタ button
        # (Phase 1-3): the old dedicated 「フィルタ ▾」 row is gone.  Applied
        # values surface as chips on the condition bar below.
        self._build_filter_popover()

        # Condition chip bar (Phase 1-3): a window-level 1-row strip directly
        # under the toolbar showing ONLY the applied search conditions as
        # chips (click = edit popover, × = drop that one dimension), plus the
        # hit count and 「すべて解除」.  Replaces both the old 「フィルタ ▾」
        # row and the Row 5 search banner; hidden (zero height) while nothing
        # is engaged.  PostGrid owns it, main_window mounts it under the
        # toolbar — same seat pattern as ``toolbar``.
        self.condition_bar = self._build_condition_bar()

        # Item E02: field-scoped tokens (``tags:`` / ``title:`` / ``body:`` …)
        # only match the DIRECT children — the recursive walk sees bare
        # descendant paths, not their post.md metadata, by design (see
        # docs/claude/viewer/search.md).  When "サブフォルダも検索" is on AND the
        # filter box carries such a token, a small note makes that limit
        # explicit instead of silently returning fewer descendants than the user
        # expects.  Control tokens (type:/rating:/score:) apply globally, so they
        # don't trip it.
        self.field_scope_note = QLabel(
            t("viewer.post_grid.field_scope_note")
        )
        self.field_scope_note.setWordWrap(True)
        # 補助文の見た目は ``common/ui/qss.py`` の 1 実装から引く（同じ値を
        # 手書きすると、そちらに書式が足されたときこの席だけ取り残される）。
        self.field_scope_note.setStyleSheet(hint_style())
        self.field_scope_note.setVisible(False)
        outer_layout.addWidget(self.field_scope_note)

    # ------------------------------------------------------------ toolbar

    def _build_toolbar(self) -> QWidget:
        """Build the window-level unified toolbar (Phase 1-1).

        Returned **parentless**; ``main_window._build_ui`` mounts it above
        the splitter, which reparents it into the window.  部品の実体は
        :mod:`.grid_chrome`（:class:`~.grid_chrome.GridToolbar` /
        :class:`~.grid_chrome.SearchField`）で、この殻は**部品を組んで意図を
        既存のスロットへ繋ぐだけ**を持つ。PostGrid still OWNS the widgets and
        their state (filter text, sort, view mode, thumbnail size, …) — the
        toolbar is only their seat — so every existing signal path,
        persistence round-trip and programmatic restore (nav history / saved
        searches) updates the toolbar display for free.
        """
        self._search_field = grid_chrome.SearchField(
            ai_available=ai_pack.available()
        )
        self._search_field.mode_clicked.connect(self._on_mode_chip_clicked)
        self._search_field.ai_chip_clicked.connect(self._on_mode_chip_ai_clicked)
        self._search_field.help_clicked.connect(self._on_filter_help_clicked)
        self._search_field.text_changed.connect(self._on_filter_changed)
        self._search_field.text_edited.connect(self._on_filter_text_edited)
        self._search_field.submitted.connect(self._on_filter_submitted)
        self._filter_prefix_completer = grid_chrome.build_prefix_completer(
            self.filter_edit
        )
        # First-focus syntax cheatsheet (session-once) + Esc-to-dismiss are
        # handled in :meth:`eventFilter`; typing hides it via ``text_edited``.
        self.filter_edit.installEventFilter(self)
        self._filter_help = grid_chrome.FilterHelpPopups(
            parent=self, anchor=self.filter_edit
        )

        bar = grid_chrome.GridToolbar(
            search_field=self._search_field,
            view_button=self._build_view_button(),
        )
        self._chrome_toolbar = bar
        bar.back_clicked.connect(self.go_back_requested.emit)
        bar.forward_clicked.connect(self.go_forward_requested.emit)
        bar.up_clicked.connect(self.go_up_requested.emit)
        bar.reload_clicked.connect(self.reload_requested.emit)
        bar.change_root_clicked.connect(self._on_change_root_clicked)
        bar.filter_clicked.connect(self._open_filter_popover)
        bar.breadcrumb.navigate.connect(self.breadcrumb_navigate.emit)
        # ↻ のツールチップは並び順で文面が変わる（部品は並びを知らない）。
        self._sync_reload_tooltip()
        return bar

    # -------------------------------------------- chrome accessors（同一実体）
    #
    # 席は :mod:`.grid_chrome` の部品が持つが、**外から観測される名前**（窓 /
    # テスト / ``tools/ui_review``）は変えない — 以下は部品が作った同一の
    # ウィジェットを返すだけの読み取り専用プロパティ。AI パック無効の配布で
    # 席そのものが無い 3 つ（``mode_chip_ai`` / ``nsfw_btn`` / ``nsfw_menu``）
    # は ``AttributeError`` を上げ、``getattr(..., None)`` / ``hasattr`` の
    # 従来判定をそのまま通す。

    @property
    def back_btn(self) -> QToolButton:
        return self._chrome_toolbar.back_btn

    @property
    def forward_btn(self) -> QToolButton:
        return self._chrome_toolbar.forward_btn

    @property
    def up_btn(self) -> QToolButton:
        return self._chrome_toolbar.up_btn

    @property
    def reload_btn(self) -> QToolButton:
        return self._chrome_toolbar.reload_btn

    @property
    def breadcrumb(self) -> BreadcrumbBar:
        return self._chrome_toolbar.breadcrumb

    @property
    def change_root_btn(self) -> QToolButton:
        return self._chrome_toolbar.change_root_btn

    @property
    def filter_btn(self) -> QToolButton:
        return self._chrome_toolbar.filter_btn

    @property
    def nav_rail_btn(self) -> QToolButton:
        return self._chrome_toolbar.nav_rail_btn

    @property
    def preview_btn(self) -> QToolButton:
        return self._chrome_toolbar.preview_btn

    @property
    def info_panel_btn(self) -> QToolButton:
        return self._chrome_toolbar.info_panel_btn

    @property
    def filter_edit(self) -> QLineEdit:
        return self._search_field.filter_edit

    @property
    def mode_chip_name(self) -> QToolButton:
        return self._search_field.mode_chip_name

    @property
    def mode_chip_body(self) -> QToolButton:
        return self._search_field.mode_chip_body

    @property
    def mode_chip_ai(self) -> QToolButton:
        chip = self._search_field.mode_chip_ai
        if chip is None:
            raise AttributeError("mode_chip_ai")
        return chip

    @property
    def _filter_icon_action(self) -> QAction:
        return self._search_field.filter_icon_action

    @property
    def _filter_help_action(self) -> QAction:
        return self._search_field.help_action

    @property
    def _filter_help_popup(self) -> QFrame | None:
        return self._filter_help.popup

    @property
    def _filter_help_auto(self) -> QFrame | None:
        return self._filter_help.auto

    @property
    def exclude_thumb_check(self) -> QCheckBox:
        return self._display_options.exclude_thumb_check

    @property
    def nsfw_btn(self) -> QPushButton:
        btn = self._display_options.nsfw_btn
        if btn is None:
            raise AttributeError("nsfw_btn")
        return btn

    @property
    def nsfw_menu(self) -> QMenu:
        menu = self._display_options.nsfw_menu
        if menu is None:
            raise AttributeError("nsfw_menu")
        return menu

    @property
    def _nsfw_actions(self) -> dict[str, QAction]:
        return self._display_options.nsfw_actions

    @property
    def _nsfw_action_group(self) -> QActionGroup | None:
        return self._display_options.nsfw_action_group

    # ------------------------------------------------- toolbar: search field

    #: 検索欄の上限幅（実体は :data:`grid_chrome.SEARCH_FIELD_MAX_W`）。
    _SEARCH_FIELD_MAX_W = grid_chrome.SEARCH_FIELD_MAX_W

    def _on_filter_text_edited(self, _text: str) -> None:
        self._hide_filter_help_auto()

    def _on_filter_submitted(self) -> None:
        self._jump_to_results()

    # ------------------------------------------------- toolbar: mode chips

    def _on_mode_chip_ai_clicked(self) -> None:
        """AIタグ chip clicked — open the AI popover, then restore the check
        state (the checkable chip auto-toggled on click, but its lit state must
        track ``_advanced_search_active`` only — UIレビュー #10)."""
        self.focus_tag_search()
        self._sync_search_mode_chips()

    def _sync_search_mode_chips(self) -> None:
        """Reflect the current search dimension in the 名前 / 本文 / AIタグ chips.

        Called from ``_rebuild_grid`` — the single display choke point — so
        programmatic restores (history 戻る, saved searches, teardown) keep
        the chips truthful without per-call-site syncing.  ここは**観測値を
        束ねるだけ**で、見た目の決定は
        :meth:`grid_chrome.SearchField.apply_modes` が 1 か所で持つ。
        """
        field = getattr(self, "_search_field", None)
        if field is None:  # chrome not built yet (defensive)
            return
        ai_active = (
            field.mode_chip_ai is not None and self._advanced_search_active()
        )
        # 全面占有一覧（横断キュレーション / 最近追加）の母集合は post.md を
        # 持たないので ``body:`` は 1 件も照合できない — AI 中 (N-05) と同じ
        # 「点かないのに押せるボタン」を作らない。押下は素の語を ``body:`` へ
        # 書き換えるので、そのまま押せると効いていた絞り込みが黙って全解除
        # される（一覧の絞り込みは素の語だけが効く）。
        overlay_active = self._overlay is not None
        body_usable = not ai_active and not overlay_active
        has_body, has_bare = grid_chrome.filter_token_kinds(
            field.filter_edit.text()
        )
        field.apply_modes(
            ai_active=ai_active,
            overlay_active=overlay_active,
            body_usable=body_usable,
            body_mode=has_body and not has_bare and body_usable,
        )
        # N-30: AI 検索は常に再帰（ワーカーが表示中フォルダ配下を丸ごと歩く）
        # ため、「サブフォルダも検索」は AI 中は無効果 — フィルターポップ
        # オーバー側のチェックを無効化し、理由をツールチップで名乗る。同期点は
        # このチョークポイント
        # （_rebuild_grid → _sync_search_mode_chips）に相乗り。
        # 🔒「ロックありのみ」中も同じ理由で無効効果 — 子孫は post.md を読まない
        # ので locked_count を持てず、範囲を広げても 1 件も採れない
        # （``_maybe_start_recursive_scan`` が走査を蹴らないことと対）。
        recursive_check = getattr(self, "recursive_check", None)
        if recursive_check is not None:
            locked_only = self._filter_locked_only
            recursive_check.setEnabled(not ai_active and not locked_only)
            if ai_active:
                tip = t("viewer.post_grid.recursive_tooltip_ai")
            elif locked_only:
                tip = t("viewer.post_grid.recursive_tooltip_locked")
            else:
                tip = t("viewer.post_grid.recursive_tooltip")
            recursive_check.setToolTip(tip)

    def _on_mode_chip_clicked(self, mode: str) -> None:
        """名前 ⇄ 本文 chip clicked — rewrite the query in place.

        書き換えの規則は純関数 :func:`grid_chrome.rewrite_query_for_mode`。
        The rewritten text flows through the normal ``textChanged`` →
        ``_on_filter_changed`` path, so no second query route exists.
        """
        edit = self.filter_edit
        new = grid_chrome.rewrite_query_for_mode(edit.text(), mode)
        if new != edit.text():
            edit.setText(new)  # fires _on_filter_changed
        self._sync_search_mode_chips()
        edit.setFocus(Qt.ShortcutFocusReason)
        edit.setCursorPosition(len(edit.text()))

    # ------------------------------------------- toolbar: 表示 popover / ⋯

    def _build_view_button(self) -> QToolButton:
        """The 「表示」 popover button: sort / layout / thumbnail size.

        The three controls keep their historical attribute names + wiring
        (``sort_combo`` / ``view_mode_combo`` / ``size_slider``) so state
        persistence, the ranked-sort placeholder swap and the slider-range
        sync are untouched — only their seat moved into a popover
        (:class:`grid_chrome.ViewPopover`).
        """
        self.view_popover_btn = grid_chrome.make_view_button(
            self._on_view_popover_clicked
        )
        self._view_popover = grid_chrome.ViewPopover(
            self,
            build_rows=self._build_view_popover_rows,
            build_options=self._build_display_options,
        )
        return self.view_popover_btn

    def _build_view_popover_rows(self, pop_lay: QVBoxLayout) -> None:
        """「並び・表示」ポップオーバーの 3 行を組む（枠から呼ばれる）."""
        # 並び順 → 表示形式 → サムネイルサイズ の 3 行は右ペインと共通の
        # ファクトリから作る（N-75 — 行の並び・ラベル・選択肢順・操作方法が
        # 左右で割れていた）。並び順コンボの現在値選択と接続だけはここで
        # 行う（ランク表示中の差し替え `_sync_sort_combo_for_rank` がある）。
        self._build_view_settings_rows(
            pop_lay,
            sort_choices=self._available_sort_labels(),
            slider_width=160,
        )
        self._select_sort_in_combo(self._sort_mode)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

    def _on_view_popover_clicked(self) -> None:
        self._view_popover.popup_at(self.view_popover_btn)

    def _build_display_options(self, parent: QWidget) -> QWidget:
        """表示オプション（``#thumb#`` 除外 / 年齢区分）の席を組んで配線する。

        実体は :class:`grid_chrome.DisplayOptions`。属性名
        (`exclude_thumb_check` / `nsfw_menu` / `_nsfw_actions`) と
        `_sync_nsfw_menu` は不変 — setChecked / isChecked / toggled を使う
        呼び出し側は無変更で動く。
        """
        opts = grid_chrome.DisplayOptions(
            parent,
            ai_available=ai_pack.available(),
            exclude_thumb_checked=self._exclude_thumb_marker,
            nsfw_label_keys=self._NSFW_LABEL_KEYS,
        )
        self._display_options = opts
        opts.exclude_thumb_toggled.connect(self._on_exclude_thumb_toggled)
        opts.nsfw_band_selected.connect(self.set_hide_nsfw)
        if opts.nsfw_menu is not None:
            # 旧・サブメニューは親メニューがグレーアウトを描いてくれたが、
            # 独立ボタンになった以上は席そのものを無効化して理由を出す
            # （席とメニューの対は :meth:`_apply_nsfw_tagsdb_gate` が持つ）。
            self._apply_nsfw_tagsdb_gate(self._tag_index is not None)
            self._sync_nsfw_menu()
        return opts

    # ------------------------------------------------------ filter popover

    def _build_filter_popover(self) -> None:
        """Adaptive filter popover — **rows generated from the dimension ledger**.

        Phase 1-3 moved these axes off the old dedicated 「フィルタ ▾」 row
        into a small popover behind the toolbar's フィルタ button; applied
        values surface as condition-bar chips whose click re-opens this
        popover.  Values are VOLATILE (never persisted) — everything starts
        all-default.  Widget attribute names + handlers are unchanged, so the
        predicate plumbing, the ``type:`` control-token sync and the search
        snapshot round-trip are untouched.

        UIレビュー 2026-08-28 提案2 第2段: 行の組み立ては
        :mod:`.filter_popover` が台帳 :mod:`.search_dimensions` の列挙で行う
        （ラベル・コロン・中立値の語・ツールチップの AI 出し分け = N-51・
        選択肢が台帳 1 枚から来るので、N-99 の書式ばらけが**軸ごとに手書き
        する余地ごと**消える）。年齢区分もそこに席を持つ（AI ポップオーバー
        の重複軸整理 — 07-25 #41）。
        """
        self._filter_popover = filter_popover.build(
            self,
            ai_on=ai_pack.available(),
            have_user_meta=self._user_meta is not None,
        )
        self._update_filter_bar()

    def _build_date_range_editors(self, date_row: QHBoxLayout) -> None:
        """投稿日行の「範囲」指定エディタ（開始 〜 終了）を *date_row* へ足す.

        ``filter_popover`` の行生成が名指しで呼ぶフック（投稿日だけが行内に
        追加のインラインコントロールを持つ軸なので、台帳の生成ループからは
        外れている）。組み立ての実体は
        :func:`grid_chrome.build_date_range_editors`。
        """
        editors = grid_chrome.build_date_range_editors(
            date_row, on_changed=self._on_tag_date_changed,
        )
        self.tag_date_from = editors.date_from
        self.tag_date_sep = editors.separator
        self.tag_date_to = editors.date_to

    def _open_filter_popover(self, anchor: QWidget | None = None) -> None:
        """Show the adaptive filter popover under *anchor* (default: the
        toolbar フィルタ button; condition-bar chips pass themselves)."""
        pop = self._filter_popover
        # ユーザータグ候補はストアの現在値 — 開くたびに詰め直す（別ペインや
        # ライトボックスからの編集も拾う。UIレビュー 07-25 #13②）。
        self._reload_user_tag_choices()
        pop.adjustSize()
        if anchor is None:
            anchor = getattr(self, "filter_btn", None)
        if anchor is not None:
            pop.move(popover_position(anchor, pop.size(), align="right"))
        pop.show()
        pop.raise_()

    #: :meth:`_filter_bar_engaged` が数えない行（実体は
    #: :data:`filter_predicates.BAR_EXCLUDED_ROWS`）— 「サブフォルダも検索」は
    #: 絞り込みではなく**検索範囲**の軸（チップも別扱い）。
    _BAR_EXCLUDED_ROWS = filter_predicates.BAR_EXCLUDED_ROWS

    def _filter_bar_engaged(self) -> bool:
        """絞り込みの軸が 1 つでも非既定値か（判定は :mod:`.filter_predicates`）."""
        return filter_predicates.bar_engaged(
            self._filter_row_engaged(), excluded=self._BAR_EXCLUDED_ROWS
        )

    def _reload_user_tag_choices(self, *, cacheable: bool = True) -> None:
        """Refill the ユーザータグ combo from the store, keeping the selection.

        UIレビュー 07-25 #13②.  The candidate list is whatever tags exist in
        ``user_meta.db`` right now, so it is refreshed after an edit and each
        time the popover opens rather than frozen at construction.  A currently
        selected tag that no longer exists anywhere is kept as a choice so the
        engaged filter doesn't silently reset itself under the user.

        UIレビュー07-25 追修: 値は :meth:`all_user_tags` のキャッシュ越しに読む
        （ポップオーバーを開くたび・履歴 1 歩ごとに全行走査しない）。
        *cacheable* を False にすると素読みし、**キャッシュも焼かない** —
        構築時の一発 fill 専用で、ホストがストアやキュレーション map を差し込む
        前の値を「確定値」として残さないため。
        """
        combo = getattr(self, "filterbar_usertag_combo", None)
        if combo is None:
            return
        if cacheable:
            choices = self.all_user_tags()
        elif self._user_meta is not None:
            choices = self._user_meta.all_tags()
        else:
            choices = []
        current = self._filterbar_user_tag
        if current and current not in choices:
            choices = [current, *choices]
        combo.blockSignals(True)
        combo.clear()
        # 中立値の語は他軸と同じ「すべて」（N-99 — 旧「(すべて)」独自語）。
        # 語は台帳から引く（``filter_popover._build_row`` と同じ書き方 — この行
        # だけ手書きだと、台帳を直しても片側が置いていかれる）。
        usertag_dim = _dim_get("usertag")
        neutral_key = (
            usertag_dim.neutral_key if usertag_dim is not None else None
        )
        combo.addItem(t(neutral_key or "common.filter.all"), "")
        for tag in choices:
            combo.addItem(tag, tag)
        idx = combo.findData(current)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _filter_row_engaged(self) -> dict[str, bool]:
        """フィルターポップオーバーの各軸が効いているか ``{軸 id: bool}``.

        値は素の状態フィールドから読む（ウィジェット不在の構成でも同じ式が
        成立する）。``_view_dimensions`` の ``engaged`` 閉包と同じ判定だが、
        そちらは AI クエリのモード判定に AI ポップオーバーのウィジェットを
        使うため、**フィルターポップオーバー構築中には呼べない**（AI 側は
        まだ建っていない）。この 1 メソッドがアクセント同期の情報源。
        """
        return filter_predicates.row_engaged(self._filter_bar_criteria())

    def _update_filter_bar(self) -> None:
        """Sync the accent state of the adaptive-filter popover controls.

        An engaged axis accents its control (via ``palette(highlight)`` so it
        tracks theme switches); the applied values themselves surface as
        condition-bar chips (Phase 1-3 — the old collapse/expand row logic is
        gone with the row).

        提案2 第2段: 軸ごとの手書きから台帳の列挙（:mod:`.filter_popover`）
        へ。エディタ種別でアクセントの QSS が決まるので、行を足すときに
        ここへ追記する必要が無い（= 片側だけアクセントが付かない、が
        起こせない）。
        """
        if getattr(self, "filterbar_media_combo", None) is None:
            return
        filter_popover.sync_accents(
            self,
            self._filter_row_engaged(),
            have_user_meta=self._user_meta is not None,
        )

    def _on_filterbar_star_changed(self) -> None:
        self._filterbar_star_min = int(self.filterbar_star_combo.currentData() or 0)
        # UIレビュー07-25 追修: コントロール操作は同じ次元のトークンを**置き換える**
        # （残すと、コンボが示す値と違う条件が黙って AND され続ける）。
        self._strip_synced_curation_token("star")
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._update_filter_bar()
        self._rebuild_grid()

    def _on_filterbar_later_toggled(self, checked: bool) -> None:
        self._filterbar_later = bool(checked)
        # UIレビュー07-25 追修: 同上 — チェックを外したのに ``later:yes`` が
        # 効き続ける（しかもチップに出ない）状態を作らない。
        self._strip_synced_curation_token("later")
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._update_filter_bar()
        self._rebuild_grid()

    def _on_filterbar_usertag_changed(self) -> None:
        self._filterbar_user_tag = str(
            self.filterbar_usertag_combo.currentData() or ""
        )
        # N-65: コントロール操作は同じ次元のトークンを**置き換える**（★ /
        # あとで見る と同じ契約 — 残すと、コンボが示す値と違う条件が黙って
        # AND され続ける）。消すのは既存タグと完全一致する同期可能形だけ。
        self._strip_synced_curation_token("mytags")
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._update_filter_bar()
        self._rebuild_grid()

    def _on_filterbar_media_changed(self) -> None:
        self._filterbar_media = self.filterbar_media_combo.currentData() or "all"
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._update_filter_bar()
        self._rebuild_grid()

    def _on_filterbar_date_changed(self) -> None:
        # 投稿日コンボの帳簿（範囲エディタの可視制御 + AI 検索の引き直し）は
        # AI 検索コントローラのハンドラへ委ね、そのあとバーの適応表示を更新
        # する。平常グリッドは、そのハンドラが起こす再構築で
        # ``_apply_date_predicate`` 経由の日付を拾う。
        self._on_tag_date_preset_changed()
        self._update_filter_bar()

    def _available_sort_labels(self) -> list[tuple[str, str]]:
        """``_SORT_LABELS`` minus the modes this install can't honour (#135).

        「スター (高い順)」 needs the user-curation store: with no ``user_meta.db``
        every entry sorts at star 0, so the option was a choice that silently
        did nothing.  Gated exactly like the フィルタ popover's ★ combo, which
        already disappears in that configuration — the two now degrade together
        (UIレビュー 07-25 #135).
        """
        if self._user_meta is not None:
            return list(_SORT_LABELS)
        return [(key, label) for key, label in _SORT_LABELS if key != "star_desc"]

    #: 投稿日系の並び順キー（``post.md`` の ``posted_at`` が唯一の情報源）。
    _POSTED_SORT_KEYS = ("posted_desc", "posted_asc")

    def _tiled_entries(self, pool: list[FolderEntry]) -> list[FolderEntry]:
        """*pool* のうち、いまグリッドにタイルとして並んでいる行だけを返す。

        再構築を通らない更新経路 (:meth:`_replace_enriched_tiles`) が
        :meth:`_sync_posted_sort_enabled` を再評価するときの母集合。再構築側は
        絞り込み後の母集合を渡すので、こちらもタイル 1 枚単位で同じ答えになる
        集合へ揃える（属性走査のみ・I/O ゼロ）。
        """
        view = self._view
        keys = set()
        for i in range(view.tile_count()):
            tile = view.tile_at(i)
            if tile is not None:
                keys.add(str(tile.path))
        return [e for e in pool if str(e.path) in keys]

    def _sync_posted_sort_enabled(self, entries: list[FolderEntry]) -> None:
        """投稿日系の並び順を post.md 不在の一覧で無効化する (N-93).

        ``post.md`` はオプショナルなので、持たない一覧では ``posted_*`` は
        ``posted_at is None`` 同士の安定ソート = 名前順と区別が付かない並びに
        なる。``star_desc`` が店の無い構成で候補から落ちる先例
        (:meth:`_available_sort_labels`) と同じ「効かない選択肢を出さない」
        方針だが、こちらは**候補の出し入れではなく行の活性**で劣化させる:
        母集合は一覧を跨いで変わる（横断一覧 / 最近追加 / 検索ヒット）ので、
        コンボを組み直すと現在の選択が飛ぶ。理由はツールチップで言う。

        いま選ばれている並びが ``posted_*`` のときは無効化しない — 「選べない
        項目が選ばれている」状態を作らないため。永続値 (``state.sort_mode``)
        には触らない。*entries* は実際に並んでいる母集合（再構築は絞り込み後の
        母集合を、再構築を通らない更新経路は :meth:`_tiled_entries` を渡す —
        2 経路が別の母集合を見ると活性が行き来する）で、``posted_at`` は
        ``FolderEntry`` の既存フィールドなので I/O はゼロ。

        判定は代理指標（post.md ファイルの有無）ではなく**日付の実在**で行う:
        ``posted_at`` は post.md の任意項目（docs/formats/post-md.md）なので、
        日付を書かない post.md / 壊れた post.md だけの一覧では
        ``has_post_md`` が真でも並びは名前順と区別が付かない。
        """
        if self._sort_combo_ranked:
            # ランク表示中のコンボは「関連度順」1 項目だけ（並びは無効）。
            return
        combo = self.sort_combo
        item_of = getattr(combo.model(), "item", None)
        if item_of is None:  # pragma: no cover (QStandardItemModel 以外)
            return
        has_post = any(e.posted_at is not None for e in entries)
        tip = (
            "" if has_post
            else t("viewer.post_grid.sort_posted_unavailable_tooltip")
        )
        for i in range(combo.count()):
            key = combo.itemData(i)
            if key not in self._POSTED_SORT_KEYS:
                continue
            item = item_of(i)
            if item is None:  # pragma: no cover (defensive)
                continue
            item.setEnabled(has_post or key == self._sort_mode)
            combo.setItemData(i, tip, Qt.ToolTipRole)

    def _select_sort_in_combo(self, sort_mode: str) -> None:
        for i in range(self.sort_combo.count()):
            if self.sort_combo.itemData(i) == sort_mode:
                self.sort_combo.setCurrentIndex(i)
                return
        self.sort_combo.setCurrentIndex(0)

    # --------------------------------------------------------- caption

    def _caption_overlay(self) -> bool:
        # The post grid is the hero surface (redesign 2026-07): its icon tiles
        # ride the title on a bottom gradient scrim with the ♡/★/あとで見る/
        # 関連度 badges consolidated into the bottom-right seat, so the ♡ badge
        # can no longer collide with the title (ui-review 2026-07-18 診断⑥).
        # The owner-requested 「画像の下に表示」 mode opts out: it reserves a
        # below-image caption strip (``caption_height > 0``) so the name sits
        # OUT of the photo on a label plate — better legibility on busy images.
        # In that mode the badges stay on the thumbnail (legacy seating) and the
        # title is external, so they never collide either.
        return self._caption_placement != "below"

    def _apply_caption_placement(self, placement: str) -> None:
        """Switch the tile name placement (overlay ⇄ below) live.

        Unknown values fall back to the overlay default (forward-compat with
        the plain-``str`` state field).  Changing it flips ``caption_height``
        between 0 (on-image scrim) and the two-line strip, so the view is
        reconfigured — the justified / row-height metrics pick the taller cells
        up through the normal ``_build_params`` path — and the surface-token
        caption band is toggled to match.  A no-op when the value is unchanged,
        so the common settings-apply (nothing touched here) never re-lays out.
        """
        placement = "below" if placement == "below" else "overlay"
        # Keep the band flag truthful even on the first apply (value unchanged
        # from the "overlay" default → the guard below returns, and the band is
        # already off), and re-lay out only on a real change.
        if placement == self._caption_placement:
            return
        self._caption_placement = placement
        self._view.set_caption_band(placement == "below")
        # Preserve the current selection across the relayout (the tile array is
        # untouched, but ensure the chosen cell stays on screen after the
        # heights change).
        self._reconfigure_view()
        cur = self._view.selected_index()
        if cur >= 0:
            self._view.ensure_visible(cur)
        self._schedule_visible_request()

    def _curation_list(self) -> CurationList | None:
        """占有中の一覧が**横断キュレーション**ならその種別（他は ``None``）。

        「一覧に居るか」ではなく「★ / あとで見る / タグの一覧に居るか」を
        訊きたい数か所（★編集の再ソート抑止・ゴースト・後追い解決）のための
        1 実装。「どちらでもいいから一覧に居るか」は ``self._overlay is not
        None`` で足りる。
        """
        overlay = self._overlay
        return None if overlay is None else overlay.curation

    def _caption_for(self, entry: FolderEntry) -> str:
        # Recursive-search hits show their root-relative path so the user
        # can tell at a glance where the match lives.  Direct children
        # aren't keyed in ``_rel_paths``, so they keep the existing
        # title / basename caption.  The view handles alignment per view
        # mode; we only build the text (head + optional subtitle).
        rel = (
            self._rel_paths.get(str(entry.path))
            or self._tag_rel_paths.get(str(entry.path))
            or (
                self._overlay.rel_paths.get(str(entry.path))
                if self._overlay is not None else None
            )
        )
        head = entry.title
        parent = ""
        if rel:
            if self._is_curation_ghost(entry.path):
                # ゴーストの「rel」はパスではなく完成済みの文言
                # （``curation_recovery.build_ghost_entries`` の
                # 「名前（見つかりません）」）— 分解してはいけない。
                head = rel
            else:
                # N-08: 名前を 1 行目へ、親パスはサブタイトル側へ回す。
                head, parent = split_rel_caption(rel)
                if (
                    entry.is_dir
                    and entry.metadata_loaded
                    and entry.title
                    and entry.title != entry.path.name
                ):
                    # post.md の投稿タイトルが判っている投稿フォルダの行は、
                    # 平常グリッドと同じ見出しを出す（親パスは副題のまま）。
                    # N-08 が守りたかったのは「省略で実体名から削られる」
                    # ファイル行で、タイトルを持つフォルダ行では生のフォルダ名
                    # （``20260101_000000`` のような ID 名）が 1 行目に居座り、
                    # 同じフォルダが面によって違う名前で並んでいた。
                    head = entry.title
        sub = _format_subtitle(
            entry,
            show_posted=self._caption_show_posted,
            show_locked=self._caption_show_locked,
            show_size=self._caption_show_size,
            show_plan=self._caption_show_plan,
        )
        if parent:
            sub = f"{parent}{SUBTITLE_SEP}{sub}" if sub else parent
        if self._view_mode == "icon":
            return head + (f"\n{sub}" if sub else "")
        return head + (f"   {sub}" if sub else "")

    # --------------------------------------------------------- public API

    def set_root(
        self, root: Path, *, pending_select: Path | None = None,
    ) -> None:
        """Backwards-compatible alias for :meth:`set_folder`.

        Updates the path label + emits ``loading_changed(True)`` before
        delegating; ``_on_metadata_finished`` emits the matching ``False``
        once the post.md pass completes.  Clears the loader cache because
        thumbnails decoded for the previous root almost never get re-used
        — the left pane navigates ROOTS, not folder→folder peer moves.
        """
        self.breadcrumb.set_plain_text(t("viewer.post_grid.loading_root", root=root))
        # 離れる母集合がまだ生きているうちに、新しい現在地自身の postref を
        # 控える（ドリルインなら投稿フォルダは今の ``_entries`` に居る）。
        self._root_postref = self._folder_postref(root)
        self._entries = []
        # The pre-search selection belongs to the previous root; the new
        # root's own gets recorded once a selection lands there.
        self._presearch_selection = None
        # A re-root exits whichever overlay listing owns the grid (H01 の横断
        # 一覧 / 最近追加されたファイル): a drill-down / bookmark jump lands on
        # the new root's plain children.  The overlay IS remembered by the
        # history entry the window pushes for the position we're leaving, so
        # 「戻る」 re-enters it (UIレビュー 07-25 #58) — the window drives that,
        # not this teardown.  片付けは退場と**同じ 1 実装**を通る（この経路が
        # ``exit_*_view`` を通らないからと本体をインライン複製していたのが
        # レビュー 2026-09-03 項目 #97 の指摘そのもの — 奪っていた並び順を
        # 返し忘れると、ドリルイン先が star_desc のままになる）。
        self._teardown_overlay()
        # Stale recursive results from the previous root would mix in
        # under the new root's filter — cancel the in-flight walk and
        # drop the cached payload before the new scan kicks off.
        self._recursive_debounce.stop()
        self._recursive_scanner.cancel()
        self._recursive_results = None
        self._rel_paths.clear()
        # Advanced-search results are root-scoped too — drop them and re-scope
        # any persistent query to the new root (tags.db is keyed by absolute
        # path, so the prefix range follows the root automatically).  The
        # vector scanner must be cancelled alongside the tag scanner: its
        # signature carries no root, so an old-root ranked result landing
        # after this point would otherwise pass every guard and show the
        # previous root's hits on the new grid.
        self._advanced_search_cancel()
        self._advanced_search_drop_results()
        self._loader.clear_cache()
        super().set_folder(root, pending_select=pending_select)
        # If the checkbox is still on and a needle persists across the
        # root change, queue a fresh walk against the new root.
        self._maybe_start_recursive_scan()
        self._maybe_start_tag_scan()
        self._scan_loading = True
        self.loading_changed.emit(True)

    # Keep ``set_folder`` callable too for code that uses the base API —
    # forward to set_root so the path label and loading signal stay in sync.
    def set_folder(
        self, folder: Path | None, *, pending_select: Path | None = None,
    ) -> None:
        if folder is None:
            super().set_folder(None, pending_select=pending_select)
            return
        self.set_root(folder, pending_select=pending_select)

    def selected_folder(self) -> Path | None:
        tile = self._view.current_tile()
        if tile is None or not tile.is_dir:
            return None
        return tile.path

    def entry_for(self, path: Path) -> FolderEntry | None:
        """Return the currently loaded left-pane entry for *path*, or ``None``.

        Looks through the direct children first, then any active
        recursive-search and advanced-search (tag / semantic / media) result
        sets, then the cross-library curation list (H01) and the 「最近追加された
        ファイル」 listing — i.e. whatever populations the grid can currently be
        showing.

        到達不能行のプレースホルダ（``overlay.ghosts``）も母集合の一員として
        答える: 3 点セット（``metadata_loaded=True`` / ``has_post_md=False`` /
        ``thumbnail_resolved=True``）は「この行には触らない」を呼び出し側へ
        伝えるためのもので、ここで ``None`` を返すと窓側の post.md 読みスキップ
        条件が evaluate できず、読めないと分かっているパスへ read が飛ぶ。
        """
        key = str(path)
        for entry in self._entries:
            if str(entry.path) == key:
                return entry
        for results in (self._recursive_results, self._tag_results):
            if results:
                for entry, _rel in results:
                    if str(entry.path) == key:
                        return entry
        overlay = getattr(self, "_overlay", None)
        if overlay is not None:
            for entry in (*overlay.entries, *overlay.ghosts):
                if str(entry.path) == key:
                    return entry
        return None

    # ----------------------------------------------------- context menu

    def _on_context_menu(self, index: int, global_pos: QPoint) -> None:
        tile = self._view.tile_at(index)
        if tile is None:
            return
        menu = self._context_menu_for(tile)
        menu.exec(global_pos)
        # ``QMenu(self)`` is parented to this pane, so exec() only hides it —
        # without this the menu, its ★ submenu and its ~14 QActions pile up on
        # the pane once per right-click (same pattern as file_list/content_view;
        # this pane was the one place the sweep missed because the call spells
        # the position argument, so a bare ``.exec()`` search did not see it).
        menu.deleteLater()

    def _on_view_similar_requested(self, index: int) -> None:
        """Hover 「◇類似」 overlay clicked (item 2-4) — seed a similar search.

        Opens the AI search popover (so the 類似画像 segment + seed row are
        visible) and anchors the vector search to the clicked image tile via
        the public :meth:`set_similar_seed` entry point.
        """
        tile = self._view.tile_at(index)
        if tile is None or tile.is_dir:
            return
        self.open_ai_popover()
        self.set_similar_seed(tile.path)

    def _context_menu_for(self, tile) -> QMenu:
        """Build the left-pane context menu for a tile (folder or file).

        Every tile gets the pane-shared base set
        (:func:`context_menus.append_common_entry_actions` — 開く /
        エクスプローラ / フルパスをコピー / ファイルをコピー / 画像なら類似検索,
        matching the right pane; ``include_copy_file=True`` keeps 「ファイルを
        コピー」 available in both panes — L06), followed by the user-curation
        section (star / あとで見る
        / ユーザータグ — ``context_menus.VERBS`` の curation 節, suppressed when no
        ``user_meta`` store is wired).  No filesystem I/O happens here (menu
        build must stay NAS-free).
        """
        menu = QMenu(self)
        self.populate_entry_menu(menu, tile)
        return menu

    def populate_entry_menu(self, menu: QMenu, tile) -> None:
        """Fill *menu* with the entry actions for *tile* (右クリックの中身).

        Split out of :meth:`_context_menu_for` so the **menu bar's 編集 menu** can
        carry the same set for the current selection without a second, drifting
        copy of it (UIレビュー 2026-08-28 N-70 案B / N-157) — the review found the
        編集 menu holding a single item while ★ / あとで見る / ユーザータグ /
        コピー系 existed only under the right button.  Callers own the ``QMenu``;
        this only appends.  No filesystem I/O (menu build stays NAS-free).
        """
        ghost = self._is_curation_ghost(tile.path)
        # 同じ根を共有するゴーストが 2 件以上あるときだけ一括の口を渡す
        # （1 件なら行単位の張り替えが正しい手段）。判定はメモリ上の集合の
        # 純パス演算で、追加 I/O ゼロ。
        prefix_group = self._ghost_prefix_group(tile.path) if ghost else None
        similar_cb = None
        if self._vector_index is not None:
            # In-pane seed: this pane owns the similar search, so no signal
            # round-trip is needed (unlike the right pane's emit).  The
            # registry limits the entry to image files.
            similar_cb = lambda p=tile.path: self.set_similar_seed(p)  # noqa: E731
        # 共通ブロックは動詞レジストリ 1 表から（開く / コピー / 探す / 印 —
        # 4 席が同じ並び）。「最近追加」はフォルダのみ、印は店があるときだけ。
        # 実体に到達できなかった行 (#133 項目 3) は ``ghost`` 軸で表の側が
        # 落とす — 「開く / エクスプローラ / 印」は全部空振りするので出さず、
        # 張り替え導線と探す手掛かりだけが残る。「この一覧から外す」は
        # **確実に消えた行にだけ**渡す（口を渡さない = 表の条件が偽）。
        append_entry_verbs(
            menu,
            EntryMenuContext(
                path=tile.path, is_dir=bool(tile.is_dir), seat=SEAT_GRID,
                navigate=self.folder_activated.emit,
                reveal_in_app=self.reveal_in_app_requested.emit,
                similar_search=similar_cb,
                recent_files=self.enter_recent_files_view,
                curation=self.curation_hooks(), host=self,
                ghost=ghost,
                rebind=self._rebind_curation_ghost if ghost else None,
                rebind_prefix=(
                    self._rebind_curation_ghost_prefix
                    if prefix_group is not None
                    else None
                ),
                rebind_prefix_n=(
                    0 if prefix_group is None else len(prefix_group[1])
                ),
                ghost_remove=(
                    self._remove_curation_ghost
                    if ghost and self._is_removable_curation_ghost(tile.path)
                    else None
                ),
            ),
        )

    def _is_curation_ghost(self, path: Path) -> bool:
        """横断一覧のプレースホルダタイル（到達不能行 — #133 項目 3）か。"""
        overlay = self._overlay
        return overlay is not None and str(path) in overlay.ghost_keys

    def _is_removable_curation_ghost(self, path: Path) -> bool:
        """**確実に消えた**ゴースト（無確認の恒久削除を出してよい行）か。

        読めなかっただけの行（``unreadable``）は実体が生きている可能性がある
        ので偽。判定はメモリ上の集合参照 1 回だけ（追加 I/O ゼロ）。
        """
        overlay = self._overlay
        return overlay is not None and str(path) in overlay.ghost_missing_keys

    def _tile_warning(self, entry) -> str:
        """ゴーストタイルに警告バッジの kind を付ける（追加 I/O ゼロ）。

        判定は ``_is_curation_ghost`` = メモリ上の集合参照 1 回だけ。キャプション
        文言（「名前（見つかりません）」）だけが差だったので、図像としては未読込
        タイルと区別が付いていなかった。
        """
        return "ghost" if self._is_curation_ghost(entry.path) else ""

    def _aspect_source(self, entry: FolderEntry) -> Path | None:
        """ゴーストはアスペクト計測不能として扱う（#133 項目 3）。

        プレースホルダの 3 点セット（``build_ghost_entries``）はローダーの
        遅延 scandir と post.md 後追いを塞ぐが、**画像拡張子を持つファイル由来
        のゴースト**だけは基底の判定（is_dir=False + 画像拡張子 → entry.path
        自身）を素通りしてアスペクトプローブに載り、死んだパスがプール枠を
        OS タイムアウトぶん占有して実タイルのアスペクト解決を遅らせる。
        ここで ``None`` へ落として probe_specs から外す（既定アスペクトで
        描かれる — 実体に触らない行にはそれが正しい形）。
        """
        if self._is_curation_ghost(entry.path):
            return None
        return super()._aspect_source(entry)

    def _remove_curation_ghost(self, path: Path) -> None:
        """「この一覧から外す」— 今の一覧種別の印だけを外す (#133 R6)。

        外すのは**この一覧が About な軸だけ**（スター付き一覧なら★、
        あとで見る一覧なら L、タグ一覧ならそのタグ 1 つ）: 他の軸の表明は
        別の一覧にまだ属しているかもしれない。書き込みと失敗警告は単一書き手
        :meth:`_apply_curation` に乗る — ゴーストガードにより post.md へは
        触らない（指摘 1 が前提）。成功したらプレースホルダを片付けるために
        一覧を解決し直す（★編集の「一覧はスナップショット」規約の例外だが、
        この操作の意図そのものが「行を消すこと」なので消えるのが正しい）。
        """
        view = self._curation_list()
        if view is None:
            return
        meta = self._user_meta_map.get(str(path))
        write = grid_overlays.ghost_unmark(
            view, meta.tags if meta is not None else (),
        )
        if write is None:
            return
        if self._apply_curation(path, *write):
            self._refresh_curation_view()

    def _rebind_curation_ghost(self, old_path: Path) -> None:
        """「現在の場所を指定…」→ ``rebind_path`` → 一覧の再解決 (#133 項目 3)。

        ファイルダイアログ・店への書き込み・地図のパッチは
        ``curation_recovery`` 側の部品に委譲し、ここは配線だけ。成功したら
        3 面（グリッド / 右一覧 / 全画面）へ ``curation_changed`` を流してから
        一覧を解決し直す — 張り替わった行は次の resolve で実タイルとして
        戻ってくる。
        """
        if self._user_meta is None:
            return
        picked = prompt_current_location(self, old_path)
        if not picked:
            return
        # 行き先が既にキュレーション済みなら、無確認の不可逆併合にしない
        # （判定は :func:`grid_overlays.rebind_merge_check`）。
        merging = grid_overlays.rebind_merge_check(
            self._user_meta_map, str(old_path), picked,
        )
        if merging is not None:
            src_meta, dst_meta = merging
            if not confirm_rebind_merge(
                self, old_path, src_meta, Path(picked), dst_meta,
            ):
                return
        meta = rebind_and_patch(
            self._user_meta, self._user_meta_map, str(old_path), picked,
        )
        if meta is None:
            # 旧行が既に無い（別ウィンドウが消した等）か書き込み失敗 —
            # #53 と同じ控えめな失敗フィードバックに乗せる。
            self.show_curation_failed_feedback(old_path)
            return
        # タグ和集合で候補集合が変わり得る（``_apply_curation`` の tags 分岐と
        # 同じ理由でオートコンプリートのキャッシュを落とす）。
        self._user_tags_cache = None
        self.curation_changed.emit()
        self._refresh_curation_view()

    def _ghost_prefix_group(self, path: Path):
        """*path* と同じ根を共有するゴースト群（判定は
        :func:`grid_overlays.ghost_prefix_group`）。"""
        return grid_overlays.ghost_prefix_group(
            self._overlay, path, self.breadcrumb.library_bases(),
        )

    def _rebind_curation_ghost_prefix(self, old_path: Path) -> None:
        """「同じ場所にあった N 件をまとめて指定…」— 根ごとの一括張り替え。

        失敗の単位はボリューム（ドライブレターの付け替え・ライブラリごとの
        移動）なので、行単位の :meth:`_rebind_curation_ghost` だけでは数百回の
        モーダル往復になる。フォルダ選択 1 回 + 件数つき確認 1 回で、ストアの
        1 トランザクションへ渡す。

        動くのは**渡した行だけ**（根の下を走査して当たった行を巻き込まない）。
        着地後は母集合を読み直す — 行単位の経路が採るパッチ（``rebind_and_patch``）
        は 1 行ぶんの規約なので、束では ``refresh_user_meta``（リネーム追従の
        着地と同じ再読込）の方が「ストアと地図が別々の答えを持たない」を
        構造的に保証する。
        """
        if self._user_meta is None:
            return
        group = self._ghost_prefix_group(old_path)
        if group is None:
            return
        old_base, members = group
        picked = prompt_rebind_prefix(self, Path(old_base))
        if not picked:
            return
        # 確認の件数はストアではなくメモリ上の地図から数える（I/O ゼロ）。
        # 綴り算術は店が実際に使う rebase_spelling と同じ 1 実装なので、
        # 見せた件数と動く行がズレない。
        targets, merges = plan_prefix_rebind(
            members, old_base, picked, self._user_meta_map,
        )
        if not targets:
            return
        if not confirm_prefix_rebind(
            self, Path(old_base), Path(picked), len(targets), merges,
        ):
            return
        moved = self._user_meta.rebind_prefix(old_base, picked, targets)
        if not moved:
            # 旧行が既に無い（別ウィンドウが消した等）か書き込み失敗 —
            # 行単位の経路と同じ控えめな失敗フィードバックに乗せる。
            self.show_curation_failed_feedback(old_path)
            return
        self.refresh_user_meta()
        self.curation_changed.emit()
        self._refresh_curation_view()

    def current_tile(self):
        """The selected tile (``key`` / ``path`` / ``is_dir``) or ``None``.

        ``current_path()`` alone cannot answer 「フォルダかファイルか」 without a
        ``stat``, and menu building must stay NAS-free — the tile already carries
        the answer from the scan.  Used by the window's 編集 menu (N-70 案B).
        """
        return self._view.current_tile()

    def curation_hooks(self) -> CurationHooks | None:
        """この席の印の口。店が無ければ ``None`` = メニューに印の節が出ない。

        右クリック・編集メニューが同じ口を使う。プレビュー列 / 全画面には
        ウィンドウが同じ 2 関数を注入する（``ContentView.set_curation_hooks`` /
        ``LightboxWindow.set_curation_provider``）。
        """
        if self._user_meta is None:
            return None
        return CurationHooks(
            provider=self._curation_badge_for, request=self.request_curation,
        )

    def request_curation(
        self,
        path: Path,
        kind: str,
        value: object,
        *,
        notify: bool = True,
        parent: QWidget | None = None,
    ) -> bool:
        """席を問わない印の要求の funnel — 書き込み + 確認フィードバック。

        右クリック（4 席）/ 編集メニュー / ``L`` / 0-5 / 右一覧・全画面からの
        転送がすべてここを通るので、「右クリックだけトーストが出ない」
        （UIレビュー 2026-09-11 N-146）や「席によって対象名の告知が無い」が
        起こせない。書き込みは :meth:`_apply_curation` の単一チョークポイントの
        まま（失敗の警告も向こうが出す — #53）。``notify=False`` は全画面用
        （自前の中央オーバーレイで告知する）。``kind="edit_tags"`` は編集
        ダイアログを開く（完了トーストはダイアログ側。*parent* を渡すとその窓の
        上に出す — 全画面は別トップレベルなので、左ペイン親のままだと裏に出る）。
        戻り値は永続化の成否。
        """
        if self._user_meta is None:
            return False
        # post.md（投稿本文）は投稿フォルダの印に読み替える — 右一覧の post.md 行で
        # 0-5 / 右クリックを撃っても、表示（ストリップ）と同じ対象へ書く（N-51）。
        # 書き込みの funnel はここ 1 本なので、経路ごとの読み替え漏れが起きない。
        path = curation_subject(path)
        if kind == "edit_tags":
            self._edit_user_tags(path, parent=parent)
            return True
        ok = self._apply_curation(path, kind, value)
        if ok and notify:
            if kind == "star":
                self.show_star_feedback(path, int(value or 0))
            elif kind == "later":
                self.show_later_feedback(path, bool(value))
        return ok

    def _edit_user_tags(self, path: Path, parent: QWidget | None = None) -> None:
        """Open a small dialog to edit *path*'s user tags (comma/space delimited).

        親付き (``QInputDialog(parent or self)`` — 全画面から開くときは全画面窓)
        なので C++ 側の所有権はその親にあり、
        ``exec()`` を抜けてローカル参照が消えてもダイアログは**非表示の子
        ウィジェットとして生き残る**。この導線は画像ごとに何度でも開けるので、
        ``deleteLater`` が無いと 1 セッションで際限なく積み上がる（issue #125
        と同型 — あちらはモデル取得の提案なので 1 インストール 1〜2 回だが、
        こちらは桁が違う）。``try``/``finally`` で必ず破棄を予約する。

        破棄は ``deleteLater``（``WA_DeleteOnClose`` ではない）: #111 の
        「シグナル発火スタックの内側で C++ オブジェクトを解放する」形を
        作らないため。``exec()`` は既に戻っており、以降このスコープは ``dlg``
        に触れない（``textValue()`` は finally より前に読む）。子の
        ``_UserTagCompleter`` は ``line`` の子なのでダイアログごと畳まれる。
        """
        from PySide6.QtWidgets import QInputDialog, QLineEdit as _QLineEdit

        meta = self._user_meta_map.get(str(path))
        current = _TAG_INPUT_SEP.join(meta.tags) if meta is not None else ""
        dlg = QInputDialog(parent if parent is not None else self)
        dlg.setWindowTitle(t("viewer.post_grid.edit_user_tags_title"))
        dlg.setLabelText(t("viewer.post_grid.edit_user_tags_label"))
        dlg.setTextValue(current)
        dlg.setInputMode(QInputDialog.TextInput)
        # OK / キャンセルをカタログ文言へ（N-01）— この画面だけ Qt 既定の
        # 語彙になるのを防ぐ。``dialogs.prompt_text`` を使わないのは、この
        # ダイアログだけ QLineEdit へ補完を付けるため。
        localize_input_dialog(dlg)
        # Best-effort autocomplete from existing user tags — per *token*, not
        # per whole line (UIレビュー 2026-08-28 N-69).
        line = dlg.findChild(_QLineEdit)
        existing = self.all_user_tags()
        if line is not None and existing:
            completer = _UserTagCompleter(existing, line)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            line.setCompleter(completer)
        if line is not None and current:
            # UIレビュー 2026-09-11 N-37: ``QInputDialog`` は show 時に既存値を
            # **全選択**するので、開いた直後の 1 打鍵が既存タグを丸ごと消して
            # いた。キュレーションは再生成できず取り消し導線も無いので、
            # 追記が既定になるようカーソルを末尾へ置き直す。show 時の全選択を
            # 上書きする必要があるため ``exec()`` の前に ``singleShot(0)`` で
            # 予約する（直接呼ぶと show 側に戻される）。
            # ラムダが捕捉するのは ``line`` だけ — ダイアログ自身を捕捉させる
            # と #111 と同型の寿命の罠になる。末尾に区切りを足しておくと
            # 追記がそのまま自然な入力になる（``split_user_tags`` は末尾の
            # 区切りを無視するので確定値は変わらない）。区切りは
            # ``split_user_tags`` が実際に割る文字（``[\\s,]``）でなければ
            # ならないので、上の ``current`` を組むのと同じ ``_TAG_INPUT_SEP``
            # を使う。
            line.setText(current + _TAG_INPUT_SEP)
            QTimer.singleShot(0, lambda ln=line: _park_cursor_at_end(ln))
        try:
            if dlg.exec() != QInputDialog.Accepted:
                return
            text = dlg.textValue()
        finally:
            dlg.deleteLater()
        from .user_meta import split_user_tags

        tags = split_user_tags(text)
        # 保存に失敗したら確定トーストは出さない (#53)。候補の詰め直しは
        # どちらでも安全（失敗時はストアの現状がそのまま読み直される）。
        if self._apply_curation(path, "tags", tags):
            self.show_user_tags_feedback(path, tags)
        # 候補は編集で増える — 次にフィルタを開いたときに新しいタグが選べるよう
        # 絞り込みコンボを詰め直す (UIレビュー 07-25 #13②)。
        self._reload_user_tag_choices()

    def show_user_tags_feedback(self, path: Path, tags: list[str]) -> None:
        """ユーザータグ確定のトースト（UIレビュー 07-25 #13①）。

        従来は OK を押しても画面が一切変わらず（バッジも無い次元なので）、
        保存されたのかどうかが分からなかった。★の
        :meth:`show_star_feedback` と同じ funnel の作法で、対象名と確定値を
        必ず添える。連打では前のトーストを差し替える。
        """
        name = self._star_target_label(path)
        self._curation_toast(
            t(
                "viewer.post_grid.user_tags_set_toast",
                tags=t("common.sep.comma").join(tags),
                name=name,
            )
            if tags
            else t("viewer.post_grid.user_tags_cleared_toast", name=name)
        )

    # ----------------------------------------------------- user curation

    def _curation_badge_for(self, path: Path) -> tuple[int, bool]:
        """Paint-time ``(star, later)`` lookup for a tile (in-memory, no I/O)."""
        meta = self._user_meta_map.get(str(path))
        if meta is None:
            return 0, False
        return meta.star, meta.later

    def _curation_filter_for(self, path: Path) -> tuple[int, tuple, bool]:
        """``(star, tags, later)`` for the filter engine (``star:``/``mytags:``/``later:``)."""
        meta = self._user_meta_map.get(str(path))
        if meta is None:
            return 0, (), False
        return meta.star, meta.tags, meta.later

    def _star_of(self, path: Path) -> int:
        """User star (0 when un-curated) for the ``star_desc`` sort key."""
        meta = self._user_meta_map.get(str(path))
        return meta.star if meta is not None else 0

    def curation_tooltip_lines(self, path: Path) -> list[str]:
        """Hover-tooltip lines describing *path*'s curation (in-memory, no I/O).

        UIレビュー 07-25 #136: ♡N / 🔒N は :meth:`ChildrenGrid._tooltip_for` で
        言葉に展開されるのに、★ と 「あとで見る」 は絵のままだった。さらに
        ユーザータグ (#13) はバッジすら無く、付けても製品のどこにも出て
        こなかった — この 3 行がその最初の表示面になる。

        Public because the right pane's file list shows the same badges from the
        same map and needs the same words (the map itself stays owned here).
        Returns ``[]`` when nothing is curated (no tooltip noise on plain tiles).
        """
        lines: list[str] = []
        if self._is_curation_ghost(path):
            # UIレビュー 2026-09-11 N-56: 張り替え（「現在の場所を指定…」）は
            # 右クリックの奥にしか無く、画面上に手掛かりがゼロだった。印の行の
            # **前**に手順を置く（``_is_curation_ghost`` は overlay の集合参照
            # だけなので、この関数の「同期のインメモリ参照のみ」契約は保つ）。
            lines.append(t("viewer.post_grid.curation_ghost_tooltip_hint"))
        meta = self._user_meta_map.get(str(path))
        if meta is None:
            return lines
        if meta.star:
            lines.append(t("viewer.common.tooltip_star", n=meta.star))
        if meta.later:
            lines.append(t("viewer.common.tooltip_later"))
        if meta.tags:
            lines.append(
                t(
                    "viewer.common.tooltip_user_tags",
                    tags=t("common.sep.comma").join(meta.tags),
                )
            )
        return lines

    def _postref_columns(self, path: Path) -> tuple:
        """Resolve the ``(service, post_id, rel_name)`` postref columns for *path*.

        * A post folder → its own ``(service, post_id, None)``.
        * A file inside a post folder → the parent post's ``(service, post_id)``
          plus the file's name relative to that folder.
        * 現在地そのものが投稿フォルダで、その直下のファイル → 現在地の
          ``(service, post_id)``（``set_root`` が控えた ``_root_postref`` —
          中に立っている投稿フォルダは ``entry_for`` の母集合に居ない）。
        * 解決できないもの（``post.md`` を持たない / まだメタデータを読んで
          いない / 母集合に居ない = ゴースト）→ ``(None, None, None)``。行は
          パスで保存されるだけで rename 追従が付かない
          （``UserMetaStore._merge`` は ``None`` を「既存 postref の保持」と
          解釈するので、既に付いている追従は失われない）。

        **純メモリ参照**であること: 印の書き込みは右クリック / 数字キー /
        タグ編集など高頻度の経路から GUI スレッドで呼ばれるので、ここで
        ``post.md`` を開くと到達不能な共有で窓が SMB タイムアウトぶん固まる。
        値はスキャンのメタデータパスが :class:`~.folder_scan.FolderEntry`
        へ載せている（キャッシュヒットでも同じ値が再現される）。
        """
        entry = self.entry_for(path)
        if entry is not None and entry.is_dir:
            return (entry.service or None), (entry.post_id or None), None
        # A file: its parent may be a post folder.
        parent = self.entry_for(path.parent)
        if parent is not None and parent.service and parent.post_id:
            return parent.service, parent.post_id, path.name
        # 投稿フォルダ**の中に立って**そのファイルへ印を付ける局面（平常
        # ブラウズで最も普通の配置）では、親は現在地なので母集合には居ない。
        # ``set_root`` が控えた現在地自身の postref がその席。
        root = self._root_or_folder
        if (
            root is not None
            and self._root_postref is not None
            and path.parent == root
        ):
            service, post_id = self._root_postref
            return service, post_id, path.name
        return None, None, None

    def _folder_postref(self, folder: Path) -> tuple[str, str] | None:
        """*folder* 自身の ``(service, post_id)``（投稿フォルダで無ければ ``None``）。

        席は 2 つで、どちらも ``post.md`` を開かない:

        * 今ロードされている母集合の :class:`~.folder_scan.FolderEntry`
          （ドリルイン直前ならドリル先はまだここに居る）。
        * フォルダプレビューキャッシュ（ローカル sqlite）の恒久列。``--root``
          直開き / ブックマーク / 履歴戻りのように母集合を経由しない入場でも、
          一度解決済みのフォルダならここで答えが出る。``mtime`` を問わない点
          読みにしているのは、``(service, post_id)`` がそのフォルダの**同一性**
          であって内容ではないため（子が増減しても投稿は同じ投稿）。
        """
        entry = self.entry_for(folder)
        if entry is not None and entry.is_dir and entry.service and entry.post_id:
            return entry.service, entry.post_id
        cache = self._folder_cache
        if cache is None:
            return None
        try:
            return cache.get_postref(folder)
        except Exception:  # pragma: no cover (cache best-effort)
            return None

    def _backfill_postrefs(self, enriched: list) -> None:
        """メタが着地した投稿の postref を、既存の印の行へ後から埋める。

        :meth:`_postref_columns` は純メモリ参照なので、メタデータ着地**前**に
        打った印は ``(None, None, None)`` で保存される（冷えた共有では一覧の
        表示とメタ着地の間が秒単位空く）。``UserMetaStore._merge`` は ``None``
        を「既存 postref の保持」と読むため、そのままでは行は postref を持て
        ないまま = リネーム追従の対象から外れたままになる。★は唯一の再生成
        不能データなので、値が分かった時点でここが埋める。

        呼ばれるのはメタデータのバッチ着地ごと（GUI スレッド）なので、穴の
        無いライブラリでは SQL を 1 本も撃たないこと — 帳簿は
        :meth:`UserMetaStore.fill_postrefs` 側がメモリに持つ。
        """
        store = self._user_meta
        if store is None:
            return
        refs = [
            (entry.path, entry.service, entry.post_id)
            for entry in enriched
            if entry.is_dir and entry.service and entry.post_id
        ]
        if not refs:
            return
        # 埋めるのは postref 列だけ（★ / タグ / later は不変）なので、画面上の
        # 見た目は変わらない — 母集合の再読み込みは要らない。
        store.fill_postrefs(refs)

    def _apply_curation(self, path: Path, kind: str, value) -> bool:
        """Persist a curation change for *path* and patch the in-memory map.

        *kind* is ``"star"`` / ``"later"`` / ``"tags"``.  Resolves the postref
        columns (rename tracking) once, writes through the store (immediate
        commit), updates ``_user_meta_map`` so paint / filter / sort see it now,
        and repaints.  A rebuild is triggered only when a curation-driven filter
        or the star sort is active (so a bare star toggle doesn't reshuffle).

        Returns **whether the change actually reached disk** (#53).  The
        ``*_checked`` store variants report a read-only volume / disk-full /
        locked DB as ``ok=False``; this is the **single writer**, so the failure
        warning is raised here — every entry point (右クリックメニュー / 数字キー /
        タグ編集ダイアログ / 情報パネル / プレビュー面) inherits it without its
        own error branch, and callers only have to skip their *success* toast.
        On failure the in-memory map is left alone: nothing changed on disk, so
        the badges must keep showing the last persisted state rather than the
        value we failed to write.
        """
        if self._user_meta is None:
            return False
        # ゴースト（到達不能行）も特例を持たない: ``entry_for`` が答える
        # ゴースト行は ``service`` / ``post_id`` が空なので ``(None, None,
        # None)`` へ自然に畳まれ、``_merge`` がそれを「既存 postref の保持」
        # と解釈して rename-tracking を守る。
        service, post_id, rel_name = self._postref_columns(path)
        if kind == "star":
            outcome = self._user_meta.set_star_checked(
                path, value, service=service, post_id=post_id, rel_name=rel_name,
            )
        elif kind == "later":
            outcome = self._user_meta.set_later_checked(
                path, value, service=service, post_id=post_id, rel_name=rel_name,
            )
        else:  # tags
            outcome = self._user_meta.set_tags_checked(
                path, value, service=service, post_id=post_id, rel_name=rel_name,
            )
            # 候補集合が変わり得る唯一の書き込み — キャッシュを落とす
            # (UIレビュー07-25 追修)。
            self._user_tags_cache = None
        if not outcome.ok:
            self.show_curation_failed_feedback(path)
            return False
        meta = outcome.meta
        # Mirror the store's own display spelling, not the tile's.  The store
        # writes ``path`` through ``absolute_spelling`` (#133 key contract), and
        # ``CurationMap.__setitem__`` *replaces* the existing key when a new
        # spelling normalises onto an already-indexed entry — so patching the
        # map with the tile's raw spelling (relative whenever ``--root`` is)
        # would swap the absolute key ``load_all`` built for a relative one, and
        # the cross-library 「スター付き一覧」 (which ``stat``s these keys, and
        # shows their basename as the caption) would start disagreeing with the
        # DB until the next restart.
        from .user_meta import absolute_spelling

        key = absolute_spelling(path)
        if meta.is_empty():
            self._user_meta_map.pop(key, None)
        else:
            self._user_meta_map[key] = meta
        # Curation-dependent view state may need a rebuild; otherwise just
        # repaint the affected tiles (cheap).
        #
        # UIレビュー 2026-08-28 N-67: 第 1 項に ``_curation_view is None`` が要る。
        # 横断一覧は入場時に ``star_desc`` を**強制**する (``_CURATION_VIEW_SORT``)
        # ので、一覧の中では第 1 項が常に真になり、★を 1 つ変えるたびに再ソート
        # されてカーソル下のタイルが動いていた。第 2 項
        # (:meth:`_curation_filter_active`) は「一覧はスナップショット」という
        # 同じ不変条件を内側のガードで既に守っている — 同じ判定式の中で片方だけが
        # 破っていた形なので、ここで揃える。
        if (
            self._curation_list() is None and self._sort_mode == "star_desc"
        ) or self._curation_filter_active():
            self._preserve_selection_for_rebuild()
            self._rebuild_grid()
        else:
            self._view.viewport().update()
        self.curation_changed.emit()
        return True

    def _curation_filter_active(self) -> bool:
        """Whether a curation-driven restriction is narrowing the grid.

        Gates the rebuild in :meth:`_apply_curation`: when a star / 「あとで見る」
        edit can change *membership*, the grid must be rebuilt, not just
        repainted.  Two sources count:

        * the filter box (``star:`` / ``mytags:`` / ``later:`` terms), and
        * the フィルタ popover's ★ floor / 「あとで見る」 controls — missing until
          UIレビュー 07-25 #61: dropping a folder below the ★ floor left it on
          screen until some unrelated rebuild, so the same restriction behaved
          differently depending on whether it was typed or clicked.

        The cross-library views (スター付き一覧 / あとで見る一覧) are deliberately
        NOT included: there the whole population IS the curation flag, so
        re-rating the item under the cursor would make it vanish mid-keystroke
        (0→3→4 becomes impossible).  Those lists stay a snapshot until re-entered.
        その除外は**打った条件にも押した条件にも等しく効く** — 検索欄の
        ``star:`` / ``mytags:`` / ``later:`` だけがガードの外に居たころは、
        同じ制限が「打ったか押したか」で消える / 消えないに割れていた。
        """
        from .filter_query import _CURATION_FIELDS

        if self._curation_list() is not None:
            return False
        if (
            self._filterbar_star_min > 0
            or self._filterbar_later
            or self._filterbar_user_tag
        ):
            return True
        return any(
            t.field in _CURATION_FIELDS
            for t in _parse_filter_query(self._filter_text)
        )

    def _on_star_key(self, star: int) -> None:
        """Digit 0–5 pressed with a tile selected — set / clear its star."""
        tile = self._view.current_tile()
        if tile is None:
            return
        # 保存できなかったときは成功トーストを出さない (#53) — 判定は funnel
        # :meth:`request_curation` が持つ（失敗の警告は単一書き手側）。
        self.request_curation(tile.path, "star", star)

    def _star_target_label(self, path: Path) -> str:
        """スタートーストに添える対象名（UIレビュー 07-25 #11）。

        グリッドに載っている対象なら**タイルの見出し**（投稿タイトル等 —
        キャプションの 1 行目。2 行目以降は日付・サイズの副題）を使い、
        載っていなければファイル名へ落とす。純粋な参照のみで I/O は無い。
        """
        idx = self._view.index_of_key(self._key_prefix + str(path))
        if idx is not None:
            tile = self._view.tile_at(idx)
            caption = getattr(tile, "caption", "") if tile is not None else ""
            first = caption.split("\n", 1)[0].strip() if caption else ""
            if first:
                return first
        return path.name or str(path)

    def show_star_feedback(self, path: Path, star: int) -> None:
        """0-5 スター確定の**共通トースト funnel**（UIレビュー 07-25 #11/#12）。

        ライトボックスの「★★★」オーバーレイと同水準の確認フィードバック
        (07-13 #20) — 特に「0 = 解除」はバッジが消えるだけで無言だった。
        グリッドの数字キー（``_on_star_key``）とプレビュー面の 0-5
        （``main_window._set_current_star`` — 分割・最大化とも）が同じここを
        通るので、3 面で挙動が揃う（#12）。

        文言には**必ず対象名を添える**（#11）: 分割ビューではフォーカスが
        グリッドかプレビューかで対象が投稿フォルダ / 代表画像ファイルに黙って
        切り替わるため、「どちらに付いたか」はトーストでしか分からない。

        連打では前のトーストを差し替える（積み上げない）。
        """
        name = self._star_target_label(path)
        self._curation_toast(
            t("viewer.post_grid.star_set_toast", stars="★" * star, name=name)
            if star
            else t("viewer.post_grid.star_cleared_toast", name=name)
        )

    def show_later_feedback(self, path: Path, later: bool) -> None:
        """「あとで見る」確定のトースト（UIレビュー 2026-08-28 N-74）。

        ★ (:meth:`show_star_feedback`) / ユーザータグ
        (:meth:`show_user_tags_feedback`) と同じ funnel の作法 — 対象名を必ず
        添え、連打では前のトーストを差し替える。キー (``L``) で撃てるように
        なった以上、右クリックのチェックマークが唯一の確認手段では足りない
        （メニューを閉じた状態で撃つと画面が変わったように見えないため）。
        """
        self._curation_toast(
            t(
                "viewer.post_grid.later_set_toast" if later
                else "viewer.post_grid.later_cleared_toast",
                name=self._star_target_label(path),
            )
        )

    def show_curation_failed_feedback(self, path: Path) -> None:
        """書き込みが永続化できなかったときの控えめな警告 (#53)。

        ★ / ユーザータグの成功トーストと**同じスロット**（``_star_toast``）を
        使って前のフィードバックを差し替える — 「★★★」の直後に失敗が積み
        上がって両方見える、という矛盾した見え方を作らない。成功より長く
        （既定 3 秒）残すのは、これが「操作が効かなかった」という見落として
        はいけない知らせだから。
        """
        self._curation_toast(
            t(
                "viewer.post_grid.curation_write_failed_toast",
                name=self._star_target_label(path),
            ),
            kind="warning",
            duration_ms=_CURATION_FAIL_TOAST_MS,
        )

    def _curation_toast(
        self,
        message: str,
        *,
        kind: str = "success",
        duration_ms: int = _CURATION_TOAST_MS,
    ) -> None:
        """印のフィードバックを出す唯一の口（差し替え規約はここだけが持つ）。

        ★ / あとで見る / ユーザータグ / 失敗警告の 4 funnel が同じブロック
        （前のトーストを ``RuntimeError`` 付きで畳む → 同じスロットへ出す）を
        手書き複製していたので、1 か所で直した修正が他へ届かなかった。
        連打で積み上げないために、成功も失敗も同じスロットを使う。
        """
        if self._star_toast is not None:
            try:
                self._star_toast.dismiss()
            except RuntimeError:
                pass  # ウィンドウ破棄などで C++ 側が先に消えた
        self._star_toast = show_toast(
            self.window(), message, kind=kind, duration_ms=duration_ms
        )

    def all_user_tags(self) -> list[str]:
        """Distinct user tags across the store (edit-dialog autocomplete).

        UIレビュー07-25 追修: 値は ``user_meta.db`` の全行走査で、フィルタ
        ポップオーバーを開くたび (:meth:`_reload_user_tag_choices`) と
        履歴の 1 歩ごと (:meth:`restore_search_state`) に GUI スレッドで
        走っていた。結果を保持し、タグが変わり得る 2 か所
        (:meth:`_apply_curation` の ``tags`` / :meth:`refresh_user_meta`) だけ
        無効化する。
        """
        if self._user_meta is None:
            return []
        if self._user_tags_cache is None:
            self._user_tags_cache = self._user_meta.all_tags()
        return list(self._user_tags_cache)

    def user_meta_for(self, path: Path):
        """Return the ``UserMeta`` for *path* (or ``None``)."""
        return self._user_meta_map.get(str(path))

    def refresh_user_meta(self) -> None:
        """Reload the in-memory curation map (e.g. after a rename-following pass)."""
        if self._user_meta is None:
            return
        # ストアを読み直す = 外で書かれたタグも入り得る (UIレビュー07-25 追修)。
        self._user_tags_cache = None
        self._user_meta_map = self._user_meta.load_all()
        self._view.viewport().update()

    # ------------------------------------------ cross-library curation list (H01)

    #: ユーザータグ横断一覧の *kind* 接頭辞 (N-71) — 実体は
    #: :data:`~.curation_list.TAG_PREFIX`（種別の意味論はそちらが単一情報源）。
    CURATION_TAG_PREFIX = TAG_PREFIX

    #: ``"tag:お気に入り"`` → ``"お気に入り"``（それ以外は ``None``）。窓・
    #: ダイアログ側が文字列 kind のまま問い合わせる公開口。
    curation_tag_of = staticmethod(CurationList.tag_of)

    def _curation_pool_paths(self, kind: str) -> list[str] | None:
        """*kind* の母集合パス（判定は :func:`grid_overlays.curation_pool_paths`）。

        未知の *kind* は ``None`` — 入場ガードと件数表示
        (:meth:`curation_pool_count`) が同じ 1 実装を共有する。
        """
        return grid_overlays.curation_pool_paths(self._user_meta_map, kind)

    def curation_pool_count(self, kind: str) -> int:
        """「印を付けた件数」— レール行ラベルの実件数 (N-117)。

        ``_user_meta_map`` のメモリ走査だけ（``enter_curation_view`` が母集合を
        作るのと**同じ 1 実装**）なので I/O はゼロ。実際に一覧へ並ぶ件数とは
        ``resolve_curation_paths`` が落とす分（消えた / 読めなかった）だけずれ
        得るため、ラベルの意味は「印を付けた件数」であって「表示される件数」
        ではない（N-09 の ``curation_error`` と整合させるための約束）。
        """
        if self._user_meta is None:
            return 0
        return len(self._curation_pool_paths(kind) or ())

    # ---------------------------------------- 全面占有一覧の共通ライフサイクル

    def _teardown_overlay(self) -> None:
        """占有一覧を畳む**唯一の**実装（入場の乗り換え / 退場 / 再ルート）。

        乗り換えは異種（横断 ⇄ 最近追加）も同種（スター付き → あとで見る /
        別フォルダの最近追加）もここを通る — ``saved_sort`` を自分で読み替える
        第 2 経路を作らない。解決 / 走査の中止・母集合の破棄・奪っていた並び順
        の返却は :meth:`OverlayList.teardown` の中に対で入っている。
        """
        overlay = self._overlay
        if overlay is None:
            return
        self._overlay = None
        self._set_sort_silently(overlay.teardown())
        # 可視タイル限定の post.md 後追い解決も一緒に落とす（受付済み集合と
        # 80ms デバウンス — 一覧が消えた後に走らせない）。
        self._reset_curation_metadata()

    def _exit_overlay(self) -> None:
        """占有一覧から平常グリッドへ戻る（Esc / チップ× / 「すべて解除」）。

        単一クラム (#60) を解除して実フォルダの道筋へ戻す。件数は続く
        ``_rebuild_grid`` が入れ直す。ルート未設定（一覧から入って一度も
        フォルダを開いていない）なら道筋そのものが無いので空表示へ落とす —
        一覧の名前を残すと退場後も現在地を偽り続ける。
        """
        if self._overlay is None:
            return
        self._teardown_overlay()
        self._set_search_status("")
        if self._root_or_folder is not None:
            self.breadcrumb.set_path(self._root_or_folder)
        else:
            self.breadcrumb.set_plain_text("")
        self._rebuild_grid()

    def _enter_overlay(
        self,
        session: grid_overlays.OverlaySession,
        cancel: Callable[[], None],
    ) -> OverlayList:
        """*session* の一覧を立ち上げる（2 つの入場口の共通尾部）。

        並び順と単一クラムはどちらも一覧の台帳から引く — 軸や一覧の種類が
        増えても導出点は :mod:`.overlay_list` の 1 か所だけ。畳むのは呼び出し
        側の仕事（畳むと入場前の並び順が戻り、それが *session* の退避値）。
        """
        overlay = self._overlay = session.open_list(cancel)
        self._set_sort_silently(overlay.sort_axis())
        self.breadcrumb.set_virtual_crumb(overlay.crumb())
        return overlay

    def enter_curation_view(self, kind: str) -> None:
        """Show the whole library's 「あとで見る」 / 「スター付き」 entries flat (H01).

        *kind* is ``"later"``, ``"starred"``, or ``"tag:<ユーザータグ>"``.  母集合
        はメモリ上の ``_user_meta_map``（単一の真実源 — このセッションで付けた
        ★も映る）から :class:`grid_overlays.OverlaySession` が組み、実タイルへの
        解決は GUI スレッドの外（1 パスが冷えた NAS では ``os.stat`` 1 回ぶん
        塞ぐ）。効いている検索は先に畳んで一覧を唯一の母集合にする。

        入場で一覧が自分について本当のことを言うように、並び順は一覧が *about*
        にしている軸へ、パンくずは単一クラムへ切り替える（どちらも
        :meth:`_enter_overlay` が一覧の台帳から引く）。ドリルインは一覧を出る
        が、窓が履歴項目に一覧を記録するので「戻る」で戻ってこられる
        (:meth:`current_curation_view`)。
        """
        if self._user_meta is None:
            return
        # 母集合はここで 1 度だけ組む（入場ガード = 「組めなければ入場しない」）。
        session = grid_overlays.OverlaySession.for_curation(
            kind, self._user_meta_map,
        )
        if session is None:
            return
        # 入れ替えが確定した時点で告げる（不発の早期 return では告げない）。
        self.population_replacing.emit()
        # 「最近追加されたファイル」 owns the whole grid too — the two overlays are
        # mutually exclusive, so entering this one leaves that.
        self.exit_recent_files_view()
        # Clear any engaged search so the curation list is the only population.
        self.clear_search_state()
        # 乗り換え（同種どうしを含む）も退場と同じ 1 実装で畳む — 畳むと
        # 入場前の並び順が戻るので、そのまま次の一覧の退避値になる
        # （``prev.saved_sort`` を読む第 2 経路を作らない）。
        self._teardown_overlay()
        # 退避する並び順は畳んだ**後**の値（畳むと入場前の並び順が戻るので、
        # それがそのまま次の一覧の退避値になる）。
        self._enter_overlay(
            replace(session, saved_sort=self._sort_mode),
            self._curation_stream.cancel,
        )
        # Show the mode immediately (empty grid + banner) while the stat pass
        # runs; the landed entries fill in via ``_on_curation_entries``.
        self._refresh_curation_view()

    def _refresh_curation_view(self) -> None:
        """今の一覧種別で母集合を組み直し、off-thread の解決を掛け直す。

        入場 (:meth:`enter_curation_view`) と、張り替え後の再解決
        (:meth:`_rebind_curation_ghost`) の共通尾部。検索状態・並び順・パンくず
        には触れない — それらを整えるのは入場だけの仕事で、張り替えの再解決が
        ユーザーの載せた絞り込みを巻き添えにしない。
        """
        overlay = self._overlay
        view = None if overlay is None else overlay.curation
        if view is None or overlay is None or self._user_meta is None:
            return
        # 母集合は入場と同じ 1 実装（:class:`grid_overlays.OverlaySession`）で
        # 組み直す — 張り替えの再解決も「いまの地図」から同じ規則で引く。
        session = grid_overlays.OverlaySession.for_curation(
            view.key, self._user_meta_map,
        )
        if session is None or session.pool is None:
            return
        paths = session.pool
        overlay.reset_population()
        overlay.pending = True
        self._reset_curation_metadata()
        self._set_overlay_status(t("viewer.post_grid.curation_loading"))
        from .user_meta import resolve_curation_paths

        # タイル説明はライブラリ基準の相対パス (N-49) — 基準はパンくずが持つ
        # 唯一の情報源をそのまま渡す（post_grid 側に複製の状態を作らない）。
        bases = self.breadcrumb.library_bases()
        # 協調キャンセル: 退場 / 再入場 / 窓じまいの cancel が per-path の stat
        # ループを刻みで止める（項目 #215 追補 — 死んだ共有では 1 stat が
        # 15〜195 秒塞ぐので、有界ドレインだけでは残りの行を撫で続ける）。
        self._curation_stream.submit_job(
            lambda job, ps=list(paths), bs=bases: resolve_curation_paths(
                ps, bs, should_cancel=job.cancel.is_cancelled
            )
        )
        self._rebuild_grid()

    def _on_curation_entries(self, payload: object) -> None:
        """Landed off-thread resolution of the curation list (H01).

        着地の読み替え（読み取り失敗を「まだありません」と言わない分岐・ゴースト
        の組み立て・件数行）は :func:`grid_overlays.curation_landing` の 1 実装。
        ここは一覧がまだ居ることを確かめて結果を配るだけ。
        """
        overlay = self._overlay
        if overlay is None or overlay.curation is None:
            return  # the view left the curation listing while this resolved
        self._set_overlay_status(
            grid_overlays.apply_landing(
                overlay,
                grid_overlays.curation_landing(
                    payload, self.breadcrumb.library_bases(),
                ),
            )
        )
        self._rebuild_grid()
        # 可視タイルの post.md を後追いで解決する (N-49 後半)。``_rebuild_grid``
        # 経由の再レイアウトでも ``visible_range_changed`` は飛ぶが、暖レイアウト
        # （同じ幾何）では飛ばないことがあるので、着地時は明示的に一度蹴る。
        self._schedule_curation_meta()

    def _curation_status_line(self) -> str:
        """一覧の下に出す件数行（組み立ては
        :func:`grid_overlays.curation_status_line`）。"""
        overlay = self._overlay
        if overlay is None:
            return ""
        return grid_overlays.curation_status_line(
            failed=overlay.failed,
            missing=overlay.missing,
            unreadable=overlay.unreadable,
        )

    # ---- 横断一覧の可視タイル限定 post.md 後追い解決 (N-49 後半) ----------

    #: 1 tick に投げる最大件数。ビューポート 1 画面分を超えて先読みしない
    #: （サムネ要求と同じ「ビューポート限定」の規律 — docs/claude/viewer/grid.md）。
    _CURATION_META_BATCH = 24

    def _reset_curation_metadata(self) -> None:
        """後追い解決の受付状態を捨てる（入場 / 退場 / 再ルート / 窓じまい）。

        受付済み集合を空にすると同時に、走行中／キュー待ちのタスクが共有する
        中断トークンも切る（解決器の ``reset`` が対で行う）。``requested`` を
        空にした直後なので「途中で殺すと requested に残ったまま二度と解決
        されない」問題は起きず、切断共有で 1 件ずつ SMB タイムアウトを待つ
        live 読みが ``~QThreadPool`` の untimed ``waitForDone`` を分オーダーで
        塞ぐ経路（:meth:`shutdown`）も閉じる。
        """
        resolver = getattr(self, "_curation_meta_resolver", None)
        if resolver is not None:
            resolver.reset()
        timer = getattr(self, "_curation_meta_timer", None)
        if timer is not None:
            timer.stop()

    def _schedule_curation_meta(self) -> None:
        """可視範囲が動いた → 80ms 後に未解決の可視タイルを拾い直す。"""
        if self._curation_list() is None:
            return
        timer = getattr(self, "_curation_meta_timer", None)
        if timer is not None:
            timer.trigger()

    def _request_curation_metadata(self) -> None:
        """ビューポート内の未解決フォルダタイルだけ post.md を読ませる。

        「可視で未解決なのはどれか」の選別は
        :func:`grid_overlays.meta_targets`。受付済みの鍵を投げ直さない帳簿と、
        1 tick の投入量の上限（``_CURATION_META_BATCH``）は解決器が持つ
        （上限は投入量であって可視範囲の上限ではない）。答えの返らなかった鍵は
        解放されるので、共有の一時失敗に当たった 1 フォルダが永久に未解決で
        固定されることはない。
        """
        overlay = self._overlay
        if overlay is None or overlay.curation is None or not overlay.entries:
            return
        buffered = self._view.visible_indices(buffer_rows=1)
        if buffered is None:
            return
        start, end = buffered
        tiles = [
            (str(tile.path), bool(tile.is_dir))
            for tile in (
                self._view.tile_at(idx) for idx in range(start, end + 1)
            )
            if tile is not None
        ]
        self._curation_meta_resolver.request(
            grid_overlays.meta_targets(
                tiles, overlay.entries, self._curation_meta_resolver.requested,
            )
        )

    def _on_curation_metadata(self, payload: object) -> None:
        """Landed post.md enrichment for visible curation tiles (N-49 後半).

        Updates the pool entry and the tile **in place** — never re-sorts and
        never re-filters.  The cross-library list is a snapshot
        (:meth:`_curation_filter_active` の不変条件): a title arriving on a tile
        the user is looking at must not make the grid reshuffle under the cursor
        (the same reasoning as ``_on_metadata_batch`` in the plain grid).
        差し替えは平常グリッドと同じ 1 実装 (:meth:`_replace_enriched_tiles`)
        を通る — そこが投稿日系の並びの活性も再評価するので、「一覧の母集合は
        ``build_curation_entry`` が日付抜きで組む → 後追いで日付が入る」の後に
        投稿日順が恒久的に無効のまま残ることがない。
        """
        overlay = self._overlay
        if (
            overlay is None
            or overlay.curation is None
            or not isinstance(payload, list)
            or not payload
        ):
            return
        overlay.entries = self._replace_enriched_tiles(overlay.entries, payload)

    def current_curation_view(self) -> str | None:
        """The active cross-library list (``"starred"`` / ``"later"`` /
        ``"tag:<名前>"``) or ``None``.

        Read by the window when it snapshots a navigation position, so 「戻る」 can
        re-enter the list a drill-down left (UIレビュー 07-25 #58).
        """
        view = self._curation_list()
        return None if view is None else view.key

    def curation_view_label(self, kind: str) -> str:
        """The list's own display name — the single source for crumb / chip / rail.

        文字列 kind で問い合わせる公開口（窓の履歴ラベル / レール行）。導出は
        :meth:`~.curation_list.CurationList.label` に一本化されているので、
        パンくず・条件チップ・レールが互いにずれることはない。未知の種別は
        *kind* をそのまま返す（呼び出し側に例外を投げない）。
        """
        view = CurationList.from_kind(kind)
        return kind if view is None else view.label()

    def _set_sort_silently(self, mode: str | None) -> None:
        """Switch the sort mode + combo WITHOUT triggering a rebuild.

        The one place that knows the combo-sync protocol: ``_sort_mode`` is
        assigned first, so letting ``_on_sort_changed`` fire from the combo would
        wedge a redundant full rebuild into the middle of an overlay's entry /
        exit (each of which ends in exactly one ``_rebuild_grid``).  Every
        overlay takeover and restore goes through here — the block used to be
        copy-pasted at five sites, where changing the protocol meant finding all
        five and the overlays silently diverging if one was missed.

        ``None`` (nothing was saved) and a mode already in effect are no-ops.
        """
        if mode is None or mode == self._sort_mode:
            return
        self._sort_mode = mode
        self.sort_combo.blockSignals(True)
        self._select_sort_in_combo(mode)
        self.sort_combo.blockSignals(False)
        self._sync_reload_tooltip()

    def exit_curation_view(self) -> None:
        """Leave the cross-library curation list, back to the plain grid (H01).

        中身は :meth:`_exit_overlay` — 一覧の種類によらず「解決を切る / 母集合を
        捨てる / 奪った並び順を返す / 単一クラムを解除する」を 1 実装で通る
        （項目 #97）。この入口が残るのは、窓・メニュー・AI 検索が「横断一覧なら
        出る」という意図で呼んでいるため。
        """
        if self._curation_list() is None:
            return
        self._exit_overlay()

    # -------------------------------------- 最近追加されたファイル一覧 (recent)

    #: Newest-N cap for the 「最近追加されたファイル」 walk.  A flat grid of more
    #: than this is not browsable anyway, and the bound is what keeps the walk's
    #: memory O(cap) on a library with hundreds of thousands of files.  The
    #: status bar reports 「N 件中 M 件」 whenever the cap actually bites, so the
    #: truncation is never silent.
    RECENT_FILES_LIMIT = 2000

    def enter_recent_files_view(self, folder: Path) -> None:
        """Show every file under *folder* flat, newest (by file mtime) first.

        The answer to 「このフォルダに最近何が増えた?」: the 投稿日 sort orders by
        when the creator *published*, so files an incremental sync adds to an old post stay
        buried — this view keys on the files' own ``st_mtime`` instead and
        descends the whole sub-tree, so a creator folder shows its newest images
        regardless of which post they landed in.

        Mirrors the cross-library curation list (H01) in shape: any active search
        is torn down so the listing is the sole population, 並び順と単一クラムは
        :meth:`_enter_overlay` の共通尾部が奪い、removable chip / Esc / 「解除」
        で出る。The two overlays are mutually exclusive — each owns the whole
        grid — so entering this one leaves the curation list.

        The walk itself is off the GUI thread (``folder_scan.walk_recent_files``
        driven by :func:`~.grid_tasks.recent_files_walk` on the pane's own
        ``_recent_stream``) with a cooperative cancel token and live 「N 件走査」
        progress; the generation guard drops a walk that lands after an exit.
        """
        session = grid_overlays.OverlaySession.for_recent(folder)
        if session is None:
            return
        # 入れ替えが確定した時点で告げる（不発の早期 return では告げない）。
        self.population_replacing.emit()
        self.exit_curation_view()
        # Clear any engaged search so the listing is the only population.
        self.clear_search_state()
        # 乗り換え（同じ一覧でフォルダを変える場合を含む）も退場と同じ
        # 1 実装で畳む（横断一覧と同じ規約 — 畳めば入場前の並び順が戻る）。
        self._teardown_overlay()
        overlay = self._enter_overlay(
            replace(session, saved_sort=self._sort_mode),
            self._recent_stream.cancel,
        )
        # Show the mode immediately (empty grid + chip) while the walk runs.
        overlay.pending = True
        self._emit_recent_progress_status()
        self._recent_stream.submit_job(
            lambda job, f=folder: recent_files_walk(
                f, self.RECENT_FILES_LIMIT, job
            )
        )
        self._rebuild_grid()

    def _emit_recent_progress_status(self) -> None:
        """Compose the in-flight 「探しています…」 line for the listing.

        Mirrors :meth:`_emit_search_progress_status` — cheap string assembly on
        a throttled tick (the walker reports at most once per ~1000 entries), so
        a minutes-long cold-NAS walk shows movement instead of a frozen label.
        文面の組み立ては :func:`grid_overlays.recent_progress_status`。
        """
        overlay = self._overlay
        self._set_overlay_status(
            grid_overlays.recent_progress_status(
                0 if overlay is None else overlay.scanned
            )
        )

    def _on_recent_progress(self, scanned: object) -> None:
        """Throttled 「N 件走査」 tick from the in-flight walk."""
        overlay = self._overlay
        if (
            overlay is None
            or not overlay.pending
            or not isinstance(scanned, int)
        ):
            return  # the view left the listing while this tick was in flight
        overlay.scanned = scanned
        self._emit_recent_progress_status()

    def _on_recent_files(self, payload: object) -> None:
        """Landed off-thread walk of the 「最近追加されたファイル」 listing.

        着地の読み替え（読み取り失敗の 3 分岐・上限で切った件数・穴の告知）は
        :func:`grid_overlays.recent_landing` の 1 実装。
        """
        overlay = self._overlay
        if overlay is None or overlay.recent is None:
            return  # the view left the listing while this walk resolved
        self._set_overlay_status(
            grid_overlays.apply_landing(
                overlay, grid_overlays.recent_landing(payload),
            )
        )
        self._rebuild_grid()

    def current_recent_view(self) -> Path | None:
        """The folder whose 「最近追加されたファイル」 listing is showing, or ``None``.

        Read by the window when it snapshots a navigation position, so 「戻る」 can
        re-enter a listing a drill-down left (same contract as
        :meth:`current_curation_view`).
        """
        overlay = self._overlay
        return None if overlay is None else overlay.recent

    def exit_recent_files_view(self) -> None:
        """Leave the 「最近追加されたファイル」 listing, back to the plain grid.

        中身は :meth:`_exit_overlay` — 横断一覧の退場と**同じ 1 実装**（項目
        #97）。走査の中止も、奪った並び順を返すことも、そちらの中で対に
        なっている。
        """
        if self._overlay is None or self._overlay.recent is None:
            return
        self._exit_overlay()

    def current_state(self) -> tuple[str, int, str, bool, str]:
        # 横断一覧・最近追加一覧が奪っている並び順は「その一覧の軸」であって
        # ユーザーの選択ではない (UIレビュー 07-25 #59) — 一覧を開いたまま終了
        # したときに star_desc / mtime_desc が永続化されないよう、入場前の値を
        # 返す。
        overlay = self._overlay
        return (
            overlay.saved_sort
            if overlay is not None and overlay.saved_sort is not None
            else self._sort_mode,
            self._icon_size,
            self._view_mode,
            self._exclude_thumb_marker,
            self._thumb_layout_mode,
        )

    def shutdown(self) -> None:
        """Stop all background scanners and debounce timers.

        「debounce timers」はこのペインが足した分（再帰検索のデバウンス・
        キュレーション meta の遅延要求・タグ検索のデバウンス）も含めて
        ``super().shutdown()`` が ``findChildren`` の 1 行でまとめて止める
        — 止め損ねると、閉じた窓の寿命いっぱいデバウンスが生き残り、close
        済みの ``FolderPreviewCache`` に対して後追い解決のバッチが走る
        （R2A-4）。

        Call from the parent window's ``closeEvent`` before tearing down
        widgets so no scanner callback fires into a partially-destroyed tree.
        ``super().shutdown()`` covers the base-class children scanner and
        aspect-probe scanner — without cancelling those, closing mid-scan of
        a huge folder hangs on QThreadPool teardown waiting for their workers
        to drain.  PostGrid adds its own search workers on top.
        """
        super().shutdown()
        self._recursive_scanner.cancel()
        self._shutdown_advanced_search()
        # 条件バーはウィンドウがマウントする（親がこのペインではない）ので、
        # ペインより先に C++ が消える経路がある。後追いの着地から届く更新を
        # 受け取らないよう、生きているうちに口を閉じる。
        bar = getattr(self, "condition_bar", None)
        if bar is not None:
            bar.release()
        # off-thread の 5 経路（body / nsfw / 横断一覧の解決 / その post.md
        # 後追い / 最近追加の走査）は全て ``GuardedStream`` なので、1 本ずつ
        # 名指しせず**このペインの下に居るストリームを列挙して**切る — 新しい
        # 経路を足したときに「shutdown への配線を書き忘れる」が起こせない
        # （レビュー 2026-09-03 項目 #56。走行中タスクの有界待ちは窓側の
        # ``_drain_loader_pools`` が同じ列挙で行う）。
        for stream in self.findChildren(GuardedStream):
            stream.cancel()
        # 絞り込み構文ヘルプの 2 面（図像から開く全文 / 初回自動の短縮版）は
        # どちらも TOP-LEVEL の ``QFrame`` — ペインを閉じても自分では閉じない
        # ので、``_shutdown_advanced_search`` が ``_search_cheatsheet_popup``
        # に対して行っているのと対称に畳む（項目 #201）。
        self._filter_help.close_all()
        # 受付済み集合と 80ms デバウンスは状態なので別途落とす（止めないと
        # 閉じた後に新しいバッチが走り出す）。
        self._reset_curation_metadata()

    def set_show_favorites(self, on: bool) -> None:
        """Toggle the ``♡N`` favorite-count overlay on icon-mode thumbnails."""
        self._view.set_show_favorites(on)

    def set_nav_state(self, can_back: bool, can_forward: bool) -> None:
        """Enable/disable the ← / → buttons to mirror the history stacks.

        Called by the window after every navigation so each button greys
        out when its stack is empty.  The mouse back/forward side-buttons
        short-circuit through the same window slots, so this keeps the
        chrome in sync with what those buttons would do.
        """
        self.back_btn.setEnabled(can_back)
        self.forward_btn.setEnabled(can_forward)

    def attach_history_menus(self, back_menu: QMenu, forward_menu: QMenu) -> None:
        """Wire delayed-popup history dropdowns onto the ←/→ buttons (B-6).

        The window builds the two ``QMenu``s (populated lazily on ``aboutToShow``
        from its history stacks) and hands them here.  ``DelayedPopup`` keeps a
        short click firing the normal navigate signal; only a long press opens
        the menu, so the existing click behaviour is untouched.
        """
        self.back_btn.setPopupMode(QToolButton.DelayedPopup)
        self.back_btn.setMenu(back_menu)
        self.forward_btn.setPopupMode(QToolButton.DelayedPopup)
        self.forward_btn.setMenu(forward_menu)

    def apply_settings(self, state) -> None:  # state: ViewerState
        """Re-apply tunables from ``ViewerState`` at runtime.

        Forwards ``scan_metadata_parallelism`` + ``aspect_probe_parallelism``
        to the workers, updates the slider upper bound, and re-applies the
        caption-field toggles (E-3), rebuilding the grid (selection preserved)
        when any of them changed so the captions re-render.
        """
        self._scanner.set_metadata_parallelism(state.scan_metadata_parallelism)
        self.set_probe_parallelism(state.aspect_probe_parallelism)
        self.apply_settings_icon_size_max(state.post_grid_icon_size_max)
        self.set_show_favorites(state.show_post_favorites)
        self._apply_caption_placement(state.tile_name_placement)
        caption_flags = (
            bool(state.caption_show_posted),
            bool(state.caption_show_locked),
            bool(state.caption_show_size),
            bool(state.caption_show_plan),
        )
        if caption_flags != (
            self._caption_show_posted,
            self._caption_show_locked,
            self._caption_show_size,
            self._caption_show_plan,
        ):
            (
                self._caption_show_posted,
                self._caption_show_locked,
                self._caption_show_size,
                self._caption_show_plan,
            ) = caption_flags
            # Captions are baked into tiles at build time, so a flag change needs
            # a re-tile.  Preserve the current selection across the rebuild.
            cur = self._view.current_path()
            if cur is not None:
                self.queue_pending_select(cur)
            self._rebuild_grid()

    # ----------------------------------------------------- scan handlers

    def _on_scan_finished_payload(
        self, generation: int, entries: list[FolderEntry],
    ) -> None:
        del generation
        self._entries = list(entries)
        # The entry set just changed — any landed / in-flight ``body:``
        # verdicts were computed over the old entries.
        self._invalidate_body_filter()
        # UIレビュー 07-25 #60: 横断一覧の表示中は現在地が「スター付き一覧」で
        # あってスキャン中のフォルダではない — 一覧に入った直後に着地した
        # スキャンで単一クラムを踏み潰さない（件数は _rebuild_grid が入れる）。
        if (
            self._root_or_folder is not None
            and self._overlay is None
        ):
            count = t("viewer.post_grid.count_suffix", n=len(entries))
            self.breadcrumb.set_path(self._root_or_folder, count)
        self._rebuild_grid()

    def _on_metadata_batch(self, generation: int, enriched: list) -> None:
        """Progressive post.md read results — merge into existing entries.

        Sort / filter membership is NOT re-applied here (shuffling items
        under the user's cursor while scrolling is worse than a
        temporarily-stale order).  We merge the enriched entries into
        ``_entries`` and update the matching tiles **in place** (new
        caption + thumbnail source so the loader can fetch the real
        thumbnail).  ``_on_metadata_finished`` handles the final re-sort
        for posted_* / name_* modes.
        """
        if generation != self._pending_scan_generation:
            return
        with measure("metadata_batch_apply", f"{len(enriched)} entries"):
            self._entries = self._replace_enriched_tiles(self._entries, enriched)
        self._backfill_postrefs(enriched)

    def _replace_enriched_tiles(
        self, pool: list[FolderEntry], enriched: list[FolderEntry],
    ) -> list[FolderEntry]:
        """後追いで届いたメタを母集合とタイルへ**その場**で反映する。

        平常グリッドの post.md パス (:meth:`_on_metadata_batch`) と横断一覧の
        後追い解決 (:meth:`_on_curation_metadata`) で同型だった処理の 1 実装。
        並べ替え・絞り込みは再適用しない（スクロール中にカーソル下のタイルを
        動かさない）が、**post.md から導かれる派生状態の再評価**はここで行う:
        再構築を通らない更新経路が 2 本ある以上、そこが唯一の再評価点になる。
        いまのところ再評価するのは投稿日系の並びの活性だけで、材料は母集合の
        ``posted_at`` 走査のみ（I/O ゼロ）。

        戻り値は差し替え済みの母集合（呼び出し側が自分の置き場へ代入する）。
        """
        # ``apply_preview`` always picks the ``#thumb#`` marker as the
        # initial candidate.  Re-select per the user's checkbox so
        # freshly-enriched entries respect the toggle from the start.
        enriched = [self._reapply_thumbnail(e) for e in enriched]
        by_path = {str(e.path): e for e in enriched}
        pool = [by_path.get(str(e.path), e) for e in pool]
        for entry in enriched:
            idx = self._view.index_of_key(self._key_prefix + str(entry.path))
            if idx is None:
                continue
            old = self._view.tile_at(idx)
            tile = self._build_tile(entry)
            if old is not None:
                # Preserve a thumbnail / aspect that already loaded before
                # the post.md pass landed, so enriching the title doesn't
                # flash the folder placeholder back in.
                self._carry_render_state(old, tile)
            self._view.replace_tile(idx, tile)
        # 活性の材料は「実際に並んでいる母集合」。再構築側が渡すのは絞り込み後
        # の母集合なので、ここで絞り込み前のプール全体を渡すと 2 経路が別の答え
        # を出す（``posted_at`` を持つ行が全部フィルタで落ちている状態でメタが
        # 着地すると、再構築が無効化した直後に並んでいない行を根拠に有効化し
        # 直す）。いまタイルになっている行へ揃える。
        self._sync_posted_sort_enabled(self._tiled_entries(pool))
        # Newly-known thumbnail sources get loader + probe tasks enqueued
        # only if they're in the current viewport.
        self._schedule_visible_request()
        return pool

    def _on_metadata_finished(self, generation: int) -> None:
        if generation != self._pending_scan_generation:
            return
        # The scan pass is settled — flip before the closing rebuild so the
        # empty-state hook stops reporting 「検索中…」 for this generation.
        self._scan_loading = False
        # ``has_post_md`` just went live on the enriched entries — a body
        # verdict computed against the shallow (has_post_md=False) entries
        # saw empty bodies everywhere, so it must be recomputed.
        body_active = self._invalidate_body_filter()
        # posted_* / name_* keys read post.md-derived fields that were
        # uniform after the shallow scan; re-sort once every post.md has
        # been parsed.  mtime_* is unaffected.  Preserve the selection.
        # A plain filter needs the closing rebuild too: its haystack includes
        # the post.md title / tags that only exist now, so hits that were
        # invisible against the shallow entries must land (previously they
        # stayed hidden until the next unrelated rebuild).  And a rebuild that
        # settled at zero tiles mid-pass painted 「検索中…」 — re-run it so the
        # message settles to 空 / 一致なし now that the pass is over.
        if body_active or self._date_bounds_active() or bool(
            self._filter_text
        ) or self._view.tile_count() == 0 or self._sort_mode in (
            "posted_asc", "posted_desc", "name_asc", "name_desc",
            "favorites_asc", "favorites_desc",
        ):
            # A date bound just gained real posted_at values from the metadata
            # pass — rebuild so the filter-bar 投稿日 predicate (item 2-2) can
            # hide folders now proven out of range (they were kept as unknown).
            # ``_preserve_selection_for_rebuild`` (not a raw overwrite): a
            # still-unresolved pending selection — e.g. a history restore whose
            # target hasn't been re-tiled yet — must survive this rebuild, not
            # be clobbered with the current (possibly empty) selection.
            self._preserve_selection_for_rebuild()
            self._rebuild_grid()
        self.loading_changed.emit(False)

    def _empty_state_inputs(self) -> grid_empty_state.GridEmptyInput:
        """0 タイルの分類 / 文言 / ボタンに要る観測値を 1 つに束ねる。

        判定そのものは Qt 非依存の :mod:`.grid_empty_state`（純関数）が持つ。
        ここはウィジェットと走査状態から**値**を読み出すだけの層で、3 つの面
        （文言・アイコン・ボタン）が同じ 1 束を読むので、片側だけ増えた分類が
        「押せるのに何も起きない」を作れない。
        """
        overlay = self._overlay
        includes, _excludes, or_pool = self._general_filter_terms()
        advanced_active = self._advanced_search_active()
        return grid_empty_state.GridEmptyInput(
            advanced_active=advanced_active,
            # 3 値の判定式は AI パネル側の ``_advanced_phase()`` 1 本。
            advanced_phase=(
                self._advanced_phase() if advanced_active else "inactive"
            ),
            overlay=grid_overlays.empty_status(
                overlay,
                narrowing_engaged=self._overlay_narrowing_engaged(),
            ),
            recursive_scanning=bool(self._recursive_scanning),
            body_pending=self._pending_body_sig.armed,
            scan_loading=bool(self._scan_loading),
            search_engaged=self._search_engaged(),
            has_browse_population=bool(self._browse_population()),
            first_run=bool(self._first_run_empty),
            first_run_default_library=bool(self._first_run_default_library),
            nsfw_hidden_count=self._nsfw_hidden_count,
            recursive_search=bool(self._recursive_search),
            only_plain_text_restriction=self._only_plain_text_restriction(),
            has_plain_includes=bool(includes or or_pool),
            can_widen_to_subfolders=self._can_widen_to_subfolders(),
            can_go_up=self._empty_card_can_go_up(),
            post_md_scoped_terms=self._has_post_md_scoped_filter_terms(),
            has_entries=bool(self._entries),
            no_post_md_anywhere=self._no_post_md_anywhere(),
        )

    def _empty_state_kind(self) -> str:
        """0 タイルのグリッドの分類（判定は :func:`grid_empty_state.classify`）.

        文言 / アイコン / ボタンの 3 面が全てこれを通るので、語・グリフ・
        ラベル・クリック先が食い違うことがない。分類の一覧と意味は
        :func:`~.grid_empty_state.classify` の docstring。
        """
        return grid_empty_state.classify(self._empty_state_inputs())

    def _only_plain_text_restriction(self) -> bool:
        """検索範囲を広げれば救えるのは「素のテキスト語だけ」で 0 件のとき。

        UIレビュー07-25 追修: ★ 下限・ユーザータグ・種別・ロック・投稿日窓など
        別の軸が効いていても ``filtered_shallow`` が出ていたため、真因ではない
        「検索範囲」を名指しし、主ボタンは高価な再帰走査を起こして結局 0 件、
        という誤誘導になっていた。ほかの軸が 1 つでも効いているなら、正直な
        「絞り込みを解除」カード (``filtered``) に落とす。

        ``field:`` 付きの項（``tags:`` / ``star:`` / ``mytags:`` …）も素の語では
        ないので同じ扱い — 再帰走査はパス名しか見ない
        (:meth:`_general_filter_terms`) ため、範囲を広げても救えない。
        """
        if self._filter_bar_engaged():
            return False
        if self._date_bounds_active():
            return False
        return not any(
            term.field is not None for term in _parse_filter_query(self._filter_text)
        )

    def _empty_state_message(self) -> str:
        """左ペインの空状態の文言 — 分類ごとの文言キーを改行で繋ぐ。

        キーの選択は :func:`grid_empty_state.message_keys`。供給側が持つ 2 系統
        だけがここで分岐する: AI 検索の 0 件 / ⚠ カード（文言は失敗の種別ごと
        に AI パネルが持つ）と、全面占有一覧の 4 状態（一覧側の台帳が答える —
        母集合が空のときの「印の付け方」の案内が軸ごとに違うため）。
        """
        kind = self._empty_state_kind()
        if kind in grid_empty_state.SUPPLIED_KINDS:
            # 席の裁定だけがここ。文言は AI パネル側が持つ。
            return self._advanced_empty_message(kind)
        overlay = self._overlay
        if overlay is not None and kind.startswith(overlay.prefix + "_"):
            return overlay.empty_message(kind[len(overlay.prefix) + 1:])
        return "\n".join(
            t(key)
            for key in grid_empty_state.message_keys(
                kind, self._empty_state_inputs()
            )
        )

    def _no_post_md_anywhere(self) -> bool:
        """並んでいる母集合のどこにも post.md が無い（分野限定条件が必ず 0 件になる）.

        ``has_post_md`` はフォルダ行にしか立たない。投稿フォルダそのものを開いて
        いる（子は全部ファイルで post.md 自身がタイルとして並ぶ）ときにフォルダ
        行だけを見ると「post.md が無い」と嘘を言うので、ファイル行は名前で見る。
        """
        dirs = [e for e in self._entries if e.is_dir]
        if dirs:
            return not any(e.has_post_md for e in dirs)
        return not any(
            e.path.name.lower() == POST_MD_NAME for e in self._entries
        )

    def _empty_card_can_go_up(self) -> bool:
        """空フォルダカードが 「上の階層へ」 を名乗ってよいか。

        UIレビュー07-25 追修: 従来は履歴の有無 (``back_btn``) だけを見ていたが、
        ウィンドウ側の ``_on_go_up`` は**登録ライブラリの境界**では黙って何も
        しない — 履歴があってもライブラリ直下なら、押しても何も起きない死んだ
        ボタンになっていた。↑ ボタンはその境界に合わせて enable 同期されている
        (``main_window._update_nav_buttons``) ので、それを条件に加える。
        履歴条件も残すのは、上位フォルダを開いたことが無い状態で
        「上の階層へ」 を勧めない従来の判断（= ライブラリ外へ迷い出さない）を
        変えないため。どちらか欠ければ 「フォルダを開く…」 に落ちる。
        """
        return self.up_btn.isEnabled() and self.back_btn.isEnabled()

    def _can_widen_to_subfolders(self) -> bool:
        """「サブフォルダも検索」を ON にすれば母集合が広がる状態か。

        UIレビュー 2026-08-28 N-60: ドリルイン移動は ``clear_search_state`` が
        範囲設定（``recursive_check``）まで落とすので、0 件になったときに範囲を
        広げ直す導線が要る。``filtered_shallow`` はそれを主ボタンに持っているが、
        ほかの軸（★下限・種別・投稿日…）が 1 つでも効いていると
        :meth:`_only_plain_text_restriction` が偽になり ``filtered`` へ落ちて、
        画面から範囲を広げる手段が消えていた。

        「広げれば救えるか」の条件は ``_apply_filter_and_sort`` の子孫マージ分岐と
        同一にする（押しても何も変わらない「効かない回復手段」を作らないため）:
        素の（``field:`` 無しの）肯定語があり、範囲が OFF で、子孫を一切採らない
        2 経路——AI 検索による結果差し替え / 🔒 ロックありのみ——に居ないこと。
        """
        if self._recursive_search or self._advanced_search_active():
            return False
        if self._filter_locked_only:
            return False
        includes, _excludes, or_pool = self._general_filter_terms()
        return bool(includes or or_pool)

    def _empty_state_icon(self) -> str:
        """空状態の文言の上に描くグリフ（:func:`grid_empty_state.icon_for`）."""
        return grid_empty_state.icon_for(self._empty_state_kind())

    def _empty_action_callbacks(self) -> dict[str, Callable[[], None]]:
        """空状態カードの動作 id → 実際の呼び出し先。

        :class:`~.grid_empty_state.ActionSpec` の ``action`` はただの文字列
        （Qt 非依存の表で「どの分類がどの一手を出すか」を固定できるように）で、
        ウィジェット側の一手へ戻すのがこの 1 表。**不変**: 鍵は
        :data:`~.grid_empty_state.ACTION_IDS` と集合一致すること（片側だけ
        増えると「押せるのに何も起きないボタン」が戻る）。
        """
        return {
            "open_folder": self._on_change_root_clicked,
            "help": self.help_requested.emit,
            "go_up": self.go_up_requested.emit,
            "close_curation": self.exit_curation_view,
            "close_recent": self.exit_recent_files_view,
            "retry_overlay": self._retry_overlay_view,
            "clear_overlay_narrowing": self._clear_narrowing_over_overlay,
            "show_nsfw": lambda: self.set_hide_nsfw("off"),
            "widen_subfolders": self._widen_to_subfolders,
            "clear_search": self._on_escape_clear,
        }

    def _empty_state_actions(self) -> list[EmptyAction]:
        """0 タイルカードのボタン列 — ラベルと押下先を同じ 1 分岐から出す。

        並びの決定は :func:`grid_empty_state.plan_actions`（Qt 非依存）。
        AI 検索の 0 件 / ⚠ カードだけは中身ごと AI パネルが供給する — 効いて
        いる軸ごとに 1 つ緩和ボタンを出すので、分類だけでは決まらない。
        """
        kind = self._empty_state_kind()
        if kind == "advanced_zero":
            return self._empty_state_relaxations()
        if kind == "advanced_error":
            return self._advanced_error_actions()
        callbacks = self._empty_action_callbacks()
        return [
            EmptyAction(t(spec.label_key), callbacks[spec.action])
            for spec in grid_empty_state.plan_actions(
                kind, self._empty_state_inputs()
            )
        ]

    def _widen_to_subfolders(self) -> None:
        """「サブフォルダも検索」を ON にして同じ needle のまま範囲を広げる.

        子孫走査はチェックボックス自身のハンドラが蹴る（UIレビュー 07-25 #26）。
        """
        self.recursive_check.setChecked(True)

    def _clear_narrowing_over_overlay(self) -> None:
        """一覧は残したまま、上に載せた条件だけ落とす（``*_filtered``）.

        ``_on_escape_clear`` は一覧そのものを畳んでしまうので通さない —
        走査 / 解決は高価。
        """
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self.clear_search_state()

    def _retry_after_scan_error(self) -> None:
        """走査失敗カードの［再試行］（基底の差し替え）.

        Route through the window's reload slot so its bookkeeping
        (random-sort reshuffle etc.) stays consistent with the ↻ button.
        """
        self.reload_requested.emit()

    def _retry_overlay_view(self) -> None:
        """読み取りに失敗したオーバーレイ一覧をもう一度組み直す（N-09）。

        入場そのものをやり直すので、母集合の収集・off-thread 解決・空状態の
        分類まで 1 本の既存経路（``enter_*_view``）を再利用する — 「失敗時だけの
        別経路」を作らない。
        """
        overlay = self._overlay
        if overlay is None:
            return
        # 再入場は ``_overlay`` が残っているあいだに行う — 入場側が「乗り換えは
        # 入場前の並び順を上書きしない」ガードを持つので二重には奪わない。
        view = overlay.curation
        if view is not None:
            self.enter_curation_view(view.key)
            return
        folder = overlay.recent
        if folder is not None:
            self.enter_recent_files_view(folder)

    def _on_scan_failed_payload(self, message: str) -> None:
        """Scan failure (I01): error state + breadcrumb + window notification."""
        super()._on_scan_failed_payload(message)
        # The breadcrumb is still showing 「読み込み中…」 (set_root's plain
        # text) — settle it on the attempted root so the location stays clear.
        if self._root_or_folder is not None:
            self.breadcrumb.set_path(self._root_or_folder, "")
        self.counts_changed.emit(0, 0)
        self.scan_failed.emit(message)

    def _on_scan_partial_payload(self, unclassified: int) -> None:
        """穴の告知は窓のステータスへ（結果は出したまま）。"""
        self.scan_partial.emit(unclassified)

    def is_library_empty(self) -> bool:
        """True when the current root settled with no entries at all (A02).

        Drives the centre pane's welcome card: only a genuinely empty library
        (no children, no active search, no scan failure — I01 keeps its own
        error card) warrants the 「フォルダを開く…」 welcome over the "select
        from the grid" hint that has nothing to select.
        """
        if self._scan_error is not None:
            return False
        if self._advanced_search_active():
            return False
        # The cross-library curation list / 最近追加されたファイル listing are
        # overlays, not the library being empty — never surface the welcome card
        # underneath one.
        if self._overlay is not None:
            return False
        return not self._entries

    def set_first_run(
        self, active: bool, *, default_library: bool = False
    ) -> None:
        """空ライブラリを「初回起動」として扱うか（N-02 — ウィンドウが教える）.

        ナビ履歴の有無も「今のルートが自動生成の既定ライブラリか」も
        :class:`main_window.ViewerWindow` しか知らないので、判定はそちら
        （空状態オーケストレータ）が行い、グリッドは結果だけ受け取って
        :meth:`_empty_state_kind` の 1 分岐に反映する。既に同じ値なら何もしない
        （空状態は毎リビルドで再評価されるため、無駄な再描画を作らない）。
        """
        active = bool(active)
        default_library = bool(default_library)
        if (
            active == self._first_run_empty
            and default_library == self._first_run_default_library
        ):
            return
        self._first_run_empty = active
        self._first_run_default_library = default_library
        self.refresh_empty_state()

    # ----------------------------------------------------- filter / sort

    @contextmanager
    def _batched_condition_clear(self):
        """複数の次元を続けて中立へ戻す間、再クエリ・再構築を 1 回に畳む.

        各 ``_clear_dim_*`` は「ユーザーが 1 つのコントロールを操作したのと
        同じ正規の経路」を通るので、それぞれが再クエリと再構築を起こす。表を
        なぞって順に呼ぶリセット導線（:meth:`~.advanced_search.
        AdvancedSearchController._on_reset_advanced_only`）ではそれが N 回になり、
        途中の中途半端な条件でスキャナを蹴ってしまう（レビュー 2026-09-03
        項目 #89）。抜けたあとに呼び出し側が 1 回だけ蹴る。
        """
        prev = getattr(self, "_suspend_requery", False)
        self._suspend_requery = True
        try:
            yield
        finally:
            self._suspend_requery = prev

    def _rebuild_grid(self, *, seed_aspect: bool = True) -> None:
        if getattr(self, "_suspend_requery", False):
            # 次元をまとめて中立化している最中（:meth:`_batched_condition_clear`）。
            return
        self._sync_sort_combo_for_rank()
        # 再構築の前に「いま選ばれているもの」を控える — ``set_tiles`` は毎回
        # 選択を落とすので、後段の :attr:`preview_context_lost` 判定は
        # 「選択が消えたか」ではなく「**新しい母集合に居るか**」で行う
        # （N-58: 選択タイルが結果に残っている絞り込みでプレビューを消さない）。
        selected_before = self._view.current_path()
        entries = self._apply_filter_and_sort(self._entries)
        # 実際に並んだ母集合で投稿日系の並びの可否を決める (N-93)。
        self._sync_posted_sort_enabled(entries)
        self._set_entries_as_tiles(entries, seed_aspect=seed_aspect)
        # Report the displayed folder / file split so the status bar stays in
        # sync with whatever is on screen (direct children, recursive walk, or
        # advanced-search results).
        folders = sum(1 for e in entries if e.is_dir)
        self.counts_changed.emit(folders, len(entries) - folders)
        # 検索・絞り込み中はパンくずの件数もコンテナ件数のままだと
        # バナーの「1件」と矛盾する (UIレビュー #26) — 表示中の件数へ
        # 同期する（名前フィルタ等の部分集合は「N 件中 M 件」表記）。
        # has_trail() ガードで set_root 直後の「読み込み中…」表示は
        # 上書きしない。
        if self._overlay is not None and self.breadcrumb.has_trail():
            # UIレビュー 07-25 #60: 横断一覧の母集合はライブラリ全体のキュレー
            # ション項目であって「いま立っているフォルダの直下」ではない。
            # ``_entries``（直下集合）を分母にすると「9 件中 2 件」という無関係な
            # 嘘になるので、表示件数だけを出す。NSFW の隠し件数も直下ブラウズの
            # 分母（``_entries``）に対する数え方なのでここでは足さない — 最近
            # 追加一覧では抑制自体は走るが、その分母は一覧の母集合であって
            # パンくずが語る「いま立っているフォルダ」ではない。
            self.breadcrumb.set_count_text(
                t("viewer.post_grid.count_suffix", n=len(entries))
            )
        elif self._root_or_folder is not None and self.breadcrumb.has_trail():
            total = len(self._browse_population())
            shown = len(entries)
            # 検索欄の語で探している間は範囲（このフォルダのみ / サブフォルダも）
            # を件数に併記する（N-26: 0 件のときしか範囲が画面に出ず、ヒットが
            # あると取りこぼしに気づけなかった）。AI 検索は常に配下全体なので
            # 併記しない（範囲の切替が無い）。
            searching = bool(self._filter_text) and not self._advanced_search_active()
            if searching and self._recursive_search:
                count = t("viewer.post_grid.count_suffix_recursive", n=shown)
            elif searching and shown == total:
                count = t("viewer.post_grid.count_suffix_shallow", n=total)
            elif searching and shown < total:
                count = t(
                    "viewer.post_grid.count_filtered_shallow",
                    total=total, shown=shown,
                )
            elif shown == total:
                count = t("viewer.post_grid.count_suffix", n=total)
            elif (
                shown < total
                and not self._advanced_search_active()
                and not self._recursive_search
            ):
                count = t(
                    "viewer.post_grid.count_filtered",
                    total=total, shown=shown,
                )
            else:
                # 詳細検索/再帰検索の結果は直下集合の部分集合ではない —
                # 表示件数のみを出す（バナーと同じ数字）。
                count = t("viewer.post_grid.count_suffix", n=shown)
            # UIレビュー 07-25 #130: 「年齢制限を隠す」 is a PERSISTENT setting with
            # no visible marker, so a partial application just looked like
            # missing files ("数が合わない").  Ride the existing hidden counter
            # onto the count display — the only place the numbers are stated —
            # instead of leaving the suppression silent.  Fully-hidden folders
            # keep their own empty-state card ("nsfw_hidden").
            if self._nsfw_hidden_count > 0 and shown > 0:
                count += t(
                    "viewer.post_grid.count_nsfw_hidden",
                    n=self._nsfw_hidden_count,
                )
            self.breadcrumb.set_count_text(count)
        # Active-search visibility (item 2 / Phase 1-3): the condition chip
        # bar and the auxiliary advanced-search surfaces both re-sync on every
        # rebuild — the single display choke point, so they can never show a
        # stale search.
        self._update_condition_bar(len(entries))
        self._update_advanced_badge()
        # Toolbar mode chips (名前/本文) re-sync here too — the same choke
        # point — so programmatic filter restores keep them truthful.
        self._sync_search_mode_chips()
        # 母集合が検索 / オーバーレイで入れ替わり、直前に選ばれていたものが
        # そこに居なくなったなら、窓のプレビュー列・右情報パネルも一緒に未選択へ
        # 落とす（N-13 / N-58）。2 つのゲートが要点:
        #
        # * ``_search_engaged()`` — 平常ブラウズの再構築（メタデータ着地・NSFW
        #   レーティング着地・並べ替え・キャプション設定）では絶対に鳴らさない。
        # * ``selected_before`` が新しい母集合に**居ない** — 絞り込みを打っても
        #   選択タイルが結果に残っているあいだはプレビューを消さない。
        #   （選択そのものは ``set_tiles`` が毎回落とすので、選択の有無では
        #   この 2 つを区別できない。）
        if self._search_engaged() and self._view.current_path() is None:
            if selected_before is None or all(
                e.path != selected_before for e in entries
            ):
                self.preview_context_lost.emit()
        self._maybe_show_click_hint(bool(entries))

    def _maybe_show_click_hint(self, has_tiles: bool) -> None:
        """クリック規約のセッション初回ヒント（UIレビュー 2026-08-28 N-146）.

        「クリック = プレビュー / ダブルクリック = 開く」という中核規約を
        教える面が F1 ヘルプと右一覧のツールチップにしか無く、肝心のグリッド
        が無言だった（07-12 D04 の再掲）。全画面の初回操作ヒント（G05 =
        ``lightbox_parts/overlays.py::_HintOverlay``）と同じ「セッション初回
        のみ・数秒で自動消灯・クリック透過」の作法を、平常ブラウズへ水平展開
        する。

        固定色の :class:`~snappix.common.ui.overlay_chrome.OverlayPill` は
        **画像の上に載る面**の固定色例外（design.md 使用ルール2）なので
        ここでは使わない — グリッドはテーマ面なので、既存のポップオーバー
        様式（``QFrame#toolbarPopover`` の QSS）+ ``hint_style`` ラベルで
        テーマに追従させる。表示条件は「平常ブラウズ（横断一覧・検索中で
        ない）に 1 枚以上のタイルが並んだ最初の 1 回」。
        """
        if (
            self._click_hint_shown
            or not has_tiles
            or self._overlay is not None
            or self._search_engaged()
        ):
            return
        self._click_hint_shown = True
        frame = QFrame(self._view)
        frame.setObjectName("toolbarPopover")
        # 純粋な案内 — 下のタイルへのクリックを一切奪わない。
        frame.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 8, 14, 8)
        label = QLabel(t("viewer.post_grid.click_hint"))
        label.setStyleSheet(hint_style())
        lay.addWidget(label)
        frame.adjustSize()
        view = self._view
        frame.move(
            max(0, (view.width() - frame.width()) // 2),
            max(0, view.height() - frame.height() - 24),
        )
        frame.show()
        frame.raise_()
        self._click_hint = frame
        QTimer.singleShot(6000, frame.hide)

    def _drop_thumb_markers(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Drop ``#thumb#`` marker *files* when the exclude toggle is on.

        Applied to search results (recursive descendants, tag / semantic /
        media file hits) so the ``#thumb#`` creator-icon images the writing tool
        writes don't surface as standalone result tiles.  Folders are never
        dropped here — their representative thumbnail already honours the toggle
        via ``select_thumbnail``.  Order is preserved (relevance / sort intact).
        """
        if not self._exclude_thumb_marker:
            return entries
        return [
            e for e in entries
            if e.is_dir or not e.path.name.lower().startswith(THUMB_MARKER_PREFIX)
        ]

    def _browse_population(self) -> list[FolderEntry]:
        """直下ブラウズの母集合（= 分母 / 空状態の判定対象）。

        「クリエイターアイコンを隠す」は検索の軸ではなく**表示の軸**なので、
        表示側 (``_apply_filter_and_sort``) だけでなく母集合側にも同じ変換を
        通す（項目#64 追修正）。素の ``self._entries`` を分母にしていたため、
        トグルを ON にしただけで:

        * 何も絞り込んでいないのにパンくずが「(4 件中 3 件)」と絞り込み表記
          へ化ける（``shown < total`` が常に成立する）
        * ``#thumb#`` しか無いフォルダの空状態が ``empty_folder`` ではなく
          ``filtered`` になり、「検索条件をすべて解除」を押しても何も戻らない
          行き止まりになる

        という 2 つの嘘が出ていた。トグル OFF なら ``_drop_thumb_markers`` が
        そのまま返すので、従来の挙動と完全に一致する。
        """
        return self._drop_thumb_markers(self._entries)

    def _general_filter_terms(self) -> tuple[list[str], list[str], list[str]]:
        """Plain (non-field-scoped) include / exclude / OR-pool filter terms.

        The recursive descendant walk and the advanced-search name overlay can
        only match against bare paths / basenames, so field-scoped terms
        (``tags:`` etc. — including ``~``-prefixed ones) are excluded here —
        they restrict the direct children only.  Returns
        ``(includes, excludes, or_pool)`` of casefolded needles: *includes*
        are AND-required, *excludes* AND-forbidden, and *or_pool* is the
        Danbooru-style ``~`` pool — when non-empty, at least one member must
        match (#3: consumers must NOT flatten it into the AND includes, or
        ``~a ~b`` silently degrades to ``a AND b`` on descendant paths).
        """
        includes: list[str] = []
        excludes: list[str] = []
        or_pool: list[str] = []
        for term in _parse_filter_query(self._filter_text):
            if term.field is not None:
                continue
            if term.exclude:
                excludes.append(term.value)
            elif term.or_group:
                or_pool.append(term.value)
            else:
                includes.append(term.value)
        return includes, excludes, or_pool

    def _apply_filter_and_sort(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        # Cross-library curation list mode (H01) overrides everything: the grid
        # shows the resolved 「あとで見る」/「スター付き」 pool, narrowed only by the
        # in-memory filter-bar predicates + plain filter box, never the direct
        # children.
        if self._overlay is not None:
            return self._apply_overlay_population()
        # When the advanced-search panel has an active query, the grid shows
        # the (recursive) tag / media-type results instead of the direct
        # children.  Otherwise this stays byte-for-byte the original behaviour.
        if self._advanced_search_active():
            # Advanced hits keep their own rating band; on top of the
            # population the "advanced"-scope dimensions of the view registry
            # apply (種別・★・あとで見る・ユーザータグ)。
            #
            # **NSFW 抑制もここを通す（UIレビュー 2026-08-28 N-04 の裁定）**:
            # 旧実装は「AI 検索は結果自体が別の年齢軸（AI パネルの年齢区分）を
            # 持つので通さない」という理由でスキップしていたが、その軸の既定値は
            # ``tag_search_rating="all"``＝何も抑制しないので、**永続設定である
            # 「年齢制限を隠す」が AI 検索した瞬間に無言で失効**していた（抑制中
            # マーカーも消えるので気づく手掛かりが無い）。2 つの軸は AND で重なる
            # ——「年齢制限を隠す」はビューの軸、AI パネルの年齢区分はクエリの軸——
            # という読みへ改めた。``_nsfw_rating_map`` は path→rating の汎用実装
            # なので、フォルダタイルにもファイルタイルにもそのまま効く。
            shown = self._apply_nsfw_filter(
                self._fold_view_dimensions(
                    "advanced", self._apply_advanced_search()
                )
            )
            # 件数と「0 件確定」の判定はパイプラインの**終端**で 1 回だけ書く
            # （レビュー 2026-09-03 項目 6）。``_apply_advanced_search`` の中で
            # 書いていた頃は、その後ろに居る ``_fold_view_dimensions`` /
            # ``_apply_nsfw_filter`` が全部落としても件数が絞り込み前のまま残り、
            # 「タイル 0 件・0 件カードも出ない・ステータスは N 件」という説明の
            # つかない画面になっていた（``_advanced_status_text`` がこの 1 個の
            # スカラーを読む。0 件カードの判定は項目 #95 で画面に載ったタイル数
            # ＝ ``_empty_state_kind`` の advanced 分岐へ移した）。
            self._advanced_match_count = len(shown)
            return shown
        # Direct children: full match surface (name + title + tags) since
        # post.md has already been parsed during the metadata pass.
        direct = list(entries)
        # 「クリエイターアイコンを隠す」 は直下ブラウズにも効かせる（レビュー
        # 2026-08-27 #64）: 以前は再帰検索の子孫と AI 検索結果にしか適用されて
        # おらず、`#thumb#` を持つ投稿フォルダを直下で開くとトグル ON でも
        # マーカーのタイルが残っていた。ツールチップは「クリエイターアイコン
        # （`#thumb#` プレフィックスのファイル）を非表示にします」と明言して
        # おり、右ペインの FileListView はトグルと無関係に常に落とすので、
        # 同じフォルダで左「4 件」/ 右「3件」という説明のつかない差になる。
        direct = self._drop_thumb_markers(direct)
        if self._filter_locked_only:
            direct = [e for e in direct if e.locked_count > 0]

        terms = _parse_filter_query(self._filter_text)
        # ``body:`` terms read post.md from disk, so they are evaluated on a
        # worker (see ``_apply_body_filter``); everything else stays a cheap
        # synchronous in-memory match.
        body_terms = [t for t in terms if t.field == "body"]
        sync_terms = [t for t in terms if t.field != "body"]
        # ``~body:`` pool members make the OR pool span the sync/async seam
        # (#4): a sync-side pool miss is then NOT final — the entry may still
        # be admitted by a matching ``~body:`` alternative on the worker.
        body_or = any(t.or_group for t in body_terms)
        or_confirmed: set[str] | None = None
        if sync_terms:
            # When "サブフォルダも検索" is on, the recursive walk surfaces
            # individual descendant files matching the query — so we
            # deliberately drop ``file_names`` from the direct haystack to
            # avoid showing both the post folder AND its matching files as
            # parallel entries.  In non-recursive mode there is no
            # descendant pass, so ``file_names`` stays in the haystack and
            # the post folder is the only entry surfaced for a file hit.
            # ``e.file_names`` is already lower-cased by the metadata pass so
            # attachment names participate in the same AND/exclusion logic as
            # the title + tags; field-scoped terms (``tags:`` / ``plan:`` …)
            # match only their attribute.
            include_file_names = not self._recursive_search
            if body_or:
                # Evaluate the AND / exclusion sync terms alone (a pool miss
                # must not reject here), and remember which entries the
                # sync-side pool members already admit — those need no body
                # verdict for the pool; the rest are re-judged against the
                # ``~body:`` members by _apply_body_filter.
                and_terms = [t for t in sync_terms if not t.or_group]
                # Control tokens (type:/rating:/score:) are skipped inside
                # _match_filter_terms and never join the pool there — keep
                # them out of the pool-hit probe too, or a lone ``~type:x``
                # would read as "pool satisfied" for every entry.
                pool_terms = [
                    t for t in sync_terms
                    if t.or_group and t.field not in _CONTROL_FIELDS
                ]
                direct = [
                    e for e in direct
                    if _match_filter_terms(
                        e, and_terms, include_file_names=include_file_names,
                        curation=self._curation_filter_for,
                    )
                ]
                or_confirmed = {
                    str(e.path) for e in direct
                    if pool_terms and _match_filter_terms(
                        e, pool_terms, include_file_names=include_file_names,
                        curation=self._curation_filter_for,
                    )
                }
            else:
                direct = [
                    e for e in direct
                    if _match_filter_terms(
                        e, sync_terms, include_file_names=include_file_names,
                        curation=self._curation_filter_for,
                    )
                ]
        if body_terms:
            direct = self._apply_body_filter(
                direct, body_terms, or_confirmed=or_confirmed,
            )

        # Descendants: only when recursive mode is enabled AND at least one
        # positive *plain* term (AND include or ``~`` OR-pool member) is
        # present.  Field-scoped terms
        # (``tags:`` etc.) can't be matched against bare descendant paths —
        # the recursive walk only sees filenames — so they restrict the
        # direct children only.  A pure-exclusion query on a deep tree would
        # match nearly every descendant, which is expensive and rarely what
        # the user means.  The recursive scanner already filtered these by
        # relative path ON THE WORKER THREAD, so here we just consume them
        # verbatim — no GUI-thread substring scan — but only while they still
        # correspond to the current query (a walk for a stale needle is
        # ignored until its replacement lands).
        gen_includes, gen_excludes, gen_or = self._general_filter_terms()
        descendants: list[FolderEntry] = []
        if (
            (gen_includes or gen_or)
            and self._recursive_search
            and self._recursive_results is not None
            and self._recursive_results_query
            == (tuple(gen_includes), tuple(gen_excludes), tuple(gen_or))
        ):
            if self._filter_locked_only:
                # Descendants have no parsed locked_count (post.md isn't read
                # for them by design), so none can satisfy locked-only —
                # suppress rather than show inconsistent results.
                pass
            else:
                descendants = [entry for entry, _rel in self._recursive_results]
                # Honour the #thumb# exclude toggle for descendant file hits.
                descendants = self._drop_thumb_markers(descendants)

        # Remember how many descendants matched so _on_recursive_results can
        # report the hit count without re-running the filter.
        self._descendant_match_count = len(descendants)
        # The "plain"-scope dimensions of the view registry (投稿日・種別・
        # ★・あとで見る・ユーザータグ — item 2-2 / H02), then NSFW view
        # suppression — all plain-browse view filters applied after the
        # query filters.
        combined = self._fold_view_dimensions("plain", direct + descendants)
        combined = self._apply_nsfw_filter(combined)
        return self._sorted_dir_first(combined)

    # ------------------------------------------------ filter-bar predicates

    def _filter_bar_criteria(self) -> filter_predicates.FilterBarCriteria:
        """フィルタバー各軸の**値**を 1 つに束ねる（述語 / アクセント同期の入口）.

        述語は :mod:`.filter_predicates` の Qt 非依存な純関数で、ホストの
        属性を直読みしない — 呼ぶ直前にここで値へ畳んで渡す。投稿日の半開窓
        だけは派生値なので載せず、:meth:`_apply_date_predicate` が
        ``_current_date_bounds()`` から直接渡す。
        """
        return filter_predicates.FilterBarCriteria(
            media=self._filterbar_media,
            star_min=self._filterbar_star_min,
            later=self._filterbar_later,
            user_tag=self._filterbar_user_tag,
            recursive=bool(self._recursive_search),
            rating=self._tag_rating_key,
            date_preset=self._tag_date_preset,
            locked_only=bool(self._filter_locked_only),
        )

    def _apply_media_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """フィルタバーの種別制限（判定は :func:`filter_predicates.apply_media`）."""
        return filter_predicates.apply_media(entries, self._filterbar_media)

    def _apply_curation_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """★ 下限 + 「あとで見る」 + ユーザータグ の合成。

        ビュー次元表は 3 軸を別々の行として持つので、本メソッドは一括適用の
        互換 API（テストの直接呼び出し先）として残る。
        """
        return filter_predicates.apply_curation(
            entries, self._filter_bar_criteria(), self._curation_filter_for
        )

    def _apply_star_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """★ 下限（``_filterbar_star_min`` > 0 のときだけ絞る）."""
        return filter_predicates.apply_star(
            entries, self._filterbar_star_min, self._curation_filter_for
        )

    def _apply_later_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """「あとで見る」フラグ（チェック ON のときだけ絞る）."""
        return filter_predicates.apply_later(
            entries, self._filterbar_later, self._curation_filter_for
        )

    def _apply_usertag_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """ユーザータグ（コンボが「すべて」以外のときだけ絞る）."""
        return filter_predicates.apply_usertag(
            entries, self._filterbar_user_tag, self._curation_filter_for
        )

    def _apply_overlay_view(
        self,
        entries: list[FolderEntry],
        rel_paths: dict[str, str],
    ) -> list[FolderEntry]:
        """Narrow + sort an overlay population (curation list / 最近追加一覧).

        Both overlays own the whole grid and hold their population in memory,
        so the *shape* of their rebuild is identical and lives here once: 素の
        テキスト項は :func:`grid_overlays.narrow_by_text`、続いて分野限定項と
        ビュー次元（種別・★・あとで見る・ユーザータグ）を重ね、最後に現在の
        並び順で **フォルダ先頭のグルーピング抜き**に並べる
        (:meth:`_sorted_flat` — その group 分けは一覧が about にしている軸を
        上書きしてしまう)。全部メモリ上（両母集合は入場時に GUI スレッドの外で
        解決済み）で、描画経路の I/O ゼロ規律を守る。

        *rel_paths* は ``str(path)`` → 絞り込みへ見せるパスで、2 つの呼び出し元
        の唯一の差（横断一覧はライブラリ基準、最近追加一覧はルート基準）。

        分野限定トークンのうち、この母集合でも**メモリだけで**答えられるもの
        （``name:`` / ``title:`` / ``star:`` / ``mytags:`` / ``later:`` —
        :data:`_OVERLAY_FIELD_TOKENS`）は実際に効かせる。効かせられないのは
        post.md 由来の行（``tags:`` / ``body:`` …）だけで、``body:`` は検索欄の
        モードチップごと無効化して見た目と一致させる
        (:meth:`_sync_search_mode_chips`)。``~`` プール項は対象外 — 効かない
        代替と効く代替が同じプールに混ざると、プール全体の意味が変わる。
        """
        items = grid_overlays.narrow_by_text(
            entries, rel_paths, *self._general_filter_terms(),
        )
        items = grid_overlays.narrow_by_fields(
            items, self._filter_text, _OVERLAY_FIELD_TOKENS,
            self._curation_filter_for,
        )
        # The "overlay"-scope dimensions of the view registry (種別・★・
        # あとで見る・ユーザータグ).  locked / 投稿日 は scope 外 — この母集合
        # （resolve_curation_paths / walk_recent_files）は locked_count=0 /
        # posted_at=None で組まれるため原理的に意味を持たず、チップも表から
        # 同じ scope 判定で消える（項目#65）。
        items = self._fold_view_dimensions("overlay", items)
        return self._sorted_flat(items)

    def _apply_overlay_population(self) -> list[FolderEntry]:
        """占有中の一覧の母集合をグリッドの並びへ（2 一覧の共通 1 実装）。

        並び順は現在のモードに従う（入場で奪った軸のままとは限らない — 一覧を
        出ずにサイズ / 名前へ並べ替えられる）。``#thumb#`` / ``post.md`` の述語は
        要らない: 最近追加一覧は走査側が落としており（新着 N 件の枠を食わせ
        ない）、横断一覧の母集合は印の付いた行だけ。

        「年齢制限を隠す」は **最近追加一覧にだけ**掛ける
        (:func:`grid_overlays.applies_nsfw`) — 効かせない側では前回のブラウズ
        再構築が残した隠蔽数をここで消す（消さないと「(…N 件を非表示中)」が
        誤って付き続ける）。

        読めずに落ちた行のプレースホルダは並べ替え・絞り込みの**外**で常に末尾
        へ付く — 「印を付けた件数」との対応を一覧が偽らないための告知タイルで
        あって、母集合の一員ではない（ソート鍵になる mtime も ★ も持たない）。
        全滅のときだけ出さない: そこは 0 タイル + ``curation_error`` カード
        （再試行 / 一覧を閉じる）の席で、タイルがあるとカードが出なくなる。
        """
        overlay = self._overlay
        if overlay is None:
            return []
        items = self._apply_overlay_view(overlay.entries, overlay.rel_paths)
        if grid_overlays.applies_nsfw(overlay):
            items = self._apply_nsfw_filter(items)
        else:
            self._nsfw_hidden_count = 0
        if overlay.ghosts and not overlay.failed:
            items = [*items, *overlay.ghosts]
        return items

    def _apply_date_predicate(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """素の閲覧グリッドへのフィルタバー投稿日制限。

        ``posted_at`` は左ペイン自身のメタデータパス（post.md を読む）が
        シードするので追加のワーカーは要らない。日付が未確定のエントリは
        残す（判定は :func:`filter_predicates.apply_date`）。
        """
        lo, hi = self._current_date_bounds()
        return filter_predicates.apply_date(entries, lo, hi)

    # ------------------------------------------------- NSFW view suppression

    def _hide_nsfw_bands(self) -> set[str]:
        """The set of dominant ratings to hide, or an empty set when off.

        判定は :func:`nsfw_filter.hide_bands` — tags.db が無ければ設定が何で
        あれ空（区分を答えられる索引が無いのに隠す相手は決まらない）。
        """
        return nsfw_filter.hide_bands(
            self._hide_nsfw, has_ratings=self._tag_index is not None,
        )

    def _apply_nsfw_filter(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Hide tiles whose representative rating is in the suppressed band.

        ビューの軸（検索の次元ではない）。判定は :func:`nsfw_filter.partition`
        で、材料はメモリ上の ``_nsfw_rating_map``（``grid_tasks.nsfw_ratings``
        が off-thread で埋める）だけ — この経路は sqlite を触らない。未知の行は
        **見えたまま**照会へ回るので、グリッドが一瞬空になることがない。各バッチ
        の着地が再構築を呼ぶ。
        """
        split = nsfw_filter.partition(
            entries,
            self._hide_nsfw_bands(),
            self._nsfw_rating_map.get,
            self._nsfw_resolver.requested,
        )
        if split.unknown_folders or split.unknown_files:
            self._kick_nsfw_scan(split.unknown_folders, split.unknown_files)
        # Remembered for the empty-state hook: when this pass is what emptied
        # the grid, the message must blame the NSFW view setting, not 絞り込み.
        self._nsfw_hidden_count = split.hidden_count
        return split.kept

    def set_hide_nsfw(self, value: str) -> None:
        """Set the NSFW suppression band ("off" / "questionable" / "explicit").

        Persistent view setting (item 2-1).  Rebuilds the grid so the change
        takes effect immediately; the ⋯-menu action's checked state is synced
        by :meth:`_sync_nsfw_menu`.  A no-op without a tags.db (nothing to
        band on) beyond storing the preference.

        **Scope: the plain browse grid only** — by design (``state.py`` の
        ``hide_nsfw`` docstring).  The right pane / stage image track /
        lightbox deliberately do NOT consume it; widening it there would mean
        duplicating the asynchronous rating-resolution machinery.  The menu
        label says so out loud (「グリッドで年齢制限を隠す」) so the scope is
        readable from the screen rather than only from the source
        (UIレビュー 2026-08-28 N-81 の〔設計〕側).
        """
        value = nsfw_filter.normalize_band(value)
        if value == self._hide_nsfw:
            return
        self._hide_nsfw = value
        self._sync_nsfw_menu()
        self._preserve_selection_for_rebuild()
        self._rebuild_grid()

    def hide_nsfw(self) -> str:
        return self._hide_nsfw

    def set_tag_indexes(self, tag_index, vector_index) -> None:
        """Re-inject freshly-opened tags.db readers without a restart (K01).

        The window calls this after re-opening ``tags.db`` (the AI-panel's
        「再読み込み」 button or the file-watcher).  It cancels any in-flight
        advanced scan, drops stale tag/vector results, re-points every surface
        that consumed the old indexes — the advanced-search panel
        (:meth:`refresh_tag_index_ui`), the NSFW ⋯-menu, the in-memory NSFW
        rating map, and the hover 「◇」 similar overlay — then rebuilds the grid.
        """
        # Stop any running query against the old reader and forget its results
        # (the tag/vector readers are about to be replaced / closed).
        self._advanced_search_cancel()
        self._advanced_search_drop_results()
        self._tag_search_enabled = False
        # 3 択モードは中立へ（項目 #93 — 書き込み口 1 本。索引が入れ替わると
        # rank / similar の可用性そのものが変わる）。
        self._set_ai_mode("and")
        self._tag_index = tag_index
        self._vector_index = vector_index
        # Re-apply the panel's has-index / has-vectors gating + scanner rebind.
        self.refresh_tag_index_ui(tag_index, vector_index)
        # The NSFW band menu is only usable with a tags.db supplying ratings; a
        # cached map keyed to the old DB is now meaningless.
        self._apply_nsfw_tagsdb_gate(tag_index is not None)
        # 「レーティングは世代非依存の事実」は tags.db が 1 つで不変のあいだ
        # だけ成り立つ前提で、DB そのものが入れ替わる本メソッドでは成立しない。
        # 解決器のワーカーは索引を**実行時に** ``self._tag_index`` から読むので、
        # 差し替えを跨いだバッチが見るのは新旧どちらか（索引が落ちた差し替えなら
        # 空 dict）で、どれも「投げたときの DB の答え」ではない。
        # 受付済み集合を空にする前に世代ごと切る（``_reset_curation_metadata``
        # と同じ規律）。切らないと、着地した旧値が空にしたばかりのマップへ
        # 復活し、しかも受付済み集合には入らないので ``_apply_nsfw_filter`` の
        # 「rating が None のときだけ再照会」条件を満たさず、そのパスは
        # セッション中二度と再解決されない。中断と帳簿の破棄は解決器の
        # :meth:`~.keyed_resolver.KeyedResolver.reset` が対で行う。
        self._nsfw_resolver.reset()
        self._nsfw_rating_map.clear()
        # The hover 「◇」 similar overlay needs a vector index; (dis)connect it to
        # match the new state (idempotent — a repeated connect is guarded).
        view = getattr(self, "_view", None)
        if view is not None:
            has_vec = vector_index is not None
            view.set_similar_overlay_enabled(has_vec)
            if has_vec and not self._similar_overlay_wired:
                view.similar_requested.connect(self._on_view_similar_requested)
                self._similar_overlay_wired = True
        # Re-kick (or tear down) the advanced-search worker for whatever query
        # survives the index swap.  Tag/semantic/similar were just disabled
        # above, but the media-type dimension (``_tag_media_type``) is NOT — a
        # non-image media search is an extension walk that never touches
        # tags.db, so it must keep running across a reload.  Without this the
        # cancelled scan is never restarted: ``_query_mode`` still reads
        # "media" (active) while ``_tag_results`` stays None, so the grid is
        # pinned empty with a "検索中…" status until the user touches a control
        # (item 2).  When nothing is active this clears the status and reverts
        # the grid to the normal direct-children view.
        self._maybe_start_tag_scan()
        self._preserve_selection_for_rebuild()
        self._rebuild_grid()

    def _apply_nsfw_tagsdb_gate(self, has_ratings: bool) -> None:
        """「年齢制限を隠す」の席とメニューを 1 対で開閉する（tags.db の有無）。

        メニューは席（``nsfw_btn``）に ``setMenu`` でぶら下がっているので、
        席が無効なままだとメニューを開く手段が無い。構築時と tags.db の再注入
        (:meth:`set_tag_indexes`) が同じ 1 実装を通ることで、片側だけ有効化して
        機能全体が再起動まで到達不能になる欠落を防ぐ（レビュー 0903 項目
        #121 — 再注入がメニューだけ有効化し、席は無効・ツールチップも
        「tags.db が必要です」のまま残っていた）。
        """
        reason = nsfw_filter.gate_tooltip(has_ratings)
        for name in ("nsfw_btn", "nsfw_menu"):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            widget.setEnabled(has_ratings)
            widget.setToolTip(reason)

    #: 年齢区分ボタンの現在値ラベル（席のラベル ⇄ メニュー項目の単一情報源）。
    #: 実体は :data:`~.nsfw_filter.LABEL_KEYS`。
    _NSFW_LABEL_KEYS = nsfw_filter.LABEL_KEYS

    def _sync_nsfw_menu(self) -> None:
        """Reflect the active NSFW band in the ⋯-menu radio actions.

        席のラベルも現在値へ差し替える (UIレビュー 2026-09-11 N-36): 設定は
        永続するのにボタンは常に「年齢制限を隠す」で、いま何が効いているかを
        メニューを開かないと確かめられなかった。中立値 (``off``) では素の
        文言へ戻す — 「隠さない」を常時出すと条件が効いて見える。
        """
        actions = getattr(self, "_nsfw_actions", None)
        if not actions:
            return
        for key, act in actions.items():
            act.setChecked(key == self._hide_nsfw)
        # ``ai_pack.available()`` が偽なら席そのものが無い（メニューと同じ
        # ガードの形）。
        btn = getattr(self, "nsfw_btn", None)
        if btn is None:
            return
        btn.setText(nsfw_filter.button_text(self._hide_nsfw))

    def _kick_nsfw_scan(
        self, folders: list[Path], files: list[Path],
    ) -> None:
        """Resolve the representative ratings of *folders* / *files* off-thread.

        Kicks are **additive, never superseding**: earlier in-flight tasks
        are NOT cancelled (the single-thread pool serialises them, and each
        batch is bounded to one viewport's unknown tiles against the local
        tags.db).  Cancelling would leave the cancelled task's paths parked
        in the resolver's 受付済み集合 without an answer ever arriving —
        navigating A→B→A then never re-resolved A, so its explicit tiles
        stayed visible for the whole session.  The shared session is cut only
        where the 受付済み集合 goes with it: :meth:`set_tag_indexes` (the
        answers of the old tags.db say nothing about the new one) and
        :meth:`shutdown`.  索引はワーカーが**実行時に** ``self._tag_index``
        から読むので、差し替えを跨いだバッチが見るのは新しい索引（索引が落ちた
        なら空 dict）で、どちらも ``set_tag_indexes`` の世代切りで捨てられる。
        ワーカーが例外で落ちたバッチの鍵は解決器が受付から
        解放するので、索引の一時的な失敗（tags.db のロック等）がそのパスを
        セッション中ずっと未解決に固定することはない。
        """
        if self._tag_index is None:
            return
        self._nsfw_resolver.request(
            [(str(p), (True, p)) for p in folders]
            + [(str(p), (False, p)) for p in files]
        )

    def _on_nsfw_ratings(self, ratings: object) -> None:
        # A rating is a generation-independent fact (path → dominant rating
        # in **that** tags.db), so every batch of the live session is merged —
        # even one submitted for a folder the user has already navigated away
        # from.  Additive kicks share the session's generation
        # (``submit_batch``), so the resolver's guard accepts the whole A→B→A
        # churn: discarding those while leaving their paths 受付済み is exactly
        # the leak that kept explicit tiles visible for a session.
        #
        # 世代が落ちるのは ``cancel`` が走ったときだけ = 受付済み集合ごと捨てる
        # 場面（``set_tag_indexes`` の DB 差し替え / ``shutdown``）で、そこで
        # 旧 DB の値を merge すると再照会されないまま固定される。
        if not isinstance(ratings, dict) or not ratings:
            return
        self._nsfw_rating_map.update(ratings)
        # Newly-known offenders can now be hidden — rebuild (selection kept).
        if self._hide_nsfw_bands():
            self._preserve_selection_for_rebuild()
            self._rebuild_grid()

    def _current_sort_spec(self):
        """``(key_fn, reverse)`` for the mode in effect — one construction site.

        Both orderings below (folders-first for browsing, flat for the overlay
        lists) read the sort from here, so a new sort key can never reach one
        and miss the other.
        """
        return _sort_spec(
            self._sort_mode, random_seed=self._random_sort_seed,
            star_of=self._star_of,
        )

    def _sorted_dir_first(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Explorer order: folders ahead of files, current sort within each."""
        key_fn, reverse = self._current_sort_spec()
        folders = [e for e in entries if e.is_dir]
        files = [e for e in entries if not e.is_dir]
        folders.sort(key=key_fn, reverse=reverse)
        files.sort(key=key_fn, reverse=reverse)
        return folders + files

    def _sorted_flat(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Pure current-mode sort — **no** folders-first grouping.

        UIレビュー 2026-08-28 N-12: the cross-library lists (and the 「最近追加
        されたファイル」 listing) are *about* one axis — ★ の高い順 / 更新日時の
        新しい順 — and entering one even forces that sort
        (``_CURATION_VIEW_SORT``).  Running the browse-grid's folders-first
        grouping as the last step silently overrode it: 「スター付き一覧」 came
        out as ★5フォルダ → ★1フォルダ → ★3ファイル, i.e. **not** in star
        order, which is the only thing that list exists to show.  These
        populations are flat pools that mix folders and files from all over the
        library, so "Explorer order" has nothing to group here anyway.  Plain
        browsing keeps :meth:`_sorted_dir_first` unchanged.
        """
        key_fn, reverse = self._current_sort_spec()
        return sorted(entries, key=key_fn, reverse=reverse)

    def reshuffle_random_sort(self) -> None:
        """Regenerate the 「ランダム」 sort seed (F5 / reload semantics).

        The window's reload handler calls this before re-scanning so a reload
        deals a fresh shuffle; within a session the seed is otherwise stable,
        keeping the random order steady across filter edits and metadata
        batches.  Cheap no-op for every other sort mode (the seed is simply
        unused).
        """
        self._random_sort_seed = random.randrange(1 << 30)

    # ------------------------------------------------ body: filter (worker)

    def _apply_body_filter(
        self, entries: list[FolderEntry], body_terms: list[_FilterTerm],
        *, or_confirmed: set[str] | None = None,
    ) -> list[FolderEntry]:
        """Gate *entries* on the async ``body:`` verdicts (kicking as needed).

        Returns only the entries the landed worker verdict admits.  While no
        verdict for the current body-term signature exists yet, kicks the
        worker (over ALL current entries, so the verdict set stays valid
        when non-body terms change) and returns ``[]`` — body-gated tiles
        appear once the worker reports, instead of freezing the GUI thread
        on cold post.md reads.

        When ``~body:`` pool members exist (#4), *or_confirmed* is the set
        of entry paths the sync-side ``~`` pool already admitted — those
        pass the pool without a body hit; every other entry must match at
        least one ``~body:`` member (the worker's *or_matched* set).  The
        signature includes ``or_group`` so flipping ``body:x`` ⇄ ``~body:x``
        never reuses a verdict computed for the other semantics.
        """
        sig = tuple((t.value, t.exclude, t.or_group) for t in body_terms)
        if self._body_filter_matches is not None and self._body_filter_sig == sig:
            # 別署名のワーカーが in-flight なら取り下げる (#104)。``body:x`` →
            # ``body:y`` → ``body:x`` と戻すとキャッシュ命中で即表示できるが、
            # y のタスクは走り続け、着地時に世代ガードを素通りして**有効な x の
            # 判定を y の判定で上書き**していた（直後の再構築が署名不一致を
            # 検知して x を再キック → グリッドが一瞬 0 件「検索中…」に落ちる）。
            if self._pending_body_sig.take_if(lambda cur: cur != sig) is not None:
                self._body_stream.cancel()  # 滞留中の emit を孤児化
            and_ok, or_hit = self._body_filter_matches
            has_or = any(t.or_group for t in body_terms)
            confirmed = or_confirmed or set()
            return [
                e for e in entries
                if str(e.path) in and_ok
                and (
                    not has_or
                    or str(e.path) in confirmed
                    or str(e.path) in or_hit
                )
            ]
        self._kick_body_scan(sig)
        return []

    def _kick_body_scan(self, sig: tuple) -> None:
        if self._pending_body_sig.peek() == sig:
            return  # this signature is already in flight
        self._pending_body_sig.set(sig)
        terms = [
            _FilterTerm("body", value, exclude, or_group)
            for value, exclude, or_group in sig
        ]
        self._body_stream.submit_job(
            lambda job, es=list(self._entries), ts=terms: body_filter_matches(
                es, ts, job.cancel
            )
        )

    def _on_body_filter_results(self, matched: object) -> None:
        # 着地したら成否によらず in-flight の帳簿を降ろす（兄弟 2 経路
        # ``_on_curation_entries`` / ``_on_recent_files`` と同じ位置）。残すと
        # ``_kick_body_scan`` が同じ署名を in-flight と誤認して二度と投げ直さず、
        # 0 タイル + 「検索中…」から出られなくなる。
        pending = self._pending_body_sig.take_if(always)
        if pending is None:  # pragma: no cover — 防御（到達しない）
            # 署名を降ろす 2 経路（別署名への取り下げ /
            # ``_invalidate_body_filter``）はどちらも直前に
            # ``_body_stream.cancel()`` を伴うので、その着地は ``bind`` の
            # 選別で既に落ちている。生きた分岐に見えないよう明示する。
            return
        if not isinstance(matched, tuple):
            # ワーカーが落ちた / 中断された（判定なし）。この署名については
            # 「該当なし」を確定させる — 再キックで同じ例外を繰り返す輪を作らず、
            # 画面は 0 件カード（＝絞り込みを解除する出口つき）へ落ち着く。
            # 語を変えれば署名が変わって解決を再試行する。
            matched = ((), ())
        self._body_filter_matches = (set(matched[0]), set(matched[1]))
        self._body_filter_sig = pending
        self._preserve_selection_for_rebuild()
        self._rebuild_grid()

    def _invalidate_body_filter(self) -> bool:
        """Drop landed / in-flight ``body:`` verdicts (entries changed).

        Returns whether ``body:`` terms are currently active in the filter
        box, so callers know a rebuild is needed to re-kick the worker.
        """
        self._body_stream.cancel()  # orphan any in-flight emit
        self._pending_body_sig.clear()
        self._body_filter_sig = None
        self._body_filter_matches = None
        return any(
            t.field == "body" for t in _parse_filter_query(self._filter_text)
        )

    # ------------------------------------------------- selection bookkeeping

    def _on_view_selection_changed(self, index: int) -> None:  # type: ignore[override]
        # Record plain-grid (no search engaged) selections so the
        # search-teardown restore knows what to go back to; selections made
        # among search results are deliberately not recorded (they belong to
        # the transient result view, and current_path() covers them at
        # teardown time).
        tile = self._view.tile_at(index)
        if (
            tile is not None
            and not self._filter_text
            and not self._advanced_search_active()
        ):
            self._presearch_selection = tile.path
        # The 「選択画像で探す」 button's enabled state + dynamic label track the
        # current selection (item 1-4d).
        self._update_similar_button_state()
        super()._on_view_selection_changed(index)

    def _preserve_selection_for_rebuild(
        self, *, ancestor_fallback: bool = False,
    ) -> None:
        """Queue the current selection for re-selection across a rebuild.

        The tag / vector workers emit up to twice per query (instant set, then
        the pruned / stat-enriched set) — without this, the phase-2 rebuild
        would drop the selection the user (or a queued ``pending_select``)
        made on the phase-1 tiles.  A pending selection that hasn't resolved
        yet takes precedence and is left untouched.

        ``ancestor_fallback=True`` is the search-teardown variant (filter
        cleared, サブフォルダ検索 / AIタグ検索 turned off): the selected item
        may be a deep descendant that won't exist on the rebuilt plain grid,
        so the resolve is allowed to land on its containing folder instead.
        """
        req = self._pending_select.peek()
        if req is None:
            cur = self._view.current_path()
            if cur is None and ancestor_fallback:
                # Nothing selected among the results (typing the filter
                # already dropped the plain-grid selection) — restore what
                # was selected before the search began.
                cur = self._presearch_selection
            if cur is None:
                return
            req = SelectRequest(cur, ancestor_fallback)
        elif ancestor_fallback:
            # 既に預かっている要求へ祖先フォールバックだけ足す（畳んだ 1 値
            # なのでパスと取り違えようがない）。
            req = req._replace(ancestor_ok=True)
        self._pending_select.set(req)

    # ---------------------------------------------- search state (nav history)

    def capture_search_state(self) -> SearchSnapshot:
        """Snapshot the filter box + recursive toggle + advanced-tag settings.

        Reuses ``save_tag_settings`` to serialise the tag panel into a scratch
        ``ViewerState`` so nothing about that (already-tested) round-trip is
        duplicated here.
        """
        scratch = ViewerState()
        self.save_tag_settings(scratch)
        # save_tag_settings never writes tag_search_enabled (volatile); force the
        # live activation flag onto the scratch so a nav-history restore faithfully
        # re-engages the captured AI-tag search (item 1-1: the flag is now an
        # internal auto-managed value, not a checkbox).
        scratch.tag_search_enabled = self._tag_search_enabled
        return SearchSnapshot(
            filter_text=self.filter_edit.text(),
            recursive=self.recursive_check.isChecked(),
            tag_state=scratch,
            # Semantic / similar-image ranking is session-only (not in
            # ViewerState) but still part of "the search we're leaving".
            ai_mode=self._ai_mode,
            similar_seed=self._similar_seed,
            filterbar_media=self._filterbar_media,
            filterbar_star_min=self._filterbar_star_min,
            filterbar_later=self._filterbar_later,
            filterbar_user_tag=self._filterbar_user_tag,
            filter_locked_only=self._filter_locked_only,
        )

    def restore_search_state(self, snap: SearchSnapshot) -> None:
        """Re-apply a previously captured search state and re-run its scans."""
        # 母集合の全面入れ替え（横断一覧と同じ扱い）— 窓は分割ビューへ戻る。
        self.population_replacing.emit()
        self.filter_edit.blockSignals(True)
        self.filter_edit.setText(snap.filter_text)
        self.filter_edit.blockSignals(False)
        self._filter_text = snap.filter_text.strip()

        self.recursive_check.blockSignals(True)
        self.recursive_check.setChecked(snap.recursive)
        self.recursive_check.blockSignals(False)
        self._recursive_search = snap.recursive

        # restore_tag_settings applies every tag_* control and kicks the tag
        # scan; _maybe_start_recursive_scan re-arms the recursive walk if the
        # checkbox + needle warrant it.  restore_enabled=True: unlike the
        # startup restore (enable is volatile), the session-internal history
        # restore must re-engage the captured search.
        self.restore_tag_settings(snap.tag_state, restore_enabled=True)

        # Semantic / similar-image ranking (only meaningful with vectors).
        # Set BEFORE the final _maybe_start_tag_scan so _query_mode() sees the
        # restored seed / enabled flag and dispatches the vector scanner; the
        # ranked results then re-flow through _resolve_pending_select, so the
        # previously-selected item is re-selected once it reappears.
        if self._vector_index is not None:
            # 書き込み口 1 本（項目 #93）— 記録されたモードが今の索引で走らない
            # なら ``_set_ai_mode`` が中立へ落とし、シードもそこで捨てられる。
            self._set_ai_mode(snap.ai_mode)
            if self._ai_mode == "similar":
                self._similar_seed = snap.similar_seed
            self._update_similar_clear_enabled()
            self._update_tag_controls_enabled()

        # Filter-bar in-place media predicate (item 2-2) — restore + sync the
        # bar's adaptive/accent state (the date preset was already restored by
        # restore_tag_settings above via restore_enabled=True).
        self._filterbar_media = snap.filterbar_media or "all"
        self._select_combo_data(self.filterbar_media_combo, self._filterbar_media)
        # Filter-bar curation predicates (H02) — restored only when the controls
        # exist (a user_meta store is wired); otherwise the flags stay inert.
        star_combo = getattr(self, "filterbar_star_combo", None)
        if star_combo is not None:
            self._filterbar_star_min = int(snap.filterbar_star_min or 0)
            self._select_combo_data(star_combo, self._filterbar_star_min)
        later_check = getattr(self, "filterbar_later_check", None)
        if later_check is not None:
            self._filterbar_later = bool(snap.filterbar_later)
            later_check.blockSignals(True)
            later_check.setChecked(self._filterbar_later)
            later_check.blockSignals(False)
        usertag_combo = getattr(self, "filterbar_usertag_combo", None)
        if usertag_combo is not None:
            # A saved search may name a tag that no longer exists anywhere;
            # ``_reload_user_tag_choices`` keeps it as a choice so the restored
            # dimension is visible (and removable) rather than silently dropped.
            self._filterbar_user_tag = str(snap.filterbar_user_tag or "")
            self._reload_user_tag_choices()
        # 「🔒 ロックありのみ」(review #21) — 検索次元の一つとして verbatim に
        # 復元する（clear_search_state が解除する軸は全て戻すのが snapshot の
        # 契約）。ハンドラ経由の再構築は末尾の _rebuild_grid が担う。
        self._filter_locked_only = bool(snap.filter_locked_only)
        self.locked_check.blockSignals(True)
        self.locked_check.setChecked(self._filter_locked_only)
        self.locked_check.blockSignals(False)
        self._update_filter_bar()

        self._maybe_start_recursive_scan()
        self._maybe_start_tag_scan()
        self._rebuild_grid()

    def clear_search_state(self) -> None:
        """Reset all search controls to inactive (drill-in navigation).

        No-op-safe: only touches controls that are actually engaged, so a
        normal drill-down with no active search costs nothing.
        """
        touched = False
        if self.filter_edit.text():
            self.filter_edit.blockSignals(True)
            self.filter_edit.clear()
            self.filter_edit.blockSignals(False)
            self._filter_text = ""
            touched = True
        if self.recursive_check.isChecked():
            self.recursive_check.blockSignals(True)
            self.recursive_check.setChecked(False)
            self.recursive_check.blockSignals(False)
            self._recursive_search = False
            touched = True
        if (
            self._tag_search_enabled or self._ai_mode != "and"
            or self._similar_seed or self.tag_input.text().strip()
        ):
            # The 「AIタグ検索」 gate checkbox is gone (item 1-1): clear the chips
            # (which also auto-disarms the flag) and drop the volatile flags.
            self.tag_input.blockSignals(True)
            self.tag_input.setText("")
            self.tag_input.blockSignals(False)
            self._tag_query_text = ""
            self._tag_search_enabled = False
            self._set_ai_mode("and")
            self._update_similar_clear_enabled()
            self._update_tag_controls_enabled()
            touched = True
        # The media-type / posted-date combos filter even without tag search on,
        # so a full clear has to neutralise them too (otherwise the grid would
        # keep showing e.g. "videos only" at the new root).  Panel expansion and
        # the folder-mode/coverage display toggles are left untouched — those
        # are view preferences, not part of the search query.
        if self._tag_media_type not in ("all", "image"):
            self._select_combo_data(self.tag_media_combo, "all")
            self._tag_media_type = "all"
            touched = True
        # A non-"all" age band now arms tag search on its own (the gate checkbox
        # is gone — item 1-1), so a full clear must neutralise it too.
        if self._tag_rating_key != "all":
            self._select_combo_data(self.tag_rating_combo, "all")
            self._tag_rating_key = "all"
            touched = True
        if self._tag_date_preset != "all":
            self._select_combo_data(self.tag_date_combo, "all")
            self._tag_date_preset = "all"
            self.tag_date_from.setVisible(False)
            self.tag_date_sep.setVisible(False)
            self.tag_date_to.setVisible(False)
            touched = True
        # Filter-bar in-place media predicate (item 2-2) — volatile, cleared
        # alongside the search so a full teardown returns to the plain grid.
        if self._filterbar_media != "all":
            self._select_combo_data(self.filterbar_media_combo, "all")
            self._filterbar_media = "all"
            touched = True
        # Filter-bar curation predicates (H02) — volatile, cleared with the rest.
        if self._filterbar_star_min > 0:
            self._select_combo_data(self.filterbar_star_combo, 0)
            self._filterbar_star_min = 0
            touched = True
        if self._filterbar_later:
            self.filterbar_later_check.blockSignals(True)
            self.filterbar_later_check.setChecked(False)
            self.filterbar_later_check.blockSignals(False)
            self._filterbar_later = False
            touched = True
        if self._filterbar_user_tag:
            self._select_combo_data(self.filterbar_usertag_combo, "")
            self._filterbar_user_tag = ""
            touched = True
        # ⋯-menu locked-only toggle — a query restriction like the rest, so a
        # full teardown (and the empty-state 解除 button) must neutralise it too.
        if self._filter_locked_only:
            self.locked_check.blockSignals(True)
            self.locked_check.setChecked(False)
            self.locked_check.blockSignals(False)
            self._filter_locked_only = False
            touched = True
        self._update_filter_bar()
        if not touched:
            return
        # Drop any in-flight / cached search results so the rebuilt grid shows
        # the plain direct children of the new root.  Mirrors ``set_root``'s
        # teardown: the rel-path caption maps and the ranked flag must go with
        # the results, or re-shown entries would keep stale relative-path
        # captions / skip sorting.
        self._recursive_debounce.stop()
        self._recursive_scanner.cancel()
        self._recursive_results = None
        self._rel_paths.clear()
        self._advanced_search_cancel()
        self._advanced_search_drop_results()
        # The optimistic 「詳細検索中…」 line may have been pushed by
        # _maybe_start_tag_scan just before this teardown (set_root re-arms the
        # scan for the new root, then the navigation clear lands here).  The
        # rebuild reconciler leaves the bar alone while the query is inactive,
        # so the busy line would otherwise outlive the search it announced.
        #
        # 占有一覧に **留まったまま** 条件だけ落とす経路（空状態カードの
        # 「絞り込みを解除」= :meth:`_clear_narrowing_over_overlay`）では、
        # 一覧が自分について語っていた行を消してはいけない — 再設定する経路が
        # 無いので二度と戻らなかった（#62 / 項目 #252）。退場も伴う経路
        # (:meth:`_on_banner_clear` / :meth:`_on_escape_clear`) は続く
        # ``_exit_overlay`` が改めて空へ倒すので、ここで戻しても残らない。
        overlay = self._overlay
        self._set_search_status("" if overlay is None else overlay.status)
        self._rebuild_grid()

    def _reapply_thumbnail(self, entry: FolderEntry) -> FolderEntry:
        """Recompute ``thumbnail_path`` / ``is_fallback_thumbnail`` for the
        current ``#thumb#``-exclude mode without touching the disk.

        Files (``is_dir=False``) and unresolved entries are returned
        unchanged — the candidate fields are only populated by the
        metadata pass on directories.
        """
        if not entry.is_dir or not entry.thumbnail_resolved:
            return entry
        chosen, is_fallback = select_thumbnail(
            entry.thumb_marker_path,
            entry.non_thumb_marker_path,
            exclude_thumb_marker=self._exclude_thumb_marker,
        )
        if (
            chosen == entry.thumbnail_path
            and is_fallback == entry.is_fallback_thumbnail
        ):
            return entry
        # dataclasses.replace keeps every untouched field (file_names, plan
        # fields, …) intact — a hand-rebuilt FolderEntry here once silently
        # dropped file_names, breaking the attachment-name filter whenever
        # the #thumb#-exclude toggle re-picked thumbnails.
        return replace(
            entry,
            thumbnail_path=chosen,
            thumbnail_resolved=True,
            is_fallback_thumbnail=is_fallback,
        )

    # -------------------------------------------------------------- slots

    def _on_filter_changed(self, text: str) -> None:
        new = text.strip()
        if self._filter_text and not new:
            # 検索欄クリア: re-select what was selected in the results — or,
            # for a deep recursive hit that vanishes from the plain grid, the
            # shown folder containing it.
            self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._filter_text = new
        self._sync_filter_control_tokens()
        self._maybe_start_recursive_scan()
        self._rebuild_grid()

    def _sync_filter_control_tokens(self) -> None:
        """Reflect ``type:`` / ``rating:`` / ``score:`` filter-box tokens into
        the matching GUI controls (the single source of truth per dimension).

        The token is treated as *equivalent to operating the control*: parsing
        it drives the filter-bar 種別 combo, the AI-panel 年齢区分 band and the
        AI-tag 精度 spin — never a second, parallel predicate — so a token and
        its combo can't double-apply or disagree.  Only a dimension the query
        actually names is overridden; an absent token leaves the control as the
        user left it (so this never fights the combos on an unrelated edit).
        Runs only on interactive filter edits (:meth:`_on_filter_changed`), so
        changing a combo directly is not clobbered until the box is edited.

        ``rating:`` / ``score:`` require a tags.db (the AI dimensions) — without
        one they are inert (see :func:`.search_dimensions.filter_help_html`).
        Combos are updated
        with signals blocked and the AI worker is re-kicked explicitly, so this
        never recurses through a control handler.
        """
        ctl = parse_control_tokens(self._filter_text)
        ai_changed = False
        # 種別 (filter-bar in-place media predicate) — always available.
        if ctl.media is not None and ctl.media != self._filterbar_media:
            self._filterbar_media = ctl.media
            self._select_combo_data(self.filterbar_media_combo, ctl.media)
        # 年齢区分 / 精度 drive the AI search — only meaningful with a tags.db.
        if self._tag_index is not None:
            if ctl.rating is not None and ctl.rating != self._tag_rating_key:
                self._tag_rating_key = ctl.rating
                self._select_combo_data(self.tag_rating_combo, ctl.rating)
                # A non-"all" band arms tag mode on its own (mirrors the combo
                # handler ``_on_tag_rating_changed``).
                includes, excludes = _parse_query(self._tag_query_text)
                self._tag_search_enabled = bool(
                    includes or excludes or self._tag_rating_key != "all"
                )
                ai_changed = True
            if ctl.score is not None:
                floor = self.tag_threshold_slider.minimum()
                clamped = max(floor, min(1.0, ctl.score))
                if abs(clamped - self._tag_threshold) > 1e-9:
                    self._tag_threshold = clamped
                    self.tag_threshold_slider.blockSignals(True)
                    self.tag_threshold_slider.setValue(clamped)
                    self.tag_threshold_slider.blockSignals(False)
                    ai_changed = True
        # ★ / ユーザータグ / あとで見る (UIレビュー 07-25 #62 / N-65) — same
        # contract as ``type:`` above: a typed ``star:>=3`` / ``mytags:タグ`` /
        # ``later:yes`` now *operates the control* instead of living as a
        # second, invisible predicate beside it.
        star_min, later, user_tag, _synced = self._parse_curation_control_tokens(
            self._filter_text
        )
        star_combo = getattr(self, "filterbar_star_combo", None)
        if (
            star_min is not None and star_combo is not None
            and star_min != self._filterbar_star_min
        ):
            self._filterbar_star_min = star_min
            self._select_combo_data(star_combo, star_min)
        usertag_combo = getattr(self, "filterbar_usertag_combo", None)
        if (
            user_tag is not None and usertag_combo is not None
            and user_tag != self._filterbar_user_tag
        ):
            self._filterbar_user_tag = user_tag
            # コンボの品揃えは「開くたび詰め直し」方式なので、選択反映も
            # ``_reload_user_tag_choices`` を通す（今の品揃えに無いタグでも
            # 先頭へ温存される — シグナルはブロック済み）。
            self._reload_user_tag_choices()
        later_check = getattr(self, "filterbar_later_check", None)
        if (
            later is not None and later_check is not None
            and later != self._filterbar_later
        ):
            self._filterbar_later = later
            later_check.blockSignals(True)
            later_check.setChecked(later)
            later_check.blockSignals(False)
        if ai_changed:
            self._maybe_start_tag_scan()
        self._update_filter_bar()

    #: Curation filter fields the フィルタ popover can express 1:1
    #: (UIレビュー 07-25 #62).  ``mytags:`` joined in N-65 (第3段): its combo
    #: has existed since 07-25 #13②, so the old exclusion reason ("no
    #: control") was stale — only the *matching-rule gap* remains, and that is
    #: resolved per-value by :meth:`_syncable_curation_value` (exact-match
    #: values sync, substring-only values stay pure text terms).
    _SYNCED_CURATION_FIELDS = frozenset({"star", "mytags", "later"})

    #: ``star:>=3`` / ``star:>2`` — the only two forms the ★N以上 combo can
    #: represent.  ``star:=5`` / ``star:3`` / ``star:<2`` mean something the
    #: combo cannot say, so they stay text-only (see
    #: :meth:`_parse_curation_control_tokens`).
    #:
    #: 桁数を縛るのは ``int()`` のため: Python は 4,300 桁を超える 10 進文字列
    #: の変換を ``ValueError`` で拒む（整数変換の DoS 対策）ので、無制限の
    #: ``\d+`` は「打った文字列で絞り込みの適用が途中で止まる」経路になる。
    #: ★は 1〜5 なので 18 桁あれば表現力は一切落ちない。
    _STAR_FLOOR_RE = re.compile(r"^(>=|>)(\d{1,18})$")

    def _syncable_curation_value(self, field: str, value: str):
        """``star:`` / ``mytags:`` / ``later:`` トークンの値 → コントロールが
        表せる値 (or ``None``).

        UIレビュー07-25 追修: 「そのトークンをコントロールで言い換えられるか」の
        判定を 1 か所に集約する — :meth:`_parse_curation_control_tokens`（読む
        側）と :meth:`_strip_synced_curation_token`（コントロール操作で書き換える
        側）が別々の条件を持つと、両者がずれた瞬間にトークンが**見えないのに
        効いている**状態になる（それがこの修正の元不具合）。

        N-65（第3段）: ``mytags:`` の同期の意味論 — トークンは連結タグ列への
        casefold **部分一致**、コンボは**完全一致**で照合規則が違う。同期する
        のは既存のユーザータグと casefold 完全一致する値だけ（返すのはストア
        の正準表記 — コンボの data 値と同一物）。部分一致にしかならない値は
        従来どおり非同期のテキスト項として残る — ``star:>=3`` だけがコンボへ
        写り ``star:=5`` が残るのと同じ「表現可能なときだけ同期」の規約。
        完全一致形は「トークンの部分一致集合 ⊇ コンボの完全一致集合」なので、
        トークンが照合に残っても AND は no-op（結果はコンボ単独と等価）。
        """
        if field == "star":
            m = self._STAR_FLOOR_RE.match(value.strip())
            if m is None:
                return None
            floor = int(m.group(2)) + (1 if m.group(1) == ">" else 0)
            return floor if 1 <= floor <= 5 else None
        if field == "mytags":
            needle = value.strip().casefold()
            if not needle:
                return None
            for tag in self.all_user_tags():
                if str(tag).casefold() == needle:
                    return str(tag)
            # 選択中だがストアから消えたタグ（``_reload_user_tag_choices`` が
            # 選択肢として温存するのと同じ配慮 — engaged な絞り込みを黙って
            # 解除しない）。
            current = self._filterbar_user_tag
            if current and current.casefold() == needle:
                return current
            return None
        if field == "later":
            return True if value.strip() in ("yes", "true", "1", "on") else None
        return None

    def _strip_synced_curation_token(self, field: str) -> None:
        """コントロールが表せる形の ``field:`` トークンだけを検索文字列から消す。

        UIレビュー07-25 追修: フィルタポップオーバーの ★ コンボ /
        「あとで見る」 を操作したら、同じ次元を指すトークンは**その操作で
        置き換わった**ものとして削除する（``_clear_dim_later`` が × に対して
        既にやっている契約を、コントロール本体にも広げる）。これが無いと
        ``later:yes`` と打ってからチェックを外したとき、チップからは消えるのに
        ``_match_filter_terms`` では AND され続け、解除する導線がどこにも
        無くなる。

        コントロールが言い換えられない形（``star:=5`` / ``star:<2`` /
        ``later:no`` / ``-`` 除外 / ``~`` OR 要素）は**触らない** — それらは
        ただのテキスト項として 絞り込みチップに出続けるのが正しい。
        """
        cur = self.filter_edit.text()
        kept: list[str] = []
        for raw in cur.split():
            if raw[:1] not in ("~", "-"):
                head, sep, value = raw.partition(":")
                if (
                    sep
                    and head.casefold() == field
                    and self._syncable_curation_value(field, value.casefold())
                    is not None
                ):
                    continue
            kept.append(raw)
        new = " ".join(kept)
        if new == cur:
            return
        self.filter_edit.blockSignals(True)
        self.filter_edit.setText(new)
        self.filter_edit.blockSignals(False)
        self._filter_text = new.strip()

    def _parse_curation_control_tokens(
        self, text: str,
    ) -> tuple[int | None, bool | None, str | None, frozenset[str]]:
        """``star:`` / ``mytags:`` / ``later:`` tokens →
        ``(star_min, later, user_tag, synced_fields)``.

        The ``type:`` control token is the template (UIレビュー 07-25 #62): a
        token the GUI can represent is treated as *equivalent to operating the
        control*, so the ★ combo / ユーザータグ combo / 「あとで見る」 check
        show what was typed instead of silently AND-ing a second copy of the
        same restriction.

        Only the syncable forms count — ``star:>=N`` / ``star:>N`` (a floor, =
        the combo's ★N以上), an affirmative ``later:``, and a ``mytags:`` whose
        value casefold-equals an existing user tag (N-65 — the combo is
        exact-match, so only exact-match values are expressible; ``user_tag``
        carries the store's canonical spelling).  Everything the controls
        cannot say (``star:=5``, ``star:<2``, ``later:no``, a substring-only
        ``mytags:``, any ``-`` exclusion or ``~`` OR member) is left alone as
        an ordinary text term.  *synced_fields* names the fields that DID map
        onto a control, so the chip bar can drop them from the 絞り込み chip
        (they own their own chip) exactly like ``type:``.

        Unlike ``type:``, the token also keeps matching inside
        ``_match_filter_terms`` — but it now says exactly what the control says,
        so the extra AND is a no-op rather than a divergence (for ``mytags:``
        the combo's exact-match narrowing is a subset of the token's substring
        match, so the AND collapses to the combo's semantics).

        UIレビュー07-25 追修: *synced* は「その形が表せる」だけでなく
        **コントロールの現在値と実際に一致している**ときにだけ立てる。値がずれた
        トークン（コントロールを操作した直後など）は 絞り込みチップに出し続け、
        「見えないのに AND されている」状態を作らない。
        """
        star_min: int | None = None
        later: bool | None = None
        user_tag: str | None = None
        synced: set[str] = set()
        have_star = getattr(self, "filterbar_star_combo", None) is not None
        have_later = getattr(self, "filterbar_later_check", None) is not None
        have_usertag = (
            getattr(self, "filterbar_usertag_combo", None) is not None
        )
        for term in _parse_filter_query(text):
            if term.field not in self._SYNCED_CURATION_FIELDS:
                continue
            if term.exclude or term.or_group:
                continue
            value = self._syncable_curation_value(term.field, term.value)
            if value is None:
                continue
            if term.field == "star":
                if not have_star:
                    continue
                star_min = value
                if value == self._filterbar_star_min:
                    synced.add("star")
            elif term.field == "mytags":
                if not have_usertag:
                    continue
                user_tag = value
                if value == self._filterbar_user_tag:
                    synced.add("mytags")
            else:
                if not have_later:
                    continue
                later = value
                if self._filterbar_later:
                    synced.add("later")
        return star_min, later, user_tag, frozenset(synced)

    def _plain_filter_label(self, text: str) -> str:
        """*text* minus every token that already owns its own condition chip.

        ``type:`` / ``rating:`` / ``score:`` always, plus the ``star:`` /
        ``mytags:`` / ``later:`` tokens that mapped onto a filter-popover
        control this time round (UIレビュー 07-25 #62 / N-65) — otherwise the
        same restriction would be both a ★ / ユーザータグ chip and part of the
        絞り込み chip.

        「所有している」 は次元あたり 1 トークンまで: 同じ次元を 2 度名指し
        した ``type:image type:video`` は後勝ちで適用されるので、負けた側は
        どのコントロールにも届かない — 落とすと適用も照合も表示もされない
        入力になるため、``type:foo`` / ``-type:video`` と同じくチップに残す
        （レビュー 2026-08-27 #162）。値をコントロールが言い換えられない
        ``star:=5`` のような形も同じ理由で残る。

        ``rating:`` / ``score:`` は **tags.db があるときだけ**コントロールへ
        届く（:meth:`_sync_filter_control_tokens`）。索引が無い構成で「所有
        している」と宣言すると、打ったトークンが適用も照合もされないまま
        絞り込みチップからも消える — #162 が名指しで禁じた形そのもの。
        所有する軸は台帳（``token_fields``）から引く（手書きの列挙を作らない）。
        """
        _star, _later, _tag, synced = self._parse_curation_control_tokens(text)
        owned = set(_CONTROL_FIELDS)
        if self._tag_index is None:
            owned -= {
                tok.field for tok in token_fields()
                if tok.control and tok.owner == "ai"
            }
        return strip_owned_control_tokens(
            text,
            owned | set(synced),
            lambda field, value: self._syncable_curation_value(
                field, value.casefold()
            ) is not None,
        )

    def _on_recursive_toggled(self, checked: bool) -> None:
        if checked == self._recursive_search:
            return
        if not checked:
            # Deep descendant hits leave the grid — keep the selection (or
            # its containing folder) rather than dropping it.
            self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._recursive_search = checked
        # フィルターポップオーバーの行になったので、他の軸と同じく効いている
        # 間はアクセントを付ける（``_filter_row_engaged`` の "recursive"）。
        self._update_filter_bar()
        self._maybe_start_recursive_scan()
        self._rebuild_grid()

    def _set_search_status(self, text: str) -> None:
        """Push *text* to the bottom-left status bar via
        ``search_status_changed``.  Empty string clears the recursive-search
        indicator (the status bar falls back to the load state)."""
        self.search_status_changed.emit(text)

    def _set_overlay_status(self, text: str) -> None:
        """占有一覧が **自分について** 語る状態行を出し、一覧へ覚えさせる。

        「最近追加されたファイル — N 件」/「N 件は見つかりませんでした（移動
        または削除済み）」は、一覧に入った時点で **一度だけ** 組まれる告知で、
        再構築の経路には再導出が無い。一覧の上に載せた条件の解除
        (:meth:`clear_search_state`) は末尾で状態行を無条件に空へ倒すので、
        覚えていないと「一覧には留まったまま件数行だけ消えて二度と戻らない」
        （レビュー 2026-08-27 #62 / 2026-09-03 項目 #252）。
        :meth:`_maybe_start_recursive_scan` の非活性分岐が同じ告知をオーバー
        レイ中だけ守っている（#165）のと対の措置で、覚える場所は一覧の状態
        そのもの（``OverlayList.status``）— 退場すれば一緒に消える。
        """
        overlay = self._overlay
        if overlay is not None:
            overlay.status = text
        self._set_search_status(text)

    def _emit_search_progress_status(self) -> None:
        """Compose the in-flight "検索中…" line from the current counters.

        Cheap string assembly only — called on cache-seed and on throttled
        progress ticks (the walker emits at most once per ~1000 entries), so
        it never becomes a display-side load even on a deep NAS tree.
        """
        parts = [t("viewer.post_grid.recursive_searching")]
        if self._recursive_seed_count:
            parts.append(
                t("viewer.post_grid.recursive_cache_hits",
                  n=self._recursive_seed_count)
            )
        if self._recursive_scanned:
            parts.append(
                t("viewer.post_grid.recursive_scanned", n=self._recursive_scanned)
            )
        self._set_search_status(" / ".join(parts))

    def _on_recursive_progress(self, generation: int, scanned: int) -> None:
        if generation != self._recursive_scanner.latest_generation():
            return  # stale walk superseded by newer filter text
        if not self._recursive_scanning:
            return
        self._recursive_scanned = scanned
        self._emit_search_progress_status()

    def _on_recursive_walk_incomplete(self, generation: int, unreadable: int) -> None:
        """列挙できなかったサブツリーがあった（``results_ready`` の直前に届く）。

        最近追加一覧の ``RecentFilesScan.unreadable_dirs`` と同じ趣旨 — 穴の
        空いた走査を「N 件」だけで見せない・0 件を「該当なし」と断定しない
        （レビュー 2026-09-03 項目 #64）。
        """
        if generation != self._recursive_scanner.latest_generation():
            return  # stale walk superseded by newer filter text
        self._recursive_unreadable = unreadable

    def _on_recursive_walk_truncated(self, generation: int, cap: int) -> None:
        """上限に達して走査を打ち切った（``results_ready`` の直前に届く）。

        打ち切りを黙ると「N 件見つかりました」が全件の顔をする — 探し物が
        上限の外に居る可能性を利用者へ返せない（``scan_partial`` と同じ
        「穴を告げる」規律）。
        """
        if generation != self._recursive_scanner.latest_generation():
            return  # stale walk superseded by newer filter text
        self._recursive_truncated = cap

    def _maybe_start_recursive_scan(self) -> None:
        """Kick (or cancel) the recursive descendant walk based on state.

        Only runs while the checkbox is on AND at least one positive
        term (an AND include or a ``~`` OR-pool member) is present: a
        recursive walk with nothing positive
        to match would have to enumerate everything just to apply
        exclusions, which on a deep NAS tree is wasteful.  Cancellation
        clears the previous results so the grid re-renders without stale
        descendant hits.
        """
        includes, _, or_pool = self._general_filter_terms()
        active = (
            self._recursive_search
            and bool(includes or or_pool)
            and self._root_or_folder is not None
            # The advanced-search panel owns the grid (and the status bar) when
            # active — don't run a competing descendant walk underneath it.
            and not self._advanced_search_active()
            # The cross-library curation list (H01) and the 「最近追加された
            # ファイル」 listing own the grid too; typing in the filter box
            # narrows them in-memory, not via a walk.
            and self._overlay is None
            # 🔒「ロックありのみ」中は子孫を 1 件も採れない
            # (:meth:`_apply_filter_and_sort` が捨てる。理由は同じ —
            # 子孫は post.md を読まないので locked_count を持たない)。
            # 蹴る側が知らないと、コールドな NAS で分オーダーの走査を丸ごと
            # 捨てたうえで「該当なし」と告げることになる。広げても救えない
            # ことは :meth:`_can_widen_to_subfolders` が既に知っている。
            and not self._filter_locked_only
        )
        if not active:
            self._recursive_debounce.stop()
            self._recursive_scanner.cancel()
            self._recursive_scanning = False
            self._recursive_scanned = 0
            self._recursive_seed_count = 0
            self._recursive_unreadable = 0
            self._recursive_truncated = 0
            # Leave the status bar alone when the advanced-search panel owns it
            # (it sets its own "✓ タグ検索 …" line) or an overlay listing does —
            # 最近追加 の 「N 件中 M 件」 summary, 横断キュレーション一覧の
            # 「一覧を読み込み中…」/「N 件は見つかりませんでした（移動または削除
            # 済み）」。横断一覧が守られていなかったため（レビュー 2026-08-27
            # #165）、消えた★の唯一の告知が絞り込み 1 文字で消え、履歴復帰でも
            # 解決待ちの表示が即座に潰れていた。otherwise clear back to the
            # load state.
            if (
                not self._advanced_search_active()
                and self._overlay is None
            ):
                self._set_search_status("")
            if self._recursive_results is not None:
                self._recursive_results = None
                self._rel_paths.clear()
            return
        # Optimistically mark "searching" through the debounce window so the
        # status bar reacts to the first keystroke, not 250 ms later.
        self._recursive_scanning = True
        self._recursive_scanned = 0
        self._recursive_seed_count = 0
        self._recursive_unreadable = 0
        self._recursive_truncated = 0
        self._emit_search_progress_status()
        self._recursive_debounce.trigger()

    def _kick_recursive_scan(self) -> None:
        if self._root_or_folder is None:
            return
        includes, excludes, or_pool = self._general_filter_terms()
        if not includes and not or_pool:
            return
        # Record the query these results will correspond to: a walk that
        # finishes after the user has edited the needle (but before the next
        # debounced kick re-issues) must not be mistaken for current matches.
        self._pending_query = (tuple(includes), tuple(excludes), tuple(or_pool))
        # Both phases — the instant cache seed and the authoritative live walk
        # — now run on the scanner's worker thread (index query, descendant
        # filtering and FolderEntry construction all off the GUI thread), so
        # the main thread no longer does the O(candidates) work that froze the
        # UI just before results appeared.  Matches arrive via
        # _on_recursive_results (seed first, then live).
        self._recursive_scanner.request(
            self._root_or_folder, includes, excludes, or_terms=or_pool,
        )

    def _on_recursive_results(
        self,
        generation: int,
        results: list[tuple[FolderEntry, str]],
        is_seed: bool,
    ) -> None:
        if generation != self._recursive_scanner.latest_generation():
            return
        # Dropped if the user toggled the checkbox off or cleared the positive
        # query meanwhile — _maybe_start_recursive_scan already reset state and
        # showing these would re-introduce hits the user no longer wants.
        includes, _, or_pool = self._general_filter_terms()
        if not self._recursive_search or not (includes or or_pool):
            return
        # Results are already query-filtered on the worker thread; tag them
        # with the query they were computed for so _apply_filter_and_sort uses
        # them verbatim (no GUI-thread re-scan) only while it still matches.
        self._recursive_results = results
        self._recursive_results_query = self._pending_query
        self._rel_paths = {str(entry.path): rel for entry, rel in results}
        # 再帰検索も 1 クエリで最大 2 回着地する（シード → 権威あるライブ）ので、
        # タグ / ベクトルワーカーと同じく再構築の前に選択を預ける。シードのヒット
        # を選んだ直後にライブが着地すると、選択だけ落ちてプレビュー列は前の項目
        # を映したまま（現在地が 2 つに割れる）になる — レビュー 0903 項目 #103。
        self._preserve_selection_for_rebuild()
        if is_seed:
            # Instant cache phase: keep the "検索中…" status, record the seed
            # hit count, and paint what we have while the live walk continues.
            self._recursive_seed_count = len(results)
            self._emit_search_progress_status()
            self._rebuild_grid()
            return
        # Authoritative live walk finished.
        self._recursive_scanning = False
        self._rebuild_grid()
        hits = self._descendant_match_count
        # UIレビュー 07-25 #115: 0 件は「完了 ✓」ではない — 絵文字を外したうえで
        # 件数ゼロ専用の「該当なし」へ分岐する（成功記号で失敗を語らない）。
        # 読めなかったサブツリーがあるなら「該当なし」と断定しない — 「読めな
        # かった」を「無かった」と言う読み方は N-09 が横断一覧・最近追加一覧で
        # 明示的に禁じたもの（レビュー 2026-09-03 項目 #64）。
        if hits:
            summary = t("viewer.post_grid.recursive_done", hits=hits)
            if self._recursive_unreadable:
                summary = " / ".join(
                    [summary, t("viewer.post_grid.scan_partial")]
                )
            if self._recursive_truncated:
                summary = " / ".join([
                    summary,
                    t(
                        "viewer.post_grid.recursive_done_truncated",
                        n=self._recursive_truncated,
                    ),
                ])
        elif self._recursive_unreadable:
            summary = t("viewer.post_grid.recursive_done_unreadable")
        else:
            summary = t("viewer.post_grid.recursive_done_none")
        if self._recursive_scanned:
            summary += t(
                "viewer.post_grid.recursive_done_scanned",
                n=self._recursive_scanned,
            )
        self._set_search_status(summary)

    def _search_engaged(self) -> bool:
        """Any dimension Esc-clearable via :meth:`_on_escape_clear` is on."""
        return (
            self._overlay is not None
            or bool(self.filter_edit.text())
            or self.recursive_check.isChecked()
            or self._advanced_search_active()
            or self._ai_mode != "and"
            # 🔒 も含めフィルターポップオーバーの軸は 1 述語から
            # （:meth:`_filter_bar_engaged` が表の導出になった）。
            or self._filter_bar_engaged()
        )

    def _overlay_narrowing_engaged(self) -> bool:
        """Whether anything narrows an overlay listing's own population.

        :meth:`_search_engaged` answers ``True`` merely because the overlay is
        open, so it cannot tell 「一覧を絞り込んでいる」 from 「一覧を開いている」.
        This is the narrower question the empty-state card needs — and it is
        now DERIVED from the view-dimension table (項目#63/#65): exactly the
        engaged dimensions whose ``scopes`` include ``"overlay"``, i.e. the
        ones :meth:`_apply_overlay_view` actually applies.  The hand-written
        list used to count locked / 投稿日, which the overlay never applies —
        misclassifying an R18-suppressed listing as ``recent_filtered``.
        """
        return any(
            "overlay" in dim.scopes and dim.engaged()
            for dim in self._view_dimensions()
        )

    def search_engaged(self) -> bool:
        """Public read of :meth:`_search_engaged` for the hosting window.

        UIレビュー 07-25 #118: main_window needs "a search is applied AND the
        grid settled empty" to switch the right panel's placeholder to the
        検索 0 件 wording — the same predicate Esc uses, so the two can never
        disagree about what "検索中" means.
        """
        return self._search_engaged()

    def _on_escape_clear(self) -> None:
        """Escape in the grid clears the active filter / search in one go.

        Delegates to :meth:`clear_search_state` (the same teardown 戻る uses),
        which is no-op-safe: it only touches controls that are actually engaged,
        so pressing Escape with nothing filtered costs nothing.  A deep search
        hit that vanishes from the plain grid falls back to its containing
        folder for the re-selection.
        """
        # The cross-library curation list (H01) and the 「最近追加されたファイル」
        # listing are overlays — Esc leaves the active one.  The narrowing the
        # user loaded ON the listing comes down with it (レビュー 2026-08-27
        # #166): ``clear_search_state`` runs on *entry*, so it says nothing
        # about conditions added afterwards, and leaving them standing silently
        # applies them to the plain grid the user lands back on — with the
        # 絞り込み chip suppressed (#15) the only remaining clue is the 件数
        # 表記.  `_on_banner_clear` already tears the overlay narrowing down
        # for exactly this reason; the two 「解除」 routes must not disagree.
        if self._overlay is not None:
            self._preserve_selection_for_rebuild(ancestor_fallback=True)
            self.clear_search_state()
            self._exit_overlay()
            return
        if self._search_engaged():
            self._preserve_selection_for_rebuild(ancestor_fallback=True)
            self.clear_search_state()

    def _on_locked_filter_toggled(self, checked: bool) -> None:
        """「ロックありのみ」 toggled in the フィルタ popover (UIレビュー 07-25 #40).

        A *search* dimension, so it is volatile like the rest of them (see
        ``state.filter_locked_only``, which is now read-and-discarded on load)
        and it preserves the selection across the rebuild exactly like the
        other filter-popover axes.
        """
        self._filter_locked_only = checked
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._update_filter_bar()
        # 子孫走査の可否が切り替わる軸なので、投入判定を掛け直す — ON では
        # 非活性分岐が走査を止めて状態行を空へ倒し（「該当なし」の言い残しを
        # 消す）、OFF では同じ語のまま走査を蹴り直す。
        self._maybe_start_recursive_scan()
        self._rebuild_grid()

    # ------------------------------------------------- condition chip bar

    def _build_condition_bar(self) -> ConditionBar:
        """Build the window-level condition chip bar.

        Returned **parentless** like the toolbar; ``main_window._build_ui``
        mounts it directly under the toolbar.  The widget itself lives in
        :mod:`.condition_bar` (組み直し・折り畳み・× の当たり判定); what this
        pane keeps is the 4 callbacks that cross back into its own state.
        """
        return ConditionBar(
            on_edit=self._edit_condition,
            on_clear=self._clear_condition_dimension,
            on_save=self._emit_save_search_requested,
            on_clear_all=self._on_banner_clear,
        )

    def _emit_save_search_requested(self) -> None:
        """[この検索を保存…] を窓へ渡す（``Signal.emit`` を配線先にしない）."""
        self.save_search_requested.emit()

    def _combo_label(self, name: str) -> str:
        """コンボの**表示文字列**（未構築なら空）— 条件チップの値部分。

        値キーから引き直さずコンボから写すのは、それが利用者の目に見えている
        語そのものだから（投稿日の「期間指定」のように、キーだけでは復元
        できない文言を持つ軸がある）。
        """
        combo = getattr(self, name, None)
        return combo.currentText() if combo is not None else ""

    def _condition_state(self) -> condition_chips.ConditionState:
        """条件チップの判定材料を 1 つの値へ束ねる — **観測時点はここ**。

        束ねてから描くまでの間に状態を動かさないこと（束ねた値で描いておき
        ながら実際のグリッドは別の条件、という時点適用を作らない）。呼び出し
        側はどれも同期の 1 続き（``_view_dimensions`` → 即座に閉包を呼ぶ /
        ``_update_condition_bar`` → 即座に描く）なので、旧実装が閉包ごとに
        直読みしていた時点と同じになる。

        フィルタバーの 8 軸は :meth:`_filter_bar_criteria` の値束ねをそのまま
        内包する（同じ軸の 2 つ目の束ねを作らない）。
        """
        ai_on = ai_pack.available()
        includes, excludes = _parse_query(self._tag_query_text)
        # 精度の中立点はスライダ下限を読むので、AI ポップオーバーが建って
        # いない構成では触らない（AI 行はそのとき表ごと消えるので使われない）。
        slider = getattr(self, "tag_threshold_slider", None)
        return condition_chips.ConditionState(
            bar=self._filter_bar_criteria(),
            ai_available=ai_on,
            query_mode=self._query_mode() if ai_on else None,
            ai_includes=tuple(includes),
            ai_excludes=tuple(excludes),
            similar_seed_name=(
                Path(self._similar_seed).name if self._similar_seed else ""
            ),
            tag_threshold=self._tag_threshold,
            precision_neutral=(
                self._precision_neutral() if slider is not None else 0.0
            ),
            display_unit=self._display_unit_key(),
            ai_media=self._tag_media_type,
            plain_filter=self._plain_filter_label(self._filter_text),
            rating_label=self._combo_label("tag_rating_combo"),
            media_label=self._combo_label("filterbar_media_combo"),
            date_label=self._combo_label("tag_date_combo"),
            unit_label=self._combo_label("tag_display_unit_combo"),
            ai_media_label=self._combo_label("tag_media_combo"),
            view_scope=self._current_view_scope(),
            overlay_label=(
                self._overlay.chip_label() if self._overlay is not None else ""
            ),
        )

    def _condition_clear_callbacks(self) -> dict[str, Callable[[], None]]:
        """条件チップの × が戻る先 ``{次元 id: 中立化}``.

        **不変**: 鍵は :data:`~.condition_chips.ACTION_IDS` と集合一致すること
        （片側だけ増えると「押せるのに何も起きない ×」が戻る）。AI パック
        無効時も縮まない — ``_clear_dim_*`` はメソッドとして常に在り、消えるの
        はチップの側だけ。表は :meth:`_view_dimensions` の ``clear`` 列にも
        そのまま渡るので、× と [詳細条件のみリセット] は 1 実装を共有する。
        """
        return {
            "ai_semantic": self._clear_dim_semantic,
            "ai_tags": self._clear_dim_tags,
            "ai_precision": self._clear_dim_precision,
            "ai_unit": self._clear_dim_unit,
            "ai_media": self._clear_dim_ai_media,
            "rating": self._clear_dim_rating,
            "media": self._clear_dim_media,
            "star": self._clear_dim_star,
            "later": self._clear_dim_later,
            "usertag": self._clear_dim_usertag,
            "date": self._clear_dim_date,
            "locked": self._clear_dim_locked,
            "recursive": self._clear_dim_recursive,
            "filter": self._clear_dim_filter,
            # 全面占有一覧は次元ではなく母集合そのもの — × は絞り込みの解除
            # ではなく一覧からの退場。
            condition_chips.OVERLAY_ID: self._exit_overlay,
        }

    def _clear_condition_dimension(self, dim_id: str) -> None:
        """チップの × — *dim_id* の軸だけを中立へ戻す（表から引く）."""
        callback = self._condition_clear_callbacks().get(dim_id)
        if callback is not None:
            callback()

    def _current_view_scope(self) -> str:
        """Which population owns the grid: ``overlay`` / ``advanced`` / ``plain``.

        The same precedence :meth:`_apply_filter_and_sort` selects its
        population with — the view-dimension registry (項目#63) keys its
        scope filtering off this so「適用される次元」と「チップに出る次元」が
        常に同じ判定を通る。
        """
        if self._overlay is not None:
            return "overlay"
        if self._advanced_search_active():
            return "advanced"
        return "plain"

    def _fold_view_dimensions(
        self, scope: str, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Fold the registry's *scope*-participating dimensions over *entries*.

        Applies, in table order, every engaged dimension that (a) declares
        *scope* in its ``scopes`` and (b) carries a generic ``apply``
        (``apply=None`` dimensions are applied by the population builders
        themselves — see :class:`_ViewDim`).  All applies are pure in-memory
        filters, so order only matters for readability.
        """
        for dim in self._view_dimensions():
            if (
                dim.apply is not None
                and scope in dim.scopes
                and dim.engaged()
            ):
                entries = dim.apply(entries)
        return entries

    def _view_dimensions(self) -> list[_ViewDim]:
        """ビュー次元表（項目#63）— 行順 = 条件バーのチップ順.

        1 行 = グリッドを絞る 1 次元。適用（:meth:`_fold_view_dimensions`）・
        チップ化（:meth:`_condition_chips`）・オーバーレイ narrowing 判定
        （:meth:`_overlay_narrowing_engaged`）が全てこの表を読む。

        判定・文言・scope・AI 帰属は Qt 非依存の :mod:`.condition_chips` が
        **値から**決め、ここは ① この時点の観測値を 1 度だけ束ね（:meth:`
        _condition_state`）② ホストしか持てない 2 列（``apply`` の述語と
        ``clear`` の中立化）を貼り合わせる、だけの層。``apply=None`` の次元
        （AI クエリ・locked・絞り込み欄・検索範囲）は母集合構築側が意味論ごと
        に適用する — 表は engaged / チップ / scope の唯一の情報源として働く。

        閉包が読むのは束ねた値なので、1 回の呼び出しの中で何度 ``engaged()``
        を呼んでも同じ答えになる（描いた条件と適用した条件が食い違わない）。
        """
        state = self._condition_state()
        applies: dict[
            str, Callable[[list[FolderEntry]], list[FolderEntry]]
        ] = {
            "media": self._apply_media_predicate,
            "star": self._apply_star_predicate,
            "later": self._apply_later_predicate,
            "usertag": self._apply_usertag_predicate,
            "date": self._apply_date_predicate,
        }
        clears = self._condition_clear_callbacks()

        def bind(row: condition_chips.DimRow) -> _ViewDim:
            at_neutral = row.at_neutral
            return _ViewDim(
                row.key,
                engaged=lambda: row.engaged(state),
                apply=applies.get(row.key),
                chip=lambda: (row.label(state), row.kind),
                clear=clears[row.key],
                scopes=row.scopes,
                chip_scopes=row.chip_scopes,
                at_neutral=(
                    None if at_neutral is None else (lambda: at_neutral(state))
                ),
            )

        return [bind(row) for row in condition_chips.rows(state)]

    def _condition_chips(self) -> list[condition_chips.ChipSpec]:
        """いま条件バーへ出すチップ列（判定は :func:`.condition_chips.chips`）.

        先頭は全面占有一覧（横断キュレーション / 最近追加）— 一覧そのものが
        最上位の次元で、× は絞り込みの解除ではなく一覧からの退場。以降は
        次元表の行順で、**いま見ている母集合が実際に適用する**軸だけが並ぶ
        （一覧に効かない locked / 投稿日 はチップも出ない: 項目#63 / #65）。
        """
        return condition_chips.chips(self._condition_state())

    def _condition_dimensions(self) -> list[tuple[str, object, str]]:
        """チップ列を ``(chip_label, clear_callback, kind)`` の三つ組で読む面。

        :meth:`_condition_chips` と同じ列を、× の呼び先まで解決した形で返す
        （条件バーの外から「いま何で絞っているか」と「どう解除するか」を対で
        読みたい呼び出し側 / テストのための口）。
        """
        clears = self._condition_clear_callbacks()
        return [
            (spec.label, clears[spec.key], spec.kind)
            for spec in self._condition_chips()
        ]

    def _panel_reset_dimensions(self) -> list[_ViewDim]:
        """AI パネルの「詳細条件のみリセット」が中立化する次元（項目 #89）.

        対象は台帳の ``panel_reset`` 列 1 か所で宣言する — 以前は
        ``_advanced_only_engaged`` / ``_on_reset_advanced_only`` /
        ``clear_search_state`` / ``save_tag_settings`` がそれぞれ別の軸リストを
        手書きしており、精度と表示単位だけが面ごとに落ちていた。AI パック
        無効時は AI 所有の行が表ごと消えるので、この列も自然に縮む。
        """
        return [
            dim for dim in self._view_dimensions()
            if (row := _dim_get(dim.key)) is not None and row.panel_reset
        ]

    #: チップ列の間隔 — 見積りと実構築が読む唯一の定数（:mod:`.condition_bar`）。
    _CHIP_SPACING = CHIP_SPACING

    @property
    def _condition_chip_host(self) -> QWidget | None:
        """いまチップを載せている席（部品側の読み口へ委譲）."""
        bar = getattr(self, "condition_bar", None)
        return bar.chip_host if bar is not None else None

    @property
    def _condition_chips_layout(self) -> QHBoxLayout | None:
        """チップ列のレイアウト（部品側の読み口へ委譲）."""
        bar = getattr(self, "condition_bar", None)
        return bar.chips_layout if bar is not None else None

    # 条件バー右側の固定要素は部品が持つ。窓 / テストが名指しで触ってきた
    # 従来の属性名を、部品の同じ 1 実体へ向け直すだけの読み口として残す。
    @property
    def condition_count_label(self) -> QLabel:
        """件数ラベル（``N 件`` — 部品側の 1 実体）."""
        return self.condition_bar.count_label

    @property
    def condition_summary_label(self) -> QLabel:
        """非表示の集約ラベル（条件の全文を 1 本の文字列で持つ）."""
        return self.condition_bar.summary_label

    @property
    def condition_save_btn(self) -> QPushButton:
        """[この検索を保存…]（可視は ``save_active`` が決める）."""
        return self.condition_bar.save_btn

    @property
    def condition_clear_btn(self) -> QPushButton:
        """[すべて解除]（Esc と同じ ``clear_search_state()``）."""
        return self.condition_bar.clear_btn

    def _make_condition_chip(self, label: str, clear_cb, kind: str) -> QFrame:
        """条件チップ 1 枚（構築は :meth:`.ConditionBar.make_chip`）."""
        return self.condition_bar.make_chip(label, kind, clear_cb)

    def _condition_chip_budget(self) -> int:
        """チップ列に使える横幅（実測は :meth:`.ConditionBar.chip_budget`）."""
        return self.condition_bar.chip_budget()

    def _condition_chips_that_fit(self, labels: list[str]) -> int:
        """先頭から何枚まで並ぶか（予算の口を 1 本に保つための薄い殻）."""
        return self.condition_bar.chips_that_fit(
            labels, budget=self._condition_chip_budget()
        )

    def _edit_condition(self, kind: str, anchor: QWidget) -> None:
        """Open the edit surface for a condition chip's *kind* (Phase 1-3).

        ``ai`` → the AI search popover, ``filter`` → the adaptive filter
        popover (both anchored at the clicked chip; 「サブフォルダも検索」 is
        a row of that popover too), ``search`` → focus the toolbar filter
        box.  ``none`` chips (the
        cross-library curation list, the 最近追加されたファイル listing) never route
        here — they only carry a × clear, and say so in their tooltip (#64).
        """
        if kind == "ai":
            self.open_ai_popover(anchor)
        elif kind == "filter":
            self._open_filter_popover(anchor)
        elif kind == "search":
            self.focus_filter()

    def _has_field_scoped_filter_terms(self) -> bool:
        """Whether the filter box carries a post.md-field-scoped token (E02).

        Field getters (``tags:`` / ``title:`` / ``body:`` …) restrict the direct
        children only; control tokens (``type:`` / ``rating:`` / ``score:``)
        drive GUI dimensions globally and are excluded here.
        """
        for term in _parse_filter_query(self._filter_text):
            if term.field is not None and term.field not in _CONTROL_FIELDS:
                return True
        return False

    def _has_post_md_scoped_filter_terms(self) -> bool:
        """検索欄に **post.md 由来**の分野トークンがあるか。

        :meth:`_has_field_scoped_filter_terms`（E02 の「直下のみ」注記用）と
        材料が違う: 条件次元レジストリの ``TokenField`` が宣言する値の出所を
        読み、``name:``（パス名）/ ``title:``（post.md 不在ならフォルダ名へ
        フォールバック）/ ``star:`` / ``mytags:`` / ``later:``（user_meta.db）を
        外す。post.md を持たないフォルダで「post.md が無いため一致しません」と
        断定してしまうと、post.md はオプショナルという製品の前提を画面が自ら
        否定することになる。
        """
        return any(
            term.field in _POST_MD_TOKEN_FIELDS
            for term in _parse_filter_query(self._filter_text)
        )

    def _update_field_scope_note(self) -> None:
        """Show the E02 note only while recursive search + a field token collide."""
        note = getattr(self, "field_scope_note", None)
        if note is None:
            return
        note.setVisible(
            self._recursive_search and self._has_field_scoped_filter_terms()
        )

    def _update_condition_bar(self, shown: int) -> None:
        """Rebuild the condition chip bar for the current search state.

        Called from ``_rebuild_grid`` (the single display choke point).
        条件が 1 つも無ければバーごと隠れる（高さ 0）。

        **破棄後 / shutdown 後に届く更新は捨てる**: 更新は非同期の着地（走査
        結果 → グリッド再構築 → ここ）から来るので、閉じた後に 1 回余分に届き
        得る。バーはウィンドウがマウントする（親がこのペインではない）ため、
        ペインより先に C++ 側が消える経路があり、そのまま進むと死んだレイ
        アウトを触って落ちる。判定は部品側の 1 つの口（``is_live()``）で、
        ホストの状態（``filter_edit`` 等）を読む**前**に抜ける。
        """
        bar = getattr(self, "condition_bar", None)
        if bar is None or not bar.is_live():
            return
        self._update_field_scope_note()
        specs = self._condition_chips()
        if not specs:
            bar.hide_bar()
            return
        # UIレビュー #15: the toolbar search field already displays the plain
        # filter text with its own × (clear button), so echoing it as a second
        # 絞り込み chip (and a second ×) is pure duplication.  Drop the
        # "search"-kind chip WHILE the field is visibly showing a term.
        #
        # 「欄の語 == ``_filter_text`` の語」の再確認は**恒真**（``_filter_text``
        # は ``filter_edit`` と同時に書かれる不変条件 — ``__init__`` の宣言
        # 参照）なので見ない。欄が語を見せているかどうかだけが材料。
        field_plain = self._plain_filter_label(self.filter_edit.text()).strip()
        bar.render(
            condition_chips.visible_chips(
                specs, field_shows_term=bool(field_plain),
            ),
            shown=shown,
            summary=condition_chips.summary_text(specs, shown),
            save_active=self.capture_search_state().is_active(),
        )

    # ----------------------------------------------- per-dimension chip clears

    def _clear_dim_semantic(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._on_ai_mode_selected("and")

    def _clear_dim_tags(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self.tag_input.setText("")  # fires _on_tag_input_changed

    def _clear_dim_precision(self) -> None:
        # Back to the product DEFAULT (N-31 — the chip only exists above
        # max(floor, default), so「×で既定へ戻す」で確実に消える。旧実装は
        # スライダ下限 = DB 記録 floor へ**永続的に**落としており、既定へ
        # 戻す導線が無かった).  Fires valueChanged → _on_tag_threshold_changed
        # → re-query.
        # A synced ``score:`` token re-applies the old precision on the next
        # rebuild, so the × has to delete it first — same rule the sibling
        # ``_clear_dim_rating`` / ``_clear_dim_media`` follow for their fields.
        self._strip_filter_control_field("score")
        self.tag_threshold_slider.setValue(self._precision_neutral())

    def _clear_dim_unit(self) -> None:
        # Back to the default 表示単位 (folder_coverage); the handler maps the
        # combo onto the two persisted bools and re-queries.
        self._select_combo_data(self.tag_display_unit_combo, "folder_coverage")
        self._on_tag_display_unit_changed()

    def _clear_dim_ai_media(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._select_combo_data(self.tag_media_combo, "all")
        self._on_tag_media_changed()

    def _clear_dim_rating(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        # Drop any ``rating:`` token first so it doesn't re-apply on rebuild,
        # then neutralise the combo (both paths end at "all").
        self._strip_filter_control_field("rating")
        self._select_combo_data(self.tag_rating_combo, "all")
        self._on_tag_rating_changed()

    def _clear_dim_media(self) -> None:
        self._strip_filter_control_field("type")
        self._select_combo_data(self.filterbar_media_combo, "all")
        self._on_filterbar_media_changed()

    def _clear_dim_star(self) -> None:
        # A synced ``star:`` token drives the combo on every edit, so the ×
        # has to delete it too or the dimension re-applies (UIレビュー 07-25 #62
        # — same rule ``_clear_dim_media`` follows for ``type:``).  消すのは
        # コントロールが言い換えられる形だけ (``_syncable_curation_value``):
        # ``star:=5`` / ``star:<2`` / ``-``・``~`` 接頭辞はコンボの持ち物では
        # なく、ただのテキスト項として絞り込みチップに残るのが正しい。
        self._strip_synced_curation_token("star")
        self._select_combo_data(self.filterbar_star_combo, 0)
        self._on_filterbar_star_changed()

    def _clear_dim_usertag(self) -> None:
        """Drop the ユーザータグ chip (UIレビュー 07-25 #13②).

        N-65: a synced ``mytags:`` token drives the combo on every edit, so
        the × deletes it too (same rule as ``_clear_dim_star``) — while the
        current tag is still known, i.e. before the combo is neutralised.
        Substring-only ``mytags:`` forms stay as text terms.
        """
        self._strip_synced_curation_token("mytags")
        self._select_combo_data(self.filterbar_usertag_combo, "")
        self._on_filterbar_usertag_changed()

    def _clear_dim_later(self) -> None:
        # 同上 — ``later:no`` などコントロールが表せない形は温存する。
        self._strip_synced_curation_token("later")
        self.filterbar_later_check.blockSignals(True)
        self.filterbar_later_check.setChecked(False)
        self.filterbar_later_check.blockSignals(False)
        self._on_filterbar_later_toggled(False)

    def _clear_dim_recursive(self) -> None:
        """Drop the 「サブフォルダも検索」 scope chip (UIレビュー 07-25 #25)."""
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self.recursive_check.setChecked(False)  # fires _on_recursive_toggled

    def _clear_dim_date(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self._select_combo_data(self.tag_date_combo, "all")
        self._on_filterbar_date_changed()

    def _strip_filter_control_field(self, field: str) -> None:
        """Remove a single ``field:`` control token from the filter box.

        A control token drives its dimension on every rebuild, so clearing the
        dimension via its banner × has to also delete the token — otherwise the
        token re-applies and the chip never goes away.  Setting the text fires
        ``_on_filter_changed`` (→ ``_sync_filter_control_tokens``), which now
        sees no such token and leaves the just-reset control alone.

        **全**出現を消すのがここの契約 — 同じ次元を 2 度名指しした
        ``type:image type:video`` で勝者だけ剥がすと、負けていた側が繰り上がって
        次元が立ち直り、× が効かなくなる。負けた側をチップに見せる側の規律は
        :func:`~.filter_query.strip_owned_control_tokens`（対の半分）。
        """
        cur = self.filter_edit.text()
        stripped = strip_control_tokens(cur, {field})
        if stripped != cur:
            self.filter_edit.blockSignals(True)
            self.filter_edit.setText(stripped)
            self.filter_edit.blockSignals(False)
            self._filter_text = stripped.strip()

    def _clear_dim_filter(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        # Clear exactly what the 絞り込み chip is showing as its own text, and
        # keep every token that owns a *separate* dimension chip (an applied
        # ``type:`` / ``rating:`` / ``score:`` and a synced ``star:`` /
        # ``mytags:`` / ``later:`` — UIレビュー 07-25 #62 / N-65).  The
        # ownership rule is read off :meth:`_plain_filter_label` (=
        # ``strip_control_tokens``) rather than re-derived from the head name:
        # that contract deliberately keeps ``~`` / ``-`` prefixed tokens and
        # values no control consumes (``type:`` / ``type:foo`` / ``score:<0.1``)
        # inside the 絞り込み chip, so a hand-written head-name test made the ×
        # a no-op on tokens the chip was showing (レビュー 0903 項目 #98).
        # 突き合わせは**位置**で行う（集合ではなく部分列）— ラベルは元の並び
        # から所有トークンを抜いたものなので、同じ綴りが 2 度出るクエリ
        # (``type:image type:image``) でも「チップが見せている 1 つ」だけが
        # 消える（集合だと綴りが同じ全部を巻き込み、チップの持ち主まで消えた）。
        raws = self._filter_text.split()
        shown = self._plain_filter_label(self._filter_text).split()
        kept: list[str] = []
        cursor = 0
        for raw in raws:
            if cursor < len(shown) and shown[cursor] == raw:
                cursor += 1  # チップが見せているトークン → これを消す
                continue
            kept.append(raw)
        self.filter_edit.setText(" ".join(kept))  # fires _on_filter_changed

    def _clear_dim_locked(self) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self.locked_check.setChecked(False)  # fires _on_locked_filter_toggled

    def _on_banner_clear(self) -> None:
        """「解除」 on the result banner — same one-shot teardown as Esc.

        Clears every engaged search dimension (filter box, recursive walk,
        AIタグ検索 + cascaded semantic / similar seed, media type, posted
        date) via :meth:`clear_search_state`, preserving the selection (or
        its containing folder) like the other teardown paths.

        In an overlay (cross-library curation list H01 / 最近追加されたファイル)
        the button additionally leaves the overlay — the listing IS the top
        condition on the bar.  The narrowing loaded ON the listing is torn down
        too: it was only ever scoped to the overlay's population, and leaving it
        standing would silently apply it to the plain grid the user lands back
        on, making 「すべて解除」 the one action that leaves conditions behind.
        """
        overlaid = self._overlay is not None
        self._preserve_selection_for_rebuild(ancestor_fallback=True)
        self.clear_search_state()
        if overlaid:
            self._exit_overlay()

    def _on_exclude_thumb_toggled(self, checked: bool) -> None:
        if checked == self._exclude_thumb_marker:
            return
        self._exclude_thumb_marker = checked
        # 表示軸のトグルなので、他の軸（★ / あとで見る / 種別 / NSFW …）と
        # 同じく再構築の前に選択を預ける — ``set_tiles`` は毎回選択を落とすので、
        # これが無いとプレビュー列だけが前の投稿を映したまま残り、‹ › の起点も
        # 先頭へ戻る（レビュー 0903 項目 #68）。
        self._preserve_selection_for_rebuild()
        # Re-pick the chosen thumbnail for every entry from the cached
        # marker / non-marker candidates (no rescan), then rebuild.  The
        # representative image changed, so the loader cache is dropped and
        # aspects are NOT seeded (forces a fresh probe at the new image).
        self._entries = [self._reapply_thumbnail(e) for e in self._entries]
        self._loader.clear_cache()
        self._pending_icons.clear()
        self._rebuild_grid(seed_aspect=False)

    def _sync_sort_combo_for_rank(self) -> None:
        """Reflect ranked (semantic / similar) results in the sort combo.

        Semantic and similar-image searches return their hits in relevance
        order and the sort mode is deliberately bypassed for them
        (``_apply_advanced_search``).  Leaving the combo interactive there is
        misleading — the user can pick a sort that silently does nothing.  So
        while ranked results are showing, swap the combo to a single disabled
        「関連度順」 entry; restore the normal mode list (re-selecting the user's
        sort) once ranking clears.  Idempotent — called on every rebuild.
        """
        ranked = bool(self._tag_results_ranked)
        if ranked:
            if self._sort_combo_ranked:
                return
            self._sort_combo_ranked = True
            self.sort_combo.blockSignals(True)
            self.sort_combo.clear()
            self.sort_combo.addItem(
                t("viewer.post_grid.sort_relevance"), "__relevance__"
            )
            self.sort_combo.setCurrentIndex(0)
            self.sort_combo.setEnabled(False)
            self.sort_combo.setToolTip(
                t("viewer.post_grid.sort_relevance_tooltip")
            )
            self.sort_combo.blockSignals(False)
            return
        if not self._sort_combo_ranked:
            return
        self._sort_combo_ranked = False
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        for key, label_key in self._available_sort_labels():
            self.sort_combo.addItem(t(label_key), key)
        self._select_sort_in_combo(self._sort_mode)
        self.sort_combo.setEnabled(True)
        self.sort_combo.setToolTip("")
        self.sort_combo.blockSignals(False)

    def _on_sort_changed(self) -> None:
        self._sort_mode = self.sort_combo.currentData() or "name_asc"
        self._sync_reload_tooltip()
        self._rebuild_grid()

    def _sync_reload_tooltip(self) -> None:
        """F5 の説明を並び順に合わせて差し替える (UIレビュー 2026-08-28 N-145).

        「ランダム」並びのときだけ F5 は**並びをシャッフルし直す**
        (:meth:`reshuffle_random_sort`) が、その副作用はツールチップにも
        ショートカット表にも書かれていなかった。前例は最大化中の戻るボタンの
        動的差し替え（07-25 #23）。
        """
        btn = getattr(self, "reload_btn", None)
        if btn is None:
            return
        btn.setToolTip(t(
            "viewer.post_grid.reload_tooltip_random"
            if self._sort_mode == "random"
            else "viewer.post_grid.reload_tooltip"
        ))

    def _view_mode_did_change(self) -> None:
        # Base behaviour FIRST: push the new view mode / layout strategy /
        # params into the GalleryView.  Skipping this left the view painting
        # list captions inside the old justified strategy (the slider-range
        # sync only reconfigures when the clamped size changes).
        super()._view_mode_did_change()
        # Then rebuild from cached entries so captions re-render for the new
        # view mode without re-running the scan.
        self._rebuild_grid()

    def _on_change_root_clicked(self) -> None:
        start = str(self._root_or_folder) if self._root_or_folder else ""
        chosen = pick_existing_directory(
            self,
            t("common.action.choose_folder"),
            start,
            sidebar=host_picker_places(self),
        )
        if chosen:
            self.root_change_requested.emit(Path(chosen))

    # ------------------------------------------------------ filter-syntax help

    def _on_filter_help_clicked(self) -> None:
        """構文ヘルプ（全文）の開閉。

        面の実体と寿命管理は :class:`grid_chrome.FilterHelpPopups`（2 面とも
        TOP-LEVEL なので開き直し・畳みで必ず ``deleteLater`` する）。
        """
        self._filter_help.toggle_full()

    # -------------------------------------------- Ctrl+F focus + auto help

    def focus_filter(self) -> None:
        """Focus the filter box and select its text (the window's Ctrl+F).

        Selecting the existing needle lets the next keystroke replace it —
        the standard find-box behaviour, and the shortest path to the most
        common "find something" action.
        """
        self.filter_edit.setFocus(Qt.ShortcutFocusReason)
        self.filter_edit.selectAll()

    def _jump_to_results(self) -> bool:
        """Hand keyboard control from the search box to the grid (#6).

        Wired to ``returnPressed`` and to ``↓`` in :meth:`eventFilter`
        (UIレビュー 07-25 #6): after typing a query there was no keyboard route
        to the results at all (Enter / ↓ inert, Esc tears the search down, the
        grid was 9 Tab stops away).  Focuses the grid and — only when nothing
        is selected yet — selects the first tile, so repeating the gesture
        never yanks an existing selection back to the top.  Returns ``True``
        when the jump happened, so the key handler can swallow the event.

        No-op while the syntax cheat-sheet popup is showing (the popup owns
        Enter / ↓ for its own dismissal flow) and while the grid has no tiles
        (nothing to move to).
        """
        auto = self._filter_help_auto
        if auto is not None and auto.isVisible():
            return False
        completer = self.filter_edit.completer()
        popup = completer.popup() if completer is not None else None
        if popup is not None and popup.isVisible():
            # The ``分野:`` prefix completer owns ↓ / Enter while its list is up.
            return False
        if self._view.tile_count() == 0:
            return False
        self.focus_grid()
        if self._view.current_tile() is None:
            self._view.select_first(emit=True)
        return True

    def eventFilter(self, obj, event):  # noqa: ANN001
        """First-focus cheatsheet + Esc handling for the filter box.

        The syntax popup auto-shows once per session when the filter box
        first gains focus (``Qt.ToolTip`` frame → the box keeps focus), and
        hides on Esc / focus-out / first keystroke (``textEdited`` wired in
        ``_build_chrome``).  Esc dismisses the popup first; a further Esc
        clears the active filter / search exactly like Esc on the grid —
        the shortcut list documents "Esc = 解除" without a focus caveat, and
        right after typing a query the focus IS the filter box (UIレビュー
        #17).
        """
        if obj is self.filter_edit:
            # Local named ``etype`` (not ``t``) to avoid shadowing the
            # module-level i18n ``t`` now imported into this file.
            etype = event.type()
            if etype == QEvent.FocusIn:
                if not self._filter_help_autoshown:
                    self._filter_help_autoshown = True
                    self._show_filter_help_auto()
            elif etype == QEvent.FocusOut:
                self._hide_filter_help_auto()
            elif etype == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                if (
                    self._filter_help_auto is not None
                    and self._filter_help_auto.isVisible()
                ):
                    self._hide_filter_help_auto()
                elif self._search_engaged():
                    self._on_escape_clear()
                else:
                    return super().eventFilter(obj, event)
                return True
            elif (
                etype == QEvent.KeyPress
                and event.key() == Qt.Key_Down
                and not event.modifiers()
            ):
                # ↓ = 「結果へ移る」 (UIレビュー 07-25 #6).  Swallowed only when
                # the jump actually happened; otherwise (cheat-sheet showing /
                # empty grid) the completer popup and the default handling keep
                # their historical behaviour.
                if self._jump_to_results():
                    return True
        return super().eventFilter(obj, event)

    def _show_filter_help_auto(self) -> None:
        """初回フォーカスの自動表示 — **短縮版**（N-116）.

        表示済みフラグ（``_filter_help_autoshown``）の ``viewer_state.json``
        永続化は意図的に**しない**: 教示機会を恒久的に失う副作用があり、
        割り込み感の主因は「毎回出ること」ではなく「初回に大きすぎること」
        だという検証結果に従う（減量だけで解消する）。
        """
        self._filter_help.show_brief()

    def _hide_filter_help_auto(self) -> None:
        self._filter_help.hide_brief()


# ``PostGrid`` ↔ AI 検索コントローラの接着（ホスト読み替え + 旧名の委譲）。
# 表は :mod:`.advanced_search_parts.facade` が 1 枚で持つ。
_search_facade.install(PostGrid)
