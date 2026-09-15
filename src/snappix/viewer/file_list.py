"""Right pane — list of files inside the currently selected folder.

Thin subclass of :class:`ChildrenGrid` that adds:

* A :class:`~snappix.common.ui.PanelHeader` (「ファイル」 title + item count)
  whose "⋯" overflow button opens a small popover holding 並び順 / 表示形式 /
  サムネイルサイズ — the same three rows, in the same order, built by the same
  ``ChildrenGrid._build_view_settings_rows`` factory as the left pane's
  「並び・表示」 popover (UIレビュー 2026-08-28 N-75).  No filter UI (the right
  pane is a leaf view).  See the `PanelHeader` section of docs/claude/design.md.
* The loading-spinner overlay (painted by :class:`GalleryView`, driven
  here via ``set_spinner_check`` + an 80 ms repaint timer) on image-file
  tiles whose PIL source hasn't reached :class:`ImageView`'s LRU yet.
* ``image_siblings(anchor)`` — neighbour-image enumeration used by
  ``ImageView`` to prefetch the next / previous image.
* A right-click context menu — the pane-shared base set
  (:func:`context_menus.append_entry_verbs`) plus this pane's
  「ファイルをコピー」 extra.
* A minimal sort in the 並び順 combo (既定 / 名前 / 更新日時 / 種類).  "既定"
  is the scanner's historical order (dirs first) with ``post.md`` demoted to
  the end of the files (UIレビュー 07-25 #52 — 本編優先); the other modes
  re-sort the *cached* shallow-scan entries GUI-side
  (no rescan), keeping folders ahead of files.  Persisted as
  ``ViewerState.file_list_sort_mode``.

Click semantics match the left pane: single-click → ``file_selected`` /
``folder_selected``; double-click → ``file_activated`` / ``folder_activated``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import PanelHeader, popover_position
from .children_grid import ChildrenGrid
from .context_menus import CurationHooks, EntryMenuContext, append_entry_verbs
from .focus_target import SEAT_FILE_LIST
from .folder_scan import (
    IMAGE_SUFFIXES,
    THUMB_MARKER_PREFIX,
    FolderEntry,
    is_meta_or_marker_file,
)
from .thumbnail_loader import ThumbnailLoader


_ICON_THUMB_SIZE_DEFAULT = 120
_ICON_THUMB_SIZE_MIN = 64

#: ⋯-popover minimal sort modes (persisted key → i18n label key).  "default"
#: keeps the scanner's pre-sorted order; the rest re-sort GUI-side from the
#: cached entries.  The first element is persisted
#: (``ViewerState.file_list_sort_mode``) — keep it stable; the second is an
#: i18n catalog key resolved via ``t()`` where the action is built.
#: 方向表記は左ペイン（``post_grid`` の全項目が「(昇順)」/「(新しい順)」を
#: 持つ）に揃える — 実装は name / type とも昇順固定、mtime は新しい順
#: （UIレビュー 2026-09-11 N-131）。``default`` はスキャン順なので方向の概念が
#: 無く、表記も持たない。**永続キー（第 1 要素）は不変**。
_FILE_SORT_LABELS: list[tuple[str, str]] = [
    ("default", "viewer.file_list.sort_default"),
    ("name", "viewer.post_grid.sort_name_asc"),
    ("mtime", "viewer.common.sort_mtime_desc"),
    ("type", "viewer.file_list.sort_type_asc"),
]


class FileListView(ChildrenGrid):
    """Right pane: per-folder file list with viewport-driven thumbnails."""

    #: Right-click "この画像に類似を検索" on an image file (C-10).  Carries the
    #: image path and its already-decoded thumbnail (``QPixmap | None``) so the
    #: left pane's seed preview can show it even though the image isn't a tile
    #: there.  Only emitted when similar search is available (VectorIndex present).
    similar_search_requested = Signal(Path, object)

    #: A curation edit was asked for on a right-list entry (UIレビュー 07-25 #19).
    #: ``(path, kind, value)`` where *kind* is ``"star"`` / ``"later"`` /
    #: ``"edit_tags"``.  Deliberately ONE signal that only *reports* the intent:
    #: the store write, the in-memory map patch and the toast all stay owned by
    #: :meth:`PostGrid._apply_curation`, so both panes can never drift.  The pane
    #: painted these badges but offered no way to set them — "読めるのに書けない".
    curation_requested = Signal(Path, str, object)

    #: 0–5 pressed with a right-list tile selected (UIレビュー 07-25 #19).  Re-emit
    #: of the view's own key signal; the window resolves it to the selected file
    #: and routes it through the same ``_set_current_star`` funnel the grid and
    #: the preview use, so all surfaces produce the same 「★★★ 対象名」 toast.
    star_key_requested = Signal(int)

    #: フォルダ行の右クリック「最近追加されたファイルを表示」
    #: (UIレビュー 2026-08-28 N-50)。左グリッドの同じ動線だけが持っていた
    #: 項目で、右ペインのフォルダ右クリックにだけ無かった。この一覧は
    #: 左ペインのビュー（``PostGrid.enter_recent_files_view``）なので、
    #: 右ペインは意図だけを報告し、窓が左ペインへ転送する
    #: （``curation_requested`` と同じ形 — 右ペインは左ペインを知らない）。
    recent_files_requested = Signal(Path)

    #: ファイル行の右クリック「このファイルの場所を開く」
    #: (UI08-28 N-64 / 2026-09-11 N-40)。左グリッドの同名シグナルと同じ形で、
    #: 窓が親フォルダ + 当の項目の選択へ着地させる。
    reveal_in_app_requested = Signal(Path)

    _ICON_SIZE_MIN = _ICON_THUMB_SIZE_MIN
    _CAPTION_PAD = 20
    _LIST_MODE_ICON_SIZE = 20
    _WITH_METADATA = False
    # 同一フォルダへの再ナビゲーション（左グリッドのリビルドが再発火する
    # ``folder_selected``、同一フォルダ内のファイル選択替え等）で一覧を
    # ブランク化しない — stale-while-revalidate（ChildrenGrid.set_folder 参照）。
    _keep_view_on_same_folder = True

    def __init__(
        self,
        loader: ThumbnailLoader,
        view_mode: str = "list",
        icon_size: int = _ICON_THUMB_SIZE_DEFAULT,
        list_icon_size: int | None = None,
        icon_size_max: int = 320,
        thumb_layout: str = "justified",
        sort_mode: str = "default",
        exclude_thumb_marker: bool = False,
        meta_cache=None,
        folder_cache=None,
        search_index=None,
        probe_parallelism: int = 2,
        parent: QWidget | None = None,
    ) -> None:
        # Per-instance loader-key namespace so multiple FileListView
        # instances (or a shared loader) can never collide.
        prefix = f"filelist:{id(self)}:"
        # Sort state must exist before super().__init__ — _build_chrome
        # (called during base construction) reads it to check the menu action.
        valid = {key for key, _ in _FILE_SORT_LABELS}
        self._sort_mode = sort_mode if sort_mode in valid else "default"
        # Cached shallow-scan result so a sort change re-sorts without a
        # rescan (the scanner already delivered everything we need).
        # ``_raw_entries`` keeps what the scanner delivered so the
        # ``#thumb#`` visibility toggle (N-81) can re-filter without one
        # either; ``_entries`` is the currently visible subset.
        self._raw_entries: list[FolderEntry] = []
        self._entries: list[FolderEntry] = []
        self._exclude_thumb_marker = exclude_thumb_marker
        # str(path) → entry mirror of ``_entries``, kept in sync everywhere
        # ``_entries`` is (re)assigned — ``entry_for`` is called once per
        # visible strip cell on every scroll, so a linear scan over a
        # several-thousand-entry folder was O(entries × visible cells).
        self._entries_by_path: dict[str, FolderEntry] = {}
        super().__init__(
            loader=loader,
            icon_size=icon_size,
            list_icon_size=list_icon_size,
            icon_size_max=icon_size_max,
            view_mode=view_mode,
            thumb_layout=thumb_layout,
            loader_key_prefix=prefix,
            meta_cache=meta_cache,
            folder_cache=folder_cache,
            search_index=search_index,
            probe_parallelism=probe_parallelism,
            parent=parent,
        )
        # Spinner timer drives ~12 fps repaints of pending image tiles
        # (the GalleryView paints the arc; we just advance the angle).
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._view.advance_spinner)
        self._view.context_menu_requested.connect(self._on_context_menu)
        # Whether the "この画像に類似を検索" item is offered (main_window sets it
        # True only when a VectorIndex is loaded).  Default False so the item
        # hides on installs without semantic vectors.
        self._similar_search_available = False
        # Curation lookup for the context menu's check states (UIレビュー #19).
        # ``None`` = no user_meta store → the whole curation section is omitted,
        # the same degradation the left pane's menu already applies.
        self._curation_provider = None
        self._view.star_key_requested.connect(self.star_key_requested)
        # UIレビュー #10: nothing is selected yet at construction time either
        # (the base ``__init__`` just sets ``_root_or_folder = None`` without
        # routing through ``set_folder`` — this pane would otherwise sit
        # blank until main_window's first ``clear()``/``set_folder(None)``
        # call, which doesn't happen until ``set_root`` runs).
        self._view.set_empty_state(
            t("viewer.file_list.empty_unselected"), "", self._empty_state_icon()
        )

    # ------------------------------------------------------------ hooks

    def _build_chrome(self, outer_layout: QVBoxLayout) -> None:
        # PanelHeader (redesign 2026-07 Phase 1-2): title + item count, with
        # the pane's secondary controls (size / view mode / sort) tucked
        # behind the "⋯" overflow button instead of a permanent header row.
        self._header = PanelHeader(t("common.label.file"))
        # H4 統一 (split-view redesign 2026-07): a right-list image double-click
        # maximises the preview, same as the grid.  The header tooltip spells
        # out the gesture (UIレビュー #21 — no other on-screen cue).
        self._header.setToolTip(t("viewer.file_list.dblclick_tooltip"))
        overflow_btn = self._header.overflow_button()
        overflow_btn.setToolTip(t("viewer.file_list.options_tooltip"))
        overflow_btn.clicked.connect(self._toggle_overflow_popover)
        outer_layout.addWidget(self._header)

        self._overflow_popup = self._build_overflow_popover()

    def _build_overflow_popover(self) -> QFrame:
        """Small click-away popover housing 並び順 / 表示形式 / サムネイルサイズ.

        Reuses the ``Qt.Popup`` frameless-frame pattern already established by
        ``advanced_search._show_search_cheatsheet`` / ``post_grid``'s filter
        help popup — a borderless ``QFrame`` that closes on click-away, kept
        as a real widget (not a ``QMenu``) so it can host the slider + combos.

        The three rows come from ``ChildrenGrid._build_view_settings_rows``,
        the same factory the left pane's 「並び・表示」 popover uses (UIレビュー
        2026-08-28 N-75).  Sort used to be an exclusive checkable ``QAction``
        group behind a ``QToolButton``, justified in a comment by "the right
        pane stays a leaf view with **no permanent sort combo** … without extra
        chrome" — but the redesign already moved BOTH panes' controls into
        click-away popovers, so nothing here is permanent any more and the
        premise no longer holds.  What was left was one setting operated two
        different ways.  State + persistence (``ViewerState.file_list_sort_mode``)
        are untouched: only the control changed.
        """
        popup = QFrame(self, Qt.Popup)
        popup.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self._build_view_settings_rows(
            layout, sort_choices=_FILE_SORT_LABELS, slider_width=100,
        )
        idx = self.sort_combo.findData(self._sort_mode)
        self.sort_combo.setCurrentIndex(max(0, idx))
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        return popup

    def _toggle_overflow_popover(self) -> None:
        if self._overflow_popup.isVisible():
            self._overflow_popup.close()
            return
        self._overflow_popup.adjustSize()
        btn = self._header.overflow_button()
        self._overflow_popup.move(
            popover_position(btn, self._overflow_popup.size())
        )
        self._overflow_popup.show()

    # ------------------------------------------------------------- sorting

    def current_sort_mode(self) -> str:
        return self._sort_mode

    def _on_sort_changed(self) -> None:
        self._on_sort_mode_chosen(self.sort_combo.currentData() or "default")

    def _on_sort_mode_chosen(self, key: str) -> None:
        if key == self._sort_mode:
            return
        self._sort_mode = key
        if self._root_or_folder is None or not self._entries:
            return  # nothing shown — the next scan applies the new mode
        # Re-sort the cached entries without a rescan; keep the selection.
        cur = self._view.current_path()
        if cur is not None:
            self.queue_pending_select(cur)
        self._set_entries_as_tiles(self._sorted_for_mode(self._entries))

    def _sorted_for_mode(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """Apply the ⋯-menu sort.  "default" trusts the scanner's order.

        Folders always stay ahead of files (both panes' invariant).  The
        keys use only shallow-scan fields (``path`` / ``mtime``), so no
        metadata pass or extra I/O is ever needed here.
        """
        mode = self._sort_mode
        if mode == "default":
            return self._demote_meta(list(entries))
        dirs = [e for e in entries if e.is_dir]
        files = [e for e in entries if not e.is_dir]
        if mode == "mtime":
            key = lambda e: e.mtime  # noqa: E731
            reverse = True
        elif mode == "type":
            key = lambda e: (e.path.suffix.lower(), e.path.name.lower())  # noqa: E731
            reverse = False
        else:  # "name"
            key = lambda e: e.path.name.lower()  # noqa: E731
            reverse = False
        dirs.sort(key=lambda e: e.path.name.lower())
        files.sort(key=key, reverse=reverse)
        return dirs + self._demote_meta(files)

    @staticmethod
    def _demote_meta(entries: list[FolderEntry]) -> list[FolderEntry]:
        """``post.md`` を末尾へ格下げする (UIレビュー 07-25 #52).

        スキャナの既定順は ``post.md`` を先頭に置くため、投稿を開くと本編の
        1 枚目が 2 番目に見えていた。本編メディアの母集合（``tile_paths`` /
        閲覧モード）からは既に外してあるので、一覧でも「メタは末尾」に格下げ
        して並びを本編優先にする（淡色描画は ``Tile.dimmed``）。相対順は
        安定ソートで保つ。
        """
        return sorted(entries, key=lambda e: is_meta_or_marker_file(e.path))

    @staticmethod
    def _is_thumb_marker(entry: FolderEntry) -> bool:
        """Whether *entry* is a ``#thumb#…`` creator-icon marker file (#23)."""
        return (
            not entry.is_dir
            and entry.path.name.lower().startswith(THUMB_MARKER_PREFIX)
        )

    def set_exclude_thumb_marker(self, exclude: bool) -> None:
        """Follow the grid's 「クリエイターアイコンを隠す」 toggle (N-81).

        #23 originally dropped ``#thumb#`` markers here unconditionally,
        claiming to match the grid tile's exclusion.  The grid's exclusion
        later became conditional on ``ViewerState.exclude_thumb_marker``
        (``post_grid._drop_thumb_markers``) and this side was never updated —
        so with the toggle OFF the two panes listed different item counts while
        a stale comment asserted they agreed (UIレビュー 2026-08-28 N-81, 案A).
        The toggle is now one setting with one meaning on both sides; hiding
        them also keeps them out of the stage image track (fed from
        ``tile_paths()``) for free.

        Re-filters the cached scan result — no rescan.
        """
        if exclude == self._exclude_thumb_marker:
            return
        self._exclude_thumb_marker = exclude
        # 並び替え (:meth:`_on_sort_mode_chosen`) と同じく、母集合を組み直す前に
        # 選択を pending へ預ける（``set_tiles`` は毎回選択を落とす）。
        cur = self._view.current_path()
        if cur is not None:
            self.queue_pending_select(cur)
        self._apply_entries(self._raw_entries)

    def _on_scan_finished_payload(
        self, generation: int, entries: list[FolderEntry],
    ) -> None:
        del generation
        self._apply_entries(entries)

    def _drop_stale_view(self) -> None:  # type: ignore[override]
        """走査失敗で古い面を捨てるとき、エントリ表と見出しの件数も戻す.

        件数と見出しは着地時 (:meth:`_update_header_count`) にしか動かないので、
        タイルだけ消すと「7 件」のまま空のエラーカードが出る。
        """
        self._entries = []
        self._entries_by_path = {}
        self._raw_entries = []
        super()._drop_stale_view()
        self._update_header_count()

    def _apply_entries(self, entries: list[FolderEntry]) -> None:
        """Keep the raw scan result, publish the visible subset of it."""
        self._raw_entries = entries
        if self._exclude_thumb_marker:
            self._entries = [e for e in entries if not self._is_thumb_marker(e)]
        else:
            self._entries = list(entries)
        self._entries_by_path = {str(e.path): e for e in self._entries}
        self._set_entries_as_tiles(self._sorted_for_mode(self._entries))
        self._update_header_count()

    def _update_header_count(self) -> None:
        """Refresh the PanelHeader's title + item-count label from ``_entries``.

        Counts every listed child (files + subfolders) — what the pane
        actually shows, not just image files — reusing the same
        ``{n}件`` wording the left pane's search banner already uses
        (``viewer.post_grid.banner_count``) instead of minting a near-dup
        catalog key.

        UIレビュー #19: when the listing is folder-dominant (a creator folder
        whose children are post folders, not files) the 「ファイル」 heading
        contradicts the content, so the title switches to the neutral 「内容」
        whenever any subfolder is present.
        """
        count = len(self._entries)
        has_folder = any(e.is_dir for e in self._entries)
        self._header.set_title(
            t("viewer.file_list.header_contents")
            if has_folder
            else t("common.label.file")
        )
        self._header.set_count_text(
            t("viewer.post_grid.banner_count", n=count) if count else ""
        )

    # ----------------------------------------------------- spinner timer

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._spinner_timer.isActive():
            self._spinner_timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._spinner_timer.stop()
        super().hideEvent(event)

    # ----------------------------------------------------- folder handoff

    def set_folder(self, folder, *, pending_select=None) -> None:  # type: ignore[override]
        # Drop the cached entry lists with the old folder so a sort-mode /
        # #thumb#-toggle change issued while the new scan is still in flight
        # can't rebuild tiles of the previous folder into the cleared view
        # (``_raw_entries`` used to survive this reset — the toggle's
        # ``_apply_entries(self._raw_entries)`` could republish the previous
        # folder's listing mid-navigation).  A same-folder re-navigation
        # keeps them: they are already this folder's entries, and the kept
        # view (``_keep_view_on_same_folder``) must stay consistent with the
        # entry caches until the rescan lands.
        if folder is None or folder != self._root_or_folder:
            self._entries = []
            self._entries_by_path = {}
            self._raw_entries = []
            # 件数だけでなく**見出しも**中立へ戻す（N-99）: 見出しは
            # ``_update_header_count``（= 着地時）でしか動かないので、
            # 件数だけ消すと直前のフォルダの「内容」が残り、ファイルしか
            # 無い次のフォルダでも「内容」を名乗り続けていた。空の
            # ``_entries`` に対しては ``has_folder=False`` = 「ファイル」 +
            # 件数空 の中立な初期状態になる。
            self._update_header_count()
        super().set_folder(folder, pending_select=pending_select)
        if folder is None:
            # UIレビュー #10: the base's set_folder(None) routes through
            # GalleryView.clear(), which blanks any settled empty-state
            # message — historically leaving this pane a silent void with no
            # explanation while nothing is selected (main_window calls
            # ``clear()`` both at startup and whenever the centre/right panes
            # reset between folders).  Re-set it right away so the pane never
            # sits fully blank.  A *selected* folder that settles empty gets
            # its own distinct wording via ``_empty_state_message`` once its
            # scan lands, so this only covers the "nothing chosen yet" case.
            self._view.set_empty_state(
                t("viewer.file_list.empty_unselected"), "", self._empty_state_icon()
            )

    def _empty_state_message(self) -> str:
        """Settled-empty hint for a *selected* folder with no children (#10).

        Deliberately worded differently from the "nothing selected"
        message set directly in :meth:`set_folder` — this one is shown while
        a folder/post IS open, it just has no files inside.
        """
        return t("viewer.file_list.empty_folder")

    def _empty_state_icon(self) -> str:
        """No glyph — this pane is a **従属面** (N-101 / 空状態オーケストレータ).

        Phase 3-4 gave every placeholder the same icon + message card grammar,
        but on a settled-empty window that produced **three folder glyphs of
        the same size in three panes**, with the subordinate two reading as
        loud as the one card that actually owns the next step.  The window's
        resolver (:mod:`empty_state`) now assigns this pane ``SECONDARY`` in
        every empty state it participates in, and ``SECONDARY`` means
        「アイコン無し・1 行・控えめ」.  The pane's own placeholders follow the
        same grammar so the two writers can never disagree.
        """
        return ""

    # ----------------------------------------------------- cache check API

    def set_cache_check(
        self, cache_check: Callable[[Path], bool] | None,
    ) -> None:
        """Register an "is this image's decode settled?" predicate.

        :class:`ViewerWindow` wires this to ``ContentView.image_decode_settled``
        （= ``ImageView.is_decode_pending`` の反転）so the view can draw a
        pending-spinner on image tiles whose decode is actually in flight.
        ``None`` removes the spinner.

        述語は「LRU 残留」ではなく「先読みウィンドウ内でまだ載っていない」で
        なければならない（項目#60）— 残留で判定すると先読み半径の外の行が
        恒久的にスピナー対象になり、80ms の再描画タイマが止まらなくなる。
        """
        self._view.set_spinner_check(cache_check)

    def notify_cache_changed(self) -> None:
        """Repaint when ImageView's cache LRU shifts (prefetch completed)."""
        self._view.notify_cache_changed()

    def set_curation_provider(self, provider) -> None:  # type: ignore[override]
        """Keep a local reference so the context menu can show current values.

        The base forwards *provider* to the view for the badge overlays; this
        pane additionally needs it to tick the right ★ / 「あとで見る」 entries in
        its own right-click menu (UIレビュー 07-25 #19).  ``None`` (no user_meta
        store) removes both the badges and the menu section.
        """
        super().set_curation_provider(provider)
        self._curation_provider = provider

    def set_similar_search_available(self, available: bool) -> None:
        """Enable/disable the context-menu "類似を検索" entry (C-10).

        Wired by :class:`ViewerWindow` to the presence of a VectorIndex — with
        no vectors the similar search would be a no-op, so the item is hidden.
        """
        self._similar_search_available = bool(available)

    # ----------------------------------------------------- settings

    def apply_settings(self, state) -> None:  # state: ViewerState
        self.set_probe_parallelism(state.aspect_probe_parallelism)
        self.apply_settings_icon_size_max(state.file_list_icon_size_max)

    def entry_for(self, path: Path) -> FolderEntry | None:
        """Return the cached shallow-scan entry for *path*, or ``None``.

        In-memory lookup only (no filesystem I/O — same rule as
        ``image_siblings``).  Used by the stage-mode image track to decide how
        to thumbnail a strip cell (folder → resolve-in-folder with the known
        mtime; file → direct decode) without a GUI-thread stat.  O(1) via
        ``_entries_by_path`` — this runs once per visible strip cell on every
        scroll, so a linear scan over ``_entries`` cost O(entries × visible
        cells) on large folders.
        """
        return self._entries_by_path.get(str(path))

    # ----------------------------------------------------- image siblings

    def image_siblings(self, anchor: Path) -> tuple[list[Path], int]:
        """Image-file siblings of *anchor* plus *anchor*'s index.

        Used by :class:`ImageView` to prefetch neighbouring images from the
        *already-scanned* tile list — no filesystem I/O, so it never blocks
        the GUI thread.

        When the view hasn't populated yet (the async scan runs in parallel
        with the initial ``show_image`` on folder entry), this returns
        ``([], -1)`` and prefetch is simply skipped for that first image.
        We deliberately do **not** fall back to a synchronous ``os.scandir``
        + per-entry ``is_file()``: on a cold NAS folder that enumeration
        cost hundreds of ms on the GUI thread, violating the viewer-wide
        "no NAS I/O on the GUI thread" rule.  Prefetch is best-effort — the
        on-demand decode in ``show_image`` still works, and once the scan
        lands the next navigation prefetches normally.
        """
        paths: list[Path] = []
        anchor_index = -1
        for i in range(self._view.tile_count()):
            tile = self._view.tile_at(i)
            if tile is None or tile.is_dir:
                continue
            p = tile.path
            if p.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if p == anchor:
                anchor_index = len(paths)
            paths.append(p)
        if anchor_index >= 0:
            return paths, anchor_index
        return [], -1

    # ----------------------------------------------------- context menu

    def _on_context_menu(self, index: int, global_pos: QPoint) -> None:
        tile = self._view.tile_at(index)
        if tile is None:
            return
        path = tile.path
        menu = self._build_context_menu(path, is_dir=tile.is_dir)
        menu.exec(global_pos)
        # ``QMenu(self)`` is parented to this pane, so exec() only hides it —
        # without this the menu + its QAction set pile up on the pane for one
        # per right-click (same pattern as content_view/_popup_entry_menu).
        menu.deleteLater()

    def _build_context_menu(self, path: Path, *, is_dir: bool) -> QMenu:
        """Pane-shared base set + this pane's 「ファイルをコピー」 extra.

        The similar-search entry emits ``similar_search_requested`` with this
        pane's already-decoded thumbnail (if any) so the left pane's seed
        preview has something to show — the seed image isn't a tile there
        (C-10).  Offered only when a ``VectorIndex`` is loaded
        (``set_similar_search_available``); the builder additionally limits
        it to image files.
        """
        menu = QMenu(self)
        similar_cb = None
        if self._similar_search_available:
            pixmap = self.pixmap_for_path(path)

            def similar_cb(p=path, pm=pixmap):  # noqa: ANN202
                self.similar_search_requested.emit(p, pm)

        # 共通ブロックは動詞レジストリ 1 表から（左グリッドと同じ並び）。
        # 印の動詞はこのペインが所有せず ``curation_requested`` で左ペインの
        # 単一書き手へ転送するだけ（UIレビュー #19）— 店が無ければ節ごと出ない。
        hooks = None
        if self._curation_provider is not None:
            hooks = CurationHooks(
                provider=self._curation_provider,
                request=lambda p, k, v: self.curation_requested.emit(p, k, v),
            )
        append_entry_verbs(
            menu,
            EntryMenuContext(
                path=path, is_dir=is_dir, seat=SEAT_FILE_LIST,
                navigate=self.folder_activated.emit,
                reveal_in_app=self.reveal_in_app_requested.emit,
                similar_search=similar_cb,
                recent_files=self.recent_files_requested.emit,
                curation=hooks, host=self,
            ),
        )
        return menu
