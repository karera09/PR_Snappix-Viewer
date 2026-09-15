"""Build a ``QPalette`` from :class:`ThemeTokens`.

The palette matters even with an app-wide QSS: custom-painted widgets read
``palette(...)`` roles (``mid`` for muted text, ``highlight`` for selection)
and Fusion falls back to it for anything the stylesheet doesn't cover
(check/radio indicators, item views with custom delegates, …).  Keeping the
palette and the QSS generated from the *same* tokens is what keeps the two
worlds in sync.

The palette is also the only way a QSS string can name a theme colour
*dynamically*: ``palette(mid)`` is re-resolved when the palette changes, so a
style built from roles follows a theme switch with no re-apply, while a style
built from literal hex values has to be rebuilt by hand from every widget's
``changeEvent``.  Every token a shared inline style needs therefore gets a
role here — otherwise each new surface has no choice but to bake its colours
in and hand-roll that ``changeEvent``.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from .tokens import ThemeTokens


def build_palette(tokens: ThemeTokens) -> QPalette:
    p = QPalette()
    window = QColor(tokens.bg_window)
    base = QColor(tokens.bg_surface)
    raised = QColor(tokens.bg_raised)
    text = QColor(tokens.text)
    disabled = QColor(tokens.text_disabled)
    accent = QColor(tokens.accent)

    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, raised)
    p.setColor(QPalette.ToolTipBase, raised)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, window)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(tokens.danger))
    p.setColor(QPalette.Link, QColor(tokens.link))
    p.setColor(QPalette.Highlight, accent)
    p.setColor(QPalette.HighlightedText, QColor(tokens.text_on_accent))
    p.setColor(QPalette.PlaceholderText, disabled)
    # Subdued secondary text / thin borders throughout the viewer use
    # ``palette(mid)``.  Fusion would otherwise derive Mid from the Button
    # colour, producing a near-invisible grey — pin it to the token.
    p.setColor(QPalette.Mid, QColor(tokens.text_muted))
    # The remaining shading roles carry the surface / line tokens that inline
    # styles need.  Fusion derives them from Button when unset, which is both
    # wrong for our surfaces and unusable from QSS; the app-wide sheet already
    # draws every frame, group box and separator itself, so nothing is left
    # relying on the derived 3D bevels.  Mapping (role <- token):
    #   Midlight <- border        (hairline dividers)
    #   Dark     <- border_strong (emphasised outlines)
    #   Light    <- bg_raised     (cards / chips sitting above the surface)
    #   Shadow   <- bg_hover      (hover wash)
    p.setColor(QPalette.Midlight, QColor(tokens.border))
    p.setColor(QPalette.Dark, QColor(tokens.border_strong))
    p.setColor(QPalette.Light, QColor(tokens.bg_raised))
    p.setColor(QPalette.Shadow, QColor(tokens.bg_hover))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    return p
