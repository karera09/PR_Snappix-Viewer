"""専用ビューを持たないファイルのメタ情報カード（``FileInfoView``）。"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...common.i18n import t
from ...common.ui import EmptyStateCard
from .._runnable import GuardedStream
from ..edge_nav import navigate_on_wheel
from ..view_prefs import _format_bytes, _reveal_in_explorer, open_with_default


def _stat_label(path: Path) -> str:
    """``stat`` a generic file into a formatted meta line (worker thread).

    ``FileInfoView`` previews PSD / 7z / etc.  A synchronous ``path.stat()``
    on the GUI thread blocks for the full NAS round-trip on every selection
    — hundreds of ms on cold SMB — so this runs on a
    :class:`~snappix.viewer._runnable.GuardedStream` worker instead.
    """
    try:
        stat = path.stat()
        mtime = _dt.datetime.fromtimestamp(stat.st_mtime)
        size = _format_bytes(stat.st_size)
        return t("viewer.content_view.file_meta", size=size, mtime=mtime)
    except OSError as exc:
        return t("viewer.content_view.file_info_error", exc=exc)


class FileInfoView(QWidget):
    """Generic non-image / non-md preview: size, mtime, open buttons.

    共通カード規格 :class:`~snappix.common.ui.EmptyStateCard` に載せて
    ``ContentView`` が持つ兄弟カードと見出し・余白・図像を揃える。
    **これは空状態ではなく内容表示**なので ``empty_state.py`` の席裁定
    （PRIMARY / SECONDARY）には登録しない — カードを部品として使うだけ。

    絶対パスは本文から外してウィジェットのツールチップへ（常設の情報パネルが
    同じものを持っている面で、カード中央に長い 1 行を置かない）。
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate=True always)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._card = EmptyStateCard(
            t("viewer.content_view.no_preview_heading"),
            icon_name="help-circle",
            emphasis="onboarding",
        )
        layout.addWidget(self._card)
        # 見出し = 状況 / 本文 = ファイル名 / 注記 = サイズ・更新日時。
        self._meta = self._card.add_note("")

        # Async stat stream (token-guarded)。self に親付けするのでワーカー
        # mid-``emit`` 中の GC も起きない。
        self._stat_stream = GuardedStream(self)
        self._stat_stream.bind(self._on_stat_done)

        self._open_btn = self._card.add_action(
            t("common.action.open_with_default"), icon_name="external-link",
        )
        self._open_btn.clicked.connect(self._on_open_default)
        # OS 起動系の専用図像。
        self._reveal_btn = self._card.add_action(
            t("common.action.open_in_explorer"), icon_name="folder-output",
        )
        self._reveal_btn.clicked.connect(self._on_reveal)

        self._path: Path | None = None

    def show_file(self, path: Path) -> None:
        self._path = path
        self._card.set_body(path.name)
        self._meta.setText(t("common.status.loading"))
        self.setToolTip(str(path))
        # stat() off the GUI thread — see _stat_label.  The stream token
        # drops a stale result if the user selects another file first.
        self._stat_stream.submit(lambda p=path: _stat_label(p))

    def _on_stat_done(self, meta: object) -> None:
        if self._path is None:
            return  # cleared while the stat was in flight
        if isinstance(meta, str):
            self._meta.setText(meta)

    def clear_file(self) -> None:
        self._stat_stream.cancel()  # invalidate any in-flight stat
        self._path = None
        self._card.set_body("")
        self._meta.clear()
        self.setToolTip("")

    def _on_open_default(self) -> None:
        # 共通ヘルパ経由 — 失敗時はステータス通知。
        if self._path is None:
            return
        open_with_default(self._path, self)

    def _on_reveal(self) -> None:
        if self._path is None:
            return
        _reveal_in_explorer(self._path, self)

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        # No scrollable content — any vertical wheel navigates immediately.
        if navigate_on_wheel(event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)
