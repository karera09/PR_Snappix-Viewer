"""Locale registry — imports every locale package and registers its catalog.

Adding a language is a two-step, call-site-free change:
  1. create ``locales/<code>/`` exposing a ``MESSAGES: dict[str, str]``
     (mirroring ``ja/``), and
  2. register it in :func:`register_all_locales` below.
Keys absent from a non-default locale fall back to ``ja`` at lookup time
(see ``_engine._lookup``), so a new locale may start partial.
"""

from __future__ import annotations

from .._engine import register_catalog


def register_all_locales() -> None:
    """Register every bundled locale catalog with the engine."""
    from . import ja

    register_catalog("ja", ja.MESSAGES)
    # Future: from . import en; register_catalog("en", en.MESSAGES)
