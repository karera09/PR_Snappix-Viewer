"""Shared mutable preview settings + small cross-view helpers.

This module is the single home for the runtime-mutable preview tunables
(scroll speed, wheel-navigation grace, text-preview cap, ZIP-preview cap)
and the tiny helpers shared by every preview sub-view (edge classification
for wheel navigation, byte formatting, explorer reveal, pixel scrolling).

The settings live here as module globals so the various view modules
(``image_view``, ``markdown_view``, ``content_view`` …) can read them via
*live* module-attribute access (``from . import view_prefs`` then
``view_prefs._PREVIEW_SCROLL_PIXELS``).  Reading them through a plain
``from .view_prefs import _PREVIEW_SCROLL_PIXELS`` would freeze the value
at import time and silently break the settings dialog, which mutates these
globals at runtime via the ``set_*`` functions below.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from PySide6.QtCore import Qt

from ..common.format import format_bytes
from ._runnable import GuardedSignals, run_detached


# Pixels scrolled per mouse-wheel notch in the preview pane (MarkdownView /
# ImageView).  Qt's default for QAbstractScrollArea is "wheelScrollLines × line
# height" (~60 px), which feels slow on tall posts and large images.  Settable
# at runtime from 設定ダイアログ ▸ 表示 → 「プレビューのスクロール量」
# (UIレビュー 07-25 #128 — 旧「表示メニューの項目」という記述は移設で陳腐化)。
_PREVIEW_SCROLL_PIXELS = 120


def set_preview_scroll_pixels(pixels: int) -> None:
    global _PREVIEW_SCROLL_PIXELS
    _PREVIEW_SCROLL_PIXELS = max(20, min(800, int(pixels)))


def get_preview_scroll_pixels() -> int:
    return _PREVIEW_SCROLL_PIXELS


def _scroll_with_pixels(widget, event) -> bool:
    """Scroll *widget*'s vertical scrollbar by ``_PREVIEW_SCROLL_PIXELS`` per
    notch.  Returns True when the event was consumed.

    Skips Ctrl/Shift modifiers (zoom, horizontal scroll) and pure-horizontal
    wheels so the existing handlers can run.
    """
    if event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
        return False
    ady = event.angleDelta().y()
    if not ady or event.angleDelta().x():
        return False
    notches = ady / 120.0
    vbar = widget.verticalScrollBar()
    vbar.setValue(vbar.value() - int(round(notches * _PREVIEW_SCROLL_PIXELS)))
    event.accept()
    return True


# Grace period: how long the user must keep scrolling past the edge before
# we actually switch files.  Without this, a single wheel flick that happens
# to land right at the boundary would immediately jump to the next file,
# which feels jarring — especially on long post.md / tall images where the
# user was reading, not intending to navigate.  Mutable via the settings
# dialog so users with different wheel hardware / preferences can tune it
# (0 = navigate immediately on first at-edge wheel).
_WHEEL_NAV_GRACE_SEC = 0.45
# If no at-edge wheel arrives for this long, the grace window restarts —
# the user paused, so treat the next flick as a fresh gesture.
_WHEEL_NAV_IDLE_RESET_SEC = 0.8


def set_wheel_nav_grace_ms(ms: int) -> None:
    global _WHEEL_NAV_GRACE_SEC
    _WHEEL_NAV_GRACE_SEC = max(0, min(3000, int(ms))) / 1000.0


def get_wheel_nav_grace_ms() -> int:
    return int(round(_WHEEL_NAV_GRACE_SEC * 1000.0))


# Image preview wheel assignment (F01).  Default OFF keeps the historical
# mapping: a plain wheel steps to the previous / next sibling file and
# Ctrl+wheel zooms.  When ON the two swap — a plain wheel zooms and the
# file-stepping moves to Ctrl+wheel — for users who treat the preview like a
# zoomable canvas.  Read *live* (module-attribute access) by
# ``ImageView.wheelEvent`` so the settings dialog toggle applies at once.
_IMAGE_WHEEL_ZOOM = False


def set_image_wheel_zoom(on: bool) -> None:
    global _IMAGE_WHEEL_ZOOM
    _IMAGE_WHEEL_ZOOM = bool(on)


def get_image_wheel_zoom() -> bool:
    return _IMAGE_WHEEL_ZOOM


# Fit-to-window 100% ceiling (F03).  Default ON: an image smaller than the
# viewport is shown at its natural size (centered) instead of being stretched
# past 100% and going soft.  OFF restores the historical "always fill the
# viewport" behaviour.  Read *live* by ``ImageView``'s fit calculations.
_IMAGE_FIT_NO_UPSCALE = True


def set_image_fit_no_upscale(on: bool) -> None:
    global _IMAGE_FIT_NO_UPSCALE
    _IMAGE_FIT_NO_UPSCALE = bool(on)


def get_image_fit_no_upscale() -> bool:
    return _IMAGE_FIT_NO_UPSCALE


# Archives larger than this skip the file-listing pass entirely.
# Reading the central directory is cheap (a small index at the end
# of the file — no extraction needed), but NAS I/O can still add
# a perceptible pause for very large archives.  Same threshold is
# reused by ``ViewerWindow`` to decide whether to extract a ZIP to a
# temp directory and treat it as a browseable folder.  Mutable at
# runtime via ``set_zip_preview_size_limit`` so the settings dialog
# can change the value without restart.
_ZIP_PREVIEW_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB


def set_zip_preview_size_limit(byte_limit: int) -> None:
    global _ZIP_PREVIEW_SIZE_LIMIT
    _ZIP_PREVIEW_SIZE_LIMIT = max(1, int(byte_limit))


def get_zip_preview_size_limit() -> int:
    return _ZIP_PREVIEW_SIZE_LIMIT


# Maximum PDF size the preview will read into memory.  Unlike the other two
# leaves this cap guards **resident** memory, not just a read pause:
# ``QPdfView`` renders pages lazily, so the ``QBuffer`` holding the file's
# bytes must stay open for as long as the PDF page is shown (peak is 2N —
# the ``bytes`` plus the ``QByteArray`` copy handed to the buffer).  The
# preview is also reached **automatically**: selecting a folder whose
# representative file is a PDF routes through ``show_pdf``, so a 200 MB
# scanned book costs that much resident memory without the user ever asking
# to open it.  Over the cap the reader skips the read entirely and the view
# says so (「既定アプリで開く」 still works).  Mutable at runtime via
# ``set_pdf_preview_size_limit`` so the settings dialog can change it
# without restart (レビュー 2026-09-03 項目#69).
_PDF_PREVIEW_SIZE_LIMIT = 100 * 1024 * 1024  # 100 MiB


def set_pdf_preview_size_limit(byte_limit: int) -> None:
    global _PDF_PREVIEW_SIZE_LIMIT
    _PDF_PREVIEW_SIZE_LIMIT = max(1, int(byte_limit))


def get_pdf_preview_size_limit() -> int:
    return _PDF_PREVIEW_SIZE_LIMIT


# Maximum bytes to read for the text preview.  Files larger than this still
# show the header info but the body is truncated with a notice.  Mutable
# via the settings dialog (``set_text_preview_max_bytes``) so users can
# raise the cap for browsing large logs / CSV without restart.
_TEXT_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB


def set_text_preview_max_bytes(byte_limit: int) -> None:
    global _TEXT_MAX_BYTES
    _TEXT_MAX_BYTES = max(64 * 1024, int(byte_limit))


def get_text_preview_max_bytes() -> int:
    return _TEXT_MAX_BYTES


# Maximum bytes of a ``.md`` **body** the post view reads.  Deliberately much
# lower than the plain-text cap above: the two leaves do not cost the same.
# TextView hands its bytes to a plain-text document, while MarkdownView turns
# them into HTML and calls ``setHtml`` — and *that* call is GUI-thread-bound
# (QTextDocument is not thread-affine enough to build off-thread) and grows
# super-linearly with the document: a 3.2 MiB .md measured ~1.0 s of frozen
# UI **after** the read cap and the off-thread render were in place.  A real
# ``post.md`` body is a few KiB, so this ceiling only ever bites files that
# are not posts (a renamed log, a generated report), which the truncation
# notice sends to 「既定アプリで開く」.  Not user-tunable: the settings dialog's
# text-preview slider is about how much *text* to show, not about how long
# the GUI may freeze.  The plain-text cap still applies as an upper bound, so
# lowering that lowers this too.
_MARKDOWN_BODY_MAX_BYTES = 256 * 1024  # 256 KiB


def get_markdown_body_max_bytes() -> int:
    return min(_MARKDOWN_BODY_MAX_BYTES, _TEXT_MAX_BYTES)


# ----------------------------------------------------------------- utilities


def _classify_edge(scrollbar, delta: int) -> tuple[bool, bool]:
    """Return ``(immediate, at_edge)`` for a wheel event in *delta*'s direction.

    * ``immediate`` — content fits within the viewport (no scrolling needed).
      Navigation should happen without the grace period, since the user has
      no reading-via-scroll gesture we could accidentally cut short.
    * ``at_edge`` — the scrollbar is already at its minimum/maximum in the
      wheel's direction.  Only when true should the caller emit a
      navigation request.

    **2026-08-28 ユーザー裁定で維持**（UIレビュー N-27「フィット時にホイールが
    猶予なく即ファイル送りになる」は見送り）: フィット表示ではスクロールという
    機能が被っていないためホイール＝ファイル送りが明示的で、猶予を挟むと
    もっさり感が出る。``immediate`` の意味づけと ``edge_nav`` 側の
    ``WheelNavGate`` 迂回はこのまま変更しないこと。
    """
    minimum = scrollbar.minimum()
    maximum = scrollbar.maximum()
    if maximum <= minimum:
        return True, True
    value = scrollbar.value()
    if delta > 0:
        return False, value <= minimum
    return False, value >= maximum


# Byte formatting now lives in ``common/format.py`` (shared with any
# plugin surface); this alias keeps the viewer-local import path the
# existing consumers use (status bar, settings dialog, content views, grid
# captions, detail window) pointing at the single implementation.
_format_bytes = format_bytes


#: アクション付き失敗トーストの表示時間（ms）。既定の 3 秒では「読んで・狙って
#: 押す」が間に合わない（``common/ui/toast`` の API docstring が明記する規約）。
_ACTION_TOAST_MS = 8000


def notify_failure(
    window,
    message: str,
    *,
    action_text: str | None = None,
    on_action=None,
) -> None:
    """*window* の属するトップレベルへ失敗を通知する（ベストエフォート）.

    「押したのに何も起きない」を残さないための共通ファネル (UIレビュー 07-25
    #74)。呼び出し側はウィジェットなら何でも渡せる（``None`` も可）。

    **着地面はトースト** (UIレビュー 2026-08-28 N-94): 成功側は 07-25 #122 で
    「``showMessage`` はステータスバー左端の現在パス表示（常設の『今どこに
    いるか』）を 3 秒間まるごと潰す。現在地を犠牲にする理由は無い」として
    トーストへ寄せてあり、同じ理由が失敗側にもそのまま当てはまる。片側だけ
    ステータスバーに残っていたのを揃える。

    ただし本関数は「ファネルを持たないホストでも動く」ことを約束している
    ので、段は 3 つに落とす: ``_show_toast`` → ``_show_status_message`` /
    ステータスバー → ログ。ホストを持たない小部品からの呼び出しでも約束は
    破れない。

    *action_text* / *on_action* を渡すと、トースト段に限り追随ボタンを 1 つ
    出す（N-40 で入った共通 API）。落ちた段では文言だけが出る — ボタンは
    近道であって唯一の入口ではない、という同 API の規約どおり。
    """
    host = None
    try:
        host = window.window() if window is not None else None
    except Exception:  # pragma: no cover (defensive — deleted C++ object)
        host = None
    if host is not None:
        toast = getattr(host, "_show_toast", None)
        if callable(toast):
            try:
                if action_text and on_action is not None:
                    toast(
                        message, "warning", _ACTION_TOAST_MS,
                        action_text, on_action,
                    )
                else:
                    toast(message, "warning")
                return
            except Exception:  # pragma: no cover (defensive)
                pass
        notify = getattr(host, "_show_status_message", None)
        if callable(notify):
            notify(message, 3000)
            return
        bar = getattr(host, "statusBar", None)
        if callable(bar):
            try:
                bar().showMessage(message, 3000)
                return
            except Exception:  # pragma: no cover (defensive)
                pass
    logger.warning("{}", message)


def _probe_exists(path: Path) -> bool:
    """*path* の実在 — **ワーカースレッドからのみ呼ぶこと**.

    OS へ出る 2 動詞（既定アプリで開く / エクスプローラで開く）が共有する
    唯一の存在確認。切断中の SMB 共有では 1 回で数十秒ブロックする（開発機の
    実測で、到達不能な UNC への ``exists()`` が 21 秒）ので、GUI スレッドから
    ここへ来る経路を作らないこと。
    """
    try:
        return path.exists()
    except OSError:  # pragma: no cover (defensive — 到達不能な NAS 等)
        return False


def _open_default_worker(path: Path) -> bool:
    """Existence probe + default-application launch — **runs off the GUI
    thread**.

    ワーカーでは ``QDesktopServices.openUrl`` を使わず「プロセス / シェル起動」
    へ機構を揃える（``_reveal_worker`` と同じ形 = どのスレッドから呼んでも
    安全）。Windows では両者とも結局 ``ShellExecute``（verb 省略）に落ちるので
    関連付けの解決結果は変わらない — 変わるのは「QtGui の API をワーカー
    スレッドから呼ばない」という一点だけ。

    失敗通知の契約 (UIレビュー 07-25 #74) は保つ: 不在は存在確認で落とし、
    起動そのものの失敗は ``OSError`` で拾う（Windows の「関連付けが無い」も
    ``os.startfile`` からは ``OSError`` として上がる）。

    Returns ``True`` when the default application was launched.
    """
    import os
    import subprocess
    import sys

    if not _probe_exists(path):
        logger.warning("Cannot open missing path: {}", path)
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        logger.warning("Failed to open {}: {}", path, exc)
        return False
    return True


# The relay QObjects have no natural parent (both shell verbs are plain
# functions called from menus / buttons), so hold a strong reference
# until the worker's queued ``done`` lands — a garbage-collected relay
# mid-``emit`` crashes the process (same hazard as image_view/markdown_view #8).
_shell_relays: set[GuardedSignals] = set()


def open_with_default(path: Path, window=None) -> None:
    """*path* を OS の既定アプリで開く。失敗したら *window* へ通知する。

    「既定アプリで開く」の**唯一の実装** (UIレビュー 07-25 #74)。以前は
    右クリックメニュー・各プレビューのボタン・ダブルクリック経路がそれぞれ
    ``QDesktopServices.openUrl`` を直接呼んでおり、右クリック経路だけが戻り値を
    捨てて失敗を握りつぶしていた（関連付けの無い拡張子で完全に無反応）。
    ここに集約して、どの入口でも失敗が必ず可視化されるようにする。

    **起動を GUI スレッドで行わない**: 到達不能な共有の上のファイルに対して
    GUI スレッドで ``QDesktopServices.openUrl`` を撃つと、開発機の実測で
    **42 秒**ウィンドウが凍った（シェルは関連付けを解決するまでに同じ共有へ
    2 往復する — ``exists()`` 単体は 21 秒）。隣の「エクスプローラで開く」が
    先に逃がしたのと同じ待ちなので、同じ形へ揃える: 確認と起動は
    ``_open_default_worker`` がワーカーで行い、失敗通知だけを GUI スレッドへ
    戻す（この関数自体は即座に戻る非同期 API なので戻り値は持たない）。
    """
    from ..common.i18n import t

    signals = GuardedSignals()
    _shell_relays.add(signals)

    def _on_done(_token: int, ok: object) -> None:
        _shell_relays.discard(signals)
        if ok:
            return
        # 失敗の主因は「その拡張子に関連付けが無い」— 次の一手はフォルダを
        # 開いて自分でアプリを選ぶこと。同じモジュールの既存導線をトーストの
        # 追随ボタンから 1 クリックで出す（UIレビュー 2026-08-28 N-94。
        # メニューの「エクスプローラで開く」も残るので、これは近道であって
        # 唯一の入口ではない）。
        notify_failure(
            window,
            t("viewer.main_window.open_default_failed"),
            action_text=t("common.action.open_in_explorer"),
            on_action=lambda p=path, w=window: _reveal_in_explorer(p, w),
        )

    signals.done.connect(_on_done)
    run_detached(
        0, lambda p=path: _open_default_worker(p), signals,
        label="open-with-default",
    )


def _reveal_worker(path: Path) -> bool:
    """Existence probe + file-manager launch — **runs off the GUI thread**.

    Both halves are filesystem-bound: ``exists()`` on a disconnected NAS
    blocks until the SMB timeout (tens of seconds — the very reason
    ``bookmark_dialog`` runs its ``is_dir()`` in a worker), and spawning the
    file manager is a process launch.  Running them off-thread keeps
    「エクスプローラで開く」from freezing the window while preserving the
    failure notice added by UIレビュー 07-25 #74 (レビュー 2026-07-31 #61).

    Returns ``True`` when the file manager was launched.
    """
    import subprocess
    import sys

    if not _probe_exists(path):
        logger.warning("Cannot reveal missing path: {}", path)
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as exc:
        logger.warning("Failed to reveal {}: {}", path, exc)
        return False
    return True


def _reveal_in_explorer(path: Path, window=None) -> None:
    """Best-effort: open the OS file manager with *path* selected.

    *window* を渡すと失敗をステータスへ通知する（UIレビュー 07-25 #74 —
    「既定アプリで開く」と同じく、押したのに無反応を残さない）。

    **追修 (UIレビュー07-25 #74)**: Windows の ``explorer /select,`` は
    存在しないパスを渡しても起動自体は成功する（``Popen`` は OSError を
    投げず、エクスプローラは既定の場所を開くか無反応）。つまり削除・改名済み
    のファイルに対してだけは「押したのに無反応」が残っていた。プロセスを
    起こす前に存在を確認し、無ければ ``open_with_default`` と同じ失敗通知へ
    落とす（OSError の捕捉は他 OS / 起動失敗のためそのまま残す）。

    **その存在確認は GUI スレッドで行わない (レビュー 2026-07-31 #61)**:
    全ペインの右クリックメニューと各プレビューのボタンがここへ合流するため、
    切断中の NAS 上のエントリで押すと SMB タイムアウトまでウィンドウが凍って
    いた。確認と起動は ``_reveal_worker`` がワーカーで行い、失敗通知だけを
    GUI スレッドへ戻す（この関数自体は即座に戻る非同期 API）。

    ワーカーは :func:`~snappix.viewer._runnable.run_detached` の**デーモン
    スレッド**で、``QThreadPool`` は使わない — プールのデストラクタは
    in-flight なタスクを無制限に待つので、切断中の NAS で押した直後に終了
    すると「窓は消えたのにプロセスが残る」に変わるだけだった。
    """
    from ..common.i18n import t

    signals = GuardedSignals()
    _shell_relays.add(signals)

    def _on_done(_token: int, ok: object) -> None:
        _shell_relays.discard(signals)
        if not ok:
            notify_failure(window, t("viewer.main_window.reveal_failed"))

    signals.done.connect(_on_done)
    run_detached(
        0, lambda p=path: _reveal_worker(p), signals,
        label="reveal-in-explorer",
    )
