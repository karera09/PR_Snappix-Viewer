"""Shared Qt dialog helpers for the viewer.

Every file / folder picker in the viewer goes through this module, and this
module goes through :mod:`snappix.common.ui.file_picker` — never
``QFileDialog``.  That class saves its state into the per-user ``QtProject``
settings store (the registry on Windows) whenever a dialog is destroyed,
which a portable application must not do implicitly; the reasoning is in the
picker module's docstring.

The picker keeps what the viewer needs from a folder dialog: listing runs
through ``QFileSystemModel``'s own worker thread, so browsing into a huge
folder or a sleeping network share does not freeze the GUI the way the
native shell dialog's enumeration once did, and the side panel offers the
host window's places (registered library roots and bookmarks) before the
drives, so jumping to another library is one click.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtWidgets import QDialog, QInputDialog, QWidget

from ..common.ui import localize_input_dialog
from ..common.ui import file_picker


def host_picker_places(widget: QWidget | None) -> list[Path] | None:
    """The places *widget*'s host window offers the folder picker.

    The list belongs to the host window (registered library roots + bookmarks
    — ``ViewerWindow.picker_places``); panes and child dialogs ask for it
    through this one helper so the side panel is identical from every entry
    point.  ``None`` when there is no such host (a stand-alone widget in a
    test); the picker then lists only the drives.

    **Walks the ancestors**, not ``window()``: ``QWidget.window()`` answers
    "the nearest ancestor that can carry a window frame", which from inside a
    ``QDialog`` is the dialog itself — it has no ``picker_places``, so every
    picker opened from a dialog would fall back to the bare panel.  Same shape
    as :func:`~snappix.viewer.context_menus.curation_hooks_from_ancestors`,
    which solves this dialog boundary for the curation hooks.
    """
    w = widget
    while w is not None:
        getter = getattr(w, "picker_places", None)
        if callable(getter):
            return list(getter())
        w = w.parentWidget()
    return None


def pick_existing_directory(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    *,
    sidebar: Iterable[Path] | None = None,
) -> str:
    """Folder picker.  Returns the chosen directory, or ``""`` when cancelled.

    *sidebar* names the places to offer in the side panel (registered library
    roots, bookmarks …); the machine's drives follow them.  Passing nothing
    derives them from *parent* (:func:`host_picker_places`), so a new entry
    point gets the same panel as every other one without having to remember
    the argument.
    """
    if sidebar is None:
        sidebar = host_picker_places(parent) or ()
    return file_picker.pick_directory(parent, caption, start, places=list(sidebar))


def pick_open_file(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    name_filter: str = "",
) -> str:
    """Existing-file picker.  Returns the path, or ``""`` when cancelled.

    *start* may name a file — it is then pre-selected inside its own folder.
    *name_filter* is Qt's filter syntax (``"画像 (*.png *.jpg)"``, entries
    separated by ``;;``).  The side panel carries the host window's places,
    as the folder picker does.
    """
    return file_picker.pick_open_file(
        parent, caption, start, name_filter,
        places=host_picker_places(parent) or (),
    )


def prompt_text(
    parent: QWidget | None, title: str, label: str, *, text: str = "",
) -> tuple[str, bool]:
    """Single-line text prompt.  Returns ``(value, accepted)``.

    Same contract as :meth:`QInputDialog.getText`, but built as an instance so
    the OK / Cancel buttons carry the catalog wording
    (:func:`~snappix.common.ui.localize_input_dialog`) rather than whatever Qt
    supplies for the active locale.
    """
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setInputMode(QInputDialog.InputMode.TextInput)
    dlg.setTextValue(text)
    localize_input_dialog(dlg)
    try:
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        return dlg.textValue(), accepted
    finally:
        dlg.deleteLater()


__all__ = [
    "host_picker_places",
    "pick_existing_directory",
    "pick_open_file",
    "prompt_text",
]
