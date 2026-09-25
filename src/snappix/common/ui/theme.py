"""Apply a theme to the running application.

``apply_theme("system" | "light" | "dark")`` is the one entry point every
tool calls (the viewer and any plugin window).  It sets:

- the Fusion style (native styles ignore palette overrides on Windows),
- a ``QPalette`` generated from the theme's tokens,
- the app-wide QSS generated from the same tokens,
- Windows title bars to the matching dark/light mode — for every window
  currently open *and*, via an application event filter, every window
  shown later (dialogs included).

"system" resolves to the light or dark token set by asking the OS colour
scheme (Qt 6.5+), so the app always renders with our own consistent
palette instead of falling back to the unstyled platform look.
"""

from __future__ import annotations

from loguru import logger
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from .palette import build_palette
from .qss import build_app_qss
from .tokens import (
    DARK_TOKENS,
    EXTRA_THEME_TOKENS,
    LIGHT_TOKENS,
    STANDARD_TOKENS,
    ThemeTokens,
)
from .wheelguard import install_wheel_value_guard
from .win_titlebar import apply_titlebar_theme, round_window_corners

_current_tokens: ThemeTokens | None = None
_current_theme: str = "system"
_watcher: "_TitlebarWatcher | None" = None
_popup_watcher: "_PopupCornerWatcher | None" = None
#: The application the two filters above (and the colour-scheme connection)
#: are attached to.  "Has an instance ever been made?" is the wrong question:
#: a process that destroys and recreates its QApplication would keep the old
#: filters — attached to a dead app — and silently lose titlebar tinting,
#: popup corner rounding and the colour-scheme follow.
_filters_app: "QApplication | None" = None
#: A coalesced :func:`reapply_qss` is queued (see that function).
_reapply_pending = False

#: Named theme registry.  "system" is not an entry — it is resolved to one
#: of the registered names by asking the OS colour scheme (see
#: :func:`resolve_tokens`; the OS pair stays pinned to the built-in
#: dark / light, never to "standard" or the extra themes).  Tools / plugins
#: add themes via :func:`register_theme` instead of editing this module.
_THEMES: dict[str, ThemeTokens] = {
    DARK_TOKENS.name: DARK_TOKENS,
    LIGHT_TOKENS.name: LIGHT_TOKENS,
    STANDARD_TOKENS.name: STANDARD_TOKENS,
    **{tokens.name: tokens for tokens in EXTRA_THEME_TOKENS},
}


def register_theme(tokens: ThemeTokens) -> None:
    """Register a theme under ``tokens.name`` for :func:`resolve_tokens`.

    Lets a tool or plugin ship its own :class:`ThemeTokens` without editing
    the shared layer: register it, then pass the name to ``apply_theme``
    (and wire a menu entry).  Re-registering an existing name with
    **different** tokens — including the built-in "dark" / "light" — raises
    ``ValueError`` so a plugin can't silently repaint the built-ins.

    **Re-registering identical tokens is a no-op**（frozen dataclass の
    同値比較。``qss.py::register_qss_fragment`` と同型）:
    プラグインのセッション内 無効化 → 再有効化は activate 再実行になる
    ため、無条件拒否だと自分が前サイクルで登録したテーマと衝突して
    再有効化が恒久失敗する。
    """
    # 予約名の検査を「登録済み」検査より先に行う: "system" は登録の有無に
    # かかわらず常に予約名として拒否する（レジストリへ "system" エントリを
    # 差し込む構成 — 例: テストスイートの dark 固定 — でも診断が変わらない）。
    if tokens.name == "system":
        raise ValueError('"system" is reserved for OS colour-scheme resolution')
    existing = _THEMES.get(tokens.name)
    if existing is not None:
        if existing == tokens:
            return
        raise ValueError(f"theme {tokens.name!r} is already registered")
    _THEMES[tokens.name] = tokens


class _TitlebarWatcher(QObject):
    """App-wide event filter: dark title bar for every window ever shown.

    Windows created after ``apply_theme`` ran (dialogs, secondary windows)
    would otherwise keep the OS-default light title bar.  A Show-event
    hook is the cheapest reliable moment: the native handle exists and the
    DWM flag applies instantly.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dark = False

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if (
            event.type() == QEvent.Show
            and isinstance(obj, QWidget)
            and obj.isWindow()
        ):
            apply_titlebar_theme(obj, self.dark)
        return False


class _PopupCornerWatcher(QObject):
    """App-wide event filter: give transient popup windows soft corners.

    Menus (``QMenu`` — menu-bar drop-downs, context menus), combo-box
    drop-downs, the toolbar popovers (frameless ``Qt.Popup`` ``QFrame``\\ s) and
    tooltips are all **opaque** top-level windows.  A QSS ``border-radius`` on
    an opaque top-level leaves the pixels outside the arc showing the window
    backing — the artefact the product owner reports as "a square backing
    sticking out behind the rounded panel" — and true translucency
    (``WA_TranslucentBackground``) does not composite reliably for these
    short-lived windows (lost drop shadow / black corners).  Instead we let
    **DWM round the actual window** at composition time
    (``round_window_corners``): the OS clips the corners, so an opaque window
    gets clean rounded corners with nothing showing through outside the arc.

    The DWM call needs the native handle, so it runs on ``Show`` (handle
    present).  Scoped to windows whose type is ``Qt.Popup`` (menus / combo
    pop-ups / toolbar popovers) or ``Qt.ToolTip`` (tooltips) so ordinary
    windows and dialogs are never touched; it is a no-op on Windows 10 /
    non-Windows, where the corners stay square (never black).

    The same Show hook also turns on ``QMenu.toolTipsVisible`` for every
    menu.  Its default is ``False``, so an action tooltip (an ancestor
    folder's full path, a verb's purpose) would silently never appear unless
    each menu-building site remembered to opt in — and which menu an action
    lands in is data flow no static check can close.  Setting it here makes
    the invariant hold by construction for every menu however it is built
    (menu-bar drop-downs, context menus, sub-menus, plugin menus).  It is
    noise-free: a menu only shows tooltips that were set explicitly (an
    action without ``setToolTip`` shows none), and Show always precedes the
    first ToolTip event, so there is no timing window.
    """

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Show or not isinstance(obj, QWidget):
            return False
        if isinstance(obj, QMenu) and not obj.toolTipsVisible():
            obj.setToolTipsVisible(True)
        if obj.isWindow() and obj.windowType() in (
            Qt.WindowType.Popup,
            Qt.WindowType.ToolTip,
        ):
            round_window_corners(obj)
        return False


def resolve_tokens(theme: str) -> ThemeTokens:
    """Map a theme name to concrete tokens ("system" asks the OS).

    Named themes come from the registry (built-ins + :func:`register_theme`).
    "system" — and, defensively, any unknown name (e.g. a stale config value
    for a theme whose plugin is gone) — resolves to the built-in dark or
    light set from the OS colour scheme.
    """
    tokens = _THEMES.get(theme)
    if tokens is not None:
        return tokens
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return DARK_TOKENS
    except Exception:  # pragma: no cover - no QGuiApplication yet
        pass
    return LIGHT_TOKENS


def current_tokens() -> ThemeTokens:
    """Tokens currently in effect (for custom-painted widgets).

    Falls back to resolving "system" when ``apply_theme`` has not run —
    e.g. widgets instantiated standalone in tests.
    """
    if _current_tokens is not None:
        return _current_tokens
    return resolve_tokens("system")


def reapply_qss() -> None:
    """Schedule a regenerate + reinstall of the app stylesheet.

    Called by ``qss.register_qss_fragment`` so a fragment registered *after*
    ``apply_theme`` ran still takes effect.  No-op before a QApplication
    exists or before the first ``apply_theme`` (the fragment is simply
    included when the theme is eventually applied).

    The work lands at the end of the current event-loop turn, and a burst of
    registrations collapses into **one** re-apply.  ``setStyleSheet`` forces
    Qt to re-polish every widget in the application — O(all widgets), tens of
    milliseconds on a real window — so a plugin whose modules register three
    or four fragments as an import side effect used to pay that cost once per
    fragment.  Anything registered within the same turn is still in the sheet
    when it is built, so the "registration order does not matter" contract
    holds.
    """
    global _reapply_pending
    app = QApplication.instance()
    if app is None or _current_tokens is None:
        return
    if _reapply_pending:
        return
    _reapply_pending = True
    QTimer.singleShot(0, _flush_reapply_qss)


def _flush_reapply_qss() -> None:
    """Install the coalesced stylesheet (see :func:`reapply_qss`)."""
    global _reapply_pending
    _reapply_pending = False
    app = QApplication.instance()
    if app is None or _current_tokens is None:
        return
    qss = build_app_qss(_current_tokens)
    # ``apply_theme`` may have run in between and already installed it.
    if app.styleSheet() != qss:
        app.setStyleSheet(qss)


def _on_color_scheme_changed(_scheme) -> None:
    """Re-apply the current theme when the OS colour scheme flips.

    Only meaningful while the active theme is "system": named light/dark
    themes are pinned and must not follow the OS.  We re-run ``apply_theme``
    with the remembered name so palette / QSS / title bars all refresh.
    """
    if _current_theme == "system":
        apply_theme("system")


def _install_app_filters(app) -> None:
    """Attach the application-wide filters to *app*, once per application.

    Three of them: the title-bar watcher (tints native frames as windows
    show), the popup corner watcher (DWM-rounds opaque popups) and the
    wheel-value guard (stops the wheel from silently editing combo / spin
    values).  The colour-scheme connection rides along because
    ``styleHints()`` belongs to the application too.

    Keyed on *which* application, not on "does an instance exist": a process
    that recreates its QApplication would otherwise keep filters bound to the
    destroyed one and lose all three behaviours with no error.  The watcher
    objects themselves are reused — they are parentless and carry no
    per-application state.
    """
    global _watcher, _popup_watcher, _filters_app, _reapply_pending
    if _filters_app is app:
        return
    # A queued re-apply belongs to the previous application and will never
    # run; clearing the flag keeps later registrations from being swallowed.
    _reapply_pending = False
    if _watcher is None:
        _watcher = _TitlebarWatcher()
    app.installEventFilter(_watcher)
    # Round the (opaque) popup / tooltip windows' corners via DWM on show, so
    # menus / combo drop-downs / toolbar popovers / tooltips get soft corners
    # without the black-corner artefact of a QSS border-radius (see the class).
    # The same watcher makes every QMenu show its actions' tooltips.
    if _popup_watcher is None:
        _popup_watcher = _PopupCornerWatcher()
    app.installEventFilter(_popup_watcher)
    # Not a theme concern, but ``apply_theme`` is the one hook both tools run
    # right after the QApplication exists: neutralise mouse-wheel value edits
    # on combo / spin boxes app-wide.
    install_wheel_value_guard(app)
    # Follow live OS dark/light toggles for the "system" theme.  The handler
    # re-checks the active theme, so connecting it is all that is needed.
    try:
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            _on_color_scheme_changed
        )
    except Exception:  # pragma: no cover - older Qt without the signal
        pass
    _filters_app = app


def apply_theme(theme: str) -> None:
    """Apply *theme* ("system" / "light" / "dark") to the application.

    Must be called **after** the ``QApplication`` exists; when it doesn't yet
    a warning is logged and the call is a no-op (nothing to style, and
    ``current_tokens()`` would keep falling back to "system" resolution —
    logging makes that mis-ordering visible instead of silent).

    When *theme* is "system" the OS colour-scheme change signal is wired once
    so a runtime dark/light switch in Windows is followed live.
    """
    global _current_tokens, _current_theme
    app = QApplication.instance()
    if app is None:
        logger.warning(
            "apply_theme({!r}) called before a QApplication exists — no-op. "
            "Call it after constructing QApplication.",
            theme,
        )
        return
    _current_theme = theme
    tokens = resolve_tokens(theme)
    _current_tokens = tokens

    # setStyle / setPalette / setStyleSheet each force Qt to re-polish every
    # widget in the application — O(all widgets), and the dominant cost of a
    # re-apply.  ViewerWindow.__init__ re-runs apply_theme with the theme that
    # is (almost always) already in effect, so skip whichever of the three is
    # already at its target state; the end result is pixel-identical.  The
    # comparisons read the *live* app state (not a module-level cache) so a
    # caller that reset the style/QSS behind our back is still repaired.
    style = app.style()
    style_name = style.objectName().lower()
    if style.metaObject().className() == "QStyleSheetStyle":
        # Once an app-wide stylesheet is installed Qt wraps the real style in
        # a QStyleSheetStyle proxy whose objectName is empty (and PySide does
        # not expose baseStyle()).  The only code that installs the app QSS is
        # this module, and it always pairs it with Fusion — so the proxy's
        # presence means Fusion is already underneath.
        style_name = "fusion"
    if style_name != "fusion":
        app.setStyle("Fusion")
    palette = build_palette(tokens)
    if app.palette() != palette:
        app.setPalette(palette)
    qss = build_app_qss(tokens)
    if app.styleSheet() != qss:
        app.setStyleSheet(qss)

    # Re-render registered themed icons (see icons.set_icon) in the new
    # palette.  Runtime import: icons.py resolves colours through us.
    from .icons import retint_all

    retint_all()

    # Tooltips appear instantly: the show *delay* is unchanged, but Windows'
    # "fade tooltips into view" animation makes the tip paint in gradually —
    # disable it so the tip is drawn in one shot.  Global app state; setting it
    # on every (re)apply is idempotent.
    app.setEffectEnabled(Qt.UI_FadeTooltip, False)
    app.setEffectEnabled(Qt.UI_AnimateTooltip, False)

    _install_app_filters(app)
    if _watcher is not None:
        _watcher.dark = tokens.is_dark
    # Retint windows that are already open (theme switched at runtime).
    for w in app.topLevelWidgets():
        if w.isVisible():
            apply_titlebar_theme(w, tokens.is_dark)
