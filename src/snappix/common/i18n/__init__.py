"""Centralised UI-string catalog for the snappix viewer GUI.

Single source of truth for user-facing display text.  Import :func:`t` and
address every label / button / message by a stable key; the wording lives in
``locales/<code>/`` (default ``ja``).  See :mod:`._engine` for the rationale
(why a key-based catalog over Qt ``tr()`` / gettext) and the runtime contract.

Typical use::

    from ..common.i18n import t          # depth varies by caller location
    button.setText(t("common.action.close"))
    label.setText(t("viewer.post_grid.banner_count", n=42))

Startup wiring (done once in each ``app.py``, before any window is built)::

    from ..common.i18n import set_locale
    from ..common.shared_prefs import resolve_startup_language
    set_locale(resolve_startup_language(config.language))

Public API:

- :func:`t` — translate a key (with optional ``str.format`` params).
- :func:`set_locale` / :func:`get_locale` / :func:`available_locales`.
- :data:`DEFAULT_LOCALE` — the authored / fallback locale (``"ja"``).
"""

from __future__ import annotations

from ._engine import (
    DEFAULT_LOCALE,
    available_locales,
    catalog,
    get_locale,
    register_catalog,
    set_locale,
    t,
)
from .locales import register_all_locales

# Load and register every bundled catalog at import time so ``t`` works
# immediately (the default locale is active until ``set_locale`` runs).
register_all_locales()

__all__ = [
    "t",
    "set_locale",
    "get_locale",
    "available_locales",
    "catalog",
    "register_catalog",
    "DEFAULT_LOCALE",
]
