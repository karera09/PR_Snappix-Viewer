"""Center pane: stacked Markdown preview for ``post.md``.

Image references inside ``post.md`` use URL-encoded relative paths
(``./%E7%B5%B5.jpg``).  Qt's ``QTextBrowser`` resolves them against
``QUrl.fromLocalFile(post_dir + "/")``; when ``loadResource`` is reached
the URL is already decoded back to a local path.  We still override
``loadResource`` defensively so we can handle quoting edge cases and log
unresolved references.

Markdown → HTML の変換・画像参照の解決・面の骨格（テンプレートとメタカード）
は :mod:`~snappix.viewer.markdown_pipeline` にある純関数群で、このモジュール
（ビューとワーカータスク）と共有する。このモジュールは分割前の名前を全て
re-export するので、外から見た import 面は分割前と変わらない。**モジュール
グローバルを差し替えるテストは実体のあるモジュールへ当てること**
（``current_tokens`` を差し替えて ``_theme_style_args`` を見るテストは
``markdown_pipeline`` 側）。
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Literal, assert_never, cast

import markdown_it
from loguru import logger
from PySide6.QtCore import (
    QEvent,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QPainter,
    QPen,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QMenu,
    QTextBrowser,
    QWidget,
)

from ..common.i18n import t
from ..common.touch import enable_touch_scroll
from ..common.ui import current_tokens, rgba
from ..common.ui.timers import DebounceMode, Debouncer
from .context_menus import (
    EntryMenuContext,
    append_entry_verbs,
    curation_hooks_from_ancestors,
)
from .image_cache import (
    MARKDOWN_CACHE_MAX_BYTES,
    MARKDOWN_CACHE_MAX_ENTRIES,
    MARKDOWN_CACHE_MAX_SINGLE_BYTES,
    BoundedImageCache,
)
from .edge_nav import route_scroll_or_navigate
from .markdown_pipeline import (
    DEFAULT_FONT_PT,
    MAX_FONT_PT,
    MIN_FONT_PT,
    _DEFAULT_FONT_PT,
    _HTML_TEMPLATE,
    _IMG_ONLY_P_RE,
    _img_src_key,
    _layout_box,
    _MAX_FONT_PT,
    _MIN_FONT_PT,
    _POST_IDENTITY_KEYS,
    _post_header_html,
    _rewrap_img_paragraphs,
    _strip_dot_slash,
    _TEXT_ONLY_RENDER_MAX_CHARS,
    _theme_style_args,
    resolve_markdown_image_path,
)
from .perf import recorder
from .post_link_index import LOCAL_SCHEME, classify_post_links
from .qimage_decode import decode_qimage, read_image_size
from .text_decode import decode_text
from ._runnable import GuardedStream, StreamOutcome
from . import view_prefs


def _diag(message: str, *args) -> None:
    """``[diag]`` freeze-investigation breadcrumb, gated by the perf recorder.

    These lines used to be unconditional INFO, which bloated ``viewer.log``
    by dozens of lines per post opened during perfectly normal browsing.
    They now share the diagnostics toggle with :mod:`perf` (診断メニューの
    パフォーマンス記録) — OFF by default, and when the user turns the
    recorder on to chase a freeze, the breadcrumbs appear at INFO exactly
    as before.  Genuine anomalies (slow ``loadResource`` outliers, the
    sync-decode fallback) stay unconditional WARNINGs elsewhere.
    """
    if recorder().is_enabled():
        logger.info(message, *args)


class _StageTimer:
    """Log elapsed wall-time for a stage (perf-recorder-gated, see _diag).

    Cost when the recorder is off is one ``perf_counter`` pair per wrap —
    negligible vs the stages being measured (markdown render, setHtml,
    disk I/O).
    """

    __slots__ = ("_name", "_detail", "_start")

    def __init__(self, name: str, detail: str = "") -> None:
        self._name = name
        self._detail = detail
        self._start = 0.0

    def __enter__(self) -> "_StageTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_exc) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        if self._detail:
            _diag(
                "[diag] {} took {:.1f}ms ({})",
                self._name, elapsed, self._detail,
            )
        else:
            _diag("[diag] {} took {:.1f}ms", self._name, elapsed)

_MD_LOCAL = threading.local()


def _md() -> markdown_it.MarkdownIt:
    """Per-thread ``MarkdownIt`` instance.

    ``render()`` is *not* thread-safe: linkify-it-py mutates instance state
    during a render, and the same renderer used to be shared between the GUI
    thread (text-only fast path in ``_render_post_text``) and the render
    stream's worker (``_render_post_body``) — stepping posts while a render
    was in flight raced the two.  Construction is cheap and happens once per thread,
    so ``threading.local`` gives isolation without serialising renders.
    """
    md = getattr(_MD_LOCAL, "md", None)
    if md is None:
        md = markdown_it.MarkdownIt(
            "commonmark", {"html": False, "linkify": True}
        ).enable("linkify")
        _MD_LOCAL.md = md
    return md


#: :func:`_read_post_body` の結末。
_MdReadKind = Literal["text", "error"]


def _read_post_body(md_path: Path) -> StreamOutcome:
    """Read a post.md body off the GUI thread (ワーカーで走る純関数).

    ``read_text`` on the GUI thread blocks for the full NAS round-trip —
    fine on a warm local disk, but a cold SMB open+read can take hundreds
    of milliseconds to seconds (worse while a background cache build is
    competing for SMB credits), freezing every interaction until the text
    arrives.  The view shows its loading notice immediately and renders
    when this result lands.

    The read is capped at :func:`view_prefs.get_markdown_body_max_bytes` and
    truncated bodies get the note ``TextView`` shows.  Without a cap the
    centre pane had one leaf with no size gate at all: every other one
    (text 2 MiB / PDF 100 MiB / ZIP 50 MiB) refuses or truncates, while a
    large ``.md`` was read whole and then rendered synchronously.

    The cap is markdown's **own** (256 KiB), not the plain-text one: moving
    the render off-thread does not move ``setHtml``, which is GUI-bound and
    super-linear in document size — at the 2 MiB text cap a single click still
    froze the UI for about a second.  See the constant for the full reasoning.
    """
    limit = view_prefs.get_markdown_body_max_bytes()
    try:
        with _StageTimer("  post.md read (worker)", md_path.name):
            with open(md_path, "rb") as fh:
                raw = fh.read(limit + 1)
    except OSError as exc:
        return StreamOutcome("error", str(exc))
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    # 符号推定は TextView と共有する（``text_decode.decode_text``）。
    # post.md 以外の任意の .md もここへ来るので、UTF-8 固定だと BOM 付き
    # UTF-8 は BOM が行頭に残って先頭見出しが段落になり、Shift_JIS /
    # UTF-16（メモ帳の「Unicode」）は全文が化ける。
    # 上限で切れた末尾の半端な多バイト文字は ``truncated`` が捨てる。
    # 最後の手段は Latin-1 ではなく UTF-8（replace）— 書式上 UTF-8 の
    # post.md に壊れたバイトが混じっても、その文字だけが U+FFFD になる。
    text = decode_text(raw, truncated=truncated, last_resort="utf-8")
    if truncated:
        text += "\n\n" + t(
            "viewer.markdown_view.body_truncated",
            mib=limit / (1024 * 1024),
        )
    return StreamOutcome("text", text)


#: :func:`_render_post_body` の結末。``ready`` の値は
#: ``(HTML with width/height baked into <img>, dict[local, QSize])`` で、
#: 2 つ目は**元画像の実寸**。表示ボックスはビュー側が ``_layout_box`` で
#: 現在のコンテンツ幅から導出し直す。
_MdRenderKind = Literal["ready", "failed"]


def _render_post_body(
    base_dir: Path, text: str, max_w: int, max_px: int = 0,
) -> StreamOutcome:
    """Render markdown + read image headers off the GUI thread (純関数).

    Posts with many large attachments otherwise freeze the app for
    seconds on folder switch: ``markdown_it`` rendering of a long body is
    non-trivial, and a header-size read (``qimage_decode.read_image_size``)
    per file is typically 10–50 ms (file open + header parse, worse on
    HDD / network storage) — 50+ sequential calls on the GUI thread
    block input.

    Running the whole pipeline (markdown → HTML → image-header sizing) in
    the background keeps the UI responsive — the main thread only shows a
    brief "loading" state and then calls ``setHtml`` once when this result
    arrives.

    *max_px* is the decoded-pixel budget per image (0 disables the clamp).
    Width alone is clamped to the viewport, so an extremely tall image
    (webtoon strip) could otherwise decode past the LRU's
    ``max_single_bytes`` — such a QImage is silently refused by the cache
    yet stays resident in the QTextDocument, bypassing the memory budget
    entirely and re-decoding on every reflow.

    失敗も値で返す（``failed``）— 読み込み中プレースホルダが永久に残らない
    ように、ビューがエラーページへ切り替えるため。
    """
    t0 = time.perf_counter()
    header_time = 0.0
    headers_read = 0
    try:
        body_html = _rewrap_img_paragraphs(_md().render(text))
        # 値は**元画像の実寸**（表示ボックスではない）。
        sources: dict[str, QSize] = {}

        def _rewrite(m: re.Match[str]) -> str:
            nonlocal header_time, headers_read
            tag = m.group(0)
            src_match = re.search(r'src="([^"]+)"', tag)
            if not src_match:
                return tag
            if " width=" in tag or " height=" in tag:
                return tag
            local = _img_src_key(src_match.group(1))
            candidate = base_dir / local
            if not candidate.is_file():
                return tag
            t_header = time.perf_counter()
            wh = read_image_size(candidate)
            header_time += time.perf_counter() - t_header
            headers_read += 1
            if wh is None:
                return tag
            src_w, src_h = wh
            source = QSize(src_w, src_h)
            # 焼き込む width/height はここでの派生値。ビュー側は
            # ``sources`` から同じ関数で導出し直すので、後からウィンドウ
            # を広げれば表示ボックスもデコードも追従する。
            target = _layout_box(source, max_w, max_px)
            sources[local] = source
            return (
                f'<img width="{target.width()}" height="{target.height()}"'
                + tag[4:]
            )

        new_html = re.sub(r"<img\b[^>]*>", _rewrite, body_html)
        total_ms = (time.perf_counter() - t0) * 1000.0
        _diag(
            "[diag] markdown render: {} headers in {:.1f}ms "
            "(header I/O {:.1f}ms, {} sized), total {:.1f}ms",
            headers_read, header_time * 1000.0, header_time * 1000.0,
            len(sources), total_ms,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Markdown render failed: {}", exc)
        return StreamOutcome("failed", str(exc))
    return StreamOutcome("ready", (new_html, sources))


#: :func:`_decode_post_image` の結末。``ready`` の値は ``(QUrl, QImage)``、
#: ``failed`` は ``QUrl`` だけ。失敗も必ず着地させる — GUI 側は投入前に
#: ``_pending_redecode`` へ印を取るので、黙ると鍵が永久に残り、その枠が
#: 白いままになる。
_MdImageKind = Literal["ready", "failed"]


def _decode_post_image(
    url: QUrl, path: Path, target_size: QSize, dpr: float = 1.0,
) -> StreamOutcome:
    """Decode a ``post.md`` image to the target layout size (純関数).

    ``QTextBrowser`` calls :meth:`MarkdownView.loadResource` synchronously
    during ``setHtml`` layout for every ``<img>``.  Returning a placeholder
    with the final dimensions lets the layout complete immediately; the
    real pixels are decoded here in the background and swapped in via
    ``addResource`` when ready.

    *target_size* is the *logical*-pixel layout box (matching the HTML
    ``<img width height>`` attrs).  We decode at ``target × dpr`` physical
    pixels and stamp the DPR onto the ``QImage`` so that on a 4K/150%
    display Qt renders the raster 1:1 at screen resolution instead of
    upscaling a logical-sized bitmap (which looks blurry).
    """
    try:
        target: QSize | None = None
        if not target_size.isEmpty():
            # Physical-pixel box; decode_qimage keeps aspect and
            # never upscales past the source, so an original smaller
            # than the physical target stays at native resolution
            # (matching the old setScaledSize clamp).
            target = QSize(
                max(1, round(target_size.width() * dpr)),
                max(1, round(target_size.height() * dpr)),
            )
        image = decode_qimage(path, target_size=target)
    except Exception as exc:  # pragma: no cover
        logger.warning("Markdown image decode failed for {}: {}", path, exc)
        return StreamOutcome("failed", url)
    if image is None or image.isNull():
        return StreamOutcome("failed", url)
    if dpr > 1.0:
        image.setDevicePixelRatio(dpr)
    return StreamOutcome("ready", (url, image))


class MarkdownView(QTextBrowser):
    """QTextBrowser that resolves relative resources from a base directory."""

    file_link_clicked = Signal(Path)  # internal link the parent should re-open
    post_link_clicked = Signal(Path)  # 📁 jump to a downloaded post's folder
    # (delta, immediate) — immediate=True skips the grace period (content
    # fits on screen, so there's nothing for the user to be in the middle of
    # reading via scroll).
    navigate_requested = Signal(int, bool)
    font_pt_changed = Signal(int)  # emitted after a Ctrl+wheel size change
    # Right-click "この画像に類似を検索" on an inline post.md image (C-10
    # extension).  Pixmap is always None here — the seed image isn't a
    # decoded resource we can cheaply thumbnail from the text document's
    # resource cache — the seed-preview row falls back to a generic icon.
    similar_search_requested = Signal(Path, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Body font size in points; 0 means "use the app default"
        # (``_DEFAULT_FONT_PT``).  Text-only reflow (``document().clear()``
        # is not needed): changing this re-renders the *same* body HTML
        # with a new ``font-size`` in the ``<style>`` block, so cached
        # image resources survive the ``setHtml`` and nothing re-decodes.
        self._font_pt = 0
        self._base_dir: Path | None = None
        # Full path of the currently shown post.md (F05 right-click menu).
        self._post_path: Path | None = None
        # Whether the "この画像に類似を検索" context-menu item is offered
        # (C-10 extension).  Set True only when a VectorIndex is loaded —
        # mirrors FileListView / ImageView's ``set_similar_search_available``.
        self._similar_search_available = False
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_anchor_clicked)
        enable_touch_scroll(self)

        # Downloaded-post link resolution.  ``_post_link_resolver`` is an
        # injected ``(url) -> folder | None`` callable (wired by the window
        # to the search index's postref table); ``_local_post_targets`` maps
        # the per-post ``snappixpost:<key>`` anchors back to their folders.
        self._post_link_resolver: Callable[[str], Path | None] | None = None
        self._local_post_targets: dict[str, Path] = {}
        # Memoised ``url -> folder | None`` results for the post currently
        # shown.  ``_classified`` runs on EVERY setHtml (reflow / font size /
        # refresh_links), so without this every resize re-queried the postref
        # index once per anchor.  Invalidated on post
        # switch, resolver swap and ``refresh_links`` (the index filled, so
        # previously unresolved links may now resolve).
        self._post_link_cache: dict[str, Path | None] = {}

        # Progressive image loading: ``set_post`` pre-reads each image's
        # header (cheap, a few KB per file) to know its final layout size,
        # and bakes ``width``/``height`` attributes into the HTML so the
        # layout is final-shaped from the first paint.  ``loadResource``
        # then returns a *tiny* placeholder — HTML attrs drive layout, so
        # the placeholder's pixel size doesn't matter and we avoid
        # allocating + zero-filling a multi-megabyte buffer per image.
        #
        # Real pixels are decoded on ``_img_stream`` in the background and
        # swapped in via ``_on_image_landed``.  投稿を切り替えると
        # :meth:`release_images` がストリームごと ``cancel`` するので、前の
        # 投稿のデコードは黙って捨てられる。
        # local ref → 元画像の実寸（レンダーがヘッダから読む値）。
        self._img_sources: dict[str, QSize] = {}
        # local ref → 現在のコンテンツ幅での表示ボックス。``_img_sources``
        # からの**派生値**で、リフローのたびに導出し直す。
        self._img_targets: dict[str, QSize] = {}
        # 本文画像のデコード。**加算的**な投入（``submit_batch``）— 1 投稿の
        # 画像は次々に要求され、``loadResource`` / ``_refresh_visible_images``
        # が「要求済み」を ``_pending_redecode`` に控えるので、後続の投入が
        # 先行を捨てると答えの返らない枠が永久に白いまま残る。全部まとめて
        # 捨てるのは投稿切替（:meth:`release_images` の ``cancel``）だけ。
        # 4 スレッド: 画像デコードは CPU バウンドで、ノート PC の CPU でも
        # スラッシングせずに並列の効きが出る本数。
        self._img_stream = GuardedStream(self, max_threads=4)
        self._img_stream.bind(self._on_image_landed)

        # Aggregated timing for the ``loadResource`` and
        # ``_apply_image`` hot paths — logging per call would spam
        # the log with dozens of lines per post.  Reset at the start of
        # each ``set_post`` and logged from ``_apply_render`` once
        # the final ``setHtml`` completes.
        self._lr_count = 0
        self._lr_total_ms = 0.0
        self._lr_max_ms = 0.0
        self._img_ready_count = 0
        self._img_ready_total_ms = 0.0
        self._img_ready_max_ms = 0.0

        # 画像ヘッダの読みを GUI スレッドから外すレンダーストリーム。投稿を
        # またいで要るのは**最新の 1 件だけ**なので投入は superseding
        # （``submit``: キュー済みは破棄・走行中の着地は世代で落ちる）。
        self._render_stream = GuardedStream(self)
        self._render_stream.bind(self._on_render_landed)
        # post.md 本文の非同期読み（1 レーン・superseding）— GUI スレッドで
        # 読んではならない理由は :func:`_read_post_body`。連続切替でも最後の
        # 1 件だけが描かれる。
        self._read_stream = GuardedStream(self)
        self._read_stream.bind(self._on_post_read)
        self._pending_md_name = ""  # for diag logging in _on_post_read
        # Content-area width the render task sized images to.  Used by
        # ``_apply_render`` to decide whether the first paint already
        # fits (skip the redundant reflow) or a resize slipped in.
        self._render_max_w = 0

        # Coalesced repaint: each finished background decode calls
        # ``addResource`` (cheap) but the paint invalidation that makes it
        # visible — ``markContentsDirty(0, characterCount())`` — is O(doc
        # size).  On a 300-image post the decode flood would fire that
        # hundreds of times, janking the *whole* app for the entire load.
        # Instead we debounce a single full-document dirty per frame, so
        # bursts of decodes coalesce into one repaint (~33 ms) each.
        self._repaint_timer = Debouncer(
            self, 33, self._flush_repaint, mode=DebounceMode.LEADING_WINDOW
        )

        # Debounced reflow: the rendered HTML bakes ``width``/``height``
        # attrs per image.  When the viewport narrows, CSS ``max-width:
        # 100%`` shrinks the width but leaves the height attr untouched,
        # which would stretch images vertically.  On resize we regenerate
        # the body HTML with each ``<img>`` resized to preserve its cached
        # aspect ratio and re-apply it via ``setHtml`` — the image
        # resource cache survives across ``setHtml`` calls so the cached
        # ``QImage`` payloads are reused (no re-decode).
        self._reflow_timer = Debouncer(
            self, 60, self._on_reflow_timeout, mode=DebounceMode.TRAILING
        )
        self._last_reflow_width = 0
        # 「次のタイマー発火は強制リフロー」の 1 ビット。
        # ``_reflow_images`` の幅ガードは「ビューポート幅が変わったか」しか
        # 見られないので、幅は同じでも表示ボックス（``_img_targets``）を
        # 書き換えた経路はガードを迂回する必要がある。その迂回を
        # 「呼ぶ側が ``_last_reflow_width = 0`` を書く」という手作業の規約に
        # すると経路ごとに片側が欠けるので、
        # 迂回の意思は ``force`` 引数で表明し、タイマー越しの経路だけがこの
        # ビットに預ける。
        self._force_reflow = False
        # Body HTML captured after the render task baked in width/height
        # per image — reused by ``_reflow_images`` to rewrite those attrs.
        self._last_body_html: str | None = None
        # 現在の投稿のメタカード HTML（_on_post_read で分離し、
        # 非同期レンダ結果の先頭へ _apply_render が挿す）。保留ではなく
        # 「今の投稿の持ち物」— 挿しても降ろさず、投稿が変わるまで有効。
        self._current_header_html = ""
        # ``changeEvent`` → ``_rerender_theme`` の再入ガード。
        self._theme_rerendering = False

        # Bounded secondary cache of decoded ``QImage``s, keyed by the
        # absolute URL string (same key Qt passes to ``loadResource``).
        # When the per-post QTextDocument resource cache grows past this
        # budget we evict the oldest images from it (swap their resource
        # back to a placeholder) while keeping them here — a later scroll
        # to an evicted image rehydrates QTextDocument synchronously from
        # the cache without a re-decode.  URLs currently holding a
        # placeholder in QTextDocument are tracked in ``_evicted_urls`` so
        # ``loadResource`` knows to consult the cache before dispatching
        # a fresh decode.
        self._img_cache: BoundedImageCache[QImage] = BoundedImageCache(
            max_bytes=MARKDOWN_CACHE_MAX_BYTES,
            max_entries=MARKDOWN_CACHE_MAX_ENTRIES,
            max_single_bytes=MARKDOWN_CACHE_MAX_SINGLE_BYTES,
            sizeof=lambda q: q.sizeInBytes(),
        )
        # Mirror of the cache's single-entry byte limit — used to derive the
        # per-image decoded-pixel budget the render task bakes into the HTML
        # (``_decode_px_budget``).  The cache keeps its limits private, and
        # every baked image must stay insertable or it escapes the LRU and
        # pins unbounded memory in the QTextDocument.
        self._cache_max_single_bytes = MARKDOWN_CACHE_MAX_SINGLE_BYTES
        self._evicted_urls: set[str] = set()

        # Re-decode-on-scroll windowing.  ``QTextDocument`` never re-invokes
        # ``loadResource`` for a URL whose resource it already holds — so an
        # image the LRU evicted to a placeholder stays blank forever once
        # scrolled away and back (and, on an image-heavy post, the initial
        # setHtml decode-flood evicts the *top* images before the user even
        # scrolls).  ``_refresh_visible_images`` walks the document layout on
        # a debounced tick, finds evicted images near the viewport, and
        # re-dispatches their decode so the slot repopulates.
        # ``_pending_redecode`` dedups tasks already in flight.
        self._pending_redecode: set[str] = set()
        #: 直近の可視帯（上下 1 画面ぶん）にあった画像 URL。``_img_cache.put``
        #: へ ``protect=`` で渡し、「いま見えている画像を退避して、その退避が
        #: 再デコードを呼び、それがまた別の可視画像を退避する」無限ループを
        #: 断つ（``ImageView`` の先読みが同じ口を使っている）。
        self._visible_urls: set[str] = set()
        self._visible_timer = Debouncer(
            self, 100, self._refresh_visible_images, mode=DebounceMode.TRAILING
        )
        self.verticalScrollBar().valueChanged.connect(
            lambda _=0: self._visible_timer.trigger()
        )

    def reconfigure_cache(
        self, *, max_bytes: int, max_entries: int, max_single_bytes: int,
    ) -> None:
        """Apply new cache limits and evict overflow into placeholders.

        Each evicted entry's URL must be swapped back to a placeholder in
        the QTextDocument resource cache — otherwise the document would
        still pin the real ``QImage`` even though our LRU dropped it.
        """
        self._cache_max_single_bytes = max_single_bytes
        # 画素予算（``_decode_px_budget``）はこの値から出るので、上限を変えた
        # ら表示ボックスも導出し直す。これが無いと上限を下げた直後のターゲット
        # が過大なまま残り、再デコードした QImage が新上限を超えて put が黙って
        # 拒否される＝「予算外で QTextDocument に居座り毎リフロー
        # 再デコード」状態になる（下の ``_visible_timer`` がその再デコードを
        # 能動的に起こすので特に効く）。幅は現在適用中のもの（リフロー済みなら
        # その幅、未リフローなら初回レンダ幅）。
        self._img_targets = self._derive_targets(
            self._last_reflow_width or self._render_max_w
        )
        # 導出し直した表示ボックスを本文 HTML へ焼き直す。幅は
        # 変わっていないので強制リフローで蹴る — 素のリフローだと即 return
        # し、「HTML の箱は旧サイズ・デコードは新（小）サイズ」の拡大表示が
        # 残る。
        self._schedule_forced_reflow()
        evicted = self._img_cache.reconfigure(
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_single_bytes=max_single_bytes,
        )
        if not evicted:
            return
        doc = self.document()
        for key in evicted:
            doc.addResource(
                QTextDocument.ResourceType.ImageResource,
                QUrl(str(key)), self._placeholder(),
            )
            self._evicted_urls.add(str(key))
        doc.markContentsDirty(0, doc.characterCount())
        # 退避で画面上の画像が空白になったかもしれない — QTextDocument は
        # 退避したリソースを自分から再要求しないので、``_apply_image``
        # の退避処理と同じく可視窓の再デコードを起こす。これが無いと設定
        # ダイアログでキャッシュ上限を下げた瞬間に表示中の本文画像が全部
        # 白いままになり、ユーザーがスクロールするまで戻らない。
        self._visible_timer.trigger()

    def _derive_targets(self, max_w: int) -> dict[str, QSize]:
        """コンテンツ幅 *max_w* での表示ボックスを全画像ぶん導出する。

        レンダータスクが焼き込んだ width/height と同じ ``_layout_box`` を
        通すので、初回描画時（``_render_max_w``）に呼べば焼き込み値と一致し、
        リフロー時に呼べば新しい幅に追従する。
        """
        max_px = self._decode_px_budget()
        return {
            local: _layout_box(src, max_w, max_px)
            for local, src in self._img_sources.items()
        }

    def _decode_px_budget(self) -> int:
        """Per-image decoded-pixel cap so every decode fits the LRU.

        ``_decode_post_image`` decodes at ``target × dpr`` physical pixels, 4
        bytes each (RGB32/ARGB32) — the render task clamps its baked
        targets to this many *logical* pixels so the resulting ``QImage``
        never exceeds the cache's ``max_single_bytes`` (an oversize put is
        silently refused, leaving the image pinned in the QTextDocument
        outside any budget and re-decoded on every reflow).  The 2 %
        headroom absorbs the per-edge rounding of the dpr multiply.
        """
        dpr = max(1.0, self.devicePixelRatioF() or 1.0)
        budget = (self._cache_max_single_bytes // 4) * 0.98
        return max(1, int(budget / (dpr * dpr)))

    def set_post_link_resolver(
        self, resolve: Callable[[str], Path | None] | None
    ) -> None:
        """Inject the ``(url) -> downloaded-post folder | None`` resolver.

        Decoupling pattern mirrors :meth:`ImageView.set_thumbnail_provider`:
        the view only sees a callable, so it stays independent of the search
        index / window wiring.  ``None`` disables link classification.
        """
        self._post_link_resolver = resolve
        self._post_link_cache.clear()

    def set_similar_search_available(self, available: bool) -> None:
        """Enable/disable the context-menu "この画像に類似を検索" entry (C-10).

        Wired by :class:`ViewerWindow` to the presence of a VectorIndex —
        with no vectors the similar search would be a no-op, so the item
        is hidden.
        """
        self._similar_search_available = bool(available)

    def _classified(self, body_html: str) -> str:
        """Tag downloaded-post links and refresh ``_local_post_targets``.

        Applied at every ``setHtml`` so resize-driven reflows keep the link
        styling.  No resolver → no-op (and the targets map is emptied).

        ``body_html`` is not just the post body: callers prepend the
        post-card header (``_current_header_html``), whose 「投稿ページ」
        line links to the post's *own* URL. That self-link would otherwise
        resolve to ``_base_dir`` (the post being shown *is* downloaded — it's
        on screen), so every post's card would gain a local-copy pill + 📁 that
        just re-opens itself. Passing ``self_folder=self._base_dir`` makes
        ``classify_post_links`` skip that self-resolution (the logic-side
        exclusion lives in ``post_link_index.py``).

        Resolutions are memoised per post (``_post_link_cache``): a body with N
        post links would otherwise re-query the postref index N times on every
        reflow / font-size change, all on the GUI thread.
        ``refresh_links`` drops the cache so a freshly filled index is
        picked up.
        """
        resolve = self._post_link_resolver
        if resolve is None:
            self._local_post_targets = {}
            return body_html
        cache = self._post_link_cache

        def _cached_resolve(url: str) -> Path | None:
            if url not in cache:
                cache[url] = resolve(url)
            return cache[url]

        new_html, targets = classify_post_links(
            body_html, _cached_resolve, self_folder=self._base_dir
        )
        self._local_post_targets = targets
        return new_html

    def _render_html(self, body_html: str) -> str:
        """Wrap a body fragment in ``_HTML_TEMPLATE`` at the current font size.

        Single choke point for every ``setHtml`` call so font-size changes
        and link classification stay consistent across the text-only fast
        path, the async image-post path, and reflow/font-size re-renders.
        """
        font_pt = self._font_pt if self._font_pt > 0 else _DEFAULT_FONT_PT
        return _HTML_TEMPLATE.format(
            body=self._classified(body_html), font_pt=font_pt,
            **_theme_style_args(),
        )

    def font_pt(self) -> int:
        """Current body font size in points (resolved default, never 0)."""
        return self._font_pt if self._font_pt > 0 else _DEFAULT_FONT_PT

    def set_font_pt(self, pt: int) -> None:
        """Set the body font size (clamped to ``_MIN_FONT_PT.._MAX_FONT_PT``).

        Re-renders the *current* body HTML (``_last_body_html``) with the
        new ``font-size`` baked into the ``<style>`` block and re-applies it
        via a single ``setHtml``.  This is text-layout-only: image ``width``/
        ``height`` attrs are untouched (font size is independent of the
        baked-in image layout box) and every already-decoded ``QImage`` is
        still resident in ``_img_cache`` / the document's resource cache, so
        ``loadResource`` hits the cache and nothing re-decodes.  Scroll
        position is preserved proportionally, matching ``_reflow_images``.
        """
        if self._apply_font_pt(pt):
            self.font_pt_changed.emit(self._font_pt)

    def _apply_font_pt(self, pt: int) -> bool:
        """描画だけを差し替える（通知はしない）。変化が無ければ ``False``。

        ``set_font_pt`` と :meth:`reset_font_pt` で**流す値が違う**（実値 /
        0 = アプリ既定に従う）ので、描画の本体はここに 1 本だけ置き、
        ``font_pt_changed`` の emit は呼び出し側が担う。
        """
        pt = max(_MIN_FONT_PT, min(_MAX_FONT_PT, pt))
        if pt == self.font_pt():
            return False
        self._font_pt = pt
        if self._last_body_html is not None:
            # ``_last_body_html`` holds the width/height attrs the render
            # task baked at *its* viewport width — which may be stale if the
            # window was resized (a reflow ran) between then and now.  CSS
            # ``max-width: 100%`` would clamp the width but leave the height
            # attr, stretching images vertically.  So image posts re-run the
            # reflow (forced, since the viewport width itself is unchanged)
            # to re-size every image to the *current* content width.
            #
            # ``_reflow_images`` does its own ``_render_html`` + ``setHtml``
            # + scroll-ratio restore, so it must not be preceded by one here:
            # that would make every Ctrl+wheel notch lay out the whole document
            # twice, the first result always discarded before it could paint.
            # The plain ``setHtml`` below stays
            # as the fallback for the cases the reflow bails out of (no
            # images, or no viewport width yet).
            if (
                self._img_targets
                and self._base_dir is not None
                and self.viewport().width() > 0
            ):
                self._reflow_images(force=True)
            else:
                sbar = self.verticalScrollBar()
                scroll_max = max(1, sbar.maximum())
                scroll_ratio = sbar.value() / scroll_max
                self.setHtml(self._render_html(self._last_body_html))
                new_max = sbar.maximum()
                sbar.setValue(round(new_max * scroll_ratio))
        return True

    def reset_font_pt(self) -> None:
        """「フォントサイズを既定に戻す」— 描画は既定 pt、**保存値は 0**。

        設定ダイアログ / ``ViewerState.markdown_font_pt`` / 適用側
        (``content_view``) はいずれも **0 = アプリ既定に従う**という規約で
        往復する。復帰項目が ``set_font_pt(_DEFAULT_FONT_PT)`` を呼ぶと
        ``font_pt_changed`` が 11 を流し、窓がそれを永続させるので、設定は
        「11 pt」を表示し、アプリ既定が変わっても追従しなくなる
        （対の片側欠落）。復帰経路だけ 0 を
        流す。Ctrl+ホイールの一般経路 (:meth:`set_font_pt`) は実値のまま。
        """
        self._apply_font_pt(_DEFAULT_FONT_PT)
        # 描画が既に既定 pt でも 0 は必ず流す（保存値が 11 に固定されたまま
        # の状態から抜けられるのはこの emit だけ）。
        self.font_pt_changed.emit(0)

    def refresh_links(self) -> None:
        """Re-classify the current post's links (e.g. after the index fills).

        Called when newly-scanned posts may have become resolvable.  Re-runs
        the reflow path (which re-applies link classification) preserving
        scroll position; a no-op when no markdown body is currently shown.
        """
        if self._last_body_html is None or self._base_dir is None:
            return
        # Newly scanned posts may have become resolvable — the memoised
        # results (including the ``None``s) are stale, so start from scratch.
        self._post_link_cache.clear()
        # Force the reflow to re-run even though the viewport width is
        # unchanged — we want a fresh classification pass, not a resize.
        self._reflow_images(force=True)

    def set_post(self, md_path: Path) -> None:
        # Inner impl wrapped with a stage timer — captures only the
        # synchronous GUI-thread cost.  The async render task completion
        # is logged separately from ``_apply_render``.
        with _StageTimer("MarkdownView.set_post", md_path.name):
            self._set_post_sync(md_path)

    def release_images(self) -> None:
        """Drop the shown post's decoded pixels and its queued render work.

        The teardown half of :meth:`set_post`, callable on its own so the
        hosting ``ContentView`` can hand the memory back when the centre pane
        leaves the markdown page.  Otherwise the
        only release path would be 「次の post.md を開いたとき」, so an image-heavy
        post's decoded pixels (``MARKDOWN_CACHE_MAX_BYTES`` = 256 MiB at the
        ceiling, plus the QTextDocument's own resource cache) would stay resident
        for the rest of the session while the user browses images / 動画 /
        ZIP.  Nothing is lost by releasing early: :meth:`set_post` re-reads and
        re-decodes unconditionally even for the same path, so the retained
        pixels would never be reused on the way back.

        Idempotent and safe with no post shown — every container is simply
        already empty.
        """
        # Any work still queued for the previous post is now obsolete —
        # 3 本のストリームをまとめて ``cancel``（キュー済みを捨て、走行中の
        # セッションを畳む）。着地の選別はストリーム側で済むので、受け側の
        # スロットに世代ガードは書かない。
        self._read_stream.cancel()
        self._render_stream.cancel()
        self._img_stream.cancel()
        self._img_sources.clear()
        self._img_targets.clear()
        self._img_cache.clear()
        self._evicted_urls.clear()
        self._pending_redecode.clear()
        self._visible_timer.stop()
        self._repaint_timer.stop()
        self._last_body_html = None
        self._current_header_html = ""
        self._local_post_targets = {}
        self._post_link_cache.clear()
        # Reset per-post aggregated stats (logged from _apply_render
        # once the final setHtml completes).
        self._lr_count = 0
        self._lr_total_ms = 0.0
        self._lr_max_ms = 0.0
        self._img_ready_count = 0
        self._img_ready_total_ms = 0.0
        self._img_ready_max_ms = 0.0

        # Drop the previous post's decoded pixels for real.  Clearing the
        # mirror LRU above only releases *our* reference — the same QImages
        # are still held by the QTextDocument's resource cache, which
        # ``setHtml`` does not flush (see ``_apply_render``).  Without
        # this, switching from an image-heavy post to a text-only one (fast
        # path) or to the read-error page never gives the memory back until
        # the next image post's ``_apply_render`` happens to call
        # ``doc.clear()``.  Done before
        # ``setBaseUrl`` so the base URL the caller sets next survives the
        # clear.
        self.document().clear()

    def _set_post_sync(self, md_path: Path) -> None:
        self._base_dir = md_path.parent
        self._post_path = md_path
        # Teardown half, shared verbatim with the page-leave release.
        self.release_images()
        self.document().setBaseUrl(
            QUrl.fromLocalFile(str(self._base_dir) + "/")
        )
        self.setSearchPaths([str(self._base_dir)])

        # Show the loading notice immediately and hand the file read to a
        # worker: a cold NAS read can take seconds and must never block the
        # GUI thread.  For warm local reads the notice is replaced within a
        # frame or two, so it's imperceptible.
        self._pending_md_name = md_path.name
        with _StageTimer("  setHtml (loading notice)"):
            self.setHtml(
                _HTML_TEMPLATE.format(
                    body='<p style="color:palette(mid);padding:24px;">'
                    f"{t('common.status.loading')}</p>",
                    font_pt=self.font_pt(),
                    **_theme_style_args(),
                )
            )
        self._read_stream.submit(lambda p=md_path: _read_post_body(p))

    def _on_post_read(self, payload: object) -> None:
        """The async post.md read landed: render it (fast or image path)."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_MdReadKind, payload.kind)
        match kind:
            case "text":
                self._render_post_text(cast(str, payload.value))
            case "error":
                self._show_read_error(cast(str, payload.value))
            case _:
                assert_never(kind)

    def _show_read_error(self, error: str) -> None:
        """post.md が読めなかった — エラーページを出す。

        Route through the single ``_render_html`` choke point so the error
        obeys the current font size, and escape the OSError string (it embeds
        the path, which may contain ``<`` / ``&``) so it can't be misparsed as
        HTML.  ``_last_body_html`` stays None so a later reflow / font change
        is a no-op on the error page.
        """
        self.setHtml(
            self._render_html(
                "<p>"
                + t(
                    "viewer.markdown_view.read_failed",
                    error=html.escape(error),
                )
                + "</p>"
            )
        )

    def _render_post_text(self, text: str) -> None:
        """読めた本文を描く（同期の text-only 経路 / 非同期のレンダー経路）."""
        md_name = self._pending_md_name
        # タイトル + 生メタブロックを日本語のメタカード HTML
        # へ変換し、markdown 描画は本文のみに絞る。メタ無しの .md は素通し。
        header_html, text = _post_header_html(text)
        self._current_header_html = header_html
        # Fast path: no image markup → render synchronously.  ``"!["`` is
        # markdown's only image syntax (HTML is disabled in ``_md()``), so
        # its absence guarantees zero ``<img>`` — a plain-text post skips
        # the background hop and its rendering cost is trivial.  A stray
        # ``![`` inside a code span merely routes through the async path
        # harmlessly (the task finds no ``<img>`` and emits plain HTML).
        # 本文が長いときは画像が無くてもワーカーへ回す: ``markdown_it`` の
        # レンダ時間は本文長に対して超線形で、同期で走らせるとキャンセル
        # できない GUI フリーズになる（画像入りの経路が既にやっている処理
        # そのものなので、``<img>`` が 0 件でもそのまま動く）。
        if "![" not in text and len(text) <= _TEXT_ONLY_RENDER_MAX_CHARS:
            with _StageTimer("  markdown render (text-only)",
                             f"{len(text)} chars"):
                body_html = header_html + _rewrap_img_paragraphs(
                    _md().render(text)
                )
            # Keep the unclassified body as the reflow/refresh base so
            # ``refresh_links`` can re-tag links once the index fills.
            self._last_body_html = body_html
            with _StageTimer("  setHtml (text-only)"):
                self.setHtml(self._render_html(body_html))
            _diag("[diag] set_post done (text-only): {}", md_name)
            return

        # Image post: render markdown + read image headers off the GUI
        # thread.  The loading notice is already showing (set before the
        # async file read was dispatched); one final ``setHtml`` lands when
        # the render task completes — no layout jumps because the
        # intermediate state has no images.
        _diag("[diag] set_post dispatched to render task ({})", md_name)

        # Size images to the body content area (viewport minus padding) —
        # the same width ``_reflow_images`` targets.  Baking the final
        # width up front means the first paint already fits and the
        # otherwise-mandatory second full-document ``setHtml`` (reflow) is
        # skipped in the common case (see ``_apply_render``).
        vp_w = self.viewport().width()
        max_w = max(100, (vp_w if vp_w > 0 else 1000) - 24)
        self._render_max_w = max_w
        # 新しい投稿: 「まだ 1 度もリフローを適用していない」へ戻す（幅ガード
        # の迂回ではなく、追跡している状態そのもののリセット）。
        self._last_reflow_width = 0
        base_dir = self._base_dir
        max_px = self._decode_px_budget()
        self._render_stream.submit(
            lambda: _render_post_body(base_dir, text, max_w, max_px)
        )

    def _on_render_landed(self, payload: object) -> None:
        """レンダー結果の着地（``ready`` / ``failed`` の 1 本口）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_MdRenderKind, payload.kind)
        match kind:
            case "ready":
                html_and_sources = cast(
                    "tuple[str, dict[str, QSize]]", payload.value
                )
                self._apply_render(*html_and_sources)
            case "failed":
                self._show_render_error(cast(str, payload.value))
            case _:
                assert_never(kind)

    def _apply_render(self, html: str, sources: dict) -> None:
        # メタカードは本文の読みで分離済み — 描画結果の
        # 本文 HTML の前に挿す（着地したのは同じ投稿のレンダーなので、
        # 控えてあるヘッダはこの本文のもの）。
        html = self._current_header_html + html
        self._img_sources = sources
        # タスクが焼き込んだ width/height と同じ幅で導出（= 一致する）。
        self._img_targets = self._derive_targets(self._render_max_w)
        self._last_body_html = html
        doc = self.document()
        _diag(
            "[diag] _apply_render entry: doc chars={}, "
            "block_count={}, targets={}, html_len={}",
            doc.characterCount(), doc.blockCount(),
            len(sources), len(html),
        )
        # Clear prior state explicitly so the final ``setHtml`` starts
        # from a clean document.  Without this, the resource cache +
        # text blocks from the preceding loading-notice (and any prior
        # post) linger and appear to compound Qt's layout cost.
        with _StageTimer("  document().clear() before final setHtml"):
            doc.clear()
        with _StageTimer(
            "_apply_render: final setHtml",
            f"{len(sources)} images, html {len(html)} chars",
        ):
            self.setHtml(self._render_html(html))
        _diag(
            "[diag] _apply_render post-setHtml: doc chars={}, "
            "block_count={}",
            doc.characterCount(), doc.blockCount(),
        )
        # Summary of loadResource calls driven by the setHtml above —
        # this path is called once per image during Qt's layout pass.
        if self._lr_count:
            _diag(
                "[diag] loadResource aggregate: {} calls, "
                "total {:.1f}ms, max {:.1f}ms",
                self._lr_count, self._lr_total_ms, self._lr_max_ms,
            )
        # The render task already sized images to the content-area width
        # (``_render_max_w``).  If the viewport hasn't changed since
        # dispatch, the first paint already fits — skip the redundant
        # second full-document ``setHtml`` a reflow would trigger (which
        # on a 300-image post doubles the layout cost *and* re-dispatches
        # every not-yet-decoded image).  Only reflow if a resize slipped
        # in while the task was reading headers (``resizeEvent`` couldn't
        # schedule one yet — ``_img_targets`` was still empty).
        self._reflow_timer.stop()
        self._force_reflow = False  # 上の setHtml が焼き直しを済ませた
        vp_w = self.viewport().width()
        cur_max_w = max(100, (vp_w if vp_w > 0 else 1000) - 24)
        if abs(cur_max_w - self._render_max_w) < 2:
            self._last_reflow_width = self._render_max_w
        else:
            self._reflow_images(force=True)
        # The setHtml above re-runs ``loadResource`` for every image and the
        # resulting decode-flood evicts whatever overflows the LRU — often
        # the *visible* top images, since they decode first.  Schedule a
        # windowing pass so those get re-decoded once the flood settles.
        self._visible_timer.trigger()

    def _show_render_error(self, message: str) -> None:
        """The background render pipeline raised — show an error page.

        Without this the loading placeholder from ``_set_post_sync`` would
        linger forever on the affected post.  Escaped + routed through the
        single ``_render_html`` choke point, mirroring
        :meth:`_show_read_error`.
        """
        self._last_body_html = None
        self.setHtml(
            self._render_html(
                "<p>"
                + t(
                    "viewer.markdown_view.render_failed",
                    message=html.escape(message),
                )
                + "</p>"
            )
        )

    def wheelEvent(self, event):  # noqa: N802 (Qt API)
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            # Ctrl+wheel zooms the body font instead of scrolling / file
            # navigation — checked first so it never falls through to the
            # scroll-edge file-nav path below.
            if delta:
                step = 1 if delta > 0 else -1
                self.set_font_pt(self.font_pt() + step)
            event.accept()
            return
        if route_scroll_or_navigate(self, event, self.navigate_requested.emit):
            return
        super().wheelEvent(event)

    def changeEvent(self, event):  # noqa: N802 (Qt API)
        """テーマ切替に本文の焼き込み色を追従させる。

        ``_HTML_TEMPLATE`` の ``code``/``pre`` 背景と ``a.localpost`` の色は
        ``setHtml`` の時点で QTextDocument の文字書式へ**焼き込まれる**。
        ``apply_theme`` は QPalette / アプリ QSS / アイコンしか更新しない
        ので、表示中の投稿だけが旧テーマの色を保ち続け（暗い地に暗い引用
        文＝コントラスト 2〜3）、投稿を開き直すまで直らない。常駐
        トースト / タグチップと同じ「パレット変更で焼き直す」型で
        塞ぐ。``StyleChange`` は拾わない（``setHtml`` 起点の再帰を招く）。
        """
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.ThemeChange,
        ):
            self._rerender_theme()
        super().changeEvent(event)

    def _rerender_theme(self) -> None:
        """現在の本文を新しいテーマ色で焼き直す（スクロール位置は保つ）。

        画像リソースは ``setHtml`` を跨いで生き残る（``loadResource`` が
        LRU に当たる）ので、フォントサイズ変更と同じくレイアウトのみの
        コストで済む。本文がまだ無い（読み込み中・エラーページ）ときは
        何もしない。

        ``_last_body_html`` はレンダータスクが焼いた時点の width/height を
        保ち続ける（``_reflow_images`` は書き換え結果をローカルに留める）
        ので、これをそのまま ``setHtml`` するとリフロー済みの寸法が捨てら
        れる。しかも ``_last_reflow_width`` は現在幅のままなので、次の
        リフローも早期 return して復帰しない。``set_font_pt`` と
        同じく、リフロー可能なら幅の再適用ごと委譲する。
        """
        if self._last_body_html is None or self._theme_rerendering:
            return
        self._theme_rerendering = True
        try:
            if (
                self._img_targets
                and self._base_dir is not None
                and self.viewport().width() > 0
            ):
                # ``_reflow_images`` は _render_html + setHtml +
                # スクロール比復元を内包する（新テーマ色もそこで焼かれる）。
                self._reflow_images(force=True)
            else:
                sbar = self.verticalScrollBar()
                scroll_ratio = sbar.value() / max(1, sbar.maximum())
                self.setHtml(self._render_html(self._last_body_html))
                sbar.setValue(round(sbar.maximum() * scroll_ratio))
        finally:
            self._theme_rerendering = False

    def resizeEvent(self, event):  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        # Only schedule reflow once the header-read render task has
        # populated ``_img_targets`` — before then there's nothing to
        # rescale and the document is either empty or the loading notice.
        if self._img_targets:
            self._reflow_timer.trigger()

    def _schedule_forced_reflow(self) -> None:
        """デバウンス越しに強制リフローを予約する。

        バーストする経路（``_apply_image`` の到着ごと・設定適用）は
        ``_reflow_images`` を直接呼ばずタイマーへ束ねたいが、幅ガードは
        「幅が変わったか」しか見ないので素の予約では即 return される。
        意思を 1 ビットに預け、タイマースロットが ``force`` として渡す。
        """
        self._force_reflow = True
        self._reflow_timer.trigger()

    def _on_reflow_timeout(self) -> None:
        """``_reflow_timer`` のスロット: 予約された force を消費する。"""
        force = self._force_reflow
        self._force_reflow = False
        self._reflow_images(force=force)

    def _reflow_images(self, *, force: bool = False) -> None:
        """Rescale every ``<img>`` to the current viewport width.

        Rebuilds the body HTML from ``_last_body_html`` by rewriting each
        image's ``width``/``height`` attrs — using the aspect ratio
        cached in ``_img_targets`` — and re-applies it via ``setHtml``.
        Cursor-level char-format updates proved unreliable: the document
        layout didn't always pick up the new image dimensions, so rows
        stayed at the stale height while the width shrank.

        ``setHtml`` is cheap enough here because the per-image resource
        cache (populated by :meth:`_apply_image` via ``addResource``)
        survives — :meth:`loadResource` hits the cached ``QImage`` and
        no decode tasks are re-dispatched.  Scroll position is preserved
        manually.

        The width guard below suppresses the no-op re-layout a bare
        ``resizeEvent`` burst would cause, but it can only see
        the *width* — a caller that changed ``_img_targets`` for some other
        reason (font size, link re-classification, theme, a new cache budget,
        an image whose real size only became known at decode time) must pass
        ``force=True`` or its new boxes are never baked into the HTML.
        Only the ``resizeEvent`` path leaves
        it ``False``.
        """
        if not self._last_body_html or self._base_dir is None:
            return
        vp_w = self.viewport().width()
        if vp_w <= 0:
            return
        # Leave a small margin for the body's default padding/margin so
        # the final laid-out width stays strictly below the content area
        # and CSS ``max-width: 100%`` never clamps us (which would leave
        # the height untouched and distort the aspect ratio).
        max_w = max(100, vp_w - 24)
        if not force and abs(max_w - self._last_reflow_width) < 2:
            return
        self._last_reflow_width = max_w
        # 表示ボックスは毎回**元画像の実寸から**導出し直す。前回の
        # 表示ボックスを縮める形にすると、狭い幅で開いた投稿はウィンドウを
        # 広げても初回幅を超えて拡大されない。
        self._img_targets = self._derive_targets(max_w)

        def _rewrite(m: re.Match[str]) -> str:
            tag = m.group(0)
            src_match = re.search(r'src="([^"]+)"', tag)
            if not src_match:
                return tag
            local = _img_src_key(src_match.group(1))
            target = self._img_targets.get(local)
            if target is None or target.width() <= 0:
                return tag
            new_w, new_h = target.width(), target.height()
            # Replace existing width/height attrs (render task always
            # injects both) — fall back to inserting them if absent.
            out = tag
            if re.search(r'\swidth="\d+"', out):
                out = re.sub(r'\swidth="\d+"', f' width="{new_w}"', out, count=1)
            else:
                out = out.replace("<img", f'<img width="{new_w}"', 1)
            if re.search(r'\sheight="\d+"', out):
                out = re.sub(r'\sheight="\d+"', f' height="{new_h}"', out, count=1)
            else:
                out = out.replace("<img", f'<img height="{new_h}"', 1)
            return out

        new_body = re.sub(r"<img\b[^>]*>", _rewrite, self._last_body_html)
        sbar = self.verticalScrollBar()
        scroll_pos = sbar.value()
        scroll_max = max(1, sbar.maximum())
        scroll_ratio = scroll_pos / scroll_max
        with _StageTimer(
            "_reflow_images: setHtml",
            f"max_w={max_w}, {len(self._img_targets)} images",
        ):
            self.setHtml(self._render_html(new_body))
        # Restore scroll position proportionally — absolute value would
        # drift after images resize because total document height changes.
        new_max = sbar.maximum()
        sbar.setValue(round(new_max * scroll_ratio))

    # Shared 1×1 transparent placeholder returned by ``loadResource``.
    # Size is driven by the ``width``/``height`` HTML attrs injected
    # upstream, so the placeholder's own pixels don't affect layout.
    # Building it once and reusing it avoids per-image allocation cost
    # that previously made ``setHtml`` freeze the GUI for seconds on
    # image-heavy posts.
    _PLACEHOLDER_IMAGE: QImage | None = None

    @classmethod
    def _placeholder(cls) -> QImage:
        if cls._PLACEHOLDER_IMAGE is None:
            img = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            cls._PLACEHOLDER_IMAGE = img
        return cls._PLACEHOLDER_IMAGE

    def _schedule_repaint(self) -> None:
        """Request one coalesced full-document repaint on the next tick.

        Called from the decode-completion hot paths instead of dirtying
        the document per image; a burst of arrivals collapses into a
        single ``markContentsDirty`` when :meth:`_flush_repaint` fires.
        """
        self._repaint_timer.trigger()

    def _flush_repaint(self) -> None:
        doc = self.document()
        doc.markContentsDirty(0, doc.characterCount())

    def _dispatch_decode(
        self, url_key: str, name: QUrl, path: Path, target: QSize,
    ) -> None:
        """Queue a background decode for *url_key* unless one is in flight.

        Shared by the "slot is empty" and "slot holds a too-small decode"
        branches of :meth:`loadResource`.  Every ``setHtml`` (font change /
        reflow / refresh_links) re-invokes ``loadResource`` for all ``<img>``,
        so without the dedupe a burst of Ctrl+wheel would multiply-dispatch
        the same decode and starve the other images.  ``_apply_image`` /
        ``_apply_image_failed`` discard the key.

        投入は ``submit_batch``（加算的）— ``_pending_redecode`` が「要求済み」
        の帳簿なので、後続の投入が先行を捨てるとその鍵の答えが二度と返らず、
        枠が白いまま残る。
        """
        if url_key in self._pending_redecode:
            return
        self._pending_redecode.add(url_key)
        url = QUrl(name)
        dpr = self.devicePixelRatioF()
        box = QSize(target)
        self._img_stream.submit_batch(
            lambda _job: _decode_post_image(url, path, box, dpr)
        )

    def _needs_sharper(
        self, local: str, image: QImage, target: QSize
    ) -> bool:
        """True when *image* has fewer pixels than *target* now needs.

        The LRU is keyed by URL only, so a decode made for a narrow window
        stays valid-looking after the user widens it — Qt would upscale it
        into the (now larger) layout box and the image looks soft.  Compare
        against the *physical* target, capped by the source's own width so
        an image smaller than the box never re-decodes forever
        (``decode_qimage`` never upscales past the source).
        """
        dpr = max(1.0, self.devicePixelRatioF() or 1.0)
        want = round(target.width() * dpr)
        src = self._img_sources.get(local)
        if src is not None and src.width() > 0:
            want = min(want, src.width())
        return image.width() + 1 < want

    def loadResource(self, type_: int, name: QUrl):  # noqa: N802 (Qt API)
        t0 = time.perf_counter()
        slow_path = ""
        try:
            if (type_ == QTextDocument.ResourceType.ImageResource
                    and self._base_dir is not None):
                # Qt resolves relative URLs against ``baseUrl`` *before*
                # calling us, so ``name`` is typically absolute
                # (``file:///N:/.../image.jpg``).  Compute the relative
                # form ourselves so we can look it up in ``_img_targets``
                # using the same key the render task stored.
                local_file = name.toLocalFile()
                local = None
                candidate: Path | None = None
                if local_file:
                    candidate = Path(local_file)
                    try:
                        local = candidate.relative_to(self._base_dir).as_posix()
                    except ValueError:
                        local = None
                if local is not None:
                    target = self._img_targets.get(local)
                    if target is not None:
                        # Re-entry from scroll after eviction: the cached
                        # QImage is still in our bounded LRU even though
                        # QTextDocument dropped it to a placeholder.  Pull
                        # it back without a decode and clear the eviction
                        # mark so we don't re-evict on the next call.
                        url_key = name.toString()
                        cached = self._img_cache.get(url_key)
                        if cached is not None:
                            self._evicted_urls.discard(url_key)
                            # ウィンドウを広げた後は表示ボックスが伸びる —
                            # 手持ちの画素で足りなければ、いま出せるものを
                            # 返しつつ背面で大きい方を焼き直す（空白を挟ま
                            # ずに解像度だけ追いつく）。
                            if self._needs_sharper(local, cached, target):
                                self._dispatch_decode(
                                    url_key, name, candidate, target
                                )
                            return cached
                        # Fast path: render task already verified the
                        # file and cached the target size.  Kick off the
                        # background decode and return the shared 1×1
                        # placeholder — HTML width/height drives layout
                        # so Qt never needs to touch the real file here.
                        #
                        # Dedupe against in-flight decodes: every setHtml
                        # (font change / reflow / refresh_links) re-invokes
                        # loadResource for all <img>.  Without this guard an
                        # image still decoding would get a *fresh* task per
                        # setHtml — bursts of Ctrl+wheel during initial load
                        # multiply-dispatch the same decode and starve other
                        # images.  ``_apply_image`` discards the key.
                        self._dispatch_decode(
                            url_key, name, candidate, target
                        )
                        return self._placeholder()
                    # No cached size（ヘッダからサイズを読めなかった画像 /
                    # ターゲット表から漏れたキー）— GUI
                    # スレッドで原寸を**同期**デコードすると、結果は LRU にも
                    # 入らず予算契約の外に置かれる。他の画像と
                    # 同じ扱いに揃える: 原寸ターゲット（空 QSize → タスク側で
                    # target=None = 原寸デコード）の非同期タスクを積んで即
                    # プレースホルダを返す。実寸は ``_apply_image`` が
                    # ``image.size()`` から登録し、リフローを 1 回蹴って
                    # レイアウトを収束させる。
                    if candidate is not None and candidate.is_file():
                        slow_path = "async-size-fallback"
                        # 診断トグル配下（``_diag``）。``loadResource`` は全
                        # ``setHtml``（初回描画・リフロー・フォント 1 ノッチ・
                        # ``refresh_links``・テーマ切替）で全 ``<img>`` ぶん
                        # 呼ばれるので、無条件 WARNING だとサイズを読めない
                        # 画像 1 本につき何度も出てログが肥大する。実際に
                        # 遅かった呼び出しは下の ``finally`` が WARNING で
                        # 拾う（同じ理由で既に絞った分岐と揃える）。
                        _diag(
                            "[diag] loadResource fallback "
                            "(no cached size): {}", candidate,
                        )
                        self._dispatch_decode(
                            name.toString(), name, candidate, QSize()
                        )
                        return self._placeholder()
            if type_ == QTextDocument.ResourceType.ImageResource:
                # 不変条件: 本文画像は Qt にデコードさせない。ここまで来るのは
                # ディスクに無い参照・投稿フォルダ外・ローカルでない URL で、
                # ``super().loadResource`` へは落とさない — 落とすと Qt はその空
                # リソースに同梱の壊れ画像グリフを GUI スレッドで画像プラグイン
                # ファクトリ経由でデコードするが、その経路は
                # ``qimage_decode._QT_READER_LOCK`` を取らないため、ワーカーの
                # Qt ラダー（0 バイト / 途中切れ画像の ``QImageReader.read``）と
                # ファクトリのロック・GIL を交差して取り合い、プロセス全体が
                # 永久停止しうる。QPainter だけで描く
                # 「読み込めなかった画像」の枠を返す — デコード失敗の枠と揃う。
                slow_path = "missing-image"
                return self._broken_marker()
            slow_path = slow_path or "super-loadResource"
            return super().loadResource(type_, name)
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._lr_count += 1
            self._lr_total_ms += elapsed_ms
            if elapsed_ms > self._lr_max_ms:
                self._lr_max_ms = elapsed_ms
            # Flag slow individual calls so outliers are visible even
            # though the aggregate summary hides them.  ``missing-image``
            # は「post.md が参照している画像がディスクに無い」という**日常的
            # な**事象（取得失敗・先行書き込み）で必ず立つ。所要時間に関係なく
            # WARNING を出すと、壊れ参照 1 本につき初回描画・
            # リフロー・フォント 1 ノッチ・refresh_links の**全 setHtml** で
            # 繰り返され、``_diag`` を作った理由（常時 INFO のログ肥大）が
            # WARNING レベルで再演する。
            # 無条件 WARNING は実際に遅かった呼び出しと同期デコード
            # フォールバックだけに絞り、残りは診断トグル配下へ落とす。
            if elapsed_ms >= 50.0:
                logger.warning(
                    "[diag] loadResource slow: {:.1f}ms "
                    "(path={}, name={})",
                    elapsed_ms, slow_path or "fast-path",
                    name.toString(),
                )
            elif slow_path:
                _diag(
                    "[diag] loadResource: {:.1f}ms (path={}, name={})",
                    elapsed_ms, slow_path, name.toString(),
                )

    def _on_image_landed(self, payload: object) -> None:
        """本文画像デコードの着地（``ready`` / ``failed`` の 1 本口）."""
        if not isinstance(payload, StreamOutcome):
            return
        kind = cast(_MdImageKind, payload.kind)
        match kind:
            case "ready":
                url_and_image = cast("tuple[QUrl, QImage]", payload.value)
                self._apply_image(*url_and_image)
            case "failed":
                self._apply_image_failed(cast(QUrl, payload.value))
            case _:
                assert_never(kind)

    def _apply_image(self, url: QUrl, image: QImage) -> None:
        t0 = time.perf_counter()
        # Swap the cached resource.  ``addResource`` alone updates the
        # cache but doesn't trigger a paint; the paint invalidation
        # (``markContentsDirty`` over the full doc) is coalesced across
        # the decode flood via ``_schedule_repaint`` so hundreds of
        # arrivals collapse into one repaint per frame instead of
        # dirtying an O(doc-size) region hundreds of times.
        doc = self.document()
        doc.addResource(
            QTextDocument.ResourceType.ImageResource, url, image
        )
        url_key = url.toString()
        self._evicted_urls.discard(url_key)
        self._pending_redecode.discard(url_key)
        # サイズ未知フォールバックの後始末: ``loadResource`` は
        # ヘッダからサイズを読めなかった画像も同期デコードせず原寸の非同期
        # タスク + プレースホルダで返す。実寸はこのデコード結果で
        # 初めて判明するので、未登録キーを ``_img_sources`` / ``_img_targets``
        # に登録し、リフローを 1 回蹴って 1×1 プレースホルダのままのレイアウト
        # を正しい寸法へ収束させる（以降は他の画像と完全に同じ経路に乗る）。
        local = self._local_key_from_url(url)
        if local is not None and local not in self._img_targets:
            src = image.size()  # 原寸デコード結果 = 元画像の実寸
            if src.width() > 0 and src.height() > 0:
                self._img_sources[local] = QSize(src)
                max_w = max(1, self._last_reflow_width or self._render_max_w)
                self._img_targets[local] = _layout_box(
                    QSize(src), max_w, self._decode_px_budget()
                )
                # 幅ガードを迂回する強制リフロー: ガードは
                # 「幅が変わったか」だけを門番にしているので、素の予約だと
                # ウィンドウ幅が同じ通常ケースで必ず即 return し、
                # ``_last_body_html`` の ``<img>`` に width/height が焼き込ま
                # れない = この画像は 1×1 のまま二度と広がらない。到着は
                # バーストするのでタイマー越しに束ねる。
                self._schedule_forced_reflow()
        # Mirror the decoded image into the bounded LRU and evict older
        # entries from QTextDocument so the document's resident memory
        # stays near ``MARKDOWN_CACHE_MAX_BYTES``.  Swapping a placeholder
        # back in via ``addResource`` releases the real ``QImage`` — the
        # next ``loadResource`` for that URL will hit our LRU if it's
        # still there, or re-decode if it was pushed out too.
        # 可視帯の画像は退避させない（``protect=``）。予算より可視窓が広い
        # 設定では、退避 → 再デコード → また別の可視画像を退避、が止まらず
        # 画像が空白のまま CPU を焼き続けていた。protect を守れない挿入は
        # ``put`` が拒否するので「最初の数枚だけ残る」安定状態へ落ちる。
        evicted = self._img_cache.put(
            url_key, image, protect=self._visible_urls,
        )
        blanked = False
        for evicted_key in evicted:
            if evicted_key == url_key:
                # 予算より 1 枚が大きく、入れた直後に自分が押し出された。
                # ここでプレースホルダを貼ると、たった今 ``addResource`` した
                # 実画像を空白で上書きし、``_visible_timer`` → 再デコード →
                # 同じ結果、の周回になる。文書に載った実画像はそのまま残す。
                continue
            doc.addResource(
                QTextDocument.ResourceType.ImageResource,
                QUrl(evicted_key), self._placeholder(),
            )
            self._evicted_urls.add(evicted_key)
            blanked = True
        self._schedule_repaint()
        # An eviction may have blanked an image that's currently on screen
        # (the decode-flood on load evicts in arrival order, not visibility
        # order).  Debounced so the storm of decodes coalesces into a single
        # windowing pass that re-decodes only what's actually visible.
        if blanked:
            self._visible_timer.trigger()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._img_ready_count += 1
        self._img_ready_total_ms += elapsed_ms
        if elapsed_ms > self._img_ready_max_ms:
            self._img_ready_max_ms = elapsed_ms
        # Log a running summary every 10 images to avoid per-image spam
        # but still surface the cumulative GUI-thread cost while a post
        # is still filling in.  Log a final summary once all queued
        # decodes have arrived.
        target_total = len(self._img_targets)
        if (self._img_ready_count % 10 == 0
                or self._img_ready_count == target_total):
            final = " (final)" if self._img_ready_count == target_total else ""
            _diag(
                "[diag] _apply_image{}: {}/{} done, "
                "total {:.1f}ms, max {:.1f}ms",
                final,
                self._img_ready_count, target_total,
                self._img_ready_total_ms,
                self._img_ready_max_ms,
            )

    def _local_key_from_url(self, url: QUrl) -> str | None:
        """絶対 file URL を ``_img_targets`` のキー（base_dir 相対）へ写す.

        ``loadResource`` が ``candidate.relative_to(base_dir)`` で導出して
        いるのと同じ正規化 — サイズ未知フォールバックの登録
        （:meth:`_apply_image`）が同じキー体系に載るための共通化。
        base_dir 外・非ローカル URL は ``None``。
        """
        if self._base_dir is None:
            return None
        local_file = url.toLocalFile()
        if not local_file:
            return None
        try:
            return Path(local_file).relative_to(self._base_dir).as_posix()
        except ValueError:
            return None

    #: 壊れた本文画像に出す枠の寸法（論理ピクセル）。原寸が分からない画像
    #: なので固定の小さな箱にする — 失敗が分かれば十分で、本文の組版を
    #: 大きく崩さない大きさ。
    _BROKEN_MARK_SIZE = QSize(96, 72)

    def _broken_marker(self) -> QImage:
        """「読み込めなかった画像」を表す枠（テーマトークンで描く）。

        テーマ切替で色が変わるので毎回描き直す（失敗は稀なので実費は無い）。
        """
        tokens = current_tokens()
        img = QImage(
            self._BROKEN_MARK_SIZE, QImage.Format_ARGB32_Premultiplied
        )
        img.fill(Qt.transparent)
        w = img.width() - 1
        h = img.height() - 1
        painter = QPainter(img)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(
                0, 0, img.width(), img.height(),
                QColor(rgba(tokens.text_muted, 0.10)),
            )
            pen = QPen(QColor(tokens.border_strong))
            pen.setWidth(1)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(0, 0, w, h)
            pen.setStyle(Qt.PenStyle.SolidLine)
            pen.setColor(QColor(tokens.text_muted))
            painter.setPen(pen)
            inset_x, inset_y = w // 3, h // 3
            painter.drawLine(inset_x, inset_y, w - inset_x, h - inset_y)
            painter.drawLine(w - inset_x, inset_y, inset_x, h - inset_y)
        finally:
            painter.end()
        return img

    def _apply_image_failed(self, url: QUrl) -> None:
        """Release the in-flight mark for a decode that produced nothing.

        ``loadResource`` / ``_redecode_if_evicted`` add the URL to
        ``_pending_redecode`` *before* dispatching, and only
        :meth:`_apply_image` discards it.  A transient read failure (NAS
        blip) would therefore strand the key: both the ``loadResource``
        dedupe guard and the ``_redecode_if_evicted`` in-flight guard skip
        that slot forever and the image stays blank until the post is
        reopened.  Discarding here keeps add/discard symmetric on the
        failure path, so scrolling back re-dispatches the decode.

        The slot also gets a visible "couldn't read this" frame.  A failed
        decode used to leave the shared 1×1 transparent placeholder, so a
        broken file read as「元々そんな画像は無かった」.  A reference to a
        *missing* file gets the same frame straight from ``loadResource``
        (never Qt's own broken-image glyph, whose decode would bypass
        ``_QT_READER_LOCK``), so both failures look alike.
        """
        url_key = url.toString()
        self._pending_redecode.discard(url_key)
        marker = self._broken_marker()
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource, url, marker
        )
        # 組版は HTML の width/height に従うので、枠の寸法をターゲット表へ
        # 入れて 1 回だけリフローを蹴る（入れないと 1×1 のまま描かれる）。
        local = self._local_key_from_url(url)
        if local is not None:
            box = QSize(self._BROKEN_MARK_SIZE)
            if self._img_targets.get(local) != box:
                self._img_sources[local] = QSize(box)
                self._img_targets[local] = box
                self._schedule_forced_reflow()
        self._schedule_repaint()

    def _refresh_visible_images(self) -> None:
        """Re-decode evicted images that are at/near the viewport.

        Walks the document layout, restricts to the band spanning one
        screenful above and below the viewport, and for every image
        fragment whose resource the LRU evicted to a placeholder,
        re-dispatches a decode (or rehydrates straight from the bounded
        LRU if the pixels happen to still be there).  This is what makes
        the eviction reversible — ``QTextDocument`` will not re-request an
        evicted resource on its own, so without this pass early/off-screen
        images stay blank once scrolled away.

        The same walk records the band's URLs into ``_visible_urls``, which
        :meth:`_apply_image` hands to ``put(protect=…)`` — the pass only
        runs while something is evicted, i.e. exactly while the budget is
        tight enough for the protection to matter.
        """
        if self._base_dir is None:
            return
        if not self._evicted_urls:
            # 予算に余裕がある = 保護は不要。古い可視集合を持ち越すと、
            # スクロール後に画面外の画像を守って新しい挿入を拒み続ける。
            self._visible_urls.clear()
            return
        doc = self.document()
        layout = doc.documentLayout()
        if layout is None:
            return
        base_url = doc.baseUrl()
        visible: set[str] = set()
        top = self.verticalScrollBar().value()
        band_h = max(1, self.viewport().height())
        # Prefetch a screenful on each side so images decode just before
        # they scroll into view rather than blank-then-pop.
        lo = top - band_h
        hi = top + 2 * band_h
        block = doc.begin()
        while block.isValid():
            rect = layout.blockBoundingRect(block)
            if rect.top() > hi:
                break  # blocks are in document order — nothing below matters
            if rect.bottom() >= lo:
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid():
                        fmt = frag.charFormat()
                        if fmt.isImageFormat():
                            key = self._redecode_if_evicted(
                                fmt.toImageFormat().name(), base_url,
                            )
                            if key is not None:
                                visible.add(key)
                    it += 1
            block = block.next()
        self._visible_urls = visible

    def _redecode_if_evicted(
        self, name_str: str, base_url: QUrl
    ) -> str | None:
        """Re-populate one image slot if it's currently an evicted placeholder.

        ``name_str`` is the ``<img src>`` as written in the HTML (URL-encoded
        relative path).  ``base_url.resolved(...)`` reproduces exactly the
        absolute URL Qt handed to :meth:`loadResource` — and hence the key
        stored in ``_evicted_urls`` — so the membership test lines up.

        Returns that absolute URL key for any resolvable image fragment (the
        caller collects the band's keys into ``_visible_urls``), or ``None``
        when the fragment isn't one of ours.
        """
        if not name_str or self._base_dir is None:
            return None
        abs_url = base_url.resolved(QUrl(name_str))
        url_key = abs_url.toString()
        if url_key not in self._evicted_urls:
            return url_key
        local = _strip_dot_slash(urllib.parse.unquote(name_str))
        target = self._img_targets.get(local)
        if target is None:
            return url_key
        # Pixels may still be resident in the bounded LRU (evicted from the
        # QTextDocument but not yet pushed out of our cache) — rehydrate
        # without a decode in that case.
        cached = self._img_cache.get(url_key)
        if cached is not None:
            self._evicted_urls.discard(url_key)
            doc = self.document()
            doc.addResource(
                QTextDocument.ResourceType.ImageResource, abs_url, cached
            )
            self._schedule_repaint()
            return url_key
        # 投入は ``_dispatch_decode`` に一本化する（in-flight 判定・
        # ``_pending_redecode`` のマーク・dpr の取得がそこに集まっている）。
        self._dispatch_decode(
            url_key, abs_url, self._base_dir / local, target
        )
        return url_key

    # ------------------------------------------------------------------ slots

    def _on_anchor_clicked(self, url: QUrl) -> None:
        # 📁 local-jump anchor injected by ``_classified``: map the key back
        # to the downloaded post's folder and let the window navigate to it.
        # Handled before the scheme gate so it works regardless of state.
        if url.scheme() == LOCAL_SCHEME:
            prefix = LOCAL_SCHEME + ":"
            s = url.toString()
            key = s[len(prefix):] if s.startswith(prefix) else url.path()
            folder = self._local_post_targets.get(key)
            if folder is not None:
                self.post_link_clicked.emit(folder)
            return
        # post.md bodies are untrusted (external-tool input): only web/mail
        # schemes may reach ``QDesktopServices.openUrl`` (= OS default
        # handler).  Everything else either resolves to a file inside the
        # post directory (emitted for in-app handling below) or is dropped —
        # a ``[開く](file://attacker-nas/share/evil.exe)`` link must never
        # launch the OS handler, with or without a base dir.  Mirrors
        # ``info_panel.InfoPanel._open_link``.
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        if self._base_dir is None or scheme not in ("", "file"):
            return

        # Resolve within the post directory only. ``QTextBrowser`` hands us
        # either a searchPaths-resolved *absolute* ``file://`` URL — POSIX
        # ``/home/…/post/attachment.txt`` or Windows ``C:/…/post/x.txt`` — or
        # a bare relative path. An absolute path is accepted only when it lives
        # inside ``_base_dir`` (never rewrite it — ``lstrip("/")`` corrupted the
        # POSIX form into a non-existent relative path); a relative path is
        # joined onto the post dir. Anything outside is dropped (never falls
        # through to the OS handler), keeping the "limited to the post
        # directory" contract.
        #
        # ``Path.is_relative_to`` compares *lexically* and does not collapse
        # ``..``, so a crafted ``…/post/../../etc/passwd`` (absolute form) or a
        # bare ``../../etc/passwd`` (relative form) would otherwise slip past the
        # containment check and let a post-md body preview a file outside the
        # post dir. Lexically normalise with ``os.path.normpath`` *before* the
        # check — deliberately not ``resolve()``, which walks symlinks on disk
        # and would reject legitimate links in NAS layouts where the post tree
        # is reached through a symlinked share. Normalising is purely textual, so
        # it collapses the ``..`` segments without touching the filesystem.
        raw = url.toLocalFile() or url.path()
        local = Path(raw)
        base = Path(os.path.normpath(self._base_dir))
        candidate: Path | None
        if local.is_absolute():
            norm = Path(os.path.normpath(local))
            candidate = norm if norm.is_relative_to(base) else None
        else:
            # Join first, then normalise the combined path and re-check that it
            # stays inside the post dir — the join alone leaves ``..`` segments
            # unresolved, so a relative ``../../…`` would escape the base.
            norm = Path(os.path.normpath(base / raw))
            candidate = norm if norm.is_relative_to(base) else None
        if candidate is not None and candidate.is_file():
            self.file_link_clicked.emit(candidate)
        # else: dropped — never fall through to ``QDesktopServices.openUrl``.

    # ------------------------------------------------------------- table of contents

    def _heading_blocks(self) -> list[tuple[int, str, int]]:
        """Walk the current document for heading blocks.

        Returns ``(level, text, block_number)`` tuples in document order.
        Reads whatever the document currently holds — safe to call while an
        image post's async render is still in flight, since the visible
        headings (from the markdown body) don't change once the initial
        text lands (only image sizing/decoding is still pending).
        """
        headings: list[tuple[int, str, int]] = []
        block = self.document().begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level > 0:
                text = block.text().strip()
                if text:
                    headings.append((level, text, block.blockNumber()))
            block = block.next()
        return headings

    def _goto_block(self, block_number: int) -> None:
        block = self.document().findBlockByNumber(block_number)
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        # setTextCursor alone can leave the block just outside the viewport
        # when the cursor has no selection; ensureCursorVisible scrolls the
        # minimum amount needed to bring it fully into view.
        self.ensureCursorVisible()

    def _build_toc_menu(self, parent: QMenu) -> QMenu:
        toc_menu = QMenu(t("viewer.markdown_view.toc_menu"), parent)
        headings = self._heading_blocks()
        if not headings:
            empty_act = toc_menu.addAction(t("viewer.markdown_view.no_headings"))
            empty_act.setEnabled(False)
            return toc_menu
        min_level = min(level for level, _text, _bn in headings)
        for level, text, block_number in headings:
            indent = "    " * (level - min_level)
            label = f"{indent}{text}"
            act = toc_menu.addAction(label)
            act.triggered.connect(
                lambda _=False, bn=block_number: self._goto_block(bn)
            )
        return toc_menu

    def _image_path_at(self, pos) -> Path | None:
        """Resolve the local image file under the cursor at *pos*, if any.

        The click position can land exactly on the image character or, in
        some layouts, just past it — so both the format at the cursor and
        the format of the character immediately before it are checked.
        """
        if self._base_dir is None:
            return None
        cursor = self.cursorForPosition(pos)
        candidates = []
        fmt = cursor.charFormat()
        if fmt.isImageFormat():
            candidates.append(fmt.toImageFormat())
        before = QTextCursor(cursor)
        if before.position() > 0:
            before.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
            fmt_before = before.charFormat()
            if fmt_before.isImageFormat():
                candidates.append(fmt_before.toImageFormat())
        for img_fmt in candidates:
            resolved = resolve_markdown_image_path(img_fmt.name(), self._base_dir)
            if resolved is not None:
                return resolved
        return None

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt API)
        menu = self._build_context_menu(event.pos())
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _build_context_menu(self, pos) -> QMenu:
        """右クリックメニューを組む（テストから exec なしで検査できるよう分離）."""
        menu = self.createStandardContextMenu(pos)
        if self._similar_search_available:
            image_path = self._image_path_at(pos)
            if image_path is not None:
                menu.addSeparator()
                similar_act = menu.addAction(t("viewer.common.similar_search_image"))
                similar_act.triggered.connect(
                    lambda _=False, p=image_path:
                    self.similar_search_requested.emit(p, None)
                )
        menu.addSeparator()
        # Ctrl+ホイールの本文フォントサイズは
        # ``ViewerState.markdown_font_pt`` へ**永続**するのに、設定 UI にも
        # メニューにも既定へ戻す手段が無いと「隠し設定」になる（誤操作で極端な
        # サイズに固定されると自力で戻せない）。変更した状態のときだけ復帰
        # 項目を出す。
        if self.font_pt() != _DEFAULT_FONT_PT:
            reset_act = menu.addAction(
                t("viewer.markdown_view.reset_font_size")
            )
            # 復帰は「0 = アプリ既定に従う」を永続させる専用経路へ。
            reset_act.triggered.connect(lambda _=False: self.reset_font_pt())
            menu.addSeparator()
        toc_menu = self._build_toc_menu(menu)
        menu.addMenu(toc_menu)
        # F05: unified 「既定アプリで開く / エクスプローラで開く / フルパスをコピー」
        # for the post.md file itself, shared with the other centre-pane views.
        if self._post_path is not None:
            menu.addSeparator()
            # 標準のテキスト項目（コピー / すべて選択）の下に共通ブロック。見出し
            # は標準項目の上に来て紛らわしいので付けない（TextView と同じ）。
            append_entry_verbs(
                menu,
                EntryMenuContext(
                    path=self._post_path, is_dir=False,
                    curation=curation_hooks_from_ancestors(self), host=self,
                    header=False,
                ),
            )
        return menu


__all__ = [
    "DEFAULT_FONT_PT",
    "MAX_FONT_PT",
    "MIN_FONT_PT",
    "MarkdownView",
    "_DEFAULT_FONT_PT",
    "_HTML_TEMPLATE",
    "_IMG_ONLY_P_RE",
    "_MAX_FONT_PT",
    "_MIN_FONT_PT",
    "_POST_IDENTITY_KEYS",
    "_img_src_key",
    "_layout_box",
    "_post_header_html",
    "_rewrap_img_paragraphs",
    "_strip_dot_slash",
    "_TEXT_ONLY_RENDER_MAX_CHARS",
    "_theme_style_args",
    "resolve_markdown_image_path",
]
