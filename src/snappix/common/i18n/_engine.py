"""Message-catalog engine — the key-based i18n core for the snappix GUIs.

Why a key-based catalog (and not Qt ``tr()`` / ``.ts`` or gettext)
------------------------------------------------------------------
Every user-facing display string is addressed by a **stable dotted key**
(``"viewer.shortcuts_dialog.col_operation"``) that resolves to a format
template in the active locale's catalog.  This is the widely-adopted "message
catalog / string table" pattern (i18next, Android ``strings.xml``, VS Code
``nls``).

It was chosen over Qt's ``tr()``/``.ts``/``.qm`` and Python ``gettext`` for
three reasons that matter to *this* codebase:

* **Consistency (表記揺れ防止).**  A key is the single source of truth for one
  string, so the *same* wording is shared by every call site through *one*
  catalog entry.  A later wording change / unification is a one-line edit to
  the catalog value — never a sweep across the source tree.  ``tr()``/gettext
  key the translation on the *source text*, so two near-identical Japanese
  strings become two entries and drift silently.
* **Works everywhere.**  ``t(key)`` is a plain function call with no ``QObject``
  / ``QCoreApplication.translate`` context requirement, so non-GUI layers
  (``common``, the viewer's worker/scan code) use the exact same mechanism as
  the widgets.
* **Portability.**  Catalogs are plain Python modules (see ``locales/``), so
  there is no ``.qm``/``.mo`` compile step and nothing to bundle through
  PyInstaller ``datas`` / resolve at runtime — a hard requirement here.

Runtime contract
----------------
* ``t(key, **params)`` looks the key up in the current locale, then falls back
  to the default locale (:data:`DEFAULT_LOCALE`, ``"ja"``), then to the key
  string itself.  A missing key or a failed ``str.format`` is logged **once**
  and degrades to a safe value — :func:`t` never raises, so a catalog gap can
  never crash the UI.
* Templates use :meth:`str.format` placeholders (``{name}`` / ``{n:02d}``).
  A literal brace in a template must be doubled (``{{`` / ``}}``) as usual.
* :func:`set_locale` is called once at startup (before any window is built).
  There is no live re-translation; a language change takes effect on the next
  launch, mirroring ``shared_prefs``'s theme semantics.
"""

from __future__ import annotations

from loguru import logger

#: The authored / fallback locale.  Every string exists here; other locales
#: may be partial and fall back to this one key-by-key.
DEFAULT_LOCALE = "ja"

#: locale -> {key: template}.  Populated by :func:`register_catalog` (called
#: from ``locales/__init__.py`` at import time).
_CATALOGS: dict[str, dict[str, str]] = {}

#: The active locale.  Starts at the default so :func:`t` works even before
#: :func:`set_locale` runs (e.g. module-level constants evaluated at import).
_current_locale: str = DEFAULT_LOCALE

#: Keys whose lookup/format already failed once — so we warn a single time
#: per key instead of flooding the log on every repaint.
_warned: set[str] = set()


def register_catalog(locale: str, messages: dict[str, str]) -> None:
    """Merge *messages* into *locale*'s catalog (idempotent per key).

    Called at import time by the locale packages and by plugins registering
    their own catalog fragment.  Merging (rather than replacing) lets a locale
    be assembled from several domain fragments.

    Re-registering a key with the *same* value is a harmless idempotent
    re-import (e.g. a plugin module imported twice).  Re-registering it with a
    *different* value is a collision — the last writer silently wins, which is
    how a plugin could clobber a core string or another plugin's key — so warn
    once per clobbered key to surface it.  The write still goes through (the
    merge semantics are unchanged); this only makes the overwrite visible.
    """
    existing = _CATALOGS.setdefault(locale, {})
    for key, value in messages.items():
        prior = existing.get(key)
        if prior is not None and prior != value:
            logger.warning(
                "i18n catalog[{}]: key {!r} overwritten "
                "({!r} -> {!r}); plugin keys must be namespaced by plugin id "
                "to avoid clobbering core / other-plugin strings",
                locale, key, prior, value,
            )
        existing[key] = value


def set_locale(locale: str) -> None:
    """Switch the active locale.  Unknown locales fall back to the default.

    Call once at startup, *before* any widgets are constructed.  An unknown
    or unregistered locale silently uses :data:`DEFAULT_LOCALE` so a junk
    persisted value can never blank the UI.
    """
    global _current_locale
    _current_locale = locale if locale in _CATALOGS else DEFAULT_LOCALE


def get_locale() -> str:
    """Return the active locale code."""
    return _current_locale


def available_locales() -> list[str]:
    """Return the sorted list of registered locale codes."""
    return sorted(_CATALOGS)


def catalog(locale: str | None = None) -> dict[str, str]:
    """Return the (read-only intent) message dict for *locale*.

    ``None`` → the active locale.  Used by the i18n test-suite to check key
    existence / duplicate values / locale parity.
    """
    return _CATALOGS.get(locale or _current_locale, {})


def _lookup(key: str) -> str | None:
    """Return the template for *key*: active locale, else default, else None."""
    active = _CATALOGS.get(_current_locale)
    if active is not None and key in active:
        return active[key]
    if _current_locale != DEFAULT_LOCALE:
        base = _CATALOGS.get(DEFAULT_LOCALE)
        if base is not None and key in base:
            return base[key]
    return None


def t(key: str, /, **params: object) -> str:
    """Translate *key* in the active locale, formatting with *params*.

    Fallback chain: active locale → default locale → the key string itself.
    ``str.format(**params)`` fills placeholders.  Any failure (missing key or
    bad/absent placeholder) is logged once and degrades safely — this
    function never raises, so the UI is robust to catalog gaps.
    """
    template = _lookup(key)
    if template is None:
        if key not in _warned:
            _warned.add(key)
            logger.warning("i18n: missing message key {!r}", key)
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    # ``TypeError`` / ``AttributeError`` belong here too: a format spec that
    # the value does not support (``{n:,}`` given ``None``) raises TypeError,
    # and an attribute access in a placeholder (``{obj.name}``) raises
    # AttributeError — both reach ``t`` from ordinary call sites that pass a
    # not-yet-known value.  The tuple stays an explicit enumeration rather than
    # bare ``Exception``: the contract "t never raises" must not become
    # "t swallows anything", which would hide real bugs in the caller.
    except (
        KeyError, IndexError, ValueError, TypeError, AttributeError,
    ) as exc:
        if key not in _warned:
            _warned.add(key)
            logger.warning(
                "i18n: format failed for key {!r} ({}); returning raw template",
                key, exc,
            )
        return template
