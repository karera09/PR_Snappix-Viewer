"""Shared Qt dialog helpers for the viewer.

**Why the folder picker is forced non-native.**  The native OS folder dialog
enumerates the browsed directory through the Windows shell, which also computes
folder sizes and generates item previews.  On an abnormally large directory
(tens of thousands of entries — e.g. a temp tree) that shell call can hang for
*minutes* inside ``QFileDialog.getExistingDirectory``'s native modal loop,
freezing the whole GUI: the main thread is parked in C++ with no way to service
the Qt event loop.  A freeze trace pinpointed exactly this — the main thread
stuck in ``getExistingDirectory`` while the user browsed into a huge folder.

Qt's own (non-native) dialog enumerates the directory itself without invoking
the shell's preview/size machinery, so it stays responsive (and cancellable)
even on pathological folders.  We trade the native look for that robustness
because this tool routinely browses very large library trees.

**Why these are instances, not the static one-liners.**  The static
``QFileDialog.getExistingDirectory`` / ``getOpenFileName`` / ``QInputDialog
.getText`` helpers hand back only the result — there is no dialog object to
configure, so the picker was stuck with Qt's defaults (UIレビュー 2026-08-28
N-01/旧N-57):

* a **Detail** view whose ``Size`` column is empty on every row and whose
  ``Type`` column reads "Folder" on every row, squeezing the one column that
  matters (the folder name) until it elides mid-word;
* labels Qt's ``qtbase_ja.qm`` does not reach (「場所:」/「ファイルの種別:」 come
  from the dialog's own ``.ui``, not the standard-button table), so they stayed
  English even after the translator was installed
  (:mod:`snappix.common.qt_i18n`).

Building the dialog here fixes both and keeps the non-native decision above
intact.  Every picker in the viewer goes through this module so the wording and
the column layout are decided once.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QDir, QUrl
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QWidget

from ..common.i18n import t
from ..common.ui import localize_input_dialog


def _sidebar_urls(places: Iterable[Path]) -> list[QUrl]:
    """The side panel's entries: *places* first, then the machine's drives.

    Qt's own dialog starts with an **empty** side panel (the native picker's
    「クイックアクセス」 comes from the shell, which the non-native decision
    above gives up), so every jump to another library meant typing or walking
    the whole path.  Feeding it the roots the user already registered — plus
    the drive list Qt would otherwise never show — restores the one-click jump
    without reaching for the shell (UIレビュー 2026-09-11 N-156).

    Duplicates are dropped while the given order is kept, and unreadable /
    offline entries are passed through untouched: probing them here would
    reintroduce the very blocking ``stat`` the non-native picker exists to
    avoid.
    """
    urls: list[QUrl] = []
    seen: set[str] = set()
    for place in places:
        text = str(place)
        if text and text not in seen:
            seen.add(text)
            urls.append(QUrl.fromLocalFile(text))
    for drive in QDir.drives():
        text = drive.absoluteFilePath()
        if text not in seen:
            seen.add(text)
            urls.append(QUrl.fromLocalFile(text))
    return urls


def _localize_file_dialog(
    dlg: QFileDialog, *, accept_key: str, file_name_key: str,
) -> None:
    """Label a picker from the catalog (the labels Qt's own qm misses).

    Harmless on a native dialog: the platform helper ignores label overrides,
    and they take effect the moment Qt falls back to its own widget dialog.
    """
    label = QFileDialog.DialogLabel
    dlg.setLabelText(label.LookIn, t("common.file_dialog.look_in"))
    dlg.setLabelText(label.FileName, t(file_name_key))
    dlg.setLabelText(label.FileType, t("common.file_dialog.file_type"))
    dlg.setLabelText(label.Accept, t(accept_key))
    dlg.setLabelText(label.Reject, t("common.action.cancel"))


def _first_selected(dlg: QFileDialog) -> str:
    files = dlg.selectedFiles()
    return files[0] if files else ""


def host_picker_places(widget: QWidget | None) -> list[Path] | None:
    """The places *widget*'s host window offers the folder picker.

    The list belongs to the host window (registered library roots + bookmarks
    — ``ViewerWindow.picker_places``); panes and child dialogs ask for it
    through this one helper so the side panel is identical from every entry
    point.  ``None`` when there is no such host (a stand-alone widget in a
    test), which leaves the picker at Qt's default.

    **Walks the ancestors**, not ``window()``: ``QWidget.window()`` answers
    "the nearest ancestor that can carry a window frame", which from inside a
    ``QDialog`` is the dialog itself — it has no ``picker_places``, so every
    picker opened from a dialog fell back to the empty panel.  Same shape as
    :func:`~snappix.viewer.context_menus.curation_hooks_from_ancestors`, which
    already solves this dialog boundary for the curation hooks.
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
    """Folder picker that avoids the native shell (see module docstring).

    Same contract as :meth:`QFileDialog.getExistingDirectory`: returns the
    chosen directory path, or ``""`` when the user cancels.

    Shown in **list** mode: with ``ShowDirsOnly`` every row is a folder, so
    Detail mode's ``Size`` (always blank) and ``Type`` (always "Folder")
    columns carry no information while costing the folder-name column its
    width.  The user can still switch to Detail from the dialog's own toolbar.

    *sidebar* names the places to offer in the side panel (registered library
    roots, bookmarks …); the machine's drives are appended after them.  Passing
    nothing derives them from *parent* (:func:`host_picker_places`), so a new
    entry point gets the same panel as every other one without having to
    remember the argument; a widget with no host still leaves Qt's default —
    an empty panel — alone, so a caller with no meaningful places to offer
    does not get a panel holding only drives.
    """
    if sidebar is None:
        sidebar = host_picker_places(parent)
    dlg = QFileDialog(parent, caption, start)
    dlg.setFileMode(QFileDialog.FileMode.Directory)
    dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dlg.setViewMode(QFileDialog.ViewMode.List)
    if sidebar is not None:
        dlg.setSidebarUrls(_sidebar_urls(sidebar))
    _localize_file_dialog(
        dlg,
        accept_key="common.action.choose",
        file_name_key="common.file_dialog.folder",
    )
    try:
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return ""
        return _first_selected(dlg)
    finally:
        dlg.deleteLater()


def pick_open_file(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    name_filter: str = "",
) -> str:
    """Existing-file picker.  Returns the path, or ``""`` when cancelled.

    Same contract as :meth:`QFileDialog.getOpenFileName` (minus the selected
    filter, which no caller uses).  *start* may name a file — it is then
    pre-selected inside its own folder, as the static helper does.

    Unlike :func:`pick_existing_directory` this one keeps the **native**
    dialog: it browses to pick a single known file rather than walking a huge
    tree, and the native dialog brings image thumbnails and the shell's
    recent-places list, which matter for choosing a reference image.  The
    catalog labels are set anyway so the Qt fallback (non-Windows, offscreen)
    is worded like the rest of the app.
    """
    dlg = QFileDialog(parent, caption)
    dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
    dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    if name_filter:
        dlg.setNameFilter(name_filter)
    if start:
        start_path = Path(start)
        if start_path.is_file():
            dlg.setDirectory(str(start_path.parent))
            dlg.selectFile(start)
        else:
            dlg.setDirectory(start)
    _localize_file_dialog(
        dlg,
        accept_key="common.action.open",
        file_name_key="common.file_dialog.file_name",
    )
    try:
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return ""
        return _first_selected(dlg)
    finally:
        dlg.deleteLater()


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
