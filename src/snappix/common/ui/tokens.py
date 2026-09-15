"""Design tokens: the colour palette + metrics every snappix GUI draws from.

Pure data — no Qt imports — so the palette can be inspected in tests and
reused by any tool in the repo.  All colours are ``#rrggbb`` hex strings;
use :func:`rgba` when a translucent CSS value is needed and
``QColor(token)`` when painting.

Rules (see docs/claude/design.md):

- Widgets must NOT hardcode colours.  Take them from here (directly via
  ``current_tokens()`` for custom painting, or indirectly via
  ``palette(...)`` roles in QSS — the application QPalette is generated
  from these tokens too).
- Exception: overlays anchored to *image content* (badges / chips drawn on
  top of thumbnails, video letterboxing) keep fixed dark-scrim colours —
  they must be legible on arbitrary photos, not match the UI theme.
- Adding a theme = creating one ``ThemeTokens`` instance (here for
  built-ins, or in the owning tool/plugin), registering it via
  ``theme.register_theme``, and wiring a menu entry; nothing else needs
  to change ("system" OS resolution stays pinned to the built-in pair).
"""

from __future__ import annotations

from dataclasses import dataclass

# ----------------------------------------------------------------- metrics
# Corner radius for interactive controls (buttons, inputs, menus).
RADIUS = 6
# Smaller radius for compact elements (chips, list selections, badges).
RADIUS_SM = 4
# Height of the window-level unified toolbar (nav / breadcrumb / search /
# view controls — layout redesign 2026-07 Phase 1-1).  Single definition so
# the widget's fixed height and any dependent QSS can never drift apart.
TOOLBAR_HEIGHT = 40

# Height of the window-level condition chip bar (applied search-condition
# chips + hit count + 「すべて解除」 — layout redesign 2026-07 Phase 1-3).
# Sits directly under the unified toolbar and collapses to zero height when
# no condition is engaged; one definition keeps the widget and its QSS in
# lockstep, like TOOLBAR_HEIGHT above.
CONDITION_BAR_HEIGHT = 32

# Height of the preview column's bottom image track (the horizontal one-row
# strip of the current post's own content shown under the centre preview
# while the preview is maximised — split-view layout redesign 2026-07).
# One definition so the widget's fixed height, its cell geometry and any
# dependent QSS can never drift apart.  (The former secondary sibling-post
# track, FILMSTRIP_POST_HEIGHT=56, was removed with the split-view redesign
# — the always-visible centre grid took over its role.)
FILMSTRIP_HEIGHT = 96

# Standard height (px) for the compact pane-header strip (title + count +
# optional "⋯" overflow entry) introduced by the 2026-07 layout redesign
# (Phase 1-2, see the `PanelHeader` section of docs/claude/design.md and
# ``common/ui/widgets.py::PanelHeader``).  One definition so every pane that
# adopts PanelHeader lines up.
PANEL_HEADER_HEIGHT = 28

# Typography scale (pt).  Body text uses the Qt default font size; the
# named steps below are for headings/captions so every view agrees.
FONT_TITLE_PT = 14      # pane titles (post title, folder preview header)
FONT_SUBTITLE_PT = 12   # secondary headers / large hints
FONT_BODY_PT = 10       # explicit body size where one must be pinned
FONT_CAPTION_PT = 9     # metadata lines, footnotes


def rgba(hex_color: str, alpha: float) -> str:
    """``#rrggbb`` + opacity (0.0–1.0) → CSS ``rgba(r, g, b, a)`` string."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


@dataclass(frozen=True)
class ThemeTokens:
    """One theme's complete colour palette, by semantic role."""

    name: str            # stable identifier ("dark" / "light" / "standard" /
                         # "extra_*") — doubles as the persisted theme value
    is_dark: bool        # drives the Windows title-bar mode

    # --- backgrounds (lightest → darkest relationship differs per theme;
    #     think in terms of *elevation*, not literal brightness)
    bg_window: str       # window chrome / panels around content
    bg_surface: str      # content areas: lists, editors, grids (QPalette Base)
    bg_raised: str       # alternate rows, cards, popup surfaces
    bg_hover: str        # hover fill on buttons / items
    bg_pressed: str      # pressed fill on buttons / items
    bg_stage: str        # image "stage" backdrop (deeper than any UI surface)
                         # — the surround behind a centred preview + the stage
                         # filmstrip, so the image reads as *exhibited* rather
                         # than sitting on a chrome panel (redesign 2026-07
                         # Phase 3-1).  Dark: near-black; light: a mid grey mat
                         # darker than the paper surfaces so light images float.
                         # NOT a QPalette role — document pages (markdown / PDF)
                         # and the browse grid keep their normal UI surfaces.

    # --- borders
    border: str          # hairline separators, control outlines
    border_strong: str   # emphasised outlines (hover on inputs, handles)

    # --- text
    text: str            # primary text (QPalette Text / WindowText)
    text_muted: str      # secondary labels, hints (QPalette Mid)
    text_disabled: str   # disabled controls / placeholders
    text_on_accent: str  # text sitting on the accent colour

    # --- accent (selection, focus, primary action)
    accent: str
    accent_hover: str
    accent_pressed: str
    link: str

    # --- status
    danger: str          # errors, destructive markers, missing paths
    warning: str
    success: str


# --------------------------------------------------------------- main themes
# 2026-07 の配色刷新（palette exploration の最終選考より）。3 つのメイン
# テーマ + 6 つの追加テーマ（下の EXTRA_THEME_TOKENS）は全てここが唯一の
# 定義場所（tests/test_no_hardcoded_colors.py のハードコード色ガードは
# tokens.py / overlay.py だけを許可する）。

# 「呂色金継ぎ」— 漆器の呂色塗りが持つ赤みを含んだ深い黒の明度階層。
# アクセントは金継ぎの金（選択・フォーカスにだけ現れる同系暖色）。
# 状態色は朱漆 (danger)・金蒔絵 (warning)・緑青 (success)。
DARK_TOKENS = ThemeTokens(
    name="dark",
    is_dark=True,
    bg_window="#171310",
    bg_surface="#1c1714",
    bg_raised="#262019",
    bg_hover="#302921",
    bg_pressed="#3b332a",
    bg_stage="#0b0908",
    border="#362d24",
    border_strong="#584a3b",
    text="#f0e7d8",
    text_muted="#b3a48f",
    # UIレビュー 2026-08-28 N-53: 旧 #6f6152 は bg_surface では床(2.9)を満たす
    # が、メニュー / ポップオーバーの下地 bg_raised では 2.69 まで落ちていた。
    # 明度を 1 段上げて raised 面でも床を越えさせる(raised 3.04 / surface 3.35)。
    text_disabled="#786959",
    text_on_accent="#221507",
    accent="#c9a24a",
    accent_hover="#dab55e",
    accent_pressed="#b28c3a",
    link="#e2bd6a",
    danger="#e0705a",
    warning="#e0a33c",
    success="#8fb573",
)

# 「白壁とエーゲの光」— 地中海の漆喰壁が反射する暖白の明度階層。アクセントは
# 補色のエーゲ海の深い青緑、bg_stage は白壁の影の青み石灰グレーのマット台紙。
# 状態色はテラコッタ赤・オークル黄・オリーブ緑。
LIGHT_TOKENS = ThemeTokens(
    name="light",
    is_dark=False,
    bg_window="#f1ebdf",
    bg_surface="#f9f5ec",
    bg_raised="#fdfbf5",
    bg_hover="#ece4d3",
    bg_pressed="#e0d5be",
    bg_stage="#99a0a5",
    border="#ddd3c0",
    border_strong="#b0a48b",
    text="#25313a",
    text_muted="#5b6570",
    # 旧 #a3a89f は bg_surface 比 2.23:1 で dark/standard (約 3.0:1) より 1 段
    # 薄く、light だけプレースホルダ/無効ラベルが読めなかった (UIレビュー
    # 07-25 #99)。3.0:1 へ揃える。
    text_disabled="#8a9089",
    text_on_accent="#ffffff",
    accent="#1e6f96",
    accent_hover="#2a7ea6",
    accent_pressed="#175a7c",
    link="#1a648a",
    danger="#b23a2a",
    warning="#976400",
    success="#3f7a44",
)

# 「スタジオ定常光」— タングステン定常光の温かい炭灰色 UI に、色再現を歪めない
# 無彩色グレー背景紙 (bg_stage) を組み合わせた第 3 の組み込みメインテーマ。
# bg_stage が UI 面より明るい中明度ニュートラルである点が dark / light と違う
# 意図的な設計（ギャラリー標準のグレー背景紙）。
STANDARD_TOKENS = ThemeTokens(
    name="standard",
    is_dark=True,
    bg_window="#282523",
    bg_surface="#302d2b",
    bg_raised="#3a3733",
    bg_hover="#44403b",
    bg_pressed="#4e4943",
    bg_stage="#4b4b4e",
    border="#4a453f",
    border_strong="#6e675e",
    text="#f0e9e2",
    text_muted="#b3aca4",
    # UIレビュー 2026-08-28 N-53（dark と同じ理由 — 旧 #7d766d は raised 2.64）。
    text_disabled="#877f75",
    text_on_accent="#2a1f0e",
    accent="#e8a13c",
    accent_hover="#f0b055",
    accent_pressed="#d18d2b",
    link="#8fc1e8",
    danger="#ea7863",
    warning="#e3c34f",
    success="#7fbf7a",
)


# -------------------------------------------------------------- extra themes
# 表示メニュー「テーマ ▸ その他」に並ぶ追加テーマ。名前 (``name``) がそのまま
# 保存値（shared_prefs.json / viewer_state.json の theme フィールド）になるので
# 変更しないこと。タプル順がメニューの並び順。
EXTRA_THEME_TOKENS: tuple[ThemeTokens, ...] = (
    # 「天文台の赤色灯」— 低彩度の藍墨階調 + 暗順応を保つ観測用赤色灯アクセント。
    ThemeTokens(
        name="extra_astro",
        is_dark=True,
        bg_window="#131521",
        bg_surface="#181b28",
        bg_raised="#202435",
        bg_hover="#272c40",
        bg_pressed="#2e344b",
        bg_stage="#0a0c14",
        border="#2b3044",
        border_strong="#4a5474",
        text="#e9e9f2",
        text_muted="#a8abc4",
        text_disabled="#5c6076",
        text_on_accent="#2a1210",
        accent="#f0705f",
        accent_hover="#f58372",
        accent_pressed="#d95a4a",
        link="#82b7ef",
        danger="#ff5c74",
        warning="#f2b25c",
        success="#6cc98f",
    ),
    # 「黒曜と熔岩」— 赤紫寄りの無彩黒 + 岩の裂け目から覗く熔岩オレンジ。
    ThemeTokens(
        name="extra_obsidian",
        is_dark=True,
        bg_window="#171114",
        bg_surface="#1e171a",
        bg_raised="#281f23",
        bg_hover="#33282c",
        bg_pressed="#3e3136",
        bg_stage="#0a0709",
        border="#362a2f",
        border_strong="#5c4a51",
        text="#f4ece7",
        text_muted="#b5a5a3",
        text_disabled="#71625f",
        text_on_accent="#240e05",
        accent="#f56a2e",
        accent_hover="#ff7d42",
        accent_pressed="#dd5a22",
        link="#ffa06a",
        danger="#ff5c5c",
        warning="#ffb454",
        success="#58c98a",
    ),
    # 「書院の和紙」— 生成りの和紙の暖白階調 + 朱肉の朱アクセント + 墨色の文字。
    ThemeTokens(
        name="extra_washi",
        is_dark=False,
        bg_window="#ede8dd",
        bg_surface="#f5f1e8",
        bg_raised="#faf7f0",
        bg_hover="#eae4d6",
        bg_pressed="#e0d8c6",
        bg_stage="#8d867a",
        border="#d9d2c2",
        border_strong="#a89f8c",
        text="#2b2723",
        text_muted="#6b6355",
        text_disabled="#aba290",
        text_on_accent="#fffcf5",
        accent="#c2401f",
        accent_hover="#d34e2a",
        accent_pressed="#a33317",
        link="#275e8a",
        danger="#a01c2e",
        warning="#946200",
        success="#3d7a45",
    ),
    # 「亜麻色のアトリエ」— リネンの暖色無彩階調 + 亜麻の花の青紫アクセント。
    ThemeTokens(
        name="extra_linen",
        is_dark=False,
        bg_window="#ede7da",
        bg_surface="#f7f3ea",
        bg_raised="#fdfaf3",
        bg_hover="#e9e1d1",
        bg_pressed="#ded4bf",
        bg_stage="#8d8578",
        border="#d8cfbd",
        border_strong="#a89d88",
        text="#38332a",
        text_muted="#6b6355",
        text_disabled="#a89e8c",
        text_on_accent="#f9f7f0",
        accent="#5566ad",
        accent_hover="#6172c1",
        accent_pressed="#44528f",
        link="#41569c",
        danger="#b23a30",
        warning="#95660a",
        success="#4e7a38",
    ),
    # 「真鍮の計器盤」— 暗い中間調の金属グレー + 磨かれた真鍮の金アクセント +
    # 真鍮の緑青 (パティナ) のリンク。
    ThemeTokens(
        name="extra_brass",
        is_dark=True,
        bg_window="#24262a",
        bg_surface="#1e2023",
        bg_raised="#2b2e32",
        bg_hover="#33363b",
        bg_pressed="#3b3f45",
        bg_stage="#121316",
        border="#3a3d42",
        border_strong="#565b63",
        text="#ece7dc",
        text_muted="#b3ab9c",
        text_disabled="#6e6a61",
        text_on_accent="#1c1710",
        accent="#c9a24d",
        accent_hover="#d6b269",
        accent_pressed="#b58e3a",
        link="#58b8a4",
        danger="#e26d5a",
        warning="#e08a3c",
        success="#7fb069",
    ),
    # 「薄暮の刻」— ブルーアワーの藍系単色階調 + 灯りはじめた街灯の琥珀アクセント。
    ThemeTokens(
        name="extra_dusk",
        is_dark=True,
        bg_window="#131a2e",
        bg_surface="#182036",
        bg_raised="#212b48",
        bg_hover="#2a3557",
        bg_pressed="#334169",
        bg_stage="#0a0e1c",
        border="#2b3453",
        border_strong="#4a5680",
        text="#e9edf8",
        text_muted="#a8b2d1",
        text_disabled="#5e6887",
        text_on_accent="#251a10",
        accent="#f2a45f",
        accent_hover="#f8b678",
        accent_pressed="#e08f45",
        link="#9db8ff",
        danger="#ff7d85",
        warning="#f5c35a",
        success="#6fd79b",
    ),
)
