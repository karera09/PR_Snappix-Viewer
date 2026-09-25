"""Binds :mod:`file_picker`'s wording to the snappix message catalog.

``file_picker.py`` is kept free of snappix imports (a verbatim copy of it
ships with the stand-alone tag scanner), so its texts arrive through
:func:`file_picker.install_text_source`.  This module holds the key map and
installs it; ``common/ui/__init__`` calls :func:`install` on import, so every
entry point that reaches the picker through this package gets catalog text.

Keys that name a wording the catalog already has (「キャンセル」「開く」…) point
at that shared key instead of minting a duplicate.
"""

from __future__ import annotations

from ..i18n import t
from . import file_picker

#: picker key → catalog key.  Must cover :data:`file_picker.TEXT_KEYS` exactly.
CATALOG_KEYS: dict[str, str] = {
    "address": "common.file_picker.address",
    "address_placeholder": "common.file_picker.address_placeholder",
    "cancel": "common.action.cancel",
    "choose": "common.action.choose",
    "column_modified": "common.label.modified",
    "column_name": "common.label.name",
    "column_size": "common.label.size",
    "file_name": "common.file_picker.file_name",
    "file_type": "common.file_picker.file_type",
    "folder": "common.file_picker.folder",
    "mkdir_failed": "common.file_picker.mkdir_failed",
    "new_folder": "common.file_picker.new_folder",
    "not_found": "common.file_picker.not_found",
    "open": "common.action.open",
    "overwrite_accept": "common.file_picker.overwrite_accept",
    "overwrite_body": "common.file_picker.overwrite_body",
    "overwrite_title": "common.file_picker.overwrite_title",
    "save": "common.file_picker.save",
    "up": "common.file_picker.up",
}


def catalog_text(key: str) -> str:
    """The catalog template behind picker *key* (unformatted)."""
    catalog_key = CATALOG_KEYS.get(key)
    return t(catalog_key) if catalog_key else key


def install() -> None:
    file_picker.install_text_source(catalog_text)


__all__ = ["CATALOG_KEYS", "catalog_text", "install"]
