"""Theme switching for the viewer window.

Thin re-export: the actual design system (colour tokens, QPalette, app-wide
QSS, Windows dark title bar) lives in :mod:`snappix.common.ui` so the
plugin windows can share it (colours and font sizes come from its tokens, never literals).

This module also owns the viewer's **theme choice tables** — the single
``(theme key, i18n label key)`` source both selection UIs (表示メニューの
テーマ / 設定ダイアログのテーマコンボ) iterate, so the two can never drift.
The theme keys must stay in sync with ``viewer/state.py::ThemeName`` and
``common/shared_prefs.py::_VALID_THEMES`` (machine-checked by
``tests/test_viewer_theme_menu.py``).
"""

from __future__ import annotations

from ..common.i18n import t
from ..common.ui import EXTRA_THEME_TOKENS, apply_theme, current_tokens

#: Main theme choices, in menu order: OS-following "system" plus the three
#: built-in main palettes.
THEME_CHOICES_MAIN: tuple[tuple[str, str], ...] = (
    ("system", "viewer.main_window.theme_system"),
    ("light", "common.theme.light"),
    ("dark", "common.theme.dark"),
    ("standard", "common.theme.standard"),
)

#: Extra theme choices, in menu order — listed under 表示 ▸ テーマ ▸ その他.
#: Keys mirror ``tokens.EXTRA_THEME_TOKENS`` names (the persisted values).
THEME_CHOICES_EXTRA: tuple[tuple[str, str], ...] = (
    ("extra_astro", "common.theme.extra_astro"),
    ("extra_obsidian", "common.theme.extra_obsidian"),
    ("extra_washi", "common.theme.extra_washi"),
    ("extra_linen", "common.theme.extra_linen"),
    ("extra_brass", "common.theme.extra_brass"),
    ("extra_dusk", "common.theme.extra_dusk"),
)

#: key → is_dark for the「その他」テーマ一覧。``ThemeTokens.is_dark`` を単一の
#: 真実源とし、明暗の情報をここで重複定義しない。
_EXTRA_THEME_IS_DARK: dict[str, bool] = {
    tok.name: tok.is_dark for tok in EXTRA_THEME_TOKENS
}


def extra_theme_label(key: str, label_key: str) -> str:
    """「その他」テーマの表示名 + 明暗サフィックス（両導線の単一情報源）.

    10 テーマの中で追加 6 種は名前（「天文台の赤色灯」「書院の和紙」…）だけ
    では明暗が読めないため、表示名に「（ダーク）」「（ライト）」を付ける。
    設定ダイアログのコンボと 表示メニュー ▸ テーマ ▸ その他 の両方が
    この 1 関数を呼ぶことで、ラベルの付け方が片側だけになれないようにする。

    i18n キーは歴史的経緯で ``viewer.settings_dialog.*`` のままにしてある
    （文言そのものは変わっておらず、キー改名は翻訳の互換を無意味に切るだけ）。
    置き場はこのモジュールが正。
    """
    name = t(label_key)
    suffix_key = (
        "viewer.settings_dialog.theme_extra_suffix_dark"
        if _EXTRA_THEME_IS_DARK.get(key)
        else "viewer.settings_dialog.theme_extra_suffix_light"
    )
    return t(suffix_key, name=name)


__all__ = [
    "apply_theme",
    "current_tokens",
    "extra_theme_label",
    "THEME_CHOICES_MAIN",
    "THEME_CHOICES_EXTRA",
]
