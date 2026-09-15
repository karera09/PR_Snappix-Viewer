"""``PostGrid`` ↔ AI 検索コントローラの接着面（**表 1 枚**）.

2 方向を 1 ファイルで宣言する:

* :func:`_install_host_adapters` — ``PostGrid`` が
  :class:`~.host.SearchHost` を満たすための読み替え。ホスト側の既存の名前
  （``_root_or_folder`` / ``_rebuild_grid`` / ``tag_rating_combo`` …）を
  インタフェースの名前へ写すだけの薄い層で、**コントローラがホストの ``_``
  付き属性を触らない**という規約はここで担保される。
* :func:`_install_delegates` — 外から観測される旧名（``grid._tag_results`` /
  ``grid._maybe_start_tag_scan()`` / ``grid.tag_input`` …）を
  ``grid._ai`` へ委譲する薄いプロパティ / メソッド。テスト・``tools/ui_review``・
  ウィンドウが従来どおりの名前で叩けるようにするための**互換面**で、新しい
  コードはコントローラを直に呼ぶこと。

どちらも表からの生成にしてあるのは、境界が「1 行足した / 消した」として
レビューに見えるようにするため（手書きのプロパティが 100 個並ぶと、増えたこと
自体が読めなくなる）。
"""

from __future__ import annotations

from .query import AiQuery

#: ``PostGrid`` の旧属性名 → :class:`~.query.AiQuery` のフィールド。
#: 読み書きとも ``_ai.query`` / ``_ai.set_query`` を通る（クエリ状態を書く口は
#: コントローラの :meth:`set_query` 1 本、という不変条件を壊さない）。
QUERY_FIELDS: dict[str, str] = {
    "_tag_query_text": "text",
    "_tag_search_enabled": "enabled",
    "_tag_threshold": "threshold",
    "_tag_media_type": "media_type",
    "_tag_folder_mode": "folder_mode",
    "_tag_coverage_mode": "coverage_mode",
    "_tag_rating_key": "rating_key",
    "_tag_date_preset": "date_preset",
    "_ai_mode": "mode",
    "_similar_seed": "similar_seed",
}

#: コントローラの素の属性をそのままの名前で読み書きする委譲。
STATE_ATTRS: tuple[str, ...] = (
    "_tag_results",
    "_tag_results_query",
    "_tag_rel_paths",
    "_tag_results_ranked",
    "_tag_results_posted_seeded",
    "_landed_outcome",
    "_advanced_match_count",
    "_similar_seed_pixmap",
    "_rank_mode_cache",
    "_deferred_media_restore",
    "_deferred_threshold_restore",
    "_tag_browser",
    "_search_cheatsheet_popup",
    "_tag_completer",
    "_tag_completer_model",
    "_tag_count_delegate",
    "_tag_suggest",
    "_tag_scanner",
    "_vector_scanner",
    "_tag_debounce",
    "_advanced_folder_cache",
)

#: 読み取り専用で見せるコントローラ所有のウィジェット（席が移っただけで、
#: 名前は永続化・スナップショット・テストの参照面なので不変）。
WIDGETS: tuple[str, ...] = (
    "ai_popover",
    "ai_popover_title",
    "ai_mode_group",
    "_ai_mode_buttons",
    "tag_help_btn",
    "tag_missing_banner",
    "tag_missing_widget",
    "tag_reload_btn",
    "tag_media_note",
    "tag_no_vectors_hint",
    "tag_input",
    "tag_browse_btn",
    "tag_examples_hint",
    "tag_precision_label",
    "tag_threshold_slider",
    "tag_media_combo",
    "tag_display_unit_row",
    "tag_display_unit_combo",
    "tag_display_unit_note",
    "tag_shared_conditions",
    "tag_shared_syntax",
    "tag_similar_btn",
    "tag_similar_clear_btn",
    "tag_rank_note",
    "tag_relevance_legend",
    "tag_seed_row",
    "tag_seed_thumb",
    "tag_seed_name",
    "tag_seed_cta",
    "tag_seed_clear",
    "tag_reset_link",
)

#: そのままコントローラへ転送するメソッド。
METHODS: tuple[str, ...] = (
    "_advanced_empty_inputs",
    "_advanced_empty_message",
    "_advanced_error_actions",
    "_advanced_error_texts",
    "_advanced_only_engaged",
    "_advanced_phase",
    "_advanced_search_active",
    "_advanced_search_cancel",
    "_advanced_search_drop_results",
    "_advanced_status_text",
    "_apply_advanced_search",
    "_build_empty_actions",
    "_clear_similar_seed",
    "_current_ai_mode",
    "_current_date_bounds",
    "_date_bounds_active",
    "_display_unit_key",
    "_drop_advanced_search_scanners",
    "_dynamic_tag_placeholder",
    "_empty_state_relaxations",
    "_engine_is_missing",
    "_ensure_advanced_search_scanners",
    "_ensure_tag_completer_model",
    "_ensure_tag_search_on",
    "_install_tag_completer",
    "_kick_tag_scan",
    "_land_tag_results",
    "_landed_error_kind",
    "_maybe_requery_for_posted_at",
    "_maybe_start_tag_scan",
    "_missing_db_banner_text",
    "_on_ai_mode_selected",
    "_on_browser_tag_excluded",
    "_on_browser_tag_selected",
    "_on_pick_similar_image",
    "_on_reload_tag_db_clicked",
    "_on_reset_advanced_only",
    "_on_similar_clear_clicked",
    "_on_tag_date_changed",
    "_on_tag_date_preset_changed",
    "_on_tag_display_unit_changed",
    "_on_tag_failed",
    "_on_tag_input_changed",
    "_on_tag_media_changed",
    "_on_tag_rating_changed",
    "_on_tag_results",
    "_on_tag_threshold_changed",
    "_on_vector_failed",
    "_on_vector_results",
    "_open_tag_browser",
    "_panel_title_text",
    "_precision_neutral",
    "_query_mode",
    "_rank_mode_available",
    "_refresh_advanced_count_text",
    "_refresh_advanced_status",
    "_refresh_tag_completer",
    "_refresh_tag_examples_hint",
    "_regate_after_vector_load",
    "_relax_ai_tags",
    "_relax_coverage",
    "_relax_date",
    "_relax_excludes",
    "_relax_hide_nsfw",
    "_relax_locked_only",
    "_relax_name_filter",
    "_relax_open_ai_popover",
    "_relax_rating",
    "_relax_threshold",
    "_relaxation_callbacks",
    "_revalidate_ai_mode",
    "_seed_from_path",
    "_set_ai_mode",
    "_set_seed_thumb_pixmap",
    "_shared_condition_values",
    "_show_search_cheatsheet",
    "_shutdown_advanced_search",
    "_size_completer_popup",
    "_sync_ai_segment",
    "_sync_ai_segment_enabled",
    "_sync_missing_db_banner",
    "_sync_tag_browser_terms",
    "_tag_db_is_broken",
    "_tag_query_signature",
    "_tag_terms_narrow_query",
    "_update_advanced_badge",
    "_update_ai_mode_ui",
    "_update_display_unit_combo",
    "_update_relevance_legend",
    "_update_reset_link",
    "_update_shared_conditions",
    "_update_similar_button_state",
    "_update_similar_clear_enabled",
    "_update_similar_seed_display",
    "_update_tag_controls_enabled",
    "add_search_tag",
    "focus_tag_search",
    "open_ai_popover",
    "refresh_tag_index_ui",
    "restore_tag_settings",
    "save_tag_settings",
    "set_similar_seed",
)

#: コントローラ生成前（chrome 構築中）に読まれ得るクエリ値の既定。フィルタ
#: バーのアクセント同期は AI ポップオーバーより先に走るので、そこだけは
#: 「まだ中立」を答える必要がある（**書き込み**は生成前には来ない）。
_DEFAULT_QUERY = AiQuery()


def _query_property(alias: str, field: str) -> property:
    def _get(self):
        ai = self._ai
        return getattr(_DEFAULT_QUERY if ai is None else ai.query, field)

    def _set(self, value) -> None:
        from dataclasses import replace

        ai = self._ai
        ai.set_query(replace(ai.query, **{field: value}))

    _get.__name__ = alias
    return property(_get, _set, doc=f"``_ai.query.{field}`` への委譲。")


def _attr_property(name: str, *, writable: bool) -> property:
    def _get(self):
        return getattr(self._ai, name)

    def _set(self, value) -> None:
        setattr(self._ai, name, value)

    _get.__name__ = name
    return property(_get, _set if writable else None,
                    doc=f"``_ai.{name}`` への委譲。")


def _method(name: str):
    def _call(self, *args, **kwargs):
        return getattr(self._ai, name)(*args, **kwargs)

    _call.__name__ = name
    _call.__doc__ = f"``_ai.{name}`` への委譲。"
    return _call


def _install_delegates(cls) -> None:
    """外から観測される旧名を ``cls._ai`` へ委譲する。"""
    for alias, field in QUERY_FIELDS.items():
        setattr(cls, alias, _query_property(alias, field))
    for name in STATE_ATTRS:
        setattr(cls, name, _attr_property(name, writable=True))
    for name in WIDGETS:
        setattr(cls, name, _attr_property(name, writable=False))
    for name in METHODS:
        setattr(cls, name, _method(name))


def _install_host_adapters(cls) -> None:
    """``cls`` を :class:`~.host.SearchHost` として読める形にする。

    実体はホスト側の既存メンバーで、ここはインタフェース名への読み替え。
    """

    # ---------------------------------------------------------- 読む（状態）
    cls.tag_index = property(lambda self: self._tag_index)
    cls.vector_index = property(lambda self: self._vector_index)
    cls.root_or_folder = property(lambda self: self._root_or_folder)
    cls.filter_locked_only = property(lambda self: self._filter_locked_only)
    cls.filterbar_media = property(lambda self: self._filterbar_media)
    cls.filterbar_star_min = property(lambda self: self._filterbar_star_min)
    cls.filterbar_later = property(lambda self: self._filterbar_later)
    cls.filterbar_user_tag = property(lambda self: self._filterbar_user_tag)
    cls.nsfw_hidden_count = property(lambda self: self._nsfw_hidden_count)
    cls.overlay_active = property(lambda self: self._overlay is not None)
    cls.requery_suspended = property(
        lambda self: bool(getattr(self, "_suspend_requery", False))
    )
    cls.thumb_key_prefix = property(lambda self: self._key_prefix)

    # ------------------------------------- 読む（ホストが席を持つ面）
    cls.rating_combo = property(lambda self: self.tag_rating_combo)
    cls.date_combo = property(lambda self: self.tag_date_combo)
    cls.date_from = property(lambda self: self.tag_date_from)
    cls.date_to = property(lambda self: self.tag_date_to)
    cls.date_separator = property(lambda self: self.tag_date_sep)

    def widget(self):
        return self

    def ai_chip_anchor(self):
        return getattr(self, "mode_chip_ai", None)

    def filter_text(self) -> str:
        return self.filter_edit.text().strip()

    def general_filter_terms(self):
        return self._general_filter_terms()

    def pixmap_for_key(self, key: str):
        return self._view.pixmap_for_key(key)

    def current_tile_path(self):
        view = getattr(self, "_view", None)
        tile = view.current_tile() if view is not None else None
        if tile is None or tile.is_dir:
            return None
        return tile.path

    def shown_tile_count(self):
        view = getattr(self, "_view", None)
        return None if view is None else view.tile_count()

    def breadcrumb_has_trail(self) -> bool:
        crumb = getattr(self, "breadcrumb", None)
        return crumb is not None and crumb.has_trail()

    def breadcrumb_count_text(self) -> str:
        return self.breadcrumb.count_text()

    def set_breadcrumb_count_text(self, text: str) -> None:
        self.breadcrumb.set_count_text(text)

    def panel_reset_dimensions(self):
        return self._panel_reset_dimensions()

    # ------------------------------------------------------------------ 呼ぶ
    def rebuild_grid(self) -> None:
        self._rebuild_grid()

    def preserve_selection_for_rebuild(self, *, ancestor_fallback: bool = False) -> None:
        self._preserve_selection_for_rebuild(ancestor_fallback=ancestor_fallback)

    def set_search_status(self, text: str) -> None:
        self._set_search_status(text)

    def sorted_dir_first(self, entries):
        return self._sorted_dir_first(entries)

    def drop_thumb_markers(self, entries):
        return self._drop_thumb_markers(entries)

    def update_filter_bar(self) -> None:
        self._update_filter_bar()

    def sync_search_mode_chips(self) -> None:
        self._sync_search_mode_chips()

    def strip_filter_control_field(self, field: str) -> None:
        self._strip_filter_control_field(field)

    def batched_condition_clear(self):
        return self._batched_condition_clear()

    def clear_filter_text(self) -> None:
        self.filter_edit.clear()

    def set_locked_only_checked(self, checked: bool) -> None:
        self.locked_check.setChecked(checked)

    def exit_overlay(self) -> None:
        self._exit_overlay()

    def maybe_start_recursive_scan(self) -> None:
        self._maybe_start_recursive_scan()

    def on_date_filter_changed(self) -> None:
        self._on_filterbar_date_changed()

    def set_search_indexes(self, tag_index, vector_index) -> None:
        self._tag_index = tag_index
        self._vector_index = vector_index

    def request_tag_db_reload(self) -> None:
        sig = getattr(self, "reload_tag_db_requested", None)
        if sig is not None:
            sig.emit()

    for fn in (
        widget, ai_chip_anchor, filter_text, general_filter_terms,
        pixmap_for_key, current_tile_path, shown_tile_count,
        breadcrumb_has_trail, breadcrumb_count_text, set_breadcrumb_count_text,
        panel_reset_dimensions, rebuild_grid, preserve_selection_for_rebuild,
        set_search_status, sorted_dir_first, drop_thumb_markers,
        update_filter_bar, sync_search_mode_chips, strip_filter_control_field,
        batched_condition_clear, clear_filter_text, set_locked_only_checked,
        exit_overlay, maybe_start_recursive_scan, on_date_filter_changed,
        set_search_indexes, request_tag_db_reload,
    ):
        setattr(cls, fn.__name__, fn)


def install(cls) -> None:
    """``PostGrid`` にホスト読み替えと旧名の委譲を載せる。"""
    from ..advanced_search import AdvancedSearchController
    from .persistence import select_combo_data

    _install_host_adapters(cls)
    _install_delegates(cls)
    # 汎用のコンボ選択ヘルパ（ホスト自身のフィルタ行も使うので、コントローラ
    # 生成前から呼べる素の staticmethod として載せる）。
    cls._select_combo_data = staticmethod(select_combo_data)
    cls._COMPLETER_POPUP_MAX_W = AdvancedSearchController._COMPLETER_POPUP_MAX_W
