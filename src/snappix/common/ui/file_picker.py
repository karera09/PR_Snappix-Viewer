"""Portable file / folder picker that does not use ``QFileDialog``.

**Why not ``QFileDialog``.**  Every ``QFileDialog`` — native or Qt's own
widget dialog, static helper or instance — saves its state (last visited
folder, sidebar shortcuts, view mode) into
``QSettings(QSettings.UserScope, "QtProject")`` when it is destroyed.  That
constructor always targets the operating system's per-user store (the
registry under ``HKCU\\Software\\QtProject`` on Windows); neither
``QSettings.setDefaultFormat`` nor ``QSettings.setPath`` redirects it.  A
portable application must not write there implicitly, so the only real fix is
to not create a ``QFileDialog`` at all.  This module is that replacement, and
a static guard keeps the class out of the code base.

**What it keeps from the old non-native picker.**  Listing goes through
``QFileSystemModel``, which gathers directory entries on its own worker
thread: a huge folder or a slow network share fills in progressively instead
of parking the GUI thread in a shell enumeration.  The dialog itself never
stats the entries it lists; the only synchronous file-system calls are the
ones a user action asks for (entering a typed path, accepting a name,
creating a folder).  The side panel takes caller-supplied *places* (library
roots, bookmarks) followed by the machine's drives, and the places are shown
as given — probing an offline share just to decorate the list would block.

**Wording.**  Labels are looked up by the short keys in :data:`TEXT_KEYS`
through a text source installed with :func:`install_text_source`; the host
application maps them to its message catalog once at start-up.  Colours and
fonts are not set here at all — the dialog inherits the application palette
and style sheet, so it follows the active theme.

**Copies.**  A byte-identical copy of this file ships with the standalone tag
scanner, which runs in its own process and cannot import this package.  A
parity test fails when the two differ, so edit both together (copy the file
verbatim — nothing in it may depend on where it lives).
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable, Iterable, Sequence
from typing import Literal, overload

from PySide6.QtCore import QDir, QEvent, QFileInfo, QModelIndex, QObject, QSize, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QStyle,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

#: Every text the picker shows, by short key.  The installed text source maps
#: each key to a template; ``{path}`` / ``{name}`` placeholders are filled here.
TEXT_KEYS: tuple[str, ...] = (
    "address",
    "address_placeholder",
    "cancel",
    "choose",
    "column_modified",
    "column_name",
    "column_size",
    "file_name",
    "file_type",
    "folder",
    "mkdir_failed",
    "new_folder",
    "not_found",
    "open",
    "overwrite_accept",
    "overwrite_body",
    "overwrite_title",
    "save",
    "up",
)

_text_source: Callable[[str], str] | None = None


def install_text_source(source: Callable[[str], str] | None) -> None:
    """Install the callable that turns a :data:`TEXT_KEYS` key into a template.

    ``None`` removes it; the picker then shows the bare keys (visible in a
    screenshot, never an exception).
    """
    global _text_source
    _text_source = source


def picker_text(key: str, **params: str) -> str:
    """The template for *key* from the installed source, formatted with *params*."""
    template = _text_source(key) if _text_source is not None else key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


class PickerMode(enum.Enum):
    DIRECTORY = "directory"
    OPEN = "open"
    OPEN_MANY = "open_many"
    SAVE = "save"


# QFileSystemModel's fixed column order.
_COL_NAME, _COL_SIZE, _COL_TYPE, _COL_MODIFIED = 0, 1, 2, 3


def parse_name_filter(entry: str) -> list[str]:
    """Glob patterns of one filter entry (``"画像 (*.png *.jpg)"`` → 2 patterns).

    Same reading as Qt's name filters: the patterns are the space-separated
    words inside the last pair of parentheses, or the whole entry when it has
    none.  ``*`` / ``*.*`` mean "everything" and come back as an empty list.
    """
    text = entry.strip()
    open_at = text.rfind("(")
    close_at = text.rfind(")")
    if 0 <= open_at < close_at:
        text = text[open_at + 1:close_at]
    patterns = [p for p in text.split() if p]
    if any(p in ("*", "*.*") for p in patterns):
        return []
    return patterns


def split_name_filters(name_filters: str | Sequence[str]) -> list[str]:
    """Normalise ``"A (*.a);;B (*.b)"`` or a sequence into a list of entries."""
    if isinstance(name_filters, str):
        entries = name_filters.split(";;")
    else:
        entries = list(name_filters)
    return [e.strip() for e in entries if e and e.strip()]


def split_quoted_names(text: str) -> list[str]:
    """``"a.png" "b.png"`` → ``["a.png", "b.png"]``; an unquoted name stays whole."""
    stripped = text.strip()
    if '"' not in stripped:
        return [stripped] if stripped else []
    names: list[str] = []
    parts = stripped.split('"')
    # Odd indices are the quoted segments.
    for i in range(1, len(parts), 2):
        if parts[i].strip():
            names.append(parts[i].strip())
    return names


def _normalise(path: str) -> str:
    """Forward-slash, cleaned absolute spelling (the form Qt's pickers return)."""
    if not path:
        return ""
    return QDir.cleanPath(QDir.fromNativeSeparators(os.path.abspath(path)))


class _PickerModel(QFileSystemModel):
    """File-system model whose column headers come from the picker's wording."""

    _HEADER_KEYS = {
        _COL_NAME: "column_name",
        _COL_SIZE: "column_size",
        _COL_MODIFIED: "column_modified",
    }

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and section in self._HEADER_KEYS
        ):
            return picker_text(self._HEADER_KEYS[section])
        return super().headerData(section, orientation, role)


class FilePickerDialog(QDialog):
    """The picker window.  Use the ``pick_*`` functions rather than this class.

    After ``exec()`` returns ``Accepted``, :meth:`selected_paths` holds the
    chosen paths (forward slashes, absolute).
    """

    def __init__(
        self,
        parent: QWidget | None,
        caption: str,
        start: str = "",
        *,
        mode: PickerMode = PickerMode.DIRECTORY,
        name_filters: str | Sequence[str] = (),
        places: Iterable[str | os.PathLike[str]] = (),
        default_name: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(caption)
        self.setObjectName("snappixFilePicker")
        self._mode = mode
        self._current = ""
        self._result: list[str] = []
        self._filters = split_name_filters(name_filters) if mode is not PickerMode.DIRECTORY else []

        style = self.style()

        # -- top row: up / address / new folder --------------------------
        self._up_button = QToolButton(self)
        # An arrow drawn by the style follows the palette; the stock icon is
        # a fixed dark glyph that disappears on a dark theme.
        self._up_button.setArrowType(Qt.ArrowType.UpArrow)
        self._up_button.setToolTip(picker_text("up"))
        self._up_button.setShortcut(QKeySequence("Alt+Up"))
        self._up_button.clicked.connect(self.go_up)

        self._address = QLineEdit(self)
        self._address.setPlaceholderText(picker_text("address_placeholder"))
        self._address.installEventFilter(self)
        address_label = QLabel(picker_text("address"), self)
        address_label.setBuddy(self._address)

        self._new_folder_button = QToolButton(self)
        self._new_folder_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)
        )
        self._new_folder_button.setToolTip(picker_text("new_folder"))
        self._new_folder_button.clicked.connect(self.create_folder)
        self._new_folder_button.setVisible(mode is not PickerMode.OPEN and mode is not PickerMode.OPEN_MANY)

        top = QHBoxLayout()
        top.addWidget(self._up_button)
        top.addWidget(address_label)
        top.addWidget(self._address, 1)
        top.addWidget(self._new_folder_button)

        # -- places | listing ---------------------------------------------
        self._places = QListWidget(self)
        self._places.setObjectName("snappixFilePickerPlaces")
        small = style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self._places.setIconSize(QSize(small, small))
        self._places.itemClicked.connect(self._on_place_clicked)
        self._places.itemActivated.connect(self._on_place_clicked)
        self._populate_places(places)

        self._model = _PickerModel(self)
        # Kept as an attribute: the model does not own its icon provider.
        self._icons = QFileIconProvider()
        self._icons.setOptions(QFileIconProvider.Option.DontUseCustomDirectoryIcons)
        self._model.setIconProvider(self._icons)
        self._model.setOption(QFileSystemModel.Option.DontUseCustomDirectoryIcons, True)
        self._model.setReadOnly(False)  # rename right after "new folder" only
        self._model.setNameFilterDisables(False)
        dir_filter = QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives
        if mode is not PickerMode.DIRECTORY:
            dir_filter |= QDir.Filter.Files
        self._model.setFilter(dir_filter)
        self._model.setRootPath("")
        self._model.directoryLoaded.connect(self._on_directory_loaded)

        self._view = QTreeView(self)
        self._view.setObjectName("snappixFilePickerView")
        self._view.setModel(self._model)
        self._view.setRootIsDecorated(False)
        self._view.setItemsExpandable(False)
        self._view.setUniformRowHeights(True)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(_COL_NAME, Qt.SortOrder.AscendingOrder)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
            if mode is PickerMode.OPEN_MANY
            else QAbstractItemView.SelectionMode.SingleSelection
        )
        self._view.setColumnHidden(_COL_TYPE, True)
        # Folders have no size; in folder mode the column would be blank.
        self._view.setColumnHidden(_COL_SIZE, mode is PickerMode.DIRECTORY)
        header = self._view.header()
        header.moveSection(header.visualIndex(_COL_MODIFIED), 1)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COL_NAME, header.ResizeMode.Stretch)
        header.resizeSection(_COL_MODIFIED, 150)
        header.resizeSection(_COL_SIZE, 90)
        self._view.activated.connect(self._on_activated)
        self._view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._view.installEventFilter(self)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._places)
        splitter.addWidget(self._view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([180, 560])

        # -- name / type ----------------------------------------------------
        self._name = QLineEdit(self)
        self._name.setText(default_name)
        self._type = QComboBox(self)
        self._type.addItems(self._filters)
        self._type.currentIndexChanged.connect(self._apply_filter)
        form = QFormLayout()
        name_key = "folder" if mode is PickerMode.DIRECTORY else "file_name"
        form.addRow(picker_text(name_key), self._name)
        if self._filters:
            form.addRow(picker_text("file_type"), self._type)
        else:
            self._type.hide()

        self._message = QLabel(self)
        self._message.setObjectName("snappixFilePickerMessage")
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.hide()

        # 標準ボタンではなく役割指定の addButton で自前のラベルを付ける（tagger の
        # 写しは snappix を import できないので、カタログ経由の localize_buttons に
        # 頼らず、この 1 実装で完結させる）。
        self._buttons = QDialogButtonBox(parent=self)
        accept_key = {
            PickerMode.DIRECTORY: "choose",
            PickerMode.OPEN: "open",
            PickerMode.OPEN_MANY: "open",
            PickerMode.SAVE: "save",
        }[mode]
        self._accept_button = self._buttons.addButton(
            picker_text(accept_key), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._cancel_button = self._buttons.addButton(
            picker_text("cancel"), QDialogButtonBox.ButtonRole.RejectRole
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(form)
        layout.addWidget(self._message)
        layout.addWidget(self._buttons)
        self.resize(760, 480)

        self._apply_filter()
        self._start_at(start)

    # ------------------------------------------------------------ accessors

    def selected_paths(self) -> list[str]:
        return list(self._result)

    def current_directory(self) -> str:
        """The listed folder (``""`` = the drive list)."""
        return self._current

    def place_paths(self) -> list[str]:
        """The side panel's entries, in order."""
        return [
            self._places.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._places.count())
        ]

    def message_text(self) -> str:
        """The inline error line (``""`` while hidden)."""
        return self._message.text() if not self._message.isHidden() else ""

    def listed_names(self) -> list[str]:
        """Names currently listed under the shown folder (sorted as displayed)."""
        root = self._view.rootIndex()
        return [
            self._model.index(row, _COL_NAME, root).data()
            for row in range(self._model.rowCount(root))
        ]

    def name_text(self) -> str:
        return self._name.text()

    def set_name_text(self, text: str) -> None:
        self._name.setText(text)

    def filter_entries(self) -> list[str]:
        return list(self._filters)

    def set_filter_index(self, index: int) -> None:
        self._type.setCurrentIndex(index)

    def select_names(self, names: Iterable[str]) -> None:
        """Select the listed entries called *names* (as a click would)."""
        sel = self._view.selectionModel()
        sel.clearSelection()
        flags = sel.SelectionFlag.Select | sel.SelectionFlag.Rows
        first = True
        for name in names:
            idx = self._model.index(self._join(name))
            if not idx.isValid():
                continue
            if first:
                self._view.setCurrentIndex(idx)
                first = False
            sel.select(idx, flags)

    # ------------------------------------------------------------ navigation

    def navigate(self, path: str) -> bool:
        """List *path* (``""`` = the drive list).  ``False`` when it is not a folder."""
        target = _normalise(path)
        if target and not QFileInfo(target).isDir():
            self._show_message(picker_text("not_found", path=QDir.toNativeSeparators(path)))
            return False
        self._clear_message()
        self._current = target
        self._model.setRootPath(target)
        self._view.setRootIndex(self._model.index(target) if target else QModelIndex())
        self._view.selectionModel().clearSelection()
        self._address.setText(QDir.toNativeSeparators(target))
        self._up_button.setEnabled(bool(target))
        self._new_folder_button.setEnabled(bool(target))
        return True

    def go_up(self) -> None:
        if not self._current:
            return
        parent = QFileInfo(self._current).dir()
        above = parent.absolutePath()
        if QDir.cleanPath(above) == QDir.cleanPath(self._current) or QDir(self._current).isRoot():
            above = ""
        self.navigate(above)

    def create_folder(self) -> None:
        """Make a new folder in the listed one and start renaming it."""
        if not self._current:
            return
        base = picker_text("new_folder")
        name = base
        n = 2
        while os.path.exists(os.path.join(self._current, name)):
            name = f"{base} ({n})"
            n += 1
        idx = self._model.mkdir(self._model.index(self._current), name)
        if not idx.isValid():
            self._show_message(
                picker_text("mkdir_failed", path=QDir.toNativeSeparators(self._join(name)))
            )
            return
        self._view.setCurrentIndex(idx)
        self._view.edit(idx)

    # ------------------------------------------------------------ internals

    def _start_at(self, start: str) -> None:
        if not start:
            self.navigate(QDir.currentPath())
            return
        if self._mode is PickerMode.DIRECTORY:
            if not self.navigate(start):
                self._clear_message()
                self.navigate("")
            return
        info = QFileInfo(start)
        if info.isDir():
            self.navigate(start)
            return
        if self.navigate(info.absolutePath()):
            self._name.setText(info.fileName())
        else:
            self._clear_message()
            self.navigate("")

    def _join(self, name: str) -> str:
        if os.path.isabs(name) or not self._current:
            return _normalise(name)
        return _normalise(os.path.join(self._current, name))

    def _populate_places(self, places: Iterable[str | os.PathLike[str]]) -> None:
        seen: set[str] = set()
        style = self.style()
        folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        drive_icon = style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        entries: list[tuple[str, str, bool]] = []
        for place in places:
            text = _normalise(os.fspath(place))
            if text and text not in seen:
                seen.add(text)
                label = os.path.basename(text.rstrip("/")) or QDir.toNativeSeparators(text)
                entries.append((text, label, False))
        for drive in QDir.drives():
            text = QDir.cleanPath(drive.absoluteFilePath())
            if text not in seen:
                seen.add(text)
                entries.append((text, QDir.toNativeSeparators(drive.absoluteFilePath()), True))
        for path, label, is_drive in entries:
            item = QListWidgetItem(drive_icon if is_drive else folder_icon, label, self._places)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(QDir.toNativeSeparators(path))

    def _on_place_clicked(self, item: QListWidgetItem) -> None:
        self.navigate(item.data(Qt.ItemDataRole.UserRole))

    def _apply_filter(self, *_args: object) -> None:
        if not self._filters:
            self._model.setNameFilters([])
            return
        entry = self._filters[max(0, self._type.currentIndex())]
        self._model.setNameFilters(parse_name_filter(entry))

    def _on_directory_loaded(self, path: str) -> None:
        """Highlight the typed / pre-filled file name once its folder is listed.

        The name field is the only state: nothing is remembered between
        navigations, so a later folder never inherits an earlier selection.
        """
        if (
            self._mode is PickerMode.DIRECTORY
            or QDir.cleanPath(path) != self._current
            or self._view.currentIndex().isValid()
        ):
            return
        name = self._name.text().strip()
        if not name or '"' in name:
            return
        idx = self._model.index(self._join(name))
        if idx.isValid():
            self._view.setCurrentIndex(idx)

    def _on_selection_changed(self, *_args: object) -> None:
        rows = [
            i for i in self._view.selectionModel().selectedRows(_COL_NAME) if i.isValid()
        ]
        if self._mode is PickerMode.DIRECTORY:
            dirs = [i for i in rows if self._model.isDir(i)]
            if dirs:
                self._name.setText(self._model.fileName(dirs[0]))
            return
        files = [i for i in rows if not self._model.isDir(i)]
        if not files:
            return
        if self._mode is PickerMode.OPEN_MANY and len(files) > 1:
            self._name.setText(" ".join(f'"{self._model.fileName(i)}"' for i in files))
        else:
            self._name.setText(self._model.fileName(files[0]))

    def _on_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        if self._model.isDir(index):
            self.navigate(self._model.filePath(index))
            if self._mode is PickerMode.DIRECTORY:
                self._name.clear()
            return
        if self._mode is not PickerMode.DIRECTORY:
            self._name.setText(self._model.fileName(index))
            self._on_accept()

    def _on_address_entered(self) -> None:
        text = self._address.text().strip()
        if not text:
            self.navigate("")
            return
        path = _normalise(text)
        info = QFileInfo(path)
        if info.isDir():
            self.navigate(path)
        elif info.isFile() and self._mode is not PickerMode.DIRECTORY:
            if self.navigate(info.absolutePath()):
                self._name.setText(info.fileName())
        else:
            self._show_message(picker_text("not_found", path=text))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Return in the address bar or the listing is theirs; without this the
        # dialog's default button would also fire and accept the dialog.
        if event.type() == QEvent.Type.KeyPress and event.key() in (  # type: ignore[attr-defined]
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            if watched is self._address:
                self._on_address_entered()
                return True
            if watched is self._view and self._view.state() != QAbstractItemView.State.EditingState:
                current = self._view.currentIndex()
                if current.isValid():
                    self._on_activated(current)
                else:
                    self._on_accept()
                return True
        return super().eventFilter(watched, event)

    def _show_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.show()

    def _clear_message(self) -> None:
        self._message.clear()
        self._message.hide()

    def _on_accept(self) -> None:
        self._clear_message()
        handler = {
            PickerMode.DIRECTORY: self._accept_directory,
            PickerMode.OPEN: self._accept_open,
            PickerMode.OPEN_MANY: self._accept_open,
            PickerMode.SAVE: self._accept_save,
        }[self._mode]
        result = handler()
        if result:
            self._result = result
            self.accept()

    def _accept_directory(self) -> list[str]:
        text = self._name.text().strip()
        if not text:
            return [self._current] if self._current else []
        path = self._join(text)
        if QFileInfo(path).isDir():
            return [path]
        self._show_message(picker_text("not_found", path=text))
        return []

    def _accept_open(self) -> list[str]:
        names = split_quoted_names(self._name.text())
        if self._mode is PickerMode.OPEN and len(names) > 1:
            names = [self._name.text().strip()]
        if not names:
            return []
        paths = [self._join(n) for n in names]
        if len(paths) == 1 and QFileInfo(paths[0]).isDir():
            self.navigate(paths[0])
            self._name.clear()
            return []
        for name, path in zip(names, paths, strict=True):
            if not QFileInfo(path).isFile():
                self._show_message(picker_text("not_found", path=name))
                return []
        return paths

    def _accept_save(self) -> list[str]:
        text = self._name.text().strip()
        if not text:
            return []
        path = self._join(text)
        info = QFileInfo(path)
        if info.isDir():
            self.navigate(path)
            self._name.clear()
            return []
        if not os.path.splitext(text)[1]:
            suffix = self._default_suffix()
            if suffix:
                path += suffix
                info = QFileInfo(path)
        if not QFileInfo(info.absolutePath()).isDir():
            self._show_message(
                picker_text("not_found", path=QDir.toNativeSeparators(info.absolutePath()))
            )
            return []
        if info.exists() and not self._confirm_overwrite(info.fileName()):
            return []
        return [path]

    def _default_suffix(self) -> str:
        if not self._filters:
            return ""
        patterns = parse_name_filter(self._filters[max(0, self._type.currentIndex())])
        for pattern in patterns:
            if pattern.startswith("*.") and not any(c in pattern[2:] for c in "*?["):
                return pattern[1:]
        return ""

    def _confirm_overwrite(self, name: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(picker_text("overwrite_title"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(picker_text("overwrite_body", name=name))
        accept = box.addButton(picker_text("overwrite_accept"), QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(picker_text("cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        try:
            box.exec()
            return box.clickedButton() is accept
        finally:
            box.deleteLater()


def _run(dialog: FilePickerDialog) -> list[str]:
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []
        return dialog.selected_paths()
    finally:
        dialog.deleteLater()


def pick_directory(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    *,
    places: Iterable[str | os.PathLike[str]] = (),
) -> str:
    """Choose an existing folder.  Returns its path, or ``""`` when cancelled."""
    paths = _run(
        FilePickerDialog(parent, caption, start, mode=PickerMode.DIRECTORY, places=places)
    )
    return paths[0] if paths else ""


@overload
def pick_open_file(
    parent: QWidget | None,
    caption: str,
    start: str = ...,
    name_filters: str | Sequence[str] = ...,
    *,
    multiple: Literal[False] = ...,
    places: Iterable[str | os.PathLike[str]] = ...,
) -> str: ...


@overload
def pick_open_file(
    parent: QWidget | None,
    caption: str,
    start: str = ...,
    name_filters: str | Sequence[str] = ...,
    *,
    multiple: Literal[True],
    places: Iterable[str | os.PathLike[str]] = ...,
) -> list[str]: ...


def pick_open_file(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    name_filters: str | Sequence[str] = (),
    *,
    multiple: bool = False,
    places: Iterable[str | os.PathLike[str]] = (),
) -> str | list[str]:
    """Choose existing file(s).

    Returns the path (``""`` when cancelled), or with ``multiple=True`` the
    list of paths (empty when cancelled).  *start* may name a file; it is then
    pre-selected inside its folder.
    """
    mode = PickerMode.OPEN_MANY if multiple else PickerMode.OPEN
    paths = _run(
        FilePickerDialog(
            parent, caption, start, mode=mode, name_filters=name_filters, places=places,
        )
    )
    if multiple:
        return paths
    return paths[0] if paths else ""


def pick_save_file(
    parent: QWidget | None,
    caption: str,
    start: str = "",
    name_filters: str | Sequence[str] = (),
    default_name: str = "",
    *,
    places: Iterable[str | os.PathLike[str]] = (),
) -> str:
    """Choose a file name to write.  Returns the path, or ``""`` when cancelled.

    A name without an extension gets the first plain ``*.ext`` of the active
    filter; an existing file is replaced only after a confirmation.
    """
    paths = _run(
        FilePickerDialog(
            parent, caption, start, mode=PickerMode.SAVE,
            name_filters=name_filters, places=places, default_name=default_name,
        )
    )
    return paths[0] if paths else ""


__all__ = [
    "FilePickerDialog",
    "PickerMode",
    "TEXT_KEYS",
    "install_text_source",
    "parse_name_filter",
    "pick_directory",
    "pick_open_file",
    "pick_save_file",
    "picker_text",
    "split_name_filters",
    "split_quoted_names",
]
