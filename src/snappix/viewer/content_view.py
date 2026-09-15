"""Center pane: stacked Markdown / image / file-info preview.

This module hosts :class:`ContentView` — the ``QStackedWidget`` that switches
between every preview mode — and the suffix routing (:func:`has_dedicated_view`
/ :meth:`ContentView.show_path`) that must stay in lockstep with it.  Each leaf
view lives in its own module under :mod:`.content`:

* :class:`FileInfoView` — :mod:`.content.file_info_view`
* :class:`PdfView` — :mod:`.content.pdf_view`
* :class:`ZipView` — :mod:`.content.zip_view`
* :class:`TextView` — :mod:`.content.text_view`
* 空 / 歓迎カード 4 種 — :mod:`.content.empty_views`

The larger views live next to this module and are imported (and re-exported
below for backward compatibility):

* :class:`MarkdownView` — :mod:`.markdown_view`
* :class:`ImageView` — :mod:`.image_view`
* :class:`MediaView` — :mod:`.media_view`
* :class:`FolderPreviewView` — :mod:`.folder_preview_view`

Runtime-mutable preview settings (scroll speed, wheel-nav grace, text /
ZIP preview caps) and the shared wheel/format helpers live in
:mod:`.view_prefs`; the ``set_*`` / ``get_*`` accessors are re-exported here
so existing importers (``main_window.py``) keep working unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from . import view_prefs
from .content._shared import _build_entry_menu, _popup_entry_menu
from .content.empty_views import (
    _EmptyFolderView,
    _EmptyView,
    _MaximizedEmptyView,
    _WelcomeView,
)
from .content.file_info_view import FileInfoView
from .content.pdf_view import PdfView, _PdfTooLarge, _read_pdf_bytes
from .content.text_view import (
    TEXT_SUFFIXES,
    TextView,
    _decode_text,
    _read_text_preview,
    _trim_incomplete_utf8_tail,
    _trim_incomplete_utf16_tail,
)
from .content.zip_view import ZipView
from .context_menus import CurationHooks
from .edge_nav import WheelNavGate
from .folder_preview_view import FolderPreviewView
from .folder_scan import (
    IMAGE_SUFFIXES,
    MEDIA_SUFFIXES,
    ZIP_DRILL_SUFFIXES,
)
from .image_view import (
    ImageView,
    apply_state as apply_image_view_state,
    connect_state_writeback as connect_image_view_writeback,
)
from .lightbox import CenterMessageOverlay
from .markdown_view import DEFAULT_FONT_PT as DEFAULT_MARKDOWN_FONT_PT, MarkdownView
from .pending import Pending
from .view_prefs import (
    get_pdf_preview_size_limit,
    get_preview_scroll_pixels,
    get_text_preview_max_bytes,
    get_wheel_nav_grace_ms,
    get_zip_preview_size_limit,
    set_pdf_preview_size_limit,
    set_preview_scroll_pixels,
    set_text_preview_max_bytes,
    set_wheel_nav_grace_ms,
    set_zip_preview_size_limit,
)

if TYPE_CHECKING:
    from .media_view import MediaView
    from .state import ViewerState


#: 遅延構築ビューの種別（``_lazy_view_failures`` のキー = ログの表示名）。
_LAZY_PDF = "PDF"
_LAZY_MEDIA = "メディア"


def has_dedicated_view(path: Path) -> bool:
    """Whether a *file* path routes to a real preview sub-view (L07).

    Kept in lockstep with ``ContentView.show_path``'s suffix routing — the two
    live in the same module for exactly that reason.  Every branch show_path
    dispatches to a dedicated view (markdown / image / PDF / ZIP / text /
    media) is mirrored here; everything else falls through to
    ``show_file_info`` (a bare metadata card) and returns False, so callers
    can hand those files to the OS default app instead of a dead preview.

    Directories are out of scope: they aren't files and route through
    ``show_folder`` / the drill-in handlers, never here.
    """
    suffix = path.suffix.lower()
    return (
        path.name.lower() == "post.md"
        or suffix == ".md"
        or suffix in IMAGE_SUFFIXES
        or suffix == ".pdf"
        or suffix in ZIP_DRILL_SUFFIXES
        or suffix in TEXT_SUFFIXES
        or suffix in MEDIA_SUFFIXES
    )


class ContentView(QWidget):
    """Stacks the three preview modes and exposes simple ``show_*`` slots."""

    #: 印の口（★ / あとで見る / ユーザータグ）。ウィンドウが
    #: :meth:`set_curation_hooks` で注入し、子ビューは
    #: ``curation_hooks_from_ancestors`` で読む。``None`` = 店なし。
    _curation_hooks: CurationHooks | None = None

    file_link_clicked = Signal(Path)  # forwarded from MarkdownView
    post_link_clicked = Signal(Path)  # 📁 downloaded-post jump from MarkdownView
    navigate_requested = Signal(int)  # ±1 — forwarded from sub-views
    # Re-emitted view-preference toggles so ``ViewerWindow`` can write them
    # back into its ``ViewerState`` (ContentView stays state-agnostic).  These
    # fire when the user flips an in-view control (context menu / control bar);
    # the settings dialog path goes the other way via ``apply_view_settings``.
    image_zoom_persist_toggled = Signal(bool)   # ImageView zoom-persist toggle
    image_minimap_toggled = Signal(bool)        # ImageView minimap toggle
    markdown_font_pt_changed = Signal(int)      # MarkdownView Ctrl+wheel font
    media_loop_toggled = Signal(bool)           # MediaView loop toggle
    media_volume_changed = Signal(int)          # MediaView volume slider (F07)
    media_playback_rate_changed = Signal(float)  # MediaView rate combo (N-136)
    # 「開いて閲覧」 on the ZIP preview (F08) — the window drills into the archive.
    zip_open_requested = Signal(Path)
    # Full dimensions (W, H) of the currently-previewed image, or (0, 0) when
    # a non-image page is shown.  Forwarded from ImageView for the status bar.
    image_info_changed = Signal(int, int)
    # The image decode for this path failed (error card is up) — forwarded
    # from ImageView so the window can retry with the next representative
    # candidate when *it* (not the user) picked the file (#84).
    image_load_failed = Signal(Path)
    # "この画像に類似を検索" (C-10 extension) — forwarded from ImageView (the
    # central image preview) and MarkdownView (inline post.md images).
    similar_search_requested = Signal(Path, object)
    # ImageView context menu 「全画面で表示 (F11)」 — the window opens the
    # fullscreen lightbox (閲覧モード) for the currently previewed image.
    image_fullscreen_requested = Signal()
    # Split-view redesign 2026-07: a still-image double-click while the
    # [grid | preview] split is showing means "maximise the preview column"
    # (forwarded from ImageView.maximize_requested; enabled via
    # ``set_double_click_maximize``).  The window toggles the preview focus.
    preview_maximize_requested = Signal()
    # 「フォルダを開く…」 on the first-run welcome card (A02) — the window
    # routes this to its root picker.
    open_folder_requested = Signal()
    # 「上の階層へ」 on the empty-folder card (UIレビュー #6) — the window
    # routes this to its go-up navigation.
    go_up_requested = Signal()
    # 「操作の基本 (F1)」 on the welcome card (E1) — the window opens the guide.
    help_requested = Signal()
    # [◧ 分割ビューに戻す (G)] on the maximised-empty card (N-85) — the window
    # routes this to the same ``_exit_stage_to_browse`` G / Esc / ヘッダー use,
    # so the history symmetry of leaving the maximised preview is unchanged.
    restore_split_requested = Signal()
    # Digit 0–5 star rating for the previewed image (UIレビュー #11): forwarded
    # from ImageView (focus inside the image) and also caught by this widget's
    # own keyPressEvent (stage mode focuses the ContentView itself), so the
    # star keys work anywhere on the stage.  The window persists to user_meta.
    star_key_requested = Signal(int)
    # 最大化プレビューを全画面（閲覧モード）とキー集合で揃えるための 2 本
    # （UIレビュー 07-25 #22）。Space = 次の画像は既存の ``navigate_requested``
    # を +1 で再利用するので新設不要。``jump_edge_requested`` は Home/End で、
    # 引数 True = 末尾 / False = 先頭。母集合（現在フォルダのファイル一覧）は
    # ウィンドウが持つため、ここは意図だけを伝えるダムなシグナル。
    jump_edge_requested = Signal(bool)

    PAGE_EMPTY = 0
    PAGE_MARKDOWN = 1
    PAGE_IMAGE = 2
    PAGE_FILE = 3
    PAGE_PDF = 4
    PAGE_ZIP = 5
    PAGE_TEXT = 6
    PAGE_MEDIA = 7
    PAGE_FOLDER = 8
    PAGE_WELCOME = 9
    PAGE_EMPTY_FOLDER = 10
    #: 空フォルダ／空ライブラリの**静音**プレースホルダ (UIレビュー 07-25 #51)。
    #: 見出し + [フォルダを開く…] を持つ案内カード（9 / 10）はグリッド席が
    #: 畳まれている時だけ出し、分割中はこちらへ格下げする。空状態
    #: オーケストレータの ``SECONDARY`` 役の描画先でもある（N-101 — アイコン
    #: 無し 1 行。文言は割当ごとに :meth:`show_empty_secondary` が差し替える）。
    PAGE_EMPTY_QUIET = 11
    #: 最大化中に主案内の行き場が無いときのカード (N-85) — 幅 0 のグリッドを
    #: 指す案内の代わりに [◧ 分割ビューに戻す (G)] を出す。
    PAGE_EMPTY_MAXIMIZED = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # UIレビュー 07-25 #5: このコンテナは既定の ``Qt.NoFocus`` のままで、
        # ``setFocus()`` が**完全な no-op** だった（最大化時の
        # 「フォーカスをプレビューへ」が実際には何も起きていなかった根本原因の
        # もう半分）。フォーカスを受けられないページ（案内カード等）を表示中の
        # フォールバック先として、また自前 keyPressEvent（0-5 / Space /
        # Home / End）の受け皿として、明示的にフォーカスを受け取れるようにする。
        self.setFocusPolicy(Qt.StrongFocus)

        self._stack = QStackedWidget()
        self._empty = _EmptyView()
        self._welcome = _WelcomeView()
        self._welcome.open_folder_requested.connect(
            self.open_folder_requested.emit
        )
        self._welcome.help_requested.connect(self.help_requested.emit)
        # 静音プレースホルダ (UIレビュー 07-25 #51): グリッド席が見えている
        # 間の「空フォルダ」側。案内カード本体（見出し + 次の一手）は操作文脈
        # を持つグリッドが 1 枚だけ出し、こちらはアイコン + 1 行に留める。
        # N-101: 従属面は**アイコン無し 1 行**（主カードと同じ大きさの
        # フォルダアイコンが 3 面に並ぶのをやめる）。
        self._empty_quiet = _EmptyView(
            t("viewer.content_view.empty_quiet"),
            icon_name=None,
            emphasis="secondary",
        )
        self._empty_maximized = _MaximizedEmptyView()
        self._empty_maximized.restore_split_requested.connect(
            self.restore_split_requested.emit
        )
        self._empty_folder = _EmptyFolderView()
        self._empty_folder.open_folder_requested.connect(
            self.open_folder_requested.emit
        )
        self._empty_folder.go_up_requested.connect(self.go_up_requested.emit)
        self._markdown = MarkdownView()
        self._markdown.file_link_clicked.connect(self.file_link_clicked.emit)
        self._markdown.post_link_clicked.connect(self.post_link_clicked.emit)
        self._markdown.navigate_requested.connect(self._on_navigate_requested)
        self._markdown.font_pt_changed.connect(self.markdown_font_pt_changed.emit)
        self._markdown.similar_search_requested.connect(
            self.similar_search_requested.emit
        )
        self._image = ImageView()
        self._image.navigate_requested.connect(self._on_navigate_requested)
        # ビュー内トグル → ホストへの書き戻しは image_view 側の集約点を通す
        # （apply_state の対。全画面側の配線漏れ = N-78 を構造で塞ぐ）。
        connect_image_view_writeback(
            self._image,
            zoom_persist=self.image_zoom_persist_toggled.emit,
            minimap=self.image_minimap_toggled.emit,
        )
        self._image.image_info_changed.connect(self.image_info_changed.emit)
        self._image.load_failed.connect(self.image_load_failed.emit)
        self._image.similar_search_requested.connect(
            self.similar_search_requested.emit
        )
        self._image.fullscreen_requested.connect(
            self.image_fullscreen_requested.emit
        )
        self._image.maximize_requested.connect(
            self.preview_maximize_requested.emit
        )
        self._image.star_key_requested.connect(self.star_key_requested.emit)
        # ステージ演出 (redesign 2026-07 Phase 3-1): the central image preview
        # is always the "stage" (the browse grid lives elsewhere, markdown is a
        # document page), so give it the bg_stage backdrop + hairline frame.
        self._image.enable_stage_background()
        self._file_info = FileInfoView()
        self._file_info.navigate_requested.connect(self._on_navigate_requested)
        # PdfView is constructed lazily on first use (_ensure_pdf) for the same
        # reason as MediaView below: QtPdf / QtPdfWidgets must not be imported
        # at viewer startup.  ``PdfView.__init__`` already defers its own Qt
        # imports "so a headless build that never opens a PDF doesn't pay the
        # QtPdfWidgets cost on startup" — but constructing it here ran that
        # import for every launch, and with no ImportError guard a missing or
        # AV-quarantined Qt6Pdf DLL took the whole window down before it was
        # ever shown (windowed frozen builds have no stderr to say why).
        self._pdf: PdfView | None = None
        # Placeholder holds stack slot 4 until the real PdfView is built.
        self._pdf_placeholder = QWidget()
        # 遅延ビューの構築に失敗した種別（"PDF" / "メディア"）。初回だけ
        # スタックトレースを出すためのメモ — ``_lazy_view_unavailable``。
        self._lazy_view_failures: set[str] = set()
        self._zip = ZipView()
        self._zip.navigate_requested.connect(self._on_navigate_requested)
        self._zip.open_requested.connect(self.zip_open_requested.emit)
        self._text = TextView()
        self._text.navigate_requested.connect(self._on_navigate_requested)
        # MediaView is constructed lazily on first use (_ensure_media) so that
        # QtMultimedia is not imported at viewer startup — environments without
        # multimedia plugins would otherwise crash on construction.
        self._media: MediaView | None = None
        self._pending_media_state: Pending["ViewerState"] = Pending()
        # Placeholder holds stack slot 7 until the real MediaView is built.
        self._media_placeholder = QWidget()
        self._folder_preview = FolderPreviewView()
        self._folder_preview.navigate_requested.connect(
            self._on_navigate_requested
        )
        # Grace-period gating for at-edge wheel navigation — shared logic
        # with the fullscreen lightbox (see edge_nav.WheelNavGate).
        self._nav_gate = WheelNavGate()

        self._stack.addWidget(self._empty)             # 0
        self._stack.addWidget(self._markdown)          # 1
        self._stack.addWidget(self._image)             # 2
        self._stack.addWidget(self._file_info)         # 3
        self._stack.addWidget(self._pdf_placeholder)   # 4 — replaced by PdfView on demand
        self._stack.addWidget(self._zip)               # 5
        self._stack.addWidget(self._text)              # 6
        self._stack.addWidget(self._media_placeholder) # 7 — replaced by MediaView on demand
        self._stack.addWidget(self._folder_preview)    # 8
        self._stack.addWidget(self._welcome)           # 9
        self._stack.addWidget(self._empty_folder)      # 10
        self._stack.addWidget(self._empty_quiet)       # 11
        self._stack.addWidget(self._empty_maximized)   # 12

        # Stop media playback whenever we switch away from the media page.
        self._stack.currentChanged.connect(self._on_page_changed)

        layout.addWidget(self._stack)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 端到達の案内オーバーレイ (UIレビュー 07-25 #106) — 全画面（閲覧
        # モード）が「もう一度 → で次の投稿へ」と教えるのに、プレビュー最大化
        # では列の端で ←→ が完全に無反応だった。**全画面と同じ部品**
        # （``lightbox.CenterMessageOverlay``）を使って体裁を揃える。
        # ページの上に浮かせるので stack ではなくこのコンテナの子。
        self._message_overlay = CenterMessageOverlay(self)

    # ------------------------------------------------------------------ API

    def show_center_message(self, text: str) -> None:
        """中央に大きめの案内メッセージを一瞬出す（1.5 秒で自動消灯）。

        プレビュー最大化中の「列の端で ←→ を押した」等の状況説明用
        (UIレビュー 07-25 #106)。閲覧モードのタイトル/予告オーバーレイと同一
        の部品なので、2 つのモードで同じ見た目・同じ寿命になる。
        """
        self._message_overlay.show_message(text)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._message_overlay.reposition()

    def show_empty(self) -> None:
        self._stack.setCurrentIndex(self.PAGE_EMPTY)

    def show_welcome(self, *, default_library: bool = False) -> None:
        """Show the first-run welcome card (A02) instead of the empty hint.

        Used when the library root has settled with no entries — the plain
        "select a folder from the grid" hint has nothing to select, so the
        centre pane offers 「フォルダを開く…」 directly.  *default_library* adds
        the note explaining the auto-created 「library」 folder (UIレビュー #6).
        """
        self._welcome.set_default_library(default_library)
        self._stack.setCurrentIndex(self.PAGE_WELCOME)

    def show_empty_folder(self, *, can_go_up: bool = True) -> None:
        """Show the empty-folder card (UIレビュー #6) for a drilled-into
        folder with no entries — 「上の階層へ」 first, welcome card never.

        *can_go_up* False（ライブラリ境界）ではその主アクションを隠す
        (UIレビュー07-25 追修 #6 — :meth:`_EmptyFolderView.set_can_go_up`)。
        """
        self._empty_folder.set_can_go_up(can_go_up)
        self._stack.setCurrentIndex(self.PAGE_EMPTY_FOLDER)

    def show_empty_quiet(self) -> None:
        """空フォルダ時の**静音**プレースホルダを出す (UIレビュー 07-25 #51).

        グリッド席が見えている間、案内カード（見出し + [上の階層へ] /
        [フォルダを開く…]）はグリッド側の 1 枚だけが持つ。プレビュー列は
        「ここには出す物が無い」ことだけを静かに言う従属面に格下げする。

        文言は既定へ戻す — 同じページを :meth:`show_empty_secondary` が
        差し替えて使うため、戻さないと前の割当の 1 行が残る。
        """
        self.show_empty_secondary(t("viewer.content_view.empty_quiet"))

    def show_empty_secondary(self, text: str) -> None:
        """空状態オーケストレータの ``SECONDARY`` 割当を描く（提案3）.

        :meth:`show_empty_quiet` と同じ静音ページだが、文言をウィンドウ側の
        割当（:mod:`empty_state` の ``message_key``）で差し替える — 各ペインが
        自分で判定するのをやめ、割当を描くだけにするための入口。
        """
        self._empty_quiet.set_heading(text)
        self._stack.setCurrentIndex(self.PAGE_EMPTY_QUIET)

    def show_empty_maximized(self, *, scan_error: bool = False) -> None:
        """最大化中に主案内の行き場が無いときのカードを出す（N-85）.

        グリッド席が幅 0 の間は「グリッドから選んでください」が見えない面を
        指すので、代わりに [◧ 分割ビューに戻す (G)] を出して行き止まりを
        作らない。*scan_error* が真なら文面を走査失敗のものへ差し替える —
        退避してきた主案内が「選ばれていません」を名乗ると、失敗した事実が
        画面から消える。
        """
        self._empty_maximized.set_scan_error(scan_error)
        self._stack.setCurrentIndex(self.PAGE_EMPTY_MAXIMIZED)

    def showing_placeholder(self) -> bool:
        """True while the centre pane shows a placeholder card.

        All placeholder pages mean "nothing is selected/previewed", so the
        window may freely swap between them as the library's empty/non-empty
        state settles without clobbering a real preview.
        """
        return self._stack.currentIndex() in (
            self.PAGE_EMPTY, self.PAGE_WELCOME, self.PAGE_EMPTY_FOLDER,
            self.PAGE_EMPTY_QUIET, self.PAGE_EMPTY_MAXIMIZED,
        )

    def show_markdown(self, md_path: Path) -> None:
        self._markdown.set_post(md_path)
        self._stack.setCurrentIndex(self.PAGE_MARKDOWN)

    def show_image(self, path: Path) -> None:
        # ページ切替を**先に**行う（他の show_* と順序を揃える — 項目#70）。
        # 逆順だと、先読み済み（``_pil_cache`` ヒット）の画像は show_image の
        # 中で同期的に _on_loaded → _refresh まで走り切るため、まだ隠れて
        # いる ImageView 上で ``_is_pannable()``（スクロールバー範囲依存）が
        # 前の画像の値を返し、カーソル / ミニマップが取り違えられる。
        self._stack.setCurrentIndex(self.PAGE_IMAGE)
        self._image.show_image(path)

    def show_file_info(self, path: Path) -> None:
        self._file_info.show_file(path)
        self._stack.setCurrentIndex(self.PAGE_FILE)

    def _ensure_pdf(self) -> "PdfView":
        """Return the PdfView, constructing it on first call.

        Deferred construction keeps QtPdf / QtPdfWidgets out of the import
        graph until a PDF is actually opened — a build whose Qt6Pdf DLLs are
        missing then fails on that one preview instead of failing to start.

        構築は**ローカル変数で完成させてから** ``self._pdf`` へ代入する:
        先に代入すると、後続の connect / 差し替えで例外が出たときに壊れた
        インスタンスが残り、次回以降は ``is None`` を素通りして「無言の
        無反応」に戻る（``_ensure_media`` も同じ形）。

        構築を試みるかどうかの門は呼び出し側（:meth:`show_pdf`）が
        ``_lazy_view_failures`` で持つ。
        """
        if self._pdf is None:
            view = PdfView()
            view.navigate_requested.connect(self._on_navigate_requested)
            # Swap the placeholder out and insert the real widget at slot 4;
            # the following pages keep their indices (removeWidget+insertWidget).
            self._stack.removeWidget(self._pdf_placeholder)
            self._stack.insertWidget(self.PAGE_PDF, view)
            self._pdf = view
        return self._pdf

    def _lazy_view_unavailable(
        self, kind: str, path: Path, *, log_exception: bool = True
    ) -> None:
        """遅延ビューの構築失敗を記録し、ファイル情報ページへ退避する.

        遅延生成にしたことで、Qt6Pdf / QtMultimedia の欠落は「起動不能」から
        「そのプレビューだけ失敗」へ降格した。ただし Qt のスロット境界は
        未捕捉例外を握り潰して継続するため、退避が無いと中央ペインが前の
        内容のまま変わらない**無言の無反応**になる（凍結 windowed ビルドは
        stderr が無いので痕跡も残らない）。しかも PDF はフォルダ選択の
        代表ファイル経由の自動経路でも来るので、ユーザーが開いた覚えが
        無くても踏む。

        **スタックトレースは種別ごとに初回だけ**: 失敗は環境要因なので
        毎回同じ結果になるが、選択は動き続ける。毎回 ``logger.exception``
        を出すとログがトレースバックで埋まり、本当の初回が読めなくなる。

        記録した種別は**再構築の門**でもある（``show_pdf`` / ``show_media``
        が入口で見る）。DLL 欠落・AV 隔離はセッション中に回復しないので、
        ファイルを跨ぐたびに import と DLL 探索をやり直さない。
        """
        first = kind not in self._lazy_view_failures
        self._lazy_view_failures.add(kind)
        if first and log_exception:
            logger.opt(exception=True).error(
                "{} プレビューを初期化できません: {}", kind, path
            )
        else:
            logger.warning("{} プレビューは利用できません: {}", kind, path)
        self.show_file_info(path)

    def show_pdf(self, path: Path) -> None:
        if _LAZY_PDF in self._lazy_view_failures:
            # この環境では作れないことが分かっている（DLL 欠落 / AV 隔離は
            # セッション中に回復しない）。再試行は import と DLL 探索の
            # ぶんだけ送りを引っかけるので、記憶を信じて退避する。
            self._lazy_view_unavailable(_LAZY_PDF, path, log_exception=False)
            return
        try:
            view = self._ensure_pdf()
        except Exception:
            self._lazy_view_unavailable(_LAZY_PDF, path)
            return
        view.show_pdf(path)
        self._stack.setCurrentIndex(self.PAGE_PDF)

    def show_zip(self, path: Path) -> None:
        self._zip.show_zip(path)
        self._stack.setCurrentIndex(self.PAGE_ZIP)

    def show_text(self, path: Path) -> None:
        self._text.show_text(path)
        self._stack.setCurrentIndex(self.PAGE_TEXT)

    def _ensure_media(self) -> "MediaView | None":
        """Return the MediaView, constructing it on first call (``None`` = 構築失敗).

        Deferred construction keeps QtMultimedia out of the import graph
        until media playback is first needed — environments without
        multimedia plugins won't crash at viewer startup.

        構築とホストへの配線は :func:`~snappix.viewer.media_view.build_media_view`
        が一手に負う（項目#71）— 全画面側 ``LightboxWindow._ensure_media`` と
        手書きで重複していた 5 点（遅延 import / navigate_requested / loop /
        volume / rate の再送出 / 保留設定の当て込み）を 1 箇所へ寄せた集約点。
        MediaView への配線を増やすときは必ず工場関数側へ足すこと。
        """
        if self._media is None:
            from .media_view import build_media_view

            # ``_ensure_pdf`` と同じく、完成させてから ``self._media`` へ
            # 代入する（途中で例外が出ても壊れたインスタンスを残さない）。
            view = build_media_view(
                on_navigate=self._on_navigate_requested,
                on_loop=self.media_loop_toggled.emit,
                on_volume=self.media_volume_changed.emit,
                on_rate=self.media_playback_rate_changed.emit,
                pending_state=self._pending_media_state.peek(),
            )
            if view is None:
                return None
            # 構築が失敗した枝では降ろさない（次の試行へ持ち越す）。
            self._pending_media_state.clear()
            # Swap the placeholder out and insert the real widget at slot 7;
            # folder_preview (slot 8) index is preserved by removeWidget+insertWidget.
            self._stack.removeWidget(self._media_placeholder)
            self._stack.insertWidget(self.PAGE_MEDIA, view)
            self._media = view
        return self._media

    def show_media(self, path: Path) -> None:
        if _LAZY_MEDIA in self._lazy_view_failures:
            # 構築できないことが分かっている環境では作り直さない（PDF 側と
            # 同じ判断 — ``show_pdf``）。
            self._lazy_view_unavailable(_LAZY_MEDIA, path, log_exception=False)
            return
        view = self._ensure_media()
        if view is None:
            # PDF 側 (``show_pdf``) と対の退避 — 同じヘルパを通す。
            # QtMultimedia の欠落は QtPdf より起きやすい（マルチメディア
            # プラグインは環境差が大きい）。トレースバックは工場関数が
            # 既に出しているので、ここでは経路とパスだけ記録する。
            self._lazy_view_unavailable(_LAZY_MEDIA, path, log_exception=False)
            return
        view.show_media(path)
        self._stack.setCurrentIndex(self.PAGE_MEDIA)

    def show_folder(
        self,
        path: Path,
        *,
        placeholder_icon: "QIcon | None" = None,
    ) -> None:
        """Preview a directory without drilling into it.

        Renders the folder's thumbnail and a read-only tile grid of its
        immediate children.  The right-pane file list is untouched so
        siblings can still be reached via wheel / arrow navigation.

        *placeholder_icon* — if the caller already has a (low-res)
        thumbnail rendered for this folder (e.g. the right-pane row
        icon), pass it here to seed the centre thumbnail immediately
        while the high-res decode runs in the background.
        """
        self._folder_preview.set_folder(path, placeholder_icon=placeholder_icon)
        self._stack.setCurrentIndex(self.PAGE_FOLDER)

    def apply_media_settings(self, state: "ViewerState") -> None:
        if self._media is None:
            # MediaView not yet constructed — stash the state so _ensure_media
            # can apply it when the widget is first created.
            self._pending_media_state.set(state)
            return
        self._media.apply_settings(state)

    def apply_view_settings(self, state: "ViewerState") -> None:
        """Push preview view-preferences (image / markdown / media) from state.

        Called once at startup and again whenever the settings dialog commits.
        Covers ImageView zoom-persist + minimap, the MarkdownView body font
        (only when a non-zero size is persisted — ``set_font_pt`` clamps to
        8..32, so applying the ``0`` default would wrongly force 8pt), and the
        MediaView loop / autoplay / volume (routed through ``apply_media_settings``
        so the lazy-construction stashing is respected).
        """
        # ImageView 分（ズーム維持 / ミニマップ + キャッシュ）は共通 fan-out
        # （image_view.apply_state — 項目#29）へ委譲。閲覧モード側の 2 つ目の
        # インスタンスと同じ入口を通ることで、設定が増えても片側だけ取り
        # 残されない。
        apply_image_view_state(self._image, state)
        # 0 = アプリ既定に従う。設定ダイアログから既定へ戻せるように
        # なった (N-138) ので、0 のときも**既定へ引き戻す**必要がある
        # （従来は 0 を「何もしない」と読んでいたため、一度大きくすると
        # 設定側から既定へ戻しても表示が戻らなかった）。
        self._markdown.set_font_pt(
            state.markdown_font_pt or DEFAULT_MARKDOWN_FONT_PT
        )
        # MediaView reads media_loop alongside autoplay / volume; a single
        # apply_settings keeps the three in one place and honours the lazy
        # construction stash.
        self.apply_media_settings(state)
        # フォルダプレビューの中央画像も F03（等倍以上に拡大しない）の対象
        # （項目#61）。設定値は view_prefs のモジュール変数から live に読むが、
        # **表示中の 1 枚**はここで測り直さないと次のリサイズまで変わらない。
        self._folder_preview.refresh_fit()

    def copy_current_image(self) -> bool:
        """Copy the previewed image to the clipboard when one is shown.

        Returns ``True`` only when the image page is current *and* an image is
        loaded (so a menu action can report "nothing to copy" otherwise).  The
        actual clipboard write is ImageView's ``copy_image_to_clipboard``.
        """
        if self._stack.currentIndex() != self.PAGE_IMAGE:
            return False
        if not self._image.has_image():
            return False
        self._image.copy_image_to_clipboard()
        return True

    def clear_media_playback(self) -> None:
        """Stop and unload the current media file."""
        if self._media is not None:
            self._media.clear_media()

    def pause_media_playback(self) -> bool:
        """再生中の動画を一時停止する（アンロードはしない — 項目#30）.

        別ウィンドウ（全画面の閲覧モード）が同じファイルの 2 つ目の
        ``QMediaPlayer`` を持って前面に出るとき、中央プレビュー側を黙って
        鳴らし続けないための入口。``clear_media_playback`` と違って
        ソースと再生位置を保持するので、閉じて戻ったときに同じ位置から
        再開できる（再選択が起きない = 同じ選択のままでも中央が空にならない）。
        """
        if self._media is None:
            return False
        return self._media.pause_playback()

    def pause_image_animation(self) -> None:
        """中央の QMovie（アニメーション画像）を止める（全画面へ退避する口）.

        ``_on_page_changed`` の「画像ページを離れたら QMovie を止める」対は
        ページ切替でしか働かない。全画面（閲覧モード）は**別ウィンドウ**で
        ページを切り替えないので、同じアニメを 2 つの QMovie がデコードし
        続けていた（``pause_media_playback`` が動画について解いているのと
        同じ形の片側欠落）。復帰は ``show_image`` / 再選択が担う。
        """
        self._image.pause_animation()

    def media_playback_position(self) -> int:
        """一時停止した動画の再生位置 (ms)。全画面への引き継ぎ用 (N-142)。"""
        return 0 if self._media is None else self._media.playback_position()

    def media_current_path(self):
        """いま MediaView が開いているファイル（無ければ ``None``）— N-142."""
        return None if self._media is None else self._media.current_path()

    def seek_media(self, position_ms: int) -> None:
        """全画面から戻ったときの位置合わせ (N-142)。"""
        if self._media is not None:
            self._media.seek(position_ms)

    def set_image_siblings_provider(
        self,
        provider: Callable[[Path], tuple[list[Path], int]] | None,
    ) -> None:
        self._image.set_siblings_provider(provider)

    def set_image_thumbnail_provider(
        self,
        provider: Callable[[Path], QPixmap | None] | None,
    ) -> None:
        self._image.set_thumbnail_provider(provider)

    def set_post_link_resolver(
        self, resolve: Callable[[str], Path | None] | None,
    ) -> None:
        """Wire the MarkdownView's downloaded-post link resolver."""
        self._markdown.set_post_link_resolver(resolve)

    def set_folder_thumbnail_loader(self, loader) -> None:
        """Wire the FolderPreviewView's child-tile ThumbnailLoader (項目#14).

        Transparent pass-through, same decoupling pattern as the two image
        providers above — the window owns the loader (and drains it at
        close); this view only threads it to the folder preview.
        """
        self._folder_preview.set_thumbnail_loader(loader)

    def set_curation_hooks(self, hooks: CurationHooks | None) -> None:
        """プレビュー列の全ビューの右クリックに印の節を出す口を注入する。

        ウィンドウが user_meta 店を開けたときだけ渡す（無ければ ``None`` の
        まま = 節が出ない、グリッドと同じ劣化）。
        """
        self._curation_hooks = hooks

    def set_similar_search_available(self, available: bool) -> None:
        """Enable/disable "この画像に類似を検索" on both ImageView and
        MarkdownView (C-10 extension).  Transparent pass-through — see
        :meth:`ImageView.set_similar_search_available` /
        :meth:`MarkdownView.set_similar_search_available`.
        """
        self._image.set_similar_search_available(available)
        self._markdown.set_similar_search_available(available)

    def force_show_stage_capsule(self) -> None:
        """Pin the image stage's floating control capsule on screen.

        For offscreen tests + the screenshot harness (the hover-reveal fade
        and idle auto-hide can't be driven without a real event loop).  Only
        meaningful while the image page is current.  See
        :meth:`ImageView.force_show_control_bar`.
        """
        self._image.force_show_control_bar()

    def set_fullscreen_available(self, available: bool) -> None:
        """Offer 「全画面で表示 (F11)」 on the central ImageView's context menu.

        Only the main window's centre preview enables this (the lightbox's
        own internal ImageView keeps it off — it is already fullscreen).
        """
        self._image.set_fullscreen_available(available)

    def set_fullscreen_button_visible(self, visible: bool) -> None:
        """ホバーカプセルの全画面ボタンだけを出し入れする (UIレビュー 07-25 #104).

        最大化中はプレビューヘッダーの ``[⛶ 全画面 (F11)]`` と重複するため
        ホストが畳む。右クリックメニューの項目は残る。
        """
        self._image.set_fullscreen_button_visible(visible)

    def set_double_click_maximize(self, enabled: bool) -> None:
        """Route a still-image double-click to ``preview_maximize_requested``.

        Split-view redesign 2026-07 — the window enables this while the
        [grid | preview] split is showing (double-click = "make it big")
        and disables it while the preview is maximised (double-click =
        the historical fit⇄actual zoom toggle).  Pass-through to
        :meth:`ImageView.set_double_click_maximize`.
        """
        self._image.set_double_click_maximize(enabled)

    def refresh_markdown_links(self) -> None:
        """Re-classify the current post's links (after the index fills).

        Guarded to the markdown page so a scan completing while an image /
        other preview is shown doesn't trigger a hidden re-render.
        """
        if self._stack.currentIndex() == self.PAGE_MARKDOWN:
            self._markdown.refresh_links()

    def invalidate_image_sibling_cache(self) -> None:
        self._image.invalidate_sibling_cache()

    def shutdown_folder_preview(self, timeout_ms: int = 2000) -> None:
        """フォルダプレビューの専用プールを close 前に有界ドレインする（項目#136）.

        ``ViewerWindow.closeEvent`` の他のワーカードレイン（``_zip_drill`` /
        ``_cache_ctrl`` / サムネローダー）と同じ列に並べるための薄い委譲。
        """
        self._folder_preview.shutdown(timeout_ms)

    def image_is_cached(self, path: Path) -> bool:
        return self._image.is_cached(path)

    def image_decode_settled(self, path: Path) -> bool:
        """右ペインの pending スピナーを出さなくてよいか（項目#60）.

        ``ViewerWindow`` が ``FileListView.set_cache_check`` へ挿す述語。
        かつては :meth:`image_is_cached`（＝ ImageView の LRU 残留）をそのまま
        挿していたが、LRU は選択中 ± 先読み半径しか持たないため、半径の外の
        行は何も進行していないのにスピナーを回し続けていた。判定は
        ``ImageView.is_decode_pending`` の 1 箇所に寄せる。
        """
        return not self._image.is_decode_pending(path)

    @property
    def image_cache_updated(self):
        return self._image.cache_updated

    def apply_cache_settings(self, state: "ViewerState") -> None:
        """Push cache sizing from the persisted state into both subviews.

        Called once at startup and again whenever the settings dialog
        commits changes.  Converting MiB → bytes here keeps the view
        classes unaware of the UI-level units.
        """
        mib = 1024 * 1024
        self._markdown.reconfigure_cache(
            max_bytes=max(1, state.markdown_cache_max_mib) * mib,
            max_entries=max(1, state.markdown_cache_max_entries),
            max_single_bytes=max(1, state.markdown_cache_max_single_mib) * mib,
        )
        # ImageView 分は共通 fan-out（image_view.apply_state — 項目#29）へ。
        # キャッシュ以外の 2 項目も一緒に流れるが冪等なので害はない。
        apply_image_view_state(self._image, state)

    def show_path(self, path: Path) -> None:
        """Route a clicked file path to the appropriate sub-view.

        **拡張子で決まるものは I/O ゼロで振り分ける (レビュー 2026-07-31 #73)**:
        以前は先頭で無条件に ``path.is_dir()`` を呼んでいたが、この関数は
        右ペインの選択が動くたび（矢印キー連打・ホイール送り）に走るホット
        パスで、高遅延 NAS では 1 選択 = 1 stat ぶんフレームが止まっていた
        （同じ理由で FileInfoView / TextView の stat・読み取り
        （``GuardedStream``）や ``_read_zip_listing`` は軒並みワーカーへ移してある）。ディレクトリはプレビュー可能な拡張子
        を持たないので、``is_dir`` の判定は拡張子で決まらなかった場合の
        フォールバック分岐だけに残す。
        """
        suffix = path.suffix.lower()
        if path.name.lower() == "post.md" or suffix == ".md":
            self.show_markdown(path)
        elif suffix in IMAGE_SUFFIXES:
            self.show_image(path)
        elif suffix == ".pdf":
            self.show_pdf(path)
        elif suffix in ZIP_DRILL_SUFFIXES:
            self.show_zip(path)
        elif suffix in TEXT_SUFFIXES:
            self.show_text(path)
        elif suffix in MEDIA_SUFFIXES:
            self.show_media(path)
        elif path.is_dir():
            # Directories preview as folder thumbnail + child tiles — drilling
            # into them is reserved for explicit double-click handlers, never
            # for scroll-driven selection changes.  (本文リンク経由でフォルダ
            # が渡る経路があるのでここは残す — ``_on_file_link_clicked``。)
            self.show_folder(path)
        else:
            self.show_file_info(path)

    def _on_page_changed(self, index: int) -> None:
        if index != self.PAGE_MEDIA and self._media is not None:
            self._media.clear_media()
        # Leaving a leaf preview page → hand its resources back (レビュー
        # 2026-08-27 #121).  Each leaf already owns a symmetric teardown, but
        # nothing ever called it: PdfView kept the whole PDF in a QBuffer plus
        # the parsed QPdfDocument, ZipView up to 5000 tree rows and TextView up
        # to 2 MiB of body text — all resident until the *next* file of that
        # kind was opened.  Safe on the return trip: every ``show_*`` wrapper
        # reloads unconditionally (no same-path fast path), so a cleared page is
        # repopulated the moment it is shown again.
        #
        # FolderPreviewView is deliberately **not** in this list (追修正
        # 2026-08-27): unlike the others its ``set_folder`` *does* have a
        # same-path fast path, and ``clear()`` drops ``_folder`` — so clearing
        # here turns every X→画像→X round trip into a fresh worker scan of the
        # real directory plus a「読み込み中…」flash.  It would not even buy
        # anything: the memory that matters is the shared 256 MiB ``_thumb_cache``
        # which ``clear()`` never touches.
        if index != self.PAGE_PDF and self._pdf is not None:
            self._pdf.clear()
        if index != self.PAGE_ZIP:
            self._zip.clear()
        if index != self.PAGE_TEXT:
            self._text.clear_text()
        if index != self.PAGE_FILE:
            self._file_info.clear_file()
        # Same contract for the post.md page (レビュー 2026-08-27 #131): the
        # MarkdownView's decoded body images (a 256 MiB LRU at the ceiling plus
        # the QTextDocument resource cache) were released only when the *next*
        # post.md was opened, so an image-heavy post's pixels sat resident for
        # the rest of the session.  ``set_post`` re-reads and re-decodes
        # unconditionally even for the same path, so nothing was ever reused on
        # the way back — the retention was pure waste.
        if index != self.PAGE_MARKDOWN:
            self._markdown.release_images()
        # Leaving the image page → pause any animated GIF/WebP so its QMovie
        # stops decoding frames while hidden (MediaView already stops via
        # clear_media above; a hidden QMovie would otherwise burn CPU for as
        # long as the user browses other content), and hand back the Qt-side
        # pixel mirrors.  ``release_pixels`` は同じ対の残り: 原寸 QPixmap は
        # 8000×4000 で 128 MB あり、``_pil_cache`` のバイト予算にも入らない
        # 無予算の常駐だった。戻りは ``show_image`` が無条件に作り直すので
        # 抱えていても再利用はされない（他の葉と同じ契約）。
        if index != self.PAGE_IMAGE:
            self._image.release_pixels()
        # Leaving the image page → clear the status-bar resolution readout.
        # (ImageView only emits real dimensions on load, so a page switch that
        # doesn't reload it would otherwise leave a stale W×H showing.)
        if index != self.PAGE_IMAGE:
            self.image_info_changed.emit(0, 0)

    def focus_current_page(self) -> None:
        """現在ページのサブビューへキーボードフォーカスを移す（UIレビュー #5）。

        最大化時にこのコンテナへ setFocus していると、ImageView 配下スコープ
        (``WidgetWithChildrenShortcut``) の + / − / R / Shift+R / F が最大化
        直後に全滅する（画像を一度クリックすれば効くのでマウス併用者は
        気づかない）。フォーカスを受けられないページ（案内カード等）では
        コンテナ自身へ落とす — 0-5 / Space / Home / End はここの
        ``keyPressEvent`` が拾うので、どちらでもキーは死なない。
        """
        page = self._stack.currentWidget()
        if page is not None and page.focusPolicy() != Qt.NoFocus:
            page.setFocus()
        else:
            self.setFocus()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Stage mode focuses the current sub-view (UIレビュー 07-25 #5); keys
        # it doesn't accept propagate up to this container, so the handling
        # below covers both the focused-container and focused-sub-view cases.
        #
        # Digit 0–5 star keys (UIレビュー #11).  Restricted to the pages that
        # preview one media file (image / video): the star target is the
        # previewed file, exactly like the grid's "selected tile" gate.
        # Modifier gating mirrors GalleryView / ImageView (bare or
        # numpad-only).
        key = event.key()
        bare = event.modifiers() in (Qt.NoModifier, Qt.KeypadModifier)
        page = self._stack.currentIndex()
        on_image = page == self.PAGE_IMAGE
        # (UIレビュー 09-11 N-138) ★は動画ページでも打てる — 全画面
        # （``lightbox._handle_key``）はページを問わず ``_set_star`` するので、
        # 同じ動画で最大化のときだけ無反応になっていた。★の対象はファイルで
        # あって「画像であること」には依存しない。
        # **Space / Home / End は広げない**: 動画ページの Space は
        # ``MediaView`` の再生 / 一時停止（07-25 #18 の裁定）。
        starrable = page in (self.PAGE_IMAGE, self.PAGE_MEDIA)
        if Qt.Key_0 <= key <= Qt.Key_5 and bare and starrable:
            self.star_key_requested.emit(key - Qt.Key_0)
            event.accept()
            return
        # UIレビュー 07-25 #22: 最大化プレビューと全画面（閲覧モード）の
        # キー集合を揃える — Space（次の画像）/ Home / End は全画面にしか
        # 無かった。0-5 と同じ流儀で画像ページに限定する（メディアページの
        # Space は MediaView の再生/一時停止、テキスト系の Home/End は
        # キャレット移動なので奪わない）。
        if bare and on_image:
            if key == Qt.Key_Space:
                self.navigate_requested.emit(1)
                event.accept()
                return
            if key in (Qt.Key_Home, Qt.Key_End):
                self.jump_edge_requested.emit(key == Qt.Key_End)
                event.accept()
                return
        super().keyPressEvent(event)

    def _on_navigate_requested(self, delta: int, immediate: bool) -> None:
        # Grace-period gating for scrollable content: the first at-edge wheel
        # only *arms* the navigation; the user must keep scrolling in the
        # same direction for ``_WHEEL_NAV_GRACE_SEC`` before we switch files.
        # ``immediate`` (content fits the viewport) navigates right away.
        # The state machine itself lives in edge_nav.WheelNavGate (shared
        # with the fullscreen lightbox).
        if self._nav_gate.check(delta, immediate):
            self.navigate_requested.emit(delta)


# Backward-compatibility re-exports.  Historically every preview view and
# settings accessor lived in this module; the leaf views moved under
# ``content/`` and the larger views to their own files, but importers (tests,
# ``main_window.py``) still expect to find them here.  ``QPixmap`` is
# referenced by type hints above (resolved lazily under
# ``from __future__ import annotations``) and re-exported alongside the views
# for parity with the old single-module layout.
from PySide6.QtGui import QPixmap  # noqa: E402
from .media_view import MediaView  # noqa: E402,F401

__all__ = [
    "ContentView",
    "FileInfoView",
    "FolderPreviewView",
    "ImageView",
    "MarkdownView",
    "MediaView",
    "PdfView",
    "QIcon",
    "QPixmap",
    "TextView",
    "ZipView",
    "TEXT_SUFFIXES",
    "CurationHooks",
    "DEFAULT_MARKDOWN_FONT_PT",
    "IMAGE_SUFFIXES",
    "MEDIA_SUFFIXES",
    "ZIP_DRILL_SUFFIXES",
    "has_dedicated_view",
    "view_prefs",
    "_PdfTooLarge",
    "_build_entry_menu",
    "_decode_text",
    "_popup_entry_menu",
    "_read_pdf_bytes",
    "_read_text_preview",
    "_trim_incomplete_utf8_tail",
    "_trim_incomplete_utf16_tail",
    "get_pdf_preview_size_limit",
    "get_preview_scroll_pixels",
    "get_text_preview_max_bytes",
    "get_wheel_nav_grace_ms",
    "get_zip_preview_size_limit",
    "set_pdf_preview_size_limit",
    "set_preview_scroll_pixels",
    "set_text_preview_max_bytes",
    "set_wheel_nav_grace_ms",
    "set_zip_preview_size_limit",
]
