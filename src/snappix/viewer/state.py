"""Viewer-only persistence: window geometry, last root, bookmarks, theme.

Stored at ``data/viewer_state.json`` so it never collides with another tool's
``config.json``.  All fields are optional so missing/corrupted state files
fall back to defaults rather than blocking startup.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ..common.paths import get_paths
from .search_dimensions import DEFAULT_TAG_THRESHOLD

# Maximum number of entries kept in the "最近開いたフォルダ" MRU list.
RECENT_ROOTS_MAX = 10

# NOTE: keep in sync with the theme choice tables in ``viewer/theme.py``
# (THEME_CHOICES_MAIN / THEME_CHOICES_EXTRA) and with
# ``common/shared_prefs.py::_VALID_THEMES`` — machine-checked by
# ``tests/test_viewer_theme_menu.py``.
ThemeName = Literal[
    "system",
    "light",
    "dark",
    "standard",
    "extra_astro",
    "extra_obsidian",
    "extra_washi",
    "extra_linen",
    "extra_brass",
    "extra_dusk",
]
SortMode = Literal[
    "name_asc",
    "name_desc",
    "mtime_asc",
    "mtime_desc",
    "posted_asc",
    "posted_desc",
    "favorites_asc",
    "favorites_desc",
    "size_asc",
    "size_desc",
    "star_desc",
    "random",
]
ViewMode = Literal["icon", "list"]
ThumbLayout = Literal["square", "justified"]


class ViewerState(BaseModel):
    geometry_b64: str = ""  # base64-encoded QByteArray from saveGeometry()
    splitter_sizes: list[int] = Field(default_factory=list)
    #: Centre [grid | preview] split sizes (split-view layout redesign
    #: 2026-07, candidate A) as ``[grid_px, preview_px]``.  Empty = the
    #: 55:45 default.  The persist path substitutes the remembered split
    #: ratio while the preview is maximised, so a leading 0 is never
    #: written (the app always starts in the split view).
    center_split_sizes: list[int] = Field(default_factory=list)
    #: Centre preview-column visibility (折り畳み導線 2026-07).  ``False`` =
    #: the preview seat of the centre split is collapsed to width 0 (grid-only
    #: look); the remembered split ratio still lives in ``center_split_sizes``
    #: so re-showing restores it.  Toggled via the toolbar pane button /
    #: 表示 popover check / F6.
    preview_visible: bool = True
    #: Right-pane 情報パネル visibility.  Toggled via
    #: the 表示 popover check / F8; when hidden the centre pane widens.
    info_panel_visible: bool = True
    #: Left-pane ナビレール visibility.  Toggled via
    #: the 表示 popover check / F7; when hidden the centre pane widens.  Defaults
    #: to shown so the rail's library / bookmark / saved-search targets are
    #: discoverable out of the box.
    nav_rail_visible: bool = True
    last_root: str = ""
    # Startup resume (B01/B02): the left-pane tile that was selected and the
    # file that was previewed when the app last saved state, plus the left
    # pane's scroll offset.  Restored on launch (gated by
    # ``restore_selection_on_startup``) so "前回見ていた画像" is one launch
    # away instead of a manual re-navigation.
    last_selected_path: str = ""
    last_previewed_path: str = ""
    last_grid_scroll: int = 0
    restore_selection_on_startup: bool = True
    # Most-recently-used top-level roots (newest first, deduplicated, capped
    # at ``RECENT_ROOTS_MAX``).  Updated whenever the user changes the root at
    # the top level (root picker / bookmark jump / launch), NOT on ordinary
    # drill-downs.  Drives the ファイル → 最近開いたフォルダ submenu.
    recent_roots: list[str] = Field(default_factory=list)
    bookmarks: list[str] = Field(default_factory=list)
    # Bookmark display names (path -> user label).  The ordered list of
    # bookmark paths stays in ``bookmarks`` (backwards compatible with older
    # state files); a path missing from this map falls back to showing the
    # path itself.  Entries are pruned alongside ``bookmarks`` removals.
    bookmark_names: dict[str, str] = Field(default_factory=dict)
    sort_mode: SortMode = "name_asc"
    # Right-pane (FileListView) minimal sort, chosen from the ⋯ menu.  Plain
    # ``str`` (not ``Literal``) on purpose — same forward-compat rationale as
    # the ``tag_search_*`` enum-ish keys below; the menu only writes known
    # keys ("default" / "name" / "mtime" / "type").  "default" = the
    # historical scanner order (dirs first, post.md leading the files).
    file_list_sort_mode: str = "default"
    icon_size: int = 160
    # UI theme.  NOTE (2026-07): the theme's single source of truth is now
    # ``data/shared_prefs.json`` (``common/shared_prefs.py``), shared with
    # plugin windows.  ``viewer/app.py`` resolves the effective theme via
    # ``resolve_startup_theme(state.theme)`` (shared wins, else this local
    # value is promoted into shared once) and folds the winner back here so the
    # theme menu's checked state stays right.  This field is kept only for
    # round-trip compatibility, NOT as the authority.
    theme: ThemeName = "system"
    # UI language (i18n locale).  Authority is ``data/shared_prefs.json`` (see
    # ``theme`` above); ``viewer/app.py`` resolves the effective locale via
    # ``resolve_startup_language(state.language)`` and folds the winner back
    # here.  Plain ``str`` (not Literal) so a future locale in the state file
    # never fails validation — same forward-compat rationale as the enum-ish
    # ``str`` fields below.
    language: str = "ja"
    # DEPRECATED as a persisted value.  「ロックありのみ」
    # is a *search* dimension — it narrows what the grid shows — so it obeys
    # the same 「検索は揮発、設定は永続」 rule as every other query axis (see the
    # ``tag_search_enabled`` note below): the field survives for round-trip
    # compatibility, but :func:`load_state` drops whatever is on disk and
    # :func:`save_state` no longer writes it.  A ``True`` left in an older state
    # file used to come back as an invisible filter behind the ⋯ menu on the
    # next launch ("半分のフォルダが消えた").  The toggle itself moved to the
    # フィルタ popover alongside the other axes.
    filter_locked_only: bool = False
    grid_view_mode: ViewMode = "icon"
    file_list_view_mode: ViewMode = "list"
    file_list_icon_size: int = 120
    # Per-mode icon sizes — the slider value is preserved separately for
    # icon mode (above) and list mode (below) so toggling the view mode
    # restores the size the user last chose for that mode.  Defaults
    # mirror the historical hard-coded list-mode icon sizes.
    list_icon_size: int = 24
    file_list_list_icon_size: int = 20
    exclude_thumb_marker: bool = False
    preview_scroll_pixels: int = 120

    # Thumbnail layout mode per pane (icon view only; list view is always
    # uniform).  "square": uniform square cells.  "justified": Eagle-style
    # row-based layout where each row fills the width and tiles keep their
    # aspect ratio (no padding gutters).  Default justified.
    grid_thumb_layout: ThumbLayout = "justified"
    file_list_thumb_layout: ThumbLayout = "justified"

    # Aspect-ratio probe header-read concurrency (shares the NAS SMB credit
    # budget with the thumbnail + metadata workers — keep low).
    aspect_probe_parallelism: int = 2

    # On-disk thumbnail cache (Eagle-style instant revisit / restart).
    # Independent byte budget from the aspect cache; never evicts aspect
    # rows.  Master thumbnails are stored at ``thumb_disk_cache_max_edge``
    # longest edge (downscale-only; larger tiles decode the original).
    thumb_disk_cache_enabled: bool = True
    thumb_disk_cache_max_mib: int = 2048
    thumb_disk_cache_max_edge: int = 1024
    # Independent byte budget for the tiny aspect-ratio cache.
    aspect_cache_max_mib: int = 256

    # Folder-preview resolution cache (which child image represents a folder
    # + that folder's post.md metadata), keyed by folder path + mtime.  Lets
    # a folder revisit skip its per-folder scandir / post.md read — the NAS
    # round-trip that re-ran every visit even when the thumbnail bytes were
    # already cached.  Own DB file + byte budget; rows are small so a modest
    # budget holds a very large library.
    folder_preview_cache_enabled: bool = True
    folder_preview_cache_max_mib: int = 64

    # Search index (file-name + post.md full-text), keyed by path + (mtime,
    # size).  An *index* (queried by prefix to enumerate matches), unlike the
    # point-lookup caches above; the live walk always runs in parallel
    # (stale-while-revalidate) so a cold / stale index only means more live
    # work, never a wrong result.  Own DB file + combined byte budget.
    # ``use_fts`` reserves the trigram FTS5 candidate prefilter (off by
    # default — the raw-text Python match is the authority).
    search_index_enabled: bool = True
    search_index_max_mib: int = 128
    search_index_build_parallelism: int = 8
    search_index_use_fts: bool = False

    # Preview image caches.  All byte values are in MiB so the settings
    # dialog can use plain integer spinboxes.  Defaults mirror the
    # module-level constants in ``image_cache.py``; on first launch the
    # state file doesn't exist so these take effect.
    markdown_cache_max_mib: int = 256
    markdown_cache_max_entries: int = 128
    markdown_cache_max_single_mib: int = 64
    # 2026-07: 512 → 2048 MiB.  A modern 5000×7000 RGBA CG page decodes to
    # ~130 MB; the old budget held only 3 such pages, so prefetch churned
    # the current/next entries out of the LRU (``load_state`` migrates a
    # persisted 512 — the old default — up to 2048).
    imageview_cache_max_mib: int = 2048
    imageview_cache_max_entries: int = 16
    imageview_cache_max_single_mib: int = 200
    imageview_prefetch_radius: int = 2

    # Thumbnail slider upper bounds (pixels).  Defaults match the
    # former hardcoded ``_ICON_SIZE_MAX`` / ``_ICON_THUMB_SIZE_MAX``.
    post_grid_icon_size_max: int = 320
    file_list_icon_size_max: int = 320

    # I/O concurrency tuning knobs.  Defaults match former module
    # constants in ``scan_worker.py`` / ``thumbnail_loader.py`` so
    # behaviour is unchanged for existing users on upgrade.
    scan_metadata_parallelism: int = 6
    thumbnail_cache_size: int = 256
    thumbnail_max_threads: int = 4

    # Which performance preset the settings dialog's パフォーマンス tab shows
    # as selected ("standard" / "nas" / "custom").  Purely a UI memo — the
    # actual tunables above stay the single source of truth (a preset just
    # writes its value set into them on selection), so older state files and
    # hand-edited knobs keep working unchanged.  Plain ``str`` for the same
    # forward-compat reason as ``file_list_sort_mode``.
    perf_preset: str = "standard"

    # Pre-cache builder (CacheBuilder) concurrency.  Deliberately decoupled
    # from the live pools above: the builder runs as an EXCLUSIVE modal op,
    # so nothing else competes for SMB credits and it can push far wider than
    # the live-browse limits.  ``walk`` = parallel ``os.scandir`` directory
    # listers; ``aspect`` = header-only reads (tiny + latency-bound, scale
    # almost linearly with parallelism); ``full`` = decode+encode+disk-write
    # (bounded by CPU + NAS bandwidth, so kept more modest).  Tunable via the
    # state file for NAS shares that throttle aggressively.
    cache_build_walk_parallelism: int = 8
    cache_build_aspect_parallelism: int = 24
    cache_build_full_parallelism: int = 8

    # Background (non-modal) cache build.  When enabled the builder runs
    # while the user keeps browsing, so its pools must stay well below the
    # exclusive-modal knobs above — they SHARE the NAS SMB credit window
    # with the live thumb(4) + metadata(6) + probe(2) pools.  Progress is
    # shown in the status bar with pause / resume / cancel controls.
    cache_build_background: bool = True
    cache_build_bg_walk_parallelism: int = 2
    cache_build_bg_aspect_parallelism: int = 6
    cache_build_bg_full_parallelism: int = 2
    cache_build_bg_index_parallelism: int = 2

    # Maximum ZIP size that the viewer will read into the central-
    # directory preview *and* extract to a temp directory when the user
    # double-clicks the archive.  Larger ZIPs fall back to a "size only"
    # placeholder so a stray click on a giant archive can't freeze the
    # UI or fill the temp directory.
    zip_preview_size_limit_mib: int = 50

    # Maximum PDF size the centre preview reads into memory.  QPdfView
    # renders pages lazily so the buffer stays resident while the PDF is
    # shown (peak 2x during load), and the preview is reached automatically
    # when a folder's representative file is a PDF — an unbounded read meant
    # a 200 MB scanned book cost that much RSS on a single click.
    # Over the cap: size-only notice.
    pdf_preview_size_limit_mib: int = 100

    # Media preview (audio / video) settings.
    media_autoplay: bool = True
    media_volume: int = 70  # 0–100
    # Loop the current media file when playback reaches the end.
    media_loop: bool = False
    # 再生速度。隣の loop / volume と同じ 3 経路（MediaView の emit →
    # ContentView / Lightbox の再送出 → 窓の state 書き込み）で持つ — 2 つの
    # MediaView は別インスタンスなので、ここに無いと分割ビューで 1.5x に
    # しても F11 の全画面側は 1.0x のままになる。
    media_playback_rate: float = 1.0

    # Markdown preview font size in points.  0 = follow the application
    # default font.  Changed live with Ctrl+wheel in the MarkdownView and
    # persisted here on close.
    markdown_font_pt: int = 0

    # 閲覧モード (fullscreen lightbox): slideshow auto-advance interval in
    # seconds.  Changed in the settings dialog (表示タブ → 画像プレビュー)
    # and applied live to an open lightbox.
    slideshow_interval_sec: int = 5
    # 閲覧モード (fullscreen lightbox): how long after the last mouse / key
    # activity the on-screen chrome (top bar, bottom control capsule,
    # filmstrip, cursor) auto-hides, in milliseconds.  Changed in the
    # settings dialog (表示タブ → 画像プレビュー, entered in seconds) and
    # applied live to an open lightbox (``LightboxWindow.set_chrome_hide_ms``).
    lightbox_chrome_hide_ms: int = 1500

    # ImageView: keep the current zoom factor / fit mode when navigating to
    # the next or previous file (comparison workflow).  Off = every image
    # opens fit-to-window, the historical behaviour.
    image_zoom_persist: bool = False
    # ImageView: show the minimap overlay (whole-image thumbnail + viewport
    # rectangle, bottom-right) while zoomed in past the viewport.
    image_minimap_enabled: bool = True
    # ImageView wheel assignment (F01).  False (default) keeps the historical
    # mapping: a plain wheel steps to the previous / next sibling and
    # Ctrl+wheel zooms.  True swaps them (plain wheel zooms, Ctrl+wheel steps)
    # for a canvas-style preview.  Applied live via ``view_prefs``.
    image_wheel_zoom: bool = False
    # ImageView fit ceiling (F03).  True (default = the new, better behaviour)
    # never scales an image past 100% when fitting to the window, so a small
    # image shows crisp at natural size instead of blurred and stretched.
    # False restores the historical "always fill the viewport" fit.
    image_fit_no_upscale: bool = True

    # Caption customisation (PostGrid tiles).  Which optional parts appear
    # in the caption subtitle under each tile.  ``show_post_favorites``
    # above governs the ♡N overlay badge and predates these.
    caption_show_posted: bool = True     # posted date (folders)
    caption_show_locked: bool = True     # 🔒N locked-content count (folders)
    caption_show_size: bool = True       # file size (files)
    caption_show_plan: bool = False      # plan name / price (folders)

    # Where the tile name label sits on the central post grid (owner request
    # 2026-07 — legibility).  "overlay" (default = current look) rides the
    # name on a bottom gradient scrim ON the thumbnail; "below" drops it OUT
    # of the image onto a surface-token band directly under the thumbnail, so
    # the text never sits over busy photo content (the thumbnail area is not
    # encroached — the tile just grows taller by the caption strip).  Plain
    # ``str`` (not ``Literal``) for the same forward-compat reason as the
    # other enum-ish keys — the settings combo only ever writes known values,
    # and an unknown value falls back to the overlay default.
    tile_name_placement: str = "overlay"   # overlay / below

    # Overlay the post favorite / like count (``♡N``) in the bottom-left
    # corner of each PostGrid thumbnail.  Reads ``FolderEntry.favorites``
    # (already parsed from the ``- favorites:`` post.md meta line); tiles
    # without a count just omit the badge.
    show_post_favorites: bool = True

    # Maximum bytes of body to load into the text preview pane.  Files
    # over this size still show but their body is truncated with a
    # notice (full content is available via "open with default app").
    text_preview_max_mib: int = 2

    # Grace period (milliseconds) the user must keep wheel-scrolling
    # past the edge before the viewer switches to the next/previous
    # file.  Set to 0 for instant navigation; higher values reduce
    # accidental file switches while reading long posts / tall images.
    wheel_nav_grace_ms: int = 450

    # Advanced search panel (PostGrid) — AI-tag / media-type / posted-date
    # filtering against the tagger's ``data/tags.db`` (read-only).  The enum-ish
    # keys are plain ``str`` (not ``Literal``) so a future combo option never
    # makes an older state file fail validation; the combos only ever write
    # known keys.  All default to the "inactive" values so a fresh install (and
    # any user without a tags.db) browses exactly as before.
    tag_search_panel_expanded: bool = False
    # DEPRECATED as a startup restore: the AI-tag search *enable* flag is now
    # volatile ("検索は揮発、設定は永続") — a persisted True used to survive
    # into the next launch as an invisible filter inside the collapsed panel
    # ("half my folders vanished").  The field stays for backward compat and
    # for the SearchSnapshot scratch round-trip (nav history), but
    # ``restore_tag_settings`` ignores it at startup and ``save_tag_settings``
    # no longer writes it.  The query/threshold/rating INPUTS below are still
    # persisted (settings), only the activation is volatile.
    tag_search_enabled: bool = False
    tag_search_query: str = ""
    # 製品既定値は条件次元レジストリの単一定数（0.35 の手書き複製を残さない
    # — 精度チップの engaged 判定も同じ定数と比較する）。
    tag_search_threshold: float = DEFAULT_TAG_THRESHOLD
    tag_search_media_type: str = "all"      # all/image/video/audio/document/archive
    tag_search_folder_mode: bool = True      # True = show folders, False = files
    # Folder-mode AND semantics: True = coverage (a folder qualifies if its
    # images COLLECTIVELY cover every include tag), False = strict (a single
    # image must carry all include tags).  Ignored in file mode.
    tag_search_folder_coverage: bool = True
    # all/safe/sfw/explicit (severity band)。投稿日と同じく **SAVED BUT NOT
    # RESTORED AT STARTUP**: 有効化フラグが揮発のため、起動時に非 all
    # を復元すると「適用されないチップ」だけが出る。セッション内復元
    # （restore_enabled=True）は従来どおり帯を往復する。
    tag_search_rating: str = "all"
    # 投稿日フィルタ。**SAVED BUT NOT RESTORED AT STARTUP** — an intentional
    # asymmetry that looks like a defect from the state file alone:
    # ``save_tag_settings`` writes all three, but
    # ``restore_tag_settings`` forces the preset back to "all" unless
    # ``restore_enabled=True``, which only the *session-internal* restores (nav
    # history 戻る / 保存した検索) pass.  Rationale is the same 「検索は揮発、
    # 設定は永続」 rule as ``tag_search_enabled``: a persisted 投稿日 window would
    # come back next launch as an invisible filter inside a collapsed popover.
    # The values are still written so the two session-internal restores can
    # round-trip through a scratch ``ViewerState``; ``tag_date_start`` /
    # ``tag_date_end`` additionally seed the range edits' initial dates.
    tag_date_preset: str = "all"             # all/today/7d/30d/1y/range
    tag_date_start: str = ""                 # ISO date (YYYY-MM-DD) or ""
    tag_date_end: str = ""                   # ISO date (YYYY-MM-DD) or ""

    # NSFW view suppression.  A **view setting**, not a
    # search dimension ("検索は揮発、設定は永続" — this one persists): when on,
    # the plain browse grid hides tiles whose representative rating is at or
    # above the chosen band.  "off" shows everything (historical behaviour),
    # "explicit" hides R-18 (dominant ``images.rating`` == "explicit"),
    # "questionable" hides R-15 相当以上 (questionable + explicit).  Plain
    # ``str`` (not ``Literal``) for the same forward-compat reason as the
    # ``tag_search_*`` keys — the combo only ever writes known keys, and an
    # unknown value falls back to "off".  Requires a tags.db to have any
    # effect (folder / image ratings come from it); the toggle is disabled
    # with a reason when no tags.db is loaded.
    hide_nsfw: str = "off"       # off / questionable / explicit

    # Saved searches / smart folders (M03).  Each entry is a serialised search
    # snapshot with a user-given ``name`` — the filter needle, the サブフォルダ
    # search toggle, the filter-bar media predicate, and the advanced-search
    # (AIタグ / 精度 / 種別 / 年齢区分 / 投稿日 / 表示単位) inputs.  Semantic /
    # 類似画像 seeds are volatile by design and are NOT saved (mirrors the
    # SearchSnapshot persistence policy).  Applied against the CURRENT root via
    # ``PostGrid.restore_search_state``.  ``list[dict]`` (not a typed model) so a
    # future serialiser field never fails validation on an older build.
    saved_searches: list[dict] = Field(default_factory=list)

    # Navigation: when drilling into a folder while a search is active (filter
    # box / recursive / advanced tag search), clear the search so the
    # destination folder shows its own contents normally, then restore that
    # exact search state when the user presses 戻る (back).  When off, the
    # search persists across navigation (the historical behaviour).  The
    # folder-*selection* restore on 戻る is unconditional and NOT governed by
    # this flag.
    search_clear_on_navigate: bool = True

    # NOTE (split-view redesign 2026-07): the former ``post_open_initial``
    # setting ("投稿を開いたとき最初に表示") was removed — the uniform folder
    # click always previews the representative image, so the switch lost its
    # reason to exist.  A persisted value in an older state file is silently
    # ignored on load (pydantic drops unknown keys); nothing is migrated.


def _state_path():
    return get_paths().data / "viewer_state.json"


#: ``viewer_state.json`` の **読み取り** と **``os.replace`` による着地** だけを
#: 直列化するプロセス内ロック。
#:
#: Windows のファイル削除・改名は、対象を開いている全ハンドルが
#: ``FILE_SHARE_DELETE`` を持っているときにしか通らない。Python の ``open`` /
#: ``read_text`` はそれを付けないので、同一プロセス内で A の
#: ``load_state``（ブックマーク突き合わせの読み取り）と B の ``save_state``
#: （``os.replace(tmp, viewer_state.json)``）が重なると、**読み取りに巻き
#: 込まれて保存が丸ごと落ちる** — 実害は保存の喪失に加えて、事実でない
#: 「保存できませんでした」の常駐警告（:func:`save_state` の戻り値の経路）。
#:
#: **ロックが囲うのはこの 2 つの短い操作だけ**で、時間のかかる tmp 書き込みは
#: ロックの外に置く（:func:`_write_json_atomic`）。「詰まったオートセーブが
#: closeEvent の 3 秒予算をロック待ちで溶かす」を起こさないための線引き: 囲うのが「小さな JSON の read + 1 回の replace」
#: なら、健全な保存先でのロック待ちはミリ秒で有界。死んだ共有では待ちも
#: 有界ではなくなるが、そのとき保存を行っているのは予算付きワーカー
#: （``main_window._save_merged_state``）なので、GUI スレッドは待たずに
#: 放棄して先へ進む — closeEvent の予算は依然として守られる。
#:
#: **ロック階層**: このロックを握ったまま呼ぶのは ``should_land`` フックだけで、
#: その先（``main_window._claim_state_generation``）が取る ``_state_write_lock``
#: は I/O をせず即返る。逆向き（``_state_write_lock`` を握ったままここへ入る）
#: 経路は存在しないので、順序は ``_STATE_FILE_LOCK`` → ``_state_write_lock``
#: の一方向。**この向きを崩す変更をしないこと**（デッドロックになる）。
_STATE_FILE_LOCK = threading.Lock()


def load_state() -> ViewerState:
    path = _state_path()
    if not path.exists():
        return ViewerState()
    try:
        # 読み取りだけをロックで囲う（並行する ``os.replace`` を待たせる）。
        with _STATE_FILE_LOCK:
            raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # ``UnicodeDecodeError`` は ``ValueError`` の子で ``OSError`` でも
        # ``JSONDecodeError`` でもない — 明示的に並べないとすり抜ける。
        # 非 UTF-8 の viewer_state.json
        # （ANSI 既定のエディタで上書き保存 / USB の 1 バイト破損）でここが
        # 抜けると、``app.py`` の ``load_state()`` 呼び出しにはガードが無く
        # プロセス最上位まで抜け、windowed 凍結ビルドでは stderr も無い
        # 「起動しても何も起きない」になる。壊れたファイルは defaults へ
        # 落とす既存規約と同じ扱いにする。
        logger.warning("viewer_state.json could not be read ({}); using defaults", exc)
        return ViewerState()

    if not isinstance(data, dict):
        logger.warning("viewer_state.json is not an object; using defaults")
        return ViewerState()

    # Migrate the interim "fit" thumb-layout (superseded by the proper
    # justified layout) so a state file written by an earlier build
    # loads cleanly under the new ``ThumbLayout`` literal.
    for key in ("grid_thumb_layout", "file_list_thumb_layout"):
        if data.get(key) == "fit":
            data[key] = "justified"

    # 2026-07: the ImageView PIL-cache default grew 512 → 2048 MiB (see the
    # field comment).  A persisted 512 is overwhelmingly the old default
    # rather than a deliberate choice, so lift it to the new default.
    # Trade-off: a user who deliberately re-enters exactly 512 MiB gets
    # bumped again on the next launch — pick any other value to opt out.
    if data.get("imageview_cache_max_mib") == 512:
        data["imageview_cache_max_mib"] = 2048

    # 「ロックありのみ」 is volatile ("検索は揮発、設定は永続").  Read-and-discard
    # rather than reject, so an older state file that persisted it still loads
    # cleanly — the value is
    # simply ignored and the session starts unfiltered.
    data.pop("filter_locked_only", None)

    # Be resilient to a single bad field: rather than discard the WHOLE
    # state file (losing geometry, last_root, bookmarks, every cache knob)
    # when one value fails validation, drop only the offending top-level
    # keys and retry so the rest of the user's settings survive.  This is
    # the safety net for the recurring "a new enum option was added to the
    # UI but the matching Literal here wasn't updated" mistake — an unknown
    # ``sort_mode`` then just falls back to its default instead of nuking
    # the file.  ``len(data)`` bounds the retries (one dropped key each).
    for _ in range(len(data) + 1):
        try:
            return ViewerState.model_validate(data)
        except ValidationError as exc:
            bad_keys = {
                err["loc"][0]
                for err in exc.errors()
                if err.get("loc") and isinstance(err["loc"][0], str)
            }
            bad_keys &= set(data)
            if not bad_keys:
                break
            logger.warning(
                "viewer_state.json: dropping invalid field(s) {} (using defaults "
                "for those)", sorted(bad_keys),
            )
            for key in bad_keys:
                data.pop(key, None)
    logger.warning("viewer_state.json could not be validated; using defaults")
    return ViewerState()


def _write_json_atomic(path, data: dict, *, should_land=None) -> bool:
    """Write *data* to *path* as JSON via a temp sibling + ``os.replace``.

    Returns **True** if the write landed (``False`` = *should_land* said no —
    tmp を捨てて静かに戻った。例外は投げない）。

    *should_land* は「**着地の直前**にもう一度だけ訊く」任意のゲート。
    ``False`` を返したら ``os.replace`` を行わず tmp を
    捨てて静かに戻る。死んだ共有では tmp への書き込みだけで数十秒かかるので、
    書き始めた時点の「自分が最新」という判断はそこで陳腐化している — 呼び出し
    側の書き込みを**ロックで直列化する代わりに**、追い越された書き込みを着地
    寸前で捨てるためのフック。直列化に戻してはいけない理由は
    ``main_window._save_merged_state`` の docstring（詰まったオートセーブが
    closeEvent の保存予算を丸ごと食う）。

    一時ファイル名は ``tempfile.mkstemp`` で**呼び出しごとに固有**にする。
    固定名 ``viewer_state.json.tmp`` を共有すると、``persist_bookmarks`` の
    docstring が明示的にサポートする「2 インスタンス並行書き込み」で、A の
    ``os.replace`` と B の書き込み（切り詰め→書き込みの非アトミック 2 段）が
    交錯すると空/部分書き込みの tmp が ``viewer_state.json`` として置換され
    得る — 壊れた JSON は次回
    ``load_state`` で defaults へ落ち、merge 機構が守ろうとしたブックマークも
    含めて全設定を失う。

    ``except BaseException: unlink`` の意図（残骸を溜めない）を**例外を
    投げずに放棄される経路**でも守るため、置換に成功した直後に古い残骸を
    掃除する（:func:`_reap_stale_tmps`）。予算付き
    teardown はワーカーごと放棄するので、この関数のどの ``except`` も通ら
    ないまま ``<name>.<rand>.tmp`` が残る（実測: 中断 3 回で 3 個）。

    **着地（``os.replace``）だけは :data:`_STATE_FILE_LOCK` の下で行う**:
    同一プロセスの ``load_state`` が読み取り用に開いて
    いる間、Windows では改名が ``PermissionError`` で落ちる。tmp への書き込み
    （＝時間のかかる方）はロックの外に残す — 詳しくはロックの docstring。
    ``should_land`` の最終確認も同じロックの中へ入れる: 「確認 → 着地」の間に
    別スレッドの着地を挟ませないため（隙間があると、新しい方が先に着地した
    直後に古い方が上書きし得る）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        with _STATE_FILE_LOCK:
            if should_land is not None and not should_land():
                os.unlink(tmp_name)
                return False
            os.replace(tmp_name, path)
    except BaseException:
        # 置換前に落ちたら中間ファイルを残さない（固定名と違い、残骸は
        # 次回の書き込みで上書きされず溜まる一方になるため）。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _reap_stale_tmps(path)
    return True


#: :func:`_reap_stale_tmps` が「もう誰も書いていない」と見なすまでの猶予 (秒)。
#: 並行インスタンス / 放棄されたワーカーが *いま書いている途中* の tmp を
#: 消さないためのマージン（1 回の書き込みは健全な保存先ならミリ秒、死んだ
#: 共有でも SMB の I/O タイムアウト 195 秒（VM 実測）を超えない）。
_STALE_TMP_AGE_S = 600.0


def _reap_stale_tmps(path, max_age_s: float = _STALE_TMP_AGE_S) -> None:
    """*path* の書き込みが残した古い ``.tmp`` 兄弟を掃除する（ベストエフォート）.

    予算付き teardown（``common/teardown.py``）は保存ワーカーを**例外を投げ
    させずに放棄する**ので、``_write_json_atomic`` の ``except`` 節は通らず
    ``viewer_state.json.<rand>.tmp`` がそのまま残る。固有名なので次の書き込み
    が上書きすることもなく、「残骸は溜まる一方」が
    現実になる — 実測で中断 3 回 = 3 個。

    掃除は**置換に成功した直後にだけ**行う: そのタイミングなら保存先が応答
    していることが実証済みで、死んだ共有で余計な I/O を積まない。*max_age_s*
    より新しい tmp は「いま書いている途中かもしれない」ので残す。unlink の
    失敗は無視する（Windows では他プロセスが開いている tmp は消せない = 生きた
    書き手が構造的に保護される）。
    """
    cutoff = time.time() - max_age_s
    try:
        stale = [
            p for p in path.parent.glob(f"{path.name}.*.tmp")
            if p.is_file() and p.stat().st_mtime < cutoff
        ]
    except OSError:  # pragma: no cover (defensive)
        return
    for leftover in stale:
        try:
            leftover.unlink()
        except OSError:
            pass


#: Model fields that are deliberately NOT written to disk — volatile search
#: state that only lives on the model for the session-internal round-trips
#: (nav history / saved searches).  See the field comments for the rationale.
_VOLATILE_FIELDS = ("filter_locked_only",)


def save_state(state: ViewerState, *, should_land=None) -> bool:
    """Write the whole state file.  Returns **False** if the write failed.

    *should_land* は :func:`_write_json_atomic` へそのまま渡す「着地直前の
    最終確認」。``False`` を返した場合も戻り値は ``True`` ＝「失敗ではない」
    — 呼び出し側が意図的に降りただけなので、「保存できませんでした」警告を
    出してはならない。

    書き込み不可な NAS / 読み取り専用メディアでは設定・ブックマーク・保存
    した検索が消えるので、失敗をログにだけ落とさず成否を返し、呼び出し元が
    「保存しました」と偽らずに利用者へ伝えられるようにする（伝達様式は
    ``ViewerWindow._notify_persist_failed`` = user_meta.db と同型の
    セッション 1 回・常駐警告トースト）。
    """
    path = _state_path()
    data = state.model_dump()
    for key in _VOLATILE_FIELDS:
        data.pop(key, None)
    try:
        _write_json_atomic(path, data, should_land=should_land)
    except OSError as exc:
        logger.warning("Failed to write viewer_state.json: {}", exc)
        return False
    return True


def _read_partial_write_base(what: str) -> tuple[dict, bytes | None] | None:
    """Read ``viewer_state.json`` as a raw dict for a partial (few-keys) write.

    Shared front half of :func:`persist_bookmarks` /
    :func:`persist_saved_searches`（実際の書き戻しは :func:`_persist_partial`）。
    返すのは ``(dict, 読んだ生バイト列)`` の組で、2 番目は
    :func:`_persist_partial` の**楽観ロック**が着地直前の照合に使うスナップ
    ショット（ファイルが無い / 壊れていて退避した後は ``None``）。読み取り
    そのものに失敗したときの扱いは ``shared_prefs`` と同じ二分法:

    * **読めない（OSError）** ≠ 壊れている — 別プロセス（アンチウイルス /
      バックアップ）が一瞬掴んでいるだけかもしれない。ここで「対象キーだけ
      のファイル」を書くと、ディスク上のジオメトリ・last_root・全キャッシュ
      設定が消える。``None`` を返して呼び出し元に書き込みを諦めさせ、終了時
      のフル ``save_state`` に委ねる。
    * **本当に壊れている（JSON として読めない / オブジェクトでない）** —
      ``viewer_state.json.bak`` へ退避してから空 dict を返す（最小ファイル
      を書く）。残骸を黙って上書きしない（手で直せる余地を残す）。
    """
    path = _state_path()
    if not path.exists():
        return {}, None
    try:
        # ``load_state`` と同じ理由でロックの下で読む:
        # 読み取り用に開いている間、並行する ``os.replace`` は Windows で
        # 失敗する。照合用スナップショットを取るため**バイト列で**読み、
        # デコードは下で行う（非 UTF-8 でも「読んだ生の中身」は残る）。
        with _STATE_FILE_LOCK:
            snapshot = path.read_bytes()
    except OSError as exc:
        logger.warning(
            "viewer_state.json could not be read while persisting {} ({});"
            " leaving the file untouched ({} stay in memory until the next"
            " full save)", what, exc, what,
        )
        return None
    try:
        raw = snapshot.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 非 UTF-8 のバイト列は「読めない」ではなく「壊れている」側。
        # ``UnicodeDecodeError`` は
        # ``OSError`` の子ではないので明示的に捕まえないと Qt スロットの
        # 外まで抜ける（「この検索を保存…」が無反応になる）。空文字を
        # 下の ``json.loads`` に渡して、退避 + 最小ファイル書き直しの
        # 「壊れている」分岐へ合流させる。
        logger.warning("viewer_state.json is not valid UTF-8 ({})", exc)
        raw = ""
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("viewer_state.json is corrupted ({})", exc)
        loaded = None
    if isinstance(loaded, dict):
        return loaded, snapshot
    logger.warning(
        "viewer_state.json is not a usable state file; backing it up"
        " and rewriting the {} fields only", what,
    )
    try:
        with _STATE_FILE_LOCK:
            path.replace(path.with_suffix(".json.bak"))
    except OSError as exc:
        logger.warning("could not back up viewer_state.json: {}", exc)
        # 退避できなかった＝壊れたファイルはそのまま残っている。照合の
        # 基準は「いま読んだ中身」で正しい。
        return {}, snapshot
    return {}, None


#: 部分書き込み（:func:`_persist_partial`）が「読んだ内容が着地直前も変わって
#: いない」ことを確かめてやり直す回数の上限。1 回の衝突は健全な保存先でも
#: 起こり得る（2 インスタンス構成は :func:`persist_bookmarks` の docstring が
#: 明示的にサポートする）が、4 回連続で衝突するのは相手が書き続けていると
#: いうことなので、そこで諦めて終了時のフル :func:`save_state` に委ねる
#: （無条件に着地させると、この機構が防いでいる巻き戻りを自分で起こす）。
_PARTIAL_WRITE_ATTEMPTS = 4


def _current_state_bytes(path) -> bytes | None:
    """*path* のいまの中身（無ければ ``None``、読めなければ ``b""``）.

    :func:`_persist_partial` の照合専用。``_STATE_FILE_LOCK`` を**握ったまま**
    呼ばれる（``_write_json_atomic`` の ``should_land`` の中）ので、ここで
    ロックを取らないこと。
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        # 読めないなら比較しようがない。従来どおり着地させる（判定不能を
        # 理由にユーザーの追加を捨てる方が実害が大きい）。
        return b""


def _persist_partial(what: str, apply_fields):
    """対象キーだけを ``viewer_state.json`` へ書き戻す（楽観ロックつき）.

    :func:`persist_bookmarks` / :func:`persist_saved_searches` の共通後半。
    *apply_fields* は読んだ生 dict を受け
    取り、**自分の管轄キーだけ**を書き換えて、呼び出し元へ返す値を返す。

    **なぜ楽観ロックが要るか**: 部分書き込みは「読んだ dict 全体を書き戻す」
    read-modify-write なので、読み取り (T0) と ``os.replace`` (T2) の間に別の
    書き手が着地させた内容 (T1) は、自分の管轄キー以外**すべて** T0 の値へ
    巻き戻る。``persist_bookmarks`` にとって ``saved_searches``（＝再生成
    できないユーザーデータ）は素通しのフィールドにすぎず、逆向きも同型。
    merge 機構（:func:`merge_bookmarks` / :func:`merge_saved_searches`）が
    守るのは自分の 2 キーだけなので、この巻き戻りは防げない。
    ``_STATE_FILE_LOCK`` も**プロセス内の** ``threading.Lock`` に過ぎず、
    2 インスタンス構成（:func:`persist_bookmarks` の docstring が明示的に
    サポートする）では効かない。窓の幅は「読み取り → dumps → mkstemp →
    tmp 書き込み → replace」で、健全なローカルならミリ秒だが、tmp 書き込みが
    遅い保存先（NAS / USB / AV スキャン中）では秒オーダーまで開く — merge
    機構がそもそも存在する理由と同じ確率帯。

    **守り方**: 読んだ生バイト列を憶えておき、``os.replace`` の**直前**
    （``_STATE_FILE_LOCK`` の中）にもう一度読んで一致を確かめる。変わって
    いたら tmp を捨てて**読み直しからやり直す** — 再試行は新しいディスク値に
    対して merge をやり直すので、相手の変更を取り込んだ上で自分の変更も載る。
    衝突窓は「tmp 書き込みぶん（秒オーダー）」から「同じロックの中の 1 read
    （マイクロ秒）」まで縮む。**ロックの中で行うのは短い読み取りだけ**（tmp
    書き込みは外）なので、:data:`_STATE_FILE_LOCK` の docstring が定める
    線引きは崩れない。

    Returns
    -------
    tuple[object | None, bool]
        ``(apply_fields の戻り値, 書き込みに失敗しなかったか)``。1 番目が
        ``None`` は「ディスクが読めず書き込みを見送った」＝呼び出し元は自分の
        in-memory 値をそのまま返すこと（保存失敗の警告には数えない）。

        再試行を出し切って諦めた場合は 2 番目が ``False`` になる — 値は
        in-memory に残るだけでディスクには載っていないので、呼び出し元が
        「保存しました」と名乗ってよい状態ではない（成否表示の契約は、
        載らなかった保存を成功と言わないこと側も含む）。「読めなくて見送った」
        との区別は従来どおり 1 番目が ``None`` かどうかが担う。
    """
    path = _state_path()
    result = None
    for _ in range(_PARTIAL_WRITE_ATTEMPTS):
        base = _read_partial_write_base(what)
        if base is None:
            return None, True
        data, snapshot = base
        result = apply_fields(data)
        try:
            landed = _write_json_atomic(
                path, data,
                should_land=lambda snap=snapshot: (
                    _current_state_bytes(path) == snap
                ),
            )
        except OSError as exc:
            logger.warning("Failed to persist {}: {}", what, exc)
            return result, False
        if landed:
            return result, True
        logger.debug(
            "{}: the state file changed under the partial write; retrying",
            what,
        )
    logger.warning(
        "{} could not be persisted without clobbering a concurrent write"
        " ({} attempts); the values stay in memory until the next full save",
        what, _PARTIAL_WRITE_ATTEMPTS,
    )
    return result, False


def persist_bookmarks(
    bookmarks: list[str],
    names: dict[str, str],
    removed: set[str] | None = None,
    cleared_names: set[str] | None = None,
) -> tuple[list[str], dict[str, str], bool]:
    """Write ONLY the bookmark fields into ``viewer_state.json`` right now.

    Bookmarks used to be persisted solely by the full ``save_state`` in
    ``closeEvent`` — a crash / kill / OS shutdown lost every bookmark added
    that session, and with two viewer instances the stale one's close
    clobbered the other's additions.  Mutation handlers call this
    immediately instead.  Every OTHER field on disk is left untouched (raw
    round-trip, no model validation), so this never overwrites settings a
    concurrent instance saved in the meantime.

    「他のフィールドに触らない」は**楽観ロックで担保している**。生 JSON 往復は「読んだ dict 全体を書き戻す」
    read-modify-write なので、素朴に書くと読み取りと ``os.replace`` の間に
    別インスタンスが着地させた変更（例: 保存済み検索の追加）を丸ごと巻き
    戻す。:func:`_persist_partial` が着地直前にディスクの中身を照合し、
    変わっていたら読み直して merge からやり直す。

    The bookmark fields themselves are **merged** with whatever is on disk
    (via :func:`merge_bookmarks`) rather than blindly overwritten, so two
    instances adding bookmarks near-simultaneously don't drop each other's
    additions in the read→write window.  *removed* is this instance's
    session-removed set (``ViewerWindow._session_removed_bookmarks``) — disk
    entries this instance explicitly deleted stay deleted; every other disk
    addition is adopted.  ``None`` means "removed nothing" (empty set).
    *cleared_names* is the same idea for display names
    (``ViewerWindow._session_cleared_bookmark_names``): names this instance
    explicitly blanked stay blanked, while names another instance set in the
    meantime survive (:func:`merge_bookmarks`).

    読み取りに失敗したときの扱いは ``shared_prefs`` と同じ二分法:

    * **読めない（OSError）** ≠ 壊れている — 別プロセス（アンチウイルス /
      バックアップ）が一瞬掴んでいるだけかもしれない。ここで「ブックマーク
      2 キーだけのファイル」を書くと、ディスク上のジオメトリ・last_root・
      recent_roots・保存済み検索・全キャッシュ設定が消える（docstring が
      約束する "every OTHER field untouched" の破れ）。書き込みを諦めて
      呼び出し元の値をそのまま返し、終了時のフル ``save_state`` に委ねる。
    * **本当に壊れている（JSON として読めない / オブジェクトでない）** —
      ``viewer_state.json.bak`` へ退避してから最小ファイルを書く。残骸を
      黙って上書きしない（手で直せる余地を残す）。

    （読み取り側の実装は :func:`_read_partial_write_base` に共通化 —
    ``persist_saved_searches`` と同じ前半。）

    3 番目の返り値は**書き込みに失敗しなかったか**。読み取り不能で書き込みを見送った上記の分岐は「失敗」に数えない —
    in-memory の値は生きたまま終了時のフル :func:`save_state` が書くという
    設計上の意図的な先送りで、そこが本当に駄目なら save_state が False を
    返す。ここを失敗扱いにすると、アンチウイルスが一瞬掴んだだけで
    「保存できません」の常駐警告が出る。
    """
    def _apply(data: dict) -> tuple[list[str], dict[str, str]]:
        disk_bookmarks = data.get("bookmarks")
        disk_names = data.get("bookmark_names")
        merged, merged_names = merge_bookmarks(
            list(bookmarks),
            dict(names),
            disk_bookmarks if isinstance(disk_bookmarks, list) else [],
            disk_names if isinstance(disk_names, dict) else {},
            set(removed or ()),
            set(cleared_names or ()),
        )
        data["bookmarks"] = merged
        data["bookmark_names"] = merged_names
        return merged, merged_names

    result, ok = _persist_partial("bookmarks", _apply)
    if result is None:
        return list(bookmarks), dict(names), ok
    # Return the merged result so the caller can adopt disk additions into its
    # own in-memory state (keeping the two in sync between full saves).
    merged, merged_names = result
    return merged, merged_names, ok


def merge_bookmarks(
    mine: list[str],
    mine_names: dict[str, str],
    disk: list[str],
    disk_names: dict[str, str],
    removed: set[str],
    cleared_names: set[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Union this instance's bookmarks with ones another instance saved.

    Used by ``closeEvent`` before the final full ``save_state`` so a stale
    instance closing last cannot clobber bookmarks a newer instance added
    (*disk* entries survive unless THIS instance explicitly *removed* them).
    Order: this instance's list first, then disk-only additions appended.

    Display names follow the **same** rule as the paths: disk is the base,
    this instance's *mine_names* overwrite it, and a deletion is expressed by
    an **explicit set** (*cleared_names*) rather than by absence.  Reading
    "the key is missing from *mine_names*" as a deletion would make every
    path this instance merely holds authoritative, so a display name another
    instance set while this window was open would be dropped on the next
    write (the very clobbering this function exists to prevent — it just
    moved from the path list to the names).  *cleared_names* is the caller's
    record of the names the user actually blanked
    (``ViewerWindow._session_cleared_bookmark_names``).
    """
    merged = list(mine)
    mine_set = set(mine)
    for b in disk:
        if b not in mine_set and b not in removed:
            merged.append(b)
    merged_set = set(merged)
    merged_names = {
        k: v for k, v in disk_names.items() if k in merged_set
    }
    merged_names.update(
        {k: v for k, v in mine_names.items() if k in merged_set}
    )
    for key in cleared_names or ():
        merged_names.pop(key, None)
    return merged, merged_names


def _saved_search_name(entry: object) -> str:
    """Normalised ``name`` key of a saved-search entry ('' when unusable)."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("name") or "").strip()


def merge_saved_searches(
    mine: list[dict],
    disk: list[dict],
    removed_names: set[str],
) -> list[dict]:
    """Union this instance's saved searches with ones another instance saved.

    :func:`merge_bookmarks` の保存済み検索版: ``name`` をキーに
    union し、同名はこのインスタンス（*mine*）側が勝つ。順序は *mine* の
    並びが先、disk 側にしか無い追加分をディスク上の順序のまま末尾へ。
    *removed_names* はこのインスタンスが明示削除・改名した旧名の集合 —
    それらの disk エントリは採り込まない（削除の取り消しにならないように）。
    ``name`` が取れない disk エントリはキー付けできないので無視する。
    """
    merged = [dict(e) for e in mine]
    seen = {_saved_search_name(e) for e in merged}
    for entry in disk:
        name = _saved_search_name(entry)
        if not name or name in seen or name in removed_names:
            continue
        merged.append(dict(entry))
        seen.add(name)
    return merged


def persist_saved_searches(
    searches: list[dict],
    removed_names: set[str] | None = None,
) -> tuple[list[dict], bool]:
    """Write ONLY ``saved_searches`` into ``viewer_state.json`` right now.

    :func:`persist_bookmarks` と同型: 生 JSON 往復で対象キーだけ
    を書き換え、他のフィールドには一切触らない。ディスク側の値とは
    :func:`merge_saved_searches` でマージするので、2 インスタンス並行時に
    後から書いた側が相手の保存済み検索を消さない。読めない / 壊れている
    ファイルの扱いは :func:`_read_partial_write_base` を参照。

    マージ結果を返すので、呼び出し元はディスク側の追加分を自分の
    in-memory 状態へ採り込める（フル保存までの同期維持）。2 番目の返り値は
    **書き込みに失敗しなかったか**（:func:`persist_bookmarks` と同じ規約 —
    読み取り不能による先送りは失敗に数えない）。
    """
    def _apply(data: dict) -> list[dict]:
        disk = data.get("saved_searches")
        merged = merge_saved_searches(
            list(searches),
            disk if isinstance(disk, list) else [],
            set(removed_names or ()),
        )
        data["saved_searches"] = merged
        return merged

    result, ok = _persist_partial("saved searches", _apply)
    if result is None:
        return [dict(e) for e in searches], ok
    return result, ok


def push_recent_root(recent: list[str], path: str) -> list[str]:
    """Return a new MRU list with *path* moved to the front (newest first).

    Pure helper (Qt-free, unit-testable): removes any existing occurrence of
    *path* so it never appears twice, prepends it, and truncates to
    :data:`RECENT_ROOTS_MAX`.  An empty *path* is ignored (returns the list
    unchanged) so a blank root never pollutes the menu.
    """
    if not path:
        return list(recent)
    out = [p for p in recent if p != path]
    out.insert(0, path)
    return out[:RECENT_ROOTS_MAX]


def encode_geometry(qbytearray) -> str:
    """Encode a QByteArray (from QMainWindow.saveGeometry) as base64 string."""
    raw = bytes(qbytearray)
    return base64.b64encode(raw).decode("ascii")


def decode_geometry(b64: str) -> bytes:
    if not b64:
        return b""
    try:
        return base64.b64decode(b64.encode("ascii"))
    except ValueError:
        return b""
