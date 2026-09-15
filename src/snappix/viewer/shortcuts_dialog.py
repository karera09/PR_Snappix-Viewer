"""Modeless 「操作ガイド」 — 作業別に引ける操作の一覧 + 画面の凡例（F1）。

A static, searchable reference of every way to do things in the viewer: the
mouse entry point (どこを押すか), the key (慣れた人の近道), and the seat the
verb applies to (どの面で効くか).  The data lives in a single module level
table (:data:`SHORTCUTS`) so it's easy to audit/update as bindings change
elsewhere in the viewer — this dialog never inspects live ``QAction``/
``QShortcut`` objects, it's a plain reference table.

UIレビュー 2026-09-11 のリデザイン E1: 旧「ショートカットと画面の凡例」は入力
装置（ナビゲーション / 画像の操作 …）でしか分類されておらず、「探す・見る・
印を付ける・整える」という**作業の軸**が製品のどこにも無かった（発見性テスト
の未到達課題はすべてここに帰着した）。行に ``task`` / ``entry`` / ``seats`` を
足し、左の作業ツリーから引ける形にする。**このガイドは補助**で、主要な操作は
マウスで画面から届くことを ``entry`` 列が保証する（キーしか入口の無い行は
``ENTRY_KEY_ONLY`` と明示し、主要機能には使わない）。ようこそカードの
[操作の基本 (F1)] と README のクイックスタート（:func:`quickstart_markdown`）
も同じ表から出る。

Modeless like :class:`~snappix.viewer.detail_window.DetailWindow` and
:class:`~snappix.viewer.perf_dialog.PerfDialog` — the caller just constructs it
with a parent and calls ``show()``/``raise_()``; there's no state to feed in.
"""

from __future__ import annotations

from typing import NamedTuple

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..common.i18n import t
from ..common.ui import (
    align_header,
    demote_close_default,
    empty_state_stack,
    localize_buttons,
)
from ._indicator import badge_icon, badge_name
from .qimage_decode import enable_clear_button


class ShortcutRow(NamedTuple):
    """1 行 = 1 動詞。先頭 3 要素は旧 ``(category, key, description)`` と互換。

    ``category`` / ``desc`` / ``entry`` / ``seats`` は i18n *catalog keys*
    （表示時に ``t(...)``）。``key`` だけは**リテラル**のキー / ジェスチャ文字列
    — ``tests/test_viewer_shortcuts.py`` が live ``QKeySequence`` と突き合わせる
    ので catalog key にしてはいけない。``task`` は :data:`TASKS` の id。
    """

    category: str
    key: str
    desc: str
    task: str
    entry: str
    seats: str


KEY_COLUMN = 1  # index of the "key" element within each SHORTCUTS row

# ---------------------------------------------------------------- 作業（task）
TASK_START = "start"        # はじめに
TASK_FIND = "find"          # 探す
TASK_VIEW = "view"          # 見る
TASK_MARK = "mark"          # 印を付ける
TASK_ARRANGE = "arrange"    # 整える
TASK_FULLSCREEN = "fullscreen"  # 全画面
TASK_LEGEND = "legend"      # 凡例
#: 作業の並び（左のツリーの順） → 見出しの i18n キー。
TASKS: tuple[tuple[str, str], ...] = (
    (TASK_START, "viewer.shortcuts_dialog.task_start"),
    (TASK_FIND, "viewer.shortcuts_dialog.task_find"),
    (TASK_VIEW, "viewer.shortcuts_dialog.task_view"),
    (TASK_MARK, "viewer.shortcuts_dialog.task_mark"),
    (TASK_ARRANGE, "viewer.shortcuts_dialog.task_arrange"),
    (TASK_FULLSCREEN, "viewer.common.lightbox_mode"),
    (TASK_LEGEND, "viewer.shortcuts_dialog.task_legend"),
)
TASK_ALL = "all"
#: 「はじめに」ページ = 初回セッションで要る動詞だけを他の作業から集めた
#: 合成ページ（表の行はそれぞれ本来の作業に属したまま）。README の
#: クイックスタート（:func:`quickstart_markdown`）も同じ並び。
QUICKSTART_DESCS: tuple[str, ...] = (
    "viewer.main_window.open_folder",
    "viewer.shortcuts_dialog.desc_drill_down",
    "viewer.shortcuts_dialog.desc_stage_mode",
    "viewer.shortcuts_dialog.desc_browse_mode",
    "viewer.shortcuts_dialog.desc_toggle_lightbox",
    "viewer.shortcuts_dialog.desc_focus_filter",
    "viewer.shortcuts_dialog.desc_clear_filters",
    "viewer.shortcuts_dialog.desc_set_star",
    "viewer.shortcuts_dialog.desc_toggle_later",
    "viewer.shortcuts_dialog.desc_show_help",
)
#: 「すべて」の見出し（既存の共通語を再利用 — 同値の複製をカタログに作らない）。
TASK_ALL_TITLE_KEY = "common.filter.all"

# ------------------------------------------------------------ 入口（entry）
# 「どこを押せば同じことができるか」— マウスで届く入口。キーしか無い行だけ
# ``ENTRY_KEY_ONLY``（主要機能に使わない、という規約をテストが固定する）。
ENTRY_TOOLBAR = "viewer.shortcuts_dialog.entry_toolbar"
ENTRY_MENU_FILE = "viewer.shortcuts_dialog.entry_menu_file"
ENTRY_MENU_EDIT = "viewer.shortcuts_dialog.entry_menu_edit"
ENTRY_MENU_SEARCH = "viewer.shortcuts_dialog.entry_menu_search"
ENTRY_MENU_VIEW = "viewer.shortcuts_dialog.entry_menu_view"
ENTRY_MENU_DIAG = "viewer.shortcuts_dialog.entry_menu_diag"
ENTRY_MENU_HELP = "viewer.shortcuts_dialog.entry_menu_help"
ENTRY_CONTEXT_MENU = "viewer.shortcuts_dialog.entry_context_menu"
ENTRY_CURATION_STRIP = "viewer.shortcuts_dialog.entry_curation_strip"
ENTRY_STAGE_HEADER = "viewer.shortcuts_dialog.entry_stage_header"
ENTRY_CAPSULE = "viewer.shortcuts_dialog.entry_capsule"
ENTRY_LIGHTBOX_BAR = "viewer.shortcuts_dialog.entry_lightbox_bar"
ENTRY_FILTER_BOX = "viewer.shortcuts_dialog.entry_filter_box"
ENTRY_CONDITION_BAR = "viewer.shortcuts_dialog.entry_condition_bar"
ENTRY_CLICK = "viewer.shortcuts_dialog.entry_click"
ENTRY_SETTINGS = "viewer.shortcuts_dialog.entry_settings"
ENTRY_PANE_TOGGLES = "viewer.shortcuts_dialog.entry_pane_toggles"
ENTRY_MEDIA_CONTROLS = "viewer.shortcuts_dialog.entry_media_controls"
ENTRY_GESTURE = "viewer.shortcuts_dialog.entry_gesture"
ENTRY_KEY_ONLY = "viewer.shortcuts_dialog.entry_key_only"
ENTRY_LEGEND = "viewer.shortcuts_dialog.entry_legend"

# ------------------------------------------------------------- 席（seats）
SEAT_ANY = "viewer.shortcuts_dialog.seat_any"
SEAT_GRID = "common.view.grid"
SEAT_GRID_LIST = "viewer.shortcuts_dialog.seat_grid_list"
SEAT_PREVIEW = "viewer.shortcuts_dialog.seat_preview"
SEAT_STAGE = "viewer.shortcuts_dialog.seat_stage"
SEAT_IMAGE = "viewer.shortcuts_dialog.seat_image"
SEAT_LIGHTBOX = "viewer.common.lightbox_mode"
SEAT_FILTER = ENTRY_FILTER_BOX
SEAT_PDF = "viewer.shortcuts_dialog.seat_pdf"
SEAT_MEDIA = "viewer.shortcuts_dialog.seat_media"

_NAV = "common.category.navigation"
_MODE = "viewer.shortcuts_dialog.cat_mode"
_WIN = "viewer.shortcuts_dialog.cat_window_display"
_IMG = "viewer.shortcuts_dialog.cat_image_ops"
_LB = "viewer.common.lightbox_mode"
_PDF = "viewer.shortcuts_dialog.cat_pdf"
_MEDIA = "viewer.shortcuts_dialog.cat_media"
_SEARCH = "common.label.search"
_OTHER = "viewer.shortcuts_dialog.cat_other"
_BADGES = "viewer.shortcuts_dialog.cat_badges"

# Verified against the current bindings in main_window.py / image_view.py /
# media_view.py / content_view.py (post_grid double-click routing) as of this
# writing.  This dialog never inspects live QAction/QShortcut objects — it's a
# plain reference — so keep this table in sync when bindings change elsewhere.
#
# The sync is guarded mechanically: ``tests/test_viewer_shortcuts.py`` collects
# every live ``QKeySequence`` from a real ViewerWindow / ImageView / MediaView
# and fails if any binding is missing from (or stale in) this table.  Non-key
# entries below (mouse side-buttons, long-press, breadcrumb click,
# double-click, the PageUp/PageDown PDF paging + the Enter/Backspace/Esc grid
# keys handled via ``keyPressEvent`` rather than a ``QShortcut``, and the
# "バッジの意味" legend rows) are documentation-only and whitelisted there.
# ``tests/test_viewer_shortcuts_guide.py`` additionally pins task / entry /
# seats per row (every row belongs to a task; primary verbs have a mouse
# entry).
SHORTCUTS: list[ShortcutRow] = [
    # -- ナビゲーション -------------------------------------------------------
    ShortcutRow(_NAV, "Ctrl+O", "viewer.main_window.open_folder", TASK_VIEW, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "Alt+Up", "viewer.main_window.go_up", TASK_VIEW, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "Alt+←", "viewer.shortcuts_dialog.desc_back", TASK_VIEW, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "Alt+→", "viewer.shortcuts_dialog.desc_forward", TASK_VIEW, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "マウス戻る/進むボタン", "viewer.shortcuts_dialog.desc_back_forward_side", TASK_VIEW, ENTRY_GESTURE, SEAT_ANY),
    ShortcutRow(_NAV, "←/→ ボタン長押し", "viewer.shortcuts_dialog.desc_history_list", TASK_VIEW, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "パンくずクリック", "viewer.shortcuts_dialog.desc_jump_ancestor", TASK_VIEW, ENTRY_CLICK, SEAT_ANY),
    ShortcutRow(_NAV, "F5", "viewer.shortcuts_dialog.desc_reload", TASK_ARRANGE, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_NAV, "←", "viewer.shortcuts_dialog.desc_select_prev", TASK_VIEW, ENTRY_CLICK, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "→", "viewer.shortcuts_dialog.desc_select_next", TASK_VIEW, ENTRY_CLICK, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "↑/↓", "viewer.shortcuts_dialog.desc_select_up_down", TASK_VIEW, ENTRY_CLICK, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "Home/End", "viewer.shortcuts_dialog.desc_select_first_last", TASK_VIEW, ENTRY_KEY_ONLY, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "PageUp/PageDown", "viewer.shortcuts_dialog.desc_select_page", TASK_VIEW, ENTRY_KEY_ONLY, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "Enter", "viewer.shortcuts_dialog.desc_open_tile", TASK_VIEW, ENTRY_CLICK, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "Backspace", "viewer.shortcuts_dialog.desc_go_up", TASK_VIEW, ENTRY_TOOLBAR, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "Esc", "viewer.shortcuts_dialog.desc_clear_filters", TASK_FIND, ENTRY_CONDITION_BAR, SEAT_ANY),
    ShortcutRow(_NAV, "1-5 / 0", "viewer.shortcuts_dialog.desc_set_star", TASK_MARK, ENTRY_CURATION_STRIP, SEAT_GRID_LIST),
    ShortcutRow(_NAV, "L", "viewer.shortcuts_dialog.desc_toggle_later", TASK_MARK, ENTRY_CURATION_STRIP, SEAT_ANY),
    ShortcutRow(_NAV, "ダブルクリック", "viewer.shortcuts_dialog.desc_drill_down", TASK_VIEW, ENTRY_CLICK, SEAT_GRID),
    ShortcutRow(_NAV, "フォルダをドロップ", "viewer.shortcuts_dialog.desc_drop_folder", TASK_VIEW, ENTRY_GESTURE, SEAT_ANY),
    # グリッド / 右一覧の Ctrl+ホイール（``GalleryView.wheelEvent`` →
    # ホストの ``zoom_handler``）は操作一覧にもツールチップにも無かった
    # （UIレビュー 2026-09-11 N-66）。画像の Ctrl+ホイール（``_IMG``）とキー
    # 文字列は重なるが、席（一覧 / 画像）で弁別するのが E1 の表の設計。
    ShortcutRow(_NAV, "Ctrl+ホイール", "viewer.shortcuts_dialog.desc_thumb_zoom", TASK_ARRANGE, ENTRY_GESTURE, SEAT_GRID_LIST),
    # -- プレビュー（分割 ⇄ 最大化） ------------------------------------------
    ShortcutRow(_MODE, "E", "viewer.shortcuts_dialog.desc_stage_mode", TASK_VIEW, ENTRY_STAGE_HEADER, SEAT_PREVIEW),
    ShortcutRow(_MODE, "G", "viewer.shortcuts_dialog.desc_browse_mode", TASK_VIEW, ENTRY_STAGE_HEADER, SEAT_STAGE),
    ShortcutRow(_MODE, "Esc", "viewer.shortcuts_dialog.desc_stage_exit", TASK_VIEW, ENTRY_STAGE_HEADER, SEAT_STAGE),
    ShortcutRow(_MODE, "画像をダブルクリック", "viewer.shortcuts_dialog.desc_dblclick_maximize", TASK_VIEW, ENTRY_CLICK, SEAT_PREVIEW),
    ShortcutRow(_MODE, "プレビューをダブルクリック", "viewer.shortcuts_dialog.desc_preview_dblclick", TASK_VIEW, ENTRY_CLICK, SEAT_STAGE),
    ShortcutRow(_MODE, "Ctrl+←", "viewer.shortcuts_dialog.desc_stage_prev_post", TASK_VIEW, ENTRY_STAGE_HEADER, SEAT_STAGE),
    ShortcutRow(_MODE, "Ctrl+→", "viewer.shortcuts_dialog.desc_stage_next_post", TASK_VIEW, ENTRY_STAGE_HEADER, SEAT_STAGE),
    ShortcutRow(_MODE, "←/→", "viewer.shortcuts_dialog.desc_stage_step_image", TASK_VIEW, ENTRY_CAPSULE, SEAT_STAGE),
    ShortcutRow(_MODE, "Space", "viewer.shortcuts_dialog.desc_stage_next_image", TASK_VIEW, ENTRY_CAPSULE, SEAT_STAGE),
    ShortcutRow(_MODE, "Home/End", "viewer.shortcuts_dialog.desc_stage_first_last_image", TASK_VIEW, ENTRY_KEY_ONLY, SEAT_STAGE),
    # -- ウィンドウ表示 -------------------------------------------------------
    ShortcutRow(_WIN, "Ctrl+I", "viewer.shortcuts_dialog.desc_open_detail", TASK_ARRANGE, ENTRY_MENU_VIEW, SEAT_ANY),
    ShortcutRow(_WIN, "Ctrl+Shift+N", "viewer.main_window.recent_files", TASK_FIND, ENTRY_MENU_VIEW, SEAT_ANY),
    ShortcutRow(_WIN, "F6", "viewer.shortcuts_dialog.desc_toggle_preview", TASK_ARRANGE, ENTRY_PANE_TOGGLES, SEAT_ANY),
    ShortcutRow(_WIN, "F7", "viewer.shortcuts_dialog.desc_toggle_nav_rail", TASK_ARRANGE, ENTRY_PANE_TOGGLES, SEAT_ANY),
    ShortcutRow(_WIN, "F8", "viewer.shortcuts_dialog.desc_toggle_info_panel", TASK_ARRANGE, ENTRY_PANE_TOGGLES, SEAT_ANY),
    ShortcutRow(_WIN, "Alt+1", "viewer.shortcuts_dialog.desc_focus_nav_rail", TASK_ARRANGE, ENTRY_CLICK, SEAT_ANY),
    ShortcutRow(_WIN, "Alt+2", "viewer.shortcuts_dialog.desc_focus_grid", TASK_ARRANGE, ENTRY_CLICK, SEAT_ANY),
    ShortcutRow(_WIN, "Alt+3", "viewer.shortcuts_dialog.desc_focus_preview", TASK_ARRANGE, ENTRY_CLICK, SEAT_ANY),
    ShortcutRow(_WIN, "Alt+4", "viewer.shortcuts_dialog.desc_focus_info_panel", TASK_ARRANGE, ENTRY_CLICK, SEAT_ANY),
    # -- 画像の操作 -----------------------------------------------------------
    ShortcutRow(_IMG, "Ctrl+0", "viewer.shortcuts_dialog.desc_actual_size", TASK_VIEW, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "Ctrl+1", "viewer.shortcuts_dialog.desc_fit_window", TASK_VIEW, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "+", "viewer.shortcuts_dialog.desc_zoom_in", TASK_VIEW, ENTRY_GESTURE, SEAT_IMAGE),
    ShortcutRow(_IMG, "-", "viewer.shortcuts_dialog.desc_zoom_out", TASK_VIEW, ENTRY_GESTURE, SEAT_IMAGE),
    ShortcutRow(_IMG, "ダブルクリック", "viewer.shortcuts_dialog.desc_toggle_zoom", TASK_VIEW, ENTRY_CLICK, SEAT_IMAGE),
    ShortcutRow(_IMG, "中クリック", "viewer.shortcuts_dialog.desc_middle_click_fit", TASK_VIEW, ENTRY_CAPSULE, SEAT_IMAGE),
    ShortcutRow(_IMG, "R", "viewer.shortcuts_dialog.desc_rotate_right", TASK_VIEW, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "Shift+R", "viewer.shortcuts_dialog.desc_rotate_left", TASK_VIEW, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "F", "viewer.shortcuts_dialog.desc_flip_horizontal", TASK_VIEW, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "Ctrl+C", "viewer.shortcuts_dialog.desc_copy_image", TASK_ARRANGE, ENTRY_CONTEXT_MENU, SEAT_IMAGE),
    ShortcutRow(_IMG, "Ctrl+ホイール", "viewer.shortcuts_dialog.desc_zoom_image", TASK_VIEW, ENTRY_GESTURE, SEAT_IMAGE),
    ShortcutRow(_IMG, "1-5 / 0", "viewer.shortcuts_dialog.desc_stage_set_star", TASK_MARK, ENTRY_CURATION_STRIP, SEAT_PREVIEW),
    # -- 全画面 ---------------------------------------------------------------
    ShortcutRow(_LB, "F11", "viewer.shortcuts_dialog.desc_toggle_lightbox", TASK_FULLSCREEN, ENTRY_STAGE_HEADER, SEAT_PREVIEW),
    ShortcutRow(_LB, "Esc", "viewer.shortcuts_dialog.desc_exit_lightbox", TASK_FULLSCREEN, ENTRY_LIGHTBOX_BAR, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "←/→", "viewer.shortcuts_dialog.desc_prev_next_image_wrap", TASK_FULLSCREEN, ENTRY_CAPSULE, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "ホイール", "viewer.shortcuts_dialog.desc_prev_next_image", TASK_FULLSCREEN, ENTRY_GESTURE, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "Home/End", "viewer.shortcuts_dialog.desc_first_last_image", TASK_FULLSCREEN, ENTRY_KEY_ONLY, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "Space", "viewer.shortcuts_dialog.desc_next_image", TASK_FULLSCREEN, ENTRY_CAPSULE, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "S", "viewer.shortcuts_dialog.desc_slideshow", TASK_FULLSCREEN, ENTRY_LIGHTBOX_BAR, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "[ / ]", "viewer.shortcuts_dialog.desc_slideshow_interval", TASK_FULLSCREEN, ENTRY_SETTINGS, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "画像上でダブルクリック", "viewer.shortcuts_dialog.desc_lightbox_toggle_zoom", TASK_FULLSCREEN, ENTRY_CLICK, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "1-5 / 0", "viewer.shortcuts_dialog.desc_lightbox_set_star", TASK_MARK, ENTRY_CURATION_STRIP, SEAT_LIGHTBOX),
    ShortcutRow(_LB, "L", "viewer.shortcuts_dialog.desc_lightbox_toggle_later", TASK_MARK, ENTRY_CURATION_STRIP, SEAT_LIGHTBOX),
    # -- PDF / メディア ---------------------------------------------------------
    ShortcutRow(_PDF, "PageUp", "common.action.prev_page", TASK_VIEW, ENTRY_GESTURE, SEAT_PDF),
    ShortcutRow(_PDF, "PageDown", "common.action.next_page", TASK_VIEW, ENTRY_GESTURE, SEAT_PDF),
    ShortcutRow(_MEDIA, "Space", "viewer.shortcuts_dialog.desc_play_pause", TASK_VIEW, ENTRY_MEDIA_CONTROLS, SEAT_MEDIA),
    ShortcutRow(_MEDIA, ",", "viewer.shortcuts_dialog.desc_frame_back", TASK_VIEW, ENTRY_KEY_ONLY, SEAT_MEDIA),
    ShortcutRow(_MEDIA, ".", "viewer.shortcuts_dialog.desc_frame_forward", TASK_VIEW, ENTRY_KEY_ONLY, SEAT_MEDIA),
    # -- 検索 -----------------------------------------------------------------
    ShortcutRow(_SEARCH, "Ctrl+F", "viewer.shortcuts_dialog.desc_focus_filter", TASK_FIND, ENTRY_FILTER_BOX, SEAT_ANY),
    ShortcutRow(_SEARCH, "Ctrl+Shift+T", "viewer.shortcuts_dialog.desc_ai_tag_search", TASK_FIND, ENTRY_TOOLBAR, SEAT_ANY),
    ShortcutRow(_SEARCH, "Enter / ↓", "viewer.shortcuts_dialog.desc_jump_to_results", TASK_FIND, ENTRY_CLICK, SEAT_FILTER),
    ShortcutRow(_SEARCH, "Ctrl+/", "viewer.post_grid.filter_help_tooltip", TASK_FIND, ENTRY_FILTER_BOX, SEAT_FILTER),
    # -- その他 ---------------------------------------------------------------
    ShortcutRow(_OTHER, "Ctrl+,", "common.action.settings", TASK_ARRANGE, ENTRY_MENU_FILE, SEAT_ANY),
    ShortcutRow(_OTHER, "Ctrl+Q", "common.action.quit", TASK_ARRANGE, ENTRY_MENU_FILE, SEAT_ANY),
    ShortcutRow(_OTHER, "Ctrl+Shift+P", "viewer.shortcuts_dialog.desc_perf_stats", TASK_ARRANGE, ENTRY_MENU_DIAG, SEAT_ANY),
    ShortcutRow(_OTHER, "F1", "viewer.shortcuts_dialog.desc_show_help", TASK_ARRANGE, ENTRY_MENU_HELP, SEAT_ANY),
    ShortcutRow(_OTHER, "タイルをドラッグ", "viewer.shortcuts_dialog.desc_drag_export", TASK_ARRANGE, ENTRY_GESTURE, SEAT_GRID_LIST),
    # -- 凡例（キー操作ではない） ---------------------------------------------
    ShortcutRow(_BADGES, "badge:favorites", "viewer.shortcuts_dialog.desc_badge_likes", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:locked", "viewer.shortcuts_dialog.desc_badge_locked", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:relevance", "viewer.shortcuts_dialog.desc_badge_relevance", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:star", "viewer.shortcuts_dialog.desc_badge_star", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:later", "viewer.shortcuts_dialog.desc_badge_later", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:thumb_fail", "viewer.shortcuts_dialog.desc_badge_thumb_fail", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:similar", "viewer.shortcuts_dialog.desc_badge_similar", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
    ShortcutRow(_BADGES, "badge:ghost", "viewer.shortcuts_dialog.desc_badge_ghost", TASK_LEGEND, ENTRY_LEGEND, SEAT_GRID_LIST),
]

#: KEY_COLUMN values with this prefix are **legend** rows — a badge kind
#: rather than a keystroke.  The suffix is a ``_indicator`` badge *kind*.
BADGE_KEY_PREFIX = "badge:"


def badge_kind_for_key(key: str) -> str | None:
    """The badge kind a KEY_COLUMN value names, or ``None`` for a real key."""
    if key.startswith(BADGE_KEY_PREFIX):
        return key[len(BADGE_KEY_PREFIX):]
    return None


# AI 機能パック（有償プラグイン）が有効なときだけ意味を持つ行の description
# キー。素の配布ではダイアログの表示から除外する（バインド自体もされない —
# main_window 側で Ctrl+Shift+T の QAction 作成がゲートされている）。
_AI_ONLY_DESC_KEYS = frozenset(
    {
        "viewer.shortcuts_dialog.desc_ai_tag_search",
        "viewer.shortcuts_dialog.desc_badge_relevance",
        # 類似検索のホバーボタンはベクトル索引（AI パック）がある左ペインでしか
        # 現れない（`PostGrid._on_view_similar_requested` / `set_similar_overlay_enabled`）。
        "viewer.shortcuts_dialog.desc_badge_similar",
    }
)


def visible_shortcuts() -> list[ShortcutRow]:
    """現在の AI パック可用性でフィルタした表示用ショートカット表。"""
    from . import ai_pack

    if ai_pack.available():
        return SHORTCUTS
    return [row for row in SHORTCUTS if row.desc not in _AI_ONLY_DESC_KEYS]


def rows_for_task(task: str, rows: list[ShortcutRow] | None = None) -> list[ShortcutRow]:
    """*task*（:data:`TASKS` の id、または :data:`TASK_ALL`）に属する行。"""
    source = SHORTCUTS if rows is None else rows
    if task == TASK_ALL:
        return list(source)
    if task == TASK_START:
        by_desc = {row.desc: row for row in source}
        return [by_desc[d] for d in QUICKSTART_DESCS if d in by_desc]
    return [row for row in source if row.task == task]


def key_hint(desc_key: str) -> str:
    """*desc_key* の動詞に割り当てられたキー文字列（無ければ ``""``）。

    表示面（ツールチップ・ラベル）がキーを併記するときはここから引く —
    手書きで併記すると表とずれる（UIレビュー 2026-09-11 D1: キーの予告面が
    F1 の表 1 枚に集中し、表示面との同期が無かった）。
    """
    for row in SHORTCUTS:
        if row.desc == desc_key and badge_kind_for_key(row.key) is None:
            return row.key
    return ""


def with_key_hint(text: str, desc_key: str) -> str:
    """*text*（ツールチップ）に *desc_key* のキーを併記する（表に無ければそのまま）。

    体裁はここ 1 か所 — 分割 / 最大化のカプセル（``image_view``）と全画面の
    カプセル（``lightbox``）が同じ形で併記する。
    """
    key = key_hint(desc_key)
    return f"{text} ({key})" if key else text


def quickstart_markdown() -> str:
    """README のクイックスタート「基本操作」節 — 「はじめに」の行を同じ表から出す。

    ``tools/gen_readme_quickstart.py`` が README.md へ書き込み、
    ``tests/test_viewer_shortcuts_guide.py`` が README と一致することを固定する
    （表を変えたら README も再生成する）。
    """
    lines = []
    for row in rows_for_task(TASK_START):
        text = f"- {t(row.desc)} — {t(row.entry)}"
        # キーの併記はキーボードの行だけ（ジェスチャ / クリックの行は入口が
        # そのまま操作なので重ねない）。
        if row.entry not in (ENTRY_GESTURE, ENTRY_CLICK, ENTRY_LEGEND):
            text += f"（{row.key}）"
        lines.append(text)
    return "\n".join(lines) + "\n"


#: 既定サイズの下限（UIレビュー 07-25 #36 の値）と高さ。実際の幅は
#: :meth:`ShortcutsDialog._fit_default_width` が内容から決める（N-92）。
_MIN_DEFAULT_WIDTH = 820
_DEFAULT_HEIGHT = 620
#: セル padding / ヘッダの余白ぶんの上乗せ（``sizeHintForColumn`` は文字の
#: 実寸に近い値を返すため、そのままだと最後の 1〜2 文字が詰まって見える）。
_TREE_CELL_SLACK = 24
#: 左の作業ツリーの幅（レイアウト寸法）。
_TASK_LIST_WIDTH = 132
#: 入口列（列2）の下限 px（``ShortcutsDialog._fit_entry_column``）。
_ENTRY_COLUMN_MIN_WIDTH = 90


class ShortcutsDialog(QDialog):
    """Modeless 操作ガイド — 左に作業ツリー、右に [操作 | キー | 入口 | 効く席]。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.shortcuts_dialog.window_title"))
        # UIレビュー 07-25 #36: the old 600x560 default left 7 of 25 visible
        # rows eliding their 操作 text even with the column resize fix below.
        self.resize(_MIN_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        self.setModal(False)
        self._task = TASK_START
        self._build_ui()
        # 既定幅は**全行**を入れて測る — 開いた直後のページ（はじめに 10 行）
        # だけで測ると、「見る」「すべて」へ切り替えた途端に最長の説明が
        # 省略される（N-92 の再発経路）。測ってから現在ページへ戻す。
        self._populate(visible_shortcuts())
        self._fit_default_width()
        self._refresh()

    def _fit_default_width(self) -> None:
        """既定サイズを**内容から**決める (UIレビュー 2026-08-28 N-92).

        07-25 #36 は同じ症状を「幅を 820px へ広げる」で塞いだが、原因は
        固定幅そのもの — 表に 1 行足す / 説明を 1 語伸ばすたびに、既定サイズで
        末尾が「…」で切れる行が復活する（実際 4 行が切れていた）。列の
        リサイズ規約（列0 Stretch + 他列 ResizeToContents + stretchLastSection
        False）は既に正しいので、残るのは**ウィンドウ幅の決め方**だけ。

        Qt 自身の内容幅（``sizeHintForColumn``）から必要幅を出し、下限
        （従来の既定）と画面幅の 9 割で挟む。以後どんな文言でも既定サイズで
        省略されない — 文言を短く保つ努力（同 N-92 の文面整理）と独立した
        恒久策として効く。

        前提は「余白を受ける Stretch 列が操作列（列0）**だけ**」であること。
        余白を複数列で分け合う（均等配分）と、総幅が足りていても列0 が
        内容幅に届かない。入口列（列2）は :meth:`_fit_entry_column` が
        内容幅を上限に**手動**で幅を決める。
        """
        needed = self._needed_width()
        screen = self.screen()
        if screen is not None:
            needed = min(needed, int(screen.availableGeometry().width() * 0.9))
        self.resize(max(_MIN_DEFAULT_WIDTH, needed), _DEFAULT_HEIGHT)

    def _needed_width(self) -> int:
        """表の全列が省略なしで収まる窓幅（画面クランプ前）。"""
        tree = self._tree
        # 列0 はツリーなので、行のインデント分だけ内容より広い枠が要る。
        needed = (
            tree.sizeHintForColumn(0)
            + tree.indentation()
            + sum(tree.sizeHintForColumn(c) for c in range(1, tree.columnCount()))
            + 2 * tree.frameWidth()
            + tree.verticalScrollBar().sizeHint().width()
            + _TREE_CELL_SLACK
            + _TASK_LIST_WIDTH
        )
        margins = self.layout().contentsMargins()
        return needed + margins.left() + margins.right()

    def _fit_entry_column(self) -> None:
        """入口列（列2）の幅 = 内容幅を上限に、表示域の一定割合まで。

        入口列を ResizeToContents にすると、窓が内容幅より狭いとき（既定幅は
        画面の 9 割で頭打ち・ユーザーの縮小）に入口が幅を先取りして操作列が
        0 幅まで潰れる。逆に Stretch にすると操作列と余白を均等に分け合い、
        総幅が足りていても操作列が内容幅に届かない。どちらでもなく、入口列は
        「内容幅まで、ただし固定幅の 2 列（キー・効く席）を除いた残りの半分を
        超えない」で手動に決め、余りは操作列（唯一の Stretch）が受ける —
        文の 2 列のうち操作列が入口列より狭くなることはない。
        """
        tree = self._tree
        fixed = tree.columnWidth(1) + tree.columnWidth(3)
        # 表示域はスクロールバーの有無で 1 段遅れて変わるので、常にバー込みの
        # 幅（枠とバーを引いたウィジェット幅）で決める — 列0 が受ける余りが
        # バーの出現で目減りして入口列に負けることがない。
        avail = (
            tree.width()
            - 2 * tree.frameWidth()
            - tree.verticalScrollBar().sizeHint().width()
        )
        cap = max(_ENTRY_COLUMN_MIN_WIDTH, (avail - fixed) // 2)
        tree.setColumnWidth(2, min(tree.sizeHintForColumn(2) + _TREE_CELL_SLACK // 2, cap))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._fit_entry_column()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(t("viewer.shortcuts_dialog.filter_placeholder"))
        enable_clear_button(self._filter_edit)
        self._filter_edit.textChanged.connect(self._apply_filter)
        outer.addWidget(self._filter_edit)

        body = QHBoxLayout()
        body.setSpacing(8)
        # 左: 作業ツリー（はじめに / 探す / 見る / 印を付ける / 整える / 全画面 /
        # 凡例 / すべて）。入力装置ではなく「何をしたいか」で引く（E1）。
        self._task_list = QListWidget()
        self._task_list.setFixedWidth(_TASK_LIST_WIDTH)
        for task_id, title_key in TASKS:
            item = QListWidgetItem(t(title_key))
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self._task_list.addItem(item)
        all_item = QListWidgetItem(t(TASK_ALL_TITLE_KEY))
        all_item.setData(Qt.ItemDataRole.UserRole, TASK_ALL)
        self._task_list.addItem(all_item)
        self._task_list.setCurrentRow(0)
        self._task_list.currentItemChanged.connect(self._on_task_changed)
        body.addWidget(self._task_list)

        right = QVBoxLayout()
        right.setSpacing(6)
        # 「はじめに」ページの要約: 3 つの面と 3 つの表示状態を 1 画面で。
        self._intro = QLabel()
        self._intro.setWordWrap(True)
        self._intro.setTextFormat(Qt.TextFormat.PlainText)
        self._intro.setText(
            "\n".join(
                (
                    t("viewer.shortcuts_dialog.intro_sheets"),
                    t("viewer.shortcuts_dialog.intro_modes"),
                    t("viewer.shortcuts_dialog.intro_note"),
                )
            )
        )
        right.addWidget(self._intro)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(
            [
                t("viewer.shortcuts_dialog.col_operation"),
                t("viewer.shortcuts_dialog.col_key"),
                t("viewer.shortcuts_dialog.col_entry"),
                t("viewer.shortcuts_dialog.col_seat"),
            ]
        )
        # 見出しの揃えは内容の揃えに合わせる (UIレビュー 07-25 #97) —
        # キー列（列1）の値は右揃えなので見出しも右。
        align_header(self._tree, right=(1,))
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        hdr = self._tree.header()
        # 余白を受ける Stretch は操作列（列0）だけ（``_fit_default_width`` の
        # 前提）。入口列（列2）は ``_fit_entry_column`` が手動で幅を決める。
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Interactive)
        for col in (1, 3):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # UIレビュー 07-25 #36: stretchLastSection defaults to True, which
        # silently overrides the ResizeToContents columns and lets Qt grab back
        # slack width for the last section — the net effect was column 0
        # (操作, the widest/most-truncated text) never actually got to stretch.
        hdr.setStretchLastSection(False)
        # (UIレビュー 08-28 N-38) 絞り込み結果が 0 件のとき、以前は空のツリー
        # だけが残り「一致が無い」のか「壊れた」のか読めなかった — 管理系
        # ダイアログ 4 枚が既に使っている共通の空状態カードへ合流させる。
        self._stack, self._empty_card = empty_state_stack(
            self._tree, icon_name="search"
        )
        right.addWidget(self._stack, 1)
        body.addLayout(right, 1)
        outer.addLayout(body, 1)

        buttons = demote_close_default(
            localize_buttons(QDialogButtonBox(QDialogButtonBox.Close))
        )
        # The Close button has RejectRole, so clicking it emits rejected();
        # a second clicked→close connection would double-invoke close().
        buttons.rejected.connect(self.close)
        outer.addWidget(buttons)

    # --------------------------------------------------------------- helpers

    def current_task(self) -> str:
        return self._task

    def select_task(self, task: str) -> None:
        """作業ツリーを *task* へ（絞り込み中なら解除して切り替える）。

        既に *task* が選ばれているときも契約は同じ。``setCurrentItem`` は
        現在項目と同じなら ``currentItemChanged`` を出さないので、絞り込み
        解除（``_on_task_changed`` がやっている）を自分でも行う — さもないと
        「同じ作業をもう一度指定したときだけ前回の絞り込みが残る」という、
        呼び出し元からは見えない例外ができる。
        """
        for i in range(self._task_list.count()):
            item = self._task_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) != task:
                continue
            if self._task_list.currentItem() is item:
                self._clear_filter()
                self._refresh()
            else:
                self._task_list.setCurrentItem(item)
            return

    def _clear_filter(self) -> None:
        """絞り込み欄を空にする（``textChanged`` は出さない — 再描画は呼び手）."""
        if not self._filter_edit.text():
            return
        self._filter_edit.blockSignals(True)
        self._filter_edit.clear()
        self._filter_edit.blockSignals(False)

    def _on_task_changed(self, current, _previous) -> None:
        if current is None:
            return
        self._task = current.data(Qt.ItemDataRole.UserRole)
        self._clear_filter()
        self._refresh()

    def _refresh(self) -> None:
        self._intro.setVisible(self._task == TASK_START)
        rows = rows_for_task(self._task, visible_shortcuts())
        # 「はじめに」は合成ページなので分類で束ねず、表の順に 1 群で出す。
        group = TASKS[0][1] if self._task == TASK_START else None
        self._populate(rows, group_key=group)

    def _populate(
        self, rows: list[ShortcutRow], group_key: str | None = None,
    ) -> None:
        """*rows* をツリーへ。*group_key* を渡すとその 1 群に束ねる（順序保持）。"""
        self._tree.clear()
        if not rows:
            self._empty_card.setText(t("viewer.shortcuts_dialog.filter_no_hits"))
            self._stack.setCurrentWidget(self._empty_card)
            return
        self._stack.setCurrentWidget(self._tree)
        groups: dict[str, QTreeWidgetItem] = {}
        for row in rows:
            category = group_key or row.category
            group = groups.get(category)
            if group is None:
                group = QTreeWidgetItem(self._tree, [t(category), "", "", ""])
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                # 見出しは行全体に渡す（列0 の幅に閉じ込めると省略される）。
                group.setFirstColumnSpanned(True)
                groups[category] = group
            desc_text = t(row.desc)
            kind = badge_kind_for_key(row.key)
            entry_text = t(row.entry)
            seat_text = t(row.seats)
            child = QTreeWidgetItem(
                group, [desc_text, "" if kind else row.key, entry_text, seat_text]
            )
            child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            # UIレビュー #1: the 操作 column is the widest/most-truncated one
            # (Stretch), and long key/gesture strings still deserve a tooltip
            # of their own — cover every column so an elided cell is always
            # fully readable on hover.
            child.setToolTip(0, desc_text)
            if kind:
                # 実物のバッジチップを貼る（文字リテラルの模写ではない）—
                # UIレビュー 08-28 提案1。名前はホバーで読める。
                child.setIcon(1, badge_icon(kind, on_surface=True))
                child.setToolTip(1, badge_name(kind))
            else:
                child.setToolTip(1, row.key)
            child.setToolTip(2, entry_text)
            child.setToolTip(3, seat_text)
        self._tree.expandAll()
        self._fit_entry_column()

    @staticmethod
    def _key_haystack(key: str) -> str:
        """Searchable text for the key column (badge rows search by name)."""
        kind = badge_kind_for_key(key)
        return (badge_name(kind) if kind else key).lower()

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        if not needle:
            self._refresh()
            return
        # 絞り込みは作業ツリーを跨いで全行から探す（「どこにあるか分からない」
        # ときの入口なので、選んでいる作業に閉じ込めない）。
        self._intro.setVisible(False)
        rows = [
            row
            for row in visible_shortcuts()
            if needle in t(row.category).lower()
            or needle in self._key_haystack(row.key)
            or needle in t(row.desc).lower()
            or needle in t(row.entry).lower()
            or needle in t(row.seats).lower()
        ]
        self._populate(rows)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        """テーマ切替でバッジ凡例のチップを描き直す (PR #87 の残件).

        凡例のチップは :func:`_indicator.badge_icon` が**その時点のトークン
        色**でラスタライズした ``QPixmap`` なので、モードレスなこの窓を開いた
        まま表示 ▸ テーマを切り替えると古い色のまま残る。とくに
        ``thumb_fail`` は ``current_tokens().warning`` を直接引く唯一のチップ
        で、ライト↔ダークの差が目に見える。行の再構築（``_populate``）は
        ツリーを作り直すだけの軽い処理なので、パレット変更のたびに現在の
        絞り込みを保ったまま貼り直す。
        """
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ThemeChange,
        ):
            # ``_build_ui`` 前に届きうる（QDialog の初期化中）ので存在を確認。
            if getattr(self, "_tree", None) is not None:
                self._apply_filter(self._filter_edit.text())


__all__ = [
    "BADGE_KEY_PREFIX",
    "ENTRY_KEY_ONLY",
    "KEY_COLUMN",
    "QUICKSTART_DESCS",
    "SHORTCUTS",
    "TASKS",
    "TASK_ALL",
    "ShortcutRow",
    "ShortcutsDialog",
    "badge_kind_for_key",
    "key_hint",
    "with_key_hint",
    "quickstart_markdown",
    "rows_for_task",
    "visible_shortcuts",
]
