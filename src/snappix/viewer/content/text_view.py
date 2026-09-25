"""プレーンテキストのプレビュー（``TextView``）と符号推定ヘルパ。"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...common.i18n import t
from ...common.touch import enable_touch_scroll
from ...common.ui import FONT_CAPTION_PT, hint_style, set_icon
from .. import view_prefs
from .._runnable import GuardedStream
from ..context_menus import (
    EntryMenuContext,
    append_entry_verbs,
    curation_hooks_from_ancestors,
)
from ..edge_nav import navigate_at_edge, navigate_on_wheel
from ..text_decode import decode_text
from ..focus_target import SEAT_PREVIEW
from ..view_prefs import _format_bytes, _reveal_in_explorer, open_with_default
from ._shared import _popup_entry_menu


TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".txt", ".log",
    ".csv", ".tsv",
    ".json",
    ".xml",
    ".yaml", ".yml",
    ".toml",
    ".ini", ".cfg", ".conf",
})


def _read_text_preview(path: Path, limit: int) -> tuple[str, str]:
    """Stat + read a text file up to the preview cap (worker thread).

    Returns ``(meta line, body text)`` — meta is empty on stat failure, the
    body carries the error notice on read failure.  Reading up to 2 MiB
    synchronously froze the UI for the full NAS round-trip on every
    .txt/.json/.csv selection; this runs on a
    :class:`~snappix.viewer._runnable.GuardedStream` worker。
    """
    meta = ""
    try:
        stat = path.stat()
        mtime = _dt.datetime.fromtimestamp(stat.st_mtime)
        meta = f"{_format_bytes(stat.st_size)}  {mtime:%Y-%m-%d %H:%M}"
    except OSError:
        pass
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit + 1)
        truncated = len(raw) > limit
        if truncated:
            # 上限で切ると末尾がほぼ必ず多バイト文字の途中になる — 半端な
            # 1 文字の扱いは ``decode_text(truncated=True)`` が符号ごとに持つ。
            raw = raw[:limit]
        text = decode_text(raw, truncated=truncated)
        if truncated:
            limit_mib = limit / (1024 * 1024)
            text += t("viewer.content_view.text_truncated", mib=limit_mib)
    except OSError as exc:
        text = t("viewer.content_view.text_read_error", exc=exc)
    return meta, text


class TextView(QWidget):
    """Plain-text file viewer backed by a read-only QPlainTextEdit.

    Encoding detection: :func:`~snappix.viewer.text_decode.decode_text`.
    Files larger than ``_TEXT_MAX_BYTES`` are truncated with a notice.
    Wheel gestures at the top/bottom edge emit ``navigate_requested``
    consistent with the other preview sub-views.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- header bar ------------------------------------------------
        header = QWidget()
        header.setObjectName("textview_header")
        header.setStyleSheet(
            "QWidget#textview_header { background: palette(window); "
            "border-bottom: 1px solid palette(mid); }"
        )
        hbox = QHBoxLayout(header)
        hbox.setContentsMargins(8, 4, 8, 4)

        self._title = QLabel()
        self._title.setStyleSheet("font-weight: bold;")
        # A non-word-wrapping QLabel reports its *full* text width as its
        # minimumSizeHint.  Left at the default size policy, a long file
        # name would force the whole centre pane's minimum width up, and
        # QSplitter widens the pane to satisfy it (stealing from the file
        # list) — so the preview pane appeared to creep wider every time a
        # text file with a longer name was opened.  ``Ignored`` drops the
        # label's width hint from the layout's minimum so the pane keeps a
        # stable width; the label still fills (and clips) the stretch space
        # it's given, with the full name available via the tooltip.
        self._title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._title.setMinimumWidth(0)
        hbox.addWidget(self._title, 1)

        self._meta = QLabel()
        self._meta.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
        hbox.addWidget(self._meta)

        self._open_btn = QPushButton(t("common.action.open_with_default"))
        set_icon(self._open_btn, "external-link")
        self._open_btn.setFixedHeight(24)
        self._open_btn.clicked.connect(self._on_open_default)
        hbox.addWidget(self._open_btn)

        self._reveal_btn = QPushButton(t("common.action.open_in_explorer"))
        set_icon(self._reveal_btn, "folder-output")  # OS 起動系の専用図像
        self._reveal_btn.setFixedHeight(24)
        self._reveal_btn.clicked.connect(self._on_reveal)
        hbox.addWidget(self._reveal_btn)

        layout.addWidget(header)

        # ---- text body -------------------------------------------------
        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self._edit.font()
        # Qt6 treats setFamily() as a single family name, so a CSS-style
        # comma list fails to match and falls back to the default font.  Use
        # setFamilies() (a real preference list) + a Monospace style hint so
        # the fallback still lands on a fixed-width face.  Size comes from the
        # design-system caption token — no hardcoded point sizes (design.md).
        font.setFamilies(["Consolas", "Yu Gothic UI"])
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(FONT_CAPTION_PT)
        self._edit.setFont(font)
        self._edit.installEventFilter(self)
        # Extend the read-only edit's native menu (copy / select-all)
        # with the shared file-entry actions instead of replacing it.
        self._edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self._edit.customContextMenuRequested.connect(self._on_edit_menu)
        enable_touch_scroll(self._edit)
        layout.addWidget(self._edit, 1)

        self._path: Path | None = None
        # Async read stream (token-guarded)。
        self._read_stream = GuardedStream(self)
        self._read_stream.bind(self._on_text_read)

    # ------------------------------------------------------------------ API

    def show_text(self, path: Path) -> None:
        self._path = path
        self._title.setText(path.name)
        self._title.setToolTip(path.name)
        self._meta.setText("")
        self._edit.setPlainText(t("common.status.loading"))
        self._read_stream.submit(
            lambda p=path, limit=view_prefs._TEXT_MAX_BYTES: (
                _read_text_preview(p, limit)
            )
        )

    def _on_text_read(self, payload: object) -> None:
        if self._path is None:
            return  # cleared while the read was in flight
        if not isinstance(payload, tuple):
            return  # defensive — the worker only ever returns (meta, text)
        meta, text = payload
        self._meta.setText(meta)
        self._edit.setPlainText(text)
        # scroll back to top
        cursor = self._edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self._edit.setTextCursor(cursor)

    def clear_text(self) -> None:
        # in-flight の読み取りも無効化する（ガード規約を 1 か所に保つ —
        # ``_path is None`` の偶然に頼らない）。
        self._read_stream.cancel()
        self._path = None
        self._title.clear()
        self._meta.clear()
        self._edit.clear()

    # ------------------------------------------------------------------ internals

    def _on_open_default(self) -> None:
        # 共通ヘルパ経由 — 失敗時はステータス通知。
        if self._path is not None:
            open_with_default(self._path, self)

    def _on_reveal(self) -> None:
        if self._path is not None:
            _reveal_in_explorer(self._path, self)

    def _on_edit_menu(self, pos) -> None:
        # Native copy / select-all PLUS the shared file-entry actions.
        menu = self._edit.createStandardContextMenu()
        if self._path is not None:
            menu.addSeparator()
            # テキスト編集の標準項目（コピー / すべて選択）の下に共通ブロック。
            # 見出しは標準項目の上に来て紛らわしいので付けない。
            append_entry_verbs(
                menu,
                EntryMenuContext(
                    path=self._path, is_dir=False, seat=SEAT_PREVIEW,
                    curation=curation_hooks_from_ancestors(self), host=self,
                    header=False,
                ),
            )
        menu.exec(self._edit.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Right-click on the header chrome → shared entry menu.
        _popup_entry_menu(self, self._path, event.globalPos())

    def eventFilter(self, obj: object, event: QEvent) -> bool:  # type: ignore[override]
        # Wheel inside the text edit: navigate only at the scroll edge;
        # otherwise the edit scrolls normally.
        if (
            obj is self._edit
            and event.type() == QEvent.Type.Wheel
            and navigate_at_edge(
                self._edit.verticalScrollBar(), event,
                self.navigate_requested.emit,
            )
        ):
            return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Wheel on the header chrome — navigate immediately.
        if navigate_on_wheel(event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)
