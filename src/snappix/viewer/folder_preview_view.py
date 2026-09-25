"""Center pane: read-only folder preview (thumbnail + child-tile grid).

Shown when a single-click / scroll lands on a folder entry in the right
pane *without* drilling into it.  All disk I/O — directory scan, ``post.md``
parse, image decode — runs off the GUI thread (the injected
``ThumbnailLoader`` for child tiles, an embedded :class:`~.image_view.
ImageView` for the centre image, this view's own
:class:`~._runnable.GuardedStream` for the scan) so the GUI never freezes
while a slow NAS folder loads.

The child grid is a :class:`gallery_view.GalleryView` in square-grid mode —
the same rendering base as the main children panes — so tile painting,
DPR handling, placeholder glyphs and hit testing are shared rather than
re-implemented on a raw ``QListWidget`` (whose ``setUniformItemSizes``
promise this view used to depend on, with undefined behaviour whenever the
per-item geometry drifted).

Thumbnail data flow:

* **Child tiles** go through an injected :class:`~.thumbnail_loader.
  ThumbnailLoader` (``ViewerWindow`` builds a small dedicated instance —
  same shape as the lightbox filmstrip loader — and threads it in via
  ``ContentView.set_folder_thumbnail_loader``).  That buys the persistent
  disk-cache tier (a session revisit paints from local disk instead of
  re-reading the NAS), a bounded 2-worker pool, queue cancellation on
  folder switch, and the closeEvent drain — none of which a view-local
  ``QThreadPool.globalInstance()`` decode would give.
* **The centre image** is a third :class:`~.image_view.ImageView`
  instance (the lightbox's reuse shape: control bar off), not a hand-rolled
  label: a private ``QLabel`` + QImage LRU + hand-written DPR / no-upscale /
  failure display would duplicate ImageView and drift from it one side at
  a time.  Embedding ImageView makes superseding decode, the error card, F03, DPR
  and the settings-wired cache budget (``ContentView`` fans
  ``image_view.apply_state`` out to this instance too) the same code as
  the centre preview's.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never, cast

from loguru import logger
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import FONT_BODY_PT, FONT_SUBTITLE_PT, hint_style
from ._runnable import GuardedStream, StreamJob, StreamOutcome
from .children_grid import build_tile
from .edge_nav import navigate_on_wheel
from .folder_scan import (
    IMAGE_SUFFIXES,
    next_representative,
    read_folder_preview_checked,
    scan_children,
)
from .gallery_view import GalleryView, Tile
from .image_view import ImageView
from .justified_layout import LayoutParams
from .representative_fallback import RepresentativeFallback

if TYPE_CHECKING:  # annotation only — no runtime dependency edge
    from .thumbnail_loader import ThumbnailLoader


#: フォルダプレビューの off-thread 仕事の結末（走査 2 種 + 次候補探索）:
#:
#: * ``scan`` — ``(thumb_path: Path | None, entries: list[FolderEntry])``
#: * ``scan_failed`` — エラーメッセージ
#: * ``fallback`` — ``found: Path | None``。代表画像のデコード失敗
#:   （``ImageView.load_failed``）を受けて ``next_representative`` で探した次候補。
#:
#: 子タイルのデコードはここを通らない — 注入された
#: :class:`~.thumbnail_loader.ThumbnailLoader` の ``loaded`` / ``failed``
#: をビューが直接受ける。中央画像のデコードは埋め込んだ
#: :class:`~.image_view.ImageView` の自前ストリームが持つ。
_PreviewKind = Literal["scan", "scan_failed", "fallback"]


def _scan_folder_preview(job: StreamJob, folder: Path) -> StreamOutcome | None:
    """Off-thread ``scan_children`` + thumbnail-path resolution (純関数).

    Runs both passes sequentially because they share a working-set
    folder and the second is cheap once the first has warmed the OS
    cache.  Any error is reported as ``scan_failed`` so the GUI can
    fall back gracefully on permission / network failures.

    協調キャンセル:フォルダ切替と窓の close でストリームが
    ``cancel`` され、走行中の scandir / post.md 読みが**次のチェックポイント
    で**止まる。これが無いと ``QThreadPool`` のデストラクタが走行中の
    runnable を無期限に待ち、到達不能な NAS を選んだ直後に閉じるとプロセス
    終了が SMB タイムアウトぶん遅れる。
    """
    cancelled = job.cancel.is_cancelled
    if cancelled():
        return None
    try:
        _parsed, marker, other, _file_names, _ok = read_folder_preview_checked(
            folder, should_cancel=cancelled,
        )
    except Exception as exc:  # noqa: BLE001 — worker boundary
        return None if cancelled() else StreamOutcome("scan_failed", str(exc))
    if cancelled():
        return None
    thumb_path = marker if marker is not None else other
    try:
        entries = scan_children(folder, should_cancel=cancelled)
    except Exception as exc:  # noqa: BLE001 — worker boundary
        return None if cancelled() else StreamOutcome("scan_failed", str(exc))
    if cancelled():
        return None
    return StreamOutcome("scan", (thumb_path, entries))


def _find_next_representative(
    job: StreamJob, folder: Path, skip: frozenset[Path],
) -> StreamOutcome | None:
    """デコードできなかった代表画像を除いた次の候補を探す (純関数)."""
    found = next_representative(folder, skip, job.cancel.is_cancelled)
    return None if job.cancel.is_cancelled() else StreamOutcome("fallback", found)


class FolderPreviewView(QWidget):
    """Read-only preview of a sub-folder: thumbnail + child tiles grid.

    Shown when scroll / single-click in the right pane lands on a folder
    entry, *without* drilling into it.  Double-click is the only path that
    still triggers root navigation (see ``main_window.py``).

    The centre is a stack of two pages — the embedded :class:`ImageView`
    (every image state: placeholder, decode, fit, error card) and a single
    notice label (scanning / no thumbnail / non-image / scan failure).
    Which one is current *is* the centre's display state, so no repaint
    path can resurrect a stale pixmap over a notice.

    The directory scan and the representative fallback probe share one
    superseding :class:`~._runnable.GuardedStream` owned by this view;
    :meth:`set_folder` / :meth:`clear` / :meth:`shutdown` fold it (queued
    work dropped, the running scan told to bail via ``job.cancel``).

    Wheel handling: the internal child grid consumes wheel notches itself
    (``GalleryView.wheelEvent`` accepts them) and the centre ImageView
    turns a fit-mode wheel into ``navigate_requested``, so this
    view's own :meth:`wheelEvent` only sees the surrounding chrome — routed
    through ``edge_nav.navigate_on_wheel``.
    """

    navigate_requested = Signal(int, bool)  # (delta, immediate)

    _MAX_CHILDREN = 60
    _CHILD_ICON_PX = 96  # logical pixels — DPR applied at decode time
    # Loader-key namespace for child-tile requests.  The loader
    # instance is dedicated to this view today, but the prefix keeps its
    # keys collision-free if it is ever shared (same pattern as the
    # lightbox filmstrip's "lightbox:<id>:" prefix).
    _CHILD_KEY_PREFIX = "folderpreview:"
    # Child tiles are decoded at 2× the logical tile edge (pre-DPR) — the
    # loader multiplies by DPR itself — matching the old view-local
    # supersampled target so grid tiles stay crisp when the cell is
    # repainted slightly larger.
    _CHILD_REQUEST_EDGE = _CHILD_ICON_PX * 2
    # Caption strip under each child tile (two elided lines à la the main
    # grid panes; the tooltip carries the full path).
    _CHILD_CAPTION_H = 32
    # 走査中に通知ページへ出す種アイコン（右一覧の行アイコン）の上限（論理
    # px）。``QIcon.pixmap`` は元より大きくしないので、通知ラベルの中で
    # 拡大表示されることは無い — 描き直しの経路も持たない静止画。
    _SEED_NOTICE_PX = 192
    # ImageView の即時プレースホルダ（``set_thumbnail_provider``）へ渡す種の
    # 取り出しサイズ。ImageView がビューポートへ合わせて描くので大きめに。
    _SEED_PLACEHOLDER_PX = 1024
    # Splitter starts biased toward the centre thumbnail so the
    # enlarged-by-default behaviour matches "プレビュー時の中央画像を
    # 拡大できるようにしてください"; the user can still drag the handle
    # to give the children grid more space.
    _SPLITTER_INITIAL_RATIO: tuple[int, int] = (2, 1)  # thumb : grid

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._folder: Path | None = None
        # True after a scan failed for the current folder so a re-click on
        # the same folder is allowed to retry (the idempotency guard would
        # otherwise leave the "読み込み失敗" state stuck forever).
        self._last_scan_failed: bool = False
        # この画面が**自分で選んだ**中央画像（走査の代表 / その次候補 =
        # ``shown``）と、デコードに失敗した候補。中央プレビューと同じ帳簿で、
        # タイルで選んだ画像は ``shown`` に入れない（黙って差し替えない）。
        # フォルダ切替 / クリアで忘れる（差し替えられたファイルを再検証する）。
        self._fallback = RepresentativeFallback()
        # ``set_folder(placeholder_icon=…)`` の種（右一覧の行アイコン）。
        # 走査が選んだ代表の即時プレースホルダとして ImageView へ渡す。
        self._seed: QPixmap | None = None
        # Injected child-tile loader.  ``None`` until the host
        # threads one in (``ContentView.set_folder_thumbnail_loader``);
        # without it child tiles settle on their static placeholder glyph
        # instead of decoding (a standalone view never blocks on C03 dots).
        self._child_loader: "ThumbnailLoader | None" = None
        # 走査と次候補探索の専有ストリーム。どちらも
        # 「今のフォルダの最新の 1 本」だけが要るので追い越し投入
        # （``submit_job``）。``QThreadPool.globalInstance()`` を使わないのは、
        # フォルダ切替でキュー待ちを捨て、到達不能 NAS の走行中 scandir へ
        # 降りるよう伝えられるようにするため（``~QThreadPool`` の無期限待ち）。
        self._stream = GuardedStream(self)
        self._stream.bind(self._on_preview_landed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self._title = QLabel()
        self._title.setStyleSheet(
            f"font-size: {FONT_SUBTITLE_PT}pt; font-weight: bold;"
        )
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        # ----- Centre (top half of the splitter) ---------------------------
        # ライトボックスと同じ再利用形の ImageView（ホバーカプセルは出さない
        # — 前後送りは右一覧の選択が担い、この面は読むだけのプレビュー）。
        self._image = ImageView()
        self._image.setFrameShape(QFrame.NoFrame)
        self._image.set_control_bar_enabled(False)
        self._image.set_thumbnail_provider(self._placeholder_for)
        self._image.navigate_requested.connect(self._on_image_navigate)
        self._image.load_failed.connect(self._on_image_load_failed)
        self._notice = QLabel()
        self._notice.setAlignment(Qt.AlignCenter)
        self._notice.setWordWrap(True)
        self._notice.setStyleSheet(hint_style())
        self._centre = QStackedWidget()
        self._centre.setMinimumHeight(120)
        self._centre.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._centre.addWidget(self._image)
        self._centre.addWidget(self._notice)
        self._centre.setCurrentWidget(self._notice)

        # ----- Children grid container (bottom half of the splitter) -----
        grid_container = QWidget()
        grid_layout = QVBoxLayout(grid_container)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(4)
        self._children_label = QLabel(t("viewer.folder_preview_view.children_heading"))
        self._children_label.setStyleSheet(hint_style(font_pt=FONT_BODY_PT))
        grid_layout.addWidget(self._children_label)

        # Square-grid GalleryView: same rendering base as the main panes
        # (aspect-preserving thumb paint inside uniform square cells, dimmed
        # placeholder glyphs, folder badges) — no fixed-canvas compositing
        # or uniform-item-size promises to maintain.  NoFocus keeps this a
        # read-only preview: the window's ←/→ file navigation must never be
        # stolen by the embedded grid's own key handling.
        #
        # ``zoom_handler`` は渡さない（この面にサイズスライダが無いので
        # 変える先が無い）。渡さないと ``GalleryView`` は Ctrl+ホイールを
        # 消費せず ``event.ignore()`` で親へ流すので、ジェスチャは
        # :meth:`wheelEvent` の「ホイール = ファイル送り」へ落ちる — 席の
        # chrome と同じ意味になり、死に手にならない。
        self._grid = GalleryView()
        self._grid.setFocusPolicy(Qt.NoFocus)
        self._grid.configure(
            view_mode="icon",
            thumb_layout="square",
            params=LayoutParams(
                target_size=self._CHILD_ICON_PX,
                spacing=6,
                caption_height=self._CHILD_CAPTION_H,
            ),
        )
        # Clicking (selecting) a child image tile swaps the centre
        # thumbnail — staying in folder-preview mode rather than drilling
        # in.  Folder tiles are deliberately no-op (drilling is reserved
        # for double-click in the right-pane file list).
        self._grid.selection_changed.connect(self._on_child_selected)
        grid_layout.addWidget(self._grid, 1)

        # ----- Vertical splitter wraps centre + grid container -------------
        # Lets the user drag to enlarge either side; default is biased
        # toward the centre thumbnail per the user's request.
        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setHandleWidth(6)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._centre)
        self._splitter.addWidget(grid_container)
        # ``setStretchFactor`` governs how *future* resizes redistribute
        # space; ``setSizes`` pins the *initial* split.  Use both so the
        # first paint matches the requested ratio and later window
        # resizes preserve it proportionally.  Qt scales the setSizes
        # values to whatever total height the splitter ends up with, so
        # the absolute numbers here are just the desired ratio.
        self._splitter.setStretchFactor(0, self._SPLITTER_INITIAL_RATIO[0])
        self._splitter.setStretchFactor(1, self._SPLITTER_INITIAL_RATIO[1])
        self._splitter.setSizes([
            self._SPLITTER_INITIAL_RATIO[0] * 1000,
            self._SPLITTER_INITIAL_RATIO[1] * 1000,
        ])
        layout.addWidget(self._splitter, 1)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------------------------ API

    @property
    def centre_image(self) -> ImageView:
        """中央画像の :class:`ImageView`（設定の fan-out と書き戻しの配線口）.

        ``ContentView`` が ``image_view.apply_state`` / ``connect_state_writeback``
        を中央プレビュー・閲覧モードと同じ入口で通すためだけに公開する。
        """
        return self._image

    def set_thumbnail_loader(self, loader: "ThumbnailLoader | None") -> None:
        """Inject the shared child-tile :class:`ThumbnailLoader` .

        The host (``ViewerWindow`` → ``ContentView``) hands in a small
        dedicated loader wired to the persistent disk / folder caches.
        Child tiles are requested through it under the
        ``folderpreview:`` key namespace; its ``loaded`` / ``failed``
        signals route back into the grid.  The loader is drained by the
        window's ``_drain_loader_pools`` at close, so no decode outlives
        the caches it writes to.
        """
        if self._child_loader is not None:
            self._child_loader.loaded.disconnect(self._on_child_loader_loaded)
            self._child_loader.failed.disconnect(self._on_child_loader_failed)
        self._child_loader = loader
        if loader is not None:
            loader.loaded.connect(self._on_child_loader_loaded)
            loader.failed.connect(self._on_child_loader_failed)

    def set_folder(
        self,
        folder: Path,
        *,
        placeholder_icon: QIcon | None = None,
        force: bool = False,
    ) -> None:
        """Render *folder* asynchronously as a non-navigating preview.

        Returns immediately after spawning the scan worker; the grid and
        main thumbnail populate progressively as workers complete.

        *placeholder_icon* (optional) — a low-resolution thumbnail the
        caller already has on screen (typically the right-pane row's
        icon).  Shown in the centre while the scan runs, then handed to the
        ImageView as the representative's instant placeholder.

        *force* — re-scan even when *folder* matches the current one (e.g.
        an explicit refresh after new files were downloaded into it).
        """
        # Idempotency: if the same folder is being set again (e.g. the
        # scroll-driven ``folder_selected`` signal and the explicit
        # ``show_path`` call in ``_on_content_navigate`` both reach us),
        # leave the in-flight or already-rendered state alone — bumping
        # the generation would discard valid worker results and flash
        # the placeholder back over an already-resolved thumbnail.
        #
        # Two exceptions re-scan the same folder: an explicit ``force``
        # refresh, and recovery after the previous scan *failed* (a
        # transient network blip must not leave the "読み込み失敗" state
        # stuck until the user detours through another folder).
        if folder == self._folder and not force and not self._last_scan_failed:
            return

        # Cancel work for the folder we are leaving: queued child-tile
        # requests are dropped from the loader (in-flight decodes
        # can't be pre-empted, but their results land on keys the rebuilt
        # grid no longer holds, so ``set_thumb`` no-ops them).  ストリームの
        # ``cancel`` はキュー待ちの走査 / 次候補探索を捨て、走行中の 1 本にも
        # 降りるよう伝える。中央画像は ``clear_image`` が
        # ImageView の 3 本のストリームを畳む。
        if self._child_loader is not None:
            self._child_loader.discard_pending_outside(set())
        self._stream.cancel()
        self._image.clear_image()

        self._folder = folder
        self._last_scan_failed = False
        self._fallback.restart(folder)
        self._fallback.shown = None
        self._seed = None

        self._title.setText(folder.name)
        self._title.setToolTip(str(folder))

        # 走査中は通知ページに種アイコン（無ければ「読み込み中」）を出す。
        # 種は ``QIcon.pixmap`` の上限まで — 元より大きくはならず、通知
        # ラベルはこれを描き直さない（走査の着地がページごと差し替える）。
        seed_notice = QPixmap()
        if placeholder_icon is not None and not placeholder_icon.isNull():
            seed = placeholder_icon.pixmap(
                self._SEED_PLACEHOLDER_PX, self._SEED_PLACEHOLDER_PX,
            )
            if not seed.isNull():
                self._seed = seed
                seed_notice = placeholder_icon.pixmap(
                    self._SEED_NOTICE_PX, self._SEED_NOTICE_PX,
                )
        if not seed_notice.isNull():
            self._show_notice_pixmap(seed_notice)
        else:
            self._show_notice(t("common.status.loading"))

        self._children_label.setText(
            t("viewer.folder_preview_view.children_heading_loading")
        )
        self._grid.clear()

        self._stream.submit_job(
            lambda job, f=folder: _scan_folder_preview(job, f)
        )

    def clear(self) -> None:
        # ``cancel`` invalidates any in-flight worker result and drops the
        # queued (not-yet-started) work outright.
        self._stream.cancel()
        self._image.clear_image()
        if self._child_loader is not None:
            self._child_loader.discard_pending_outside(set())
        self._folder = None
        self._last_scan_failed = False
        self._fallback.restart(None)
        self._fallback.shown = None
        self._seed = None
        self._title.clear()
        self._title.setToolTip("")
        self._show_notice("")
        self._children_label.setText(t("viewer.folder_preview_view.children_heading"))
        self._grid.clear()

    def suspend_animation(self) -> None:
        """中央のアニメーション画像を隠れている間だけ止める（:meth:`resume_animation` と対）.

        このページは離れても ``set_folder`` の同一パス早道で戻る（再表示が
        ``show_image`` を通らない）ので、``pause_animation`` ではなく
        退避の対（止めた側が戻す）を使う。
        """
        self._image.suspend_animation()

    def resume_animation(self) -> None:
        """:meth:`suspend_animation` が止めた再生だけを再開する."""
        self._image.resume_animation()

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """走行中のプレビュー読みを止めてストリームを有界に空にする.

        ``ViewerWindow.closeEvent`` が ``_zip_drill`` / ``_cache_ctrl`` /
        サムネローダーに掛けている close 前ドレインの、このビュー版。
        ``QThreadPool`` のデストラクタは走行中の ``QRunnable`` が終わるまで
        呼び出しスレッドを**無期限に**ブロックするので、これが無いと到達
        不能な NAS フォルダを選んだ直後に閉じたとき、窓が消えた後の破棄
        シーケンス（``viewer/app.py`` の局所変数解放）が SMB タイムアウト
        ぶん止まる。「協調キャンセル → キュー破棄 → 有界待ち」は
        :meth:`~._runnable.GuardedStream.request_shutdown` の 1 本に畳んで
        あり、待ちは有界 — 諦めてもワーカーはキャッシュへ書かない
        （``read_folder_preview_checked`` / ``scan_children`` /
        ``next_representative`` は読み取りだけ）ので、閉じたストアへの書き込み
        事故にはならない。中央画像のデコードは ``clear_image`` が ImageView の
        ストリームを畳む（窓の close 前ドレインは子孫の ``GuardedStream`` を
        全部拾うので、そちらの有界待ちもそこで掛かる）。
        """
        self._image.clear_image()
        if not self._stream.request_shutdown(max(0, timeout_ms)):
            logger.warning(
                "フォルダプレビューのワーカーが {}ms で終わりませんでした",
                timeout_ms,
            )

    # ----------------------------------------------------------- centre

    def _show_notice(self, text: str) -> None:
        """中央を通知ページ（文字だけ）にする."""
        self._notice.setPixmap(QPixmap())
        self._notice.setText(text)
        self._centre.setCurrentWidget(self._notice)

    def _show_notice_pixmap(self, pixmap: QPixmap) -> None:
        """中央を通知ページ（走査中の種アイコン）にする."""
        self._notice.setText("")
        self._notice.setPixmap(pixmap)
        self._centre.setCurrentWidget(self._notice)

    def _show_centre_image(self, path: Path) -> None:
        """*path* を中央の ImageView で表示する（画像でなければ通知へ）."""
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            # Non-image candidate (e.g. PDF first page) — notice only.
            self._image.clear_image()
            self._show_notice(
                t("viewer.folder_preview_view.thumb_non_image", name=path.name)
            )
            return
        self._centre.setCurrentWidget(self._image)
        self._image.show_image(path)

    def _placeholder_for(self, path: Path) -> QPixmap | None:
        """ImageView の即時プレースホルダ（手元に既にある低解像度の絵）.

        走査の代表には右一覧の行アイコン（種）、タイルで選んだ画像には
        そのタイルのサムネイル。どちらも I/O 無し。
        """
        if self._seed is not None and path == self._fallback.shown and not (
            self._fallback.skip
        ):
            return self._seed
        idx = self._grid.index_of_key(str(path))
        tile = self._grid.tile_at(idx) if idx is not None else None
        if tile is not None and tile.pixmap is not None and not tile.pixmap.isNull():
            return tile.pixmap
        return None

    def _on_image_navigate(self, delta: int, immediate: bool) -> None:
        """中央 ImageView のフィット時ホイール送りを面の送りへ流す."""
        self.navigate_requested.emit(delta, immediate)

    def _on_image_load_failed(self, path: Path) -> None:
        """この面が選んだ代表画像が壊れていた — 次の候補を探す.

        規則は中央プレビューと共有の :class:`~.representative_fallback.
        RepresentativeFallback` が決める。予算を使い切ったら最後の失敗カードに
        落ち着く。
        """
        folder = self._folder
        skip = self._fallback.on_failed(folder, path)
        if skip is None or folder is None:
            return
        self._stream.submit_job(
            lambda job, f=folder, s=skip: _find_next_representative(job, f, s)
        )

    # ----------------------------------------------------------- slot impls

    def _on_preview_landed(self, payload: object) -> None:
        """走査 / 次候補探索の着地（3 つの結末を受ける 1 本口）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_PreviewKind, payload.kind)
        match kind:
            case "scan":
                scan = cast("tuple[Path | None, list]", payload.value)
                self._apply_scan(*scan)
            case "scan_failed":
                self._apply_scan_failed(cast(str, payload.value))
            case "fallback":
                self._apply_fallback(cast("Path | None", payload.value))
            case _:
                assert_never(kind)

    def _apply_scan(
        self,
        thumb_path: Path | None,
        entries: list,
    ) -> None:
        # Kick off the centre image (or a notice) as soon as we know the
        # path.  It races the child-tile decodes.
        if thumb_path is None:
            self._image.clear_image()
            self._show_notice(t("viewer.folder_preview_view.thumb_none"))
        else:
            self._fallback.shown = thumb_path
            self._show_centre_image(thumb_path)

        # Order: directories first (alphabetical), then files
        # (alphabetical).  ``scan_children`` returns mixed entries with
        # placeholder titles for folders — entry.title is just the
        # filename in that case, so a case-insensitive sort is enough.
        dirs = sorted(
            (e for e in entries if e.is_dir),
            key=lambda c: c.title.lower(),
        )
        files = sorted(
            (e for e in entries if not e.is_dir),
            key=lambda c: c.title.lower(),
        )
        ordered = dirs + files

        shown_entries = ordered[: self._MAX_CHILDREN]
        skipped_count = len(ordered) - len(shown_entries)

        # Image children to request from the injected loader —
        # requested only after set_tiles so every tile is in place before
        # a synchronous cache-hit emit can post back.
        thumb_targets: list[tuple[str, Path]] = []
        # Tiles no decode will ever settle: sub-folders (their
        # representative image resolution is a panes-side concern), files
        # ``scan_children`` tagged as thumbnailable that this preview
        # doesn't decode (PDF / video), and — with no loader injected —
        # image files too.  ``GalleryView`` treats a tile with a thumbnail
        # source — or an unresolved folder — as "a decode is in flight"
        # and paints the 「読み込み中」 dots until the host settles it, so
        # say so up front or they stay pending forever (C03).
        never_decoded: list[str] = []
        tiles: list[Tile] = []
        for entry in shown_entries:
            path = entry.path
            key = str(path)
            # エントリ → タイルの写像は席をまたぐ 1 実装（``build_tile``）。
            # この席が決めるのは名前空間・キャプション・ツールチップだけで、
            # 「薄く描く」「代替サムネイルの警告枠」等の表示属性は左右ペインと
            # 同じ規則で決まる（手組みだと片側だけ欠ける形が繰り返し出る）。
            tiles.append(build_tile(
                entry, key=key, caption=path.name, tooltip=str(path),
            ))
            if (
                not entry.is_dir
                and path.suffix.lower() in IMAGE_SUFFIXES
                and self._child_loader is not None
            ):
                thumb_targets.append((key, path))
            else:
                never_decoded.append(key)
        self._grid.set_tiles(tiles)
        for key in never_decoded:
            self._grid.mark_thumb_failed(key)

        if not shown_entries:
            self._children_label.setText(
                t("viewer.folder_preview_view.children_heading_empty")
            )
        elif skipped_count > 0:
            self._children_label.setText(
                t(
                    "viewer.folder_preview_view.children_heading_truncated",
                    shown=len(shown_entries),
                    skipped=skipped_count,
                )
            )
        else:
            self._children_label.setText(
                t("viewer.folder_preview_view.children_heading")
            )

        # Child-tile thumbnails via the injected loader: the
        # loader owns caching (in-memory LRU + persistent disk masters),
        # DPR scaling and the bounded worker pool.  A cache hit emits
        # ``loaded`` synchronously on this same stack — tiles are already
        # set above, so the slot lands correctly.
        if self._child_loader is not None and thumb_targets:
            size = QSize(self._CHILD_REQUEST_EDGE, self._CHILD_REQUEST_EDGE)
            dpr = self._effective_dpr()
            for key, path in thumb_targets:
                loader_key = self._CHILD_KEY_PREFIX + key
                self._child_loader.request(loader_key, path, size, dpr=dpr)
                idx = self._grid.index_of_key(key)
                tile = self._grid.tile_at(idx) if idx is not None else None
                if tile is not None and not tile.thumb_loaded:
                    # ソースがボックスより小さいキーの再要求にローダーは
                    # 沈黙する（呼び出し側が既に絵を
                    # 持っている前提）。作り直したタイルは持っていないので、
                    # 常駐デコードがあれば ``cached_image`` から再シードする
                    # （他ホストと同じ流儀）。
                    image = self._child_loader.cached_image(loader_key)
                    if image is not None and not image.isNull():
                        self._grid.set_thumb(key, QPixmap.fromImage(image))

    def _apply_scan_failed(self, message: str) -> None:
        # Mark the failure so a re-click on the same folder retries rather
        # than being short-circuited by the idempotency guard in set_folder.
        self._last_scan_failed = True
        logger.warning("Folder preview scan failed: {}", message)
        self._children_label.setText(
            t(
                "viewer.folder_preview_view.children_heading_scan_failed",
                message=message,
            )
        )
        # 通知ページへ切り替えるだけで失敗表示が確定する — 種アイコンを
        # 描き直す経路はもう無い（表示状態はページ 1 つ）。
        self._image.clear_image()
        self._show_notice(t("viewer.folder_preview_view.thumb_load_failed"))

    def _apply_fallback(self, found: Path | None) -> None:
        """次候補探索の着地（フォルダ切替・タイル選択はストリームの cancel で
        古い着地を捨てる）。``None`` = 候補が尽きた — 最後の失敗カードを残す."""
        if found is None:
            return
        self._fallback.shown = found
        self._show_centre_image(found)

    def _on_child_loader_loaded(self, key: str, image: QImage) -> None:
        """A child-tile decode from the injected loader landed.

        Staleness needs no generation counter here: keys embed the child's
        absolute path, and ``GalleryView.set_thumb`` no-ops on a key the
        current grid doesn't hold — a late result from a folder the user
        already left simply misses.
        """
        if not key.startswith(self._CHILD_KEY_PREFIX):
            return  # not ours (a shared loader serving another consumer)
        if image is None or image.isNull():
            return
        tile_key = key[len(self._CHILD_KEY_PREFIX):]
        self._grid.set_thumb(tile_key, QPixmap.fromImage(image))

    def _on_child_loader_failed(self, key: str) -> None:
        """Settle a child tile whose decode failed (C03).

        Without this a broken / truncated image keeps painting the
        「読み込み中」 dots for the whole session — ``_tile_awaits_thumb``
        only clears once the host reports success or failure.
        """
        if not key.startswith(self._CHILD_KEY_PREFIX):
            return
        self._grid.mark_thumb_failed(key[len(self._CHILD_KEY_PREFIX):])

    def _on_child_selected(self, index: int) -> None:
        """User clicked a child tile — promote it to the centre image.

        Folder tiles and non-image files are no-ops.  For images, the
        tile's already-decoded grid thumbnail is the ImageView's instant
        placeholder (:meth:`_placeholder_for`) while the full decode runs.
        """
        tile = self._grid.tile_at(index)
        if tile is None or tile.is_dir:
            return
        path = tile.path
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            return
        # 同じタイルの再選択は ImageView の LRU の早道で即座に返るので、
        # 「もう中央にある」の手書きガードは持たない（失敗した画像なら
        # 再読み込みになる — 失敗カードの [再読み込み] と同じ意味）。
        # ユーザーが明示的に選んだ画像 — 失敗しても黙って差し替えない。
        # 飛行中の次候補探索も捨てる（着地が選んだ画像を奪い返さない）。
        # タイルは走査の着地後にしか無いので、捨てるのは次候補探索だけ。
        self._fallback.shown = None
        self._stream.cancel()
        self._show_centre_image(path)

    # ----------------------------------------------------------- internals

    def _effective_dpr(self) -> float:
        """DPR that respects both the widget and its screen.

        ``QWidget.devicePixelRatioF`` returns 1.0 until the widget has
        actually been mapped to a screen, which would silently make
        every initial child-tile decode soft on HiDPI displays.  Falling
        back to the screen's own ratio keeps quality correct on the first
        paint, and harmlessly reports the higher value when both agree.
        """
        widget_dpr = self.devicePixelRatioF()
        screen = self.screen() or QApplication.primaryScreen()
        screen_dpr = screen.devicePixelRatio() if screen is not None else 1.0
        return max(widget_dpr, screen_dpr) or 1.0

    def closeEvent(self, event):  # noqa: N802 (Qt API)
        """ペインを閉じる = 背景の読みを止める（``ChildrenGrid`` と同型）.

        ``shutdown`` は窓の ``closeEvent`` からしか届いていなかったので、窓を
        伴わずにペインだけ閉じると専用プールの走行中ワーカーが残り、C++ の
        デストラクタが**無期限に**それを待つ（``~QThreadPool``）。``shutdown``
        は純粋なキャンセルで冪等なので、ここでも呼んで二重に困ることはない。
        """
        self.shutdown()
        super().closeEvent(event)

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        # Wheel events that reach the view itself (cursor over the title,
        # the centre notice or children-label area — anywhere *outside*
        # the grid and the centre ImageView) always advance the right-pane
        # selection, mirroring :class:`FileInfoView`'s "any wheel = navigate"
        # semantics.  The children :class:`GalleryView` consumes plain
        # wheels internally for its own scrolling, so this normally only
        # triggers on the surrounding chrome — matching the user's intent of
        # "scroll outside the list → move to the next file like a normal
        # image preview".  Ctrl+wheel over the grid also lands here: that
        # seat takes no ``zoom_handler`` (no size slider to move), so the
        # grid declines the gesture instead of swallowing it, and the seat's
        # own meaning wins.
        if navigate_on_wheel(event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)
