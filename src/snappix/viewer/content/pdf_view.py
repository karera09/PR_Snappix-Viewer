"""PDF プレビュー（``PdfView``）。

**QtPdf / QtPdfWidgets はモジュールレベルで import しないこと**:
``PdfView.__init__`` と :meth:`PdfView._on_pdf_bytes` /
:meth:`PdfView._on_zoom_changed` の関数内 import が遅延 import の段で、
``ContentView`` 側の遅延構築（``_ensure_pdf``）と両方揃って初めて「DLL 欠落
時に起動不能ではなくこのプレビューだけが失敗する」が成立する
（docs/claude/viewer/content.md「遅延 import の二段構え」）。
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from loguru import logger
from PySide6.QtCore import QBuffer, QByteArray, QPointF, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...common.i18n import t
from ...common.touch import enable_touch_scroll
from ...common.ui import EmptyStateCard, hint_style, set_icon
from .._runnable import GuardedStream
from ..edge_nav import navigate_at_edge
from ..view_prefs import (
    _format_bytes,
    get_pdf_preview_size_limit,
    open_with_default,
)
from ._shared import _popup_entry_menu


class _PdfTooLarge(NamedTuple):
    """``_read_pdf_bytes`` の「上限超のため読まなかった」戻り値."""

    size: int


def _read_pdf_bytes(
    path: Path, size_limit: int,
) -> "bytes | _PdfTooLarge | None":
    """Read a PDF's bytes for the preview (worker thread; ``None`` = failed).

    ``QPdfDocument.load(str)`` reads the file synchronously via QFile — a
    cold NAS open + read of a multi-MB PDF blocked the GUI thread, the one
    preview left doing so.  Reading the bytes here (Python ``open``, which
    also sidesteps the SMB/CJK-path issue) and handing them to
    ``load(QIODevice)`` on the GUI thread keeps the round-trip off the main
    thread; the in-memory parse that remains is fast.  Runs on a
    :class:`~snappix.viewer._runnable.GuardedStream` worker。

    ``size_limit`` を超えるファイルは**読まずに** :class:`_PdfTooLarge` を
    返す: ZIP（``_read_zip_listing``）/ テキストと同じく、重いリーフは
    まず ``stat`` で頭打ちにする。PDF だけ上限が無く、しかも
    ``QPdfView`` はページを遅延レンダするので読んだバイト列は表示中ずっと
    常駐する（ピークは ``QByteArray`` のコピーぶん 2N）。フォルダの代表
    ファイルが PDF なら一覧でクリックしただけでこの経路に入るため、上限が
    無いと「開いた覚えのない 200 MB」が黙って常駐していた。上限は
    ZIP と同じく dispatch 時点の値を渡す（走行中の設定変更で再ゲートしない）。
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("PDF stat failed for {}: {}", path, exc)
        return None
    if size > size_limit:
        return _PdfTooLarge(size)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("PDF read failed for {}: {}", path, exc)
        return None


# Zoom presets exposed in the PdfView combo box.  The first two entries
# map to QPdfView's built-in fit modes; the numeric entries switch to
# ``Custom`` zoom mode with the matching ``zoomFactor``.  The fit-mode
# entries hold i18n keys (resolved via ``t()`` at ``addItem`` time) so the
# combo wording stays catalog-driven; the numeric presets are language-
# neutral percentages left as literals.

_PDF_ZOOM_FIT_WINDOW = "viewer.content_view.pdf_zoom_fit_window"
_PDF_ZOOM_FIT_WIDTH = "viewer.content_view.pdf_zoom_fit_width"
_PDF_ZOOM_PRESETS: list[tuple[str, float]] = [
    ("100%", 1.0),
    ("150%", 1.5),
    ("200%", 2.0),
]


class PdfView(QWidget):
    """Embedded PDF preview backed by ``QPdfView``.

    A single ``QPdfDocument`` is kept for the lifetime of the widget and
    re-used across ``show_pdf()`` calls — reloading in place avoids leaking
    a renderer per file as the user navigates.  Page mode is ``MultiPage``
    so scrolling flows continuously across page boundaries; a compact
    toolbar row above the view provides page navigation (prev/next button +
    spin box) and a zoom-mode combo (fit-to-window / fit-to-width / fixed
    percentages).

    Wheel gestures at the top / bottom of the scroll range emit
    ``navigate_requested`` just like :class:`ImageView` and
    :class:`MarkdownView`, so arrow / wheel-through navigation is
    consistent across preview types.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Lazy import so a headless build that never opens a PDF doesn't
        # pay the QtPdfWidgets cost on startup.
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- toolbar row -------------------------------------------------
        toolbar = QWidget()
        toolbar.setObjectName("pdfview_toolbar")
        toolbar.setStyleSheet(
            "QWidget#pdfview_toolbar { background: palette(window); "
            "border-bottom: 1px solid palette(mid); }"
        )
        tbar = QHBoxLayout(toolbar)
        tbar.setContentsMargins(8, 4, 8, 4)

        self._prev_btn = QToolButton()
        set_icon(self._prev_btn, "chevron-left")
        self._prev_btn.setToolTip(t("common.action.prev_page"))
        self._prev_btn.clicked.connect(self._on_prev_page)
        tbar.addWidget(self._prev_btn)

        self._page_spin = QSpinBox()
        self._page_spin.setMinimum(1)
        self._page_spin.setMaximum(1)
        self._page_spin.setFixedWidth(64)
        self._page_spin.valueChanged.connect(self._on_page_spin_changed)
        tbar.addWidget(self._page_spin)

        self._page_total_label = QLabel("/ 1")
        tbar.addWidget(self._page_total_label)

        self._next_btn = QToolButton()
        set_icon(self._next_btn, "chevron-right")
        self._next_btn.setToolTip(t("common.action.next_page"))
        self._next_btn.clicked.connect(self._on_next_page)
        tbar.addWidget(self._next_btn)

        tbar.addSpacing(16)

        self._zoom_combo = QComboBox()
        self._zoom_combo.addItem(t(_PDF_ZOOM_FIT_WINDOW))
        self._zoom_combo.addItem(t(_PDF_ZOOM_FIT_WIDTH))
        for label, _factor in _PDF_ZOOM_PRESETS:
            self._zoom_combo.addItem(label)
        self._zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)
        tbar.addWidget(self._zoom_combo)

        tbar.addStretch(1)
        layout.addWidget(toolbar)

        self._doc = QPdfDocument(self)
        self._view = QPdfView(self)
        self._view.setDocument(self._doc)
        self._view.setPageMode(QPdfView.PageMode.MultiPage)
        self._view.setZoomMode(QPdfView.ZoomMode.FitInView)
        enable_touch_scroll(self._view)
        layout.addWidget(self._view, 1)

        self._nav = self._view.pageNavigator()
        self._nav.currentPageChanged.connect(self._on_current_page_changed)

        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet(hint_style())
        self._status.hide()
        layout.addWidget(self._status)

        # 失敗面（上限超 / 読めない / パース失敗）は素テキスト 1 行ではなく
        # 共通カード規格に載せて、逃げ道のボタンをその場に持たせる
        # （FileInfoView / ZipView と同じ体裁）。読み込み中の告知だけは
        # 軽い ``_status`` のまま。
        self._error_card = EmptyStateCard(
            t("viewer.content_view.pdf_error_heading"),
            icon_name="alert-triangle",
            emphasis="onboarding",
        )
        self._error_reload_btn = self._error_card.add_action(
            t("viewer.content_view.pdf_reload"), icon_name="refresh",
        )
        self._error_reload_btn.clicked.connect(self._on_error_reload)
        self._error_open_btn = self._error_card.add_action(
            t("common.action.open_with_default"), icon_name="external-link",
        )
        self._error_open_btn.clicked.connect(self._on_error_open_default)
        self._error_card.hide()
        layout.addWidget(self._error_card, 1)

        self._toolbar = toolbar
        self._path: Path | None = None
        # Guards against the spin box's programmatic updates (from
        # ``_on_current_page_changed``) re-triggering a page jump.
        self._updating_page_ui = False
        # Async byte-read stream (token-guarded)。The QBuffer the document
        # loads from must outlive the load, so it's pinned here and
        # released on the next load / clear.
        self._read_stream = GuardedStream(self)
        self._read_stream.bind(self._on_pdf_bytes)
        self._pdf_buffer: QBuffer | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)

    def show_pdf(self, path: Path) -> None:
        self._path = path
        # Read the bytes off the GUI thread (see _read_pdf_bytes); the parse
        # happens in _on_pdf_bytes once they land.  Show a loading notice so
        # a slow NAS read doesn't leave the previous PDF or a blank view up.
        self._view.hide()
        self._error_card.hide()
        self._toolbar.setEnabled(True)
        self._status.setText(t("common.status.loading_name", name=path.name))
        self._status.show()
        limit = get_pdf_preview_size_limit()
        self._read_stream.submit(lambda p=path, n=limit: _read_pdf_bytes(p, n))

    def _release_document(self) -> None:
        """直前の PDF のバイト列とパース済み文書を手放す。

        失敗面（上限超 / 読めない）は同じページに留まるので ``clear()``
        が走らない。ここを通さないと直前の PDF ぶんの ``QBuffer`` +
        ``QPdfDocument`` が常駐したままになる。
        """
        self._doc.close()
        if self._pdf_buffer is not None:
            self._pdf_buffer.close()
            self._pdf_buffer = None

    def _show_error_card(self, body: str) -> None:
        """失敗面を出す — 操作列は無効化し、その場に逃げ道を置く。

        無効化しないとページ送り / ページ番号 / ズームは押せるが
        ``_jump_relative`` は ``pageCount() <= 0`` で即 return し、
        ``_on_zoom_changed`` は空のビューにモードを設定するだけの
        「押せるのに何も起きない」死にボタンになる。
        """
        self._view.hide()
        self._status.hide()
        self._toolbar.setEnabled(False)
        self._error_card.set_body(body)
        self._error_card.show()
        self._set_page_range(1, 1)

    def _on_error_reload(self) -> None:
        """失敗カードの [再読み込み] — 同じ PDF をもう一度読み直す。"""
        if self._path is not None:
            self.show_pdf(self._path)

    def _on_error_open_default(self) -> None:
        if self._path is not None:
            open_with_default(self._path, self)

    def _on_pdf_bytes(self, data: object) -> None:
        from PySide6.QtPdf import QPdfDocument

        if self._path is None:
            return  # cleared while the read was in flight
        path = self._path
        if isinstance(data, _PdfTooLarge):
            # 上限超 — 読んでいないので常駐もしない。ZipView._on_too_large と
            # 同じ体裁で「サイズだけ告げて、既定アプリへ逃がす」。
            self._release_document()
            self._show_error_card(
                t(
                    "viewer.content_view.pdf_too_large",
                    size=_format_bytes(data.size),
                    limit_mib=get_pdf_preview_size_limit() // (1024 * 1024),
                )
            )
            return
        if data is None:
            self._release_document()
            self._show_error_card(
                t("viewer.content_view.pdf_load_error", name=path.name)
            )
            return
        # Load from an in-memory buffer that outlives the document (QPdfView
        # renders pages lazily, so the device must stay open).  Replace any
        # previous buffer only after the new load succeeds.
        buffer = QBuffer()
        buffer.setData(QByteArray(data))  # type: ignore[arg-type]
        if not buffer.open(QBuffer.ReadOnly):
            self._release_document()
            self._show_error_card(
                t("viewer.content_view.pdf_load_error", name=path.name)
            )
            return
        self._doc.close()
        # NB: the ``load(QIODevice)`` overload returns void — unlike
        # ``load(str)`` which returns a ``QPdfDocument.Error``.  Reading its
        # (nonexistent) return value yields ``None``, which compares unequal
        # to ``Error.None_`` and made every buffer-loaded PDF look failed.
        # For an in-memory QBuffer the parse is synchronous (status flips to
        # Ready immediately), so read the outcome from ``error()`` instead.
        self._doc.load(buffer)
        err = self._doc.error()
        if err != QPdfDocument.Error.None_:
            buffer.close()
            self._release_document()
            logger.warning("PDF preview load failed for {}: {}", path, err)
            self._show_error_card(
                t("viewer.content_view.pdf_load_error_code",
                  name=path.name, code=err.name)
            )
            return
        # Swap in the new buffer, releasing the previous one.
        if self._pdf_buffer is not None:
            self._pdf_buffer.close()
        self._pdf_buffer = buffer
        self._status.hide()
        self._error_card.hide()
        self._toolbar.setEnabled(True)
        self._view.show()
        page_count = max(1, self._doc.pageCount())
        self._set_page_range(1, page_count)

    def clear(self) -> None:
        self._read_stream.cancel()  # invalidate any in-flight read
        self._path = None
        self._release_document()
        self._status.hide()
        self._error_card.hide()
        self._toolbar.setEnabled(True)
        self._view.show()
        self._set_page_range(1, 1)

    def _set_page_range(self, current: int, total: int) -> None:
        self._updating_page_ui = True
        try:
            self._page_spin.setMaximum(max(1, total))
            self._page_spin.setValue(current)
        finally:
            self._updating_page_ui = False
        self._page_total_label.setText(f"/ {total}")

    # ------------------------------------------------------------ page nav

    def _on_prev_page(self) -> None:
        self._jump_relative(-1)

    def _on_next_page(self) -> None:
        self._jump_relative(1)

    def _jump_relative(self, delta: int) -> None:
        if self._doc.pageCount() <= 0:
            return
        current = self._nav.currentPage()
        target = max(0, min(self._doc.pageCount() - 1, current + delta))
        if target != current:
            self._nav.jump(target, QPointF(0, 0), self._nav.currentZoom())

    def _on_page_spin_changed(self, value: int) -> None:
        if self._updating_page_ui or self._doc.pageCount() <= 0:
            return
        target = max(0, min(self._doc.pageCount() - 1, value - 1))
        self._nav.jump(target, QPointF(0, 0), self._nav.currentZoom())

    def _on_current_page_changed(self, page: int) -> None:
        self._updating_page_ui = True
        try:
            self._page_spin.setValue(page + 1)
        finally:
            self._updating_page_ui = False

    # ------------------------------------------------------------ zoom

    def _on_zoom_changed(self, index: int) -> None:
        from PySide6.QtPdfWidgets import QPdfView

        if index == 0:
            self._view.setZoomMode(QPdfView.ZoomMode.FitInView)
        elif index == 1:
            self._view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        else:
            preset_index = index - 2
            if 0 <= preset_index < len(_PDF_ZOOM_PRESETS):
                _label, factor = _PDF_ZOOM_PRESETS[preset_index]
                self._view.setZoomMode(QPdfView.ZoomMode.Custom)
                self._view.setZoomFactor(factor)

    # ------------------------------------------------------------ events

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.key() == Qt.Key_PageDown:
            self._jump_relative(1)
            event.accept()
            return
        if event.key() == Qt.Key_PageUp:
            self._jump_relative(-1)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        # Mirror ImageView's edge-aware navigation: forward to siblings
        # when the embedded scroll area is already at its travel limit in
        # the wheel's direction.  Otherwise let the event bubble to the
        # QPdfView for intra-document scrolling.
        if navigate_at_edge(
            self._view.verticalScrollBar(), event,
            self.navigate_requested.emit,
        ):
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # 葉ビュー共通の 「既定アプリで開く / エクスプローラで開く / フルパスをコピー」.
        _popup_entry_menu(self, self._path, event.globalPos())
