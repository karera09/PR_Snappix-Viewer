"""Shared UI design system for the snappix GUI tools (viewer and plugin windows).

Single source of truth for colours, typography and widget styling: callers
take colours and font sizes from these tokens instead of hard-coding them.

Public API:

- :func:`apply_theme` — apply a named theme ("system" / "light" / "dark")
  to the running ``QApplication`` (Fusion style + QPalette + app-wide QSS
  + Windows dark title bar).
- :func:`current_tokens` — the :class:`ThemeTokens` currently in effect,
  for custom-painted widgets that need concrete colours.
- :data:`DARK_TOKENS` / :data:`LIGHT_TOKENS` / :data:`STANDARD_TOKENS` —
  the three built-in main palettes; :data:`EXTRA_THEME_TOKENS` — the extra
  built-in themes listed under 表示 ▸ テーマ ▸ その他.
- :func:`localize_buttons` — Japanese-label a ``QDialogButtonBox``'s standard
  buttons.
- :func:`localize_input_dialog` — same for a ``QInputDialog``'s OK / Cancel.
- :func:`confirm_action` — verb-labelled confirmation modal (「完全に削除する」
  rather than 「はい」) for irreversible / consequential answers.
- :func:`warn_modal` — its acknowledge-only sibling: the 「失敗 = モーダル」
  notice, plain-text by default because its body embeds untrusted strings.
- :func:`demote_close_default` — strip the implicit accent/default styling
  from a terminal-only button box (view-only dialogs whose only action is
  「閉じる」).
- :func:`frame_intersects_any_screen` / :func:`center_on_primary` — the
  saved-geometry restore guard (判定 + 復帰導線) shared by every snappix
  main window; see ``window_geometry.py``.

Extension points (tools / future plugins add to the design system without
editing it):

- :func:`register_theme` — add a named :class:`ThemeTokens` set.
- :func:`register_qss_fragment` — append token-derived app-wide QSS that
  survives theme switches.
- :func:`register_icons` — add SVG glyphs to the themed icon set.
- :func:`hint_style` / :func:`chip_style` / :func:`hairline_style` — the
  shared inline styles, written with ``palette(...)`` roles so they follow a
  theme switch without the widget re-applying them.
- :func:`show_toast` — non-modal corner notification (success / status
  feedback; see the design policy's modal-vs-non-modal principle).
- ``file_picker`` — the file / folder picker (``pick_directory`` /
  ``pick_open_file`` / ``pick_save_file``); ``QFileDialog`` is not used
  anywhere because it writes the per-user ``QtProject`` settings store.
- :class:`OverlayCapsule` / :class:`OverlayPill` — bases for chrome drawn on
  top of image content (fixed overlay palette + styled background + parent
  clamp + auto-hide); colours live in ``overlay.py``.
"""

from .tokens import (
    CONDITION_BAR_HEIGHT,
    DARK_TOKENS,
    EXTRA_THEME_TOKENS,
    FILMSTRIP_HEIGHT,
    LIGHT_TOKENS,
    STANDARD_TOKENS,
    FONT_TITLE_PT,
    FONT_SUBTITLE_PT,
    FONT_BODY_PT,
    FONT_CAPTION_PT,
    PANEL_HEADER_HEIGHT,
    RADIUS,
    RADIUS_SM,
    TOOLBAR_HEIGHT,
    ThemeTokens,
    rgba,
)
from .buttons import (
    confirm_action,
    demote_all_defaults,
    demote_close_default,
    localize_buttons,
    localize_input_dialog,
    warn_modal,
)
from .elided_label import ElidedLabel
from .focus_band import FocusBandMixin, FocusBandTarget, PaneFocusBands
from .icons import (
    fixed_icon,
    fixed_pixmap,
    icon,
    register_icons,
    set_icon,
    set_icon_pixmap,
    svg_source,
)
from .notifications import (
    NotificationCenter,
    NotificationRecord,
    notification_center_for,
)
from .overlay_chrome import OverlayCapsule, OverlayPill
from .qss import (
    caption_size_style,
    chip_style,
    hairline_style,
    hint_style,
    register_qss_fragment,
)
from .popover import popover_position
from .theme import apply_theme, current_tokens, register_theme, resolve_tokens
from .toast import Toast, show_toast
from .widgets import (
    align_form_labels,
    align_header,
    empty_state_stack,
    EmptyStateCard,
    indent_to_form_column,
    PanelHeader,
)
from .window_geometry import (
    center_on_primary,
    frame_intersects_any_screen,
)
from . import file_picker_catalog as _file_picker_catalog

# The picker module carries no catalog of its own (see file_picker_catalog).
_file_picker_catalog.install()

__all__ = [
    "fixed_icon",
    "fixed_pixmap",
    "icon",
    "register_icons",
    "set_icon",
    "set_icon_pixmap",
    "svg_source",
    "ThemeTokens",
    "DARK_TOKENS",
    "LIGHT_TOKENS",
    "STANDARD_TOKENS",
    "EXTRA_THEME_TOKENS",
    "FONT_TITLE_PT",
    "FONT_SUBTITLE_PT",
    "FONT_BODY_PT",
    "FONT_CAPTION_PT",
    "CONDITION_BAR_HEIGHT",
    "FILMSTRIP_HEIGHT",
    "PANEL_HEADER_HEIGHT",
    "RADIUS",
    "RADIUS_SM",
    "TOOLBAR_HEIGHT",
    "rgba",
    "align_form_labels",
    "align_header",
    "apply_theme",
    "ElidedLabel",
    "center_on_primary",
    "confirm_action",
    "current_tokens",
    "demote_all_defaults",
    "demote_close_default",
    "empty_state_stack",
    "EmptyStateCard",
    "chip_style",
    "frame_intersects_any_screen",
    "hairline_style",
    "OverlayCapsule",
    "OverlayPill",
    "PanelHeader",
    "hint_style",
    "caption_size_style",
    "indent_to_form_column",
    "localize_buttons",
    "localize_input_dialog",
    "popover_position",
    "register_qss_fragment",
    "register_theme",
    "resolve_tokens",
    "show_toast",
    "warn_modal",
    "Toast",
    "NotificationCenter",
    "NotificationRecord",
    "notification_center_for",
    "FocusBandMixin",
    "FocusBandTarget",
    "PaneFocusBands",
]
