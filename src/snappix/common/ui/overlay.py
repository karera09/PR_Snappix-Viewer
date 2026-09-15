"""Fixed colour palette for overlays drawn ON TOP OF image content.

This is the registered **theme-independent exception** to the design system
(see docs/claude/design.md 使用ルール 2): badges / chips / scrims / capsules
that ride on top of arbitrary thumbnails or full-screen photos must stay
legible on any image, so they use fixed dark scrims + fixed light chrome +
fixed accent hues instead of following the theme tokens.

Every fixed overlay colour in the viewer lives here so a future palette
change has exactly one place to look.  **UI-surface colours do NOT belong
here — those go through ``tokens.py`` (theme-following).**  Only add a colour
here when it is painted over image content and must be theme-independent.

Values are grouped by *meaning*; constants that happen to share the same
value are unified, but constants with different values are kept separate
even when their roles are similar (e.g. the two button-hover tints differ by
5 in alpha — that is preserved, not "rounded together").

``QColor`` objects are safe to build at import time (no ``QApplication``
needed), matching the long-standing pattern in ``_indicator.py``.  For QSS
strings, pass a constant through :func:`rgba_str`.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from .tokens import DARK_TOKENS

# --------------------------------------------------------------- dark scrims
# Translucent black backgrounds behind overlay chrome, by weight.
SPINNER_SCRIM = QColor(0, 0, 0, 110)        # thumbnail loading spinner backdrop
SCRIM_CHIP = QColor(0, 0, 0, 140)           # badge chips / similar button / zoom + counter capsules
SCRIM_CHIP_STRONG = QColor(0, 0, 0, 150)    # favorites / star / badge-row chips / control bar
SCRIM_HEAVY = QColor(0, 0, 0, 170)          # lightbox mat + hint / title overlays

# Bottom title-band gradient on tile thumbnails (transparent → near-opaque).
# Three stops, not two (UIレビュー 2026-08-28 N-20): with a plain 0→190 ramp
# the caption's FIRST line sits at f≈0.10–0.5 of the band and only got α20–100,
# measuring 2.7–6.2:1 on bright thumbnails (AA 4.5:1 未達が常態).  The band now
# reserves a run-up (``gallery_view_parts.painter.SCRIM_RAMP``) above the
# text so the mid stop — α140 ≈ 4.7:1 against white — lands exactly where
# the first line starts.
TILE_SCRIM_MID = QColor(0, 0, 0, 140)
TILE_SCRIM_TOP = QColor(0, 0, 0, 0)
TILE_SCRIM_BOTTOM = QColor(0, 0, 0, 190)

# --------------------------------------------------------------- light chrome
# Translucent white fills / text painted on the dark scrims above.
PLACEHOLDER_WHITE = QColor(255, 255, 255, 28)   # filmstrip cell placeholder
CELL_MAT_WHITE = QColor(255, 255, 255, 12)      # filmstrip cell mat (behind a loaded thumb)
LIGHTBOX_BTN_HOVER = QColor(255, 255, 255, 40)  # lightbox top-bar button hover
OVERLAY_BTN_HOVER = QColor(255, 255, 255, 45)   # image control-bar button hover
SPINNER_TRACK = QColor(255, 255, 255, 70)       # spinner track ring
SPINNER_ARC = QColor(255, 255, 255, 230)        # spinner moving arc
OVERLAY_TEXT = QColor(255, 255, 255, 235)       # chip text / similar glyph
OVERLAY_TEXT_STRONG = QColor(255, 255, 255, 240)  # title band / relevance text
# 本編ではない内部/メタファイル（post.md・``#thumb#``）のタイトル。画像上の
# 帯なので QPalette の Disabled ロール（＝下帯キャプション側の減光）は使え
# ず、同じ「1 段落とす」をオーバーレイの語彙で表したもの
# (UIレビュー 07-25 #52)。
OVERLAY_TEXT_DIM = QColor(255, 255, 255, 150)

# CSS colour string for large text QLabels on the heaviest scrim (lightbox /
# control-bar glyphs).  Bright, but sourced so it reads on near-black; kept as
# a hex string because it feeds QSS ``color:`` and SVG ``stroke=``.
CTRL_ICON = "#f5f5f5"

# CSS colour string for the audio-only label painted over the black video
# widget.  Same family as :data:`CTRL_ICON` (a QSS ``color:`` on a fixed-black
# ground), listed here rather than spelled at the draw site so the promise
# "the palette changes from tokens.py and overlay.py alone" actually holds —
# a bare ``color: white`` at the widget was the one hole that broke it.
MEDIA_LABEL_TEXT = "#ffffff"

# CSS colour string for the video widget's letterbox ground.  Fixed black is
# the convention for video surfaces (the frame is the content; the ground must
# not tint it), and it is a QSS ``background:`` so it lives here as a string
# next to :data:`MINIMAP_BACKDROP`, its painted twin.
MEDIA_LETTERBOX = "#000000"

# ------------------------------------------------------------------- minimap
# The pan minimap is a miniature of the image itself, so its backdrop and the
# viewport rectangle drawn on top of it are image-overlay colours like every
# other constant here — they were the last three fixed colours still spelled
# out at their draw site (``Qt.GlobalColor.black`` / ``.yellow``), where the
# hardcoded-colour guard structurally cannot see them.
MINIMAP_BACKDROP = QColor(0, 0, 0)          # letterbox behind the miniature
MINIMAP_VIEW_RECT = QColor(255, 255, 0)     # "you are here" viewport outline

# --------------------------------------------------------------- accent hues
# Fixed colour accents on badges — deliberately NOT the theme accent, so
# readability is tied to the underlying image rather than the UI theme.
FOLDER_FILL = QColor(245, 200, 66)          # gold folder pictogram fill
FOLDER_BORDER = QColor(140, 95, 15)         # folder pictogram outline
FOLDER_LIST_TINT = QColor(245, 200, 50, 20)  # soft gold row wash (list mode)
HEART_RED = QColor(255, 110, 130, 245)      # favorites heart
STAR_GOLD = QColor(255, 205, 60, 250)       # star rating glyph
LATER_BLUE = QColor(70, 150, 230, 235)      # "watch later" pennant
RELEVANCE_BLUE = QColor(30, 90, 160, 205)   # relevance-percent pill

# Lightbox big-label text colour: brightest token text, on the heaviest scrim.
# Token-derived (not a raw literal) but kept here so the overlay palette is
# discoverable in one place.
LABEL_TEXT = DARK_TOKENS.text


def rgba_str(color: QColor) -> str:
    """``QColor`` → CSS ``rgba(r, g, b, a)`` for QSS (integer 0–255 alpha).

    Matches the alpha convention the viewer's overlay QSS already uses
    (Qt accepts a 0–255 integer alpha in ``rgba()``); distinct from
    ``tokens.rgba`` which takes a 0.0–1.0 opacity for theme-following
    translucency.
    """
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
