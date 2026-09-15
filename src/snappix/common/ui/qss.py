"""Generate the app-wide Qt stylesheet from :class:`ThemeTokens`.

Everything here derives from the tokens — no literal colours.  The QSS
covers the chrome-level widgets (buttons, inputs, scrollbars, menus, tabs,
splitters, …); item views with custom delegates and custom-painted widgets
keep reading the QPalette, which is generated from the same tokens.

Styling notes (Qt stylesheet quirks worth keeping in mind):

- QScrollBar must be styled *completely* (handle + add/sub-line zeroed +
  transparent page areas) — a partial style falls back to an ugly hybrid.
- Styling a QComboBox / QSpinBox *box* makes QStyleSheetStyle stop drawing
  their default arrows (verified empirically; menu checkmarks and submenu
  arrows DO survive).  The chevron images referenced by ``::down-arrow`` /
  ``::up-arrow`` are tiny SVGs generated per theme colour into
  ``data/cache/qss/`` under the portable app root (QSS ``url()`` cannot
  embed data URIs).  Keeping them inside the app tree honours the
  portability rule (no writes to %TEMP% / the user home) and lets the files
  be reused across runs.  Best-effort: if the write fails the rules are
  omitted and the arrows are simply absent.
- Popup top-levels (QMenu, the combo drop-down view, the ``#toolbarPopover``
  frames) get NO QSS ``border-radius``: a radius on an opaque top-level paints
  the pixels outside the arc with the window backing, leaving a square backing
  poking out behind the rounded panel.  Their corners are rounded by DWM on the
  actual window instead (see ``theme.py::_PopupCornerWatcher``), which keeps the
  fill opaque and clips the corners cleanly — square (never black) on Windows 10
  / non-Windows.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .tokens import FONT_CAPTION_PT, RADIUS, RADIUS_SM, ThemeTokens, rgba

_assets_dir: Path | None = None

#: ``(assets dir, glyph, colour, stroke) -> (svg source written, url path)``.
#: :func:`build_qss` asks for several glyph files every time it runs, and each
#: ask reads the file back to compare — synchronous I/O on the GUI thread,
#: against a portable base that may be a NAS share.  The source is part of the
#: value (not just the key) so a glyph table replaced at runtime
#: (``icons.register_icons``) still rewrites the file.  The assets dir is part
#: of the key so redirecting it invalidates nothing else.
_glyph_cache: dict[tuple[Path, str, str, float], tuple[str, str]] = {}


def _resolve_assets_dir() -> Path:
    """The portable cache dir for generated chevron SVGs (``data/cache/qss``).

    Kept inside the app's ``data/`` tree — never %TEMP% / the user home — so
    the portability contract holds and the tiny SVGs can be reused across
    runs.  ``common.ui`` importing ``common.paths`` is allowed (both are
    shared layer).
    """
    from ..paths import get_paths

    return get_paths().data / "cache" / "qss"


def _chevron_url(direction: str, color: str) -> str | None:
    """Write a chevron SVG tinted *color* and return its QSS url path (see
    :func:`_glyph_url` — kept as the named entry point the arrow rules and
    their tests use)."""
    # stroke-width 2.5: the arrows render at 10-12 px, where the icon
    # default (2) looks too thin.
    return _glyph_url(f"chevron-{direction}", color, stroke_width=2.5)


def _glyph_url(name: str, color: str, *, stroke_width: float = 2.5) -> str | None:
    """Write glyph *name* tinted *color* as an SVG and return its QSS url path.

    One file per (glyph, colour) so repeated ``apply_theme`` calls and
    light/dark switches coexist in the same cache dir.  Returns ``None`` when
    the file cannot be written (the referencing rules are then omitted —
    cosmetic degradation only).

    The file name keys only the glyph name and the colour, **not** the glyph
    source itself, so a plain ``exists()`` check would pin a user's ``data/``
    to the shape/stroke shipped by whatever version created it — a new
    release's redrawn chevron would never appear (review #116).  Compare the
    rendered source with what is on disk instead and rewrite only when it
    differs (unchanged runs stay write-free).
    """
    # Runtime import: icons.py is the one home for glyph paths + the SVG
    # scaffold; QSS only adds the file write (url() cannot embed data URIs).
    from .icons import svg_source

    global _assets_dir
    try:
        if _assets_dir is None:
            _assets_dir = _resolve_assets_dir()
            _assets_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = _assets_dir
        svg = svg_source(name, color, stroke_width=stroke_width)
        key = (assets_dir, name, color, stroke_width)
        cached = _glyph_cache.get(key)
        if cached is not None and cached[0] == svg:
            # This process already wrote exactly this source to that file.
            return cached[1]
        path = assets_dir / f"{name}-{color.lstrip('#')}.svg"
        try:
            on_disk: str | None = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # missing / unreadable / corrupt
            on_disk = None
        if on_disk != svg:
            path.write_text(svg, encoding="utf-8")
        url = path.as_posix()
        _glyph_cache[key] = (svg, url)
        return url
    except OSError:  # pragma: no cover - readonly/full temp volume
        return None


def _qss_url(path: str) -> str:
    """Wrap *path* in a double-quoted QSS ``url()`` token.

    The path MUST be quoted: unquoted, an apostrophe (or other CSS-special
    character) in the portable app root — e.g. ``D:/Tom's Files/…`` — makes
    Qt's CSS parser fail, and a parse failure discards the *entire*
    application stylesheet, not just this declaration.  Double quotes with
    ``\\`` / ``"`` escaped keep any real filesystem path valid.
    """
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'url("{escaped}")'


def _arrow_rules(t: ThemeTokens) -> str:
    """``::down-arrow`` / ``::up-arrow`` rules with generated chevrons."""
    down = _chevron_url("down", t.text_muted)
    up = _chevron_url("up", t.text_muted)
    down_dis = _chevron_url("down", t.text_disabled)
    up_dis = _chevron_url("up", t.text_disabled)
    if down is None or up is None:
        return ""
    down_url, up_url = _qss_url(down), _qss_url(up)
    rules = f"""
QComboBox::down-arrow {{
    image: {down_url};
    width: 12px;
    height: 12px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: {up_url};
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: {down_url};
    width: 10px;
    height: 10px;
}}
"""
    if down_dis is not None and up_dis is not None:
        down_dis_url, up_dis_url = _qss_url(down_dis), _qss_url(up_dis)
        rules += f"""
QComboBox::down-arrow:disabled {{ image: {down_dis_url}; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled {{
    image: {up_dis_url};
}}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
    image: {down_dis_url};
}}
"""
    return rules


#: Check/radio indicator edge (px).  Matches Fusion's own 14px box so no layout
#: shifts when the rules below take over the drawing.
_CHECK_INDICATOR_PX = 14

#: Every selector prefix whose ``::indicator`` gets the outlined-box treatment.
#: ``QRadioButton`` draws the same box with a full border-radius (a circle);
#: the item views carry checkable rows (フィルタのツリー / 一覧) whose indicator
#: is the identical control.
_INDICATOR_SELECTORS = (
    "QCheckBox",
    "QTreeView",
    "QListView",
    "QTableView",
    "QRadioButton",
)


def _indicator_rules(t: ThemeTokens) -> str:
    """``::indicator`` rules — an explicitly outlined box + tick, per selector.

    Unstyled, Fusion derives the unchecked indicator's outline from
    ``QPalette`` roles ``Dark`` / ``Shadow``, which ``palette.py::build_palette``
    never sets — the resulting outline measured **1.08 (dark) / 1.67 (light) /
    1.19 (standard)** against ``bg_raised`` for the checkbox, and **1.04** for
    the radio button, i.e. every theme was below WCAG 1.4.11's 3:1 for non-text
    controls and the control read as "no control at all".  Pinning the border to
    ``text_muted`` (= ``palette(mid)``, the same role ``build_palette`` already
    pins for exactly this class of Fusion derivation) lifts it to 5.3–6.6.

    Styling the indicator makes Qt stop painting the native mark (design.md
    「QSS を書くと画像必須になる」), so the marks come from the shared glyph
    table via :func:`_glyph_url` — and the ``:checked`` fill **must** be
    generated alongside the frame: with only the frame rule, checked and
    unchecked render identically.  If a glyph write fails that mark rule is
    dropped and the state still reads as a filled accent box — unambiguous
    against unchecked, never an unstyled hybrid.

    Which mark depends on the control, because the marks mean different
    things: a checkbox takes the tick (any number of them may be on), a radio
    takes a filled dot (exactly one of the group is on).  Sharing the tick
    made a radio group read as "checkboxes with rounder corners".
    ``:indeterminate`` gets a dash so a tri-state item ("some children
    selected") is not pixel-identical to unchecked.
    """
    edge = _CHECK_INDICATOR_PX
    dash = _glyph_url("minus", t.text_on_accent, stroke_width=3.0)
    dash_line = f"    image: {_qss_url(dash)};\n" if dash is not None else ""
    rules = ""
    for selector in _INDICATOR_SELECTORS:
        # ラジオだけは同じ箱を「丸」で、選択の印も「丸」で描く。
        is_radio = selector == "QRadioButton"
        radius = edge // 2 if is_radio else RADIUS_SM
        mark = _glyph_url(
            "radio-dot" if is_radio else "check",
            t.text_on_accent,
            stroke_width=3.0,
        )
        mark_line = f"    image: {_qss_url(mark)};\n" if mark is not None else ""
        rules += f"""
{selector}::indicator {{
    width: {edge}px;
    height: {edge}px;
    border: 1px solid {t.text_muted};
    border-radius: {radius}px;
    background: {t.bg_surface};
}}
{selector}::indicator:hover {{
    border-color: {t.accent};
}}
{selector}::indicator:checked {{
{mark_line}    background: {t.accent};
    border-color: {t.accent};
}}
{selector}::indicator:checked:hover {{
    background: {t.accent_hover};
    border-color: {t.accent_hover};
}}
{selector}::indicator:indeterminate {{
{dash_line}    background: {t.accent};
    border-color: {t.accent};
}}
{selector}::indicator:indeterminate:hover {{
    background: {t.accent_hover};
    border-color: {t.accent_hover};
}}
{selector}::indicator:disabled {{
    border-color: {t.text_disabled};
    background: {t.bg_window};
}}
{selector}::indicator:checked:disabled {{
    background: {t.text_disabled};
    border-color: {t.text_disabled};
}}
{selector}::indicator:indeterminate:disabled {{
    background: {t.text_disabled};
    border-color: {t.text_disabled};
}}
"""
    return rules


# QSS は文字列定数として配布物に載るので、CSS コメントに経緯（レビュー項目
# 番号等）を書かない。由来: destructiveButton = UI レビュー #13 /
# QProgressBar::chunk の淡色 = UI レビュー 07-25 #17 / thumbSizeSlider の
# 当たり判定 = UI レビュー 2026-08-28 N-126 / toast の面 = UI レビュー #16 /
# stage の QAbstractScrollArea#id 指定 = UI レビュー 07-25 #1 / 節見出しの
# 各領域は Phase 1〜3 の段階導入で追加 / toast の kind 配色を QSS 側へ寄せた
# 経緯は #117 / #117追補 / #184。
def build_qss(t: ThemeTokens) -> str:
    # Translucent accent used for selected-but-unfocused / checked states.
    accent_soft = rgba(t.accent, 0.30)
    # Translucent accent wash for the progress-bar chunk — the bar's own text
    # sits on top of it (UIレビュー 07-25 #17).
    progress_chunk = rgba(t.accent, 0.35)
    # Translucent danger fills for the destructive-button hover/press states.
    danger_hover = rgba(t.danger, 0.12)
    danger_pressed = rgba(t.danger, 0.22)

    return f"""
/* ---------------------------------------------------------- tooltips */
/* A compact "explanation" surface, deliberately set apart from the chrome so
   it reads as secondary and floating: the *lightest* neutral fill (bg_pressed,
   distinct from the window/panel elevations so it never blends in) and
   caption-size text.
   NO `padding` here on purpose: a `padding` on QToolTip makes Qt inflate the
   internal QTipLabel margin to (h-padding + frame) on ALL four sides — e.g.
   `padding: 2px 8px` balloons a one-line tip from ~22px to ~42px tall, and the
   value barely tracks the number you set.  Dropping the rule leaves the tight
   native frame margin (~2px), which is the compact look we want.
   Corners are rounded by DWM on the opaque tip window (see theme.py) — NOT via
   a QSS border-radius, which on an opaque top-level leaves black pixels outside
   the arc.  The fade-in is disabled (instant paint), also from theme.py. */
QToolTip {{
    background: {t.bg_pressed};
    color: {t.text};
    border: 1px solid {t.border_strong};
    font-size: {FONT_CAPTION_PT}pt;
}}

/* -------------------------------------------------------------- toasts */
/* common/ui/toast.py::Toast — the floating corner notification.  The body
   deliberately mirrors the tooltip above (bg_pressed + border_strong): a
   raised-surface toast melts into the page in the light theme.  The
   ``kind`` accent stripe is selected by the ``toastKind`` dynamic
   property, set once at construction (Toast normalises unknown kinds to
   "info", and the property never changes afterwards, so no unpolish/repolish
   wiring is needed).  Living in this app-wide sheet — instead of a
   per-widget setStyleSheet baking the current tokens into each toast —
   makes theme switches follow automatically and removes the
   changeEvent(PaletteChange) hook + Windows re-entry guard the per-widget
   approach required.
   The kind → colour mapping mirrors toast.py::_KIND_ROLES (guarded by
   tests/test_ui_toast.py). */
QFrame#toast {{
    background-color: {t.bg_pressed};
    border: 1px solid {t.border_strong};
    border-left: 3px solid {t.accent};
    border-radius: {RADIUS}px;
}}
QFrame#toast[toastKind="success"] {{ border-left: 3px solid {t.success}; }}
QFrame#toast[toastKind="warning"] {{ border-left: 3px solid {t.warning}; }}
QFrame#toast[toastKind="error"] {{ border-left: 3px solid {t.danger}; }}
QFrame#toast QLabel {{
    color: {t.text};
    background: transparent;
    border: none;
}}

/* ------------------------------------------------------------ buttons */
QPushButton {{
    background: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS}px;
    padding: 4px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {t.bg_hover};
    border-color: {t.border_strong};
}}
QPushButton:pressed {{
    background: {t.bg_pressed};
}}
QPushButton:focus {{
    border-color: {t.accent};
}}
QPushButton:default {{
    background: {t.accent};
    color: {t.text_on_accent};
    border-color: {t.accent};
}}
QPushButton:default:hover {{
    background: {t.accent_hover};
}}
QPushButton:default:pressed {{
    background: {t.accent_pressed};
}}
QPushButton:disabled {{
    background: {t.bg_window};
    color: {t.text_disabled};
    border-color: {t.border};
}}

/* Destructive actions (irreversible bulk delete etc.):
   objectName marker only, so a normal button stays untouched by default.
   Text/border go to `danger`; fill stays the plain button surface so the
   red reads as a warning accent, not a solid alarm block. */
QPushButton#destructiveButton {{
    color: {t.danger};
    border-color: {t.danger};
}}
QPushButton#destructiveButton:hover {{
    background: {danger_hover};
    border-color: {t.danger};
}}
QPushButton#destructiveButton:pressed {{
    background: {danger_pressed};
}}
QPushButton#destructiveButton:disabled {{
    color: {t.text_disabled};
    border-color: {t.border};
}}

QToolButton {{
    background: transparent;
    color: {t.text};
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
    padding: 3px 6px;
}}
QToolButton:hover {{
    background: {t.bg_hover};
}}
QToolButton:pressed {{
    background: {t.bg_pressed};
}}
QToolButton:checked {{
    background: {accent_soft};
    border-color: {t.accent};
}}
QToolButton:disabled {{
    color: {t.text_disabled};
}}

/* ------------------------------------------------------ text inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit {{
    background: {t.bg_surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 3px 6px;
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QDateEdit:hover {{
    border-color: {t.border_strong};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {{
    border-color: {t.accent};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled {{
    color: {t.text_disabled};
    background: {t.bg_window};
}}
/* Windows 11 / windowsvista used to render the spinbox inner line-edit
   with a hardcoded dark colour on dark palettes; pin it explicitly. */
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit {{
    background: transparent;
    border: none;
    color: {t.text};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {t.bg_hover};
}}

/* ---------------------------------------------------------- comboboxes */
QComboBox {{
    background: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 3px 8px;
}}
QComboBox:hover {{
    border-color: {t.border_strong};
}}
QComboBox:focus {{
    border-color: {t.accent};
}}
QComboBox:disabled {{
    color: {t.text_disabled};
    background: {t.bg_window};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border_strong};
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
    outline: none;
}}

/* ------------------------------------------------------------- menus */
QMenuBar {{
    background: {t.bg_window};
    color: {t.text};
}}
QMenuBar::item {{
    background: transparent;
    padding: 4px 10px;
    border-radius: {RADIUS_SM}px;
}}
QMenuBar::item:selected {{
    background: {t.bg_hover};
}}
QMenuBar::item:pressed {{
    background: {t.bg_pressed};
}}
/* No border-radius: an opaque top-level shows the window backing outside a
   QSS arc (the "square behind the rounded menu" artefact).  DWM rounds the
   real window instead — see theme.py::_PopupCornerWatcher. */
QMenu {{
    background: {t.bg_raised};
    color: {t.text};
    border: 1px solid {t.border_strong};
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 24px 5px 12px;
    border-radius: {RADIUS_SM}px;
}}
QMenu::item:selected {{
    background: {t.accent};
    color: {t.text_on_accent};
}}
QMenu::item:disabled {{
    color: {t.text_disabled};
    background: transparent;
}}
QMenu::separator {{
    height: 1px;
    background: {t.border};
    margin: 4px 8px;
}}

/* -------------------------------------------------------- separators */
/* A bare QFrame HLine/VLine is drawn by Qt from `WindowText`, i.e. the same
   weight as body text — a hard black rule across a popover.  Chrome rules
   take the hairline `border` token like every other divider; the frames set
   `NoFrame` so Qt's own bevel does not double up on this fill. */
QFrame#popoverSeparator {{
    background: {t.border};
    max-height: 1px;
}}
QFrame#toolbarSeparator {{
    background: {t.border};
    max-width: 1px;
}}

/* -------------------------------------------------------- scrollbars */
/* Track stays transparent; the THUMB carries the whole affordance, so it
   uses `text_muted` (a text-weight neutral) rather than the hairline
   `border_strong` — the 2px margin leaves only 8px of visible thumb and a
   border-weight fill was indistinguishable from the pane behind it.  Hover
   goes to `accent` so grabbing it reads as an interaction.  All four
   sub-controls are defined below (partial QScrollBar styling is forbidden). */
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.text_muted};
    border-radius: 4px;
    min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t.accent};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {t.text_muted};
    border-radius: 4px;
    min-width: 24px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t.accent};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* --------------------------------------------------------- splitters */
QSplitter::handle {{
    background: {t.bg_window};
}}
QSplitter::handle:hover {{
    background: {accent_soft};
}}

/* -------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {t.text_muted};
    padding: 6px 14px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {t.text};
}}
QTabBar::tab:selected {{
    color: {t.text};
    border-bottom: 2px solid {t.accent};
}}

/* --------------------------------------------------------- group box */
QGroupBox {{
    border: 1px solid {t.border};
    border-radius: {RADIUS}px;
    margin-top: 10px;
    padding-top: 6px;
    font-weight: bold;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {t.text};
}}

/* ------------------------------------------------------- panel header */
/* common/ui/widgets.py::PanelHeader — the 28px pane-header strip. Background
   stays transparent (the pane behind supplies its own surface colour); only
   the hairline bottom border is drawn, separating the header from the
   content below. */
QWidget#panelHeader {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {t.border};
}}

/* ----------------------------------------------------------- nav rail */
/* The left navigation rail (nav_rail.py::NavRail) sits one elevation BELOW
   the centre pane: bg_window (vs the centre's bg_surface) so it reads as a
   sunk surface, with a hairline right border separating it from the grid.
   Its section lists go transparent + borderless so they blend into that one
   sunk face rather than drawing their own bg_surface boxes on top of it. */
QWidget#navRail {{
    background: {t.bg_window};
    border: none;
    border-right: 1px solid {t.border};
}}
QListWidget#navRailList {{
    background: transparent;
    border: none;
}}
QListWidget#navRailList::item {{
    padding: 3px 8px;
}}

/* ------------------------------------------------- views and headers */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget, QTableWidget {{
    background: {t.bg_surface};
    alternate-background-color: {t.bg_raised};
    border: 1px solid {t.border};
    outline: none;
}}
QTableView {{
    gridline-color: {t.border};
}}
QHeaderView::section {{
    background: {t.bg_window};
    color: {t.text_muted};
    border: none;
    border-bottom: 1px solid {t.border};
    border-right: 1px solid {t.border};
    padding: 4px 8px;
}}

/* -------------------------------------------------------- status bar */
QStatusBar {{
    background: {t.bg_window};
    color: {t.text_muted};
    border-top: 1px solid {t.border};
}}
QStatusBar::item {{
    border: none;
}}

/* ------------------------------------------------------ progress bar */
QProgressBar {{
    background: {t.bg_surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    text-align: center;
    color: {t.text};
}}
/* The chunk is a *tinted* accent, not a solid one:
   QProgressBar draws its text (tokens.text) centred over the whole groove,
   so a solid accent fill leaves the label at 1.8–2.4:1 wherever the chunk
   has reached — permanently so at 100 %.  A 0.35 wash keeps the progress
   unmistakable while the label stays 5.7–7.4:1 on every built-in theme. */
QProgressBar::chunk {{
    background: {progress_chunk};
    border-radius: {RADIUS_SM - 1}px;
}}

/* ------------------------------------------------------------ slider */
QSlider::groove:horizontal {{
    height: 4px;
    background: {t.bg_pressed};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {t.accent};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: {t.text};
}}
QSlider::handle:horizontal:hover {{
    background: {t.accent_hover};
}}
/* サムネイルサイズのスライダだけは縦の当たり判定を広げる。
   素の ``QSlider:horizontal`` は
   ``sizeHint().height() == 15px`` しかなく、上下方向に掴める帯が細い
   （横方向は ``_slider.enable_click_jump`` がグルーブ全幅を当たりにする
   ので実効的な制約ではない）。無印セレクタを触るとメディアのシーク／音量
   バーや設定ダイアログのスライダまで背が伸びてコントロールバーの縦寸に
   波及するため、objectName で**この 1 種類に限定**する（付与は
   ``children_grid.ChildrenGrid._build_chrome``）。 */
QSlider#thumbSizeSlider:horizontal {{
    min-height: 24px;
}}
QSlider#thumbSizeSlider::handle:horizontal {{
    width: 18px;
    height: 18px;
    margin: -7px 0;
    border-radius: 9px;
}}

/* --------------------------------------------------- unified toolbar */
/* The window-level 40px bar (tokens.TOOLBAR_HEIGHT) hosting nav /
   breadcrumb / global search / view controls.  One strong bar: surface
   fill + a single hairline bottom border. */
QWidget#unifiedToolbar {{
    background: {t.bg_surface};
    border: none;
    border-bottom: 1px solid {t.border};
}}
/* Global search field: a bordered container so the mode chips and the
   options button read as part of ONE field; the inner QLineEdit drops its
   own box. */
QFrame#toolbarSearch {{
    background: {t.bg_surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS}px;
}}
QFrame#toolbarSearch QLineEdit {{
    border: none;
    background: transparent;
    padding: 1px 2px;
}}
/* Search-mode chips (名前 / 本文 / AIタグ) inside the field. */
QToolButton#searchModeChip {{
    font-size: {FONT_CAPTION_PT}pt;
    color: {t.text_muted};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 1px 6px;
}}
QToolButton#searchModeChip:hover {{
    background: {t.bg_hover};
    color: {t.text};
}}
QToolButton#searchModeChip:checked {{
    background: {accent_soft};
    color: {t.text};
    border-color: {t.accent};
}}
/* The generic `QToolButton:disabled` rule cannot reach a chip: the ID
   selector above wins on specificity, so a disabled chip kept its enabled
   colours and read as merely unselected. */
QToolButton#searchModeChip:disabled {{
    color: {t.text_disabled};
    border-color: {t.border};
}}
/* Small popovers dropped from toolbar buttons (表示 / 検索オプション).
   No border-radius (opaque Qt.Popup top-level — a QSS arc leaves the window
   backing showing outside it); DWM rounds the window — theme.py. */
QFrame#toolbarPopover {{
    background: {t.bg_raised};
    border: 1px solid {t.border_strong};
}}

/* -------------------------------------------------- condition chip bar */
/* The window-level 32px strip (tokens.CONDITION_BAR_HEIGHT) under the
   toolbar showing the APPLIED search conditions as removable chips.
   Same surface + hairline treatment as the toolbar so the two read as
   one continuous chrome block; the bar hides entirely when empty. */
QWidget#conditionBar {{
    background: {t.bg_surface};
    border: none;
    border-bottom: 1px solid {t.border};
}}
/* One applied-condition chip: accent-outlined pill; the label part is a
   flat QToolButton (click = edit popover), the × a small borderless drop
   button.  Accent marks "this is narrowing the grid right now". */
QFrame#conditionChip {{
    background: {rgba(t.accent, 0.10)};
    border: 1px solid {t.accent};
    border-radius: {RADIUS_SM}px;
}}
QFrame#conditionChip QToolButton {{
    border: none;
    background: transparent;
    padding: 0px 2px;
    color: {t.accent};
    font-size: {FONT_CAPTION_PT}pt;
}}
QFrame#conditionChip QToolButton:hover {{
    background: {t.bg_hover};
}}

/* ------------------------------------------------------- stage backdrop */
/* The centre preview in ステージモード shows the image on a dedicated
   backdrop (tokens.bg_stage) — deeper than any chrome surface so the image
   reads as *exhibited*, not pasted on a panel.  Applied to the ImageView
   scroll area (the surround behind the centred image) and its label; the
   label additionally carries a 1px hairline in `border` so the image's
   actual display rect is framed.  Document pages (markdown / PDF) and the
   browse grid keep their normal UI surfaces — only the picture pages get
   staged.
   The surround MUST be selected as `QAbstractScrollArea#id`: a
   QScrollArea's viewport is not painted by a
   `QWidget#<viewport-id> {{ background }}` rule even with
   WA_StyledBackground — the previous viewport-targeted rule silently did
   nothing and every theme showed bg_window behind the image.  Qt routes a
   background set on the scroll area itself to the viewport, which is
   exactly the surround we want. */
QAbstractScrollArea#stageImageArea {{
    background: {t.bg_stage};
}}
QLabel#stageImageLabel {{
    background: {t.bg_stage};
    border: 1px solid {t.border};
}}
""" + _arrow_rules(t) + _indicator_rules(t)


# --------------------------------------------------------------- fragments

#: Tool-/plugin-registered QSS generators appended after the base sheet.
_QSS_FRAGMENTS: list[Callable[[ThemeTokens], str]] = []


def _fragment_key(fn: Callable[[ThemeTokens], str]) -> tuple[str, str]:
    """登録済みフラグメントの同一性キー（モジュール + 修飾名）。

    プラグインの無効化 → 再有効化はホストの ``_purge_modules`` がモジュールを
    破棄して再 import するため、``import`` 副作用で登録するフラグメントは
    サイクルごとに**別の関数オブジェクト**として現れる。同一性を関数オブジェクト
    ではなく定義位置で見ることで、再登録を積み増しではなく差し替えにできる
    （レビュー 2026-07-31 #121）。
    """
    return (
        getattr(fn, "__module__", "") or "",
        getattr(fn, "__qualname__", "") or repr(fn),
    )


def register_qss_fragment(fn: Callable[[ThemeTokens], str]) -> None:
    """Register an app-wide QSS fragment generated from the theme tokens.

    ``apply_theme`` sets the application stylesheet with **one** ``build_qss``
    output — a tool or plugin that called ``app.setStyleSheet`` itself would
    lose its styles on the next theme switch.  Fragments registered here are
    appended to the base sheet on every (re)apply instead, so app-level
    styles survive theme switches without editing the shared layer.

    *fn* receives the active :class:`ThemeTokens` and returns a QSS string
    (derive every colour from the tokens — no literals; see
    docs/claude/design.md).  When a theme is already applied the stylesheet
    is refreshed immediately, so registration order vs ``apply_theme`` does
    not matter.

    **同じ定義位置の再登録は差し替え**（:func:`_fragment_key`）。プラグインの
    セッション内 無効化 → 再有効化では ``import`` 副作用の登録が毎サイクル
    走るため、無条件 append だとサイクル数ぶん同一 QSS が連結され、purge 済み
    モジュールの関数オブジェクトも残留し続ける（レビュー 2026-07-31 #121）。
    差し替えなら登録位置（＝適用順）も保たれる。
    """
    key = _fragment_key(fn)
    for i, existing in enumerate(_QSS_FRAGMENTS):
        if _fragment_key(existing) == key:
            _QSS_FRAGMENTS[i] = fn
            break
    else:
        _QSS_FRAGMENTS.append(fn)
    # Runtime import to avoid a cycle (theme.py imports us at module level).
    from .theme import reapply_qss

    reapply_qss()


def build_app_qss(t: ThemeTokens) -> str:
    """The full application stylesheet: base sheet + registered fragments.

    A fragment that raises is skipped with a warning — one broken plugin
    style must not take down the whole theme.
    """
    parts = [build_qss(t)]
    for fn in _QSS_FRAGMENTS:
        try:
            parts.append(fn(t))
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.warning("QSS fragment {!r} failed: {}", fn, exc)
    return "\n".join(parts)


# ------------------------------------------------------------ inline styles


def hint_style(*, font_pt: int | None = None) -> str:
    """Inline ``setStyleSheet`` string for auxiliary hint / caption labels.

    The design system's role table maps secondary labels and hints to
    ``text_muted`` — exposed to widgets as ``palette(mid)`` (the QPalette is
    generated from the same tokens), so the returned string tracks theme
    switches with no re-apply needed.  Both tools previously hand-wrote this
    (some with ``palette(placeholder-text)``, splitting one semantic role
    across two colours); every hint label goes through here now.

    ``font_pt`` optionally pins the label's point size (pass one of the
    ``FONT_*_PT`` tokens).
    """
    style = "color: palette(mid);"
    if font_pt is not None:
        style += f" font-size: {font_pt}pt;"
    return style


def chip_style(*, font_pt: int | None = None, radius: int = RADIUS_SM) -> str:
    """Inline ``setStyleSheet`` string for a small raised chip / badge.

    Built from :class:`QPalette` roles only (``light`` = ``bg_raised``,
    ``midlight`` = ``border``, ``windowtext`` = ``text``), so — exactly like
    :func:`hint_style` — the chip follows a theme switch on its own.  A chip
    whose colours are baked in as hex has to be rebuilt from the widget's
    ``changeEvent``, and that hand-rolled follow-up is what gets forgotten.

    ``font_pt`` optionally pins the point size (pass a ``FONT_*_PT`` token).
    """
    style = (
        "background: palette(light);"
        " border: 1px solid palette(midlight);"
        f" border-radius: {radius}px;"
        " color: palette(windowtext);"
    )
    if font_pt is not None:
        style += f" font-size: {font_pt}pt;"
    return style


def hairline_style(*, strong: bool = False) -> str:
    """Inline ``setStyleSheet`` string for a 1px divider drawn as a background.

    ``palette(midlight)`` is the hairline ``border`` token and
    ``palette(dark)`` the emphasised ``border_strong`` one, so the rule
    tracks theme switches.  A bare ``QFrame`` HLine/VLine would otherwise be
    drawn from ``WindowText`` — body-text weight, i.e. a hard rule.
    """
    role = "dark" if strong else "midlight"
    return f"background: palette({role}); border: none;"
