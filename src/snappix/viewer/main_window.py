"""Viewer main window: 3-pane layout (folder grid / preview / file list).

Wires together :class:`PostGrid` (left), :class:`ContentView` (centre) and
:class:`FileListView` (right), plus a navigation history (root drill-down /
← back through history / ↑ to filesystem parent / ←→ for prev/next
sub-folder) and persistent state.

Extracted collaborators — the window remains the composition point:

- ``nav_history.py`` — :class:`NavEntry` + the long-press ←/→ history
  dropdowns (the back/forward stack semantics stay here, on the window);
- ``zip_drill.py`` — :class:`ZipDrillController`, the ZIP drill-in
  extraction pipeline (the window keeps the temp-dir map + close sweep);
- ``cache_build_controller.py`` — :class:`CacheBuildController`, the
  settings dialog's cache-management backend (modal + background builds).
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from loguru import logger
from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QFileSystemWatcher,
    QObject,
    QRect,
    QSize,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QImage,
    QKeyEvent,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QDialog,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ._runnable import GuardedStream, StreamJob
from .cache_build_controller import CacheBuildController
from .curation_list import CurationList
from .detail_window import DetailWindow
from .empty_state import (
    EmptyStateInput,
)
from .content_view import (
    ContentView,
    has_dedicated_view,
    set_pdf_preview_size_limit,
    set_preview_scroll_pixels,
    set_text_preview_max_bytes,
    set_wheel_nav_grace_ms,
    set_zip_preview_size_limit,
)
from .view_prefs import (
    notify_failure,
    open_with_default,
    set_image_fit_no_upscale,
    set_image_wheel_zoom,
)
from .file_list import FileListView
from .focus_target import (
    SEAT_LIGHTBOX,
    Target,
    curation_subject,
    focused_seat,
    resolve_target,
)
from .gallery_view import GalleryView
from .info_panel import _DETAIL_THUMB_EDGE, FileDetail, InfoPanel
from .qimage_decode import read_image_size
from .breadcrumb import path_segments
from .nav_rail import NavRail, bookmark_label
from .post_md import ParsedPost, read_post_meta_checked
from ._sqlite_cache import open_with_recovery
from .dialogs import pick_existing_directory, prompt_text
from .lightbox import LightboxWindow, MediaResume
from .locations import base_label, is_zip_temp_path, location_bases, location_label
from .nav_history import (
    HistoryMenus,
    NavEntry,
    SearchTransition,
    SetRootResult,
    install_mouse_nav,
)
from .search_index import SearchIndex
from .health_check import PART_SUFFIX
from .folder_scan import (
    IMAGE_SUFFIXES,
    PDF_SUFFIXES,
    POST_MD_NAME,
    THUMB_MARKER_PREFIX,
    VIDEO_SUFFIXES,
    ZIP_DRILL_SUFFIXES,
    is_meta_or_marker_name,
    next_representative,
)
from .representative_fallback import RepresentativeFallback

# Media the fullscreen lightbox (閲覧モード) can play back — images + videos.
_LIGHTBOX_MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
# Backoff (ms) before the single retry of a failed 情報パネル
# post.md meta read.  Long enough for a writer to finish / a share hiccup to
# pass, short enough that the card still appears "immediately" to the user.
_INFO_META_RETRY_DELAY_MS = 250
# Fallback width (px) for the right 情報パネル when it is re-shown from a hidden
# state and the splitter has no remembered size for it.
_INFO_PANEL_DEFAULT_WIDTH = 280
# Default / re-show width (px) for the left ナビレール.  A
# thin, fixed-ish column carved out of the centre pane when the rail is shown.
_NAV_RAIL_DEFAULT_WIDTH = 200
# Default [grid, preview] sizes for the centre [グリッド | プレビュー] split
# (split-view layout redesign 2026-07, candidate A).  QSplitter treats these
# as proportions when the pane is wider/narrower, so 550:450 = the spec's
# 55:45 default ratio.
_CENTER_SPLIT_DEFAULT_SIZES = [550, 450]
# 起動時の既定ウィンドウサイズ。``__init__`` の初期化と ``_restore_geometry``
# の画面外フォールバックが**同じ値**を使う必要があるので定数で共有する
# （別々に書くと、保存ジオメトリが画面外だったときだけ既定と違う大きさで
# 開く）。
_DEFAULT_WINDOW_SIZE = (1280, 800)
# ドラッグ追従が「再表示用の記憶幅 / 記憶比率」として採用する最小席幅 (px)。
# ``splitterMoved`` はドラッグ**中**の全サンプルで飛ぶため、席を 0 まで畳む
# ジェスチャは必ず [866, 8] のような極小サンプルを通過する。それを記憶して
# しまうとトグル再表示（F6 / F7 / F8）が数 px の帯を復元して no-op に見え、
# しかも ``_collect_state`` がその比率を永続化するので再起動しても直らない。
# この幅を下回る席は「畳んだ」と同一視して記憶しない。
_SPLIT_REMEMBER_MIN_PX = 80
# ドラッグ追従の記憶更新に課す下限は、極小サンプルの排除（上の
# _SPLIT_REMEMBER_MIN_PX）からさらに一段強く「両席が再表示既定幅以上」まで
# 引き上げる: 畳みジェスチャは 0 に着地する前に必ず
# 80〜既定幅の帯を通過するため、80px 下限だけでは記憶が既定幅未満まで
# 侵食され、トグル再表示が細い列を復元してしまう。外殻の 2 席は
# _NAV_RAIL_DEFAULT_WIDTH / _INFO_PANEL_DEFAULT_WIDTH をそのまま px 下限に
# 使う。中央分割の既定 _CENTER_SPLIT_DEFAULT_SIZES は「合計 1000 の比率」
# なので px では課せず、既定シェアを現在の合計幅へスケールした値の半分
# （= _CENTER_SPLIT_REMEMBER_MIN_SHARE 倍）を席ごとの下限にする — 55:45 の
# 半分なら 7:3 級の正当な設定比は引き続き記憶されるが、畳みへ向かう
# 終盤のサンプルは構造的に弾かれる。
_CENTER_SPLIT_REMEMBER_MIN_SHARE = 0.5
# ``closeEvent`` の永続化（``viewer_state.json`` の書き込み + 全 sqlite ストアの
# ``close`` = WAL チェックポイント）に与える**合計**予算 (秒)。
#
# なぜ 3.0 か:
#
# * **健全な保存先には十分すぎる**。state は数十 KB の JSON 1 本、WAL は直前の
#   ドレイン（``_cache_ctrl.shutdown`` / ``_drain_loader_pools``）で書き手が
#   止まった後の小さな残りなので、ローカルでも正常な LAN 共有でも実測は
#   ミリ秒〜数百ミリ秒に収まる。1 秒ではコールドな NAS で健全なチェック
#   ポイントまで諦めかねず、3 秒なら余裕がある。
# * **死んだ共有の I/O タイムアウトより桁で小さい**。到達不能な SMB では 1 回の
#   I/O が 15〜195 秒ブロックする（VM 実測）。予算がその桁に届いていては
#   「閉じない」という症状は消えない。同じ判断（NAS が死んでいるときに GUI を
#   ユーザーへ返す）を下している ``path_probe.probe_path_kind`` の 2.0 秒と
#   同オーダーに揃える。
# * **超過して諦めても壊れない**。キャッシュは再生成可能。``user_meta.db``
#   は書き込みごとに commit + ``synchronous=FULL`` なので、落ちるのは WAL の
#   チェックポイントだけでコミット済みデータは次回 open 時に回収される。
#   ``viewer_state.json`` は tmp + ``os.replace`` の原子的書き込みなので、
#   最悪でも「このセッションの UI 状態が直前の autosave 時点に戻る」。
_CLOSE_PERSIST_BUDGET_S = 3.0
# 稼働中（オートセーブ / デバウンス保存 / 設定ダイアログ）の state 保存で
# **GUI スレッドが完了を待つ**上限 (秒)。
#
# 保存そのものは常にワーカーで走る（``_persist_state_snapshot``）。ここで
# 決めるのは「呼び出し元が結果を知る必要があるか」だけ:
#
# * オートセーブ / デバウンス保存は結果を使わないので **0 秒 = 待たない**
#   （待つと 60 秒ごとに GUI が死んだ共有の I/O タイムアウトぶん固まる —
#   実測では何も操作しなくても 52 秒以上のフリーズになった）。
# * 設定ダイアログだけは契約（成否で「適用しました」と常駐警告を
#   出し分ける）があるので待つ。健全な保存先ではミリ秒で返るため 5 秒は
#   偽陰性を生まない一方、超過したときの「保存できていない」は**事実**
#   （保存先が 5 秒応答していない）なので、そのまま警告に出してよい。
_SETTINGS_PERSIST_WAIT_S = 5.0
from .path_probe import probe_path_kind
from .pending import MARK, OneShot, Pending, always
from .perf import measure, recorder
from .perf_dialog import PerfDialog
from .pool_teardown import drain_or_strand
from .post_grid import (
    PostGrid,
    SearchSnapshot,
    describe_search_payload,
    deserialize_search_snapshot,
    saved_search_tooltip,
    serialize_search_snapshot,
)
from .stage_view import PreviewColumn, StageFilmstrip, StageHeader
from .zip_drill import ZipDrillController
from .post_link_index import parse_post_url
from .settings_dialog import SettingsDialog
from ._menu_text import menu_label
from .shortcuts_dialog import TASK_START, ShortcutsDialog
from .state import (
    ViewerState,
    decode_geometry,
    encode_geometry,
    load_state,
    merge_bookmarks,
    merge_saved_searches,
    persist_bookmarks,
    persist_saved_searches,
    push_recent_root,
    save_state,
)
from .state_flush import BoundedFlusher
from .view_prefs import _format_bytes
from . import ai_pack
from .ai_pack import TAGS_DB_NAME
from .user_meta import UserMetaStore
from .theme import (
    THEME_CHOICES_EXTRA,
    THEME_CHOICES_MAIN,
    apply_theme,
    extra_theme_label,
)
from .thumb_disk_cache import sweep_orphan_blobs_async
from .thumbnail_loader import ThumbnailLoader
from .viewer_cache import ViewerCacheStore, discard_legacy_cache_dbs
from .window_help import WindowHelp
from .window_status import WindowStatus
from ..common.fsutil import pick_library_base, tail_display_labels
from ..common.i18n import t
from ..common.paths import get_paths
from ..common.shared_prefs import (
    add_library_root,
    apply_library_roots,
    load_shared_prefs,
    update_shared_prefs,
)
from ..common.teardown import Deadline, run_tasks_before_deadline
from ..common.ui import (
    PaneFocusBands,
    center_on_primary,
    frame_intersects_any_screen,
    show_toast,
)
from ..common.ui.timers import DebounceMode, Debouncer


class _MainThreadMonitor(QObject):
    """Log when the GUI thread stops servicing its event loop.

    A repeating ``QTimer`` schedules a tick every ``interval_ms``.  When
    a tick fires later than expected — because Qt was busy running a
    slow synchronous call between iterations of the event loop — the
    difference is the duration the GUI was frozen.  Anything over
    ``threshold_ms`` is logged at WARNING level so a developer reading
    ``viewer.log`` can correlate a freeze with the immediately preceding
    log lines (e.g. a ``setHtml`` stage timer).

    Kept lightweight: just a ``monotonic`` comparison per tick, so
    running it permanently is fine.

    **出力はレート制限する**。1 回のフリーズが 1 行では
    済まないのが実測: 復帰直後に ``QTimer`` のキャッチアップ tick が連続で
    走るため、1 回の停止が「``~2187ms`` から ``~750ms`` まで 13 行」に化ける。
    死んだ共有では停止が繰り返し起きるので、この監視だけで毎分数十行を
    吐き続ける — 診断のためのログが、診断したい障害の最中に**ログを埋める
    側**へ回る。``_MIN_WARN_INTERVAL_S`` に 1 行へ畳み、その間の最大停止と
    抑制件数を同じ行に載せる（情報は落とさない）。

    これは**燃料を減らすだけの緩和策**であって、有界化そのものではない
    （1 行でも保存先が詰まれば同じ）。塞がらないことの保証は
    ``common/logging.py`` の ``_BoundedSink`` 側の責務。
    """

    #: 連続する freeze 警告を畳む最小間隔 (秒)。
    _MIN_WARN_INTERVAL_S = 5.0

    def __init__(
        self, interval_ms: int = 200, threshold_ms: int = 500,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._interval_sec = interval_ms / 1000.0
        self._threshold_sec = threshold_ms / 1000.0
        self._last = time.monotonic()
        self._last_warned_at = 0.0
        self._pending = 0
        self._worst_sec = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        now = time.monotonic()
        lag_sec = (now - self._last) - self._interval_sec
        self._last = now
        if lag_sec >= self._threshold_sec:
            self._pending += 1
            self._worst_sec = max(self._worst_sec, lag_sec + self._interval_sec)
        if not self._pending:
            return
        if now - self._last_warned_at < self._MIN_WARN_INTERVAL_S:
            # 畳んでいる最中。**捨てているのではない** — tick は停止していない
            # ときも回り続けるので、窓が明けた最初の tick が下でまとめて出す。
            return
        pending, self._pending = self._pending, 0
        worst, self._worst_sec = self._worst_sec, 0.0
        self._last_warned_at = now
        logger.warning(
            "[diag] GUI thread was blocked for ~{:.0f}ms "
            "(tick interval {:.0f}ms{})",
            worst * 1000.0,
            self._interval_sec * 1000.0,
            f"; worst of {pending} stalls in the last "
            f"{self._MIN_WARN_INTERVAL_S:g}s" if pending > 1 else "",
        )


# ステータスバー「選択ファイル名 · サイズ」permanent セグメントの上限幅 (px)。
# sanitize.py が明示サポートする 250/255 バイト境界のファイル名でラベルが
# 1000px 超に膨張し、stretch=1 の現在パス表示を圧殺する
# — 超過分は中央省略しフルテキストはツールチップで担保する。
_FILE_INFO_LABEL_MAX_PX = 360


def _stat_file_label(path: Path) -> str:
    """Return the status-bar "name · size" label for *path* (off-thread body).

    A single ``os.stat`` is usually milliseconds, but on a cold NAS folder (or
    while a background cache build is competing for SMB credits) it can stall
    for hundreds of ms — and it runs on EVERY selection change, so it must not
    block the GUI (dispatched on ``ViewerWindow._file_info_stream``).  Directories / missing
    paths return ``""`` so the caller clears the segment.
    """
    import stat as _stat
    try:
        st = path.stat()
    except OSError:
        return ""
    if _stat.S_ISDIR(st.st_mode):
        return ""
    return f"{path.name} · {_format_bytes(st.st_size)}"


def _tags_db_signature(path: Path) -> tuple[float, int] | None:
    """``(mtime, size)`` of ``tags.db``, or ``None`` when it is absent.

    Module-level (not a method) because it is the body dispatched off the GUI
    thread by ``ViewerWindow._probe_tags_db`` — a worker closure
    over a plain ``Path`` can't reach into the window's state by accident.  On
    a half-dead SMB share holding ``data/`` this ``stat`` blocks for the whole
    protocol timeout, which is exactly why it must not run on the GUI thread
    (no blocking disk I/O on the GUI thread).
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _read_file_detail(path: Path) -> dict:
    """Read a selected file's detail fields off the GUI thread (2026-07-20).

    Feeds the 情報パネル's file-detail card: 種別 / サイズ / 更新日時 / 解像度.
    Everything here touches the disk (``stat`` + a header-only image decode for
    the resolution), and it runs on EVERY file selection, so — like
    :func:`_stat_file_label` / :func:`detail_window._read_fs_info` — it is
    dispatched on ``ViewerWindow._file_detail_stream`` and never blocks selection changes on a
    cold NAS.  Returns display-ready strings; ``resolution_text`` is ``""`` for
    non-images (the card omits that row).
    """
    import stat as _stat

    suffix = path.suffix.lower().lstrip(".")
    kind = suffix.upper() if suffix else t("common.label.file")
    size_text = "—"
    mtime_text = "—"
    try:
        st = path.stat()
    except OSError:
        return {
            "kind": kind, "size_text": size_text,
            "mtime_text": mtime_text, "resolution_text": "",
        }
    if not _stat.S_ISDIR(st.st_mode):
        size_text = _format_bytes(st.st_size)
    try:
        mtime_text = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        mtime_text = "—"
    resolution_text = ""
    if path.suffix.lower() in IMAGE_SUFFIXES:
        wh = read_image_size(path)
        if wh is not None:
            resolution_text = f"{wh[0]} × {wh[1]}"
    return {
        "kind": kind, "size_text": size_text,
        "mtime_text": mtime_text, "resolution_text": resolution_text,
    }


def _subtree_overlaps(a: Path, b: Path) -> bool:
    """一方が他方のサブツリー（同一含む）に含まれるかの文字列判定。

    :meth:`ViewerWindow.notify_library_changed` の「表示中ルートは影響範囲か」
    判定用。Windows の大文字小文字ゆれ（外部ツール由来のパスは casing が
    揃わない — folder_preview_cache の COLLATE NOCASE と同じ理由）に耐える
    よう ``os.path.normcase`` で正規化する。FS には触れない。
    """
    sa = os.path.normcase(str(a)).rstrip("\\/")
    sb = os.path.normcase(str(b)).rstrip("\\/")
    if not sa or not sb:
        return False
    return sa == sb or sa.startswith(sb + os.sep) or sb.startswith(sa + os.sep)


class ViewerWindow(QMainWindow):
    def __init__(
        self,
        root: Path,
        state: ViewerState,
        initial_select: Path | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(t("viewer.main_window.window_title"))
        self.resize(*_DEFAULT_WINDOW_SIZE)
        # Accept folder / file drops onto the window to change the root.
        self.setAcceptDrops(True)

        self._state = state
        self._root: Path = root
        self._history: list[NavEntry] = []  # back stack (visited positions)
        self._forward: list[NavEntry] = []  # forward stack (undone by 戻る)
        # Bookmarks THIS instance deliberately removed — closeEvent's merge
        # re-adopts disk-side additions from a concurrent instance EXCEPT
        # these, so a stale window closing last can't resurrect them.
        self._session_removed_bookmarks: set[str] = set()
        # 表示名を**明示的に空にした**ブックマーク。名前も「不在 = 削除」で
        # 表すと、このインスタンスが持っているだけのパスの名前まで自分が
        # 権威になり、開いている間に別インスタンスが付けた名前を巻き添えで
        # 消す（パス側で塞いだのと同じ事故が名前側で起きる）。
        self._session_cleared_bookmark_names: set[str] = set()
        # 保存済み検索の同型セット: このインスタンスが管理
        # ダイアログで削除・改名した旧 name。マージがディスク側の同名
        # エントリを再採用しないための除外リスト。
        self._session_removed_saved_searches: set[str] = set()
        self._current_folder: Path | None = None
        # プラグイン基盤（plugin_host/）。app.py の bootstrap_plugins が
        # window 構築後に attach する。セーフモード（--no-plugins）や
        # bootstrap 前は None のまま — 管理ダイアログは host 無しでも動く。
        self._plugin_host = None
        # bootstrap が配線する PluginEvents（安定 API のイベントバス）。
        # set_root 成功時の root_changed emit だけが本体側のフック。
        self._plugin_events = None
        # ヘルプ + 診断のモードレス子窓 4 本（操作ガイド / 計測統計 / 健全性
        # チェック / AI セットアップ手引き）は部品 ``window_help.WindowHelp``
        # が所有する（``self._help`` — 下の遅延プロパティ）。
        # Modeless "詳細情報" window (opened from the 表示 menu).  Tracks the
        # last selected/previewed item via ``_current_preview_path`` so it can
        # be populated immediately on open and kept in sync on each selection.
        self._detail_window: DetailWindow | None = None
        self._current_preview_path: Path | None = None
        # 閲覧モード (fullscreen lightbox).  Built lazily on first use so the
        # startup path (and the shortcut-sync test's window construction)
        # never pays for it; owns a dedicated small ThumbnailLoader for the
        # filmstrip (created alongside).
        self._lightbox: LightboxWindow | None = None
        self._lightbox_loader: ThumbnailLoader | None = None
        # ステージモードのフィルムストリップ専用サムネイルローダー。
        # ライトボックスの帯と同じく遅延生成・小プール（2 スレッド）・永続
        # キャッシュ共有 — 左ペインの常駐 pixmap で賄えないタイルだけ解決する。
        self._strip_loader: ThumbnailLoader | None = None
        # Temp directories created by extracting ZIP archives, mapped to
        # the original ZIP path so the window title can show the friendly
        # name instead of a cryptic ``tmpXXXXXX`` path.  Owned here (not by
        # the ZipDrillController) because ``set_root`` / the history menus
        # need it for friendly labels.  Cleaned up on ``closeEvent`` —
        # mid-session orphans are tolerated because the 50 MB cap keeps
        # total temp usage bounded.
        self._zip_temp_dirs: dict[Path, Path] = {}
        # First-image centre-preview probe for folders without post.md (see
        # ``_on_folder_selected``).  専用 1 スレッド + 世代 + 協調キャンセルを
        # 1 つにまとめた :class:`GuardedStream`: 代表画像の BFS は最大 ~33 回の
        # scandir で、半死の共有では返らない。グローバル ``QThreadPool`` に
        # 乗せると同居する短命プローブ（``_stat_file_label`` / 情報パネルの
        # ファイル詳細 / ``dir_exists_probe``）の枠を長時間奪う。窓の
        # ``_drain_loader_pools`` は ``findChildren(GuardedStream)`` で母集団を
        # 掃くので、ここに置くだけで予算内ドレイン + 窓じまいの cancel に乗る。
        self._preview_stream = GuardedStream(self)
        self._preview_stream.bind(self._on_first_image_found)
        self._preview_probe_folder: Path | None = None
        self._preview_probe_hint: Path | None = None
        # 代表画像フォールバック: 「ウィンドウが選んだ」代表画像と、デコードに
        # 失敗して次候補へ譲った候補（フォルダプレビューと同じ帳簿）。
        self._preview_fallback = RepresentativeFallback()
        # 代表画像のフォールバック探索が in-flight か。
        # True の間だけ解像度ラベルの「画像なし」クリアを据え置く。
        self._preview_fallback_pending = False

        # Persistent caches (portable, under data/).  The aspect cache
        # feeds the justified layout (tiny, high-value); the disk cache
        # persists decoded thumbnails for instant revisits.  Both validate
        # against the filesystem (mtime/size) — the FS is always the source
        # of truth.  Independent byte budgets; thumbnail eviction never
        # touches the aspect cache.
        #
        # それら 3 つは ``viewer_cache.db`` 1 ファイルに同居する
        # （:class:`~.viewer_cache.ViewerCacheStore` — 接続 / WAL / 破損復旧 /
        # close が 1 系統）。窓は従来どおり個別のキャッシュ参照を配線に配るが、
        # **閉じるのはストア 1 本**（closeEvent の列挙を参照）。
        self._cache_store, self._search_index = self._build_caches(state)
        store = self._cache_store
        self._meta_cache = store.aspect if store is not None else None
        self._disk_cache = store.thumbs if store is not None else None
        self._folder_cache = store.previews if store is not None else None
        # Read-only reader for the tagger's tags.db (advanced tag search).
        # Separate from the managed caches above: it's an external,
        # read-only index with no byte budget / pruning, so it isn't part of
        # the _build_caches tuple.  ``None`` when no tags.db is present →
        # the advanced-search panel hides its tag controls.
        self._tag_index = self._open_tag_index()
        # Read-only reader for the per-image embedding vectors (image_vectors +
        # tag-vector matrix) powering semantic / similar-image search. ``None``
        # when no vectors are present → the panel's semantic controls disable.
        self._vector_index = self._open_vector_index()
        # User curation layer (stars / user tags / watch-later).  The viewer's
        # own writable store under data/ (unlike tags.db which is read-only);
        # ``None`` when the volume is read-only → curation UI is hidden.
        # ``_user_meta_error`` carries the *reason* so the disappearance isn't
        # silent — surfaced once on first show (``showEvent``).
        self._user_meta, self._user_meta_error = self._open_user_meta()
        self._user_meta_notice_shown = False
        # 設定 / ブックマーク / 保存した検索の書き込み失敗を告げる常駐警告
        # トーストの one-shot ガード（``_notify_persist_failed``）。
        self._persist_notice_shown = False

        self._loader = ThumbnailLoader(
            cache_size=state.thumbnail_cache_size,
            max_threads=state.thumbnail_max_threads,
            parent=self,
            disk_cache=self._disk_cache,
            cache_edge=state.thumb_disk_cache_max_edge,
            folder_cache=self._folder_cache,
        )
        self._file_thumb_loader = ThumbnailLoader(
            cache_size=state.thumbnail_cache_size,
            max_threads=state.thumbnail_max_threads,
            parent=self,
            disk_cache=self._disk_cache,
            cache_edge=state.thumb_disk_cache_max_edge,
            folder_cache=self._folder_cache,
        )
        # フォルダプレビューの子タイル用ローダー: lightbox /
        # フィルムストリップの専用ローダーと同型（2 ワーカー + 永続ディスク
        # / フォルダキャッシュ共有・キーは "folderpreview:" 名前空間）。
        # FolderPreviewView の自前 globalInstance デコードを置き換え、
        # ディスクキャッシュ再利用・保留キューのキャンセル・closeEvent の
        # ドレイン（_drain_loader_pools）に乗せる。注入は _build_ui 後の
        # set_folder_thumbnail_loader。
        self._folder_preview_loader = ThumbnailLoader(
            cache_size=128,
            max_threads=2,
            parent=self,
            disk_cache=self._disk_cache,
            cache_edge=state.thumb_disk_cache_max_edge,
            folder_cache=self._folder_cache,
        )

        self._build_ui()
        # ZIP drill-in extraction pipeline (zip_drill.py).  The window
        # keeps the temp-dir map (titles / history labels / close sweep); the
        # controller registers extractions into it and calls back into
        # ``set_root`` on success.
        self._zip_drill = ZipDrillController(
            self,
            self._zip_temp_dirs,
            show_zip_preview=self._show_zip_preview_fallback,
            open_extracted_root=self._open_extracted_zip_root,
            status_message=self._notify_extracted,
            parent=self,
        )
        # Cache-build backend for the settings dialog
        # (cache_build_controller.py).  Constructed after _build_ui so it can
        # wire the status bar's CacheBuildStatusWidget signals.
        self._cache_ctrl = CacheBuildController(
            state=self._state,
            disk_cache=self._disk_cache,
            meta_cache=self._meta_cache,
            folder_cache=self._folder_cache,
            search_index=self._search_index,
            tag_index=self._tag_index,
            # 呼び出し可能な provider を渡す:
            # 固定タプルだと遅延生成の ``_strip_loader`` /
            # ``_lightbox_loader`` と ``_folder_preview_loader`` が
            # 「サムネイルキャッシュを削除」の記憶 LRU フラッシュから漏れる。
            loaders=self._all_loaders,
            status_widget=self._cache_build_status,
            status_message=self._show_status_message,
            current_root=lambda: self._root,
            notify=self._show_toast,
            parent=self,
        )
        # In-memory copy of the shared library-root list (M02), seeded once from
        # shared_prefs.json.  data/ may live on a NAS, so this startup read is
        # the only synchronous one (app.py already read the same file for the
        # language / theme before the window existed); later reads go through
        # the bounded ``_shared_prefs_flush`` worker.  Kept in sync as
        # the user registers / manages roots so ``_rebuild_library_menu`` stays
        # NAS-free (it reads this list, never the filesystem).
        self._library_roots: list[str] = list(self._load_library_roots())
        # Default browse root (paths.library) — shown as the Japanese
        # 「ライブラリ」 label in the breadcrumb / status bar instead of the raw
        # English folder name.  Resolved once (cached paths).
        self._default_library: Path = get_paths().library
        # Seed the breadcrumb's library-relative rendering;
        # the pane was built above, so this reaches its breadcrumb before the
        # first ``set_root`` scan lands its trail.
        self._refresh_library_bases()
        self._build_menus()
        self._install_shortcuts()
        # K01: watch data/tags.db for the tagger writing / replacing it, so a
        # scan started after the viewer opened is picked up without a restart.
        self._init_tags_watcher()
        # AI エンジン provider はプラグインの activate（ウィンドウ構築後）で
        # 登録される — 登録された瞬間にインデックスを開き直して AI UI を
        # 点灯させる（既存の tags.db 再読込経路を再利用）。closeEvent で外す。
        ai_pack.add_provider_callback(self._on_ai_provider_changed)
        apply_theme(self._state.theme)
        set_preview_scroll_pixels(self._state.preview_scroll_pixels)
        set_zip_preview_size_limit(
            self._state.zip_preview_size_limit_mib * 1024 * 1024
        )
        set_pdf_preview_size_limit(
            self._state.pdf_preview_size_limit_mib * 1024 * 1024
        )
        set_text_preview_max_bytes(
            self._state.text_preview_max_mib * 1024 * 1024
        )
        set_wheel_nav_grace_ms(self._state.wheel_nav_grace_ms)
        set_image_wheel_zoom(self._state.image_wheel_zoom)
        set_image_fit_no_upscale(self._state.image_fit_no_upscale)
        self._restore_geometry()
        # Freeze monitor: logs a warning to viewer.log whenever the GUI
        # event loop stalls for ≥500ms.  Lets us pinpoint which user
        # action (scroll, folder switch, etc.) corresponds to a freeze
        # and how long it lasted.
        self._main_thread_monitor = _MainThreadMonitor(parent=self)
        # ``closeEvent`` が最後まで走ったことの印。閉じた窓は自走タイマーを
        # 1 つも持たない（下の closeEvent が全部止める）ので、テストハーネス
        # はこれを見て「もう破棄してよい窓」を判別する（tests/conftest.py の
        # 刈り取り — close は隠すだけで破棄しないため、閉じ忘れではなく
        # **閉じたが捨てられない**窓がワーカー 1 つに数十個溜まっていた）。
        self._teardown_done = False
        # Rename-following worker bridge (user_meta): builds a postref→folder
        # resolver off-thread and repoints curation rows whose folder was
        # renamed by a writer-side naming-pattern change (see _kick_rename_follow).
        # 直近に歩いた基準（``_rename_follow_base``）。この配下への移動では
        # 蒔き直さない（全 set_root で歩くと、降下 / ↑ / 戻る・進むのたびに
        # 最大 20,000 フォルダの走査が背景で走る）。
        self._rename_follow_root: Path | None = None
        # 専用 1 スレッド + 世代 + 協調キャンセル:
        # ``build_moved_resolver`` は最大 20,000 フォルダのディレクトリ列挙で、
        # グローバル ``QThreadPool`` に乗せると同居する短命タスクの枠を長時間
        # 奪う。世代だけでは着地した結果を捨てるだけでウォークは止まらないので、
        # 世代と ``CancelToken`` を同時に動かす :class:`GuardedStream` へ寄せる
        # （ルート切替は ``submit_job`` の追い越し、closeEvent は
        # ``_drain_loader_pools`` の ``request_shutdown`` が降ろす）。
        self._rename_follow_stream = GuardedStream(self)
        self._rename_follow_stream.bind(self._on_rename_follow_done)
        # 降りたウォークも「既に DB へ書いた張り替え」を残して降りる
        # （``resolve_moved_entries`` の契約）ので、着地が捨てられる経路
        # （cancel / closeEvent）でも母集合を読み直す口を持たせる。
        self._rename_follow_stream.bind_cancelled(self._on_rename_follow_cancelled)
        #: ワーカーが「store へ書いた」と置いていく one-shot（GUI 側で 0 化）。
        self._rename_follow_pending = False
        # Startup resume (B01/B02): re-select the last selected tile, restore
        # the previewed file once its folder populates, and re-apply the left
        # pane's scroll offset.  One-shot state, gated by the setting.
        self._last_previewed_file: Path | None = None
        self._pending_restore_preview: Pending[Path] = Pending()
        # 「内容が変わったかもしれない」明示リロード
        # （F5 / ``notify_library_changed``）の one-shot。次に着地する
        # ``folder_selected`` が同一フォルダの pending-select 再発火でも、
        # 純 UI リビルド向けの右ペイン再スキャン抑止を 1 回だけ解除して
        # 従来どおり再スキャンさせる（鮮度は refire 再スキャン依存）。
        self._pending_content_rescan: OneShot = Pending()
        # File-detail card (製品オーナー 2026-07-20 の役割再定義): when a concrete
        # file is selected, the right panel shows ITS detail (thumbnail / 種別 /
        # サイズ / 更新日時 / 解像度 / スター / path) instead of mirroring the
        # folder listing.  The stat/resolution read is off-thread + token-guarded
        # like the info-meta read; this slot holds the card currently being
        # filled (fields land async, thumbnail may land later still).  宣言が
        # ここに在るのは、カードを読む側（``_sync_curation_strips``）が窓の
        # 組み立て途中にも走るため——スロットだけ先に作っておく。
        self._pending_file_detail: Pending[FileDetail] = Pending()
        startup_select: Path | None = None
        if state.restore_selection_on_startup:
            if state.last_selected_path:
                sel = Path(state.last_selected_path)
                # Tiles are direct children of the root — anything else can't
                # resolve (pure path comparison; no filesystem I/O here).
                if sel.parent == root:
                    startup_select = sel
            if state.last_previewed_path:
                self._pending_restore_preview.set(
                    Path(state.last_previewed_path)
                )
        # B01/B02: latch True while a startup resume (selection / previewed
        # file / scroll) is queued but hasn't landed yet.  On a cold NAS the
        # 5s debounced autosave can fire before the scan populates the grid;
        # without this guard _collect_state would read the still-empty live
        # UI (current_path()==None, no previewed file, scroll 0) and clobber
        # the very resume fields it is about to restore.  Cleared the moment
        # the first selection lands (see _on_folder_selected).
        self._startup_restore_pending = bool(
            state.restore_selection_on_startup
            and (
                state.last_selected_path
                or state.last_previewed_path
                or state.last_grid_scroll > 0
            )
        )
        # 最初のスキャン着地で「選択が無ければ先頭
        # タイルを選択 + グリッドへフォーカス」を一度だけ行う one-shot。
        # 初回起動（復元なし）はグリッド無選択のままプレビュー列 + 情報パネルが
        # プレースホルダだけになり、しかもキーボードフォーカスはツールバーの
        # 「↑」に居た（Space 一発でライブラリ外へ）。復元起動では選択が既に
        # 入っているのでフォーカス移動だけ行う。
        self._startup_focus_pending = True
        # An explicit launch-time file selection (the exe was handed a file
        # path, so its parent became the root) takes precedence over the
        # session-restore pick — tiles are direct children of the root, so
        # only a file sitting directly under it can resolve.
        if initial_select is not None and initial_select.parent == root:
            startup_select = initial_select
        # ``assume_exists=True``: app.py already probed the root with a
        # timeout (B05) — never re-stat an offline share on the GUI thread.
        self.set_root(root, pending_select=startup_select, assume_exists=True)
        if state.restore_selection_on_startup and state.last_grid_scroll > 0:
            self._post_grid.set_pending_scroll(state.last_grid_scroll)
        # Crash-safe persistence (B03): a periodic autosave plus a debounced
        # save after top-level root changes, so a crash / kill / OS shutdown
        # loses at most the last interval instead of the whole session.
        #
        # 保存はワーカーで走るので、前回が返る前に次の tick が来る
        # 「死んだ共有」でワーカーが積み上がらないようにする在庫フラグ。
        #
        # **「暇なら set」の向き**で持つ（再指摘 M-1）: 待ちたい呼び出し元
        # （設定ダイアログ）が ``Event.wait`` で前回の完了を予算内で待てる。
        # 「忙しいなら set」だと解除を待つ標準の口が無く、ポーリングか即諦め
        # （＝健全なディスクでも「保存できませんでした」の偽陰性）になる。
        self._state_save_idle = threading.Event()
        self._state_save_idle.set()
        # 即時フラッシュ（ブックマーク / 保存済み検索 / shared_prefs.json）の
        # 有界待ちワーカー。フル保存が
        # ``_persist_state_snapshot`` で持っている「GUI スレッドで値を確定 →
        # 予算付きワーカーで突き合わせ + 書き込み」の 2 段を、即時フラッシュ
        # 側にも同じ形で当てる。保存先ごとに 1 本（同じファイルへのフラッシュ
        # を積み増さない門を各インスタンスが持つ — ``state_flush`` 参照）。
        self._bookmark_flush = BoundedFlusher(
            "bookmarks", thread_name="bookmark-flush",
        )
        self._saved_search_flush = BoundedFlusher(
            "saved searches", thread_name="saved-search-flush",
        )
        # ``shared_prefs.json`` の 3 経路（テーマ / ライブラリ登録 / ライブラリ
        # 管理）はどれも「最新のディスク内容へ差分を当てる」冪等な書き込みな
        # ので**合流**させる。門に弾かれた書き込みを捨てると、差分適用の常で
        # 恒久に消える（``viewer_state.json`` 側に対応フィールドが無く救済も
        # されない）。合流ならスレッドを増やさずに飛行中のワーカーが続けて
        # 実行し、``closeEvent`` の ``wait_idle`` が追走ぶんまで待つ。
        self._shared_prefs_flush = BoundedFlusher(
            "shared_prefs.json", thread_name="shared-prefs-flush",
            coalesce=True,
        )
        # 書き込みの順序保証（再指摘 M-2）。詰まったオートセーブと終了時保存が
        # 同時に飛ぶと、``save_state`` は**全フィールドの完全置換**なので
        # ``os.replace`` の着地順で古い方が勝ち得る。守るのは**世代番号**で、
        # 追い越された書き込みを ``os.replace`` の直前で捨てる（R4）。この
        # ロックが囲うのは ``_claim_state_generation`` の read-modify-write
        # だけ — I/O を囲うと、詰まったオートセーブが終了時保存の予算を
        # ロック待ちで溶かす（``_save_merged_state`` の docstring）。
        #
        # ファイル操作そのものの直列化は state.py 側の ``_STATE_FILE_LOCK``
        # （読み取り + ``os.replace`` の 2 つの短い操作だけ）が
        # 担う。**ロック階層は ``_STATE_FILE_LOCK`` → この ``_state_write_lock``
        # の一方向**（着地直前の世代確認がこの順で入れ子になる）。逆向きに
        # 取る経路を作らないこと。
        self._state_write_lock = threading.Lock()
        self._state_snapshot_seq = 0
        self._state_written_gen = 0
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60_000)
        self._autosave_timer.timeout.connect(self._persist_state_snapshot)
        self._autosave_timer.start()
        self._state_save_debounce = Debouncer(
            self, 5_000, self._persist_state_snapshot, mode=DebounceMode.TRAILING
        )
        # Seed the MRU with the launch root (once the root actually resolved).
        if self._root is not None:
            self._record_recent_root(self._root)
        # After the launch root is known, run a lightweight rename-following
        # pass so stars survive a folder rename (off-thread; no-op when the
        # store has no postref'd curation yet).
        self._kick_rename_follow()

    # ----------------------------------------------------------- caches

    def _build_caches(self, state: ViewerState):
        """Open the merged disk-cache store + the search index.

        Best-effort: a sqlite open failure (locked file, read-only volume)
        degrades that store to ``None`` so the viewer still runs (layout falls
        back to live probing + thumbnail-derived aspects; thumbnails decode
        without persistence; folders re-scan live; search runs fully live).
        Returns ``(cache_store, search_index)``; the individual caches are
        read off ``cache_store.aspect`` / ``.thumbs`` / ``.previews``.

        アスペクト比 / サムネイル索引 / フォルダプレビューは 1 ファイル
        （``viewer_cache.db`` = :class:`~.viewer_cache.ViewerCacheStore`）に
        同居する。設定でオフのキャッシュは表だけが空のまま残り、ハンドルは
        配られない。検索索引は列挙する索引で役割が違うため別ファイルのまま。

        統合前の 3 DB が残っていれば**読まずに削除**する（再生成可能・移行の
        詳細は :func:`~.viewer_cache.discard_legacy_cache_dbs`）。索引を失った
        WebP blob は全件孤児になるので、破損退避と同じ掃引シームへ載せる。
        """
        paths = get_paths()
        data = paths.data
        cache_db = paths.viewer_cache_db
        blob_dir = paths.thumb_blob_dir
        search_db = data / "viewer_search_index.db"
        # 退避 / 旧 DB 破棄のどちらでも「新しい索引は空 = 全 blob が孤児」に
        # なる。掃除そのものは新しい索引が開いた**後**に、その索引を見ながら
        # 行う（無条件に掃くと、直後に走り出した store() の新しい blob まで
        # 消して行だけが残る）。
        blobs_orphaned = {"hit": bool(discard_legacy_cache_dbs(data))}
        cache_store = self._open_cache(
            "ディスクキャッシュ",
            cache_db,
            lambda: ViewerCacheStore(
                cache_db,
                blob_dir=blob_dir,
                thumb_max_bytes=state.thumb_disk_cache_max_mib * 1024 * 1024,
                preview_max_bytes=(
                    state.folder_preview_cache_max_mib * 1024 * 1024
                ),
                thumbs_enabled=state.thumb_disk_cache_enabled,
                previews_enabled=state.folder_preview_cache_enabled,
            ),
            lambda s: s.prune(
                aspect_max_bytes=state.aspect_cache_max_mib * 1024 * 1024
            ),
            ViewerCacheStore.SCHEMA_TABLES,
            lambda: blobs_orphaned.__setitem__("hit", True),
        )
        search_index = (
            self._open_cache(
                # 用語（検索索引）を設定ダイアログと揃えるため、
                # 種別名の単一ソースである stat_*_name キーを共有する。
                t("viewer.settings_dialog.stat_search_index_name"),
                search_db,
                lambda: SearchIndex(
                    search_db,
                    max_bytes=state.search_index_max_mib * 1024 * 1024,
                    use_fts=state.search_index_use_fts,
                ),
                lambda c: c.prune(),
                SearchIndex.SCHEMA_TABLES,
                None,
            )
            if state.search_index_enabled else None
        )
        if blobs_orphaned["hit"]:
            thumbs = cache_store.thumbs if cache_store is not None else None
            if thumbs is not None:
                # 行を再参照した blob は残す（clear() と同じガード）。
                thumbs.sweep_orphans_async()
            else:
                # サムネ索引が無い = この起動で store() は走らないので、
                # 無条件の掃引で構わない。
                sweep_orphan_blobs_async(blob_dir)
        return cache_store, search_index

    @staticmethod
    def _open_cache(
        label, db_path, factory, prune, schema_tables=(), on_quarantined=None,
    ):
        """Open one cache + prune it, degrading to ``None`` on any failure.

        Shared best-effort policy for :meth:`_build_caches`: a sqlite open or
        prune failure (locked file, read-only volume) logs a warning naming the
        cache and returns ``None`` so the viewer keeps running without it.

        破損 DB（``sqlite3.DatabaseError``）に限り :func:`open_with_recovery`
        が ``db_path`` を ``.corrupt`` へ 1 回だけ退避して再作成を試みる。これに
        より、破損 DB が ``data/`` に残置されても毎起動 ``None`` 劣化して性能劣化
        が恒久化する事態を避ける。

        起動時 ``prune``（全表走査）は ``verify`` として ``open_with_recovery`` の
        管轄に入れる — page2 以降だけ破損した DB や異種 sqlite ファイルは接続直後
        の PRAGMA / 移行を素通りし ``prune`` で初めて ``DatabaseError`` になるため、
        prune も退避+再作成の対象にしないと退避が永久に走らない。
        ``schema_tables``（各ストアの ``SCHEMA_TABLES``）は「異種 sqlite
        ファイルでストア自身のテーブルが無い」ケース（実 sqlite では
        ``OperationalError: no such table: <name>``）を破損と分類するために渡す。
        ``verify`` 失敗時は ``open_with_recovery`` がハンドルを閉じてから
        送出するので、``None`` 劣化してもファイルハンドルはリークしない。
        """
        try:
            return open_with_recovery(
                factory, db_path, verify=prune, schema_tables=schema_tables,
                on_quarantined=on_quarantined,
            )
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("{} を利用できません: {}", label, exc)
            return None

    def _open_tag_index(self):
        """Open the tagger's ``tags.db`` read-only, or ``None`` if absent.

        Routed through :mod:`ai_pack` — the AI feature pack (paid plugin)
        gates both index opening and every AI UI surface.  Best-effort like
        the caches: any failure degrades to ``None`` so the viewer runs
        without tag search.
        """
        return ai_pack.open_tag_index(get_paths().data)

    def _open_vector_index(self):
        """Open the semantic-search vectors read-only, or ``None`` if absent.

        Routed through :mod:`ai_pack` — the lazy import inside keeps numpy
        out of the plain (AI-less) build entirely.
        """
        return ai_pack.open_vector_index(get_paths().data)

    # ------------------------------------------------- tags.db live reload (K01)

    def _on_ai_provider_changed(self) -> None:
        """AI エンジン provider の登録/解除に追随する（ai_pack コールバック）。

        ウィンドウは provider 未登録（インデックス None）で構築される —
        プラグインの ``activate`` が provider を登録した瞬間にここが呼ばれ、
        既存の tags.db 再読込経路でインデックスを開き直して AI UI を点灯
        させる。deactivate（登録解除）でも呼ばれ、その場合は
        ``open_tag_index`` が ``None`` を返すので tags.db 消失と同じ劣化に
        落ちる。
        """
        self._reload_tag_indexes(announce=False)

    def _init_tags_watcher(self) -> None:
        """Watch ``data/tags.db`` (and its dir) for tagger writes (item K01).

        A :class:`QFileSystemWatcher` fires on every write; the tagger streams
        many during a scan, so a debounce timer coalesces the flurry and only
        reloads once the file has been quiescent for a few seconds (no reload
        mid-write).  The directory is watched too so the *appearance* of a
        brand-new tags.db (first-ever scan) is caught, not just edits.

        No disk touch happens on the GUI thread past this constructor:
        the watcher slot only rearms the debounce, and the existence /
        ``(mtime, size)`` probe runs off-thread through
        :meth:`_probe_tags_db`.  The one ``stat`` here is unavoidable —
        ``QFileSystemWatcher.addPath`` stats internally anyway — and it runs
        once, during window construction, next to the (far heavier)
        synchronous ``tags.db`` open.
        """
        self._tags_db_path = get_paths().data / TAGS_DB_NAME
        # AI パック無効（素の配布）では watcher を張らない — tags.db が現れても
        # ai_pack ゲートで index は None のままで、reload 通知（AIタグDB…）が
        # AI 文言を漏らすだけになる。タイマー等の属性も
        # 作らない（参照側はこのメソッドが接続したシグナル経由のみ）。
        if not ai_pack.available():
            self._tags_db_sig = None
            return
        self._tags_watcher = QFileSystemWatcher(self)
        data_dir = str(get_paths().data)
        # One ``stat`` serves both the watcher arming and the initial baseline
        # (it used to be an ``exists()`` plus a ``stat()``).
        self._tags_db_sig: tuple[float, int] | None = _tags_db_signature(
            self._tags_db_path
        )
        try:
            self._tags_watcher.addPath(data_dir)
            if self._tags_db_sig is not None:
                self._tags_watcher.addPath(str(self._tags_db_path))
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("tags.db watcher setup failed: {}", exc)
        self._tags_watcher.fileChanged.connect(self._on_tags_db_changed)
        self._tags_watcher.directoryChanged.connect(self._on_tags_db_changed)
        # closeEvent が 1 度だけ外すための目印。2 度目の close で
        # 外しにいくと PySide が SystemError を投げる（``_focus_hook_connected``
        # と同じ理由・同じ作法）。
        self._tags_watch_connected = True
        # Debounce: restarted on every change signal; only when it fires without
        # a further change (mtime static ≥ interval) do we reload.
        self._tags_reload_timer = Debouncer(
            self, 2500, self._on_tags_reload_debounced, mode=DebounceMode.TRAILING
        )
        # Off-thread signature probe.  窓の他のプローブと同じ
        # :class:`GuardedStream`（専用 1 スレッド + 世代 + 着地の選別）。
        self._tags_probe_reload = False
        self._tags_probe_stream = GuardedStream(self)
        self._tags_probe_stream.bind(self._on_tags_db_probed)

    def _on_tags_db_changed(self, _path: str = "") -> None:
        """Watcher fired — rearm the quiescence debounce, touch no disk.

        The slot must not ``stat`` (e.g. ``tags_db.exists()`` to re-add a
        path :class:`QFileSystemWatcher` drops once the file is atomically
        replaced): with ``data/`` on a half-dead SMB share that is a full
        protocol timeout per event, and the tagger emits a flurry of them per
        scan.  Both the existence check and the ``(mtime, size)`` comparison run
        in the worker dispatched once the flurry goes quiet, so a running scan
        costs zero GUI-thread I/O no matter how many events arrive.
        """
        self._tags_reload_timer.trigger()

    def _on_tags_reload_debounced(self) -> None:
        """The write flurry went quiet — probe the file off the GUI thread."""
        self._probe_tags_db(reload_on_change=True)

    def _probe_tags_db(self, *, reload_on_change: bool) -> None:
        """Dispatch the ``tags.db`` existence + ``(mtime, size)`` probe.

        ``reload_on_change=False`` only refreshes the baseline signature (used
        right after a manual / provider-driven reload has already re-opened the
        indexes) — it never triggers a second reload.

        The generation token makes this *converging*: only the newest request
        is applied, so a single ``_tags_probe_reload`` flag matching the newest
        dispatch is all the intent that has to be carried.  A no-op when the AI
        pack is unavailable — the watcher and its bridge are not built then.
        """
        stream = getattr(self, "_tags_probe_stream", None)
        if stream is None:
            return
        self._tags_probe_reload = reload_on_change
        path = self._tags_db_path
        # The payload is wrapped in a 1-tuple so a *worker exception* (which
        # the stream reports as a bare ``None`` payload) stays distinguishable
        # from a legitimate "file absent" (``None`` signature).
        stream.submit(lambda: (_tags_db_signature(path),))

    def _on_tags_db_probed(self, payload: object) -> None:
        """Apply a landed probe (GUI thread) — see :meth:`_probe_tags_db`."""
        if not isinstance(payload, tuple):
            return  # the worker raised (the stream reports ``None``)
        sig = payload[0]
        self._rearm_tags_db_watch(present=sig is not None)
        if not self._tags_probe_reload:
            self._tags_db_sig = sig
            return
        # Reload only if the file actually changed since the last known state
        # (the watcher also fires for unrelated files in data/).
        if sig == self._tags_db_sig:
            return
        self._tags_db_sig = sig
        self._reload_tag_indexes(announce=True, refresh_signature=False)

    def _rearm_tags_db_watch(self, *, present: bool) -> None:
        """Re-add tags.db to the watcher after an atomic replace.

        :class:`QFileSystemWatcher` drops a watched file path once it is
        replaced (sqlite WAL checkpoint / tagger rewrite); the directory watch
        survives, which is what keeps waking the debounce.  Called only from a
        landed probe, so at most once per quiescence window instead of once per
        watcher event — and only when the probe just proved the file readable,
        so ``addPath``'s own internal stat lands on a share that answered
        milliseconds ago.
        """
        if not present:
            return
        target = str(self._tags_db_path)
        try:
            if target not in self._tags_watcher.files():
                self._tags_watcher.addPath(target)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("tags.db watcher re-add failed: {}", exc)

    def _on_reload_tag_db_requested(self) -> None:
        """バナー / エラーカードの［再読み込み］— 結果を必ず告げる.

        ``reload_tag_db_requested`` は引数なしシグナルなので直結すると
        ``announce`` は既定の False になり、押した本人だけが唯一フィードバック
        の無い経路になっていた（自動リロードは announce=True で告げている）。
        ``connect`` にラムダを直書きしないのは、テストから呼べる面を残すため。
        """
        self._reload_tag_indexes(announce=True)

    def _reload_tag_indexes(
        self, *, announce: bool = False, refresh_signature: bool = True
    ) -> None:
        """Close + re-open tags.db and re-inject the fresh readers (item K01).

        Re-points every consumer of the old ``TagIndex`` / ``VectorIndex`` — the
        left pane, the cache-build controller, an open detail window, and the
        right-pane / central-preview similar-search availability — so a scan run
        after the viewer opened takes effect without a restart.  Read access is
        ``mode=ro`` + ``PRAGMA query_only``, so re-opening is safe even while the
        tagger holds the DB.

        ``refresh_signature`` re-takes the baseline ``(mtime, size)`` so the
        watcher doesn't announce this very reload a second time.  It goes
        through the off-thread probe and is skipped when the
        caller is the probe itself, which already holds a fresh signature.
        """
        old_tag, old_vec = self._tag_index, self._vector_index
        self._tag_index = self._open_tag_index()
        self._vector_index = self._open_vector_index()
        if refresh_signature:
            self._probe_tags_db(reload_on_change=False)
        # Close the previous readers only after re-opening (the new handles are
        # independent connections); guard against double-close on the same file.
        for old, new in ((old_tag, self._tag_index), (old_vec, self._vector_index)):
            if old is not None and old is not new:
                try:
                    old.close()
                except Exception:  # pragma: no cover (defensive)
                    pass
        # Left pane: panel gating, scanners, NSFW menu, similar overlay.
        self._post_grid.set_tag_indexes(self._tag_index, self._vector_index)
        # Cache-build controller reads tag_index for its settings-dialog stats.
        self._cache_ctrl.set_tag_index(self._tag_index)
        # Right pane + central preview similar-search availability.
        has_vec = self._vector_index is not None
        self._file_list.set_similar_search_available(has_vec)
        self._content.set_similar_search_available(has_vec)
        # An open detail window follows the current selection off the new DB.
        if self._detail_window is not None:
            # 表示規則（``_ai_ui`` を含む可視条件 + 活性条件）は窓側が持つ。
            self._detail_window.set_tag_indexes(self._tag_index, self._vector_index)
            self._detail_window.show_path(self._current_preview_path)
        if announce:
            # 結果はトーストの単一ファネルで告げる（成功は非モーダル。ステータス
            # バーの一時メッセージだとバナーの［再読み込み］から来たとき見落とす）。
            # 「開けた」と「まだ読めない」を kind で
            # 描き分ける。
            opened = self._tag_index is not None
            self._show_toast(
                t("viewer.main_window.tags_db_reloaded") if opened
                else t("viewer.main_window.tags_db_reload_gone"),
                "success" if opened else "warning",
            )

    def _open_user_meta(self) -> tuple[UserMetaStore | None, str | None]:
        """Open (creating if absent) the user-curation store + a failure reason.

        Best-effort like the caches: a read-only volume / locked file / corrupt
        DB degrades to ``None`` so the viewer runs without stars / user tags /
        「あとで見る」.  Unlike the caches, the degradation is NOT silent: the
        store holds the only non-regenerable user data, so an empty curation
        surface is indistinguishable from "every star was lost".  ``open_or_report``
        hands back the reason and :meth:`_notify_user_meta_unavailable`
        shows it once, non-modally, on first show.  The corrupt file itself is
        never deleted / recreated — see ``user_meta.open_or_report``.
        """
        try:
            return UserMetaStore.open_or_report(get_paths().data)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("user_meta.db unavailable: {}", exc)
            return None, str(exc)

    def _notify_user_meta_unavailable(self) -> None:
        """One-shot, non-modal notice that curation writes are off.

        A warning toast rather than a dialog: startup must not be gated on an
        OK press, and the status bar's transient line is too easy to miss for a
        whole feature going dark.  ``duration_ms=0`` keeps it until clicked —
        the same treatment ``cache_build_controller`` gives its partial-failure
        warning; a 3-second toast during the first scan would be missed.
        """
        reason = getattr(self, "_user_meta_error", None)
        if not reason or getattr(self, "_user_meta_notice_shown", True):
            return
        self._user_meta_notice_shown = True
        self._show_toast(
            t("viewer.main_window.user_meta_unavailable", reason=reason),
            "warning",
            duration_ms=0,
        )

    def _notify_persist_failed(self) -> None:
        """設定 / ブックマーク / 保存した検索の書き込み失敗を 1 度だけ告げる。

        書き込み不可な NAS / 読み取り専用メディアでは ``save_state`` /
        ``persist_bookmarks`` / ``persist_saved_searches`` がログを 1 行残す
        だけなので、告げなければ利用者には直後の「保存しました」成功トースト
        しか見えない（＝データが消えたことに気づけない）。

        様式は :meth:`_notify_user_meta_unavailable` をそのまま踏襲する
        （新しい通知様式を増やさない）: **セッション 1 回・``duration_ms=0`` の
        常駐警告トースト**。3 秒トーストでは「機能が丸ごと効かなくなった」級の
        事実が見逃されるという同じ理由がここにも当てはまり、モーダルにすると
        保存のたびに OK 押下を強いることになる。呼び出し元は失敗時に成功
        トーストを出さない（片方だけ残ると矛盾した 2 枚が並ぶ）。
        """
        if getattr(self, "_persist_notice_shown", False):
            return
        self._persist_notice_shown = True
        self._show_toast(
            t("viewer.main_window.persist_failed"), "warning", duration_ms=0,
        )

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.setHandleWidth(6)

        self._post_grid = PostGrid(
            loader=self._loader,
            icon_size=self._state.icon_size,
            list_icon_size=self._state.list_icon_size,
            sort_mode=self._state.sort_mode,
            view_mode=self._state.grid_view_mode,
            exclude_thumb_marker=self._state.exclude_thumb_marker,
            icon_size_max=self._state.post_grid_icon_size_max,
            thumb_layout=self._state.grid_thumb_layout,
            meta_cache=self._meta_cache,
            folder_cache=self._folder_cache,
            search_index=self._search_index,
            tag_index=self._tag_index,
            vector_index=self._vector_index,
            user_meta=self._user_meta,
            probe_parallelism=self._state.aspect_probe_parallelism,
        )
        # Propagate the persisted scan parallelism so the first folder
        # load uses the user's setting, not the module default (via the
        # grid's public wrapper, not its private ``_scanner``).
        self._post_grid.set_scan_metadata_parallelism(
            self._state.scan_metadata_parallelism
        )
        # 「ロックありのみ」はセッション限りの検索軸
        # （``state.filter_locked_only`` は load で捨てられ save でも書かれない）。
        # ここで読み書きしても常に False を往復するだけの死にコードなので、
        # 起動時の setChecked / 終了時の書き戻しは持たない — 初期値は
        # ``PostGrid._filter_locked_only = False`` とチェックボックス既定で足りる。
        # NSFW view suppression (item 2-1) — a persistent view setting.
        self._post_grid.set_hide_nsfw(self._state.hide_nsfw)
        # Restore the advanced-search panel controls from persisted state.
        self._post_grid.restore_tag_settings(self._state)
        self._post_grid.set_show_favorites(self._state.show_post_favorites)
        self._post_grid.folder_selected.connect(self._on_folder_selected)
        self._post_grid.folder_activated.connect(self._on_folder_activated)
        self._post_grid.file_selected.connect(self._on_grid_file_selected)
        self._post_grid.file_activated.connect(self._on_grid_file_activated)
        self._post_grid.go_back_requested.connect(self._on_go_back)
        self._post_grid.go_forward_requested.connect(self._on_go_forward)
        self._post_grid.go_up_requested.connect(self._on_go_up)
        # E1: 初回起動カードの [操作の基本 (F1)] → 操作ガイドの「はじめに」。
        self._post_grid.help_requested.connect(
            lambda: self._open_shortcuts_dialog(TASK_START)
        )
        self._post_grid.breadcrumb_navigate.connect(self._on_breadcrumb_navigate)
        self._post_grid.reveal_in_app_requested.connect(self._on_reveal_in_app)
        self._post_grid.reload_requested.connect(self._on_reload)
        self._post_grid.root_change_requested.connect(self._on_root_change_requested)
        self._post_grid.loading_changed.connect(self._on_loading_changed)
        self._post_grid.scan_failed.connect(self._on_scan_failed)
        self._post_grid.scan_partial.connect(self._on_scan_partial)
        self._post_grid.search_status_changed.connect(self._on_search_status_changed)
        self._post_grid.counts_changed.connect(self._on_counts_changed)
        # K01: the AI-panel's 「再読み込み」 button re-opens tags.db in place.
        self._post_grid.reload_tag_db_requested.connect(
            self._on_reload_tag_db_requested
        )
        # 条件バーの「この検索を保存…」— メニュー項目と同じスロット。
        self._post_grid.save_search_requested.connect(self._on_save_current_search)
        self._install_history_menus()

        self._content = ContentView()
        self._content.file_link_clicked.connect(self._on_file_link_clicked)
        self._content.post_link_clicked.connect(self._on_post_link_clicked)
        self._content.navigate_requested.connect(self._on_content_navigate)
        # 最大化プレビューの Home/End（全画面と同じ
        # 「先頭 / 末尾の画像へ」）。Space は navigate_requested(+1) で届く。
        self._content.jump_edge_requested.connect(self._on_content_jump_edge)
        # In-view preference toggles (context menu / control bar) write back
        # into the persisted state so they survive a restart (saved on close).
        self._content.image_zoom_persist_toggled.connect(
            self._on_image_zoom_persist_toggled
        )
        self._content.image_minimap_toggled.connect(self._on_image_minimap_toggled)
        self._content.markdown_font_pt_changed.connect(
            self._on_markdown_font_pt_changed
        )
        self._content.media_loop_toggled.connect(self._on_media_loop_toggled)
        self._content.media_volume_changed.connect(self._on_media_volume_changed)
        self._content.media_playback_rate_changed.connect(
            self._on_media_playback_rate_changed
        )
        # F08: 「開いて閲覧」 on the ZIP preview drills into the archive, exactly
        # like a double-click.
        self._content.zip_open_requested.connect(self._open_zip_as_folder)
        self._content.image_info_changed.connect(self._on_image_info_changed)
        # 代表画像がデコードできなかったときだけ、
        # 同じフォルダの次候補へ静かにフォールバックする。
        self._content.image_load_failed.connect(self._on_preview_image_failed)
        # A02: 「フォルダを開く…」 on the empty-library welcome card routes to
        # the same root picker as the File menu / header button.
        self._content.open_folder_requested.connect(self._pick_root)
        # E1: ようこそカードの [操作の基本 (F1)] → 操作ガイドの「はじめに」。
        self._content.help_requested.connect(
            lambda: self._open_shortcuts_dialog(TASK_START)
        )
        # 「上の階層へ」 on the empty-folder card — same
        # navigation as ↑ / Alt+Up.
        self._content.go_up_requested.connect(self._on_go_up)
        # 最大化中・未選択のカードの [◧ 分割ビューに戻す (G)] は G /
        # Esc / ステージヘッダーと**同じ**離脱経路へ（履歴の対称性を共有）。
        self._content.restore_split_requested.connect(self._exit_stage_to_browse)
        # C-10 extension: "この画像に類似を検索" from the central image
        # preview / inline post.md images.  Same handler as the right-pane
        # file list's similar-search entry.
        self._content.similar_search_requested.connect(
            self._on_similar_search_requested
        )
        # Resolve ``post.md`` body links to downloaded posts via the search
        # index's ``postref`` table (a no-op when the index is disabled).
        self._content.set_post_link_resolver(self._resolve_post_link)

        self._file_list = FileListView(
            loader=self._file_thumb_loader,
            view_mode=self._state.file_list_view_mode,
            icon_size=self._state.file_list_icon_size,
            list_icon_size=self._state.file_list_list_icon_size,
            icon_size_max=self._state.file_list_icon_size_max,
            thumb_layout=self._state.file_list_thumb_layout,
            sort_mode=self._state.file_list_sort_mode,
            exclude_thumb_marker=self._state.exclude_thumb_marker,
            meta_cache=self._meta_cache,
            folder_cache=self._folder_cache,
            search_index=self._search_index,
            probe_parallelism=self._state.aspect_probe_parallelism,
        )
        # Click semantics match the left pane: single-click previews (no
        # ZIP drill), double-click activates (ZIP drill-in, etc.).  Folder
        # signals stay the same — single-click navigates, double-click
        # drills down.
        # 「クリエイターアイコンを隠す」 is ONE setting for both panes.
        # The grid owns the toolbar chrome (the window is only its seat), so
        # the window relays the toggle to the right pane the same way it
        # reaches into ``locked_check`` / ``size_slider``.
        self._post_grid.exclude_thumb_check.toggled.connect(
            self._file_list.set_exclude_thumb_marker
        )
        self._file_list.file_selected.connect(self._on_file_selected)
        self._file_list.file_activated.connect(self._on_file_activated)
        self._file_list.folder_activated.connect(self._on_file_list_folder_activated)
        # 右一覧のフォルダ右クリック「最近追加されたファイルを表示」を
        # 左ペインのビューへ転送する — 右ペインは左ペインを知らない。
        self._file_list.recent_files_requested.connect(
            self._post_grid.enter_recent_files_view
        )
        self._file_list.reveal_in_app_requested.connect(self._on_reveal_in_app)
        self._file_list.folder_selected.connect(self._on_file_list_folder_selected)
        # Backspace（``GalleryView.keyPressEvent`` の ``go_up_requested``）を
        # 右一覧からも受ける（左ペインだけ繋ぐと右一覧にフォーカスがあるとき
        # 無反応で、効かない理由も画面から読めない）。↑ボタン / Alt+Up と同じ
        # スロットへ寄せる — 両ペインで同じキーが同じ意味になる。
        self._file_list.go_up_requested.connect(self._on_go_up)
        # C-10: right-pane "この画像に類似を検索" seeds the left pane's similar
        # search.  Always connect, but only offer the menu item when vectors
        # exist (else set_similar_seed is a no-op and the menu stays hidden).
        self._file_list.similar_search_requested.connect(
            self._on_similar_search_requested
        )
        self._file_list.set_similar_search_available(
            self._vector_index is not None
        )
        # User-curation overlays on the right pane reuse the left pane's
        # in-memory map (single source of truth), so a star set on a post
        # folder shows on its files and vice-versa.  The window relays the
        # left pane's ``curation_changed`` to repaint this pane + any lightbox.
        if self._user_meta is not None:
            self._file_list.set_curation_provider(
                self._post_grid._curation_badge_for
            )
            # バッジの意味をホバーで言葉に展開する —
            # 左ペインと同じ行（同じ provider）を右ペインにも配る。
            self._file_list.set_tooltip_extra_provider(
                self._post_grid.curation_tooltip_lines
            )
            # 右一覧も★バッジを描くので、右クリックと 0-5 キーにも付与手段を
            # 持たせる（「読めるのに書けない」非対称を作らない）。どちらの入口も左ペインの単一書き手へ送るだけ（所有は移さない）。
            self._file_list.curation_requested.connect(
                self._on_file_list_curation_requested
            )
            self._file_list.star_key_requested.connect(
                self._on_file_list_star_key
            )
            self._post_grid.curation_changed.connect(self._on_curation_changed)
            # プレビュー列（画像 / PDF / ZIP / テキスト / メディア / post.md）の
            # 右クリックにも印の節を出す。
            # 口は左ペインと同じ 2 関数（同期の辞書引き + 単一書き手の funnel）。
            self._content.set_curation_hooks(self._post_grid.curation_hooks())
        # 全面占有オーバーレイ入場・検索着地でグリッドの母集合が
        # 入れ替わり選択が残らなかったとき、プレビュー列と右パネルを set_root と
        # 同じリセット規約へ落とす。``_file_list`` / ``_content`` が揃った後で
        # 配線する（スロットが両方に触る）。
        self._post_grid.preview_context_lost.connect(
            self._on_grid_preview_context_lost
        )
        # 母集合を全面入れ替えする入口は、どこから撃たれても分割ビューへ戻す。
        # 入口ごとに窓側で前置きを書くと新しい入口で片側が欠けるので、グリッド
        # 自身に告げさせて配線 1 本で受ける。
        self._post_grid.population_replacing.connect(
            self._leave_stage_for_overlay
        )
        # C-10 extension: same availability gate for the central preview
        # (ImageView + inline post.md images in MarkdownView).
        self._content.set_similar_search_available(
            self._vector_index is not None
        )
        # 閲覧モード: offer 「全画面で表示 (F11)」 on the centre ImageView's
        # context menu (the lightbox's own internal ImageView keeps it off).
        self._content.set_fullscreen_available(True)
        self._content.image_fullscreen_requested.connect(self._toggle_lightbox)

        # ImageView prefetches neighboring image files via the right-pane
        # list.  Wire it here (not inside ContentView) so ImageView stays
        # decoupled from file_list — the provider is just a callable.
        self._content.set_image_siblings_provider(self._file_list.image_siblings)
        # Stage-0 placeholder: reuse the right pane's already-decoded
        # thumbnail so the central preview paints instantly (blurry) while
        # the full-resolution decode runs.  Same decoupling rationale as
        # the siblings provider — ImageView only sees a callable.
        self._content.set_image_thumbnail_provider(self._file_list.pixmap_for_path)
        # フォルダプレビューの子タイルサムネ供給 — 専用ローダーを
        # ContentView 経由で FolderPreviewView へ注入する。
        self._content.set_folder_thumbnail_loader(self._folder_preview_loader)
        # File list's spinner overlay reads current cache residency for
        # each image item; connect the cache_updated signal so prefetch
        # completions immediately clear their pending indicator.
        self._file_list.set_cache_check(self._content.image_decode_settled)
        self._content.image_cache_updated.connect(
            self._file_list.notify_cache_changed
        )
        # Push persisted cache sizing into the two views before any image
        # is rendered.  Without this, the first post after launch would
        # use the module-level defaults and only switch to the user's
        # saved values on the next settings-dialog commit.
        self._content.apply_cache_settings(self._state)
        # Same startup push for the grid / file-list tunables (caption flags,
        # tile-name placement, list caps): without this they only take effect
        # after the first settings-dialog commit.
        self._post_grid.apply_settings(self._state)
        self._file_list.apply_settings(self._state)
        # Restore persisted view-preferences into the sub-views at startup.
        # apply_view_settings covers media (autoplay / volume / loop) via
        # apply_media_settings as well as image zoom-persist / minimap and the
        # markdown font, so no separate apply_media_settings call is needed.
        self._content.apply_view_settings(self._state)

        # --- Split-view centre (layout redesign 2026-07, candidate A).
        # The centre area is a horizontal QSplitter: [PostGrid | preview
        # column].  The old browse ⇄ stage page switch (QStackedWidget) is
        # gone — "stage" is now just the preview-maximised split preset
        # (grid pane collapsed to 0 via setSizes).  ``_ui_mode`` keeps its
        # historical values ("browse" = split view / "stage" = preview
        # maximised) so the NavEntry history semantics stay byte-compatible.
        # Both panes are parented ONCE here; every split ⇄ maximise flip is
        # setSizes only, so the grid's scroll position and selection survive
        # every round trip with no flicker (never reparented).
        self._ui_mode = "browse"
        # 席のフォーカス履歴 — メニューバーがキーボードモードで焦点を握って
        # いる間も ``resolve_target`` が直前の席で解決できるように覚える。
        self._last_focus_seat: str | None = None
        QApplication.instance().focusChanged.connect(self._on_app_focus_changed)
        self._focus_hook_connected = True
        # 同一ルートの再スキャン（F5 / notify_library_changed）で最大化を維持
        # したまま非同期スキャンを待つときのフラグ。着地（loading→False）時に
        # 現選択が消えていたら分割へフォールバックする。
        self._stage_settle_pending = False
        # 全画面（ライトボックス）へ入ったときの表示態。
        # 投稿横断で現ルート外へ渡ると復路が ``set_root`` の再ルートになり、
        # そちらは必ず分割へ着地する（ナビゲーションは分割、という既定）ため、
        # 「最大化から F11 → 閉じたら分割に戻っていた」と黙って態が変わって
        # いた。入場時の態を控えて復路で戻す。
        self._lightbox_entry_mode = "browse"
        # プレビューヘッダーのタイトル（post.md の実タイトル — 非同期の
        # info-meta 読み取りが着地したときだけ (folder, title) で上書きする）。
        self._stage_title_override: "tuple[Path, str] | None" = None
        self._info_meta_folder: Path | None = None
        # 代表画像の自動選択（フォルダ選択 → 右一覧の pending-select）で
        # ファイル詳細カードがメタカードを置き換えないための one-shot 抑止
        # （共通コア: post.md のメタは「追加表示」— 自動選択で潰さない）。
        self._auto_select_no_detail: Path | None = None
        # --- 画像トラック（96px）: プレビュー最大化中のみ表示 — 分割時は右
        # 情報パネルのファイル一覧が同役割。中身は右ペイン file list のタイル
        # 集合の鏡映で、‹ ›/←→ の画像送りと完全同期。旧・投稿トラック
        # （StagePostStrip 56px）は完全廃止（常時見えるグリッド本体が代替）。
        self._image_strip = StageFilmstrip()
        self._image_strip.request_thumb.connect(
            self._on_image_strip_thumb_requested
        )
        self._image_strip.clicked.connect(self._on_image_strip_clicked)
        # 兄弟セルの★有無マーク — 左ペインの
        # インメモリ map からの同期読み取り（sqlite / NAS なし）。
        self._image_strip.set_curation_provider(
            self._post_grid._curation_badge_for
        )
        # 画像トラックは右ペインのタイル再構築（スキャン着地 / 並び替え）に追従。
        self._file_list.tiles_changed.connect(self._on_file_list_tiles_changed)
        # プレビューヘッダー（常設）: 分割時 = ‹ › の項目送り（アイコンのみ）+
        # タイトル + n/m + [⤢ 最大化 (E)]。最大化時 = [◧ 分割に戻す (G)] が
        # 加わり、右端が [閲覧モード (F11)] に入れ替わる。
        self._stage_header = StageHeader()
        self._stage_header.back_requested.connect(self._exit_stage_to_browse)
        self._stage_header.fullscreen_requested.connect(self._toggle_lightbox)
        # 分割時のヘッダー右端 [⤢ 最大化 (E)] —
        # 出口 [◧ 分割に戻す] と同じ席・同じ文法の入口。
        self._stage_header.maximize_requested.connect(self._enter_stage_mode)
        self._stage_header.prev_post_requested.connect(
            lambda: self._step_post(-1)
        )
        self._stage_header.next_post_requested.connect(
            lambda: self._step_post(1)
        )
        # post.md 本文表示中の「‹ 画像に戻る」。
        self._stage_header.back_to_media_requested.connect(
            self._on_stage_back_to_media
        )
        # 0-5 スターキー（プレビューでもグリッド/ライトボックス
        # と同様にスターを付けられる）。ハンドラ側で user_meta 不在は no-op。
        self._content.star_key_requested.connect(self._on_stage_star_key)
        # プレビュー列（旧 _stage_page）。子ビューが消費しない面のダブル
        # クリックは分割 ⇄ 最大化のトグル。
        self._preview_column = PreviewColumn()
        preview_layout = QVBoxLayout(self._preview_column)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        preview_layout.addWidget(self._stage_header)
        preview_layout.addWidget(self._content, 1)
        preview_layout.addWidget(self._image_strip)
        self._image_strip.setVisible(False)
        self._preview_column.double_clicked.connect(self._toggle_preview_focus)
        # 画像上のダブルクリック: 分割中は「最大化」、最大化中は従来の
        # 等倍⇄フィット切替（_set_preview_focus がフラグを反転する）。
        self._content.preview_maximize_requested.connect(
            self._toggle_preview_focus
        )
        self._content.set_double_click_maximize(True)

        self._center_split = QSplitter(Qt.Horizontal)
        self._center_split.setHandleWidth(6)
        self._center_split.addWidget(self._post_grid)
        self._center_split.addWidget(self._preview_column)
        self._center_split.setStretchFactor(0, 11)
        self._center_split.setStretchFactor(1, 9)
        # 分割比: 永続値（無ければ 55:45）を適用し、最大化からの復帰用に
        # 記憶する。ユーザーのドラッグ（splitterMoved）で記憶を更新。
        split_sizes = self._initial_center_split_sizes(
            self._state.center_split_sizes
        )
        self._center_split.setSizes(split_sizes)
        self._center_split_saved: list[int] = list(split_sizes)
        # 初回表示時に実幅へ比率スケールして再適用する one-shot（構築時の
        # setSizes は未レイアウト幅に対するもので配分が比率どおりにならない）。
        self._center_split_ratio_applied = False
        self._center_split.splitterMoved.connect(self._on_center_split_moved)
        # プレビュー列の表示トグル（折り畳み導線 2026-07）: False = 分割の
        # プレビュー席を幅 0 に畳んだ「グリッドだけ」の見た目。分割比の記憶
        # （_center_split_saved）は常に両ペイン正の値を保つ — 畳みは表示
        # 状態であって比率ではない。再表示 / E での最大化が記憶比率から復元
        # する。ドラッグで 0 にした場合もこのフラグへ同一視で追従する
        # （_on_center_split_moved）。
        self._preview_visible = bool(self._state.preview_visible)
        # QAction / 表示 popover check / ツールバーボタンの相互 setChecked
        # 再入ガード（_syncing_info_panel と同型）。
        self._syncing_preview = False
        # 最大化に入る直前の「F6 で非表示」状態。最大化は
        # プレビュー席を必ず可視にするため、分割へ戻るときにこのフラグで
        # 元の非表示状態へ戻す（設定が黙って上書きされない）。
        self._preview_hidden_before_stage = False
        if not self._preview_visible:
            total = sum(split_sizes)
            self._center_split.setSizes([max(1, total), 0])

        # Left seat = ナビレール: always-on library /
        # bookmark / saved-search lists.  A dumb view — the window feeds it lists
        # from the same _rebuild_* points that refresh the menus, and acts on its
        # click / manage signals via the existing navigation + dialog routes.
        self._nav_rail = NavRail()
        self._nav_rail.navigate_root.connect(self._on_root_change_requested)
        self._nav_rail.navigate_bookmark.connect(self._jump_to_bookmark)
        self._nav_rail.apply_saved_search.connect(self._apply_saved_search)
        # 横断キュレーション一覧をレールから直接開く
        # （メニューの奥と同じ ``enter_curation_view`` へ配線）。
        self._nav_rail.open_curation.connect(self._on_rail_curation_requested)
        self._nav_rail.manage_libraries.connect(self._manage_libraries)
        self._nav_rail.manage_bookmarks.connect(self._manage_bookmarks)
        self._nav_rail.manage_saved_searches.connect(self._manage_saved_searches)
        # Re-entrancy guard for the mutual setChecked between the F7 QAction and
        # the 表示 popover check (same pattern as _syncing_info_panel).
        self._syncing_nav_rail = False
        # キュレーション行の初期投入（以後は counts_changed で追従）。
        self._sync_nav_rail_curation()

        # Right seat = 情報パネル: the post meta card
        # stacked above the file list (which is reparented into it).  The panel
        # is a thin view — the window feeds it an already-parsed ParsedPost via
        # ``_refresh_info_meta`` (read off the GUI thread); ``_file_list`` stays
        # the same object, so every existing wiring above is untouched.
        self._info_panel = InfoPanel(self._file_list)
        if self._user_meta is not None:
            # 印ストリップ（E2）: 情報パネル最上段 / ステージヘッダーの 2 席を
            # 左ペインの単一 funnel へつなぐ（全画面の席は ``_ensure_lightbox``）。
            self._info_panel.set_store_available(True)
            self._info_panel.curation_requested.connect(
                self._on_file_list_curation_requested
            )
            self._stage_header.set_store_available(True)
            self._stage_header.curation_requested.connect(
                self._on_file_list_curation_requested
            )
        # メタカード末尾の「本文を読む」リンク（分割ビュー再設計 2026-07）:
        # クリックで選択中投稿の post.md 本文をプレビューへ出す。
        self._info_panel.post_body_requested.connect(self._on_read_post_body)
        # Off-thread post.md meta read (専用ストリーム — 素早いナビゲーション
        # では古い読みが着地せず捨てられる。``_file_info_*`` と同じ形)。
        self._info_meta_stream = GuardedStream(self)
        self._info_meta_stream.bind(self._on_info_meta_read)
        # Last-read (folder, ParsedPost|None) so the meta card can be re-shown /
        # collapsed on a mode change without re-reading post.md
        # (``_apply_meta_card`` collapses it while the stage shows that post.md).
        self._info_meta_last: "tuple[Path | None, ParsedPost | None] | None" = None
        # post.md read の一過性失敗（共有の瞬断・書き込み中の並走
        # 読み等）で ``parsed=None`` が選択変更まで固定されないよう、失敗着地
        # から短い backoff 後に **1 回だけ** 再読込するタイマー。無限リトライは
        # NAS 断で有害なので ``_info_meta_retried`` が回数上限（=1）を担う。
        self._info_meta_retried = False
        self._info_meta_retry_timer = Debouncer(
            self,
            _INFO_META_RETRY_DELAY_MS,
            self._retry_info_meta,
            mode=DebounceMode.ONE_SHOT,
        )
        # ``_pending_file_detail``（宣言は上の起動復元ブロック）へ入れるカードの
        # フィールドを埋める off-thread の読み（token ガードは stream 側）。
        self._file_detail_stream = GuardedStream(self)
        self._file_detail_stream.bind(self._on_file_detail_read)
        # Re-entrancy guard: the F8 QAction and the 表示 popover check both feed
        # _on_info_panel_toggled and are synced back to it, so guard the
        # mutual setChecked from looping.
        self._syncing_info_panel = False

        self._splitter.addWidget(self._nav_rail)
        self._splitter.addWidget(self._center_split)
        self._splitter.addWidget(self._info_panel)
        # フォーカスがどの席にあるかを**見出し帯**で示す（キーの効き方が
        # 6 通りに分岐する設計には、文言より可視化）。灯るのは
        # フォーカスを内包する最も内側の節の見出しで、Alt+1/2/3 の
        # 移動先もそのまま見える。グリッドは見出しを持たないので登録しない —
        # 選択タイルの減光（GalleryView._selection_active）が同じ役を担う。
        self._pane_focus_bands = PaneFocusBands(
            [
                *self._nav_rail.focus_band_targets(),
                (self._preview_column, self._stage_header),
                *self._info_panel.focus_band_targets(),
            ],
            self,
        )
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 7)
        self._splitter.setStretchFactor(2, 2)
        # ContentView wraps a QStackedWidget whose minimumSizeHint is the
        # max of all sub-views (MediaView/PdfView/... control bars).  Without
        # this, QSplitter can't shrink the center pane past that hint and
        # cascades the drag to the opposite pane instead.  R2 審査の指摘:
        # プレビュー列に min 幅（320px 等）を入れるとこのドラッグ競合が再発
        # するため、必ず 0 を維持すること。
        self._nav_rail.setMinimumWidth(0)
        self._center_split.setMinimumWidth(0)
        self._post_grid.setMinimumWidth(0)
        self._preview_column.setMinimumWidth(0)
        self._content.setMinimumWidth(0)
        self._file_list.setMinimumWidth(0)
        self._info_panel.setMinimumWidth(0)

        # Base [nav-rail | centre | file list] sizes (old-layout migration in
        # _initial_splitter_sizes), then carve the rail's default column out of
        # the centre when the rail is shown and no width is remembered (old
        # snapshots persisted 0 for the then-empty seat).
        sizes = self._initial_splitter_sizes(self._state.splitter_sizes)
        if self._state.nav_rail_visible and len(sizes) == 3 and sizes[0] == 0:
            give = min(_NAV_RAIL_DEFAULT_WIDTH, max(0, sizes[1] - 100))
            sizes[1] -= give
            sizes[0] = give
        self._splitter.setSizes(sizes)
        # Remembered 情報パネル width so re-showing it after a hide restores a
        # sensible column (a hidden splitter child reports width 0).  Seeded
        # from the persisted/default right-column size.
        self._info_panel_saved_width = max(
            self._splitter.sizes()[2], _INFO_PANEL_DEFAULT_WIDTH
        )
        # Same remembered-width machinery for the ナビレール.
        self._nav_rail_saved_width = max(
            self._splitter.sizes()[0], _NAV_RAIL_DEFAULT_WIDTH
        )
        # Apply the persisted visibilities (F7/F8 / 表示 popover toggles).  The
        # 表示 popover checks + the QActions are synced to these in _build_menus.
        self._info_panel.setVisible(self._state.info_panel_visible)
        self._nav_rail.setVisible(self._state.nav_rail_visible)
        # 外殻スプリッタのハンドルドラッグで幅 0 まで畳んだ状態をトグル OFF と
        # 同一視する（折り畳み導線 2026-07）: checked 表示と保存幅の追従のみ —
        # ドラッグ中に setVisible はしない（同じジェスチャで引き戻せるように）。
        self._splitter.splitterMoved.connect(self._on_outer_split_moved)

        # Unified toolbar: the left pane's chrome rows
        # live on ONE window-level 40px bar above the splitter.  The widgets
        # (and their state / persistence) are still owned by PostGrid — the
        # window only mounts the bar, so all signal wiring above is untouched.
        # Directly under it sits the condition chip bar: applied
        # search-condition chips + hit count + すべて解除, full window width,
        # hidden (zero height) while nothing is engaged.  Same ownership
        # pattern — PostGrid owns, the window only mounts.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._post_grid.toolbar)
        central_layout.addWidget(self._post_grid.condition_bar)
        # ナビ系ツールバーボタンを Tab リングから外す。
        # 載せると起動直後の初期フォーカスがツールバー先頭の「↑ 上の階層へ」に
        # 落ち、最初の Space/Enter 一発でライブラリの外へ出てしまう（グリッドへは
        # マウスか Tab 十数回でしか到達できない）。ナビは ←/→/Alt+←/Alt+→/
        # Backspace/F5 とメニューでキーボード到達できるので、ボタン自体を
        # フォーカス対象にする必要は無い。
        for btn in (
            self._post_grid.back_btn,
            self._post_grid.forward_btn,
            self._post_grid.up_btn,
            self._post_grid.reload_btn,
        ):
            btn.setFocusPolicy(Qt.NoFocus)
        central_layout.addWidget(self._splitter, 1)
        self.setCentralWidget(central)
        # ステータスバーの 6 セグメントは部品 ``window_status.WindowStatus``
        # が組む（合成ロジックと同じ場所に構築を置く）。各セグメントは
        # ウィンドウ属性として公開される（``_path_label`` / ``_counts_label``
        # / ``_file_info_label`` / ``_resolution_label`` / ``_loading_label``
        # / ``_cache_build_status``）。
        self._status.build_status_bar()
        # 「ファイル名 · サイズ」セグメントの非同期 stat（off-thread —
        # ``_stat_file_label``）。専用ストリームの世代が矢印キー連打中の古い
        # 着地を捨てる。
        # 進行中の stat が「フォルダの代表画像」宛か。着地時に
        # 「代表:」の淡色接頭を付けるかどうかを決める。
        self._file_info_representative = False
        self._file_info_path: Path | None = None
        self._file_info_stream = GuardedStream(self)
        self._file_info_stream.bind(self._on_file_info_statted)
        # Tab 巡回を画面の並び（左上 → 右下）に揃える。
        self._apply_tab_order()

    def _tab_order_widgets(self) -> list[QWidget]:
        """Tab 巡回に載せる席を**画面の見た目の並び**で返す.

        ツールバー（パンくず → フォルダを開く → 検索欄 → フィルタ →
        並び・表示）→ 3 ペイン（ナビレール → グリッド →
        プレビュー → 情報パネルの一覧）。ナビ 4 ボタン・モードチップ・
        ペイントグルは NoFocus なのでそもそも巡回に乗らない。
        ``getattr`` ガードは __init__ を通さないテストハーネス向け。
        """
        grid = self._post_grid
        seats: list[QWidget | None] = [
            getattr(grid, "breadcrumb", None),
            getattr(grid, "change_root_btn", None),
            getattr(grid, "filter_edit", None),
            getattr(grid, "filter_btn", None),
            getattr(grid, "view_popover_btn", None),
            self._nav_rail,
            grid.findChild(GalleryView),
            self._content,
            self._file_list.findChild(GalleryView),
        ]
        chain: list[QWidget] = []
        for seat in seats:
            if seat is not None:
                chain.extend(self._tab_stops(seat))
        return chain

    @staticmethod
    def _tab_stops(widget: QWidget) -> list[QWidget]:
        """*widget* が占める席の**実フォーカス受け**を視覚順に返す.

        パンくずのように「自身は NoFocus で、中の子ボタンが Tab を受ける」
        複合ウィジェットは、子（= 移動のたびに作り直されるセグメント）まで
        展開しないと巡回順を決められない — 実測でも、席だけを並べた
        ``setTabOrder`` では新しいセグメントがチェーン末尾に付いたままだった。
        """
        if widget.focusPolicy() != Qt.NoFocus:
            return [widget]
        return [
            child for child in widget.findChildren(QWidget)
            if child.focusPolicy() != Qt.NoFocus
        ]

    def _tab_order_key(self) -> tuple[str, int]:
        """巡回チェーンが変わりうる条件の**安価な**指紋.

        チェーンの構成が変わるのは実質パンくずだけ（他の席は同一の
        ウィジェットが座り続ける）。パンくずは ``set_path`` / 折りたたみで
        行レイアウトを組み直すので、(ルート, パンくず行の要素数) で
        「作り直された」を判定できる — どちらも再帰探索なしで読める。
        """
        grid = getattr(self, "_post_grid", None)
        crumb = getattr(grid, "breadcrumb", None) if grid is not None else None
        layout = crumb.layout() if crumb is not None else None
        return (
            str(getattr(self, "_root", "") or ""),
            layout.count() if layout is not None else -1,
        )

    def _apply_tab_order(self, *, only_if_changed: bool = False) -> None:
        """視覚順の Tab 巡回を（再）適用する.

        既定の巡回はウィジェットの**生成順**なので、後から生成される
        パンくずのセグメントボタンや、ツールバーより先に作られるペイン群が
        画面の並びと食い違って現れていた（報告書: 「パンくず・表示オプションが
        後方に紛れる」）。パンくずは移動のたびにセグメントを作り直すため、
        フォルダが変わるたびに再適用する（``_on_counts_changed`` から）。

        *only_if_changed* はその高頻度経路用:
        ``_on_counts_changed`` はグリッド再構築のたび = **絞り込み 1 文字ごと**
        に走るのに対し、この再適用は 3 ペイン分の再帰 ``findChildren`` を伴う
        （setTabOrder の回数だけ見れば安いが、走査が重い）。
        :meth:`_tab_order_key` が変わったときだけ歩く。
        """
        if only_if_changed:
            key = self._tab_order_key()
            if key == getattr(self, "_tab_order_applied_key", None):
                return
            self._tab_order_applied_key = key
        else:
            self._tab_order_applied_key = self._tab_order_key()
        widgets = self._tab_order_widgets()
        for first, second in zip(widgets, widgets[1:], strict=False):
            self.setTabOrder(first, second)

    @staticmethod
    def _initial_splitter_sizes(saved: list[int] | None) -> list[int]:
        """Column sizes for the [nav-rail seat | centre | file list] splitter.

        Migration: older snapshots persisted
        ``[post_grid, content, file_list]`` for the former three-pane seating.
        In the current seating the first column is the nav-rail seat
        (persisted as 0 by that format), so an old snapshot —
        recognisable by a non-zero first entry — folds its grid+content
        widths into the new centre column instead of donating ~320px to an
        invisible pane.  New-format snapshots (first entry 0) apply as-is.
        """
        if saved and len(saved) == 3:
            if saved[0] > 0:
                return [0, saved[0] + saved[1], saved[2]]
            return list(saved)
        return [0, 1000, 280]

    @staticmethod
    def _initial_center_split_sizes(saved: list[int] | None) -> list[int]:
        """[grid, preview] sizes for the centre split (candidate A, 55:45).

        A persisted snapshot applies as-is when it names two panes that are
        BOTH visible (the persist path substitutes the remembered split for
        a collapsed 0 on either side — maximised ``[0, x]`` or preview
        folded ``[x, 0]`` — so a 0 only appears in hand-edited / corrupt
        state — fall back to the default rather than seeding
        ``_center_split_saved`` with a ratio that can't be restored to).
        The preview-collapsed look itself round-trips via
        ``ViewerState.preview_visible``, not via a 0 in this snapshot.

        席が ``_SPLIT_REMEMBER_MIN_PX`` 未満の比率も「復元できない比率」と
        して既定へ倒す — ドラッグ追従が書き残しうる
        ``[866, 8]`` 級の潰れた比率を、次回起動で引きずらないため。
        """
        if (
            saved
            and len(saved) == 2
            and saved[0] >= _SPLIT_REMEMBER_MIN_PX
            and saved[1] >= _SPLIT_REMEMBER_MIN_PX
        ):
            return list(saved)
        return list(_CENTER_SPLIT_DEFAULT_SIZES)

    def _apply_center_split_sizes(self, sizes: list[int]) -> None:
        """*sizes* を比率として現在の中央幅へスケールして適用する。

        QSplitter の ``setSizes`` は合計が実幅と食い違うと比率どおりに配分
        されない（実測: 55:45 を要求しても 42:58 に潰れる）ため、記憶した
        分割比は常に現在の合計幅へ正規化してから適用する。
        """
        split = self._center_split
        total = sum(split.sizes())
        a, b = (sizes + [0, 0])[:2]
        denom = a + b
        if total > 0 and denom > 0:
            grid = round(total * a / denom)
            split.setSizes([grid, total - grid])
        else:
            split.setSizes(list(sizes))

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        # 初回表示: 分割比（永続値 / 既定 55:45）を実幅へスケールして適用
        # （one-shot）。レイアウト確定後に走らせるため 1 tick 遅延させる。
        # getattr 既定 True: __init__ を通さないテストハーネス（_bare_window）
        # では何もしない（set_root 系の getattr ガードと同じ理由）。
        #
        # 受け手コンテキスト形（3 引数）で積む: 窓が tick より先に破棄されると
        # Qt がコールバックを自動キャンセルするので、破棄済みの子ウィジェット
        # （``_center_split`` / トースト）へ触れて "Internal C++ object already
        # deleted" をイベントループへ送出しない（``children_grid`` の同型の
        # 遅延適用と同じ流儀）。2 引数形には受け手が無く、破棄後も走る。
        if not getattr(self, "_center_split_ratio_applied", True):
            self._center_split_ratio_applied = True
            QTimer.singleShot(0, self, self._apply_initial_center_split)
        # キュレーション保存が無効な理由を初回表示で一度だけ知らせる。
        # 同じ getattr 既定（``_bare_window`` ハーネスでは何もしない）。
        if getattr(self, "_user_meta_error", None) and not getattr(
            self, "_user_meta_notice_shown", True
        ):
            QTimer.singleShot(0, self, self._notify_user_meta_unavailable)

    def _apply_initial_center_split(self) -> None:
        if self._ui_mode != "browse":
            return
        if not getattr(self, "_preview_visible", True):
            # 前回終了時にプレビュー列を畳んでいた: 実幅でも畳み直すだけ
            # （記憶比率 _center_split_saved は復元用にそのまま温存）。
            split = self._center_split
            split.setSizes([max(1, sum(split.sizes())), 0])
            return
        self._apply_center_split_sizes(self._center_split_saved)

    def _remember_center_split(self, sizes: list[int]) -> None:
        """ドラッグ中の *sizes* を「再表示で戻る比率」として採用する。

        畳みジェスチャの通過点を記憶に採らないよう、両席に「再表示既定
        （55:45）の各シェアを現在幅へスケールした値 ×
        ``_CENTER_SPLIT_REMEMBER_MIN_SHARE``」の下限を課す（
        ``_SPLIT_REMEMBER_MIN_PX`` の 80px 下限だけでは 80px〜既定幅の帯を通る
        畳みサンプルが記憶を侵食し、F6 の再表示や最大化解除が細い列を復元して
        その比率がそのまま永続化される）。下限未満のサンプルでは直前の正常な記憶を温存する。
        """
        if len(sizes) != 2:
            return
        total = sum(sizes)
        denom = sum(_CENTER_SPLIT_DEFAULT_SIZES)
        if total <= 0:
            return
        for size, default in zip(sizes, _CENTER_SPLIT_DEFAULT_SIZES, strict=False):
            floor = total * (default / denom) * _CENTER_SPLIT_REMEMBER_MIN_SHARE
            if size < floor:
                return
        self._center_split_saved = list(sizes)

    def _on_center_split_moved(self, _pos: int, _index: int) -> None:
        """Track the user's centre-split drags (split-view redesign).

        While the split view is showing, remember every two-pane ratio the
        user settles on so a maximise → restore round trip comes back to it
        (and ``_collect_state`` persists it).  Dragging the grid pane back
        out *while maximised* means "return to the split view": adopt the
        dragged sizes as the remembered ratio and run the normal exit path
        (history symmetry included).  Collapsing the preview to 0 by hand
        stays plain split mode — the grid-only look the redesign kept
        available.  Collapsing the *grid* to 0 by hand IS the maximised
        look, so it takes the same transition as E (mode, header, strip,
        history).

        記憶比率の採用には「両席が再表示既定シェア×
        ``_CENTER_SPLIT_REMEMBER_MIN_SHARE`` 以上」の下限を課す
        （``_remember_center_split`` 参照）: この関数は
        ドラッグ中の全サンプルで呼ばれるので、席を 0 まで畳むジェスチャは
        必ず途中の細いサンプルを通過し、そのまま記憶すると再表示が細い帯に
        なってしまう。
        """
        sizes = self._center_split.sizes()
        if len(sizes) != 2:
            return
        if self._ui_mode == "stage":
            if sizes[0] > 0:
                self._remember_center_split(sizes)
                self._exit_stage_to_browse()
        elif sizes[0] > 0 and sizes[1] > 0:
            self._remember_center_split(sizes)
            # ドラッグで畳みから引き戻した = プレビュー再表示（トグルと同一視）。
            self._set_preview_visible_flag(True)
        elif sizes[1] == 0:
            # プレビュー席を手で 0 まで畳んだ = トグル OFF と同一視（checked
            # 状態と永続フラグが追従する。記憶比率は上の分岐が「畳みへ向かう
            # 途中の極小サンプル」を弾いたうえで保持済み）。
            self._set_preview_visible_flag(False)
        else:
            # グリッド席を手で 0 まで畳んだ = プレビュー最大化と同一視（E と
            # 同じ遷移 — 履歴・ヘッダー・画像トラック・Esc が揃って追従する。
            # 記憶比率は上の分岐が保持済みで、_set_preview_focus 側も [0, x]
            # では退避しない。setSizes は splitterMoved を発火しないので再入
            # しない）。
            self._enter_stage_mode()
        # 席が 0 を跨いだサンプルでは空状態の裁定入力が変わる（外殻スプリッタ
        # 側と同じ — 畳んだ席は主案内を持てない）。
        self._resync_placeholder_on_seat_change()

    def _build_menus(self) -> None:
        bar = self.menuBar()

        # File menu
        file_menu = bar.addMenu(t("viewer.main_window.menu_file"))
        act_open = QAction(t("viewer.main_window.open_folder"), self)
        act_open.setShortcut(QKeySequence("Ctrl+O"))
        act_open.triggered.connect(self._pick_root)
        file_menu.addAction(act_open)
        self._recent_menu = file_menu.addMenu(
            t("viewer.main_window.recent_folders")
        )
        self._rebuild_recent_menu()
        # ライブラリ submenu (M02): explicitly-registered browse roots the user
        # switches between, distinct from the auto-tracked MRU above.
        self._library_menu = file_menu.addMenu(
            t("viewer.main_window.library_menu")
        )
        self._library_menu.setToolTip(t("viewer.main_window.library_menu_hint"))
        # 「現在のフォルダを登録」の活性は今のルートで決まる（ZIP 展開先は
        # 登録できない）— ブックマークと同じく開くたびに評価し直す。
        self._library_menu.aboutToShow.connect(self._sync_library_register_action)
        self._rebuild_library_menu()
        act_up = QAction(t("viewer.main_window.go_up"), self)
        act_up.setShortcut(QKeySequence("Alt+Up"))
        act_up.triggered.connect(self._on_go_up)
        file_menu.addAction(act_up)
        # ライブラリ境界では ↑ を無効化する
        # （``_update_nav_buttons`` がツールバーの ↑ と一緒に同期する）。
        self._act_go_up = act_up
        act_up.setEnabled(self._can_go_up())
        file_menu.addSeparator()
        act_settings = QAction(t("common.action.settings"), self)
        act_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self._open_settings_dialog)
        file_menu.addAction(act_settings)
        # プラグイン管理（plugin_host/dialog.py）。セーフモード起動でも
        # 開ける（host 無しでは検出 + 設定変更のみ = 次回起動から反映）。
        act_plugins = QAction(t("viewer.plugins.menu"), self)
        act_plugins.triggered.connect(self._open_plugin_manager)
        file_menu.addAction(act_plugins)
        file_menu.addSeparator()
        act_quit = QAction(t("common.action.quit"), self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # Edit menu — operations on what the user has selected.
        # Everything search-flavoured lives in the dedicated 検索 menu below
        # (users looking for search scan the menu bar for a 検索 entry and
        # never think to open 編集).
        #
        # This menu carries ★ / あとで見る / ユーザータグ の付与 and the shared
        # コピー系 so they are not reachable ONLY under the right button (which
        # is unreachable while the preview is maximised).  All of it is
        # "an operation on the current selection", which is what 編集 is for.
        # The per-selection block is rebuilt on ``aboutToShow`` from the very
        # same builder the right-click menu uses (``PostGrid.populate_entry_menu``)
        # so the two can never drift; the あとで見る toggle stays a PERSISTENT
        # QAction because its ``L`` accelerator must work before the menu has
        # ever been opened.
        edit_menu = bar.addMenu(t("viewer.main_window.menu_edit"))
        self._edit_menu = edit_menu
        # No shortcut on this QAction: Ctrl+C is already an ImageView-focus
        # QShortcut (and QTextBrowser's text copy) — a menu accelerator would
        # steal it globally.  The label notes the focus-scoped key instead.
        act_copy_image = QAction(t("viewer.main_window.copy_shown_image"), self)
        act_copy_image.triggered.connect(self._on_copy_current_image)
        edit_menu.addAction(act_copy_image)
        edit_menu.addSeparator()
        # 「あとで見る」 toggle — the third curation verb gets a key.
        # ``L`` は未使用（``grep Key_L`` = 0 件）で、E / G と同じ
        # 素の英字ウィンドウショートカット: フォーカス中の QLineEdit が
        # ShortcutOverride を受けるので、検索欄で "l" を打っても発火しない。
        self._act_toggle_later = QAction(t("viewer.post_grid.watch_later"), self)
        self._act_toggle_later.setShortcut(QKeySequence(Qt.Key_L))
        self._act_toggle_later.setToolTip(
            t("viewer.main_window.watch_later_hint")
        )
        self._act_toggle_later.triggered.connect(self._on_toggle_later)
        # メニューには載せず、ウィンドウ直下のショートカット担体だけにする —
        # 編集メニューの「あとで見る」は選択対象の共通ブロック（右クリックと
        # 同じ builder）が出すので、載せると 2 行並ぶ。
        # ラベル側に「(L)」を併記して予告は残す。
        self.addAction(self._act_toggle_later)
        # 横断一覧への入口。
        self._curation_menu = edit_menu.addMenu(
            t("viewer.nav_rail.section_curation")
        )
        self._curation_menu.aboutToShow.connect(self._rebuild_curation_menu)
        self._rebuild_curation_menu()
        # 選択中の項目に対する共通操作（右クリックと同一の builder）。
        self._edit_entry_actions: list[QAction] = []
        self._edit_entry_host: QMenu | None = None
        edit_menu.aboutToShow.connect(self._sync_edit_menu)

        # Search menu — filter-box focus (Ctrl+F), AI tag search, saved
        # searches.  Ctrl+F goes to the filter box because "find something in
        # the current view" is the most common search action.  post.md 本文の
        # 検索は同じ検索欄の ``body:`` 構文（「本文」チップ）に統合されている
        # — 単独の全文検索ダイアログは持たない。
        search_menu = bar.addMenu(t("viewer.main_window.menu_search"))
        act_filter = QAction(t("viewer.main_window.filter_left_pane"), self)
        act_filter.setShortcut(QKeySequence("Ctrl+F"))
        act_filter.triggered.connect(self._focus_filter_box)
        search_menu.addAction(act_filter)
        # AI 検索の入口は AI 機能パック（有償プラグイン）有効時のみ現れる —
        # 素の配布では「存在するのに使えない機能」を UI に出さない方針。
        if ai_pack.available():
            act_ai_tag = QAction(t("viewer.main_window.ai_tag_search"), self)
            act_ai_tag.setShortcut(QKeySequence("Ctrl+Shift+T"))
            act_ai_tag.triggered.connect(self._focus_tag_search)
            search_menu.addAction(act_ai_tag)
        # Saved searches / smart folders (M03): name the current search and
        # re-apply it later against whatever root is open.
        search_menu.addSeparator()
        act_save_search = QAction(t("viewer.main_window.save_search"), self)
        act_save_search.triggered.connect(self._on_save_current_search)
        search_menu.addAction(act_save_search)
        self._saved_search_menu = search_menu.addMenu(
            t("viewer.main_window.saved_searches")
        )
        self._rebuild_saved_search_menu()

        # Bookmarks menu
        self._bookmarks_menu = bar.addMenu(t("viewer.main_window.menu_bookmarks"))
        # 追加/解除の活性は「今のルートが登録済みか」で
        # 決まる — 開くたびに評価し直す（メニュー再構築はブックマークの増減
        # 時だけなので、それだけではルート移動に追随しない）。
        self._bookmarks_menu.aboutToShow.connect(self._sync_bookmark_actions)
        self._rebuild_bookmarks_menu()

        # View menu — preview focus + detail window + theme
        view_menu = bar.addMenu(t("viewer.main_window.menu_view"))
        # 分割 ⇄ プレビュー最大化の項目（分割ビュー再設計 2026-07）。E/G の
        # 実バインドはこの QAction が担う — _install_shortcuts 側に素の
        # QShortcut を重複登録しないこと（両方がマッチすると
        # activatedAmbiguously になり双方 dead になる）。
        # 「プレビューを最大化」は実際にはトグル（E は
        # 分割 ⇄ 最大化を往復する）なので、メニューにも現在の状態を示し
        # ラベルと動作を一致させる。同メニュー内の F6/F7/F8 と同じ
        # **checkable ☑ 文法**へ揃える（checked = いま最大化中）。
        self._act_stage_mode = QAction(
            t("viewer.main_window.preview_maximize_menu"), self, checkable=True
        )
        self._act_stage_mode.setShortcut(QKeySequence(Qt.Key_E))
        self._act_stage_mode.setChecked(self._ui_mode == "stage")
        self._act_stage_mode.triggered.connect(
            lambda _checked=False: self._toggle_preview_focus()
        )
        view_menu.addAction(self._act_stage_mode)
        # 「分割ビューに戻す」は最大化中しか意味を持たない片道の項目 — 分割中は
        # 無効化して「押せるのに何も起きない」を無くす。
        self._act_browse_mode = QAction(
            t("viewer.main_window.preview_split_menu"), self
        )
        self._act_browse_mode.setShortcut(QKeySequence(Qt.Key_G))
        self._act_browse_mode.setEnabled(self._ui_mode == "stage")
        self._act_browse_mode.triggered.connect(
            lambda _checked=False: self._exit_stage_to_browse()
        )
        view_menu.addAction(self._act_browse_mode)
        view_menu.addSeparator()
        act_detail = QAction(t("viewer.main_window.detail_window"), self)
        act_detail.setShortcut(QKeySequence("Ctrl+I"))
        act_detail.triggered.connect(self._open_detail_window)
        view_menu.addAction(act_detail)
        # 閲覧モード（全画面ライトボックス）の発見性向上 — G05: F11 と同じ入口を
        # メニューにも出す（プレビュー中の画像、無ければ現在フォルダの先頭から起動）。
        # F11 はラベル内表記ではなく setShortcut で載せる
        # （E/G と同じパターン — ラベル内に書くとメニューのショートカット列だけが
        # 空になり、この項目だけ列が欠けて見える）。文言はメニュー実物と
        # ショートカット一覧で同じキーを共有する。
        act_lightbox = QAction(t("viewer.common.lightbox_mode"), self)
        act_lightbox.setShortcut(QKeySequence(Qt.Key_F11))
        act_lightbox.triggered.connect(self._toggle_lightbox)
        view_menu.addAction(act_lightbox)
        view_menu.addSeparator()
        # 「最近追加されたファイル」: flatten the selected folder (or, with nothing
        # selected, the current root) into a newest-first file listing keyed on
        # the files' own 更新日時.  Lives in 表示 because it is a way of looking at
        # the folder you are already in, not a search; the folder right-click
        # menu carries the same entry point for an explicitly-picked folder.
        self._act_recent_files = QAction(
            t("viewer.main_window.recent_files"), self
        )
        self._act_recent_files.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self._act_recent_files.setToolTip(
            t("viewer.main_window.recent_files_hint")
        )
        self._act_recent_files.triggered.connect(
            lambda _checked=False: self._on_recent_files_requested()
        )
        view_menu.addAction(self._act_recent_files)
        view_menu.addSeparator()
        # ペイン表示トグル 3 兄弟（折り畳み導線 2026-07 — メニューの並びは
        # 画面上の配置と同じ 左→右）。各ペインとも QAction（ショートカット）
        # + ツールバー右端のペイントグルボタンの 2 導線が同じハンドラへ
        # 集まり、checked 同期はハンドラ側の _set_*_checks が一手に行う。
        # （「並び・表示」popover には同じ ☑ を置かない — 同一バー上の
        # 二重提示になる。）
        # ナビレール (左) 表示トグル: F7。
        self._act_nav_rail = QAction(
            t("viewer.main_window.nav_rail_toggle"), self, checkable=True
        )
        self._act_nav_rail.setShortcut(QKeySequence("F7"))
        self._act_nav_rail.setChecked(not self._nav_rail.isHidden())
        self._act_nav_rail.toggled.connect(self._on_nav_rail_toggled)
        view_menu.addAction(self._act_nav_rail)
        self._post_grid.nav_rail_btn.setChecked(not self._nav_rail.isHidden())
        self._post_grid.nav_rail_btn.toggled.connect(self._on_nav_rail_toggled)
        # プレビュー列 (中央右) 表示トグル (折り畳み導線 2026-07): F6。
        self._act_preview = QAction(
            t("viewer.main_window.preview_toggle"), self, checkable=True
        )
        self._act_preview.setShortcut(QKeySequence("F6"))
        self._act_preview.setChecked(self._preview_visible)
        self._act_preview.toggled.connect(self._on_preview_toggled)
        view_menu.addAction(self._act_preview)
        self._post_grid.preview_btn.setChecked(self._preview_visible)
        self._post_grid.preview_btn.toggled.connect(self._on_preview_toggled)
        # 情報パネル (右) 表示トグル: F8。
        self._act_info_panel = QAction(
            t("viewer.main_window.info_panel_toggle"), self, checkable=True
        )
        self._act_info_panel.setShortcut(QKeySequence("F8"))
        self._act_info_panel.setChecked(not self._info_panel.isHidden())
        self._act_info_panel.toggled.connect(self._on_info_panel_toggled)
        view_menu.addAction(self._act_info_panel)
        self._post_grid.info_panel_btn.setChecked(
            not self._info_panel.isHidden()
        )
        self._post_grid.info_panel_btn.toggled.connect(
            self._on_info_panel_toggled
        )
        view_menu.addSeparator()
        # (kept as attributes — like _saved_search_menu / _bookmarks_menu — so
        # tests can reach the real menu objects without fragile traversal)
        theme_menu = self._theme_menu = view_menu.addMenu(
            t("viewer.main_window.theme_menu")
        )
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        # (label_key holds the i18n key; the stable theme key stays as userData.)
        # The choice tables live in viewer/theme.py so this menu and the
        # settings dialog's theme combo can never drift apart.

        def _add_theme_action(menu, key: str, label: str) -> None:
            act = QAction(label, self, checkable=True)
            act.setData(key)
            act.setChecked(self._state.theme == key)
            act.triggered.connect(lambda _checked=False, k=key: self._on_theme_chosen(k))
            self._theme_group.addAction(act)
            menu.addAction(act)

        for key, label_key in THEME_CHOICES_MAIN:
            _add_theme_action(theme_menu, key, t(label_key))
        # 追加テーマ 6 種は「その他」サブメニューへ（同じ排他グループに入れる
        # ので、チェックマークはメイン 4 択と合わせて常に 1 つだけ点く）。
        # ラベルは設定ダイアログのコンボと同じ ``extra_theme_label``（明暗
        # サフィックス付き）— 素の名前だけでは 6 種の明暗が読めない
        # （設定ダイアログのコンボと同じ表記に揃える）。
        self._theme_extra_menu = theme_menu.addMenu(
            t("viewer.main_window.theme_extra_menu")
        )
        for key, label_key in THEME_CHOICES_EXTRA:
            _add_theme_action(
                self._theme_extra_menu, key, extra_theme_label(key, label_key)
            )

        # Diagnostics menu — perf measurement toggle + stats dialog.
        # Intended for debugging "the left pane feels slow on my NAS":
        # enable measurement, reproduce the slow navigation, open the
        # stats dialog to see which stage (scandir / post.md / thumbnail
        # decode / main-thread UI) is actually consuming the time.
        diag_menu = bar.addMenu(t("viewer.main_window.menu_diagnostics"))
        self._act_toggle_perf = QAction(
            t("viewer.main_window.perf_measure_enable"), self, checkable=True
        )
        self._act_toggle_perf.setChecked(recorder().is_enabled())
        self._act_toggle_perf.toggled.connect(self._on_toggle_perf)
        diag_menu.addAction(self._act_toggle_perf)

        act_show_perf = QAction(t("viewer.main_window.perf_stats_show"), self)
        act_show_perf.setShortcut(QKeySequence("Ctrl+Shift+P"))
        act_show_perf.triggered.connect(self._open_perf_dialog)
        diag_menu.addAction(act_show_perf)

        act_clear_perf = QAction(t("viewer.main_window.perf_clear"), self)
        act_clear_perf.triggered.connect(lambda: recorder().clear())
        diag_menu.addAction(act_clear_perf)

        diag_menu.addSeparator()
        act_health = QAction(t("viewer.main_window.health_check"), self)
        act_health.triggered.connect(self._open_health_dialog)
        diag_menu.addAction(act_health)

        # J01: promote cache pre-build out of the settings dialog's collapsed
        # cache tab onto the menu.  Same CacheBuildController path the settings
        # dialog uses (its ``_bg_builder`` guard blocks a second concurrent
        # build); a status tip explains what pre-building buys the user.
        act_build_cache = QAction(t("viewer.common.cache_prebuild"), self)
        act_build_cache.setStatusTip(t("viewer.main_window.cache_prebuild_hint"))
        act_build_cache.setToolTip(t("viewer.main_window.cache_prebuild_hint"))
        act_build_cache.triggered.connect(self._open_cache_prebuild)
        diag_menu.addAction(act_build_cache)

        # User-triggered Explorer shell integration (「Snappix Viewer で
        # 開く」 right-click verb).  Portable policy forbids automatic registry
        # writes, so this stays an explicit, reversible opt-in behind a dialog.
        diag_menu.addSeparator()
        act_shell = QAction(t("viewer.shell_integration.menu"), self)
        act_shell.triggered.connect(self._open_shell_integration_dialog)
        diag_menu.addAction(act_shell)

        # Help menu — shortcut cheat-sheet + legal documents + version info.
        help_menu = bar.addMenu(t("common.menu.help"))
        # 「使い方」を探す初見が最初に見る行。中身は
        # ショートカット一覧と同じ面（マウス規約・ドリルイン・バッジ凡例まで
        # 載っている事実上の「操作の基本」— shortcuts_dialog の docstring 参照）
        # なので、面を増やさず**別名の入口**を足すだけにする。
        act_getting_started = QAction(
            t("viewer.main_window.getting_started"), self
        )
        act_getting_started.setStatusTip(
            t("viewer.main_window.getting_started_hint")
        )
        act_getting_started.setToolTip(
            t("viewer.main_window.getting_started_hint")
        )
        # 入口の名前が作業を名指ししている（「はじめに」）ので、着地も同じ
        # ページにする — 前回開いたページのまま上がってこない。
        act_getting_started.triggered.connect(
            lambda _checked=False: self._open_shortcuts_dialog(TASK_START)
        )
        help_menu.addAction(act_getting_started)
        # 入口名は着地タイトルと同じキーで描く — 改名が
        # 窓側だけで止まって「キーボードショートカット一覧」を選んだ人が
        # 「ショートカットと画面の凡例」に着く、という不一致を構造的に断つ
        # （バッジ凡例がここにしか無いことも、名前で分かるようになる）。
        act_shortcuts = QAction(t("viewer.shortcuts_dialog.window_title"), self)
        act_shortcuts.setShortcut(QKeySequence("F1"))
        # F1 だけはアプリ全体スコープにする。既定の
        # ``WindowShortcut`` だと詳細情報 / 健全性 / 統計 / 操作ガイド自身の
        # ようなモードレスの別トップレベル窓、そして全画面ライトボックスに
        # フォーカスがある間は届かず、「困ったら F1」という唯一の出口が死ぬ。
        # F11 / E / G / Alt+1-3 は広げない（行き先の席が主窓にしか無い）。
        # プロセス内の ViewerWindow は 1 つ（``app.py`` が唯一の生成点）なので
        # ``activatedAmbiguously`` は起きない — **多窓化するならここが衝突点**。
        act_shortcuts.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        act_shortcuts.triggered.connect(self._on_help_shortcut)
        help_menu.addAction(act_shortcuts)
        # A08: point users at the shipped AI-tag search setup guide
        # (同梱の tagger-models.md — legal_docs が解決) — the モデル入手→配置→スキャン path.
        # AI 機能パック有効時のみ（素の配布に AI の導線を出さない）。
        if ai_pack.available():
            act_tagger_setup = QAction(
                t("viewer.main_window.tagger_setup"), self
            )
            act_tagger_setup.triggered.connect(self._help.open_tagger_setup_doc)
            help_menu.addAction(act_tagger_setup)
        help_menu.addSeparator()
        # 同梱の手引き（dist 直下の
        # 「はじめにお読みください.txt」）へアプリ内から辿れるようにする。
        # 解決は legal_docs（get_paths().base 基準）— 利用規約・ライセンスの
        # 導線と同型で、ビューアは配布レイアウトを自前で知らない。
        act_readme = QAction(t("viewer.main_window.shipped_readme"), self)
        act_readme.triggered.connect(self._help.open_shipped_readme)
        help_menu.addAction(act_readme)
        act_terms = QAction(t("common.legal.terms_menu"), self)
        act_terms.triggered.connect(self._show_terms)
        help_menu.addAction(act_terms)
        act_licenses = QAction(t("common.legal.third_party_menu"), self)
        act_licenses.triggered.connect(self._help.open_third_party_licenses)
        help_menu.addAction(act_licenses)
        help_menu.addSeparator()
        # A discoverable route to data/logs for problem reports — placed by
        # About so "report a problem" material sits together.
        act_logs = QAction(t("viewer.main_window.open_logs_folder"), self)
        act_logs.triggered.connect(self._help.open_logs_folder)
        help_menu.addAction(act_logs)
        act_about = QAction(t("viewer.main_window.about_menu"), self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

        # プラグインの安定 API（PluginContext.get_menu / add_menu_action）が
        # 参照するトップメニューのレジストリ。キーは公開契約
        # （docs/PLUGIN_DEVELOPMENT.md の MENU_IDS）なので変えないこと。
        self._menus = {
            "file": file_menu,
            "edit": edit_menu,
            "search": search_menu,
            "bookmarks": self._bookmarks_menu,
            "view": view_menu,
            "diagnostics": diag_menu,
            "help": help_menu,
        }

    # ------------------------------------------------------------- plugins

    def attach_plugin_host(self, host) -> None:
        """bootstrap_plugins が構築済みの PluginHost を渡す（app.py 起動時）。"""
        self._plugin_host = host

    def set_safe_mode(self, on: bool) -> None:
        """セーフモード（``--no-plugins`` / ``SNAPPIX_NO_PLUGINS=1``）の告知.

        ``logger.info`` とプラグイン管理ダイアログの注記だけでは
        **自分で開かないと気づけない**。配布の
        ``.bat`` に ``--no-plugins`` を書いたまま忘れる導線があるので、常時
        見えるタイトルバーへ出す。``_plugin_host is None`` を代用しないのは、
        それが「まだ attach されていない」とも区別できないため。
        """
        self._safe_mode = bool(on)
        self._sync_window_title()

    def notify_safe_mode(self) -> None:
        """タイトルの「（セーフモード）」が何を意味するかを 1 度だけ告げる.

        説明文（``safe_mode_note``）はプラグイン管理ダイアログの中にしか無く、
        初見では括弧書きの意味も戻し方も分からない。``duration_ms=0`` で
        クリックするまで残し、追随ボタンからその管理ダイアログへ導く。
        呼ぶのは ``show()`` の**後**（``app.main``）— 可視化前に出すと席がずれる。
        """
        self._show_toast(
            t("viewer.plugins.safe_mode_note"),
            "warning",
            duration_ms=0,
            action_text=t("viewer.plugins.menu"),
            on_action=self._open_plugin_manager,
        )

    def _sync_window_title(self) -> None:
        """現在のルート（+ セーフモード）からウィンドウタイトルを組み立てる."""
        root = getattr(self, "_root", None)
        if root is None:
            title = t("viewer.main_window.window_title")
        else:
            zip_origin = self._zip_temp_dirs.get(root)
            title = (
                t("viewer.main_window.window_title_zip", name=zip_origin.name)
                if zip_origin is not None
                else t("viewer.main_window.window_title_root", root=root)
            )
        if getattr(self, "_safe_mode", False):
            title = t("viewer.main_window.window_title_safe_mode", title=title)
        self.setWindowTitle(title)

    def _open_plugin_manager(self) -> None:
        from ..common.paths import get_paths
        from .plugin_host.dialog import PluginManagerDialog

        dlg = PluginManagerDialog(
            self,
            host=self._plugin_host,
            plugins_dir=get_paths().base / "plugins",
            safe_mode=self._plugin_host is None,
        )
        dlg.exec()
        dlg.deleteLater()  # 親付き exec ダイアログは隠れるだけで残る

    def _install_shortcuts(self) -> None:
        # ←/→ step the left-pane selection.  MediaView also binds ←/→ (with
        # WidgetWithChildrenShortcut) for sibling-file navigation; when a
        # MediaView control (slider / combo / button) has focus BOTH shortcuts
        # match and Qt fires ``activatedAmbiguously`` on each instead of
        # ``activated``, so neither default handler runs and ←/→ go dead.  We
        # resolve the tie ourselves: if the centre pane (MediaView) currently
        # owns focus, route to sibling navigation; otherwise step the grid.
        sc_left = QShortcut(QKeySequence(Qt.Key_Left), self,
                            activated=lambda: self._step_or_navigate(-1))
        sc_right = QShortcut(QKeySequence(Qt.Key_Right), self,
                             activated=lambda: self._step_or_navigate(1))
        sc_left.activatedAmbiguously.connect(lambda: self._step_or_navigate(-1))
        sc_right.activatedAmbiguously.connect(lambda: self._step_or_navigate(1))
        QShortcut(QKeySequence(Qt.Key_F5), self, activated=self._on_reload)
        # 閲覧モード (fullscreen lightbox): F11 は表示メニューの QAction 側に
        # setShortcut で載っている（E/G と同じパターン。
        # ラベル内に「(F11)」と書くとメニューのショートカット列が空になる）ので、
        # ここに素の QShortcut を重複登録しないこと（両方マッチで
        # activatedAmbiguously になり双方 dead になる）。ライトボックス窓の中では
        # F11/Esc は自前のキーフィルタが処理する（別トップレベル窓なので
        # この WindowShortcut は届かない）。
        # 分割 ⇄ プレビュー最大化のキー (Lightroom G/E semantics) は
        # 表示メニューの QAction 側に載っている（ラベルは
        # 「プレビューを最大化 (E)」☑ /「分割に戻す (G)」）ので、ここに素の QShortcut を
        # 重複登録しないこと（両方マッチで ``activatedAmbiguously`` になり
        # 双方 dead になる）。  Plain-letter window
        # shortcuts stay safe next to text inputs either way: a focused
        # QLineEdit accepts the ShortcutOverride for character keys, so typing
        # "e"/"g" in the toolbar search field never switches modes.
        # Esc はウィンドウレベルの**単一ハンドラ**へ集約する。ステージ中だけ
        # 有効な QShortcut と分割ビューで GalleryView.keyPressEvent が拾う
        # 二本立てにすると、プレビュー列 / 右一覧にフォーカスがあるとき Esc が
        # 完全に無反応になる（どちらの経路にも
        # 乗らない席がある = 配線漏れが構造的に再発する）。常時有効な 1 本にして
        # ``_on_escape`` 側で「テキスト入力へ委譲 → 最大化解除 → 検索/絞り込みの
        # 一括クリア」を分岐させる（フォーカス位置によらず同じ順序）。
        self._sc_escape = QShortcut(
            QKeySequence(Qt.Key_Escape), self, activated=self._on_escape
        )
        # 長押しのリピートで最大化解除から条件の一括解除まで進ませない。
        self._sc_escape.setAutoRepeat(False)
        # Ctrl+←/→ switch the previewed post without stealing the plain ←/→
        # image stepping inside the current post.  Enabled only while the
        # preview is maximised (split mode already has plain ←/→ = grid
        # stepping and the header's ‹前へ|次へ› buttons — 中立の項目送り;
        # a live
        # Ctrl+←/→ would also fight word-jump in focused text fields).
        self._sc_stage_prev = QShortcut(
            QKeySequence("Ctrl+Left"), self,
            activated=lambda: self._step_post(-1),
        )
        self._sc_stage_next = QShortcut(
            QKeySequence("Ctrl+Right"), self,
            activated=lambda: self._step_post(1),
        )
        self._sc_stage_prev.setEnabled(False)
        self._sc_stage_next.setEnabled(False)
        # Keyboard counterparts to the ←/→ chrome buttons.
        QShortcut(QKeySequence("Alt+Left"), self, activated=self._on_go_back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self._on_go_forward)
        # ペイン間のフォーカス移動の専用キー（Tab だけでは実測 14 ストップ
        # 掛かる）。F6/F7/F8 は「左 / 中 / 右の席の
        # **表示**トグル」という内部で一貫した体系なので触らず、**移動**だけを
        # Alt+1/2/3 として足す（既存の Alt 割当は Alt+Up / Alt+← / Alt+→ の
        # 3 つだけで、数字とは衝突しない）。移動先はフォーカス帯（見出し）
        # でそのまま見える。
        QShortcut(
            QKeySequence("Alt+1"), self, activated=self._focus_nav_rail_pane
        )
        QShortcut(
            QKeySequence("Alt+2"), self, activated=self._focus_grid_pane
        )
        QShortcut(
            QKeySequence("Alt+3"), self, activated=self._focus_preview_pane
        )
        # 右情報パネル（4 席目）への直行キー。
        QShortcut(
            QKeySequence("Alt+4"), self, activated=self._focus_info_panel_pane
        )
        # Mouse back/forward (XButton1/XButton2) can't be bound as shortcuts,
        # so an application-wide event filter routes them to the same slots.
        # そのフィルタは **プロセスに 1 本** で、``nav_history`` のモジュール
        # singleton が持つ。窓ごとに ``app.installEventFilter(self)`` すると
        # ``notify`` が配送する全イベント × 生存窓数の C++→Python 遷移になり
        # （実測 5 窓で 0.36 → 11.9us/event）、しかも ``removeEventFilter`` の
        # 対が無いと閉じた窓のぶんまで課金され続ける。
        install_mouse_nav()

    # ------------------------------------------- ペイン間フォーカス移動

    def _focus_nav_rail_pane(self) -> None:
        """Alt+1 — ナビレールへフォーカス（隠れていれば先に出す）。"""
        if self._nav_rail.isHidden() or self._splitter.sizes()[0] == 0:
            self._on_nav_rail_toggled(True)
        self._nav_rail.focus_first_row()

    def _focus_grid_pane(self) -> None:
        """Alt+2 — 中央グリッドへフォーカス。

        最大化中はグリッド席が幅 0 なので、まず分割ビューへ戻す（見えない
        ペインへフォーカスだけ移すと「キーが死んだ」ようにしか見えない —
        ``_on_recent_files_requested`` が最大化中に取る作法と同じ）。
        """
        if self._ui_mode == "stage":
            self._exit_stage_to_browse()
        self._post_grid.focus_grid()

    def _focus_preview_pane(self) -> None:
        """Alt+3 — 中央プレビューへフォーカス（畳んでいれば先に開く）。"""
        sizes = self._center_split.sizes()
        if self._ui_mode != "stage" and len(sizes) == 2 and sizes[1] == 0:
            self._on_preview_toggled(True)
        self._content.focus_current_page()

    def _focus_info_panel_pane(self) -> None:
        """Alt+4 — 右情報パネルへフォーカス（畳んでいれば先に開く）。

        最大化中でも情報パネルは席に残っている（畳むのはグリッド側だけ）ので
        Alt+2 と違って分割へ戻さない。着地は一覧グリッド（``file_list``）—
        パネルの中で唯一キー操作を持つ面。
        """
        sizes = self._splitter.sizes()
        if self._info_panel.isHidden() or (len(sizes) == 3 and sizes[2] == 0):
            self._on_info_panel_toggled(True)
        self._file_list.focus_grid()

    def _step_or_navigate(self, delta: int) -> None:
        """Handle a global ←/→ press, disambiguating against MediaView.

        When focus is inside the centre content pane (e.g. a MediaView slider
        or video widget), ←/→ mean "previous/next sibling file" — the same as
        MediaView's own ←/→ binding — so route through the content-navigate
        path.  Everywhere else, ←/→ step the left-pane grid selection.  This
        also fires from ``activatedAmbiguously`` so ←/→ never go dead while a
        MediaView control holds focus (the two shortcuts otherwise collide).

        Stage mode: the grid is hidden and the centre shows the
        preview, so ←/→ always mean image stepping — the same content-navigate
        path as focus-in-content — regardless of where focus sits.  Filmstrip
        post switching has its own keys (Ctrl+←/→), so the two never collide.
        """
        focus = QApplication.focusWidget()
        if getattr(self, "_ui_mode", "browse") == "stage" or (
            focus is not None and self._content.isAncestorOf(focus)
        ):
            self._on_content_navigate(delta)
        else:
            self._post_grid.step_selection(delta)

    # ----------------------------------------------------------- navigation

    def _update_nav_buttons(self) -> None:
        """Sync the ←/→/↑ chrome buttons with the history stacks + root."""
        grid = getattr(self, "_post_grid", None)
        if grid is None:  # __init__ を通さないテストハーネス
            return
        grid.set_nav_state(bool(self._history), bool(self._forward))
        # ルートが登録ライブラリそのものなら「↑」を無効化する。停止条件が
        # FS ルートだけだと、ライブラリ直下から ↑ を押すと NAS ルートの親の
        # スキャンが始まり、パンくずの「ライブラリ外へ迷い出ない」方針とも
        # 食い違う。
        can_up = self._can_go_up()
        grid.up_btn.setEnabled(can_up)
        act_up = getattr(self, "_act_go_up", None)
        if act_up is not None:
            act_up.setEnabled(can_up)

    def _can_go_up(self) -> bool:
        """「上の階層へ」が意味を持つか。

        **パンくずが親クラムを出すかどうか**と同じ答えを返す（= 上の段が
        あるときだけ True）。パス演算のみ = NAS へ stat しない。

        判定を「現在ルートが登録ライブラリと一致するか」で書くと、外側の
        ライブラリの中に内側のライブラリを登録した構成で、内側の直下から
        ↑ が黙って死ぬ（パンくずは ``pick_library_base`` の**最外**基準を
        採るので親クラムは押せる = 対の片側欠落）。境界の定義を
        パンくずの分節 :func:`~snappix.viewer.breadcrumb.path_segments` 1 本へ
        寄せると、① 登録ライブラリ直下では段が 1 つ = False（
        「ライブラリ外へ迷い出ない」は不変）② 入れ子ライブラリの内側では
        最外基準からの段が続く = True ③ ライブラリ外のフォルダを直接開いた
        ときは FS の全段が出る = True（従来どおり）の 3 つが 1 つの規則で
        揃う。``path_segments`` の最後から 2 番目の段は常に ``root.parent``
        なので、``_on_go_up`` の行き先とも一致する。

        ``getattr`` ガードは構築順（``_default_library`` はペイン構築後に
        決まる）と、``__init__`` を通さないナビゲーションのテストハーネス向け
        — ライブラリ基準が未確定の間は従来どおり「↑ 可」に倒す。
        """
        root = getattr(self, "_root", None)
        if root is None:
            return False
        if root.parent == root:
            return False
        if getattr(self, "_default_library", None) is None:
            return True
        if getattr(self, "_library_roots", None) is None:
            # 追修: ライブラリ一覧すら未設定（``__init__`` を通さないハーネス /
            # 構築途中）— 従来どおり「↑ 可」に倒す。空状態カードの [上の階層へ]
            # もこの述語で出し分けるため、ここで例外を上げない。
            return True
        return len(path_segments(root, self._compute_library_bases())) > 1

    # ------------------------------------------------- mouse side buttons
    # ``nav_history._BackForwardRouter``（プロセス singleton のアプリ級
    # フィルタ）が、アクティブな窓がこの 2 つを持っていれば呼ぶ
    # （``nav_history.MouseNavTarget``）。
    # ``ViewerWindow.eventFilter`` は override しない:
    # **窓ごとにアプリ級フィルタを張らないこと**（全イベント × 生存窓数の
    # C++→Python 遷移になり、閉じた窓のぶんも課金され続ける）。

    def go_back(self) -> None:
        """マウスの戻るボタン（XButton1）."""
        self._on_go_back()

    def go_forward(self) -> None:
        """マウスの進むボタン（XButton2）."""
        self._on_go_forward()

    @staticmethod
    def _is_reachable_dir(path: Path) -> bool:
        """``path.is_dir()`` のバウンデッド版.

        ナビゲーションの同期ゲート（``set_root`` / ↑）が使う。素の
        ``is_dir()`` は到達不能な SMB 共有に対して**数十秒**呼び出しスレッド
        をブロックするため、MRU から死んだ共有を開くと GUI が 15.75 秒固まった
        うえで（``is_dir`` が False を返しても、そこから出るはずのダイアログが
        フリーズ明けの操作と噛み合わず）無言に見えていた — 同じ状況で ⟳
        （再読み込み）は 5.0 秒でエラーカード + [再試行] を出すので、この経路
        だけが劣化していた。

        判定は ``path_probe.probe_path_kind`` の 2 秒プローブへ委譲し、
        **タイムアウト（``None``）は「ディレクトリとみなす」**。これは起動
        （``app.py`` の ``last_root`` 復帰）とブックマークジャンプが既に採って
        いる作法そのもの: 「今この瞬間到達できない」は「消えた」ではないので、
        先へ進めて非同期スキャンの失敗（エラーカード + [再試行] = ⟳ と同じ面）
        に報告させる。確定した不在（``missing`` / ``file``）でだけ従来どおり
        「フォルダが見つかりません」プロンプトへ落ちる。
        """
        return probe_path_kind(str(path)) in ("dir", None)

    def set_root(
        self,
        root: Path,
        *,
        push_history: bool = False,
        pending_select: Path | None = None,
        clear_search: bool = False,
        restore_search: SearchSnapshot | None = None,
        assume_exists: bool = False,
        keep_mode: bool = False,
    ) -> SetRootResult:
        """Re-root the left pane at *root*.  Returns how it ended.

        ``False`` (with a warning dialog) when *root* is no longer a
        directory — the window's state, both history stacks, and the panes
        are left untouched, so callers that mutate the stacks around a
        navigation (戻る/進む, ルート変更) must check the result before
        committing their stack operations.

        ``assume_exists=True`` skips the synchronous ``is_dir`` gate — used at
        startup (B05), where an offline / waking NAS root must not block the
        GUI thread; the async scan then reports failure via the error card.
        「見つかりません」プロンプトで 再試行 を選んだ場合も同じ扱いで先へ
        進む（GUI スレッドの再 stat をしない）。

        ``keep_mode=True`` は「同一ルートの再スキャン」（F5 リロード /
        ``notify_library_changed``）専用で、既定の「新ルートは必ずブラウズへ
        戻る」を抑止して現在の UI モード（ステージ）を維持する。維持は
        モードだけでなく**表示内容**も含む — 中央プレビュー / 右ペイン /
        ``_current_folder`` をブランク化せず、表示中ファイルを pending-restore
        （B01 と同機構）に積んで着地後の選択復元を同じ位置へ復帰させる。
        **表示内容の維持は UI モードに依らない** — 分割ビューでもプレビュー列は常時可視なので、最大化中と同じく
        閲覧中の画像を保つ。最大化そのものの維持だけがステージ限定。再
        スキャンは非同期なので、着地時（``_on_loading_changed`` の loading→False）
        に現選択が消えていた場合のみブラウズへフォールバックする（そのとき初めて
        旧表示を空白化する）。異なるルートを開くときは（``keep_mode`` に関わらず）
        常にブラウズへ戻す。
        """
        previous_root = self._root
        if not assume_exists and not self._is_reachable_dir(root):
            # I07: don't just warn — explain the likely cause (offline drive /
            # rename / delete) and offer 再試行 / 別のフォルダを開く.
            #
            # 「再試行」のたびに GUI スレッドで ``is_dir()`` を再実行する
            # ループにしないこと — 切断 NAS では 1 回あたり数十秒ウィンドウが
            # 固まる。再試行は**同期 stat を
            # 繰り返さず**、起動経路（B05 / ``assume_exists``）と同じく先へ
            # 進めて、存在確認は非同期スキャンの失敗（エラーカード + その中の
            # 再試行ボタン）に委ねる。
            outcome = self._handle_missing_folder(root)
            if outcome != "retry":
                # 「別のフォルダを開く」で内側が着地した（同じルートを選び
                # 直した場合も含む — トレイルはハードリセット済み）なら、
                # 呼び出し側が「何も変わっていない」と読んで状態を巻き戻さない
                # よう結末を型で分ける。
                return (
                    SetRootResult.REROUTED
                    if outcome == "rerouted"
                    else SetRootResult.UNCHANGED
                )
        if push_history and self._root is not None:
            # Remember the *whole* position we're leaving — root, selected
            # sub-folder, and (when clear-on-navigate is enabled) the active
            # search — so 戻る restores the selection and search, not just the
            # root.
            self._history.append(self._capture_current_position())
            # A fresh drill-down/up branches off the trail, so any path the
            # user had undone with 戻る is no longer reachable via 進む.
            self._forward.clear()
            # Drilling into a folder from an active search clears the search so
            # the destination shows its own contents (paired with the restore
            # on 戻る above).  Governed by the setting.
            if self._state.search_clear_on_navigate:
                clear_search = True
        # Resolve the search post-processing once, up front: restore precedes
        # clear, and a no-op transition leaves any persistent search alone
        # (the precedence lives in SearchTransition, not inline here).
        transition = SearchTransition(restore=restore_search, clear=clear_search)
        # 走行中のリネーム追従ウォークは前のルートのためのもの — 結果を捨てる
        # だけでなくウォーク自体を止める。
        # getattr ガードは __init__ を通さないナビゲーションのテストハーネス向け。
        stream = getattr(self, "_rename_follow_stream", None)
        if stream is not None:
            stream.cancel()
        self._root = root
        self._sync_window_title()
        # ステージ維持（keep_mode=True・同一ルート・ステージ表示中）は
        # 中央/右ペインのブランク化より**前**に確定させる。モード分岐より先に
        # ``show_empty()`` が走ると、「ステージ維持」でも表示中の画像が
        # 空白化し、着地後の選択復元が ``_current_folder=None`` により先頭
        # （post.md / 代表画像）へリセットされる。getattr ガードは
        # __init__ を通さないナビゲーションのテストハーネス向け（下と同じ）。
        # 「表示中ファイルを保つか」（keep_position）と「最大化を保つか」
        # （keep_stage）は別の問い。分割ビューでもプレビュー列は**常時可視**
        # なので、閲覧中の画像は最大化中かどうかに関わらず同じ価値を持つ。
        # 両方を ``_ui_mode == "stage"`` 込みの 1 条件で判定すると、分割ビュー
        # での F5 / notify_library_changed だけが表示中ファイルを捨てて代表
        # 画像（先頭）へ戻る非対称になる。
        keep_position = keep_mode and previous_root == root
        keep_stage = (
            keep_position
            and getattr(self, "_center_split", None) is not None
            and getattr(self, "_ui_mode", "browse") == "stage"
        )
        if not keep_position:
            # 「再ルートは選択を捨てる」規約の実体は :meth:`_reset_preview_panes`
            # に切り出してある（全面占有オーバーレイ入場・検索着地も
            # 同じものを呼ぶ）。中央/右のブランク化は ``post_grid.set_root`` の
            # 前でよい: 新ルートのスキャンは非同期で、選択は着地後の
            # pending-select が ``folder_selected`` 経由で入れ直す。
            self._reset_preview_panes()
        elif getattr(self, "_last_previewed_file", None) is not None:
            # 位置維持: 表示中ファイルの選択位置を再スキャン越しに保持する。
            # 着地後の選択復元（``_on_folder_selected``）が startup-resume（B01）
            # と同じ pending-restore 機構で右ペインの同ファイルを再選択し、その
            # ``file_selected`` が中央の表示を再確定する（親フォルダが変わって
            # いれば ``_on_folder_selected`` 側で破棄される安全弁も同じ）。
            self._pending_restore_preview.set(self._last_previewed_file)
        self._post_grid.set_root(root, pending_select=pending_select)
        if keep_position and getattr(self, "_content", None) is not None:
            # 位置維持でも兄弟リストは再スキャンで入れ替わる — prefetch した
            # PIL ソースは捨てる（非維持側は ``_reset_preview_panes`` が同じ事を
            # 済ませている）。getattr ガードは __init__ を通さないナビゲーション
            # のテストハーネス向け。
            self._content.invalidate_image_sibling_cache()
        # Navigation always lands in the split view (candidate A): re-rooting
        # means "show me this folder's contents", i.e. the grid must be
        # visible.  getattr guard for the __init__-bypassing navigation test
        # harness (same as ``_plugin_events`` below).  例外: 同一
        # ルートの再スキャン（keep_mode=True・F5 / notify_library_changed）は
        # プレビュー最大化を維持する。再スキャンは非同期なので、着地時に現
        # 選択が消えていた場合のみ ``_on_loading_changed`` で分割へ戻す。
        #
        # 検索トランジションより**前**に畳むこと: 母集合を入れ替える復元
        # （``restore_search_state``）はグリッド側から「分割へ戻せ」を告げる
        # ので、最大化のまま入ると履歴を自分で操作した直後（戻る/進む）に
        # 取り崩し付きの出口が二重に走る。ここで先に畳んでおけば告知は
        # no-op になる（モード切替は検索状態に触らないので順序を入れ替えて
        # 問題ない）。
        if getattr(self, "_center_split", None) is not None:
            if keep_stage:
                self._stage_settle_pending = True
            else:
                self._stage_settle_pending = False
                # 離脱位置は再ルート側が既に push している。
                self._enter_browse_mode(reconcile_history=False)
        # Apply the resolved search transition (post_grid.set_root has already
        # re-scoped/re-kicked any persistent search against the new root, so
        # this runs last and wins).
        transition.apply(self._post_grid)
        # （情報パネルのメタカード / ファイル詳細のクリアは
        # ``_reset_preview_panes`` が済ませている。ステージ維持中は選択ごと保つ
        # ので触らない。）
        # Move the ナビレール's ライブラリ highlight onto the new root (same
        # getattr guard for the __init__-bypassing navigation harness).
        if getattr(self, "_nav_rail", None) is not None:
            self._nav_rail.set_current_root(root)
        # パンくずの基準列はルートで変わりうる（ZIP ドリルインの展開先は
        # そのアーカイブが境界 — :meth:`_compute_library_bases`）ので押し直す。
        # getattr ガードは __init__ を通さないナビゲーションのテストハーネス向け。
        if (
            getattr(self, "_library_roots", None) is not None
            and getattr(self, "_default_library", None) is not None
        ):
            self._refresh_library_bases()
        # Reflect the new stack depths on the ←/→ buttons.
        self._update_nav_buttons()
        # ライブラリが替わったらリネーム追従を蒔き直す（前回のウォークは
        # ``_kick_rename_follow`` が cancel する）。蒔き直さないと、ナビレールで
        # 別ライブラリへ切り替えたセッションではそのライブラリのリネームに★が
        # 追従せず、起動し直すまで孤児のまま残る。同一ルートの再スキャン
        # （F5 / notify_library_changed）と、既に歩いた基準の**配下**への移動
        # （降下 / ↑ / 戻る・進む）は前回の結果が覆っているので対象外 —
        # 全 set_root で蒔き直すと、移動のたびに新ルート配下の最大 20,000
        # フォルダの ``os.scandir`` が背景で走り、NAS では前景のスキャナと
        # 同じ共有を奪い合う。getattr ガードは __init__ を通さないハーネス向け。
        if (
            previous_root != root
            and getattr(self, "_rename_follow_stream", None) is not None
            and self._rename_follow_needed(root)
        ):
            self._kick_rename_follow()
        # 安定 API のイベント（プラグイン向け）。着地が確定したここで emit。
        # **ルートが実際に変わったときだけ**（公開契約は「グリッドのルートが
        # 変わった」— F5 / notify_library_changed の同一ルート再スキャンで
        # 飛ばすと、購読側の再構築・再スキャンが無意味に走る）。
        # getattr ガード: ナビゲーションのテストハーネスは __init__ を通さず
        # set_root を呼ぶため、未配線でも安全に no-op にする。
        plugin_events = getattr(self, "_plugin_events", None)
        if plugin_events is not None and previous_root != root:
            plugin_events.root_changed.emit(root)
        return SetRootResult.LANDED

    def _reset_preview_panes(self) -> None:
        """中央プレビュー列 + 右情報パネルを「未選択」へ落とす共通リセット。

        :meth:`set_root` が持っていた「再ルートは選択を捨てる」規約の実体を
        1 メソッドへ切り出したもの。**新しい特殊分岐ではなく既存契約の再利用**で、
        グリッド全面を占有する母集合の入れ替え（横断キュレーション一覧 / 最近
        追加されたファイル一覧 / AI 検索結果 / 平常の検索着地）も
        :meth:`_on_grid_preview_context_lost` 経由でここへ合流する
        （さもないと入場しても「現在地」がプレビューヘッダー・右パネル・
        パンくずで 3 つに割れる）。

        含むもの: 現在フォルダ / ステータスバー（パス + ファイル情報）/ 右ペインの
        タイル列 / 中央プレビュー / prefetch 済み兄弟リスト / 情報パネルの
        メタカード + ファイル詳細 / ステージヘッダー。

        含まないもの:

        * **先頭タイルの自動選択** — 横断項目は別ボリューム上に
          ありうるので、入場と同時に代表画像の読み出しを起こすと NAS で待たされる。
        * **UI モードの切替**（``_enter_browse_mode``）— それは set_root 側の
          ナビゲーション規約であって「選択を捨てる」規約ではない。
        * **ステージヘッダーのタイトルを一覧名に差し替えること** —
          一覧名はパンくずが担う。ここは :meth:`_update_stage_header` の既存の
          算出規則にそのまま任せる（未選択なので ``n/m`` 位置は消える）。
        """
        # The bottom-left status shows the currently selected / previewed
        # item's path; with nothing selected yet it falls back to the current
        # browse location (the breadcrumb can be collapsed at
        # narrow pane widths, so the status bar must still answer "where am I").
        # Reset ``_current_folder`` first so the fallback resolves to the root,
        # not the folder being left.
        self._current_folder = None
        self._set_path_status(None)
        self._last_previewed_file = None
        self._file_list.clear()
        self._content.show_empty()
        # Drop prefetched PIL sources — they're all from the old folder and the
        # provider now returns a different sibling list.
        self._content.invalidate_image_sibling_cache()
        # getattr ガードは __init__ を通さないナビゲーションのテストハーネス向け
        # （set_root 系の既存規約と同じ）。
        if getattr(self, "_info_panel", None) is not None:
            self._refresh_info_meta(None)  # ← ステージヘッダーも更新する
            self._refresh_file_detail(None)
        else:
            self._update_stage_header()

    def _on_grid_preview_context_lost(self) -> None:
        """左グリッドの選択が検索 / 一覧の母集合入れ替えで消えた.

        :attr:`PostGrid.preview_context_lost` の受け口。プレビュー列と右パネルが
        「もうグリッドに無いもの」を映し続けないよう、:meth:`_reset_preview_panes`
        へ合流させる。既に未選択なら何もしない（着地のたびに走る信号なので、
        冪等な早期 return で無駄な再描画・off-thread 読みを起こさない）。

        判定に ``_current_preview_path`` を使わないこと — あれは選択が無いとき
        現在地（``_current_folder or _root``）へフォールバックする表示用の値
        なので、リセット後も ``None`` にはならない。
        """
        if self._current_folder is None and self._last_previewed_file is None:
            return
        self._reset_preview_panes()

    def _prompt_folder_not_found(
        self, root: Path, *, bookmark: str | None = None,
    ) -> str:
        """Warn that *root* is unreachable and ask what to do next (I07).

        Returns ``"retry"`` (proceed anyway — the async scan reports the real
        failure), ``"open_other"`` (let the caller route to the folder
        picker), ``"remove_bookmark"``（ブックマーク経路のみ）, or ``"close"``
        (give up).  Split out as its own method so the callers stay readable and
        the branch is unit-testable by stubbing this one call.

        *bookmark* を渡すと「このブックマークを削除」ボタンが増える
        — 死んだブックマークをその場で片付けられる。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(t("viewer.main_window.folder_not_found"))
        box.setText(t("viewer.main_window.folder_not_found_body"))
        box.setInformativeText(str(root))
        retry_btn = box.addButton(
            t("common.action.retry"), QMessageBox.AcceptRole,
        )
        open_btn = box.addButton(
            t("viewer.main_window.folder_not_found_open_other"),
            QMessageBox.ActionRole,
        )
        remove_btn = None
        if bookmark is not None:
            remove_btn = box.addButton(
                t("viewer.main_window.folder_not_found_remove_bookmark"),
                QMessageBox.DestructiveRole,
            )
        box.addButton(t("common.action.close"), QMessageBox.RejectRole)
        box.setDefaultButton(retry_btn)
        box.exec()
        clicked = box.clickedButton()
        # 親付き QMessageBox は exec 後も親が所有し続ける — 死んだ
        # ブックマーク / パンくずを踏むたびに積み上がるので明示的に解放する
        # （deleteLater はイベントループ復帰時なので clicked の比較は安全）。
        box.deleteLater()
        if clicked is retry_btn:
            return "retry"
        if clicked is open_btn:
            return "open_other"
        if remove_btn is not None and clicked is remove_btn:
            return "remove_bookmark"
        return "close"

    def _handle_missing_folder(
        self, path: Path, *, bookmark: str | None = None,
    ) -> Literal["retry", "rerouted", "cancelled"]:
        """「フォルダが見つかりません」への共通応答.

        ルート変更系 / パンくず / ブックマーク / ↑（上の階層へ）の 4 経路が
        ここへ合流する（経路ごとに応答が割れないよう 1 本にする）。

        - ``"retry"``: 再試行が選ばれた。続行側は ``assume_exists=True`` で
          進めること — GUI スレッドで ``is_dir()`` を再実行しない。
        - ``"rerouted"``: 「別のフォルダを開く」で内側の ``_pick_root()`` が
          着地し、トレイルもハードリセット済み（同じルートを選び直した場合も）。
        - ``"cancelled"``: 何も変わっていない（閉じた / ブックマーク削除 /
          ピッカーを取り消した）。
        """
        choice = (
            self._prompt_folder_not_found(path, bookmark=bookmark)
            if bookmark is not None
            else self._prompt_folder_not_found(path)
        )
        if choice == "retry":
            return "retry"
        if choice == "open_other":
            return "rerouted" if self._pick_root() else "cancelled"
        if choice == "remove_bookmark" and bookmark is not None:
            self._remove_bookmark(bookmark)
        return "cancelled"

    def _capture_current_position(self) -> NavEntry:
        """Snapshot the whole left-pane position for the nav history.

        Bundles the root, the selected tile, the active search, and the scroll
        offset (B-13) so 戻る/進む can restore all four.  Used at every history
        push so the three call sites stay in lockstep.
        """
        return NavEntry(
            self._root,
            self._current_selection(),
            self._capture_curation_search(),
            self._post_grid.scroll_value(),
            # ビュー状態履歴（リデザイン提案3): 中央スタックのモードも位置の
            # 一部として記録する。getattr ガードは __init__ を通さない
            # ナビゲーションのテストハーネス向け（set_root 系と同じ）。
            getattr(self, "_ui_mode", "browse"),
            # 横断キュレーション一覧も「位置」の一部 —
            # 記録しないと 1 件ドリルインした時点で一覧が消え、「戻る」でも
            # 戻れない使い捨てになる。
            self._post_grid.current_curation_view(),
            # 「最近追加されたファイル」一覧も同じ理由で位置の一部。
            self._post_grid.current_recent_view(),
        )

    def _capture_curation_search(self) -> "SearchSnapshot | None":
        """Snapshot the narrowing applied ON TOP of an overlay listing.

        ``_capture_current_search`` returns ``None`` when the clear-on-navigate
        setting is off, which is right for folder positions (search persists on
        its own) but wrong for an overlay listing (横断キュレーション一覧 /
        最近追加されたファイル): leaving one tears the overlay down, so nothing
        would survive to re-narrow it on 「戻る」.  When a listing is showing we
        therefore always take the snapshot.
        """
        if (
            self._post_grid.current_curation_view() is None
            and self._post_grid.current_recent_view() is None
        ):
            return self._capture_current_search()
        snap = self._post_grid.capture_search_state()
        return snap if snap.is_active() else None

    def _capture_current_search(self) -> SearchSnapshot | None:
        """Snapshot the left pane's active search for the nav history.

        Returns ``None`` when the clear-on-navigate setting is off (search is
        left to persist across navigation, the historical behaviour) or when
        no search is currently engaged — so a plain drill-down records no
        search to manage.
        """
        if not self._state.search_clear_on_navigate:
            return None
        snap = self._post_grid.capture_search_state()
        return snap if snap.is_active() else None

    def _current_selection(self) -> Path | None:
        """The left pane's currently selected tile (folder OR file).

        Used to remember what to re-select on 戻る.  ``current_path()`` is more
        precise than ``_current_folder`` (which only tracks the folder context
        for the right pane): for a file/image surfaced by search it returns the
        file itself, so restoring it re-selects that exact item once it reappears
        in the (re-run) results.
        """
        return self._post_grid.current_path()

    # ---------------------------------------------------------------- slots

    def _on_folder_selected(self, folder: Path) -> None:
        with measure("folder_selected", str(folder)):
            # Startup resume (B01): when the previously previewed file lives in
            # this folder, select it in the right pane once the scan lands (its
            # file_selected then restores the centre preview).
            restore = self._pending_restore_preview.peek()
            # 復元対象 (restore) がいままさに中央に表示中のファイルなら
            # one-shot 消費しない — 同一ルート再スキャン（keep_mode）は 2 段
            # リビルド（fast list → metadata enrichment）が folder_selected を
            # リビルド毎に再発火するため、初回で消費すると 2 回目が post.md /
            # 先頭画像の選択へ復元を上書きしてしまう。復元中はプレビューの
            # 共通ファンネル ``_set_path_status``（``_current_preview_path`` を
            # folder へ進めてしまう）もスキップして表示中ファイルを保つ。右ペイン
            # が実際にそのファイルを選択した時点（``_on_file_selected`` が None
            # へ戻す + 同ファンネルを再駆動）か、別フォルダへ移った時点で解除。
            # B01 の起動時復元（中央はまだ未表示）は one-shot。
            keep_preview = (
                restore is not None
                and self._current_preview_path == restore
            )
            if restore is not None and restore.parent != folder:
                restore = None
                keep_preview = False
            if not keep_preview:
                self._pending_restore_preview.clear()
            # The first folder selection landing marks the end of the startup
            # resume window (B01/B02): from here on _collect_state may persist
            # the live resume fields again.
            self._startup_restore_pending = False
            # 上の one-shot は「右ペインが対象を選んだ時点」で解除
            # されるが、同一ルート再スキャンの 2 段リビルド（fast list →
            # metadata enrichment）はそのあとにも folder_selected を再発火する。
            # その再発火は restore を消費済みで keep_preview=False になり、下の
            # 代表画像差し替え（``_show_preview_file``）が閲覧中の画像を先頭へ
            # 奪い返す（速い機械では復元が後着で隠れ、遅い機械で表に出る）。
            # 選択が変わっていない = 再発火なので、中央がこのフォルダ内のファイル
            # を表示しているなら奪わない。右ペインの選択位置もその表示中ファイル
            # で選び直す（``restore`` が None のままだと下の早期 return が
            # pending_select 無しでファイル一覧を組み直し、選択が落ちる）。
            refired = folder == self._current_folder
            if not keep_preview and refired:
                current = self._current_preview_path
                if (
                    current is not None
                    and current != folder
                    and current.parent == folder
                ):
                    keep_preview = True
                    restore = current
            # 同一フォルダ再発火の分類。左グリッドの
            # リビルド（メタデータ 2 段・#thumb# トグル・検索 teardown 復元
            # 等）は pending-select の適用（``is_resolving_pending_select``）
            # として選択を emit し直すだけで内容は変わっていない — 右ペインの
            # 再スキャン（``set_folder`` → ``_scanner.request`` = NAS への
            # scandir + stat）を抑止し、選択の入れ直しだけ行う。F5 /
            # ``notify_library_changed`` は「内容が変わったかもしれない」明示
            # リロードで、その鮮度はこの refire 再スキャンに依存するため、
            # one-shot（``_pending_content_rescan``）が抑止を 1 回だけ解除
            # する。ユーザーのクリック / キー操作による選択替えは pending-
            # select 経由ではない（フラグが立たない）ので常に再スキャン側。
            content_rescan = self._pending_content_rescan.consume(always)
            pure_ui_refire = (
                refired
                and not content_rescan
                and self._post_grid.is_resolving_pending_select()
            )
            if not refired:
                # Cross-folder move: prefetched PIL images from the previous
                # folder will never be reached by arrow-key navigation again,
                # so reclaim their budget for the new folder's prefetch fill.
                self._content.invalidate_image_sibling_cache()
            self._current_folder = folder
            if not keep_preview:
                self._set_path_status(folder)
            # クリック意味の一様化（2026-07 分割ビュー再設計・共通コア）:
            # フォルダのシングルクリックは post.md の有無・混在・空を問わず
            # 常に「代表画像プレビュー」。post.md は右の情報パネルにメタ
            # カードを**追加表示**するだけ（有無判定は _refresh_info_meta が
            # 担う）。本文はメタカードの「本文を読む」リンクかファイル一覧の
            # post.md 行から 1 クリック。
            # 役割再定義 2026-07-20: フォルダ選択はフォルダの「詳細」= 投稿メタ
            # （メタカード）を出す文脈なので、直前のファイル詳細カードは畳む。
            #
            # ただし**選択が変わらない純 UI 再発火**は畳まない:
            # 右ペインの選択が既に ``restore`` に居るなら、「畳む → 再選択の
            # ``file_selected`` で戻す」往復そのものが不要で、往復に頼ると
            # どちらへ転んでも壊れる — 同じ項目が選択済みだと
            # ``GalleryView.select_index`` は emit を省くので明示選択の
            # カードだけが消える（lost-update）し、逆に emit を強制すると
            # 代表画像の**自動**選択（one-shot 抑止 ``_auto_select_no_detail``
            # は初回 emit で消費済み）が ``_on_file_selected`` で明示選択と
            # 区別できず、メタカードがファイルカードに置き換わる。選択が
            # 変わらないなら右ペイン・詳細カード・中央プレビューは no-op が正
            # （中央の再デコードもプローブ世代のバンプも起きない）。
            # ``reselect_same_folder(pending_select=None)`` は「表示中フォルダ
            # が一致しスキャンも健全」の判定だけで選択に触れない。
            selection_unchanged = (
                pure_ui_refire
                and keep_preview
                and self._file_list.current_path() == restore
                and self._file_list.reselect_same_folder(
                    folder, pending_select=None
                )
            )
            if not selection_unchanged:
                self._refresh_file_detail(None)
            # 上の再スキャン抑止の対: 純 UI 再発火（左グリッドのリビルド由来）で
            # 同じフォルダのメタが既に解決済みなら、カードを消して post.md を
            # 読み直さない。無条件だと再発火のたびにカードが消え、ステージ
            # ヘッダーのタイトルが「投稿タイトル → フォルダ名 → 投稿タイトル」
            # と往復し、冷えた共有では余分な NAS I/O になる。F5 /
            # notify_library_changed は明示リロードで pure_ui_refire にならない
            # （``_pending_content_rescan`` の one-shot）ので鮮度は落ちない。
            meta_known = (
                pure_ui_refire
                and self._info_meta_last is not None
                and self._info_meta_last[0] == folder
            )
            if meta_known:
                self._apply_meta_card()
                self._update_stage_header()
            else:
                self._refresh_info_meta(folder)
            # keep_preview の間は代表画像への差し替えを行わない — 最大化維持の
            # 再スキャンで表示中の画像が先頭へ戻るのを防ぐ。右ペインの
            # 再選択（pending_select=restore）の ``file_selected`` が同じ表示を
            # 再確定する。
            if keep_preview:
                if selection_unchanged:
                    return
                with measure("folder_selected_list", str(folder)):
                    # 純 UI リビルド再発火は再スキャンせず選択適用のみ。
                    # スキャン失敗中などで断られたら再スキャンへ
                    # フォールバック。
                    if not (
                        pure_ui_refire
                        and self._file_list.reselect_same_folder(
                            folder, pending_select=restore
                        )
                    ):
                        self._file_list.set_folder(
                            folder, pending_select=restore
                        )
                return
            with measure("folder_selected_show_image", str(folder)):
                # Fast path: the left pane usually already resolved a
                # representative image for this folder (metadata pass /
                # preview cache) — reuse it instead of re-walking the
                # folder here on the GUI thread (``find_first_image`` is up
                # to ~33 scandir calls, seconds on NAS).  A non-marker hint
                # matches the probe's own preference and is final; a
                # ``#thumb#`` marker (exclude-toggle OFF) is shown as an
                # interim while the async probe looks for a non-marker
                # image, preserving the historical preference.
                preview_image = self._grid_preview_hint(folder)
                if preview_image is not None:
                    self._show_preview_file(preview_image)
                else:
                    # フォルダは選択済み — 「グリッドから選んでください」の
                    # 未選択ヒント（``show_empty``）はここでは自己矛盾する
                    # 案内になる（画像が BFS 深さ上限より深いフォルダで
                    # 起きる）。状態即応の静音
                    # プレースホルダ「プレビューする項目がありません」を出す。
                    self._content.show_empty_quiet()
                hint_is_final = (
                    preview_image is not None
                    and not preview_image.name.lower().startswith(
                        THUMB_MARKER_PREFIX
                    )
                )
                if not hint_is_final:
                    self._start_first_image_probe(folder, preview_image)
            with measure("folder_selected_list", str(folder)):
                if restore is None and preview_image is not None:
                    # 代表画像の自動選択でファイル詳細カードがメタカードを
                    # 置き換えないための one-shot 抑止（メタは「追加表示」）。
                    self._auto_select_no_detail = preview_image
                # 純 UI リビルド再発火は再スキャンせず選択適用のみ。
                # 断られたら再スキャンへフォールバック。
                if not (
                    pure_ui_refire
                    and self._file_list.reselect_same_folder(
                        folder, pending_select=restore or preview_image
                    )
                ):
                    self._file_list.set_folder(
                        folder, pending_select=restore or preview_image
                    )

    def _grid_preview_hint(self, folder: Path) -> Path | None:
        """The left pane's already-resolved preview image for *folder*, if any.

        Returns ``None`` when the grid hasn't resolved the folder yet (cold
        cache) or the resolution isn't an image/PDF (``find_first_image``'s
        domain).  May be a ``#thumb#`` marker — the caller decides whether
        that is final or just an interim.
        """
        entry = self._post_grid.entry_for(folder)
        if entry is None or not entry.is_dir:
            return None
        if not entry.thumbnail_resolved or entry.thumbnail_path is None:
            return None
        if entry.thumbnail_path.suffix.lower() not in (IMAGE_SUFFIXES | PDF_SUFFIXES):
            return None
        return entry.thumbnail_path

    def _show_preview_file(self, path: Path) -> None:
        """Render a folder's representative file in the centre pane by kind.

        The representative comes from ``_grid_preview_hint`` / the first-image
        probe, whose domain is images **and** PDFs (``find_first_image`` treats
        PDFs as thumbnailable).  Sending a PDF to ``show_image`` produces a
        broken preview because ImageView can't decode it, so route by
        suffix: PDFs go to ``show_pdf``, everything else (always an image here)
        to ``show_image``.  A dedicated dispatch — rather than the more general
        ``ContentView.show_path`` — keeps this GUI-thread hot path free of the
        ``is_dir()`` stat ``show_path`` does for a known-file preview.
        """
        # 「ウィンドウが選んだ」代表画像 — デコード失敗時に次候補へ譲る対象を
        # 覚えておく（ユーザーの明示選択と区別する）。
        self._preview_fallback.shown = path
        # ステータスバーのファイル情報セグメントを「代表: 名前 · サイズ」で
        # 埋める。選択はフォルダのままなので ``_set_path_status`` の
        # 経路では空になる — 代表画像の表示はこのメソッドが唯一の funnel。
        # hasattr ガードは __init__ を通さないテストハーネス向け（既存規約）。
        if hasattr(self, "_file_info_stream"):
            self._update_file_info_label(path, representative=True)
        if path.suffix.lower() in PDF_SUFFIXES:
            self._content.show_pdf(path)
        else:
            self._content.show_image(path)

    def _start_first_image_probe(
        self,
        folder: Path,
        hint: Path | None,
        *,
        skip: frozenset[Path] = frozenset(),
    ) -> None:
        self._preview_probe_folder = folder
        self._preview_probe_hint = hint
        if not skip:
            # skip 無し = フォールバックではない通常のプローブ（フォルダ
            # 切替など）。前フォルダの失敗記録と探索中フラグを引き継がない。
            self._preview_fallback.restart(folder)
            self._preview_fallback_pending = False
        # 投入は **superseding**（``submit_job``）: キュー済みの先行プローブを
        # 捨て、走行中の 1 本にはセッションの cancel が届く。プールは 1 スレッド
        # なので、積み増しにすると「もう誰も見ていないフォルダ」の先頭 scandir を
        # 1 件ずつ払い終えるまで最新のプローブが**開始すらしない** — 半死の共有
        # ではそれが 1 件あたり数十秒になる。走行中の 1 本は Qt に止める API が
        # 無いので、そちらは協調的に降りる（``_find_images_bfs`` が先頭の
        # scandir の前に ``job.cancel`` を見る）。
        self._preview_stream.submit_job(
            lambda job, f=folder, s=skip: next_representative(
                f, s, job.cancel.is_cancelled,
            )
        )

    def _on_preview_image_failed(self, path: Path) -> None:
        """代表画像がデコードできなかった — 同フォルダの次候補へ逃がす.

        探索はデコード可能性を見ないので、何もしなければ先頭が壊れているだけの
        フォルダは選んだ瞬間にエラーカードになる（起動時の先頭フォルダ自動選択
        では一発目の画面）。規則（ウィンドウ自身が選んだ代表だけ・上限あり）は
        :class:`~.representative_fallback.RepresentativeFallback` が決める。
        全候補が壊れていれば ``_on_first_image_found`` が ``None`` で何もせず、
        最後に出たエラーカードがそのまま残る。
        """
        folder = self._current_folder
        skip = self._preview_fallback.on_failed(folder, path)
        if skip is None or folder is None:
            return
        # 次候補を探している **最中** の「画像なし」は過渡状態でしかない
        # ので、解像度ラベルをそこで消さない（消すと候補を試すたびに
        # W×H が明滅し、全滅時以外は必ず復帰する = 意味のないちらつき）。
        # 着地（`_on_first_image_found`）でフラグを落とし、そこで初めて
        # 「本当に何も出せない」ときのクリアが効く。
        self._preview_fallback_pending = True
        self._start_first_image_probe(folder, None, skip=skip)

    def _on_first_image_found(self, preview: object) -> None:
        if self._preview_probe_folder != self._current_folder:
            return
        path = preview if isinstance(preview, Path) else None
        # フォールバック探索はここで終わる（採用でも打ち切りでも）。以後の
        # 「画像なし」は過渡状態ではないのでラベルのクリアを再び通す。
        # 打ち切り時にラベルを能動的にクリアはしない — 候補を試している間の
        # (0,0) は「次を試す前の一瞬」でしかなく、それを根拠に表示中の
        # 解像度を消すのは余計なちらつきだから（最後に出るのは
        # エラーカードで、解像度欄は次のプレビューで更新される）。
        self._preview_fallback_pending = False
        if path is None or path == self._preview_probe_hint:
            # Nothing better than what's already shown (the interim hint, or
            # the empty page when the folder truly has no preview image).
            return
        # Route by suffix — the probe can surface a PDF (find_first_image
        # treats PDFs as thumbnailable), which ImageView cannot decode.
        self._show_preview_file(path)
        # 代表画像の自動選択（フォルダ選択の続き）— ファイル詳細カードで
        # メタカードを潰さない（_on_folder_selected と同じ one-shot 抑止）。
        self._auto_select_no_detail = path
        self._file_list.set_pending_select(path)

    def _on_folder_activated(self, folder: Path) -> None:
        # 「開く」の一様化（2026-07 分割ビュー再設計・共通コア）: フォルダの
        # ダブルクリック / Enter は post.md の有無を問わず常に**ドリルダウン**
        # （Explorer と同じ心理モデル — 「post.md 有 → ステージ」の分岐は
        # 持たない）。「大きく見る」は E / プレビューのダブルクリックが担う。
        self.set_root(folder, push_history=True)

    def reveal_in_app(self, path: Path) -> None:
        """「このファイルの場所を開く」の**公開口**（席から祖先経由で引かれる）.

        自前の配線を持たない葉のビュー（プレビュー列の各面 / 全画面の画像
        ビュー）は :func:`~.context_menus.reveal_hook_from_ancestors` で祖先の
        窓からこの名前を探す。左右 2 ペインだけが自分のシグナルで届けている
        ので、この口が無いとプレビュー列と全画面には動詞が**出ない** — 実体が
        どこにあるか名前でしか示せない横断一覧・検索ヒットで、まさにそこから
        戻りたい面が抜ける。
        """
        self._on_reveal_in_app(path)

    def _on_reveal_in_app(self, path: Path) -> None:
        """右クリック「このファイルの場所を開く」の着地.

        横断一覧・検索ヒットの行は「どこにあるか」を名前でしか示せないので、
        実体の置き場所へアプリ内で行く手段を持たせる。親フォルダを
        ルートにして当の項目を選ぶ — 左グリッドのシングルクリック着地と同じ形。
        ``push_history=True`` は必須（履歴を消さない着地）。``is_dir`` ゲートは置かない: ``set_root`` 自身の単発ゲート
        と「見つかりません」プロンプトに合流させる（``_on_file_list_folder_
        activated`` と同じ裁定）。
        """
        self.set_root(path.parent, push_history=True, pending_select=path)

    def _on_file_list_folder_selected(self, folder: Path) -> None:
        # Right-pane single-click / scroll-driven selection-change on a
        # sub-folder: render a non-navigating preview (thumbnail + child
        # tiles) in the centre.  Re-rooting is reserved for double-click
        # (``folder_activated`` → ``_on_file_list_folder_activated``) so
        # scroll / arrow-key selection doesn't trigger a navigation.
        #
        # Pass the right-pane row's already-rendered icon as a placeholder
        # so the centre has something to show immediately — the worker
        # threads then replace it with the full-resolution decode.
        #
        # ユーザー起点のサブフォルダ選択も、実行中の代表画像プローブを失効
        # させる: このハンドラは ``_current_folder``
        # を変えないため、stale なプローブ着地がこのフォルダプレビューを親
        # フォルダの先頭画像で上書きしてしまう。失効そのものは下の
        # ``_set_path_status`` が落とす（世代バンプは funnel 一本）。
        placeholder = self._file_list.current_icon()
        self._set_path_status(folder)
        self._content.show_folder(folder, placeholder_icon=placeholder)
        # サブフォルダ選択 = 中央はフォルダプレビュー — ファイル詳細カードは畳む。
        self._refresh_file_detail(None)
        # 画像トラックのハイライトはフォルダ行の選択にも追従（ブラウズ中 no-op）。
        self._sync_image_strip_current()

    def _on_file_list_folder_activated(self, folder: Path) -> None:
        # 右一覧のフォルダをダブルクリック = **そのフォルダへドリルダウン**。
        #
        # ``set_root(folder.parent, pending_select=folder)`` で「左ペインの
        # シングルクリックと同じ着地」にすると、**同じジェスチャ（ダブル
        # クリック）が左右で別の階層に着地する**説明できない差になるので、
        # 左グリッドの ``_on_folder_activated`` と同じ「開く = ドリルダウン」
        # へ揃える。
        #
        # 事前の同期 ``is_dir`` ゲートは置かない: ``set_root`` 自身の単発
        # ゲート + I07「見つかりません」プロンプトに合流させる（同じ stat の
        # 二重化 + 失敗時の無言 return を作らない）。
        self.set_root(folder, push_history=True)

    def _on_grid_file_selected(self, path: Path) -> None:
        # A file was selected in the left pane.  When the file is a
        # descendant surfaced by recursive / advanced search (its parent
        # sits below the current root), keep the search results in the left
        # pane but open its containing folder in the right pane — so the
        # user can browse the folder's contents without losing the search
        # (re-rooting to the folder is reserved for double-click, handled
        # by ``_on_grid_file_activated``).  Files sitting directly under the
        # current root keep the existing behaviour: clear the per-folder
        # right pane and just render the file in the centre preview.
        #
        # ユーザー起点の明示選択は実行中の代表画像プローブを失効させる:
        # このハンドラは
        # ``_current_folder = path.parent`` を立てるので、直前に同じフォルダで
        # 走り出したプローブの 2 ガード（世代・フォルダ一致）を両方通過して
        # しまい、着地が中央プレビューと右ペイン選択だけを先頭画像へ奪い返す
        # （``_current_preview_path`` は選んだファイルのまま = 三者不整合）。
        # 失効は下の 2 分岐がどちらも通る ``_set_path_status`` が落とす
        # （世代バンプは funnel 一本）。
        self._last_previewed_file = path  # startup-resume capture (B01)
        self._pending_restore_preview.clear()
        # 右パネル = 選択ファイルの詳細表示エリア（役割再定義 2026-07-20）。
        self._refresh_file_detail(path)
        # ``parent.is_dir()`` の同期 stat は置かない:
        # このハンドラは矢印キーで検索結果を歩くたびに走るホットパスで、
        # コールド NAS では選択移動ごとに GUI が数百 ms 止まる
        # （``_stat_file_label`` を off-thread 化したのと同じ理由）。消失時は
        # ``set_folder`` の非同期スキャン失敗（エラーカード）に委ねる。
        parent = path.parent
        if parent != self._root:
            self._current_folder = parent
            self._set_path_status(path)
            self._refresh_info_meta(parent)
            self._content.show_path(path)
            self._file_list.set_folder(parent, pending_select=path)
            return
        # A file sitting directly under the current root: populate the right
        # pane with the root's own file list and select this file there, so it
        # behaves like the descendant case above (the right pane used to go
        # blank, leaving no way to browse sibling files without re-rooting).
        self._current_folder = self._root
        self._set_path_status(path)
        self._refresh_info_meta(self._root)
        self._content.show_path(path)
        self._file_list.set_folder(self._root, pending_select=path)

    def _on_grid_file_activated(self, path: Path) -> None:
        # Double-click / Enter on a file in the left pane.  Media files
        # (images / videos — direct children AND search-surfaced descendants)
        # MAXIMISE the preview: the first click of the double-click already
        # routed the selection (``_on_grid_file_selected``), so the preview
        # column shows the file and the right pane its containing folder —
        # the activation just flips the split to the maximised preset.
        # 閲覧モード (fullscreen lightbox) stays reachable via F11 / the 表示
        # menu only (the single fullscreen entrance).  Non-media
        # descendants keep the historical "drill into the containing folder"
        # semantics; direct-child ZIPs drill in; unpreviewable kinds launch
        # the OS default app; every other previewable kind stages in place.
        #
        # 右一覧の ``_on_file_activated`` と同じ 2 点: ユーザーが明示的に
        # 開いた物は代表画像マークを持たない、かつ実行中の代表画像プローブを
        # 失効させる — バンプが
        # 無いと stale な着地が開いたファイルを先頭画像へ奪い返す。
        # **ここのバンプは funnel（``_set_path_status``）へ寄せられない**:
        # 下の分岐は ``_current_preview_path == path`` のとき
        # funnel を呼ばず、ZIP ドリルイン / 既定アプリ起動の枝はそもそも
        # 通らないため、手書きのまま残す（重複ではなく、funnel の穴埋め）。
        self._preview_fallback.shown = None
        self._preview_stream.cancel()
        if path.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            if self._current_preview_path != path:
                self._set_path_status(path)
                self._content.show_path(path)
            self._enter_stage_mode()
            return
        parent = path.parent
        if parent != self._root:
            # 事前の同期 ``is_dir`` は行わない —
            # 消失時は set_root の単発ゲートが I07 プロンプトで応答する。
            self.set_root(parent, push_history=True, pending_select=path)
            return
        if path.suffix.lower() in ZIP_DRILL_SUFFIXES:
            self._open_zip_as_folder(path)
            return
        # A file the viewer has no dedicated preview for (PDF is previewed, but
        # e.g. .docx / .psd / .exe are not): double-click means "open" in
        # Explorer terms, so launch the OS default app rather than re-showing
        # the same bare info card single-click already displays.
        if not has_dedicated_view(path):
            self._open_with_default_app(path)
            return
        self._content.show_path(path)
        self._enter_stage_mode()

    def _on_go_back(self, *, assume_exists: bool = False) -> bool:
        """「戻る」を 1 ホップ。*assume_exists* は :meth:`set_root` へ渡す.

        既定は「行き先を 2 秒プローブで確かめる」。一括ジャンプの**中間**
        ホップだけが真を渡す（``_navigate_history_steps``）。
        """
        if not self._history:
            return False
        entry = self._history.pop()
        # Snapshot BEFORE navigating: on success it becomes the 進む entry;
        # capture must run while this window still shows the position we're
        # leaving.
        snapshot = self._capture_current_position()
        result = self._navigate_history(entry, assume_exists=assume_exists)
        if not result:
            # The destination vanished (deleted / offline NAS) — set_root
            # warned and changed nothing, so put the entry back where it was
            # (the trail is preserved; the user can retry once the share is
            # back) and just re-sync the ←/→ buttons.
            self._restore_failed_history_entry(self._history, entry, result)
            return False
        # Remember where we were so 進む can re-do this hop, capturing its
        # selection + search + scroll too.  set_root with push_history=False
        # leaves both stacks alone apart from this push.
        self._forward.append(snapshot)
        self._update_nav_buttons()
        return True

    def _on_go_forward(self, *, assume_exists: bool = False) -> bool:
        """:meth:`_on_go_back` の対（*assume_exists* の意味も同じ）."""
        if not self._forward:
            return False
        entry = self._forward.pop()
        snapshot = self._capture_current_position()
        result = self._navigate_history(entry, assume_exists=assume_exists)
        if not result:
            # Mirror of _on_go_back's failure path: restore the entry, keep
            # the trail intact, re-sync the buttons.
            self._restore_failed_history_entry(self._forward, entry, result)
            return False
        # Mirror of _on_go_back: the current position rejoins the back stack so
        # 戻る works again after a 進む.
        self._history.append(snapshot)
        self._update_nav_buttons()
        return True

    def _restore_failed_history_entry(
        self, stack: "list[NavEntry]", entry: NavEntry,
        result: SetRootResult,
    ) -> None:
        """戻る/進むが失敗したときに pop したエントリを積み直す。

        ただし**何も変わっていないとき（``UNCHANGED``）だけ**。もう 1 つの失敗 ``REROUTED`` は「I07 プロンプト
        の『別のフォルダを開く』が内側で ``_on_root_change_requested`` を完走
        し、**別ルートへ遷移したうえでトレイルをハードリセットした**」。後者で
        積み直すと、ユーザーが明示的に開き直した直後のはずの ← が有効なまま
        残り、押すと同じ「フォルダが見つかりません」プロンプトへ何度でも落ちる。
        積み直さない = ``_on_root_change_requested`` の「ルート変更はトレイルの
        ハードリセット」という約束を守る。
        """
        if result is SetRootResult.UNCHANGED:
            stack.append(entry)
        self._update_nav_buttons()

    def _navigate_history(
        self, entry: NavEntry, *, assume_exists: bool = False,
    ) -> SetRootResult:
        """Move to a remembered position (back / forward), restoring its
        selected sub-folder and search state.  Returns ``set_root``'s verdict
        — ``UNCHANGED`` means nothing changed (vanished destination),
        ``REROUTED`` means the I07 プロンプト が内側で別ルートを開いた。

        *assume_exists* は ``set_root`` の同期ゲート（2 秒の
        ``probe_path_kind``）を飛ばす。一括ジャンプの**中間**
        ホップだけが真 — 通過点でしかない位置に存在確認を撃つと、到達不能な
        共有では 2 秒 × n の積算フリーズと、n 本の張り付いた "path-probe"
        デーモンスレッドになる（早期 break は成立しない: タイムアウトは
        「ディレクトリとみなす」規約なので ``set_root`` は True を返す）。
        """
        # ビュー状態履歴（リデザイン提案3): 同一ルートでモード（分割 ⇄
        # プレビュー最大化）だけ違う位置へのホップはモード切替 + 選択/
        # スクロール復元で完結させる（set_root の同一ルート再スキャンを
        # 踏まない fast path — 最大化直後の「←」= 分割へ戻る、と「→」での
        # 再最大化がここに乗る）。検索状態が一致するときだけ — 統合ツール
        # バーはウィンドウレベルなので最大化中も検索は変えられる。違って
        # いれば下のフル復元経路（set_root + restore_search）で検索ごと
        # 復元する。異なるルートへのホップは従来どおり set_root（分割着地）。
        # getattr ガードは __init__ を通さないナビゲーションのテストハーネス
        # 向け。
        same_root = entry.root == self._root
        if (
            getattr(self, "_center_split", None) is not None
            and same_root
            and entry.mode != self._ui_mode
            # 比較は「積んだ時と同じ捉え方」で行う —
            # エントリ側は ``_capture_curation_search`` で積まれるため、一覧の
            # 上に絞り込みが載っていると常にスナップショットを持つ。現在側を
            # ``_capture_current_search`` で取ると「移動時に検索を解除」OFF で
            # 必ず None になり、何も変わっていなくても不一致 → フル復元（＝
            # 一覧のツリー全再走査）へ落ちる。
            and entry.search == self._capture_curation_search()
            # 横断一覧の出入りは母集合ごと入れ替わるので、モード切替だけの
            # fast path には乗せない。
            and entry.curation == self._post_grid.current_curation_view()
            and entry.recent == self._post_grid.current_recent_view()
        ):
            if entry.mode == "browse":
                # 最大化中に Ctrl+←/→ で投稿を送ってから「←」で抜けると、
                # 履歴エントリが最大化に入った時点の選択を持っているため選択
                # だけが巻き戻る（G / Esc / ヘッダーの出口は現選択を保つので、
                # 3 出口のうち 1 つだけ挙動が違ってしまう）。
                # 離脱時の現選択でエントリの selected を上書きして揃える。
                current = self._post_grid.current_path()
                target = current if current is not None else entry.selected
                # 遷移先エントリは既に pop 済み。
                self._enter_browse_mode(reconcile_history=False)
                if target is not None:
                    self._post_grid.set_pending_select(target)
                # スクロール位置は書かない — 畳まれた席のアンカー（見ていた
                # 位置、または最大化中に動いた選択）を分割復帰のリレイアウトが
                # 適用する。G / Esc / ヘッダーの出口と同じ 1 本の経路。
            else:
                if (
                    entry.selected is not None
                    and self._post_grid.current_path() != entry.selected
                ):
                    self._post_grid.select_path(entry.selected)
                self._enter_stage_mode(push_history=False)
            return SetRootResult.LANDED
        # When the entry carried an active search, restore it.  When it didn't
        # (and clear-on-navigate is on), clear so a search restored at an
        # adjacent position doesn't bleed onto this one.  With the setting off,
        # neither runs and search persists.
        clear = self._state.search_clear_on_navigate and entry.search is None
        # 横断キュレーション一覧の位置: 一覧は
        # ``enter_curation_view`` が入口で検索状態を落としてから母集合を組む
        # ため、set_root では検索を復元させず（clear）、入場後に改めて
        # 絞り込みを載せ直す。こうしないと復元した絞り込みが入場で消える。
        # 「最近追加されたファイル」一覧の位置も同型（入場が検索状態を落とすので
        # set_root では復元させず、入場後に絞り込みを載せ直す）。
        restoring_curation = entry.curation is not None
        restoring_recent = entry.recent is not None
        restoring_overlay = restoring_curation or restoring_recent
        landed = self.set_root(
            entry.root,
            push_history=False,
            pending_select=entry.selected,
            restore_search=None if restoring_overlay else entry.search,
            clear_search=True if restoring_overlay else clear,
            assume_exists=assume_exists,
        )
        if not landed:
            return landed
        if restoring_overlay:
            if restoring_curation:
                self._post_grid.enter_curation_view(entry.curation)
            else:
                self._post_grid.enter_recent_files_view(entry.recent)
            if entry.search is not None:
                self._post_grid.restore_search_state(entry.search)
            # 履歴の戻り / 進みは母集合を変えない — 現在地だけ。
            self._sync_nav_rail_curation(counts=False)
        # Restore the remembered scroll offset (B-13).  Queued AFTER set_root so
        # it isn't reset by the fresh scan; the pane applies it once its tiles
        # are laid out (and, for a search restore, once the async results land).
        self._post_grid.set_pending_scroll(entry.scroll)
        # 同一ルートの最大化位置が検索違いでフル復元に落ちてきた場合（例:
        # 最大化中に検索を変えて戻った後の「→」）: モードも位置の一部なので
        # 最大化まで復元する。異なるルートは従来どおり分割着地のまま。
        if (
            same_root
            and entry.mode == "stage"
            and getattr(self, "_center_split", None) is not None
        ):
            self._enter_stage_mode(push_history=False)
        return SetRootResult.LANDED

    # ------------------------------------------------- history dropdown (B-6)

    def _install_history_menus(self) -> None:
        """Build + attach the long-press history dropdowns on the ←/→ buttons.

        The menus live in ``nav_history.py::HistoryMenus`` and are populated
        lazily on ``aboutToShow`` from the two stacks (read through the
        callables), so they always reflect the current history without
        per-navigation rebuilds.  The buttons keep their normal short-click
        navigate behaviour (DelayedPopup); only a long press opens these.
        """
        self._history_menus = HistoryMenus(
            self,
            back_stack=lambda: self._history,
            forward_stack=lambda: self._forward,
            entry_label=self._history_entry_label,
            navigate_steps=self._navigate_history_steps,
        )
        self._post_grid.attach_history_menus(
            self._history_menus.back_menu, self._history_menus.forward_menu
        )

    def _history_root_name(self, root: Path) -> str:
        """The friendly display name for a history entry's root folder.

        A ZIP-extracted temp root shows the friendly archive name (from
        ``_zip_temp_dirs``) rather than a cryptic ``tmpXXXX`` path; the default
        library shows the Japanese 「ライブラリ」 label (the breadcrumb /
        status bar already localise it, so the dropdown must not leak the raw
        English folder name).
        """
        return self._location_label(root)

    def _history_entry_label(self, entry: NavEntry) -> tuple[str, str]:
        """Display name + tooltip for a history entry.

        The root's name alone is not enough: the dropdown would fill with
        runs of identical rows — 「最大化」 pushes the
        browse position it left (``_enter_stage_mode``), and the two 横断一覧 /
        「最近追加されたファイル」 views are whole-grid overlays *on* a root, so
        the same folder name legitimately appears several times in a row with
        nothing to tell the entries apart.  ``NavEntry`` already carries every
        distinguishing field; this renders them:

        * ``curation`` / ``recent`` — the **view**'s own name (those positions
          are not "that folder" at all).
        * ``selected`` — 「〈ルート〉 — 〈選択〉」.
        * ``mode == "stage"`` — 「〈…〉（最大化）」.
        * ``search`` — a suffix naming the filter text (or a generic
          「絞り込み中」 when the narrowing came from a non-text predicate).

        Tooltips carry the real path (plus the selected item's, when there is
        one), so the elided menu row is always recoverable on hover.
        """
        root = entry.root
        base = self._history_root_name(root)
        tip = str(root)
        if entry.curation:
            # 一覧名の単一情報源（``PostGrid.curation_view_label``）を通す。
            # 手書きの 2 値分岐だとユーザータグ横断一覧
            # （``"tag:<名前>"``）が全部「あとで見る一覧」に丸められる。
            base = self._post_grid.curation_view_label(entry.curation)
        elif entry.recent is not None:
            base = t(
                "viewer.main_window.history_recent_label",
                name=entry.recent.name or base,
            )
            tip = str(entry.recent)
        elif entry.selected is not None:
            base = t(
                "viewer.main_window.history_selected_label",
                name=base, selected=entry.selected.name,
            )
            tip = str(entry.selected)
        if entry.mode == "stage":
            base = t("viewer.main_window.history_stage_label", name=base)
        snapshot = entry.search
        if snapshot is not None and snapshot.is_active():
            text = snapshot.filter_text.strip()
            base = (
                t("viewer.main_window.history_search_label", name=base, text=text)
                if text
                else t("viewer.main_window.history_filtered_label", name=base)
            )
        return base, tip

    def _navigate_history_steps(self, n: int, *, forward: bool) -> None:
        """Jump *n* steps back (or forward) at once (B-6 dropdown pick).

        Implemented as *n* single-step hops through the existing
        ``_on_go_back`` / ``_on_go_forward`` so every invariant those maintain
        (opposite-stack push, search clear/restore, scroll restore, button
        sync) holds exactly — a bulk jump is just their repetition.  A failed
        hop (vanished destination — the step already warned and restored the
        stacks) aborts the remaining hops instead of re-hitting the same dead
        entry *n* more times.

        **存在確認は最終到達点だけ**。
        ``set_root`` は先頭で ``path_probe.probe_path_kind``（既定 2 秒）を
        撃つが、到達不能な共有ではそれが毎回タイムアウトし、規約どおり
        「ディレクトリとみなして続行」するので ``set_root`` は True を返す —
        つまり早期 break は**一度も成立せず**、15 件ジャンプは 2 秒 × 15 =
        最大 30 秒 GUI を止め、``probe_path_kind`` が join 後も止められない
        デーモンスレッドを 15 本残す。中間ホップは通過点でしかないので、
        ↑Up / パンくず祖先クリック / ドロップ / ブックマークが既に持つ
        「判定は 1 回きり → 以後 ``assume_exists=True``」の規約へ揃える
        （実在確認は非同期スキャンのエラーカード + [再試行] に委ねる）。
        """
        step = self._on_go_forward if forward else self._on_go_back
        for i in range(n):
            if not step(assume_exists=(i < n - 1)):
                break

    def _on_go_up(self) -> None:
        # ライブラリ境界で止める（ボタン / Alt+Up の
        # 無効化だけでなくハンドラ側でも守る — グリッドの Backspace や
        # 空フォルダカードの「上の階層へ」も同じここへ集まるため）。
        if not self._can_go_up():
            return
        parent = self._root.parent
        if parent == self._root:
            return
        current = self._root
        # バウンデッドなゲート — 素の ``is_dir`` は死んだ共有で GUI を
        # 数十秒止める。``set_root`` の同期ゲートと同じ判定を使う。
        if not self._is_reachable_dir(parent):
            # 無言 return にしない（空フォルダカードの主要導線でもあるのに
            # 何も起きなくなる）。他の 3 経路と同じ
            # 「見つかりません」応答へ合流する。
            if self._handle_missing_folder(parent) != "retry":
                return
        # どちらの枝も「進む」と決めた後なので ``set_root`` に判定をやり直させ
        # ない: 渡さないと同じパスを 2 回プローブすることになり、
        # 死んだ共有ではフリーズが 2 倍、起きかけの共有では 1 回目と 2 回目で
        # 答えが変わって「見つかりません」へ落ちる。
        self.set_root(
            parent,
            push_history=True,
            pending_select=current,
            assume_exists=True,
        )

    def _on_breadcrumb_navigate(self, path: Path) -> None:
        # A breadcrumb ancestor segment was clicked.  Jump to it exactly like
        # ↑Up: push the current position onto the back stack and clear the
        # forward stack (a fresh jump branches the trail).  A vanished path
        # (deleted / renamed between render and click) warns and is ignored.
        # ↑Up と同じバウンデッドなゲート — 生の ``is_dir`` は死んだ
        # 共有で GUI を数十秒止める。兄弟実装なので判定も同じものを使う
        # （片側だけ生 stat のままだと、パンくずの祖先クリックだけが数十秒
        # フリーズしたうえ「見つかりません」を名乗る非対称になる）。
        # **現在地の早期 return はプローブより前**。後ろに置くと、
        # 死んだ共有で現在地のパンくずを押しただけで 2 秒固まったうえ
        # 「フォルダが見つかりません」プロンプトが出て、閉じても何も起きない
        # （＝ I/O も対話も完全に無駄）。何もしないと決まっている操作で
        # ファイルシステムへ訊かないこと。
        if path == self._root:
            return
        if not self._is_reachable_dir(path):
            # 生パス 1 行の警告ではなく、4 経路共通の
            # 「見つかりません」応答へ（原因の説明 + 再試行 / 別のフォルダ）。
            if self._handle_missing_folder(path) != "retry":
                return
        came_from = self._root
        # ↑Up と同じく、ここまで来たら判定は済んでいる（二重
        # プローブをしない）。``assume_exists`` 付きの ``set_root`` は現状
        # 失敗しないが、戻り値のガードは残す（set_root の契約は「着地したか」
        # であって、将来別の理由で False になり得る — 片側だけ落ちた選択予約が
        # 変わらないペインへ積まれないようにする）。
        if not self.set_root(path, push_history=True, assume_exists=True):
            return
        # Like ↑Up's pending_select, but the old root may sit several levels
        # below the clicked ancestor — the fallback selects (and scrolls to)
        # the destination's direct child on the way down to where we were.
        self._post_grid.set_pending_select(came_from, ancestor_fallback=True)

    def _on_reload(self) -> None:
        # 存在確認の同期 ``is_dir`` ゲートは置かない:
        # 切断 NAS では SMB タイムアウトまで GUI スレッドが凍結し、しかも
        # False 時は無言 no-op になる。「見つかりません」再試行と同じく
        # ``assume_exists=True``
        # で先へ進め、存在確認は非同期スキャンの失敗（エラーカード + 再試行
        # ボタン）に委ねる。
        # Preserve the *selected tile* across the reload, not just the folder
        # context: ``_current_selection()`` (= the left pane's current_path())
        # returns the selected file OR folder, whereas ``_current_folder`` is
        # None for a plain root-level file selection / ZIP preview.  This
        # matches the back/forward path, which restores via the same accessor.
        keep_selected = self._current_selection()
        # F5 semantics for the 「ランダム」 sort: deal a fresh shuffle (no-op
        # for every other sort mode).
        self._post_grid.reshuffle_random_sort()
        # フィルムストリップの失敗確定セル（デコード失敗グリフ）を再試行対象へ
        # 戻す: パス列不変の再スキャンでは set_images が呼ばれない
        # （サムネ破棄防止）ため、ファイル修復後の F5 の再試行入口はここ。
        self._reset_filmstrip_failures()
        # 同一ルートの再スキャン: ステージモードを保持する。再スキャン後に
        # 保持対象の選択が消えたときだけ set_root が着地時にブラウズへ落とす。
        # 内容が変わったかもしれない明示リロード — 着地後の refire でも右ペインを
        # 再スキャンさせる（純 UI リビルド時の再スキャン抑止を 1 回だけ解除）。
        self._pending_content_rescan.set(MARK)
        # 中央プレビューの同一パス早道も 1 回解除する — 着地の再選択が
        # 同じファイルを書き換え後の中身で読み直せるように。
        self._content.invalidate_shown_path()
        self.set_root(
            self._root,
            push_history=False,
            pending_select=keep_selected,
            keep_mode=True,
            assume_exists=True,
        )

    def notify_library_changed(self, paths: list[Path]) -> None:
        """外部ツール/プラグインが *paths* 配下のファイルを書き換えたと通知する。

        in-place 上書き（post.md / 画像バイトの差し替え）は親フォルダの mtime
        を変えないため、フォルダプレビューキャッシュが恒久的に古い答えを
        返し続ける — ここで対象サブツリーを落とし、表示中のペインが影響範囲
        なら選択を保ったまま再スキャンする。ジョブ完了などのまとまった単位で
        呼ぶこと（ファイル 1 件ごとに呼ばない）。

        **GUI スレッド専用**（``set_root`` は GUI スレッド以外から呼ぶと Qt が
        壊れる）。行儀の悪いプラグインがワーカースレッドから呼んだ場合を検出
        して警告する — 契約は変えないが、非 GUI 呼び出しを黙って壊れるのではなく
        表面化させる。パスがシンボリックリンク/ジャンクションで別名参照される
        構成では、出力パスと表示中ルートが別名だと ``_subtree_overlaps`` の
        文字列判定が空振りしうる（FS に触れない設計なので許容 — その場合は
        次回のナビゲーションで解決される）。
        """
        app = QApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            logger.warning(
                "notify_library_changed called off the GUI thread "
                "({}); plugins must marshal this to the GUI thread. Ignoring.",
                QThread.currentThread(),
            )
            return
        if not paths:
            return
        if self._folder_cache is not None:
            for p in paths:
                self._folder_cache.invalidate_under(p)
        if self._root is None or not any(
            _subtree_overlaps(self._root, p) for p in paths
        ):
            return
        # _on_reload と同じ「選択タイル保持」リロード。``assume_exists=True`` で
        # ルートの同期 ``is_dir`` を行わない: この通知は
        # ユーザー操作なしに発火する（プラグインのジョブ完了等）ため、切断 NAS での
        # SMB タイムアウト分 GUI が凍結してしまう。フォルダ消失ダイアログ
        # （set_root の I07 プロンプト）も同フラグで出ない — ルート消失時は
        # 非同期スキャンの失敗（エラーカード）が引き受ける。ステージ閲覧中に
        # 勝手にグリッドへ切り替わらないよう keep_mode=True でモードを維持する。
        # 内容が書き換わった通知なので、失敗確定セルも再試行対象へ戻す
        # （F5 と同じ再試行入口）。
        self._reset_filmstrip_failures()
        # F5 と同じく「内容が変わったかもしれない」明示リロード — 着地後の
        # refire でも右ペインを再スキャンさせる。
        self._pending_content_rescan.set(MARK)
        self._content.invalidate_shown_path()  # F5 と同じ（中身が変わった）
        self.set_root(
            self._root,
            push_history=False,
            pending_select=self._current_selection(),
            keep_mode=True,
            assume_exists=True,
        )

    def _on_root_change_requested(
        self,
        path: Path,
        pending_select: Path | None = None,
        *,
        assume_exists: bool = False,
    ) -> SetRootResult:
        # Navigate FIRST: a vanished destination (set_root warns + no-ops)
        # must not wipe the trail the user still has.
        # ``assume_exists``: 呼び出し側が既に「見つかりません」応答を出して
        # 再試行を選ばれた場合 — 同期 stat を重ねない。
        result = self.set_root(
            path,
            push_history=False,
            pending_select=pending_select,
            assume_exists=assume_exists,
        )
        if not result:
            return result
        # Picking a brand-new root is a hard reset of the trail.
        self._history.clear()
        self._forward.clear()
        self._update_nav_buttons()
        self._record_recent_root(path)
        return result

    def picker_places(self) -> list[Path]:
        """Folders the non-native picker offers in its side panel.

        Qt's own dialog has no shell 「クイックアクセス」, so the places the user
        already named — registered library roots first, then bookmarks — are
        what the panel is for.  Paths are handed over unprobed: a ``stat`` on
        an offline NAS entry here would block the GUI thread, which is the very
        thing the non-native picker exists to avoid.
        """
        places: list[Path] = []
        seen: set[Path] = set()
        for raw in list(self._library_roots) + list(self._state.bookmarks):
            place = Path(raw)
            if place not in seen:
                seen.add(place)
                places.append(place)
        return places

    def _pick_root(self) -> bool:
        """選んだフォルダへ開き直し、着地したか（入れ子の「別のフォルダを
        開く」での着地を含む）を返す。取り消し・到達不能は ``False``。"""
        chosen = pick_existing_directory(
            self,
            t("common.action.choose_folder"),
            str(self._root),
            sidebar=self.picker_places(),
        )
        if not chosen:
            return False
        result = self._on_root_change_requested(Path(chosen))
        return result is not SetRootResult.UNCHANGED

    # ----------------------------------------------------------- window D&D

    @staticmethod
    def _first_local_path(mime) -> Path | None:  # noqa: ANN001
        """First local-file URL in *mime*, or ``None`` (window drop helper).

        Purely inspects the URL payload — no filesystem I/O — so it is cheap
        enough to call on every ``dragEnterEvent`` without stat-ing an offline
        share on drag-hover.
        """
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                local = url.toLocalFile()
                if local:
                    return Path(local)
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Accept a folder / file drop onto the window to change the root.
        # Child widgets that accept their own drops (advanced_search / tag_chips
        # similar-search seed) consume the event before it reaches the window,
        # so this never steals their D&D.  Drags that ORIGINATE inside this app
        # (grid tile / centre preview export drags — event.source() is the
        # in-app widget) are ignored: they exist to export files to other apps
        # or feed the similar-search seed row, and a slip of the mouse must not
        # re-root the viewer onto the dragged image's folder.
        if (
            event.source() is None
            and self._first_local_path(event.mimeData()) is not None
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Keep signalling "droppable" as the cursor moves (some platforms drop
        # the acceptance from dragEnterEvent otherwise).
        if (
            event.source() is None
            and self._first_local_path(event.mimeData()) is not None
        ):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.source() is not None:  # in-app drag — never re-root
            event.ignore()
            return
        path = self._first_local_path(event.mimeData())
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        # Resolving dir-vs-file touches the filesystem, but only once on the
        # actual drop (not on hover).  A folder becomes the new root; a file
        # opens its parent folder with the file pre-selected — mirroring the
        # positional-argument launch path.  Unlike the folder picker
        # (_on_root_change_requested = hard trail reset), a drop pushes the
        # position being left onto the back stack so 戻る undoes the drop —
        # an accidental drop would otherwise silently destroy the whole trail.
        #
        # 判別も**バウンデッド**に行う。ドロップ受けは dir/file の判別が
        # 要るが、``probe_path_kind`` はまさに ``dir`` / ``file`` / ``missing``
        # を返す関数（引数起動が同じ判別に使っている）なので生 stat は要らない。生 stat の
        # ままだと、死んだ共有のパスを（stale なエクスプローラ窓等から）
        # 落としたとき GUI が最大 2 回ぶんの SMB タイムアウト固まったうえ
        # 無言 no-op になる。タイムアウト（``None``）は他の同期ゲートと同じ
        # 「ディレクトリとみなす」— 報告は非同期スキャンのエラーカード +
        # [再試行] に委ねる。
        #
        # **プローブの結果を捨てて ``set_root`` に判定させ直さないこと**:
        # ``assume_exists`` を渡さないと ``set_root`` が
        # ``_is_reachable_dir`` で**同じパスをもう一度**プローブし、(a) 死んだ
        # 共有では固まる時間が 2 倍（最大 4 秒）になり、(b) スリープから起き
        # かけの共有（1 回目タイムアウト → 2 回目 ``file``）では、**ファイルの
        # ドロップなのに、そのファイル名を名乗る「フォルダが見つかりません」
        # プロンプト**へ落ちてドロップが無効化される。判定は 1 回きり。
        kind = probe_path_kind(str(path))
        if kind in ("dir", None):
            landed = self.set_root(path, push_history=True, assume_exists=True)
            # **MRU に載せるのは「dir だと確認できた」ときだけ**。
            # ``None`` はタイムアウト＝*未確定*で、スリープから起きかけの共有
            # へファイルを落とすとここへ来る。ナビゲーションを楽観的に進める
            # のは正しい（報告は非同期スキャンのエラーカードに委ねる）が、MRU
            # は**メニューに残り続ける永続状態**なので、当て推量でファイル
            # パスを焼き付けない。本当にフォルダなら ``last_root`` として
            # 記録され、次に開いたときに MRU へ載る。
            if landed and kind == "dir":
                self._record_recent_root(path)
            return
        parent = path.parent
        # 親は別パスなので改めてゲートする（``file`` なら実質必ず通る）。
        # ここも判定は 1 回きり — 通った時点で ``assume_exists`` を渡す。
        if self._is_reachable_dir(parent) and self.set_root(
            parent, push_history=True, pending_select=path, assume_exists=True,
        ):
            self._record_recent_root(parent)

    def _on_file_selected(self, path: Path) -> None:
        # Right-pane single-click: preview only.  ZIPs do NOT drill in on
        # single click — that's reserved for ``file_activated``
        # (double-click) to match the left pane's semantics.
        # 代表画像の自動選択（pending-select の解決経路のみ）か? — ユーザーの
        # 明示クリック / 直接の select_path は含めない（同じパスでも詳細
        # カードを出すのが正）。
        auto_no_detail = (
            self._auto_select_no_detail == path
            and self._file_list.is_resolving_pending_select()
        )
        self._auto_select_no_detail = None
        # ユーザー起点の明示選択は実行中の代表画像プローブを失効させる:
        # ``_on_first_image_found`` のガードは
        # generation と ``_current_folder`` しか見ないため、同一フォルダ内で
        # 別ファイルを選び直しても stale なプローブ着地が中央プレビューと
        # 右ペイン選択を先頭画像へ奪い返してしまう。バンプは下の
        # ``_set_path_status`` が落とす（世代バンプは funnel 一本）。
        # プローブ自身の pending-select 解決（``auto_no_detail`` の文脈）は
        # ``path == _preview_fallback.shown`` で funnel 側が除外するので、
        # ``#thumb#`` 暫定表示の非同期差し替えは殺されない。
        self._last_previewed_file = path  # startup-resume capture (B01)
        self._pending_restore_preview.clear()
        # 代表画像の自動選択（右一覧の pending-select 解決）で来たときは
        # 「代表:」接頭を保つ: 代表がフォルダ直下にあると右一覧が
        # その行を自動選択するため、素通しだと直前に立てた接頭がここで消え、
        # 同じジェスチャ（フォルダのシングルクリック）の表記が代表の在り処で
        # 2 通りに割れる。
        self._set_path_status(path, representative=auto_no_detail)
        self._content.show_path(path)
        # 右パネルは中央の選択（このファイル）の詳細表示エリア — ファイル詳細
        # カードを出す（post.md / #thumb# は _refresh_file_detail 側で除外）。
        # ただしフォルダ選択に伴う代表画像の**自動**選択では出さない — その
        # 文脈の右パネルはフォルダの詳細（メタカード）であり、自動選択が
        # メタカードを置き換えてはならない（共通コア: メタは追加表示）。
        if not auto_no_detail:
            self._refresh_file_detail(path)
        # 画像トラックのハイライトは右ペイン選択（= ‹ ›/←→ の画像送り先）に
        # 追従する（最大化中のみ — ヘッダーの n/m は常時更新）。
        self._sync_image_strip_current()

    def _on_file_activated(self, path: Path) -> None:
        # ユーザーが**明示的に**開いた物は、たとえそれがフォルダの代表画像と
        # 同一パスでも「ウィンドウが選んだ代表画像」ではない。マークを
        # 落としておかないと、デコード失敗時のフォールバック再プローブが
        # 「選んだ物を黙って次候補へ差し替える」挙動になる（
        # ``_on_preview_image_failed`` の docstring が明記する『ユーザーの
        # 明示選択の失敗はエラーカードのまま』に反する）。
        # あわせて実行中の代表画像プローブも失効させる: 選択と同じく
        # アクティベートも ``_current_folder`` を変えないため、バンプしないと
        # stale な着地（フォールバック再プローブを含む）が中央プレビューを
        # ユーザーの開いたファイルから先頭画像へ奪い返す。
        # ``_on_grid_file_activated`` と同じ理由で funnel へ寄せない —
        # 下の枝は ``_current_preview_path == path`` のとき ``_set_path_status``
        # を呼ばず、ZIP ドリルインの枝はそもそも通らない。
        self._preview_fallback.shown = None
        self._preview_stream.cancel()
        if path.suffix.lower() in ZIP_DRILL_SUFFIXES:
            self._open_zip_as_folder(path)
            return
        # 右一覧の画像/動画ダブルクリックも左グリッドと同じ「プレビュー
        # 最大化」へ（全画面ライトボックスへは直行しない — F11 / 表示メニューが
        # 全画面の唯一の入口）。
        if path.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            if self._current_preview_path != path:
                self._set_path_status(path)
                self._content.show_path(path)
            self._enter_stage_mode()
            return
        self._set_path_status(path)
        # Unpreviewable file → OS default app on double-click, mirroring
        # the left pane; previewable kinds keep the in-place preview.
        if not has_dedicated_view(path):
            self._open_with_default_app(path)
            return
        self._content.show_path(path)
        # この「最大化」はメディア枝だけでなく**専用ビューを持つ全種別**に効く
        # （左グリッドの ``_on_grid_file_activated`` も同じ位置で最大化する）。
        self._enter_stage_mode()

    def _on_similar_search_requested(self, path: Path, pixmap: object) -> None:
        # Right-pane "この画像に類似を検索" (C-10): seed the left pane's similar
        # search.  The pixmap (this pane's decoded thumbnail, or None) rides
        # along so the seed-preview row can show the image even though it isn't
        # a left-pane tile.  set_similar_seed enables semantic mode + kicks the
        # vector scan; it's a no-op without a VectorIndex.
        pm = pixmap if isinstance(pixmap, QPixmap) else None
        self._post_grid.set_similar_seed(path, pm)

    def _open_zip_as_folder(self, zip_path: Path) -> None:
        """Drill into a ZIP via the extraction controller.

        See ``zip_drill.py::ZipDrillController.open_zip_as_folder`` — a
        background extract into a temp dir (window-modal progress dialog,
        cooperative cancel) that re-roots the viewer on success, or the
        central-directory preview fallback for oversized archives.
        """
        self._zip_drill.open_zip_as_folder(zip_path)

    def _show_zip_preview_fallback(self, zip_path: Path) -> None:
        # ZipDrillController's over-size-limit fallback: show the ZIP's
        # central-directory listing in the centre pane instead of extracting.
        self._content.show_zip(zip_path)
        self._stack_show_zip_preview()

    def _open_extracted_zip_root(self, temp_dir: Path) -> None:
        # ZipDrillController's success path: descend into the extracted tree
        # exactly like a folder drill-down (history push included).
        self.set_root(temp_dir, push_history=True)

    def _show_status_message(self, text: str, timeout_ms: int) -> None:
        """Transient status-bar message — the **second** rung of the failure
        funnel (``view_prefs.notify_failure``), not a notification channel of
        its own.

        design.md: 一時メッセージをステータスバーへ直書きしないこと。左端の
        常設パス表示（いま どこに居るか）を数秒まるごと潰すため。成功 / 情報は
        :meth:`_show_toast`、失敗は ``notify_failure`` を使う — ここが残って
        いるのは、トーストのファネルを持たないホストでも失敗を落とさない、と
        いう ``notify_failure`` の約束のため。
        """
        self.statusBar().showMessage(text, timeout_ms)

    def _notify_extracted(self, text: str, duration_ms: int) -> None:
        """``ZipDrillController`` の告知口（成功トーストへ変換する）。

        コントローラ側の型（``Callable[[str, int], None]``）はそのまま — 着地
        面だけをステータスバーからトーストへ差し替える。
        """
        self._show_toast(text, "success", duration_ms)

    def _show_toast(
        self,
        message: str,
        kind: str = "info",
        duration_ms: int = 3000,
        action_text: str | None = None,
        on_action=None,
    ) -> None:
        """Raise a non-modal toast on this window (I05/I06 success/info feed).

        The single funnel every success/info notification passes through so the
        design system's "成功=非モーダル" principle has one call site.  ``kind``
        ∈ ``{"info", "success", "warning", "error"}`` (see ``common/ui/toast``).
        ``duration_ms`` は表示時間（既定 3 秒。0 でクリックまで残す）— 長め /
        短めが要る呼び出し側もこの 1 本を通せるようにするための引数で、
        ``show_toast`` を親ウィンドウへ直接呼ぶ回避実装を作らないこと。
        ``action_text`` / ``on_action`` は追随ボタン 1 つ（共通 API）—
        近道であって唯一の入口にはしないこと。
        """
        show_toast(
            self,
            message,
            kind=kind,
            duration_ms=duration_ms,
            action_text=action_text,
            on_action=on_action,
        )

    def _stack_show_zip_preview(self) -> None:
        # ``_content.show_zip`` already switches the stack page; the
        # right-pane file list isn't relevant for a stand-alone preview,
        # so clear it to match the behaviour of other single-file previews.
        self._current_folder = None
        self._file_list.clear()
        self._refresh_info_meta(None)
        self._refresh_file_detail(None)

    def _on_content_navigate(self, delta: int) -> None:
        # Centre pane (ImageView / MediaView / TextView) advanced via ←/→
        # or an at-edge wheel notch.  Moving the right-pane selection emits
        # ``selection_changed`` → ``file_selected`` *synchronously* (Qt
        # direct connection), which already routes through
        # ``_on_file_selected`` → ``show_path`` and loads the new file in
        # the centre.  We must therefore NOT call ``show_path`` again here:
        # doing so decoded every file twice, doubling the per-step cost
        # that made fast wheel navigation feel heavy.
        #
        # 歩く母集合は右ペインの生タイル列ではなく ``tile_paths()``
        # （post.md / #thumb# を除いた表示可能メディア）— ヘッダーの n/m・
        # 画像トラック・閲覧モードのプレイリストと同じ集合にする。
        # これが無いと ‹ で Markdown ビューへ着地して
        # 「1 枚目が 2/8」になる。
        self._step_media_selection(delta)

    def _media_paths_and_index(self) -> tuple[list[Path], int]:
        """表示可能メディアの母集合と現在位置.

        ``tile_paths()`` は右ペインのタイル列から内部ファイル（post.md /
        ``#thumb#…``）を除いたもの。現在選択がその集合の外（= post.md を
        一覧から選んでいる）なら index は ``-1``。
        """
        paths = self._file_list.tile_paths()
        current = self._file_list.current_path()
        if current is None:
            return paths, -1
        try:
            return paths, paths.index(current)
        except ValueError:
            return paths, -1

    def _step_media_selection(self, delta: int) -> None:
        """表示可能メディアの母集合内で右ペイン選択を 1 つ進める / 戻す。"""
        paths, index = self._media_paths_and_index()
        if not paths:
            return
        if index < 0:
            # post.md を選んでいる状態からの送り — 集合の端から入り直す。
            new = 0 if delta > 0 else len(paths) - 1
        else:
            new = index + delta
        if 0 <= new < len(paths) and (index < 0 or new != index):
            self._file_list.select_path(paths[new])
            return
        self._notify_media_edge(delta)

    def _notify_media_edge(self, delta: int) -> None:
        """投稿内の端で ←→ を押したときの案内.

        全画面（閲覧モード）は端で「もう一度 → で次の投稿へ」と教えるので、
        プレビュー最大化中も無反応にしない（キーが壊れたようにしか見え
        ない）。同じ部品の中央オーバーレイで、次/前の投稿へ行くキー
        （Ctrl+←/→ — 最大化中のみ有効なショートカット）を案内する。
        分割ビュー中は ‹ › ボタンと選択の見た目で「端」が読めるので出さない。
        """
        if getattr(self, "_ui_mode", "browse") != "stage":
            return
        content = getattr(self, "_content", None)
        if content is None:
            return
        content.show_center_message(
            t("viewer.content_view.edge_next_post_hint") if delta > 0
            else t("viewer.content_view.edge_prev_post_hint")
        )

    def _on_content_jump_edge(self, last: bool) -> None:
        """プレビューの Home/End — 現在フォルダの先頭 / 末尾ファイルへ。

        ``_on_content_navigate`` と同じく右ペイン（情報パネルのファイル一覧）の
        選択を動かすだけ: ``selection_changed`` → ``file_selected`` が同期で
        走り、中央プレビュー・画像トラック・ステータスがまとめて追従する。
        母集合も ‹ › と同一（表示可能メディアのみ）。
        """
        paths = self._file_list.tile_paths()
        if not paths:
            return
        self._file_list.select_path(paths[-1] if last else paths[0])

    def _on_file_link_clicked(self, path: Path) -> None:
        # Markdown body link → if it's a file in the current folder, route it
        # through the right pane so the selection stays in sync.
        if self._current_folder and path.parent == self._current_folder:
            # If the path is already selected, select_path finds index ==
            # _selected and returns without emitting selection_changed, so
            # _on_file_selected / show_path would never be called.  Detect
            # that case and fall through to show_path directly.
            already_selected = self._file_list.current_path() == path
            if not already_selected:
                self._file_list.select_path(path)
                # select_path emitted file_selected → _on_file_selected →
                # show_path already handled the preview; nothing more to do.
                return
        self._set_path_status(path)
        self._content.show_path(path)

    def _postref_folder(self, service: str, post_id: str) -> Path | None:
        """``(service, post_id)`` → candidate folder via the search index.

        Thin guarded wrapper around ``SearchIndex.resolve_postref``.  Purely
        textual — no filesystem verification — so it is safe on GUI-thread
        hot-ish paths (plugin result handlers may call it per post id without
        paying a NAS stat).  ``None`` when the index is disabled or cold.
        """
        if self._search_index is None:
            return None
        try:
            return self._search_index.resolve_postref(service, post_id)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("resolve_postref failed: {}", exc)
            return None

    def _resolve_post_link(self, url: str) -> Path | None:
        """Map a ``post.md`` body URL to a downloaded post's folder.

        Parses the URL to ``(service, post_id)`` and looks it up in the
        search index's ``postref`` table.  **Purely textual — no filesystem
        verification**: this resolver runs on the GUI
        thread for EVERY anchor in the body on EVERY ``setHtml`` (initial
        render, viewport reflows, font-size changes, ``refresh_links``), so a
        per-link NAS ``is_dir``/``is_file`` would freeze the GUI for links × latency
        on cold SMB — the same NAS-free rule the rest of markdown_view keeps.
        A stale index entry can therefore tag a since-deleted post; the
        click-time gate in ``_on_post_link_clicked`` (one stat per click)
        keeps the jump itself safe.  ``None`` when the index is disabled, the
        URL isn't a recognised post page, or the post isn't downloaded.
        """
        parsed = parse_post_url(url)
        if parsed is None:
            return None
        return self._postref_folder(*parsed)

    def _on_post_link_clicked(self, folder: Path) -> None:
        # 📁 in a post body → navigate the left pane to the downloaded post:
        # re-root to its parent and select it (same pattern as a right-pane
        # folder double-click), which fires ``folder_selected`` → its post.md.
        # クリック時の 1 回だけの stat も**バウンデッド**に: 他のナビゲーション同期ゲートと同じ 2 秒プローブで、到達不能
        # （タイムアウト）は「消えた」ではないので先へ進める。
        if not self._is_reachable_dir(folder):
            # 索引の ``postref`` 行は消されない（search_index が prune する
            # のは ``node`` だけ）ので、フォルダをリネーム / 移動 / 削除した
            # 後も 📁 は出続ける。黙って return するとボタンが完全な無反応に
            # 見えるため、理由を出す。
            self._show_toast(
                t("viewer.main_window.post_link_folder_missing"), "warning"
            )
            return
        # 親のプローブはしない（同じクリックで 2 回目の 2 秒
        # ゲートを積まない）。子が ``dir`` と分かったなら親も必ずディレクトリ
        # で、タイムアウトだったなら他のゲートと同じ「あるとみなして進む」。
        self.set_root(
            folder.parent,
            push_history=True,
            pending_select=folder,
            assume_exists=True,
        )

    # ------------------------------------------------------ user curation

    def _on_curation_changed(self) -> None:
        """A star / user-tag / watch-later change landed — repaint mirrors.

        The left pane already updated its own view / map; here we repaint the
        right pane and an open lightbox, which read the same map through the
        provider, so every surface's badges agree.

        受け手はこの 1 関数で、面が増えるたびに後追いで欠落を塞いできた
        （下のコメント群がその履歴）。面を足すときは、ここへ 1 行足すのと
        同じ差分で ``tests/test_viewer_ui_review_0828_curation.py`` の面の表
        （``test_one_curation_write_reaches_every_surface_that_shows_a_mark``）
        にも 1 行足すこと — 表は遅延生成の 3 面（全画面 / 画像トラック /
        詳細情報ウィンドウ）を出した状態で 1 回の書き込みを追いかける。
        """
        self._file_list.refresh_curation()
        if self._lightbox is not None and self._lightbox.isVisible():
            self._lightbox.refresh_curation()
        # 最大化中の画像トラックの★マークも同じ map を読む。
        # getattr ガードは __init__ を通さないテストハーネス向け（本ファイル
        # 内の他のクローム参照と同じ流儀）。
        strip = getattr(self, "_image_strip", None)
        if strip is not None:
            strip.update()
        # レールの「印を付けた件数」とユーザータグ行も同じ変更を映す
        # — メモリ走査なので I/O ゼロ。
        self._sync_nav_rail_curation()
        # プレビューヘッダーの現在★も同じ変更を映す。
        self._update_stage_header()
        # 情報パネルの詳細カードも同じ map を読んでいるが、値はカード生成時の
        # スナップショットなので★を変えても再選択するまで古いままになる。
        # 表示中の path のときだけ詰め直す — 同じ path
        # を渡す ``set_file_detail`` はサムネイルを保持するので、ここで
        # 再デコードは起きない。ユーザータグ行も同じ経路で追従する。
        detail = self._pending_file_detail.peek()
        if detail is not None:
            detail.star = self._file_star(detail.path)
            detail.user_tags = self._file_user_tags(detail.path)
            detail.later = self._file_later(detail.path)
            self._info_panel.set_file_detail(detail)
        # 投稿情報カードはここから塗り直さない: E2 で印の 3 行はカードから最上段の
        # 印ストリップへ移り、カードに残ったのは post.md 由来の行だけになった
        # （``InfoPanel.set_post_meta`` の docstring）。印はストリップ側が
        # ``_update_stage_header`` → ``_sync_curation_strips`` で同じペインの席に
        # 映すので、ここで ``_apply_meta_card`` を呼ぶと★を 1 つ押すたびにカードの
        # QLabel を全部作り直すだけになる。
        # 詳細情報ウィンドウ（Ctrl+I）の印の 3 行は同じ変更を映す
        # — この窓は ``show_path`` = 選択変更でしか
        # 更新されないため、選択を動かさない★付けでは開いたまま古い値が残る。
        detail_win = getattr(self, "_detail_window", None)
        if detail_win is not None and detail_win.isVisible():
            detail_win.refresh_curation()

    def _rename_follow_base(self, root: Path) -> Path:
        """リネーム追従が歩く基準（登録ライブラリの最外基準。外なら *root*）.

        ライブラリ基準から歩けば、そのライブラリ内の移動（降下 / ↑ / 戻る）は
        1 度のウォークで覆える。パンくずと同じ基準選びを使うので「どこまでが
        同じライブラリか」の第二実装を作らない。
        """
        if getattr(self, "_library_roots", None) is None:
            # ライブラリ一覧が未確定（構築途中 / __init__ を通さないハーネス）。
            return root
        picked = pick_library_base(root, self._compute_library_bases())
        return picked[0] if picked is not None else root

    def _rename_follow_needed(self, root: Path) -> bool:
        """*root* でリネーム追従を蒔き直す必要があるか.

        直前に歩いた基準の配下（同一を含む）なら、その結果が現在地を覆って
        いるので蒔き直さない。
        """
        last = getattr(self, "_rename_follow_root", None)
        if last is None:
            return True
        try:
            return not root.is_relative_to(last)
        except (OSError, ValueError):  # pragma: no cover (defensive)
            return True

    def _kick_rename_follow(self) -> None:
        """Off-thread: re-point curation rows onto renamed post folders.

        Builds a ``postref -> current folder`` resolver by scanning the current
        library base for the specific postrefs that have curation (bounded,
        early-exit), then repoints any store rows whose path vanished.  A no-op
        when there is no store, no root, or no postref'd curation.  Guarded so a
        rapid root change drops a stale result; on completion the panes'
        in-memory map is refreshed only if something actually moved.

        歩く基準は現在ルートではなく :meth:`_rename_follow_base`（登録
        ライブラリの最外基準）— ライブラリ内の移動で歩き直さずに済むように、
        「どこまで覆ったか」を :attr:`_rename_follow_root` に残す。
        """
        if self._user_meta is None or self._root is None:
            return
        wanted = set(self._user_meta.postrefs())
        if not wanted:
            return
        root = self._rename_follow_base(self._root)
        self._rename_follow_root = root
        store = self._user_meta

        def _work(job: StreamJob):
            from .user_meta import build_moved_resolver

            resolver = build_moved_resolver(
                root, wanted, should_cancel=job.cancel.is_cancelled,
            )
            # 3 脚目（行ごとの stat ループ）にも同じトークンを渡す — ここだけ
            # 渡し忘れると、ルート切替 / closeEvent の cancel が最後の脚へ届
            # かず、死んだ共有で窓じまいがその分待たされる。
            moved = store.resolve_moved_entries(
                resolver, should_cancel=job.cancel.is_cancelled,
            )
            if moved > 0:
                # ``resolve_moved_entries`` はキャンセルされても「その時点で
                # 決まった張り替え」を store へ commit 済みで戻る。着地
                # （``done``）はセッションが畳まれていれば捨てられるので、
                # 「書いた」という事実だけはここで GUI 側へ残す — 捨てられた
                # 着地とともに落とすと、DB は張り替わったのに母集合
                # （_user_meta_map）が古いまま、そのセッションの間ずっと★
                # バッジが出ない（再 kick されない枝ではウォークも蒔かれない）。
                # 走るワーカーは専用プール 1 本きりなので、この代入と GUI 側の
                # 読み取り = 0 化は着地を挟んで前後し、競合しない。
                self._rename_follow_pending = True
            return moved

        # 追い越し（``submit_job``）が前回のウォークを止めてから新しい世代を
        # 配る（世代だけ進めても走行中の scandir は止まらない）。
        self._rename_follow_stream.submit_job(_work)

    def _on_rename_follow_done(self, payload: object) -> None:
        moved = payload if isinstance(payload, int) else 0
        self._flush_rename_follow(moved > 0)

    def _on_rename_follow_cancelled(self) -> None:
        """降りたウォークが書き残した張り替えを引き取る。

        ``bind_cancelled`` はストリームが**止まった**ときだけ来る
        （追い越しでは来ない）。追い越された分は次の着地
        （:meth:`_on_rename_follow_done`）が同じ pending を引き取るので、
        どちらの降り方でも「書いたのに映らない」は残らない。同じ
        キャンセルで複数回来るが、pending の読み取り = 0 化が冪等に
        している。
        """
        self._flush_rename_follow(False)

    def _flush_rename_follow(self, moved: bool) -> None:
        # 0 化は「真だったときだけ」書く: ``cancel()`` の同期 emit はワーカーが
        # まだ走っている最中に GUI 側でここを通すので、読みと書きの間にワーカー
        # の ``= True`` が挟まりうる（タプル代入の LOAD/STORE は不可分ではない）。
        # 条件を噛ませば、失われうる並びが「偽を読んで偽を書く」だけになる。
        pending = self._rename_follow_pending
        if pending:
            self._rename_follow_pending = False
        if not (moved or pending):
            return
        # Rows moved onto the current-root folders — refresh the panes'
        # in-memory map so the badges appear on the renamed tiles.
        # refresh_user_meta は _apply_curation と同じ尾部を通るので、★並び /
        # 印の絞り込みの再構築と curation_changed（→ _on_curation_changed が
        # 右一覧・レール件数・ステージヘッダー・情報パネル・全画面・詳細情報
        # ウィンドウへ配る）はここで個別に足さない。
        self._post_grid.refresh_user_meta()

    def _set_current_star(
        self, star: int, path: Path | None = None, *, notify: bool = True
    ) -> None:
        """Set the user star for *path* (default: the current preview file).

        Routes through the left pane's ``_apply_curation`` so the store write,
        the in-memory map, and the ``curation_changed`` mirror repaint all go
        through the single owner.

        確認フィードバックも同じ left pane の共通 funnel
        (``show_star_feedback``) を通す — これで
        グリッド / プレビュー（分割）/ プレビュー（最大化）の 3 面が同じ
        「★★★ 対象名」トーストになる。対象名を必ず添える理由:
        同じ 0-5 でもフォーカス位置により投稿フォルダとファイルで対象が
        変わるため、どちらに付いたかがトーストだけで分かるようにする。

        ``notify=False`` は**閲覧モード（全画面ライトボックス）専用**: あちらは
        画面中央に自前の「★★★」オーバーレイを出すので、背後の親ウィンドウへ
        トーストを重ねない（3 面統一の対象は分割 / 最大化のプレビュー面）。

        書き込みが**永続化できなかったとき**は成功トーストを出さない —
        ``_apply_curation`` が偽を返し、失敗の警告トーストは向こうが出す。
        """
        if self._user_meta is None:
            return
        target = path if path is not None else self._current_preview_path
        if target is None:
            return
        self._post_grid.request_curation(target, "star", star, notify=notify)

    def _on_file_list_curation_requested(
        self, path: Path, kind: str, value: object,
    ) -> None:
        """右一覧のキュレーション操作を左ペインの単一書き手へ渡す.

        ``_apply_curation`` の所有は :class:`PostGrid` のまま — こちらは「どの
        パスに何を」を伝えるだけ。★は共通トースト funnel を通すので、対象名
        つきの確認が 3 面（グリッド / プレビュー / 右一覧）で揃う。
        ``edit_tags`` は左ペインの編集ダイアログをそのまま開く（補完候補も
        完了トーストも 1 実装のまま）。
        """
        if self._user_meta is None:
            return
        # 種別ごとの分岐（★はトースト funnel、あとで見るは別の funnel、タグは
        # ダイアログ）は左ペインの ``request_curation`` に 1 本化された。
        self._post_grid.request_curation(path, kind, value)

    def _on_file_list_star_key(self, star: int) -> None:
        """右一覧で 0-5 — 選択中のファイルへスターを付ける."""
        target = self._file_list.current_path()
        if target is None:
            return
        self._set_current_star(star, target)

    def _on_stage_star_key(self, star: int) -> None:
        """プレビューでの 0-5 キー → 表示中画像へスターを永続化.

        ContentView（プレビュー列）にフォーカスがあるときだけ emit される。
        分割ビュー化でプレビューは常時可視なので、分割・最大化を問わず
        受け付ける（中央が裏面で不可視になる状態は無いのでブラウズ中ガードは
        持たない）。user_meta 不在時は ``_set_current_star`` 側が no-op —
        グリッドの数字キーと同じ劣化。
        """
        self._set_current_star(star)

    # ------------------ split view ⇄ preview maximised (candidate A, 2026-07)

    def ui_mode(self) -> str:
        """現在の中央領域モード — ``"browse"``（分割ビュー）か ``"stage"``
        （プレビュー最大化）。値は履歴 (``NavEntry.mode``) 互換のため温存。"""
        return self._ui_mode

    def _set_preview_focus(self, on: bool) -> None:
        """分割 ⇄ プレビュー最大化の**見た目**を適用する（履歴に触れない）。

        実体は中央スプリッタの分割比プリセット切替のみ（ページ遷移なし・
        reparent なし）: 最大化 = グリッド席を 0 に畳む / 分割 = 記憶した
        分割比（既定 55:45）へ戻す。付随して、最大化専用の UI（ヘッダーの
        [◧ 分割に戻す]/[閲覧モード]・画像トラック・Esc / Ctrl+←→
        ショートカット・画像ダブルクリックの意味）を切り替える。履歴
        push/pop の対称性は呼び出し側（``_enter_stage_mode`` /
        ``_enter_browse_mode``）が担う — 取り崩しは
        ``_enter_browse_mode`` の**既定**で、履歴を自分で操作した直後の
        2 経路だけが ``reconcile_history=False`` を明示する。
        """
        split = self._center_split
        sizes = split.sizes()
        restore_preview_hidden = False
        if on:
            # F6（プレビュー非表示）で畳んだ状態から
            # 最大化すると、下の ``_set_preview_visible_flag(True)`` が永続
            # 設定を黙って上書きしてしまう。最大化前の非表示状態を覚えておき、
            # G / Esc で分割へ戻るときに復元する（1 フラグ）。
            self._preview_hidden_before_stage = not self._preview_visible
            if len(sizes) == 2 and sizes[0] > 0 and sizes[1] > 0:
                self._center_split_saved = list(sizes)
            # 畳む前のスクロール位置は GalleryView が縮退レイアウトの間
            # アンカーとして預かり、幅が戻った最初のリレイアウトで適用する
            # （``GalleryView._do_relayout``）ので、ここでは何も控えない。
            total = sum(sizes) if sizes else 0
            split.setSizes([0, max(1, total)])
        else:
            restore_preview_hidden = getattr(
                self, "_preview_hidden_before_stage", False
            )
            self._preview_hidden_before_stage = False
            if len(sizes) == 2 and sizes[0] == 0:
                saved = self._center_split_saved
                if not (len(saved) == 2 and saved[0] > 0):
                    saved = list(_CENTER_SPLIT_DEFAULT_SIZES)
                # 比率で復元 — 最大化中にウィンドウ幅が変わっていても破綻しない。
                self._apply_center_split_sizes(saved)
                # 預けたアンカーの適用（見ていた位置 / 最大化中に動いた選択）
                # は幅が戻ったリレイアウトが行う。ジオメトリ反映済みなら保留
                # リレイアウトをその場で畳み、縮退時の位置が一瞬見えるのを
                # 避ける（未反映なら幅 0 のままで、アンカーは預けたまま残る）。
                self._post_grid._view.flush_pending_relayout()
        # どちらの分岐でもプレビュー席は可視で終わる（最大化 = [0, x] /
        # 分割復帰 = 記憶比率）: 畳み中に E / activate で最大化した場合の
        # 「自動再表示」もここで checked 状態・永続フラグへ反映される。
        self._set_preview_visible_flag(True)
        self._stage_header.set_maximized(on)
        # 表示メニューの ☑ / 有効状態を追従する。getattr
        # ガードは _build_menus 前に走る構築順・テストハーネス向け。
        act_max = getattr(self, "_act_stage_mode", None)
        if act_max is not None:
            act_max.setChecked(on)
        act_split = getattr(self, "_act_browse_mode", None)
        if act_split is not None:
            act_split.setEnabled(on)
        self._set_stage_shortcuts_enabled(on)
        # 画像ダブルクリック: 分割中 = 最大化 / 最大化中 = 等倍⇄フィット。
        self._content.set_double_click_maximize(not on)
        # 最大化中はヘッダー右端に [⛶ 全画面 (F11)] が
        # 常設されるので、ホバーカプセル側の全画面ボタンは重複（しかも
        # フィット/全画面が隣接して紛らわしい）。カプセルのボタンだけ畳む
        # （右クリックメニューの「全画面で表示」は残す = 入口は減らさない）。
        self._content.set_fullscreen_button_visible(not on)
        # 最大化中の「←」は履歴の意味論どおり「分割ビューへ
        # 戻る」1 手になる（最大化時に分割位置を積んでいるため）。出口として
        # 使われることを見越して、最大化中だけツールチップを動的に差し替える。
        self._post_grid.back_btn.setToolTip(
            t("viewer.main_window.back_tooltip_maximized")
            if on
            else t("viewer.post_grid.back_tooltip")
        )
        # 帯の同期はヘッダー更新まで面倒を見る（素直に両方
        # 呼ぶと最大化のたびに ``tile_paths()`` の全走査が 2 回走る）。同期が
        # 走らなかったときだけ下でヘッダーを直接更新する。
        header_synced = False
        if on:
            header_synced = self._refresh_image_strip()
            # フォーカスは**現在ページのサブビュー**へ。
            # +/-/R/Shift+R/F は ImageView 配下スコープ
            # (WidgetWithChildrenShortcut) なので、親の ContentView に
            # setFocus するとズーム・回転が最大化直後に全滅する。ContentView 自身の
            # keyPressEvent（0-5 / Space / Home / End）は、サブビューが
            # 受理しなかったキーが親へ伝播して届く。
            self._content.focus_current_page()
        else:
            self._image_strip.setVisible(False)
            self._post_grid.focus_grid()
        if not header_synced:
            self._update_stage_header()
        # グリッド席の畳み状態が変わった = 中央に案内カードを出してよいかが
        # 変わる。プレビュー中は no-op（``showing_
        # placeholder`` ガード）なので、空の状態のときだけ効く。
        self._sync_centre_placeholder()
        if restore_preview_hidden:
            # 最大化前の「F6 で非表示」へ戻す（表示トグル 3 導線の
            # checked と永続フラグは ``_on_preview_toggled`` が揃える）。
            self._on_preview_toggled(False)

    def _toggle_preview_focus(self) -> None:
        """E / プレビューのダブルクリック: 分割 ⇄ 最大化のトグル。"""
        if self._ui_mode == "stage":
            self._exit_stage_to_browse()
        else:
            self._enter_stage_mode()

    def _enter_stage_mode(self, *, push_history: bool = True) -> None:
        """プレビューを最大化する（旧ステージの後継 — 分割比プリセット）。

        E キー（表示メニュー）/ 画像・プレビュー対象ファイルの activate /
        プレビューのダブルクリックから。グリッドはスプリッタの席に残った
        まま幅 0 に畳まれるだけ（reparent しない）なので、分割へ戻ったとき
        スクロール位置と選択はそのまま。プレビューは分割中も選択伝播で
        更新され続けているため、切替は瞬時。

        **ビュー状態履歴（リデザイン提案3)**: 最大化は「位置」として直前の
        分割位置を履歴に積む — ツールバー「←」の 1 回目が「分割へ戻る」に
        なり、一つ前のフォルダへ飛び越さない。履歴からの再入（進む「→」）は
        ``push_history=False`` で重複プッシュを避ける。
        """
        if self._ui_mode == "stage":
            return
        if push_history and self._root is not None:
            # モードがまだ "browse" のうちに位置を確定させる（mode="browse"）。
            # 新たな分岐なので forward はクリア（ドリルダウンと同じ意味論）。
            self._history.append(self._capture_current_position())
            self._forward.clear()
            self._update_nav_buttons()
        self._ui_mode = "stage"
        self._set_preview_focus(True)

    def _filter_help_popup_open(self) -> bool:
        """ツールバー絞り込み欄の構文ヘルプ（自動表示ポップアップ）が出ているか。

        ポップアップは ``Qt.ToolTip`` フレームで、閉じる責務は post_grid 側の
        ``filter_edit`` eventFilter が持つ（Esc で閉じる）。ウィンドウレベルの
        Esc（``_on_escape``）はそれを奪ってしまうので、開いている間は「取り消すものがある
        入力欄」として委譲側へ倒すための判定（読み取りのみ）。
        """
        grid = getattr(self, "_post_grid", None)
        popup = getattr(grid, "_filter_help_auto", None) if grid else None
        try:
            return popup is not None and popup.isVisible()
        except RuntimeError:  # pragma: no cover - C++ 側が先に消えた
            return False

    def _on_escape(self) -> None:
        """ウィンドウレベル Esc の単一ハンドラ。

        Esc は「いま開いているものを 1 段閉じる」キーとして、フォーカス位置に
        よらず**常に同じ優先順位**で処理する:

        1. **編集中の入力ウィジェットへ委譲** — 入力中の Esc（取り消しのつもり）が
           最大化やモードを畳んでしまわないように。
        2. **プレビュー最大化中なら分割ビューへ戻す**（従来のステージ Esc）。
        3. **それ以外は絞り込み / 検索の一括解除**（``_on_escape_clear`` —
           GalleryView.keyPressEvent 経由にすると、プレビュー列や右一覧に
           フォーカスがあるとき誰も拾わず無反応になる）。

        入力ウィジェットの種別ごとの扱い（1 の内訳）:

        * **QLineEdit（テキストあり・書込可 / 構文ヘルプ表示中）** — QShortcut が
          奪った Esc をその欄へ委譲して本来の処理（絞り込みクリア / 補助ポップ
          アップ閉じ = filter_edit 自前の eventFilter）を走らせ、モードは維持する。
        * **QLineEdit（空・ポップアップも無し）** — Esc に取り消すものが無く、
          委譲すると Esc が完全な no-op になってキーボードで最大化から出られなく
          なる（トラップ）。通常どおり 2 / 3 へ進む。
        * **QAbstractSpinBox（プレビュー内 PDF のページ入力等）** — Qt は Esc を
          ローカル処理しない（keyboardTracking で入力は即コミット済み）ため、
          委譲は恒久 no-op のトラップになる。編集状態だけを終わらせる —
          フォーカスをプレビュー本体へ返してモードは維持し、次の Esc が通常経路で
          ブラウズへ戻せるようにする。フォーカスが spin box 内部の QLineEdit へ
          落ちるプラットフォーム差にも親判定で対応する。
        * **読み取り専用 / 非入力ウィジェット** — 委譲せず 2 / 3 へ。

        委譲中はこの Esc ショートカット自身を一時的に無効化する — ``sendEvent`` で
        送り直した Esc が QApplication の通知経路で再びこのショートカットにマッチし、
        ``_on_escape`` が無限再帰するのを防ぐため。
        """
        focus = QApplication.focusWidget()
        spin = None
        if isinstance(focus, QAbstractSpinBox):
            spin = focus
        elif isinstance(focus, QLineEdit) and isinstance(
            focus.parentWidget(), QAbstractSpinBox
        ):
            spin = focus.parentWidget()
        if spin is not None and not spin.isReadOnly():
            self._content.setFocus()
            return
        if (
            isinstance(focus, QLineEdit)
            and not focus.isReadOnly()
            and (focus.text() or self._filter_help_popup_open())
        ):
            self._sc_escape.setEnabled(False)
            try:
                for kind in (QEvent.KeyPress, QEvent.KeyRelease):
                    QApplication.sendEvent(
                        focus, QKeyEvent(kind, Qt.Key_Escape, Qt.NoModifier),
                    )
            finally:
                self._sc_escape.setEnabled(True)
            return
        if self._ui_mode == "stage":
            self._exit_stage_to_browse()
            return
        # 分割ビュー: 絞り込み / 検索を一括解除（グリッド・プレビュー・右一覧の
        # どこにフォーカスがあっても同じ結果になる）。
        self._post_grid._on_escape_clear()

    def _exit_stage_to_browse(self) -> None:
        """ユーザー明示の最大化解除（G / Esc / ヘッダーの [◧ 分割に戻す]）。

        :meth:`_enter_browse_mode` の薄い別名。
        履歴の取り崩しは既定なので、この名前は「明示の出口」であることを
        読み手へ伝えるだけの役割になっている。
        """
        self._enter_browse_mode()

    def _enter_browse_mode(self, *, reconcile_history: bool = True) -> None:
        """分割ビュー（グリッド + プレビュー）へ戻す（G / Esc / set_root）。

        既定（*reconcile_history* が真）では、最大化時に積んだ履歴エントリ
        （同一ルート・``mode="browse"`` がスタック頂上に居る）を**対称に
        取り崩す** — エントリを pop して現在の最大化位置を forward へ退避
        するので、解除後の「→」で再最大化でき、「←」が最大化前の位置を
        二重に再現することもない。頂上が別エントリ（履歴経由の再入等）の
        ときは素のモード切替だけ行う。分割表示中は no-op（G キーの空打ちで
        forward を壊さない）。

        *reconcile_history* を偽にするのは、**履歴を自分で操作した直後の
        呼び出し口だけ** — ``set_root``（再ルートの離脱位置を既に push 済み）
        と ``_navigate_history``（遷移先エントリを既に pop 済み）の 2 つ。

        **既定を「取り崩す」側に置いている理由**: 素のモード切替を既定に
        すると、対称な取り崩しを呼ぶかどうかが呼び出し側の知識になり、
        離脱経路（``_on_recent_files_requested`` や、``_on_loading_changed``
        の同一ルート再スキャンで保持対象が消えたときの離脱）ごとに同じ規約
        違反を起こしやすい。新しい離脱経路が
        素朴に ``_enter_browse_mode()`` と書いても対称になるよう、既定を
        安全側へ倒してある。**この既定を戻さないこと**。
        """
        if self._ui_mode == "browse":
            return
        if reconcile_history:
            # 退避する現在位置は**まだ最大化中**に採る（mode="stage"）ので、
            # モード切替より前に行う。
            top = self._history[-1] if self._history else None
            if (
                top is not None
                and top.mode == "browse"
                and top.root == self._root
            ):
                self._history.pop()
                self._forward.append(self._capture_current_position())
        self._ui_mode = "browse"
        self._set_preview_focus(False)
        if reconcile_history:
            self._update_nav_buttons()

    def _set_stage_shortcuts_enabled(self, on: bool) -> None:
        # Esc は含めない — ウィンドウレベル常時有効の
        # 単一ハンドラ (``_sc_escape`` → ``_on_escape``) へ集約してある。
        for sc in (self._sc_stage_prev, self._sc_stage_next):
            sc.setEnabled(on)

    def _step_post(self, delta: int) -> None:
        """投稿切替（ヘッダーの ‹前の投稿|次の投稿› / 最大化中の Ctrl+←/→）.

        グリッドの選択を動かすだけ → 既存の selection→プレビュー/右ペイン
        伝播がそのまま走り、ヘッダー / 画像トラックのハイライトも追従する。
        分割・最大化のどちらでも有効（ショートカット側は最大化中のみ活性 —
        ``_set_stage_shortcuts_enabled``）。
        """
        self._post_grid.step_selection(delta)

    # --------------------------------- 情報パネル (右)

    def _on_info_panel_toggled(self, visible: bool) -> None:
        """Show/hide the right 情報パネル (F8 / 表示 popover check).

        Keeps the QAction, the popover check and the persisted state all in
        lockstep (guarded against the mutual ``setChecked`` re-entering), and
        remembers the panel's width so a re-show restores its column instead of
        leaving it collapsed at zero.
        """
        if self._syncing_info_panel:
            return
        self._syncing_info_panel = True
        try:
            if not visible and not self._info_panel.isHidden():
                sizes = self._splitter.sizes()
                # レール側（``_on_nav_rail_toggled``）と同じ下限 — 理由は
                # そちらのコメント（対称適用）。
                if (
                    len(sizes) == 3
                    and sizes[2] >= _INFO_PANEL_DEFAULT_WIDTH
                ):
                    self._info_panel_saved_width = sizes[2]
            self._info_panel.setVisible(visible)
            if visible:
                sizes = self._splitter.sizes()
                if len(sizes) == 3 and sizes[2] == 0:
                    give = min(
                        self._info_panel_saved_width, max(0, sizes[1] - 100)
                    )
                    sizes[1] -= give
                    sizes[2] = give
                    self._splitter.setSizes(sizes)
            self._set_info_panel_checks(visible)
            self._state.info_panel_visible = visible
        finally:
            self._syncing_info_panel = False
        # 席の可視性は空状態オーケストレータの入力（畳まれた席は案内を持て
        # ない）。畳んでいる間に状態が変わっていても、開き直した瞬間に正しい
        # 割当へ揃える — プレビュー列のトグルと同じ後置き（冪等・I/O ゼロ）。
        self._sync_centre_placeholder()

    def _set_info_panel_checks(self, visible: bool) -> None:
        """情報パネルの全トグル UI（F8 QAction / ツールバーボタン）の checked
        を *visible* へ揃える（再入ガード付き — 外殻スプリッタのドラッグ 0
        追従からも呼ばれる）。"""
        was = self._syncing_info_panel
        self._syncing_info_panel = True
        try:
            self._act_info_panel.setChecked(visible)
            self._post_grid.info_panel_btn.setChecked(visible)
        finally:
            self._syncing_info_panel = was

    # --------------------------------- ナビレール (左)

    def _on_nav_rail_toggled(self, visible: bool) -> None:
        """Show/hide the left ナビレール (F7 / 表示 popover check).

        Mirror of :meth:`_on_info_panel_toggled` for the left column: keeps the
        QAction, the popover check and the persisted state in lockstep (guarded
        against the mutual ``setChecked`` re-entering) and remembers the rail's
        width so a re-show restores its column instead of collapsing to zero.

        隠すときは解放幅の**行き先を明示する**: 外殻の stretch は
        0/7/2 なので、素で隠すと Qt が解放幅を中央と情報パネルへ 7:2 で配る
        一方、再表示は中央からしか取り戻さない。この非対称のままだと F7 の往復
        ごとに情報パネルが約 51px ずつ太り、中央が同じだけ痩せる（終了時
        に ``_collect_state`` がそのサイズを書くのでドリフトは再起動後も残る）。
        情報パネルは畳む前の幅のまま据え置き、解放幅は全部中央へ渡す。
        """
        if self._syncing_nav_rail:
            return
        self._syncing_nav_rail = True
        try:
            before = self._splitter.sizes()
            if not visible and not self._nav_rail.isHidden():
                # 記憶幅の採用条件はドラッグ追従（``_on_outer_split_moved``）
                # と同じ「再表示既定幅以上」に揃える: ハンドルで
                # 細く引いた直後に F7 で畳むと、下限が無ければその細い幅が
                # 記憶され、再表示が読めない帯になり ``_collect_state`` が
                # それを永続化してしまう。既定幅未満のときは直前の記憶を温存。
                if len(before) == 3 and before[0] >= _NAV_RAIL_DEFAULT_WIDTH:
                    self._nav_rail_saved_width = before[0]
            self._nav_rail.setVisible(visible)
            if not visible and len(before) == 3 and before[0] > 0:
                total = sum(self._splitter.sizes())
                if total > 0:
                    info = min(before[2], max(0, total - 100))
                    self._splitter.setSizes([0, total - info, info])
            if visible:
                sizes = self._splitter.sizes()
                if len(sizes) == 3 and sizes[0] == 0:
                    give = min(
                        self._nav_rail_saved_width, max(0, sizes[1] - 100)
                    )
                    sizes[1] -= give
                    sizes[0] = give
                    self._splitter.setSizes(sizes)
            self._set_nav_rail_checks(visible)
            self._state.nav_rail_visible = visible
        finally:
            self._syncing_nav_rail = False
        # レールの開閉は中央 2 席の幅を動かす = 空状態の入力が変わる
        # （情報パネル側と同じ後置き — 冪等・I/O ゼロ）。
        self._sync_centre_placeholder()

    def _set_nav_rail_checks(self, visible: bool) -> None:
        """ナビレールの全トグル UI の checked を揃える（:meth:`_set_info_panel_checks`
        のレール版）。"""
        was = self._syncing_nav_rail
        self._syncing_nav_rail = True
        try:
            self._act_nav_rail.setChecked(visible)
            self._post_grid.nav_rail_btn.setChecked(visible)
        finally:
            self._syncing_nav_rail = was

    def _on_outer_split_moved(self, _pos: int, _index: int) -> None:
        """外殻 [レール | 中央 | 情報] スプリッタのドラッグ追従（折り畳み導線）.

        ハンドルドラッグで端の席を幅 0 まで畳んだ状態はトグル OFF と同一視
        する: checked 表示（QAction / popover check / ツールバーボタン）だけを
        追従させ、``setVisible`` はしない — 同じドラッグで引き戻せるまま残す
        （途中で隠すとハンドルごと消えてジェスチャが破綻する）。幅が再表示
        既定幅（``_NAV_RAIL_DEFAULT_WIDTH`` / ``_INFO_PANEL_DEFAULT_WIDTH``）
        以上ある間だけ再表示用の記憶幅を更新する（この関数は
        ドラッグ**中**の全サンプルで呼ばれるため、下限が無い/低い（
        ``_SPLIT_REMEMBER_MIN_PX`` の 80px）と畳みへ向かう途中のサンプルが記憶幅を既定幅未満へ侵食し、
        トグル再表示が細い列になる。既定幅未満のサンプルでは直前の記憶を
        温存する）。永続化は ``_collect_state`` が同じ「幅 0 = OFF」規約で書く。
        """
        sizes = self._splitter.sizes()
        if len(sizes) != 3:
            return
        if not self._nav_rail.isHidden():
            if sizes[0] >= _NAV_RAIL_DEFAULT_WIDTH:
                self._nav_rail_saved_width = sizes[0]
            self._set_nav_rail_checks(sizes[0] > 0)
        if not self._info_panel.isHidden():
            if sizes[2] >= _INFO_PANEL_DEFAULT_WIDTH:
                self._info_panel_saved_width = sizes[2]
            self._set_info_panel_checks(sizes[2] > 0)
        # 席が 0 を跨いだサンプルでは空状態の裁定入力が変わる（畳んだ席は
        # 主案内を持てない）— トグル 3 本と同じ再裁定をここでも通す。
        self._resync_placeholder_on_seat_change()

    # ------------------------------- プレビュー列トグル（折り畳み導線 2026-07）

    def _on_preview_toggled(self, visible: bool) -> None:
        """Show/hide the centre preview column (F6 / 表示 popover check /
        toolbar pane button).

        OFF = 分割スプリッタのプレビュー席を幅 0 に畳む（現在比率は
        ``_center_split_saved`` に保存）/ ON = 記憶比率で復元。**最大化との
        相互作用**: 最大化中の OFF はまず正規の出口（``_exit_stage_to_browse``
        — 履歴対称性込み）で分割へ戻してから畳む — グリッドが消えたまま
        真っ黒にならない。畳み中の再最大化（E / activate）は
        ``_set_preview_focus`` 側が自動再表示 + checked 追従する。
        ``setSizes`` は ``splitterMoved`` を発火しないので
        ``_on_center_split_moved`` と干渉しない。
        """
        if self._syncing_preview:
            return
        self._syncing_preview = True
        try:
            split = self._center_split
            if visible:
                if self._ui_mode != "stage":
                    sizes = split.sizes()
                    if len(sizes) == 2 and sizes[1] == 0:
                        saved = self._center_split_saved
                        if not (
                            len(saved) == 2 and saved[0] > 0 and saved[1] > 0
                        ):
                            saved = list(_CENTER_SPLIT_DEFAULT_SIZES)
                        self._apply_center_split_sizes(saved)
            else:
                if self._ui_mode == "stage":
                    self._exit_stage_to_browse()
                sizes = split.sizes()
                if len(sizes) == 2 and sizes[1] > 0:
                    if sizes[0] > 0:
                        self._center_split_saved = list(sizes)
                    split.setSizes([max(1, sum(sizes)), 0])
        finally:
            self._syncing_preview = False
        self._set_preview_visible_flag(visible)
        # プレビュー席の可視性は空状態オーケストレータの入力（畳まれた席は
        # 主案内を持てない）。畳んでいる間に状態が変わっていても、開き直した
        # 瞬間に正しい割当へ揃うようにする。
        self._sync_centre_placeholder()

    def _set_preview_visible_flag(self, visible: bool) -> None:
        """プレビュー列の表示状態フラグ + 全トグル UI の checked + 永続値を
        *visible* へ揃える（再入ガード付き — ドラッグ追従 /
        ``_set_preview_focus`` からも呼ばれる）。getattr ガードは __init__ を
        通さないテストハーネスと構築順（_build_menus 前）のため。"""
        self._preview_visible = visible
        state = getattr(self, "_state", None)
        if state is not None:
            state.preview_visible = visible
        was = getattr(self, "_syncing_preview", False)
        self._syncing_preview = True
        try:
            act = getattr(self, "_act_preview", None)
            if act is not None:
                act.setChecked(visible)
            grid = getattr(self, "_post_grid", None)
            if grid is not None:
                grid.preview_btn.setChecked(visible)
        finally:
            self._syncing_preview = was

    def _refresh_nav_rail_libraries(self) -> None:
        """Push the current library roots + highlight to the rail.

        Called from :meth:`_rebuild_library_menu` so the rail follows every
        register / manage exactly like the ファイル ▸ ライブラリ submenu.  Reuses
        ``_compute_library_bases`` (default library first, then registered
        roots) — pure path arithmetic, so it stays NAS-free.
        """
        rail = getattr(self, "_nav_rail", None)
        if rail is None:
            return
        rail.set_libraries(self._compute_library_bases(), self._root)

    def _refresh_nav_rail_bookmarks(self) -> None:
        """Push the current bookmarks to the rail (from _rebuild_bookmarks_menu)."""
        rail = getattr(self, "_nav_rail", None)
        if rail is None:
            return
        rail.set_bookmarks(self._state.bookmarks, self._state.bookmark_names)

    def _refresh_nav_rail_saved_searches(self) -> None:
        """Push the saved searches to the rail (from _rebuild_saved_search_menu)."""
        rail = getattr(self, "_nav_rail", None)
        if rail is None:
            return
        searches = self._state.saved_searches
        # 名前だけでは中身も適用範囲も分からない —
        # 条件サマリ + 「現在のフォルダを起点に適用」をレール行にも配る
        # （メニュー・管理ダイアログと同じ 1 本の要約から）。
        rail.set_saved_searches(
            searches, [saved_search_tooltip(e) for e in searches],
        )

    # ------------------------------------ 編集メニュー（選択中の項目）

    def _rebuild_curation_menu(self) -> None:
        """「編集 ▸ スター・あとで見る」 — 横断一覧への入口.

        レール行と同じ 3 種（スター付き / あとで見る / 各ユーザータグ）を同じ
        1 本の情報源（``PostGrid.curation_pool_count`` / ``all_user_tags``）から
        組む。``aboutToShow`` で組み直すのは、タグの増減と件数が編集のたびに
        変わるため。
        """
        menu = getattr(self, "_curation_menu", None)
        if menu is None:
            return
        menu.clear()
        grid = self._post_grid
        if self._user_meta is None:
            menu.setEnabled(False)
            return
        menu.setEnabled(True)
        kinds = CurationList.kinds(grid.all_user_tags())
        for kind in kinds:
            label = t(
                "viewer.nav_rail.curation_row_count",
                label=grid.curation_view_label(kind),
                n=grid.curation_pool_count(kind),
            )
            act = QAction(label, self)
            # 件数の意味（印を付けた数）と、同じ行き先が**レールにも常設**で
            # あることを添える。メニューは
            # レールを畳んでいる利用者の唯一の入口なので削らず、2 つが同じ
            # 行き先だと分かるようにする。
            act.setToolTip(
                "\n".join((
                    t(
                        "viewer.nav_rail.curation_row_count_hint",
                        n=grid.curation_pool_count(kind),
                    ),
                    t("viewer.nav_rail.curation_row_rail_hint"),
                ))
            )
            act.triggered.connect(
                lambda _checked=False, k=kind: self._on_rail_curation_requested(k)
            )
            menu.addAction(act)

    def _sync_edit_menu(self) -> None:
        """編集メニューを現在の対象へ同期する（``aboutToShow``）.

        選択に束縛された共通エントリ操作（右クリックと同じ builder）を組み直す
        — ``QAction`` がパスをクロージャに持つので、状態更新では済まない。
        前回分は自分が作ったものだけを外すので、プラグインが
        ``PluginContext.get_menu("edit")`` で足した項目（公開契約 MENU_IDS）
        には触れない。
        """
        grid = self._post_grid
        menu = getattr(self, "_edit_menu", None)
        if menu is None:
            return
        for act in self._edit_entry_actions:
            menu.removeAction(act)
        self._edit_entry_actions = []
        # 対象は「動詞 × 席」の唯一の判定点 ``resolve_target``。マウスで開いた
        # ときは焦点が席に残り、キーボードモードでメニューバーが焦点を握って
        # いるときは ``focused_seat`` が直前の席を引き継ぐ — 開き方で対象が
        # 変わらない。席の外（ツールバー等）からは既定（最大化中はプレビュー、
        # 分割ビューではグリッドの選択）。
        target = resolve_target(self)
        if target is None:
            return
        # 右クリックと同一の builder で組み、その QAction 群をこのメニューへ
        # 移す。host は寿命を持たせるためだけに保持する（表示はしない）。
        host = QMenu(self)
        old_host, self._edit_entry_host = self._edit_entry_host, host
        if old_host is not None:
            old_host.deleteLater()
        grid.populate_entry_menu(host, target)
        # 既存項目との区切りは builder の見出し（先頭に差し込まれる）より前 —
        # host 側に作って一緒に移す（メニュー側に作ると host と違って回収
        # されず、開くたびに区切りの QAction が溜まる）。
        if host.actions():
            host.insertSeparator(host.actions()[0])
        for act in host.actions():
            menu.addAction(act)
            self._edit_entry_actions.append(act)

    def _on_toggle_later(self, checked: bool = False) -> None:
        """``L`` — フォーカスのある席の対象の「あとで見る」を反転する.

        対象は ``resolve_target``（0-5 / 右クリックと同じ判定点）。書き込みと
        確認トーストは左ペインの funnel ``PostGrid.request_curation`` が持つ。
        全画面は別窓で ``L`` が届かないため ``LightboxWindow`` 自身が受ける。
        """
        self._toggle_later_target(resolve_target(self))

    def _toggle_later_target(self, target: "Target | None") -> None:
        if self._user_meta is None or target is None:
            return
        lb = getattr(self, "_lightbox", None)
        if target.seat == SEAT_LIGHTBOX and lb is not None:
            # 席が全画面（= 全画面がアクティブ、または両窓とも非アクティブで
            # 判定材料が無い）なら、告知（中央オーバーレイ）を持つ全画面自身の
            # 経路へ委ねる — ここから直接書くとトーストも出ない無音の書き込みに
            # なる。本窓がアクティブなら ``focused_seat`` が
            # 本窓の席を返すので、この分岐には来ずに下の funnel（本窓のトースト）
            # で告知される。
            lb._toggle_later()
            return
        meta = self._post_grid.user_meta_for(target.path)
        want = not bool(getattr(meta, "later", False))
        self._post_grid.request_curation(target.path, "later", want)

    def _on_app_focus_changed(self, _old, _new) -> None:
        """席にフォーカスが着地するたびに覚える（``focused_seat`` の引き継ぎ元）."""
        seat = focused_seat(self)
        if seat is not None and seat != SEAT_LIGHTBOX:
            self._last_focus_seat = seat

    def _leave_stage_for_overlay(self) -> None:
        """``PostGrid.population_replacing`` の受け口 — 分割ビューへ戻す.

        入れ替え（横断キュレーション一覧 / 保存した検索の適用 / 最近追加された
        ファイル）は検索状態の破棄・並び順の切替・パンくずの置換・全パスの
        off-thread 解決を伴うのに、最大化中はグリッド席が幅 0 なのでそれが
        1px も見えない。入口はどれも最大化中に撃てる（ナビレールは外殻
        スプリッタに残るので最大化中も可視・編集メニューは窓ショートカット）
        ため、**入れ替え前に必ず分割へ戻す**。抜け方は G / Esc / ヘッダーと
        同じ正規の出口（履歴の取り崩し込み）。

        窓側の入口ごとに前置きを書くのではなく、入れ替えを実行するグリッドに
        告げさせて配線 1 本で受ける — 新しい入口（プラグイン API・新メニュー）
        が増えても片側だけ欠けることがない。

        getattr ガードは __init__ を通さないテストハーネス向け。
        """
        if getattr(self, "_ui_mode", "browse") != "browse":
            self._exit_stage_to_browse()

    def _on_rail_curation_requested(self, kind: str) -> None:
        """ナビレールの「キュレーション」行 → 横断ビュー.

        メニューの 「スター付き一覧」/「あとで見る一覧」 と同じ入口
        （``enter_curation_view``）へ流すだけ — レール側はダムビュー。
        """
        self._post_grid.enter_curation_view(kind)
        # 一覧へ入るだけ = 母集合は不変。現在地ハイライトだけ書き直す。
        self._sync_nav_rail_curation(counts=False)

    def _on_recent_files_requested(self) -> None:
        """表示メニュー「最近追加されたファイル」 (Ctrl+Shift+N) — 対象を決めて開く.

        対象は **左グリッドで選択中のフォルダ**、選択が無い（またはファイルを
        選んでいる）ときは **いま開いているルート**。「選択中のフォルダ内の
        すべてのファイル」を素直に読むとこの 2 通りしかなく、どちらでも「いま
        見ている場所の新着」になる。フォルダ右クリックの同名項目は対象が自明な
        ぶんこの推測を挟まない。

        すでに一覧を開いているときは**その一覧のフォルダ**が対象（= 再走査）。
        一覧の母集合はファイルだけなので ``selected_folder()`` が必ず ``None``
        になり、素通しだとルート（ライブラリ全体）へ黙って乗り換えてしまう —
        入場は履歴を積まないので、元の一覧には「戻る」でも帰れない。

        最大化中でも撃てるウィンドウショートカットだが、分割ビューへ戻すのは
        グリッド側の ``population_replacing`` を受けた
        :meth:`_leave_stage_for_overlay` — ここで前置きは書かない。
        """
        target = (
            self._post_grid.current_recent_view()
            or self._post_grid.selected_folder()
            or self._root
        )
        if target is None:
            return
        self._post_grid.enter_recent_files_view(target)

    def _sync_nav_rail_curation(self, *, counts: bool = True) -> None:
        """レールのキュレーション行を現在の横断ビュー状態へ同期する.

        「現在地」表示は選択とは独立した描画なので、
        グリッドの再構築（``counts_changed``）と ``curation_changed`` に相乗り
        して入場・退場・件数変化のどれにも追従できる。

        件数とユーザータグ行はどちらも
        左ペインのメモリ上の ``_user_meta_map`` 由来（``curation_pool_count`` /
        ``all_user_tags``）なので I/O はゼロ — レールは相変わらずダムビューで、
        自分では何も読まない。

        *counts* が ``False`` のときは**現在地だけ**を書き直す。母集合
        (``_user_meta_map``) が変わるのは ``_apply_curation`` と
        ``refresh_user_meta`` の 2 か所だけなのに、``counts_changed`` は
        絞り込みの 1 打鍵ごとに飛ぶので、``all_user_tags`` + ``2 + タグ数``
        回のマップ全走査を打鍵ごとに繰り返すことになる。``_apply_tab_order(only_if_changed=True)`` が同じ経路で
        採っている「変わったときだけ歩く」の姉妹。現在地は
        ``enter_curation_view`` / ``enter_recent_files_view`` 経由でも動くので、
        ``counts=False`` でも必ず書き直す（落とすとレールの現在地
        表示が追従しなくなる）。
        """
        rail = getattr(self, "_nav_rail", None)
        if rail is None:
            return
        if self._user_meta is None:
            # 店が無い構成は行ごと消す。走査を伴わないので *counts* に依らず
            # 毎回通してよい（そもそも件数を数える相手が居ない）。店が開けな
            # かった理由があるときは、レールの空文言も「付ければ一覧できます」
            # から「保存できません」へ差し替える（理由の詳細は起動時の
            # 警告トーストが 1 度だけ出す）。
            rail.set_curation(
                False, reason=getattr(self, "_user_meta_error", None)
            )
            rail.set_current_curation(None)
            return
        grid = self._post_grid
        if counts:
            tags = grid.all_user_tags()
            kinds = CurationList.kinds(tags)
            rail.set_curation(
                True,
                {kind: grid.curation_pool_count(kind) for kind in kinds},
                tags,
            )
        rail.set_current_curation(grid.current_curation_view())

    def _refresh_info_meta(self, folder: Path | None) -> None:
        """Update the 情報パネル meta card for *folder* (off-thread read).

        Clears the card immediately (no stale flash while navigating), then —
        when *folder* isn't already known to lack a ``post.md`` — dispatches an
        off-thread :func:`read_post_meta_checked`; :meth:`_on_info_meta_read` caches the
        result and calls :meth:`_apply_meta_card`, which decides whether to show
        it (collapsed while the stage shows that same post.md).
        Token-guarded so a rapid selection change drops the superseded read.
        """
        # 走っている読みを降ろす（このあと投げ直すとは限らない — 下の 2 つの
        # 早期 return がその枝）。
        self._info_meta_stream.cancel()
        # 選択が動いたので post.md 再読込のリトライ予約はご破算（新しい読みが走る）。
        self._info_meta_retried = False
        self._info_meta_retry_timer.rearm()
        self._info_meta_last = None
        self._info_panel.set_post_meta(None)
        # ステージヘッダーのタイトル上書きも同じ読み取りに載せる（フォルダが
        # 替わったので一旦フォルダ名へ戻し、読めたら実タイトルに差し替える）。
        self._info_meta_folder = folder
        self._stage_title_override = None
        self._update_stage_header()
        if folder is None:
            return
        # Skip the read when the grid's already-loaded entry says "no post.md"
        # (no GUI-thread I/O — a cheap in-memory lookup).  Unknown / not-yet
        # enriched entries fall through to the off-thread read, which returns
        # None for a folder without a post.md anyway.
        entry = self._post_grid.entry_for(folder)
        if entry is not None and entry.metadata_loaded and not entry.has_post_md:
            return
        self._submit_info_meta_read(folder)

    def _submit_info_meta_read(self, folder: Path) -> None:
        """post.md のメタ読みを投入する**唯一の口**（初回 / 失敗後のリトライ）.

        初回とリトライの 2 箇所から呼ぶ。リトライは
        「いま最新の要求」なので、追い越し（``submit``）で新しい世代を取る
        のが正しい — 着地は通常経路 :meth:`_on_info_meta_read` に乗る。
        """
        self._info_meta_stream.submit(
            lambda p=folder / POST_MD_NAME: read_post_meta_checked(p)
        )

    def _on_info_meta_read(self, payload: object) -> None:
        # payload = (ParsedPost | None, retryable) from read_post_meta_checked;
        # a non-tuple means the worker itself raised — treat as retryable.
        parsed: ParsedPost | None = None
        retryable = True
        if isinstance(payload, tuple) and len(payload) == 2:
            maybe, flag = payload
            parsed = maybe if isinstance(maybe, ParsedPost) else None
            retryable = bool(flag)
        # 一過性の read 失敗（共有の瞬断・書き込み中の並走読みで
        # 空 head 等）だと post.md が実在してもカードが選択変更まで出ない。
        # read が「失敗」と報告したか、スキャン済みエントリが has_post_md=True
        # と知っているのに None が返ったときだけ、短い backoff 後に 1 回に
        # 限り再読込する（リトライ待ちの間も None を確定させる —
        # カードは畳まれたまま、リトライ着地で上書きされる）。
        if parsed is None and not self._info_meta_retried:
            folder = self._info_meta_folder
            entry = (
                self._post_grid.entry_for(folder) if folder is not None else None
            )
            known_present = (
                entry is not None and entry.metadata_loaded and entry.has_post_md
            )
            if folder is not None and (retryable or known_present):
                self._info_meta_retried = True
                self._info_meta_retry_timer.trigger()
        # Cache + apply through the single funnel so the double-display
        # suppression (stage showing this post.md) is honoured whether it's already true now or
        # becomes true on a later mode change.
        self._info_meta_last = (self._info_meta_folder, parsed)
        self._apply_meta_card()
        # ステージヘッダー: post.md の実タイトルが判明したら現在地表示を
        # フォルダ名 → タイトルへ格上げする（同じ off-thread 読みの副産物 —
        # 追加 I/O ゼロ）。タイトルが無い/読めない投稿はフォルダ名のまま。
        if (
            parsed is not None
            and parsed.title.strip()
            and self._info_meta_folder is not None
        ):
            self._stage_title_override = (
                self._info_meta_folder, parsed.title.strip(),
            )
        else:
            self._stage_title_override = None
        self._update_stage_header()

    def _retry_info_meta(self) -> None:
        """backoff 後の post.md 再読込（1 選択につき 1 回限り）.

        タイマーは :meth:`_refresh_info_meta`（= 選択変更）で必ず停止される
        ので、発火時の ``_info_meta_folder`` は予約時と同じ選択を指している。
        着地は通常経路 :meth:`_on_info_meta_read` に乗り、
        ``_info_meta_retried`` が立っているので再々読みはしない。
        """
        folder = self._info_meta_folder
        if folder is None:
            return
        self._submit_info_meta_read(folder)

    def _apply_meta_card(self) -> None:
        """Show / collapse the 情報パネル post meta card from live state.

        The card mirrors the selected post's ``post.md`` metadata — but while
        the preview already shows that same ``post.md`` body, the two are an
        identical 7-row double display, so the card is
        collapsed in exactly that case.  条件は「プレビューが当該 post.md
        本文を表示中」だけ（モード非依存 — 分割ビューでもプレビューは常時可視）。A post shown as its representative
        image keeps the card (a file selection additionally hides it via the
        panel's own file-card / meta-card mutual exclusivity).  Cheap +
        idempotent, so it can be re-run on every selection change.
        """
        # getattr ガードは __init__ を通さないテストハーネス向け
        # （``_capture_current_position`` 等と同じ流儀）— ``_on_curation_changed``
        # からも呼ばれるので、素の窓でも安全に落ちること。
        last = getattr(self, "_info_meta_last", None)
        if last is None:
            return  # no post.md read yet — card already cleared
        folder, parsed = last
        showing_md = (
            folder is not None
            and self._current_preview_path == folder / POST_MD_NAME
        )
        self._info_panel.set_post_meta(None if showing_md else parsed)

    def _on_read_post_body(self) -> None:
        """メタカードの「本文を読む」→ 選択中投稿の post.md をプレビューへ。

        まず右一覧の post.md 行の選択を試みる（既存の
        ``file_selected`` → ``show_path`` 経路に乗るので、画像トラック /
        ヘッダー / 二重表示抑止が全部自動で追従する）。スキャン未着地等で行が
        まだ無ければ直接 ``show_markdown`` へフォールバックする。
        """
        last = self._info_meta_last
        folder = last[0] if last is not None else self._current_folder
        if folder is None:
            return
        post_md = folder / POST_MD_NAME
        if self._file_list.select_path(post_md):
            return
        # 右一覧がまだ着地していない（コールド NAS / CI 負荷）。行が並んだ
        # 時点で post.md を選ぶよう pending に積み替える — 積み替えないと、
        # フォルダ選択が積んだ**代表画像**の pending が着地時に解決されて
        # ``file_selected`` → ``show_path`` がいま出した本文を画像で奪い返す
        # （負荷下の反復実行で実測）。ユーザー操作は飛行中の
        # 自動選択を陳腐化させる（右一覧の選択を経由しない経路）。同じ理由で
        # 代表画像プローブも失効させる
        # （``_on_file_selected`` の明示選択と同じ扱い）。
        self._file_list.set_pending_select(post_md)
        self._set_path_status(post_md)
        self._content.show_markdown(post_md)
        # プレビューが本文になったのでメタカードの二重表示抑止を再評価する。
        self._apply_meta_card()

    # ------------------------------------------------ file-detail card (右)

    def _refresh_file_detail(self, path: Path | None) -> None:
        """Drive the 情報パネル file-detail card for the selected *path*.

        ``None`` (or a marker/meta file — ``post.md`` / ``#thumb#…``) hides the
        card: those aren't "content" the user selected to view, and ``post.md``
        is represented by the centre markdown + the post meta card instead.  A
        real file shows the card immediately (name / path / star + a resident
        pane thumbnail if one is decoded), then fills 種別 / サイズ / 更新日時 /
        解像度 from an off-thread read and, if no thumbnail was resident, an
        off-thread strip-loader decode.
        """
        # 走っている読みを降ろす（カードを隠す枝では投げ直さない）。
        self._file_detail_stream.cancel()
        # 「メタ / マーカーか」の判定は folder_scan の単一定義へ委譲する
        # （ここに手書きで写すと大文字小文字の扱いが片側だけ古くなる）。
        if path is None or is_meta_or_marker_name(path.name):
            self._pending_file_detail.clear()
            self._info_panel.set_file_detail(None)
            if path is not None:
                # A meta/marker file (e.g. post.md) was explicitly selected —
                # re-evaluate the double-display suppression now that the preview changed.
                self._apply_meta_card()
            # 印ストリップの対象はファイル詳細の席を読むので、席を空けた後に配り直す。
            self._update_stage_header()
            return
        detail = FileDetail(
            path=path,
            name=path.name,
            star=self._file_star(path),
            user_tags=self._file_user_tags(path),
            later=self._file_later(path),
        )
        self._pending_file_detail.set(detail)
        thumb = self._detail_resident_thumb(path)
        self._info_panel.set_file_detail(detail, thumbnail=thumb)
        if thumb is None:
            self._request_detail_thumb(path)
        self._file_detail_stream.submit(lambda p=path: _read_file_detail(p))
        # A concrete file is selected — the panel hides the meta card while the
        # file card is up; keep the card's own state coherent for when it isn't.
        self._apply_meta_card()

    def _on_file_detail_read(self, payload: object) -> None:
        detail = self._pending_file_detail.peek()
        if detail is None or not isinstance(payload, dict):
            return
        detail.kind = payload.get("kind", "—")
        detail.size_text = payload.get("size_text", "—")
        detail.mtime_text = payload.get("mtime_text", "—")
        detail.resolution_text = payload.get("resolution_text", "")
        # Same path → set_file_detail keeps whatever thumbnail is already shown.
        self._info_panel.set_file_detail(detail)

    def _file_star(self, path: Path) -> int:
        """The user's star (0–5) for *path* from the in-memory map (no I/O)."""
        try:
            star, _later = self._post_grid._curation_badge_for(path)
        except Exception:  # pragma: no cover (defensive — provider absent)
            return 0
        return int(star or 0)

    def _file_user_tags(self, path: Path) -> tuple[str, ...]:
        """The user's own tags for *path* from the in-memory map (no I/O).

        詳細カードがユーザータグの表示面になる（付けたタグが製品のどこにも
        出てこない次元にしない）。
        """
        try:
            meta = self._post_grid.user_meta_for(path)
        except Exception:  # pragma: no cover (defensive — no store)
            return ()
        return tuple(getattr(meta, "tags", ()) or ()) if meta is not None else ()

    def _file_later(self, path: Path) -> bool:
        """The 「あとで見る」 flag for *path* from the in-memory map (no I/O).

        The file card shows ★ + ユーザータグ and this one too, so the three
        curation dimensions are displayed in the same amount on each surface
        that shows them.
        """
        try:
            _star, later = self._post_grid._curation_badge_for(path)
        except Exception:  # pragma: no cover (defensive — provider absent)
            return False
        return bool(later)

    def _detail_resident_thumb(self, path: Path) -> "QPixmap | None":
        """An already-decoded pane thumbnail for *path*, if any (no NAS I/O)."""
        for pane in (self._file_list, self._post_grid):
            pm = pane.pixmap_for_path(path)
            if pm is not None and not pm.isNull():
                return pm
        return None

    def _request_detail_thumb(self, path: Path) -> None:
        """Decode a thumbnail for the file-detail card via the shared loader.

        Only for thumbnailable files (image / video / PDF); other kinds keep the
        blank thumbnail.  Worker returns a ``QImage``; ``_on_stage_thumb_loaded``
        converts to ``QPixmap`` on the GUI thread and hands it to the panel.
        """
        if path.suffix.lower() not in (_LIGHTBOX_MEDIA_SUFFIXES | PDF_SUFFIXES):
            return
        loader = self._ensure_strip_loader()
        size = QSize(_DETAIL_THUMB_EDGE, _DETAIL_THUMB_EDGE)
        key = self._DETAIL_KEY_PREFIX + str(path)
        loader.request(
            key, path, size, dpr=self.devicePixelRatioF(),
        )
        # ソースがボックスより小さいキーの再要求にローダーは沈黙する
        # （「呼び出し側が既に絵を持っている」前提の契約）。詳細カードは
        # ``set_file_detail`` が選択のたびにサムネ枠を空へ戻すので持っていない
        # — 常駐デコードがあれば ``cached_image`` から明示的に再シードする
        # （``children_grid`` / ``folder_preview_view`` と同じ流儀）。
        image = loader.cached_image(key)
        if image is not None and not image.isNull():
            self._info_panel.set_file_thumbnail(path, QPixmap.fromImage(image))

    # -------------------------------- preview image track (split-view 2026-07)

    _IMG_STRIP_KEY_PREFIX = "stage-imgstrip::"
    _DETAIL_KEY_PREFIX = "info-detail::"

    def _on_file_list_tiles_changed(self) -> None:
        # Right pane rebuilt (scan landing / re-sort) — keep the image track
        # in sync (a cheap no-op while the split view is showing) and the
        # header's n/m position fresh (updated in both modes).  分割中は帯の
        # 同期が冒頭で降りるので、そのときだけヘッダーを直接更新する（
        # 素直に両方呼ぶと最大化中に ``tile_paths()`` の全走査が 3 回走る）。
        if not self._refresh_image_strip():
            self._update_stage_header()

    def _refresh_image_strip(self) -> bool:
        """画像トラック（96px）を右ペインのタイル集合へ同期する。

        プレビュー最大化中のみ（分割中は帯ごと非表示 — 右情報パネルの
        ファイル一覧が同役割）。中身は「現在の投稿（フォルダ）の中身のうち
        **画像・動画**」= ``_stage_media_paths(tile_paths())`` で、ヘッダーの
        ``n/m``（``_stage_position_readout``）と母数を揃える（帯が
        ``.part`` まで並べると「3 枚あるのに 2/2」と読める）。歩く母集合
        ``tile_paths`` そのものは変えない（``playlist ⊆ tile_paths`` の不変
        条件）。パス列不変なら ``set_images`` を呼ばず（呼ぶと取得済み
        サムネが全破棄され、リビルド毎にチラつくため）、1 件以下なら帯を畳む。

        戻り値は「帯を同期した（＝ヘッダーも更新済み）」か。呼び出し元が
        ヘッダー更新を二重に走らせないための合図。
        """
        strip = getattr(self, "_image_strip", None)
        if strip is None or self._ui_mode != "stage":
            return False
        paths = self._stage_media_paths(self._file_list.tile_paths())
        if paths != strip.paths():
            strip.set_images(paths)
        strip.setVisible(len(paths) > 1)
        self._sync_image_strip_current(paths)
        return True

    def _sync_image_strip_current(
        self, paths: "list[Path] | None" = None,
    ) -> None:
        """右ペイン選択（= 表示中ファイル）→ 画像トラックのハイライト追従。

        カプセルの ‹ › / ←→ は右ペインの選択を歩む（``_on_content_navigate``）
        ため、ここに追従させるだけで「送れば帯が動く」が成立する。帯の追従は最大化中のみだが、ヘッダーの ``n/m`` 位置は
        分割中も常時更新する（ヘッダーは常設）。getattr ガードは __init__ を
        通さないテストハーネス向け。

        *paths* は呼び出し元が既に取った右ペインのタイル列（``tile_paths()``
        はキャッシュを持たない全走査なので、あるものは使い回す）。
        帯と同じメディアだけの列を渡してもよい（``_stage_position_readout``
        が自分で ``_stage_media_paths`` を通すので冪等）。
        """
        strip = getattr(self, "_image_strip", None)
        if strip is None:
            return
        if self._ui_mode == "stage":
            current = self._file_list.current_path()
            strip_paths = strip.paths()
            index = -1
            if current is not None:
                try:
                    index = strip_paths.index(current)
                except ValueError:
                    index = -1
            if index != strip.current_index():
                strip.set_current(index)
        self._update_stage_header(paths)

    def _sync_curation_strips(
        self, header, target: "Path | None", panel_target: "Path | None" = None,
    ) -> None:
        """情報パネル / ステージヘッダーの印ストリップへ対象と値を配る（E2）.

        *target* はヘッダー（プレビューが映しているもの）、*panel_target* は
        情報パネル（パネルが説明しているもの — 代表画像を自動表示中なら投稿
        フォルダ）。省略時は同じ。値は左ペインのインメモリ map の同期読み
        （I/O なし）。全画面の席は ``LightboxWindow`` が自分に注入された
        プロバイダから同じ map を読む。店が無いときは 2 席とも
        ``set_store_available(False)`` のまま隠れている。
        """
        panel = getattr(self, "_info_panel", None)
        if panel_target is None:
            panel_target = target
        # getattr ガード: __init__ を通さないテストハーネス（stage header 単体）。
        if getattr(self, "_user_meta", None) is None:
            header.set_curation(None)
            if panel is not None:
                panel.set_curation_target(None)
            return
        for seat, seat_target in ((header, target), (panel, panel_target)):
            if seat is None:
                continue
            if seat_target is None:
                seat.set_curation(None) if seat is header else seat.set_curation_target(None)
                continue
            values = (
                self._file_star(seat_target),
                self._file_later(seat_target),
                self._file_user_tags(seat_target),
            )
            if seat is header:
                header.set_curation(seat_target, *values)
            else:
                panel.set_curation_target(
                    seat_target, *values,
                    display_name=self._location_label(seat_target),
                )

    def _update_stage_header(
        self, paths: "list[Path] | None" = None,
    ) -> None:
        """プレビューヘッダーの現在地（タイトル）と ``n/m`` 位置を更新する。

        ヘッダーは分割・最大化を問わず常設なので、モードに関わらず更新する。
        タイトルは現フォルダ名を即時表示し、post.md の実タイトルが非同期の
        info-meta 読み取りで判明したらそれに差し替える（``_on_info_meta_read``
        が ``_stage_title_override`` を積んで再度ここを呼ぶ）。``n/m`` は
        右ペインのタイル列（= ‹ ›/←→ の画像送りが歩む列）内の現在位置。

        *paths* を渡すと ``tile_paths()`` の再走査を省く。
        """
        header = getattr(self, "_stage_header", None)
        if header is None:
            return
        folder = self._current_folder or self._root
        title = ""
        if folder is not None:
            # 既定ライブラリ / ZIP 展開先はパンくず・履歴と同じ友好名。
            title = self._location_label(folder)
            override = self._stage_title_override
            if override is not None and override[0] == folder and override[1]:
                title = override[1]
        if paths is None:
            paths = self._file_list.tile_paths()
        current = self._file_list.current_path()
        position, position_tooltip, back_to_media = self._stage_position_readout(
            paths, current,
        )
        header.set_context(
            title, position, position_tooltip, back_to_media=back_to_media,
        )
        # 全画面ボタンの予告: 非メディア**ファイル**表示中の F11 は
        # フォルダ流し見へ落ちる。判定は ``_toggle_lightbox`` と共有する。
        header.set_fullscreen_folder_mode(self._preview_is_non_media_file())
        # 現在★の表示: 0-5 キーの対象が「投稿
        # フォルダ」か「プレビュー中の画像ファイル」かはフォーカス位置で
        # 変わるため、いま何に何個付いているかを見えるようにする。対象は
        # プレビューが実際に映しているもの（= 右ペインの選択、無ければ
        # 現在フォルダ）。**ヘッダーのストリップが実際に出るのは最大化中
        # かつ幅が足りるときだけ**（``stage_view._sync_strip``）で、分割
        # ビューでは情報パネル側の席しか見えない — ここは席の可視性を
        # 判定せず対象と値を配るだけなので、「常設」と読まないこと。
        subject = folder
        if self._current_folder is None and (
            self._post_grid.current_curation_view() is not None
            or self._post_grid.current_recent_view() is not None
        ):
            subject = None  # 横断一覧で無選択: ライブラリ根を印の対象にしない
        star_target = self._current_preview_path or self._file_list.current_path()
        if star_target is None or star_target == folder:
            star_target = subject
        if star_target is not None:
            # 投稿本文（post.md）を表示中は投稿フォルダの印。
            star_target = curation_subject(star_target)
        # 情報パネルの席は「パネルが説明しているもの」に付く（カードの排他と
        # 同じ規則 — ``_refresh_file_detail`` の対象）: ファイルカードが出て
        # いればそのファイル、出ていなければ投稿フォルダ自身（代表画像を自動で
        # 映しているだけのときも同じ）。ヘッダーの席はプレビューが映している
        # もの（= プレビューでの 0-5 と同じ対象）。
        detail = self._pending_file_detail.peek()
        panel_target = detail.path if detail is not None else (subject or star_target)
        self._sync_curation_strips(header, star_target, panel_target)
        # 送りボタンの活性: 歩ける先が無いとき
        # （空グリッド・端）に押せる見た目のまま無反応にしない。``step_selection``
        # と同じ母集団（グリッドのタイル列）・同じ規則で判定する — 無選択でも
        # ``step_selection`` は端のタイルを掴んで動くので、タイルさえあれば
        # 両方向とも歩ける（無効化するのは空グリッドだけ）。
        grid_view = getattr(self._post_grid, "_view", None)
        if grid_view is None:
            header.set_step_enabled(False, False)
            return
        count = grid_view.tile_count()
        index = grid_view.selected_index()
        if index < 0:
            header.set_step_enabled(count > 0, count > 0)
        else:
            header.set_step_enabled(
                0 < index < count,
                0 <= index < count - 1,
            )

    def _preview_is_non_media_file(self) -> bool:
        """表示中が「画像・動画ではない**ファイル**」か（全画面ボタンの予告条件）.

        フォルダ選択（代表画像プレビュー / フォルダプレビュー）は対象外 —
        そこでの F11 は元々「このフォルダを流し見」が素直な期待で、G05 の
        設計どおり。予告が要るのは ``.part`` / ZIP / PDF / テキストのように
        **ファイルを見ているのに別のファイルが開く**ケースだけ。

        判定は I/O なし（右ペインの既知エントリと現在フォルダの比較のみ）。
        """
        preview = self._current_preview_path
        if preview is None:
            return False
        if preview.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            return False
        if preview == self._current_folder or preview == self._root:
            return False
        file_list = getattr(self, "_file_list", None)
        entry = file_list.entry_for(preview) if file_list is not None else None
        return not (entry is not None and entry.is_dir)

    @staticmethod
    def _stage_media_paths(paths: "list[Path]") -> "list[Path]":
        """*paths*（右ペインのタイル列）のうち画像・動画だけを表示順で返す。"""
        return [
            p for p in paths
            if p.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES
        ]

    def _stage_position_readout(
        self, paths: "list[Path]", current: "Path | None",
    ) -> "tuple[str, str, bool]":
        """ヘッダーの位置カウンタ ``(表示文字列, ツールチップ, 戻り導線)``.

        **表示層だけをメディア基準にする**。歩く母集合そのもの（``ChildrenGrid.tile_paths`` = ‹ ›/←→ と
        選択同期が使う列）は**変えない** — その列は ``playlist ⊆ tile_paths``
        の不変条件を担っており、全画面を閉じたときの着地ファイルが
        必ず右ペインのタイルとして存在することを保証しているため。ここで
        変わるのはヘッダーに見える数字だけで、``.part`` / ZIP / PDF /
        テキストのような非メディアを数に含めないので、分割・最大化と全画面の
        n/m が食い違わなくなる。

        表示中が画像・動画でないときは位置を出さず、代わりに:

        * ``post.md`` 本文 → 「投稿本文」+ 戻り導線 True（post.md は
          母集合の外なので n/m もトラックのハイライトも消え、何も出さないと
          最大化中の現在地が完全に無所属になる）
        * フォルダ（右一覧のサブフォルダ行）→ 「フォルダ」
        * ``.part`` → 「ダウンロード途中」
        * それ以外の非メディア → 種別ラベル（「ZIP ファイル」…）
        """
        if current is None:
            return "", "", False
        media = self._stage_media_paths(paths)
        if current.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            try:
                index = media.index(current)
            except ValueError:
                # 母集合の外のメディア（フィルタ途中など）— 嘘の位置は出さない。
                return "", "", False
            return (
                t("viewer.stage_view.position", n=index + 1, m=len(media)),
                t("viewer.stage_view.position_tooltip"),
                False,
            )
        if current.name.lower() == POST_MD_NAME:
            return (
                t("viewer.stage_view.position_post_body"),
                t("viewer.stage_view.position_post_body_tooltip"),
                bool(media),
            )
        # 右一覧のサブフォルダ行を選ぶと ``current`` はディレクトリになる。
        # 拡張子をそのまま種別にすると「作品集 vol.2」が種別「2」になる。
        # 判定はインメモリの ``entry_for`` のみ — GUI スレッドで
        # ``is_dir()`` を呼ぶと到達不能 NAS で窓が止まる。未 populate で
        # ``None`` のときは種別ラベルへ落ちる。
        entry = self._file_list.entry_for(current)
        if entry is not None and entry.is_dir:
            return (
                t("common.label.folder"),
                t("viewer.stage_view.position_folder_tooltip"),
                False,
            )
        if current.suffix.lower() == PART_SUFFIX:
            # 拡張子をそのまま見せると「PART」としか読めない。
            # 書きかけファイルであることは健全性チェックと同じ語彙で言う。
            return (
                t("viewer.stage_view.position_part"),
                t("viewer.stage_view.position_part_tooltip"),
                False,
            )
        suffix = current.suffix.lstrip(".").upper()
        if not suffix:
            # 拡張子が無いファイル — 「{kind} ファイル」に埋める語が無い。
            kind = t("common.label.file")
            return (
                kind,
                t("viewer.stage_view.position_kind_tooltip", kind=kind),
                False,
            )
        return (
            t("viewer.stage_view.position_kind", kind=suffix),
            t("viewer.stage_view.position_kind_tooltip", kind=suffix),
            False,
        )

    def _on_stage_back_to_media(self) -> None:
        """post.md 本文表示中の「画像に戻る」→ 先頭の画像・動画を選び直す."""
        media = self._stage_media_paths(self._file_list.tile_paths())
        if media:
            self._file_list.select_path(media[0])

    def _reset_filmstrip_failures(self) -> None:
        """画像トラックの失敗確定セルを再試行対象へ戻す.

        F5 リロード / ``notify_library_changed`` の再スキャン入口から呼ぶ。
        パス列が不変の再スキャンでは ``_refresh_image_strip`` が
        ``set_images`` を呼ばない（取得済みサムネの破棄防止）ため、失敗
        グリフだけを選択的に落とさないとファイル修復後も永久に残る。
        getattr ガードは __init__ を通さないテストハーネス向け（set_root 系
        と同じ）。
        """
        strip = getattr(self, "_image_strip", None)
        if strip is not None:
            strip.reset_failed()

    def _on_image_strip_clicked(self, index: int) -> None:
        # 画像トラックのセルクリック → 右ペイン選択を切り替え。その
        # ``file_selected``/``folder_selected`` が中央プレビューを駆動する
        # （‹ ›/←→ と同じ経路なのでハイライト同期も自動で追従する）。
        paths = self._image_strip.paths()
        if 0 <= index < len(paths):
            self._file_list.select_path(paths[index])

    def _ensure_strip_loader(self) -> ThumbnailLoader:
        """フィルムストリップ用の小さな専用ローダー（遅延生成）。

        ライトボックスの ``_lightbox_loader`` と同じ構成: 2 ワーカー +
        永続ディスク / フォルダキャッシュ共有。ワーカーは QImage のみを返し、
        QPixmap 変換は ``_on_stage_thumb_loaded``（GUI スレッド）で行う。
        """
        if self._strip_loader is None:
            self._strip_loader = ThumbnailLoader(
                cache_size=128,
                max_threads=2,
                parent=self,
                disk_cache=self._disk_cache,
                cache_edge=self._state.thumb_disk_cache_max_edge,
                folder_cache=self._folder_cache,
            )
            self._strip_loader.loaded.connect(self._on_stage_thumb_loaded)
            # デコード失敗（破損画像 / フォルダに絵が無い）を確定させる。
            # 未接続だと失敗セルが永久プレースホルダのまま「読み込み中」に見え続け、
            # 一度 request したパスは再要求もされない。グリッド側 C03
            # （mark_thumb_failed → 静的グリフ）と同じ劣化に揃える。
            self._strip_loader.failed.connect(self._on_stage_thumb_failed)
        return self._strip_loader

    def _on_image_strip_thumb_requested(self, path_obj: object) -> None:
        """画像トラックのサムネ供給（``request_thumb`` → ``set_thumb`` 契約）。

        右ペインの常駐 pixmap を最優先（NAS 往復ゼロ・右ペインと同じ絵）。
        無いものはフォルダ→resolve-in-folder / デコード可能ファイル→直接
        デコードで共用 ``_strip_loader`` へ。post.md 等のサムネ不能ファイルは
        「読み込み中」に見える永久プレースホルダを避け、静的ドキュメント
        グリフで即確定させる（デコード失敗の ✗ とは区別）。
        """
        path = path_obj if isinstance(path_obj, Path) else None
        if path is None:
            return
        pm = self._file_list.pixmap_for_path(path)
        if pm is not None and not pm.isNull():
            self._image_strip.set_thumb(path, pm)
            return
        loader = self._ensure_strip_loader()
        edge = self._image_strip.THUMB_EDGE
        size = QSize(edge, edge)
        dpr = self.devicePixelRatioF()
        key = self._IMG_STRIP_KEY_PREFIX + str(path)
        entry = self._file_list.entry_for(path)
        if entry is not None and entry.is_dir:
            if entry.thumbnail_resolved and entry.thumbnail_path is not None:
                loader.request(key, entry.thumbnail_path, size, dpr=dpr)
            else:
                loader.request(
                    key, path, size, resolve_in_folder=True, dpr=dpr,
                    folder_mtime=entry.mtime,
                )
            self._reseed_image_strip_thumb(loader, key, path)
            return
        if path.suffix.lower() in (_LIGHTBOX_MEDIA_SUFFIXES | PDF_SUFFIXES):
            loader.request(key, path, size, dpr=dpr)
            self._reseed_image_strip_thumb(loader, key, path)
            return
        # サムネ化できない種別（post.md / テキスト / アーカイブ等）。
        self._image_strip.set_thumb(
            path, self._stage_doc_glyph(self._image_strip)
        )

    def _reseed_image_strip_thumb(
        self, loader: ThumbnailLoader, key: str, path: Path,
    ) -> None:
        """常駐デコードから画像トラックのセルを再シードする。

        ローダーの「ソースがボックスより小さいキー（source-limited）の再要求
        には沈黙する」契約は、呼び出し側が既にその絵を持っていることを前提に
        している（``ThumbnailLoader.cached_image`` の docstring）。
        ``FilmstripView.set_images`` は ``_pixmaps`` / ``_requested`` を全破棄
        するので、フォルダを離れて戻った帯はその前提を満たさない —
        ``THUMB_EDGE`` 未満のソースは ``request`` が無言 return し、セルが
        「読み込み中」のプレースホルダで固定される。他ホスト
        （children_grid / folder_preview_view）と同じ流儀でここを塞ぐ。

        ローダーの常駐 LRU を覗くだけの読み取り専用 peek なので NAS 往復は
        無い。GUI スレッド専用（QPixmap 生成のため）。
        """
        image = loader.cached_image(key)
        if image is not None and not image.isNull():
            self._image_strip.set_thumb(path, QPixmap.fromImage(image))

    def _on_stage_thumb_loaded(self, key: str, image: QImage) -> None:
        # QPixmap 変換は GUI スレッド（このスロット）でのみ行う。プレフィクス
        # で画像トラック / 情報パネル詳細サムネのどちら宛かを振り分ける
        # （ローダーは共用）。
        if key.startswith(self._IMG_STRIP_KEY_PREFIX):
            path = Path(key[len(self._IMG_STRIP_KEY_PREFIX):])
            self._image_strip.set_thumb(path, QPixmap.fromImage(image))
            return
        if key.startswith(self._DETAIL_KEY_PREFIX):
            # 情報パネル file-detail card thumbnail (no resident pane pixmap).
            path = Path(key[len(self._DETAIL_KEY_PREFIX):])
            self._info_panel.set_file_thumbnail(path, QPixmap.fromImage(image))

    def _on_stage_thumb_failed(self, key: str) -> None:
        """画像トラックのデコード失敗セルを静的な失敗グリフで確定させる.

        ``FilmstripView`` の空セルは平坦なプレースホルダ塗りで、失敗しても
        「読み込み中」と区別が付かず、一度 request 済みのパスは（``set_images``
        まで）再要求されないため永久にプレースホルダで残る。ここで失敗を確定
        した静的グリフ（テーマ追従・パレット参照）を ``set_thumb`` で供給する
        ことで、① セルが読み込み中に見え続けるのを止め、② pixmap 常駐により
        再要求を抑止する — グリッド側 C03（``mark_thumb_failed`` → 静的グリフ）と
        同じ劣化に揃える。``mark_failed`` は失敗として記録もするので、F5 再スキャン
        （``reset_failed``）でこのセルだけ再デコードへ戻せる（パス列不変の
        再スキャンでは ``set_images`` が呼ばれずグリフが常駐し続けるため）。
        GUI スレッド専用（QPixmap 生成のため）。

        **フォルダは失敗ではない**: 代表画像を
        持たないフォルダは ``resolve_in_folder`` の解決に失敗してここへ来るが、
        それは「絵が無い」だけで破損ではない。凡例に 1 行も無い「×」（慣習的に
        破損を意味する）で描くと、左グリッドが同じ対象を金のフォルダ図像で
        描いているのと 2 面で食い違う。``_stage_doc_glyph`` が post.md 等へ
        ニュートラルな書類図像を供給しているのと**同じ分岐点**で種別を見る。
        """
        # mark_failed 側で path が現タイル集合外なら無視される（スケール前に判定）。
        if key.startswith(self._IMG_STRIP_KEY_PREFIX):
            path = Path(key[len(self._IMG_STRIP_KEY_PREFIX):])
            # getattr ガードは __init__ を通さないテストハーネス向け
            # （本ファイルの既存規約）。
            file_list = getattr(self, "_file_list", None)
            entry = file_list.entry_for(path) if file_list is not None else None
            glyph = (
                self._stage_folder_glyph(self._image_strip)
                if entry is not None and entry.is_dir
                else self._stage_failed_glyph(self._image_strip)
            )
            self._image_strip.mark_failed(path, glyph)
            return
        if key.startswith(self._DETAIL_KEY_PREFIX):
            # 情報パネル詳細カードのサムネ枠（最低 ``_DETAIL_THUMB_EDGE`` の
            # QLabel）も同じ失敗グリフで確定させる（成功側の
            # ``_on_stage_thumb_loaded`` と対 — 片方だけだと破損画像・0 バイト
            # ファイルを選ぶたびに 200px の空白が確定も再試行もされないまま残る）。
            # 宛先が別ファイルへ移っていれば ``set_file_thumbnail`` が no-op。
            path = Path(key[len(self._DETAIL_KEY_PREFIX):])
            self._info_panel.set_file_thumbnail(
                path,
                self._stage_failed_glyph(
                    self._info_panel, edge=_DETAIL_THUMB_EDGE
                ),
            )

    def _stage_glyph_canvas(self, strip, edge: int | None = None) -> "tuple[QPixmap, int]":
        """帯 *strip* のセル寸に合わせた透明キャンバス (pixmap, edge) を作る.

        *edge* を渡すと帯以外（情報パネルの詳細サムネ枠など）の寸法へも
        使い回せる。
        """
        edge = strip.THUMB_EDGE if edge is None else edge
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, round(edge * dpr)), max(1, round(edge * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        return pm, edge

    def _stage_failed_glyph(self, strip=None, *, edge: int | None = None) -> QPixmap:
        """失敗確定セル用の「壊れた画像」グリフを 1 枚描く（テーマ追従）.

        色はフィルムストリップのパレット（``text`` 色の α 変調）から取り、
        ハードコードしない（design.md）。セル全面の淡いプレースホルダ地に、
        中央へニュートラルな「×」を重ねて「読み込み中」の平坦プレースホルダと
        視覚的に区別する。*strip* 省略時は画像トラック。*edge* を渡すと帯以外
        （情報パネルの詳細サムネ枠）の寸法でも同じ絵を描ける。
        """
        from PySide6.QtGui import QColor, QPainter

        strip = strip if strip is not None else self._image_strip
        pm, edge = self._stage_glyph_canvas(strip, edge)
        text_color = strip.palette().text().color()
        bg = QColor(text_color)
        bg.setAlpha(24)
        mark = QColor(text_color)
        mark.setAlpha(120)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(0, 0, edge, edge, bg)
        inset = max(3, edge // 4)
        pen = painter.pen()
        pen.setColor(mark)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(inset, inset, edge - inset, edge - inset)
        painter.drawLine(edge - inset, inset, inset, edge - inset)
        painter.end()
        return pm

    def _stage_folder_glyph(self, strip=None, *, edge: int | None = None) -> QPixmap:
        """代表画像を持たないフォルダ用の中立フォルダ図像.

        失敗の「×」でも書類（``_stage_doc_glyph``）でもなく、**左グリッドと
        同じ金のフォルダ図像**を描いて 2 面の表現を揃える。silhouette は
        バッジ語彙レジストリ（``_indicator.paint_folder_pictogram`` — グリッド
        タイルのフォルダバッジと同一パス）から引くので、新しい手書きグリフは
        増えない。地の淡いプレースホルダ塗りは他のステージ図像と共通。
        """
        from PySide6.QtGui import QColor, QPainter

        from ._indicator import paint_folder_pictogram

        strip = strip if strip is not None else self._image_strip
        pm, edge = self._stage_glyph_canvas(strip, edge)
        bg = QColor(strip.palette().text().color())
        bg.setAlpha(24)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(0, 0, edge, edge, bg)
        inset = max(3, edge // 4)
        paint_folder_pictogram(
            painter, QRect(inset, inset, edge - 2 * inset, edge - 2 * inset)
        )
        painter.end()
        return pm

    def _stage_doc_glyph(self, strip) -> QPixmap:
        """サムネ化できないファイル（post.md 等）用のドキュメントグリフ.

        失敗の「×」と区別されるニュートラルな書類アイコン（角折れ矩形 +
        本文線）。色は帯のパレット（``text`` 色の α 変調）— design.md の
        ハードコード禁止に従う。pixmap を常駐させることで再要求も抑止する。
        """
        from PySide6.QtGui import QColor, QPainter

        pm, edge = self._stage_glyph_canvas(strip)
        text_color = strip.palette().text().color()
        bg = QColor(text_color)
        bg.setAlpha(24)
        mark = QColor(text_color)
        mark.setAlpha(120)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(0, 0, edge, edge, bg)
        pen = painter.pen()
        pen.setColor(mark)
        pen.setWidth(2)
        painter.setPen(pen)
        # 書類の外形（縦長矩形）+ 角折れ + 本文 2 行。
        w = max(8, edge // 3)
        h = max(10, edge // 2)
        x0 = (edge - w) // 2
        y0 = (edge - h) // 2
        fold = max(3, w // 3)
        painter.drawLine(x0, y0, x0 + w - fold, y0)
        painter.drawLine(x0 + w - fold, y0, x0 + w, y0 + fold)
        painter.drawLine(x0 + w, y0 + fold, x0 + w, y0 + h)
        painter.drawLine(x0 + w, y0 + h, x0, y0 + h)
        painter.drawLine(x0, y0 + h, x0, y0)
        inset = max(2, w // 5)
        for frac in (0.45, 0.65):
            y = y0 + round(h * frac)
            painter.drawLine(x0 + inset, y, x0 + w - inset, y)
        painter.end()
        return pm

    # ------------------------------------------------------ lightbox (閲覧モード)

    def _ensure_lightbox(self) -> LightboxWindow:
        """Build the lightbox (+ its filmstrip thumbnail loader) on first use.

        The dedicated loader keeps 2 worker threads and shares the persistent
        disk / folder caches with the main loaders.  While the lightbox is up
        the panes are idle (fullscreen), so the extra threads don't push the
        live NAS parallelism budget in practice.
        """
        if self._lightbox is None:
            self._lightbox_loader = ThumbnailLoader(
                cache_size=128,
                max_threads=2,
                parent=self,
                disk_cache=self._disk_cache,
                cache_edge=self._state.thumb_disk_cache_max_edge,
                folder_cache=self._folder_cache,
            )
            lb = LightboxWindow(loader=self._lightbox_loader, parent=self)
            # Reuse the right pane's resident thumbnails for the stage-0
            # placeholder + filmstrip (no extra NAS I/O when warm).
            lb.set_thumbnail_provider(self._file_list.pixmap_for_path)
            # Cross-post navigation is a depth-first walk of the current
            # browse root's subtree.  ``folder_paths`` orders the root's
            # direct children (left pane's live sort / filter); the root
            # itself bounds the DFS so it never wanders above what the user
            # is browsing.
            lb.set_post_provider(self._post_grid.folder_paths)
            lb.set_root_provider(lambda: self._root)
            # 現在画像のスターを左下カウンタへ併記する —
            # 左ペインのインメモリ map からの同期読み取り（sqlite / NAS なし）。
            lb.set_star_provider(self._post_grid._star_of)
            lb.set_slideshow_interval(self._state.slideshow_interval_sec)
            lb.set_chrome_hide_ms(self._state.lightbox_chrome_hide_ms)
            # 閲覧モードの MediaView（遅延生成）にもユーザーのメディア設定を反映し、
            # ループ / 音量変更を中央ペインと同じく ViewerState へ永続化する
            # （item 8）— これが無いとライトボックスの動画がハードコード既定の
            # 音量 70 / 自動再生 ON で再生される。
            lb.apply_media_settings(self._state)
            # 閲覧モードの ImageView は中央ペインとは別インスタンスなので、
            # ImageView 系のユーザー設定（キャッシュ / 先読み / ズーム維持 /
            # ミニマップ）も同じく流し込む — これが無いと
            # 設定が閲覧モードにだけ届かず、モジュール既定のまま動き続ける。
            lb.apply_view_state(self._state)
            lb.media_loop_toggled.connect(self._on_media_loop_toggled)
            lb.media_volume_changed.connect(self._on_media_volume_changed)
            lb.media_playback_rate_changed.connect(
                self._on_media_playback_rate_changed
            )
            # 全画面の ImageView 右クリックで切り替えたビュー設定も中央ペインと
            # 同じハンドラで ViewerState へ書き戻す（これが無いと押せる
            # のに保存されず、次回オープン時 apply_view_state が上書きする）。
            lb.image_zoom_persist_toggled.connect(self._on_image_zoom_persist_toggled)
            lb.image_minimap_toggled.connect(self._on_image_minimap_toggled)
            lb.post_changed.connect(self._on_lightbox_post_changed)
            lb.closed.connect(self._on_lightbox_closed)
            # 数字キー 0–5 → user_meta へ書き込み（左ペイン経由で一本化）。
            if self._user_meta is not None:
                # ``L`` と画像の右クリック（印の節）も同じ funnel へ。全画面は
                # 自前の中央オーバーレイで告げるので ``notify=False``。
                lb.set_curation_provider(self._post_grid._curation_badge_for)
                lb.set_user_tags_provider(self._file_user_tags)
                lb.curation_requested.connect(
                    lambda path, kind, value, lb=lb: self._post_grid.request_curation(
                        path, kind, value, notify=False, parent=lb
                    )
                )
                lb.star_key_requested.connect(
                    # notify=False: ライトボックスは中央オーバーレイで自前に
                    # 確認を出す（★の確認トーストは分割 /
                    # 最大化のプレビュー面が対象）。
                    lambda star, path: self._set_current_star(
                        star, path, notify=False
                    )
                )
            self._lightbox = lb
        return self._lightbox

    def _toggle_lightbox(self) -> None:
        """F11 / 表示メニュー: open the lightbox, or close it if already up.

        Opens on the previewed image when there is one; otherwise (G05) falls
        back to the currently-browsed folder and lets the lightbox scan it
        off-thread and land on its first image/video — so "全画面でこのフォルダを
        流し見" works straight from a folder selection, not only after previewing
        a single image.

        非メディアを**表示中**にこの経路へ落ちるときだけは、意図した機能でも
        「別のファイルが無言で開いた」と読まれる。ボタン側のラベルは
        ``_sync_stage_fullscreen_mode`` が予告し、押下時はライトボックス着地
        後に一言告げる。
        """
        if self._lightbox is not None and self._lightbox.isVisible():
            self._lightbox.close()
            return
        path = self._current_preview_path
        # 右一覧でサブフォルダを選んでいるなら、流し見の対象は**その**フォルダ。
        # フォルダ行の選択は ``_current_folder`` を動かさないので、
        # それを使うと親フォルダが開いて「隣の投稿まで混ざる」と読まれる。判定は
        # 右一覧のインメモリ表（``entry_for``）だけで行う — GUI スレッドで
        # ``is_dir()`` を呼ぶと到達不能 NAS で窓が止まる。拡張子付きのフォルダ名
        # （``2024.mp4`` 等）がメディア扱いで開かれる退化も同じガードで塞がる。
        entry = self._file_list.entry_for(path) if path is not None else None
        if entry is not None and entry.is_dir:
            folder = path
        elif path is not None and path.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            self._open_lightbox(path, self._search_result_playlist(path))
            return
        else:
            folder = self._current_folder or self._root
        rep = self._preview_fallback.shown
        if rep is not None and rep.suffix.lower() in _LIGHTBOX_MEDIA_SUFFIXES:
            # 代表画像を表示中なら見えている画像から入る（直下が空のフォルダで空カードにしない）。
            self._open_lightbox(rep)
            return
        if folder is not None:
            # 入場時の態を控える（復路が再ルートになる場合の戻し先）。
            # getattr ガードは __init__ を通さないテストハーネス向け。
            self._lightbox_entry_mode = getattr(self, "_ui_mode", "browse")
            self._pause_centre_media_for_lightbox()
            notice = (
                t("viewer.lightbox.folder_open_notice")
                if self._preview_is_non_media_file()
                else ""
            )
            self._ensure_lightbox().open_folder(folder, notice)
            return
        # 失敗通知は共通ファネルへ（トースト → ステータス → ログの
        # 3 段。ステータスバー直書きは常設のパス表示を潰す）。
        notify_failure(self, t("viewer.main_window.no_fullscreen_image"))

    def _search_result_playlist(self, path: Path) -> "list[Path] | None":
        """G07 の確定プレイリスト — 検索/絞り込み結果の流し見.

        左ペインが検索次元をエンゲージしている（``search_engaged``）間は、
        表示中のメディアタイルの並びがそのまま閲覧モードのプレイリストになる
        — フォルダ再列挙も投稿横断も行わず、「絞り込んだ結果だけを流し見」する
        （``ChildrenGrid.media_tile_paths`` / ``LightboxWindow.open_at`` の
        ``playlist`` 経路。この配線が無いと、検索結果からの F11 はフォルダ
        完全列挙へフォールバックしてヒットしなかった画像も混ざる）。

        通常ブラウズ（検索なし）と、起点が結果に含まれない場合は ``None`` を
        返し、フォルダ列挙のプレイリストに委ねる。
        """
        if not self._post_grid.search_engaged():
            return None
        playlist = self._post_grid.media_tile_paths()
        if path not in playlist:
            # 結果がフォルダタイルだけ（通常の投稿一覧）か、起点が右ペイン
            # 由来で結果に無い — フォルダ列挙の従来経路が正しい。
            return None
        return playlist

    def _pause_centre_media_for_lightbox(self) -> "MediaResume | None":
        """閲覧モードを開く前に中央プレビューの動画を一時停止する.

        閲覧モードは自前の ``MediaView``（2 つ目の ``QMediaPlayer`` +
        ``QAudioOutput``）を持ち、別トップレベルウィンドウなので
        ``ContentView._on_page_changed`` の「メディアページを離れる = 止める」
        対が働かない。放置すると同じ動画を 2 つのプレイヤーが別位置で同時
        再生し、音声が二重になる。停止ではなく一時停止なので、閉じて戻れば
        中央は同じ位置のまま（選択が動かない経路でも中央が空にならない）。

        同じ理由・同じ契機で **``QMovie``（アニメ GIF / WebP）も止める**:
        ページ切替の対が働かないのは動画だけの事情ではなく、放置すると同じ
        アニメを 2 つの ``QMovie`` がデコードし続ける。

        戻り値は一時停止した動画の :class:`~.lightbox.MediaResume` — 全画面側の
        ``MediaView`` は別インスタンスで位置を知らないので、その値を
        引き継がせるために返す。

        「止められたか」と「位置を読めるか」は別の問い。``pause_media_playback()`` は既に一時停止中なら no-op で
        ``False`` を返すので、その戻り値でゲートすると **ユーザーが Space で
        自分で止めてから F11 したときだけ** 位置が引き継がれず 0:00 から
        始まる（再生中なら引き継ぐので挙動が反転する）。復路
        （:meth:`_apply_lightbox_media_resume`）はパス一致だけで判定して
        いるので、往路も再生状態を見ない。未構築 / 未ロードなら
        ``media_current_path()`` が ``None`` を返し、引き継ぎ無し
        （``None``）として 0 から流し直す。
        """
        self._content.pause_media_playback()
        self._content.pause_image_animation()
        path = self._content.media_current_path()
        if not isinstance(path, Path):
            return None
        return MediaResume(path, self._content.media_playback_position())

    def _open_lightbox(
        self, path: Path, playlist: "list[Path] | None" = None,
    ) -> None:
        # 入場時の態を控える（復路が再ルートになる場合の戻し先）。
        # getattr ガードは __init__ を通さないテストハーネス向け。
        self._lightbox_entry_mode = getattr(self, "_ui_mode", "browse")
        paused_at = self._pause_centre_media_for_lightbox()
        lb = self._ensure_lightbox()
        # 中央で一時停止した動画の位置を全画面へ引き継ぐ。
        lb.set_resume_media(paused_at)
        if playlist is not None:
            # G07: search / filter result — the flat displayed list is the
            # authoritative playlist (no folder rescan, no cross-post).
            lb.open_at(path, playlist=playlist)
            return
        # Known siblings come from the right pane's already-scanned tiles
        # (no filesystem I/O); when the pane hasn't populated yet the
        # lightbox falls back to its own off-thread scandir.
        siblings, index = self._file_list.image_siblings(path)
        lb.open_at(path, siblings if index >= 0 else None)

    def _apply_lightbox_media_resume(self, resume: "MediaResume | None") -> None:
        """全画面側で進んだ再生位置を中央プレビューへ当てる.

        「引き継ぎ無し」を表すのは **値そのものが無いこと**だけ——往路
        （``MediaView`` の保留 seek）と同じ裁定で、``0`` は番兵ではなく
        「先頭へ戻す」という正当な位置。全画面でシークバー左端へ戻して
        から閉じたときに ``0`` を捨てると、中央は入場前の位置のまま残り、
        往路のコメントが避けたい「位置が 2 回飛ぶ」状態になる
        （閉じても同じファイルなら再選択は ``selection_changed`` を出さず、
        中央プレビューは作り直されない）。メディアページを見ていなければ
        :meth:`LightboxWindow.media_playback_position` が ``None`` を
        返すので、その判定だけで劣化せずに落ちる。
        """
        if resume is None:
            return
        if self._content.media_current_path() == resume.path:
            self._content.seek_media(max(0, resume.position_ms))

    def _on_lightbox_post_changed(self, folder: Path, image: Path) -> None:
        # Cross-post transition: keep the main window's selection following
        # along (folder_selected repopulates centre + right pane in the
        # background) so closing the lightbox lands where the user was.
        self._post_grid.select_path(folder)

    def _on_lightbox_closed(
        self, folder: object, image: object, crossed: bool,
    ) -> None:
        # 全画面で進めた動画の再生位置を中央プレビューへ返す —
        # 往路だけ引き継ぐと「行きは続きから / 帰りは止めた位置」と
        # 位置が 2 回飛ぶ。パネル同期より先に控えておき、着地後に
        # 「中央が同じファイルを開いたままなら」だけ当てる（別の
        # ファイルへ移っていたら黙って捨てる = 劣化は無い）。
        # getattr ガードは __init__ を通さないテストハーネス向け。
        lb = getattr(self, "_lightbox", None)
        self._apply_lightbox_media_resume(
            lb.media_playback_position() if lb is not None else None
        )
        # 往路で止めた中央のアニメ GIF / WebP を戻す。同じ画像のまま閉じると
        # 下の再選択は emit せず中央は作り直されないので、ここで戻さないと
        # 止まったまま残る（別の画像へ移っていれば QMovie は作り直し済みで
        # no-op）。
        self._content.resume_image_animation()
        # Sync the final position back into the panes: the post folder in the
        # left pane (when it changed) and the last-viewed file in the right
        # pane — whose selection also routes the centre preview to it.
        if not isinstance(image, Path):
            return
        target = folder if isinstance(folder, Path) else image.parent
        if target != self._current_folder:
            # 横断していない（右一覧のサブフォルダ行から F11 した場合は入場
            # フォルダ自体が ``_current_folder`` の子）— 下の再ルートに落とすと
            # 開いて閉じただけで左グリッドが投稿の中へ降り、検索が破棄され
            # 履歴も 1 段積まれる。右一覧側で選び直す（画像が無ければフォルダ行）。
            if not crossed:
                if not self._file_list.select_path(image):
                    self._file_list.select_path(target)
                return
            # 検索結果から開いた全画面（G07）の確定プレイリストは複数フォルダに
            # 跨るので、閉じた folder が ``_current_folder`` と違っても横断では
            # ない。検索中の左グリッドはファイルの
            # タイルを持つ — 最後に見た画像のタイルを選べばそれが着地で、
            # ``file_selected`` が右一覧・中央まで追従させる。フォルダの
            # ``select_path`` は必ず失敗し、下の再ルートが検索を破棄してしまう。
            if self._post_grid.search_engaged() and self._post_grid.select_path(
                image
            ):
                return
            # The DFS lightbox can cross into a deeper / different-level
            # folder that isn't shown as a tile under the current root.  When
            # ``select_path`` can't find it there, re-root one level up so it
            # appears as a selectable row (same move as a right-pane folder
            # double-click); the pending-select then fires ``folder_selected``
            # → right pane + centre for it.
            if not self._post_grid.select_path(target):
                # Re-root repopulates the panes asynchronously; selecting
                # the exact image would race that scan, so land the user
                # on the folder (its default preview) and stop here.
                # 事前の同期 ``is_dir`` ゲートは置かない — 消失時は
                # set_root の単発ゲートが I07 プロンプトで応答する（無言で
                # ファイル一覧の再選択に落とさない）。
                self.set_root(
                    target.parent, push_history=True, pending_select=target
                )
                # 再ルートは「ナビゲーションは分割へ着地する」既定に従って
                # 最大化を解く。全画面へ最大化から入っていたなら態を戻す
                # — ``_stage_settle_pending`` を立てるのが要点で、
                # 再ルートは非同期なので着地時に保持対象が消えていれば
                # ``WindowStatus.settle_stage_after_scan`` が分割へ落とす安全弁
                # に乗る。
                # 履歴は再ルート側が既に push している。
                if getattr(self, "_lightbox_entry_mode", "browse") == "stage":
                    self._stage_settle_pending = True
                    self._enter_stage_mode(push_history=False)
                self._lightbox_entry_mode = "browse"
                return
        if not self._file_list.select_path(image):
            self._file_list.set_pending_select(image)

    # -------------------------------------------------------- bookmarks/menu

    def _record_recent_root(self, path: Path) -> None:
        """Move *path* to the front of the 最近開いたフォルダ MRU list.

        Called only on top-level root changes (launch / root picker / bookmark
        jump), never on ordinary drill-downs, so the menu reflects the folders
        the user explicitly opened.  Persistence rides on the full
        ``save_state`` in ``closeEvent`` (``recent_roots`` is part of the state
        model); the menu is rebuilt immediately so it's fresh next time it's
        opened.
        """
        updated = push_recent_root(self._state.recent_roots, str(path))
        if updated != self._state.recent_roots:
            self._state.recent_roots = updated
            # The menu only exists once ``_build_menus`` has run; guard so a
            # partially-constructed window (or a test stub) can still record the
            # MRU state without a live menu widget.
            if getattr(self, "_recent_menu", None) is not None:
                self._rebuild_recent_menu()
        # NOTE: the ファイル → ライブラリ submenu (M02) is an *explicit*
        # registration list, independent of the MRU — it is populated ONLY by
        # _register_current_library / LibraryDialog, never here.  Auto-seeding
        # it from every top-level root change (as this used to) silently turned
        # it into a second MRU and defeated its "curated library switch" intent,
        # so _record_recent_root deliberately touches only recent_roots now.
        # Top-level root changes are worth surviving a crash (B03) — queue a
        # debounced full state save instead of waiting for closeEvent.
        if getattr(self, "_state_save_debounce", None) is not None:
            self._schedule_state_save()

    def _rebuild_recent_menu(self) -> None:
        """Repopulate the ファイル → 最近開いたフォルダ submenu from state.

        Deliberately NAS-free: every entry is added enabled with **no
        ``is_dir`` check** — a per-entry stat here runs on the GUI thread at
        startup / on every root change, and an offline NAS share would freeze
        the whole window for its timeout × up to 10 entries.  A vanished path is
        handled at click time instead: ``_on_root_change_requested`` →
        ``set_root`` warns and leaves the trail + MRU untouched.  Menu
        building must stay NAS-free (same rule as ``context_menus.py``).
        """
        menu = self._recent_menu
        menu.clear()
        if not self._state.recent_roots:
            act_empty = QAction(t("viewer.common.no_history"), self)
            act_empty.setEnabled(False)
            menu.addAction(act_empty)
            return
        for raw in self._state.recent_roots:
            act = QAction(raw, self)
            act.setToolTip(raw)
            act.triggered.connect(
                lambda _checked=False, p=raw: self._on_root_change_requested(
                    Path(p)
                )
            )
            menu.addAction(act)

    # ------------------------------------------------------- libraries (M02)

    def _compute_library_bases(self) -> list[tuple[Path, str]]:
        """Library roots the breadcrumb renders the trail relative to.

        「基準パス → 友好ラベル」の表そのもの（正本は
        :func:`~snappix.viewer.locations.location_bases`）— ZIP 展開先
        （アーカイブ名）→ 既定ライブラリ（「ライブラリ」）→ 登録ライブラリ
        （末尾フォルダ名、衝突時のみ ``親/名前``）。パンくずの基準にも、
        各面の場所の名乗り（:meth:`_location_label`）にも同じ表を使う。
        No filesystem I/O — pure path arithmetic, so it stays NAS-free.
        getattr ガードは構築順（``_library_roots`` は ``_build_ui`` 後に張られる）と、
        ``__init__`` を通さないテストハーネス向け。
        """
        return location_bases(
            getattr(self, "_zip_temp_dirs", {}),
            self._default_library,
            getattr(self, "_library_roots", ()),
        )

    def _location_label(self, path: Path) -> str:
        """場所の表示名（窓の全ての面がこれ 1 本で名乗る — ``locations``）."""
        return location_label(path, self._compute_library_bases())

    def _refresh_library_bases(self) -> None:
        """Push the current library roots to the breadcrumb."""
        self._post_grid.breadcrumb.set_library_bases(self._compute_library_bases())
        # ライブラリ登録の増減は「↑」の停止条件を変える。
        if getattr(self, "_history", None) is not None:
            self._update_nav_buttons()

    @staticmethod
    def _load_library_roots() -> list[str]:
        """Registered library roots from shared_prefs (local file, NAS-free)."""
        try:
            return list(load_shared_prefs().library_roots)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("could not load library roots: {}", exc)
            return []

    def _rebuild_library_menu(self) -> None:
        """Repopulate the ファイル → ライブラリ submenu (M02).

        NAS-free like ``_rebuild_recent_menu``: every registered root is added
        enabled with **no ``is_dir`` check** (a per-entry stat on the GUI thread
        would freeze the window on an offline share).  A vanished path is caught
        at click time by ``set_root``.  Unlike the auto-tracked MRU, this
        list is explicit — the user registers / removes roots — so the entries
        act as durable "library switch" targets.

        ラベルは生パス全文ではなく「末尾フォルダ名（衝突時のみ 親/名前）」
        （レール / パンくず基点 / ライブラリ管理と同じ規則で、生パス全文は
        ツールチップが持つ）。
        """
        menu = self._library_menu
        menu.clear()
        labels = tail_display_labels(list(self._library_roots))
        for raw in self._library_roots:
            act = QAction(labels[raw], self)
            act.setToolTip(raw)
            act.triggered.connect(
                lambda _checked=False, p=raw: self._on_root_change_requested(
                    Path(p)
                )
            )
            menu.addAction(act)
        if not self._library_roots:
            act_empty = QAction(t("viewer.main_window.no_libraries"), self)
            act_empty.setEnabled(False)
            menu.addAction(act_empty)
        menu.addSeparator()
        act_register = QAction(
            t("viewer.main_window.library_register_current"), self
        )
        act_register.triggered.connect(self._register_current_library)
        menu.addAction(act_register)
        self._act_library_register = act_register
        self._sync_library_register_action()
        act_manage = QAction(menu_label("viewer.library_dialog.title"), self)
        act_manage.setEnabled(bool(self._library_roots))
        act_manage.triggered.connect(self._manage_libraries)
        menu.addAction(act_manage)
        # Keep the ナビレール's ライブラリ section in step with this submenu.
        self._refresh_nav_rail_libraries()

    def _root_is_ephemeral(self) -> bool:
        """今のルートが閉じると消える ZIP 展開先か（永続化の席が引く 1 本）."""
        return is_zip_temp_path(getattr(self, "_root", None))

    def _refuse_ephemeral_root(self) -> bool:
        """ZIP 展開先なら登録を断って案内する（断ったら ``True``）.

        メニュー項目は無効化しているが、項目を経由しない呼び出しにも同じ
        判定を効かせる（書き込み口で拒否する）。
        """
        if not self._root_is_ephemeral():
            return False
        self._show_toast(t("viewer.main_window.zip_temp_not_registrable"), "info")
        return True

    def _sync_library_register_action(self) -> None:
        """「現在のフォルダをライブラリに登録」の活性を今のルートへ合わせる."""
        act = getattr(self, "_act_library_register", None)
        if act is None:
            return
        root = getattr(self, "_root", None)
        act.setEnabled(root is not None and not self._root_is_ephemeral())

    def _register_current_library(self) -> None:
        """Register the current root as a library (M02)."""
        # ``str(Path)`` は空にならないので「空文字なら return」は死んだ分岐
        # だった（本当に None を取り得るなら ``str(None) == "None"`` が真値
        # として登録されてしまう）。同ファイル内で ``_root`` を None 込みで
        # 扱う述語（``_can_go_up`` / ``_sync_bookmark_actions``）と前提を揃える。
        root = getattr(self, "_root", None)
        if root is None or self._refuse_ephemeral_root():
            return
        target = str(root)
        # 書き込みは ``shared_prefs.json`` の 1 本の flusher を通る（テーマ /
        # ライブラリ管理と共有）。**投げっぱなし
        # にはしない**: ``BoundedFlusher.run`` は先行フラッシュが飛行中だと
        # ``work`` を呼ばずに落とす仕様で、``add_library_root`` は差分書き込み
        # （後続のスナップショット保存に相乗りできない）なので、捨てられると
        # 登録は恒久に消える。``library_roots`` は ``viewer_state.json`` 側に
        # 対応フィールドが無く救済もされない。待ちは有界（ブックマーク /
        # 保存した検索と同じ ``_SETTINGS_PERSIST_WAIT_S``）で、落ちたときは
        # 成功トーストを出さず常駐警告へ回す。
        landed, _ = self._shared_prefs_flush.run(
            lambda: add_library_root(target),
            wait_s=_SETTINGS_PERSIST_WAIT_S,
        )
        if target not in self._library_roots:
            self._library_roots.append(target)
        self._rebuild_library_menu()
        self._refresh_library_bases()
        if not landed:
            self._notify_persist_failed()
            return
        self._show_toast(
            t("viewer.main_window.library_registered"), "success", 4000,
        )

    def _manage_libraries(self) -> None:
        """Open the library-management dialog (list / delete / reorder — M02).

        ダイアログへ渡す baseline は起動時スナップショット
        （``self._library_roots``）ではなく**開く瞬間のディスク実データ**、
        OK 時の書き込みは全置換ではなく「baseline との差分 + 提示順」を
        ``apply_library_roots`` で最新ディスクリストへ適用する。これで
        起動時に shared_prefs.json を読めなかったセッション（degraded）でも
        空スタンドイン由来のリストが実データを置換できず、他プロセスが
        途中で足したルートも保存される。

        baseline の**読み取り**も書き込みと同じ ``_shared_prefs_flush`` の
        有界ワーカーに載せる。data/ が NAS に
        載る運用では 1 回の I/O が数十秒塞がり得るので、開く側だけ GUI
        スレッドの同期読みだと「OK 後の書き込みは 5 秒で劣化するのに、
        ダイアログが出るまで窓全体が固まる」片側欠落になる。予算切れ・
        例外・読めなかった（degraded の空スタンドイン）・先行書き込みへの
        合流（値が返らない）のときは in-memory の ``self._library_roots``
        を baseline に劣化させる — 書き込みは ``apply_library_roots`` の
        差分適用なので、baseline が古くてもディスク側の未知のルートは
        消えない。
        """
        from .library_dialog import LibraryDialog

        def _read_disk_roots() -> list[str] | None:
            prefs = load_shared_prefs()
            if prefs._degraded_load:
                return None
            return list(prefs.library_roots)

        read_ok, disk_roots = self._shared_prefs_flush.run(
            _read_disk_roots, wait_s=_SETTINGS_PERSIST_WAIT_S,
        )
        baseline = (
            list(disk_roots)
            if read_ok and disk_roots is not None
            else list(self._library_roots)
        )
        dlg = LibraryDialog(baseline, self)
        if dlg.exec() == QDialog.Accepted:
            result = dlg.result_roots()
            base_set = set(baseline)
            result_set = set(result)
            added = [r for r in result if r not in base_set]
            removed = [r for r in baseline if r not in result_set]
            # ここは差分適用の**戻り値**（他プロセスの追加を含む最新リスト）を
            # メニューへ反映するので待つ。待ちは有界 — 死んだ共有で無限に固まる代わりに、5 秒でユーザーの
            # 選んだリストを表示して先へ進む（書き込みはワーカーが続ける）。
            landed, merged = self._shared_prefs_flush.run(
                lambda: apply_library_roots(
                    added=added, removed=removed, order=result,
                ),
                wait_s=_SETTINGS_PERSIST_WAIT_S,
            )
            self._library_roots = merged if landed and merged is not None else result
            self._rebuild_library_menu()
            self._refresh_library_bases()
            if not landed:
                # 先行フラッシュが飛行中だと ``run`` は work を呼ばずに落とす
                # ため、削除・並べ替えがディスクに載っていない。黙って閉じない
                # （ブックマーク / 保存した検索と同じ成否表示契約）。
                self._notify_persist_failed()
        dlg.deleteLater()

    # ---------------------------------------------------- saved searches (M03)

    def _rebuild_saved_search_menu(self) -> None:
        """Repopulate the 編集 → 保存した検索 submenu (M03).

        NAS-free: saved searches are pure query payloads (names + serialised
        filter/tag state), never paths, so there is nothing to stat.  A click
        re-applies the search against the CURRENT root.
        """
        menu = self._saved_search_menu
        menu.clear()
        # Keep the ナビレール's 保存した検索 section in step with this submenu
        # (before the early-return branch below so it fires either way).
        self._refresh_nav_rail_saved_searches()
        searches = self._state.saved_searches
        if not searches:
            act_empty = QAction(t("viewer.main_window.no_saved_searches"), self)
            act_empty.setEnabled(False)
            menu.addAction(act_empty)
            return
        for idx, entry in enumerate(searches):
            name = str(entry.get("name") or "").strip() or t(
                "viewer.main_window.saved_search_unnamed"
            )
            act = QAction(name, self)
            act.setToolTip(saved_search_tooltip(entry))
            act.triggered.connect(
                lambda _checked=False, i=idx: self._apply_saved_search(i)
            )
            menu.addAction(act)
        menu.addSeparator()
        act_manage = QAction(menu_label("viewer.saved_search_dialog.title"), self)
        act_manage.triggered.connect(self._manage_saved_searches)
        menu.addAction(act_manage)

    def _on_save_current_search(self) -> None:
        """Name the current search state and store it as a smart folder (M03)."""
        snap = self._post_grid.capture_search_state()
        if not snap.is_active():
            QMessageBox.information(
                self,
                t("viewer.main_window.save_search"),
                t("viewer.main_window.save_search_none"),
            )
            return
        payload = serialize_search_snapshot(snap)
        # 命名欄が空欄だと「何を保存しようとしているのか」を思い出しながら
        # 名前を考える必要がある。条件チップと同じ語彙の要約（保存した検索の
        # ツールチップと同じ関数）を既定名として置く — そのまま OK でも
        # 意味の通る名前になり、書き換えても構わない。
        # 静的 getText ではなく dialogs.prompt_text — OK/キャンセルが
        # 他のダイアログと同じカタログ文言になる。
        name, ok = prompt_text(
            self,
            t("viewer.main_window.save_search"),
            t("viewer.main_window.save_search_prompt"),
            text=describe_search_payload(payload),
        )
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        entry = {"name": name, **payload}
        # Replace an existing same-named search (update-in-place) rather than
        # accumulate duplicates.
        searches = [
            e for e in self._state.saved_searches
            if str(e.get("name") or "").strip() != name
        ]
        searches.append(entry)
        # 再追加はこのセッションの削除記録を打ち消す（ブックマークの
        # discard と同型）。
        self._session_removed_saved_searches.discard(name)
        # 即時 flush + ディスク側とのマージ（persist_bookmarks と同型）:
        # デバウンス保存の全フィールド上書きに任せると、並行
        # インスタンスが保存した検索を 5 秒差で消し得る。in-memory 更新と
        # メニュー再構築は先、書き込みは有界ワーカー。
        if not self._persist_saved_searches_now(searches):
            # ディスクに載らなかった保存を「保存しました」と言わない
            # （常駐警告は ``_persist_saved_searches_now`` が出している）。
            return
        # 他の成功通知（ブックマーク追加・設定適用…）と同じくトーストで返す
        # — 同じ「操作が通った」を同じ面で返す。
        self._show_toast(t("viewer.main_window.save_search_done"), "success")

    def _apply_saved_search(self, index: int) -> None:
        """Re-apply the saved search at *index* against the current root (M03)."""
        searches = self._state.saved_searches
        if not (0 <= index < len(searches)):
            return
        try:
            snap = deserialize_search_snapshot(searches[index])
        except Exception as exc:  # pragma: no cover (defensive)
            # ログだけだと利用者から見て「押したのに何も起きない」—
            # 失敗はトーストで必ず可視化する。
            logger.warning("could not apply saved search: {}", exc)
            self._show_toast(
                t("viewer.main_window.saved_search_apply_failed"), "error"
            )
            return
        # 履歴復元用の restore_search_state ではなく、占有一覧を先に出る口
        # （保存検索は「現在のフォルダを起点に適用」する約束）。
        self._post_grid.apply_saved_search(snap)

    def _manage_saved_searches(self) -> None:
        """Open the saved-search management dialog (rename / delete — M03)."""
        from .saved_search_dialog import SavedSearchDialog

        dlg = SavedSearchDialog(self._state.saved_searches, self)
        if dlg.exec() == QDialog.Accepted:
            before = {
                str(e.get("name") or "").strip()
                for e in self._state.saved_searches
            }
            result = dlg.result_searches()
            after = {str(e.get("name") or "").strip() for e in result}
            # 削除・改名で消えた旧名を記録し、残った名前は記録から外す
            # （_manage_bookmarks と同型）。
            self._session_removed_saved_searches |= before - after
            self._session_removed_saved_searches -= after
            # 即時 flush + ディスク側とのマージ。書き込みは
            # 有界ワーカー・常駐警告は共通ヘルパ側。
            self._persist_saved_searches_now(result)
        dlg.deleteLater()

    def _rebuild_bookmarks_menu(self) -> None:
        menu = self._bookmarks_menu
        menu.clear()
        # Keep the ナビレール's ブックマーク section in step with this menu
        # (before the early-return branch below so it fires either way).
        self._refresh_nav_rail_bookmarks()
        # 横断キュレーション一覧の入口はレール常設 +「編集 ▸ キュレーション」
        # に一本化し（ここにも置くとレールとの二重表記になる）、ブックマーク
        # メニューはブックマークだけを持つ。
        # 対の動詞は「ブックマークに追加 / ブックマークから外す」（「削除」は
        # ディスクからの削除＝破壊的操作に読める）で、登録状態で
        # 活性を切り替える（``_sync_bookmark_actions`` / aboutToShow）。
        act_add = QAction(t("viewer.main_window.bookmark_add_current"), self)
        act_add.triggered.connect(self._add_current_bookmark)
        menu.addAction(act_add)
        self._act_bookmark_add = act_add
        act_remove = QAction(t("viewer.main_window.bookmark_remove_current"), self)
        act_remove.triggered.connect(self._remove_current_bookmark)
        menu.addAction(act_remove)
        self._act_bookmark_remove = act_remove
        self._sync_bookmark_actions()
        act_manage = QAction(menu_label("viewer.bookmark_dialog.title"), self)
        act_manage.triggered.connect(self._manage_bookmarks)
        menu.addAction(act_manage)
        menu.addSeparator()
        if not self._state.bookmarks:
            act_empty = QAction(t("viewer.main_window.no_bookmarks"), self)
            act_empty.setEnabled(False)
            menu.addAction(act_empty)
            return
        for raw in self._state.bookmarks:
            # 表示名 → フォルダ名 → 生パス（レールと同じ 1 実装 — 片側だけ
            # 生パスのまま、を作らない）。フルパスはツールチップに残す。
            act = QAction(bookmark_label(raw, self._state.bookmark_names), self)
            act.setToolTip(raw)
            act.triggered.connect(lambda _checked=False, p=raw: self._jump_to_bookmark(p))
            menu.addAction(act)

    def _sync_bookmark_actions(self) -> None:
        """現在ルートの登録状態に合わせて追加/解除の活性を切り替える.

        登録済みなら「ブックマークに追加」を、未登録なら「ブックマークから
        外す」を無効化する — 「押せるのに何も起きない」を無くす（追加側は
        既登録トーストで応じるので活性のままでもよいが、対の一方だけが
        意味を持つ状態を見た目で示すほうが誤解が少ない）。
        """
        add = getattr(self, "_act_bookmark_add", None)
        remove = getattr(self, "_act_bookmark_remove", None)
        if add is None or remove is None:
            return
        root = getattr(self, "_root", None)
        registered = root is not None and str(root) in self._state.bookmarks
        # ZIP 展開先は閉じると消える — 登録しても次回は「見つかりません」になる。
        ephemeral = self._root_is_ephemeral()
        add.setEnabled(root is not None and not registered and not ephemeral)
        remove.setEnabled(registered)
        # 対象名を項目名に出す（★系トーストが対象名を必須にしているのと同じ
        # 規律）。ここは aboutToShow でも走るので、ルートが変わった
        # 後の 1 回目の表示から追随する。名前が取れないドライブ直下は素の
        # 文言へ落とす。
        name = (
            bookmark_label(str(root), {})
            if root is not None and not ephemeral
            else ""
        )
        add.setText(
            t("viewer.main_window.bookmark_add_current_named", name=name)
            if name and name != str(root)
            else t("viewer.main_window.bookmark_add_current")
        )

    def _persist_bookmarks_now(self) -> bool:
        """Flush the bookmark fields to disk immediately (crash-proofing).

        Bookmarks were historically only saved by ``closeEvent`` — a crash /
        kill lost the whole session's additions, which users noticed as
        "お気に入りが消える".  Every mutation calls this right away.

        ``persist_bookmarks`` merges with whatever another instance wrote to
        disk since (minus this session's removals), so near-simultaneous
        adds from two viewers don't clobber each other.  We adopt the merged
        result back into our own state — and rebuild the menu if disk
        additions changed the list — so memory stays consistent with disk.

        **書き込みはワーカーで走る**。ここは
        フル保存（:meth:`_persist_state_snapshot`）と同じ 2 段構え: GUI
        スレッドで書く値を確定（呼び出し元が ``self._state`` を更新し、
        メニュー / レールを再構築済み）→ ``persist_bookmarks`` の read+write
        は :class:`~snappix.viewer.state_flush.BoundedFlusher` のワーカーで
        行い、待つのは ``_SETTINGS_PERSIST_WAIT_S`` 秒まで。到達不能な共有で
        「ブックマークを 1 個足す」と GUI が数十秒（実測 15〜195 秒）固まる
        代わりに、5 秒で「保存できませんでした」へ劣化する。

        Returns **False** when the write did not land so the caller can
        skip its 「保存しました」 toast; the resident warning itself is raised
        here so every mutation route reports the loss the same way.
        """
        # ワーカーへ渡すのは複製 — 走行中に GUI スレッドが ``self._state`` を
        # 書き換えても、書き出す内容が中途半端に混ざらないようにする
        # （``_persist_state_snapshot`` の ``model_copy`` と同じ理由）。
        bookmarks = list(self._state.bookmarks)
        names = dict(self._state.bookmark_names)
        removed = set(self._session_removed_bookmarks)
        cleared = set(self._session_cleared_bookmark_names)
        landed, result = self._bookmark_flush.run(
            lambda: persist_bookmarks(bookmarks, names, removed, cleared),
            wait_s=_SETTINGS_PERSIST_WAIT_S,
        )
        if not landed or result is None:
            # 予算超過 / 例外（ワーカー側でログ済み）。in-memory の値は生きた
            # ままなので、終了時のフル ``save_state`` が改めて書く。
            self._notify_persist_failed()
            return False
        merged, merged_names, ok = result
        if merged != self._state.bookmarks or merged_names != self._state.bookmark_names:
            self._state.bookmarks = merged
            self._state.bookmark_names = merged_names
            self._rebuild_bookmarks_menu()
        if not ok:
            self._notify_persist_failed()
        return ok

    def _persist_saved_searches_now(self, searches: list[dict]) -> bool:
        """保存済み検索を *searches* へ差し替え、即時フラッシュする.

        :meth:`_persist_bookmarks_now` と同型: in-memory を先に更新してメニューを再構築し、
        ``persist_saved_searches`` の read+write はワーカーで有界に走らせる。
        マージ結果（並行インスタンスがディスクへ足した検索）は戻ってきたら
        採り込む。

        戻り値は **ディスクに載ったか**（呼び出し元は偽なら
        「保存しました」と名乗らない。常駐警告はここで 1 回出す）。
        """
        self._state.saved_searches = searches
        self._rebuild_saved_search_menu()
        snapshot = [dict(e) for e in searches]
        removed = set(self._session_removed_saved_searches)
        landed, result = self._saved_search_flush.run(
            lambda: persist_saved_searches(snapshot, removed),
            wait_s=_SETTINGS_PERSIST_WAIT_S,
        )
        if not landed or result is None:
            self._notify_persist_failed()
            return False
        merged, ok = result
        if merged != self._state.saved_searches:
            self._state.saved_searches = merged
            self._rebuild_saved_search_menu()
        if not ok:
            self._notify_persist_failed()
        return ok

    def _add_current_bookmark(self) -> None:
        # ``str(Path)`` は空にならないので「空文字なら return」は死んだ分岐
        # だった（本当に None を取り得るなら ``str(None) == "None"`` が真値
        # として登録されてしまう）。同ファイル内で ``_root`` を None 込みで
        # 扱う述語（``_can_go_up`` / ``_sync_bookmark_actions``）と前提を揃える。
        root = getattr(self, "_root", None)
        if root is None or self._refuse_ephemeral_root():
            return
        target = str(root)
        # 対象名はトーストに必ず添える（★系と同じ規律）。
        name = bookmark_label(target, self._state.bookmark_names)
        if target in self._state.bookmarks:
            # Adding an already-registered folder must not be a silent
            # no-op — tell the user it's already there instead.
            self._show_toast(
                t("viewer.main_window.bookmark_already", name=name), "info"
            )
            return
        # Added with no display name — the raw path shows until the user
        # renames it via the management dialog.
        self._state.bookmarks.append(target)
        self._session_removed_bookmarks.discard(target)
        # 付け直した = 以前の「名前を消した」記録は効かせない（削除記録の
        # discard と同型）。別インスタンスが付けた名前はそのまま採る。
        self._session_cleared_bookmark_names.discard(target)
        self._rebuild_bookmarks_menu()
        # 書き込みが落ちたときは「追加しました」と言わない
        # （``_persist_bookmarks_now`` が常駐警告トーストを既に出している）。
        if self._persist_bookmarks_now():
            self._show_toast(
                t("viewer.main_window.bookmark_added", name=name), "success"
            )

    def _remove_current_bookmark(self) -> None:
        self._remove_bookmark(str(self._root))

    def _remove_bookmark(self, target: str) -> None:
        """*target*（生パス文字列）のブックマーク登録を外す。

        メニューの「ブックマークから外す」と、死んだブックマークを踏んだとき
        の「このブックマークを削除」の共通実装。
        """
        if target in self._state.bookmarks:
            self._state.bookmarks.remove(target)
            self._state.bookmark_names.pop(target, None)
            self._session_removed_bookmarks.add(target)
            self._session_cleared_bookmark_names.add(target)
            self._rebuild_bookmarks_menu()
            if self._persist_bookmarks_now():  # 失敗時は成功トーストを出さない
                self._show_toast(
                    t("viewer.main_window.bookmark_removed"), "success"
                )

    def _manage_bookmarks(self) -> None:
        from .bookmark_dialog import BookmarkDialog

        dlg = BookmarkDialog(
            self._state.bookmarks, self._state.bookmark_names, self,
        )
        if dlg.exec() == BookmarkDialog.Accepted:
            before = set(self._state.bookmarks)
            before_names = set(self._state.bookmark_names)
            self._state.bookmarks = dlg.result_bookmarks()
            self._state.bookmark_names = dlg.result_names()
            after = set(self._state.bookmarks)
            after_names = set(self._state.bookmark_names)
            self._session_removed_bookmarks |= before - after
            self._session_removed_bookmarks -= after
            # 空欄にした表示名は「不在」ではなく明示集合で表す（マージが
            # 他インスタンスの名前を巻き添えにしないため）。名前を付け直したら
            # その記録は外す。
            self._session_cleared_bookmark_names |= before_names - after_names
            self._session_cleared_bookmark_names -= after_names
            self._rebuild_bookmarks_menu()
            self._persist_bookmarks_now()
        # result_bookmarks/result_names were read above; safe to release.
        dlg.deleteLater()

    def _jump_to_bookmark(self, raw: str) -> None:
        path = Path(raw)
        # GUI スレッドで同期 ``is_dir()`` しない — 切断 / スリープ中の NAS
        # 上のブックマークをクリックすると、警告が出るより前に SMB タイム
        # アウト（数十秒）ぶんウィンドウ全体が凍る。起動経路（B05）と同じ
        # ワーカースレッド + ハードタイムアウト 2s の共通プローブを使う。
        # タイムアウト（= 共有がまさに起きようとしている）は存在扱いで
        # 素通しし、実在確認は非同期スキャン失敗のエラーカードに委ねる。
        kind = probe_path_kind(raw)
        if kind in ("file", "missing"):
            # 生パス 1 行の警告ではなく共通応答へ。
            # ブックマーク経路だけは「このブックマークを削除」も出す — 死んだ
            # 登録をその場で片付けられないと、押すたび同じ警告に当たり続ける。
            if self._handle_missing_folder(path, bookmark=raw) != "retry":
                return
        # プローブ済み（または存在扱い）なので GUI スレッドで stat し直さない。
        self._on_root_change_requested(path, assume_exists=True)

    def _focus_filter_box(self) -> None:
        """Ctrl+F — jump to the left pane's filter box (text pre-selected)."""
        self._post_grid.focus_filter()

    def _focus_tag_search(self) -> None:
        """Ctrl+Shift+T — open the AI search popover + focus the AI-tag input."""
        self._post_grid.focus_tag_search()

    def _open_settings_dialog(self) -> None:
        # SettingsDialog writes directly into self._state on OK, so after
        # ``exec`` returns Accepted we just push the new numbers into the
        # live views.  Rejecting leaves state untouched.  The cache-management
        # group is backed by the CacheBuildController.
        prev_theme = self._state.theme
        dlg = SettingsDialog(self._state, self, cache_controller=self._cache_ctrl)
        if dlg.exec() == SettingsDialog.Accepted:
            self._apply_settings_live()
            # I02: the display tab carries the theme combo — mirror any change
            # onto the live theme, the 表示メニュー check state and shared_prefs.
            if self._state.theme != prev_theme:
                self._apply_theme_choice(self._state.theme)
            # I05: confirm the settings landed (non-modal; the tunables are
            # applied live above so there is nothing else to acknowledge).
            # 「適用しました」を名乗る前に実際にディスクへ書く — 終了時の
            # フル保存任せだと、書き込み不可な媒体では成功表示の裏で
            # 設定が丸ごと消える。失敗時は常駐警告トーストのみ。
            # 保存自体はワーカーで走るので、ここだけが完了を待つ
            # （待たないと成否を名乗れない）。待ちは有界 — 5 秒応答しない
            # 保存先で「適用しました」と言う方が嘘になる。
            if self._persist_state_snapshot(wait_s=_SETTINGS_PERSIST_WAIT_S):
                self._show_toast(
                    t("viewer.main_window.settings_applied"), "success"
                )
            else:
                self._notify_persist_failed()
        dlg.deleteLater()  # results already committed into self._state

    def _apply_settings_live(self) -> None:
        """Fan out the current ``ViewerState`` to every live subsystem.

        Mirrors the layout-level "push cache settings before first render"
        done in :meth:`_build_ui` — called after the settings dialog
        commits so users don't have to restart the viewer for any of the
        tunables to take effect.
        """
        state = self._state
        self._content.apply_cache_settings(state)
        self._post_grid.apply_settings(state)
        self._file_list.apply_settings(state)
        # プール数 / 記憶 LRU 枚数は**本体 2 本だけ**。専用ローダー 3 本（フォルダプレビュー / フィルム
        # ストリップ / ライトボックス）は「小プール固定（128 枚 / 2 本）」
        # という設計判断で作られているので、ここは意図して配らない。
        # 一方 ``cache_edge``（下の ``set_disk_cache``）はプールサイズと
        # 無関係な純粋なユーザー設定なので**全ローダー**へ配る。
        self._loader.apply_settings(
            state.thumbnail_cache_size, state.thumbnail_max_threads
        )
        self._file_thumb_loader.apply_settings(
            state.thumbnail_cache_size, state.thumbnail_max_threads
        )
        # Persistent cache budgets (independent: thumbnail eviction never
        # touches the aspect cache).
        #
        # 予算の適用は sqlite を叩く（``prune`` は全表走査）ので、破損 DB を
        # 踏むと ``sqlite3.DatabaseError`` が出る。ここは設定ダイアログの適用
        # スロットから同期で走る経路なので、素通しだと Qt のスロットを貫通して
        # しまい、「設定を反映しました」の代わりにアプリごと落ちる（起動時は
        # ``_open_cache`` が同じ失敗を握って ``None`` 劣化起動する）。1 本ごとにログして続行し、他のキャッシュと以降の設定反映は
        # 巻き添えにしない。
        if self._disk_cache is not None:
            try:
                self._disk_cache.set_max_bytes(
                    state.thumb_disk_cache_max_mib * 1024 * 1024
                )
                # Enforce the (possibly shrunk) budget immediately, symmetric
                # with folder_cache / search_index below.  Without this a budget
                # cut only takes effect at the next startup prune, dumping the
                # whole over-budget unlink onto the GUI thread before the window
                # paints.  ThumbDiskCache.prune defers the blob unlink to a
                # daemon thread, so this call is GUI-safe.
                self._disk_cache.prune()
                # ``cache_edge`` は**全ローダー**へ配る。ディスクキャッシュは 5 本で共有していて、
                # ``cache_edge`` は「この長辺を超える要求ではディスク
                # キャッシュを丸ごとバイパスする / 保存マスターの長辺」を
                # 決める純粋なユーザー設定なので、2 本だけに配ると設定変更後
                # は同じ共有キャッシュに対して 2 本と 3 本が別々の長辺で
                # 読み書きし続ける。列挙は ``_all_loaders`` に一本化して
                # ある（手書きの集合を増やさない）。
                for loader in self._all_loaders():
                    loader.set_disk_cache(
                        self._disk_cache, state.thumb_disk_cache_max_edge
                    )
            except Exception as exc:
                logger.warning(
                    "サムネイルディスクキャッシュへの設定反映に失敗しました: {}",
                    exc,
                )
        if self._meta_cache is not None:
            try:
                self._meta_cache.prune(state.aspect_cache_max_mib * 1024 * 1024)
            except Exception as exc:
                logger.warning(
                    "アスペクトキャッシュへの設定反映に失敗しました: {}", exc,
                )
        if self._folder_cache is not None:
            try:
                self._folder_cache.set_max_bytes(
                    state.folder_preview_cache_max_mib * 1024 * 1024
                )
                self._folder_cache.prune()
            except Exception as exc:
                logger.warning(
                    "フォルダプレビューキャッシュへの設定反映に失敗しました: {}",
                    exc,
                )
        if self._search_index is not None:
            try:
                self._search_index.set_max_bytes(
                    state.search_index_max_mib * 1024 * 1024
                )
                self._search_index.prune()
            except Exception as exc:
                logger.warning("検索索引への設定反映に失敗しました: {}", exc)
        set_preview_scroll_pixels(state.preview_scroll_pixels)
        set_zip_preview_size_limit(
            state.zip_preview_size_limit_mib * 1024 * 1024
        )
        set_pdf_preview_size_limit(
            state.pdf_preview_size_limit_mib * 1024 * 1024
        )
        set_text_preview_max_bytes(state.text_preview_max_mib * 1024 * 1024)
        set_wheel_nav_grace_ms(state.wheel_nav_grace_ms)
        set_image_wheel_zoom(state.image_wheel_zoom)
        set_image_fit_no_upscale(state.image_fit_no_upscale)
        # View-preferences (image zoom-persist / minimap, markdown font, media
        # loop + autoplay + volume) — apply_view_settings routes media through
        # apply_media_settings so calling it here covers the media path too.
        self._content.apply_view_settings(state)
        # 閲覧モード: slideshow interval + media (loop/autoplay/volume) apply
        # live to an open lightbox (mirror the centre pane so a
        # settings change reaches the lightbox MediaView without reopening).
        if self._lightbox is not None:
            self._lightbox.set_slideshow_interval(state.slideshow_interval_sec)
            self._lightbox.set_chrome_hide_ms(state.lightbox_chrome_hide_ms)
            self._lightbox.apply_media_settings(state)
            # ImageView 系設定（キャッシュ / 先読み / ズーム維持 / ミニマップ）
            # も同じく生きたまま反映する。
            self._lightbox.apply_view_state(state)

    # ------------------------------------------- 走査ライフサイクル（委譲）
    #
    # 実装はウィンドウ部品 ``window_status.WindowStatus``（``self._status``）。
    # 状態ラベルの素材（``load_status`` ほか）と走査着地で消費する one-shot 2 本
    # （``startup_focus_pending`` / ``stage_settle_pending``）はすべて部品が所有
    # し、ここに残すのはシグナル接続先とテスト / UI 撮影ハーネスが名指しする
    # 口だけ。同名の ``_`` 付き属性は下の透過プロパティで部品の状態へ抜ける。

    def _settle_startup_focus(self) -> None:
        """初回スキャン着地の一手 — 実体は ``window_status.WindowStatus``.

        窓側に口を残すのは、UI の撮影ハーネスがこの名前を差し替えて「初回着地の
        1 枚」を撮るため（部品側もこの口を経由して呼ぶ）。
        """
        self._status.settle_startup_focus()

    def _on_loading_changed(self, loading: bool) -> None:
        """左ペイン走査の立ち上がり / 着地 — 実体は ``window_status.WindowStatus``."""
        self._status.on_loading_changed(loading)

    def _on_scan_partial(self, unclassified: int) -> None:
        self._status.on_scan_partial(unclassified)

    def _on_scan_failed(self, message: str) -> None:
        self._status.on_scan_failed(message)

    def _on_search_status_changed(self, text: str) -> None:
        self._status.on_search_status_changed(text)

    def _on_thumb_pending_changed(self, count: int) -> None:
        self._status.on_thumb_pending_changed(count)

    def _refresh_status_label(self) -> None:
        """ステータスバー右端の状態ラベル — 実体は ``window_status.WindowStatus``."""
        self._status.refresh_label()

    def _set_path_status(
        self, path: Path | None, *, representative: bool = False,
    ) -> None:
        # Show the currently selected / previewed item's path in the
        # drag-selectable bottom-left label.  Tooltip carries the full path
        # so long values clipped by the status bar are still readable.
        # With no selection, fall back to the current browse location so the
        # status bar always answers "where am I" even while the breadcrumb is
        # collapsed at narrow pane widths.
        if path is None:
            path = self._current_folder or self._root
        text = str(path) if path is not None else ""
        # The default library shows the Japanese 「ライブラリ」 label rather than
        # its raw English folder name as the current location;
        # the tooltip still carries the real path.
        label = (
            base_label(path, self._compute_library_bases())
            if path is not None
            else None
        )
        display = label if label is not None else text
        self._path_label.setText(display)
        # setText scrolls a long value to its end; show it from the start.
        self._path_label.setCursorPosition(0)
        self._path_label.setToolTip(text)
        # Status bar: selected file's name + size (one os.stat, best-effort —
        # failures / directories just clear the segment).  *representative* は
        # 呼び出し側（代表画像の自動選択を知っている経路）から素通しする —
        # 「代表:」接頭の出し分けをこの funnel 1 本に揃えるため。
        self._update_file_info_label(path, representative=representative)
        # This is the single funnel every selection/preview passes through, so
        # it's where we keep the detail window in sync with the current item.
        self._current_preview_path = path
        # …and where the「ウィンドウが選んだ代表画像」マークを落とす:
        # 別の物が選ばれた時点でフォールバック対象ではなくなる。代表画像の
        # 自動選択（pending-select 解決）は同じパスなのでマークが残る。
        # getattr ガード: この funnel はテストハーネスが `__init__` を通さずに
        # （`ViewerWindow.__new__`）呼ぶため、素の属性アクセスだと
        # AttributeError になる（plugin_events emit と同じ既存規約）。
        #
        # **代表画像プローブの世代バンプも同じ分岐で落とす**。マークと世代は
        # 「いま中央が映しているのは、もうプローブが選ぼうとしていた物では
        # ない」という**同じ事実**を表す 2 機構なので、後者を選択ハンドラ側の
        # 手書きにすると経路を 1 つ足すたび同じ取りこぼしが起きる。
        # ``_preview_probe_folder`` との比較は二重の降ろしを避けるためだけの
        # もの（``_on_folder_selected`` の直後に ``_start_first_image_probe``
        # 自身の投入＝追い越しが続く）。代表画像の**自動**選択は
        # ``path == _preview_fallback.shown`` が成立するのでここへ入らず、
        # ``#thumb#`` 暫定表示から非マーカー画像への非同期差し替えは生きる。
        fallback = getattr(self, "_preview_fallback", None)
        if fallback is None or path != fallback.shown:
            if fallback is not None:
                fallback.shown = None
            if path != getattr(self, "_preview_probe_folder", None):
                stream = getattr(self, "_preview_stream", None)
                if stream is not None:
                    stream.cancel()
        if self._detail_window is not None and self._detail_window.isVisible():
            self._detail_window.show_path(path)

    def _update_file_info_label(
        self, path: Path | None, *, representative: bool = False,
    ) -> None:
        """Refresh the status bar's "filename · size" segment for *path*.

        The stat runs off-thread (``_file_info_stream`` + ``_stat_file_label``)
        so a cold-NAS round-trip never blocks selection changes; a directory,
        missing path, or ``os.stat`` failure clears the segment rather than
        surfacing an error (the path is already shown in the bottom-left
        label).

        *representative* を立てると「代表:」の接頭を付けて表示する
        （フォルダ選択中の代表画像プレビューでは選択がディレクトリなので、
        付けなければこのセグメントが空になり、隣に W×H だけが出て
        「どのファイルを見ているのか」が画面のどこにも無くなる）。接頭は省略
        不可: 無いと「このファイルが選択中」と誤読され、実際の選択が
        フォルダであるという事実と衝突する。
        """
        # 走っている stat を降ろす（``path is None`` の枝では投げ直さない）。
        self._file_info_stream.cancel()
        self._file_info_representative = bool(representative)
        self._file_info_path = path
        if path is None:
            self._file_info_label.setText("")
            self._file_info_label.setToolTip("")
            return
        self._file_info_stream.submit(lambda p=path: _stat_file_label(p))

    def _on_file_info_statted(self, text: object) -> None:
        full = text if isinstance(text, str) else ""
        rep_tooltip = ""
        # getattr ガードは __init__ を通さないテストハーネス向け（既存規約）。
        if full and getattr(self, "_file_info_representative", False):
            full = t("viewer.main_window.file_info_representative", info=full)
            rep_tooltip = t(
                "viewer.main_window.file_info_representative_tooltip",
                path=self._relative_display_path(self._file_info_path),
            )
        # 上限幅を超える長大ファイル名は中央省略 —
        # permanent widget は sizeHint 分の幅を確保するため、素の setText では
        # 255 バイト級の正当な境界入力でステータスバーの他セグメント
        # （現在パス表示・サイズ表記）が圧殺・クリップされる。
        elided = self._file_info_label.fontMetrics().elidedText(
            full, Qt.ElideMiddle, _FILE_INFO_LABEL_MAX_PX
        )
        self._file_info_label.setText(elided)
        # 代表画像のときは常に「どこのファイルか」をツールチップで補う
        # （相対パス — BFS 降下でサブフォルダの絵が代表になることがある）。
        self._file_info_label.setToolTip(
            rep_tooltip or (full if elided != full else "")
        )

    def _relative_display_path(self, path: "Path | None") -> str:
        """*path* を現在フォルダからの相対表記で（無理ならフルパスで）返す."""
        if path is None:
            return ""
        base = self._current_folder or self._root
        if base is not None:
            try:
                return str(path.relative_to(base))
            except ValueError:
                pass
        return str(path)

    def _on_counts_changed(self, folders: int, files: int) -> None:
        self._status.on_counts_changed(folders, files)

    def _is_grid_seat_collapsed(self) -> bool:
        """グリッド席が畳まれているか — 実体は ``window_status.WindowStatus``."""
        return self._status.is_grid_seat_collapsed()

    def _is_preview_seat_collapsed(self) -> bool:
        """プレビュー列が畳まれているか — 実体は ``window_status.WindowStatus``."""
        return self._status.is_preview_seat_collapsed()

    def _is_info_seat_collapsed(self) -> bool:
        """右情報パネルの席が畳まれているか — 実体は ``window_status.WindowStatus``."""
        return self._status.is_info_seat_collapsed()

    def _empty_state_input(self) -> EmptyStateInput:
        """空状態リゾルバへの観測値 — 実体は ``window_status.WindowStatus``."""
        return self._status.empty_state_input()

    def _resync_placeholder_on_seat_change(self) -> None:
        self._status.resync_placeholder_on_seat_change()

    def _sync_centre_placeholder(self) -> None:
        """空状態オーケストレータ — 実体は ``window_status.WindowStatus``.

        3 ペインの空状態を 1 か所で裁定する唯一の適用点。ウィンドウ側に口を
        残すのは、席トグル / 件数着地 / テストがこの名前で呼ぶため。
        """
        self._status.sync_centre_placeholder()

    def _on_image_info_changed(self, width: int, height: int) -> None:
        self._status.on_image_info_changed(width, height)

    def _update_resolution_label(self) -> None:
        self._status.update_resolution_label()

    # ------------------------------------------- view-preference write-back

    def _on_image_zoom_persist_toggled(self, on: bool) -> None:
        self._state.image_zoom_persist = on

    def _on_image_minimap_toggled(self, on: bool) -> None:
        self._state.image_minimap_enabled = on

    def _on_markdown_font_pt_changed(self, pt: int) -> None:
        self._state.markdown_font_pt = pt

    def _on_media_loop_toggled(self, on: bool) -> None:
        self._state.media_loop = on

    def _on_media_volume_changed(self, vol: int) -> None:
        # F07: persist the user's volume so it survives a restart (saved on
        # close alongside the other view preferences).
        self._state.media_volume = vol

    def _on_media_playback_rate_changed(self, rate: float) -> None:
        # 速度も loop / volume と同じく永続化する。
        self._state.media_playback_rate = float(rate)

    # ------------------------------------------------------- detail window

    def _open_detail_window(self) -> None:
        if self._detail_window is None:
            self._detail_window = DetailWindow(
                self._tag_index, self,
                similar_available=self._vector_index is not None,
                # ★ / あとで見る / ユーザータグ の 3 行。
                user_meta=self._user_meta,
            )
            self._detail_window.destroyed.connect(self._on_detail_window_destroyed)
            # C-10: "このタグで検索" from the tag table drives the left pane's
            # AI-tag search (add_search_tag enables + expands the panel).
            self._detail_window.tag_search_requested.connect(
                self._on_detail_tag_search
            )
            # E15: "この画像で類似検索" seeds the left pane's similar-image search
            # from the detail window's current image.
            self._detail_window.similar_search_requested.connect(
                self._on_detail_similar_search
            )
        self._detail_window.show_path(self._current_preview_path)
        self._detail_window.show()
        self._detail_window.raise_()
        self._detail_window.activateWindow()

    def _on_detail_tag_search(self, tag: str) -> None:
        """詳細情報ウィンドウの「このタグで検索」.

        効果（条件チップバーの更新）は**背後の本窓にしか出ない**ので、
        モードレスな詳細情報ウィンドウが重なっているとその場に何の
        反応も無いように見える。本窓の共通トーストファンネルへ 1 本流して、
        隣の「この画像で類似検索」と同じ扱いに揃える。
        """
        self._post_grid.add_search_tag(tag)
        self._show_toast(
            t("viewer.main_window.detail_tag_search_toast", tag=tag), "info"
        )

    def _on_detail_similar_search(self, path: object) -> None:
        # E15: the detail window's 「この画像で類似検索」 seeds the left pane's
        # similar-image search.  set_similar_seed enables semantic mode + kicks
        # the vector scan; a no-op without a VectorIndex (the button is hidden
        # then anyway).
        if isinstance(path, Path):
            self._post_grid.set_similar_seed(path)
            # タグ検索と同じく、効果が出る面（本窓）で受理を告げる。
            self._show_toast(
                t(
                    "viewer.main_window.detail_similar_search_toast",
                    name=path.name,
                ),
                "info",
            )

    def _on_detail_window_destroyed(self) -> None:
        self._detail_window = None

    def _on_theme_chosen(self, key: str) -> None:
        # 表示メニューのテーマ項目から選ばれた経路。共通の適用ハンドラへ委譲する。
        self._apply_theme_choice(key)

    def _apply_theme_choice(self, key: str) -> None:
        """Apply *key* as the live theme + persist it (menu / dialog shared).

        Both the 表示メニュー (``_on_theme_chosen``) and the settings dialog's
        theme combo (I02) funnel through here so the check state, the live
        palette, and ``shared_prefs.json`` stay in lockstep whichever entry
        point the user used.
        """
        self._state.theme = key  # type: ignore[assignment]
        self._sync_theme_menu_check(key)
        apply_theme(key)
        # ``shared_prefs.json`` is the theme's single source of truth across
        # all tools — mirror the choice there so plugin windows adopt it on
        # its next launch (no live cross-process sync).  The local
        # ``viewer_state.json`` write (on close) stays for round-trip compat.
        # A write failure is logged only and must not break the UI.
        #
        # 書き込みは**背景で・待たない**。
        # ``update_shared_prefs`` は ``load_shared_prefs`` の read →
        # ``save_shared_prefs`` の tmp 書き込み + ``os.replace`` という
        # 同期 read-modify-write で、``data/`` がチーム共有の NAS に載る
        # 想定運用（この製品の中心的な使い方）では 1 回の I/O が 15〜195 秒
        # ブロックする（VM 実測 — ``_CLOSE_PERSIST_BUDGET_S`` の docstring）。
        # テーマ切替は最も頻繁な操作で、in-memory と ``apply_theme`` で
        # 見た目は既に反映済み。失敗しても実害は「次回起動時に他ツールへ
        # 伝播しない」だけなので、成否を待つ理由が無い（設定ダイアログのような
        # 成否表示契約はこの経路には無い）。
        self._shared_prefs_flush.run(
            lambda: self._write_theme_pref(key), wait_s=0.0,
        )

    @staticmethod
    def _write_theme_pref(key: str) -> None:
        """``shared_prefs.json`` へテーマを書く（ワーカースレッド専用）.

        ウィジェットに触らないこと（:class:`BoundedFlusher` の規約）。
        """
        try:
            update_shared_prefs(theme=key)
        except OSError as exc:
            logger.warning("could not write theme to shared_prefs.json: {}", exc)

    def _sync_theme_menu_check(self, key: str) -> None:
        """Check the 表示メニュー theme action matching *key* (I02 sync).

        Checks the winner and unchecks the rest explicitly: the action group
        is exclusive, but its uncheck-the-others behaviour rides on the
        checked action's signals — which are blocked here precisely so this
        sync can't re-fire ``_on_theme_chosen`` when the dialog (not the
        menu) is the source of the change.
        """
        group = getattr(self, "_theme_group", None)
        if group is None:
            return
        for act in group.actions():
            want = act.data() == key
            if act.isChecked() != want:
                blocked = act.blockSignals(True)
                act.setChecked(want)
                act.blockSignals(blocked)

    # ------------------------------------------------------- ウィンドウ部品

    @property
    def _help(self) -> WindowHelp:
        """ヘルプ + 診断の部品（:class:`window_help.WindowHelp`）.

        メニュー 2 本（ヘルプ / 診断）が起動する面 — モードレス子窓 4 本・
        同梱文書の口・計測トグル・エクスプローラ統合 — の実装はここが持つ。
        初回アクセスで作るのは、``ViewerWindow.__init__`` を通さないテスト
        ハーネス（``ViewerWindow.__new__`` + ``QMainWindow.__init__`` +
        必要な属性だけスタブ）から委譲メソッドが呼ばれても成立させるため
        （このファイル全体の ``getattr`` ガードと同じ規約）。部品は窓を
        **親に持つ QObject** なので、``QMainWindow.__init__`` まで通って
        いない殻では shiboken が ``RuntimeError`` を出す — 素の
        ``__new__`` だけのハーネスは元々この経路に触れない。
        """
        part = self.__dict__.get("_help_part")
        if part is None:
            part = WindowHelp(self)
            self.__dict__["_help_part"] = part
        return part

    @property
    def _status(self) -> WindowStatus:
        """ステータスバー合成 + 席裁定の部品（:class:`window_status.WindowStatus`）.

        遅延生成の理由は :attr:`_help` と同じ。
        """
        part = self.__dict__.get("_status_part")
        if part is None:
            part = WindowStatus(self)
            self.__dict__["_status_part"] = part
        return part

    # 状態ラベルの素材と走査着地の one-shot は ``WindowStatus`` が所有する。
    # ここに残すのは**透過プロパティ**だけ — テストと他クラスタ（``set_root`` /
    # ライトボックスの復路 / 起動シーケンス）がこの名前で読み書きし続けられる
    # ようにするための口で、値の置き場は部品側 1 か所。

    @property
    def _load_status(self) -> str:
        """ステータスバー右端の読み込み状態（実体は ``WindowStatus``）."""
        return self._status.load_status

    @_load_status.setter
    def _load_status(self, value: str) -> None:
        self._status.load_status = value

    @property
    def _thumb_status(self) -> str:
        """サムネイル生成の残件文言（実体は ``WindowStatus``）."""
        return self._status.thumb_status

    @_thumb_status.setter
    def _thumb_status(self, value: str) -> None:
        self._status.thumb_status = value

    @property
    def _scan_loading(self) -> bool:
        """左ペイン走査が進行中か（実体は ``WindowStatus``）."""
        return self._status.scan_loading

    @_scan_loading.setter
    def _scan_loading(self, value: bool) -> None:
        self._status.scan_loading = value

    @property
    def _load_failed(self) -> bool:
        """現ルートの走査が失敗した sticky フラグ（実体は ``WindowStatus``）."""
        return self._status.load_failed

    @_load_failed.setter
    def _load_failed(self, value: bool) -> None:
        self._status.load_failed = value

    @property
    def _startup_focus_pending(self) -> bool:
        """初回スキャン着地の一手が未消費か（実体は ``WindowStatus``）."""
        return self._status.startup_focus_pending

    @_startup_focus_pending.setter
    def _startup_focus_pending(self, value: bool) -> None:
        self._status.startup_focus_pending = value

    @property
    def _stage_settle_pending(self) -> bool:
        """ステージ維持の保険が未消費か（実体は ``WindowStatus``）."""
        return self._status.stage_settle_pending

    @_stage_settle_pending.setter
    def _stage_settle_pending(self, value: bool) -> None:
        self._status.stage_settle_pending = value

    # ----------------------------------------------------------- diagnostics
    #
    # 実装はウィンドウ部品 ``window_help.WindowHelp``（``self._help``）。ここに
    # 残すのは、テスト / UI 撮影ハーネス / 他モジュールが名指ししている
    # 委譲の口だけ（それ以外の入口はメニューから部品へ直接つながる）。

    def _on_toggle_perf(self, enabled: bool) -> None:
        self._help.toggle_perf(enabled)

    def _open_perf_dialog(self) -> None:
        self._help.open_perf_dialog()

    def _on_perf_enabled_changed(self, enabled: bool) -> None:
        self._help.on_perf_enabled_changed(enabled)

    @property
    def _perf_dialog(self) -> "PerfDialog | None":
        """モードレス計測統計ダイアログ（実体は部品が所有）."""
        return self._help.perf_dialog

    def _open_health_dialog(self) -> None:
        self._help.open_health_dialog()

    @property
    def _health_dialog(self):
        """モードレス健全性チェックダイアログ（実体は部品が所有）."""
        return self._help.health_dialog

    def _open_cache_prebuild(self) -> None:
        self._help.open_cache_prebuild()

    def _open_shell_integration_dialog(self) -> None:
        """診断 ▸ エクスプローラ統合… — 実体は ``window_help.WindowHelp``."""
        self._help.open_shell_integration_dialog()

    # ------------------------------------------------------------------ help

    def _open_shortcuts_dialog(self, task: str | None = None) -> None:
        self._help.open_shortcuts_dialog(task)

    def _on_help_shortcut(self) -> None:
        self._help.on_help_shortcut()

    @property
    def _shortcuts_dialog(self) -> "ShortcutsDialog | None":
        """モードレス操作ガイド（実体は部品が所有）."""
        return self._help.shortcuts_dialog

    def open_logs_folder(self) -> None:
        """Public entry for panes that offer 「ログフォルダを開く」.

        走査失敗カードは自分でファイラを起動せず、ホストの 1 実装を呼ぶ
        （失敗時の通知も含めて経路が 1 本になる）。
        """
        self._help.open_logs_folder()

    def _show_about(self) -> None:
        self._help.show_about()

    def _show_terms(self) -> None:
        self._help.show_terms()

    def _open_with_default_app(self, path: Path) -> None:
        """Hand *path* to the OS default application.

        Used when a file has no dedicated in-app preview: double-click / Enter
        should "open" it the way Explorer would.  実装は共通ヘルパ
        ``view_prefs.open_with_default``（右クリック
        経路・各プレビューのボタンと同じ 1 か所）で、失敗時のステータス通知も
        そこが行う。
        """
        open_with_default(path, self)

    def _on_copy_current_image(self) -> None:
        # Menu entry (編集 → 表示中の画像をコピー).  Only the image page can copy;
        # 成功トーストは ``ImageView.copy_image_to_clipboard`` の内側 1 箇所が
        # 出す（3 入口で一貫させるため、ここでは重ねて出さない）。失敗理由
        # だけを出す。直書きの ``showMessage`` にすると同じ文言が Ctrl+C 経路
        # （``image_view`` のトースト）と別の面に出るので、「開く」失敗と同じ
        # 共通ファネルへ寄せて 1 面に揃える。
        if not self._content.copy_current_image():
            notify_failure(self, t("viewer.main_window.no_copyable_image"))

    # ------------------------------------------------------------- geometry

    def _restore_geometry(self) -> None:
        """Restore the persisted geometry, rejecting an off-screen result.

        Qt itself clamps a restored frame back onto an attached screen in
        the common cases (measured on Qt 6.11: a saved 40000,40000 comes
        back at the primary screen's origin), so this guard is a backstop,
        not the primary defence — it exists for the residue Qt's clamp
        does not cover (``restoreGeometry`` returning False on a blob from
        a different Qt version, and screen configurations that change
        between the restore and the show).

        Because it *is* a backstop, it has to actually work when it fires:
        recentre on the primary screen rather than only resizing.  Resizing
        alone leaves an off-screen window off-screen — the very symptom
        being guarded against.  判定と復帰導線はどちらも
        ``common/ui/window_geometry.py`` の共有ヘルパ — プラグインの窓も
        同じ 2 関数を使う（同型のガードを別々に手書きすると、片側だけが
        ``resize`` のみの no-op で残りやすい）。
        """
        raw = decode_geometry(self._state.geometry_b64)
        if raw:
            if (
                not self.restoreGeometry(QByteArray(raw))
                or not frame_intersects_any_screen(self)
            ):
                center_on_primary(self, _DEFAULT_WINDOW_SIZE)

    def _all_loaders(self) -> list[ThumbnailLoader]:
        """このウィンドウが所有する ThumbnailLoader を**全部**集める。

        窓は 5 本のローダーを持つ（グリッド ``_loader`` / 右ペイン
        ``_file_thumb_loader`` / フォルダプレビュー子タイル
        ``_folder_preview_loader`` / 遅延生成のフィルムストリップ
        ``_strip_loader`` / 全画面 ``_lightbox_loader``）。「全ローダーに
        当てる」処理（closeEvent のドレイン、「サムネイルキャッシュを削除」
        の記憶 LRU フラッシュ）は必ずここを通すこと — 手書きで一部だけ
        列挙すると、残りのローダーがセッション中ずっと古い絵を返し続ける。
        遅延生成の 2 本は未構築なら
        単に含まれない（構築された時点で自動的に対象へ入る）ので、
        呼び出し側は**都度**このメソッドを呼ぶこと（結果を持ち回らない）。
        """
        loaders = [
            self._loader,
            self._file_thumb_loader,
            self._folder_preview_loader,
        ]
        if self._lightbox_loader is not None:
            loaders.append(self._lightbox_loader)
        if self._strip_loader is not None:
            loaders.append(self._strip_loader)
        return loaders

    def _drain_loader_pools(self, timeout_ms: int) -> None:
        """Clear queued + bounded-wait every worker pool the window owns.

        Called during ``closeEvent`` before the disk cache is closed so no
        decode worker outlives the cache it stores into.  Goes through the
        loaders' public shutdown API: ``request_shutdown()`` releases decodes
        parked in a bounded wait (a broken video otherwise pins its worker
        for up to 8 s — past this drain's timeout, and, if the wait straddles
        interpreter teardown, forever: the non-daemon pool thread then blocks
        process exit), ``clear_cache()`` drops the pending request
        queues, ``wait_for_done(timeout)`` bounds the wait on the (at most a
        few) already-running decodes rather than blocking indefinitely on NAS
        I/O.  Best-effort — a failure only means a slightly less orderly
        teardown, but it is logged so a hung worker is visible post-mortem.

        ``GuardedStream`` の各プールも同じ予算に載せる: あれは所有ウィジェットの子なので「ウィジェットと一緒に
        片付く」が、``QThreadPool`` のデストラクタは in-flight タスクを無制限
        に待つ = 予算の**外側**（closeEvent 後のウィジェット木の破棄）で死んだ
        共有の I/O タイムアウトぶん止まる。列挙は ``findChildren`` なので、
        新しいストリームを足しても片側欠落にならない。予算は 3 段目と**同じ
        1 本**の ``Deadline`` を共有する（本数ぶん ``timeout_ms`` が積算すると、
        ストリームを足すほど最悪の閉じ待ちが伸びる）。

        **最後に窓の QObject 木そのものから掃く**: 上の 2 ループは「ローダー」
        「``GuardedStream``」という*種類*の列挙なので、生の ``QThreadPool`` を
        自前で持つ葉（プレビュー各種 / 詳細情報ウィンドウ / 各スキャナ）は
        どちらにも載らず、破棄時の無制限待ちが残る。窓配下のプールは
        全て ``QThreadPool(<ウィジェット>)`` = 窓の子孫なので、
        ``findChildren(QThreadPool)`` が母集団の全員に到達する（子トップ
        レベル窓も含む）。ここを通せば新しい葉は**何も配線しなくても**予算内
        ドレインに載る。予算は 1 本の :class:`Deadline` を全プールで共有する
        （プールの本数ぶん ``timeout_ms`` が積算しないように）。

        キャンセルを持つ shutdown（``_zip_drill`` / ``_cache_ctrl`` /
        ``shutdown_folder_preview``）は**この呼び出しより前**に置くこと:
        汎用パスが吸収できるのは「待ち」だけで、キャンセルされていない仕事は
        予算いっぱい待つことになる。
        """
        # 3 段とも**同じ 1 本の予算**で回す（ローダー / ストリーム / プールの
        # 本数ぶん ``timeout_ms`` が積算しないように — 段ごとに満額を配ると、
        # 死んだ共有で 5 本がそれぞれ塞がったとき、ここだけで
        # 5 × ``timeout_ms`` 止まる）。
        budget = Deadline(timeout_ms / 1000.0)
        loaders = self._all_loaders()
        # 先に**全部の**ローダーへ停止要求と待ち行列の破棄を当ててから待つ:
        # 1 本目の待ちの間に後続のローダーが未着手の仕事を拾い続けないように。
        for loader in loaders:
            try:
                loader.request_shutdown()
                loader.clear_cache()
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning("thumbnail pool shutdown request failed: {}", exc)
        for loader in loaders:
            try:
                share = int(budget.remaining * 1000)
                if not loader.wait_for_done(share):
                    logger.warning(
                        "thumbnail pool did not drain within {}ms "
                        "(a decode worker is still running)", share,
                    )
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning("thumbnail pool drain failed: {}", exc)
        for stream in self.findChildren(GuardedStream):
            try:
                # ログに出すのは**この 1 本に配れた残予算**（`timeout_ms` では
                # ない）。先頭の 1 本が死んだ共有で予算を食い切ると後続は
                # 0ms = cancel だけになるので、満額待って落ちたのか 1 ミリ秒も
                # 待っていないのかが事後解析で読めなくなる。
                share = int(budget.remaining * 1000)
                if not stream.request_shutdown(share):
                    logger.warning(
                        "guarded stream did not drain within {}ms "
                        "(a blocking read is still running)", share,
                    )
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning("guarded stream drain failed: {}", exc)
        # 予算内に空かなかったプールは ``drain_or_strand`` が窓の木から外して
        # 退避する — 外さないと窓の破棄時にプールのデストラクタが走行中の
        # タスクを無制限に待ち、待ちが予算の外側へ逃げるだけになる（終了の
        # 有界化は ``app.main`` の ``exit_if_pools_stranded`` が受け持つ）。
        for pool in self.findChildren(QThreadPool):
            try:
                share = int(budget.remaining * 1000)
                if not drain_or_strand(pool, share):
                    logger.warning(
                        "worker pool did not drain within {}ms "
                        "(a blocking task is still running)", share,
                    )
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning("worker pool drain failed: {}", exc)

    def _collect_state(self) -> None:
        """Fold the live UI state into ``self._state`` (shared by closeEvent
        and the periodic autosave, B03).

        **ここでディスクへ触らないこと**。ウィジェットからの
        収集は GUI スレッドでしかできない一方、ディスク側との突き合わせ
        （:meth:`_merge_disk_state`）は ``viewer_state.json`` の**読み込み**
        なので、到達不能な共有では呼び出しスレッドを数十秒ブロックする。
        突き合わせと書き込みは両方 :meth:`_save_merged_state` が持ち、常に
        予算付きワーカー側で走る（この関数を I/O 込みへ戻すと、GUI スレッド
        でのブロックが復活する）。
        """
        (
            sort_mode,
            icon_size,
            grid_view_mode,
            exclude_thumb_marker,
            grid_thumb_layout,
        ) = self._post_grid.current_state()
        self._state.geometry_b64 = encode_geometry(self.saveGeometry())
        sizes = self._splitter.sizes()
        # 情報パネル: persist its visibility (幅 0 までドラッグで
        # 畳んだ状態はトグル OFF と同一視), and when it
        # is hidden/collapsed substitute the remembered width so the persisted
        # right column isn't frozen at 0 (which would re-open the panel
        # collapsed next launch).
        self._state.info_panel_visible = (
            not self._info_panel.isHidden()
            and (len(sizes) != 3 or sizes[2] > 0)
        )
        if len(sizes) == 3 and (self._info_panel.isHidden() or sizes[2] == 0):
            sizes = [sizes[0], sizes[1], self._info_panel_saved_width]
        # ナビレール: same treatment — persist visibility and
        # substitute the remembered width so column 0 isn't frozen at 0.
        self._state.nav_rail_visible = (
            not self._nav_rail.isHidden()
            and (len(sizes) != 3 or sizes[0] > 0)
        )
        if len(sizes) == 3 and (self._nav_rail.isHidden() or sizes[0] == 0):
            sizes = [self._nav_rail_saved_width, sizes[1], sizes[2]]
        self._state.splitter_sizes = sizes
        # 中央 [グリッド | プレビュー] の分割比 (split-view redesign 2026-07)。
        # どちらかの席が 0（最大化 [0, x] / プレビュー畳み [x, 0]）のときは
        # 記憶している分割比を代わりに永続する — 畳み/最大化の状態自体は
        # ``preview_visible``（とモード非永続 = 常に分割で起動）が担い、
        # 比率スナップショットには常に「復元できる分割」だけを書く。
        split_sizes = self._center_split.sizes()
        if len(split_sizes) == 2 and (
            split_sizes[0] == 0 or split_sizes[1] == 0
        ):
            split_sizes = list(self._center_split_saved)
        self._state.center_split_sizes = split_sizes
        # プレビュー列の表示状態（折り畳み導線 2026-07）。ドラッグ 0 も
        # トグル OFF もフラグ側 (_set_preview_visible_flag) で同一視済み。
        self._state.preview_visible = self._preview_visible
        self._state.last_root = str(self._root) if self._root else ""
        # ZIP ドリルインの展開先は閉じると消える — 生の一時パスを ``last_root``
        # に書くと次回は missing でライブラリへ落ち（開いていた位置を失う）、
        # 掃除に失敗して残っていればアプリの ``data/tmp`` をルートに起動する。
        # 展開元 ZIP の親フォルダ + その ZIP の選択として書き、閉じた位置へ戻す。
        zip_resume = self._zip_resume_position()
        if zip_resume is not None:
            self._state.last_root = zip_resume[0]
        # Startup resume (B01/B02): remember the selection / previewed file /
        # scroll offset.  ``current_path`` reads the built tiles only (no I/O).
        # BUT: while the startup restore is still in flight (cold NAS — the
        # scan hasn't populated the grid, so the live UI is empty), skip these
        # three so a debounced autosave doesn't overwrite the saved targets
        # with blanks.  Keep the existing self._state values until the restore
        # lands (selection resolved / preview consumed).
        sel = self._post_grid.current_path()
        restore_in_flight = (
            self._pending_restore_preview.armed
            or (self._startup_restore_pending and sel is None)
        )
        if zip_resume is not None:
            # 展開先の中の選択 / プレビュー / スクロールは親フォルダでは
            # 意味を持たない — 展開元 ZIP を選択として置き直す。
            self._state.last_selected_path = zip_resume[1]
            self._state.last_previewed_path = ""
            self._state.last_grid_scroll = 0
        elif not restore_in_flight:
            self._state.last_selected_path = str(sel) if sel is not None else ""
            self._state.last_previewed_path = (
                str(self._last_previewed_file)
                if self._last_previewed_file is not None else ""
            )
            self._state.last_grid_scroll = self._post_grid.scroll_value()
            # ZIP を出た直後などで一時パスが残っていても永続しない。
            if is_zip_temp_path(self._state.last_selected_path):
                self._state.last_selected_path = ""
            if is_zip_temp_path(self._state.last_previewed_path):
                self._state.last_previewed_path = ""
        self._state.sort_mode = sort_mode
        self._state.icon_size = icon_size
        self._state.list_icon_size = self._post_grid.current_list_icon_size()
        self._state.grid_view_mode = grid_view_mode  # type: ignore[assignment]
        self._state.file_list_view_mode = self._file_list.current_view_mode()  # type: ignore[assignment]
        self._state.file_list_sort_mode = self._file_list.current_sort_mode()
        self._state.file_list_icon_size = self._file_list.current_icon_size()
        self._state.file_list_list_icon_size = self._file_list.current_list_icon_size()
        # ``filter_locked_only`` は書き戻さない（save_state の _VOLATILE_FIELDS
        # で落ちる揮発フィールド）。
        self._state.hide_nsfw = self._post_grid.hide_nsfw()
        self._post_grid.save_tag_settings(self._state)
        self._state.exclude_thumb_marker = exclude_thumb_marker
        self._state.grid_thumb_layout = grid_thumb_layout  # type: ignore[assignment]
        self._state.file_list_thumb_layout = (
            self._file_list.current_thumb_layout()  # type: ignore[assignment]
        )

    def _zip_resume_position(self) -> "tuple[str, str] | None":
        """ルートが ZIP 展開先なら永続用の ``(last_root, last_selected_path)``.

        展開元 ZIP が分かれば「その親フォルダ + ZIP の選択」、分からない
        （掃除に失敗して前回から残った展開先で起動した）なら ``("", "")`` =
        次回は既定ライブラリ。展開先でなければ ``None``。
        """
        root = self._root
        if root is None or not is_zip_temp_path(root):
            return None
        for temp_dir, zip_path in self._zip_temp_dirs.items():
            if root == temp_dir or temp_dir in root.parents:
                return str(zip_path.parent), str(zip_path)
        return "", ""

    def _merge_disk_state(self, target: ViewerState) -> None:
        """ディスク側の並行インスタンスの追加を *target* へ取り込む.

        ``viewer_state.json`` を 1 回読むので **I/O を行う** — 呼び出し口は
        :meth:`_save_merged_state` **だけ**にすること（常に予算付きワーカー
        側で走り、GUI スレッドからは呼ばれない）。書き込み先を引数で
        受けるのは、稼働中の保存が GUI スレッドの ``self._state`` を後ろから
        書き換えないようにするため（保存はスナップショットの複製に対して
        行う）。
        """
        # Re-adopt bookmarks a concurrent (newer) instance saved to disk
        # while this window was open, minus the ones this window removed —
        # otherwise the LAST instance to close clobbers the others'
        # additions with its stale in-memory list.
        try:
            disk = load_state()
            target.bookmarks, target.bookmark_names = merge_bookmarks(
                target.bookmarks,
                target.bookmark_names,
                disk.bookmarks,
                disk.bookmark_names,
                self._session_removed_bookmarks,
                self._session_cleared_bookmark_names,
            )
            # 保存済み検索も同じ理由で disk とマージ: フル保存は
            # 全フィールド上書きなので、並行インスタンスが保存した検索を
            # 最後に閉じた側の stale リストで消さない。disk はブックマーク
            # merge が読んだ 1 回分を再利用（追加 I/O ゼロ）。
            target.saved_searches = merge_saved_searches(
                target.saved_searches,
                disk.saved_searches,
                self._session_removed_saved_searches,
            )
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("Bookmark merge on close failed: {}", exc)

    def _next_state_generation(self) -> int:
        """スナップショットへ通し番号を振る（GUI スレッド専用・再指摘 M-2）.

        新しいスナップショットほど大きい番号を持ち、:meth:`_save_merged_state`
        がこの順序でしかディスクへ載らないことを保証する。
        """
        self._state_snapshot_seq += 1
        return self._state_snapshot_seq

    def _save_merged_state(self, snapshot: ViewerState, generation: int) -> bool:
        """突き合わせ + 書き込み — **必ずワーカースレッドで走る**.

        ディスク突き合わせ（``load_state``）と書き込み（``save_state``）の
        両方が I/O なので、まとめて予算付きワーカーへ載せる唯一の口。呼ぶ
        時点で :meth:`_collect_state` が GUI 状態の収集を済ませているので、
        この関数はウィジェットに一切触れない — 触れさせないこと（Qt の
        ウィジェット API は GUI スレッド限定）。

        *snapshot* は GUI スレッドが作った ``self._state`` の複製で、merge の
        結果もここへ書く。``self._state`` を直接渡さないのは、稼働中の保存
        （オートセーブ）がワーカー側から GUI スレッドの状態を後ろから書き
        換えないため。**``_merge_disk_state`` を外さないこと**: 並行
        インスタンスがディスクへ足したブックマーク / 保存済み検索を、この
        窓の stale なリストで上書き消去する（＝「お気に入りが消える」事故。
        ブックマークは並行インスタンス間でマージする契約）。

        予算超過で放棄されても壊れないのは ``save_state`` が tmp +
        ``os.replace`` の原子的書き込みだから（既存の ``viewer_state.json``
        は無傷。取り残された中間 tmp は次の成功書き込みが掃除する —
        ``state.py::_write_json_atomic``）。

        **順序保証（M-2）**: ``_write_json_atomic`` が壊れたファイルを
        残さないのは「ファイル整合性」の話でしかなく、**内容**は守られない。
        死んだ共有では「SMB タイムアウト 1 周期ぶん古い」オートセーブのワーカー
        と終了時のワーカーが同時に飛び、どちらも全フィールドの完全置換なので、
        共有が復旧して両方が着地すると ``os.replace`` の順次第で**古い方が
        勝つ**（merge が守るのはブックマークと保存済み検索だけで、
        geometry / last_root / MRU / ソート / 全設定は守られない）。

        守り方は**世代番号だけ**で、I/O は直列化しない（再指摘 M-2 R4）。
        ``_state_write_lock`` を ``load_state`` + ``save_state`` ごと囲って
        いた頃は、詰まったオートセーブのワーカーがロックを握ったまま
        ``save_state`` にぶら下がり、``closeEvent`` の保存ワーカーは 3 秒の
        予算をまるごとロック待ちで溶かして**保存を試みることすらできなかった**
        （健全だが遅いディスク — 大きな WAL / USB — でオートセーブと × が
        1 tick 衝突しただけでも同じ）。いまロックが守るのは
        ``_state_written_gen`` の read-modify-write だけで、保持時間は
        マイクロ秒。

        世代の確認は **2 回**行う: 着手時（もう追い越されているなら I/O を
        始めない）と、``state.py::save_state`` の ``should_land`` フック＝
        **``os.replace`` の直前**。後者が本命で、「SMB タイムアウト 1 周期ぶん
        固まっていた古い保存」はここで捨てられる（tmp は消される）。書き込み
        自体は並行のままなので、詰まった保存が他の保存を待たせることは無い。
        いまは「最終確認 → ``os.replace``」が state.py 側の
        ``_STATE_FILE_LOCK`` の中で不可分に行われるので、
        この確認をすり抜けた 2 本が着地順を入れ替えることも無い。同じロックは
        ``load_state`` の読み取りも囲う — Windows では読み取り用に開かれた
        ファイルへの ``os.replace`` が失敗するため（＝保存の喪失 + 事実で
        ない「保存できませんでした」）。**囲うのは短い 2 操作だけで、tmp への
        書き込みはロックの外**なので、詰まった保存が他の保存を待たせる時間は
        依然として有界。
        """
        if not self._claim_state_generation(generation):
            return False
        try:
            # ここはブロックし得る I/O（死んだ共有では丸ごと張り付く）。
            # **ロックの外**で行うこと。
            self._merge_disk_state(snapshot)
            return save_state(
                snapshot,
                should_land=lambda: self._claim_state_generation(generation),
            )
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("Failed to persist viewer state: {}", exc)
            return False

    def _claim_state_generation(self, generation: int) -> bool:
        """*generation* がまだ最新なら記録して ``True``（M-2 の順序保証）.

        ロックが守るのはこの read-modify-write **だけ**。ここに I/O を持ち
        込むと、詰まった保存が他の保存を道連れにする（＝ closeEvent が 3 秒の
        予算をロック待ちで溶かし、保存を試みることすらできなくなる）。
        """
        with self._state_write_lock:
            if generation < self._state_written_gen:
                # 待っている間に新しいスナップショットが先に着地した
                # （＝この内容はもう古い）。上書きしない。
                logger.debug(
                    "stale state save dropped (gen {} < {})",
                    generation, self._state_written_gen,
                )
                return False
            self._state_written_gen = generation
            return True

    def _persist_state_snapshot(
        self, *, wait_s: float = 0.0,
    ) -> bool:
        """Collect + write the state file now (autosave / debounced save, B03).

        Same payload as the close-time save, so a crash / kill / OS shutdown
        loses at most one autosave interval of tweaks instead of the whole
        session (geometry, last root, MRU, sort, caption flags…).

        **書き込みは常にワーカースレッド**。``load_state`` + ``save_state`` を
        GUI スレッドで無制限に実行すると、死んだ共有では「何も操作して
        いないのに 60 秒ごとに 52 秒フリーズ」する。

        *wait_s* は**呼び出し元が結果を待つ**上限。既定の 0 秒は「投げっぱ
        なし」= オートセーブ / デバウンス保存（戻り値を使わないので常に
        ``False``）。設定ダイアログだけが成否表示の契約のために待ち、戻り値で
        「適用しました」と常駐警告を出し分ける。

        前回の保存がまだ返っていないときの扱いは *wait_s* で分かれる
        （再指摘 M-1）:

        * ``wait_s == 0``（オートセーブ / デバウンス）は**見送る**。保存先が
          応答していない証拠なので、積み増してもスタックしたワーカーと
          ハンドルが増えるだけになる（次の tick で再挑戦する）。
        * ``wait_s > 0``（設定ダイアログ）は**予算の残りだけ完了を待ってから**
          判断する。オートセーブの QTimer はモーダルダイアログ中も発火するので、
          即 ``False`` を返すと**健全なディスクでも** OK 押下が前回保存と 1 tick
          衝突しただけで「保存できませんでした」の**消えない**警告トースト
          （``_notify_persist_failed`` は ``duration_ms=0`` のセッション 1 回）
          になってしまう。

        待ちと保存は **1 本の予算**（``Deadline(wait_s)``）を共有する — 別々に
        配ると最悪 2 倍待つことになり、「閉じない」と同型の積算タイムアウトへ
        戻る。
        """
        deadline = Deadline(wait_s)
        idle = self._state_save_idle
        if not idle.is_set() and not idle.wait(deadline.remaining):
            logger.debug("state save skipped: the previous save has not returned")
            return False
        # ウィジェット読み取りは GUI スレッド必須。ディスクには触らない。
        self._collect_state()
        # ワーカーへ渡すのは複製 — 保存中に GUI スレッドが self._state を
        # 触っても、書き出す JSON が中途半端な混ざり方をしない。
        snapshot = self._state.model_copy(deep=True)
        generation = self._next_state_generation()
        landed: list[bool] = []
        idle.clear()

        def _save() -> None:
            try:
                landed.append(self._save_merged_state(snapshot, generation))
            finally:
                idle.set()

        run_tasks_before_deadline(
            [("viewer_state.json", _save)],
            deadline,
            thread_name="state-save",
        )
        return bool(landed) and landed[0]

    def _schedule_state_save(self) -> None:
        """Debounced state save — coalesces bursts (e.g. MRU updates)."""
        self._state_save_debounce.trigger()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # プラグインへ先に停止を伝える（deactivate + UI 回収はベストエフォート
        # — 例外は host 側で隔離されるので本体の teardown は必ず続行する）。
        # getattr ガード: __init__ を通さないテストハーネス対策（set_root 同様）。
        # provider コールバックを deactivate_all より先に外す — プラグインの
        # deactivate が unregister_provider を呼ぶと登録解除通知が発火するが、
        # 閉じかけのウィンドウでインデックス再読込を走らせる意味はない
        # （detail window の show_path 等が無駄な I/O をする）。
        ai_pack.remove_provider_callback(self._on_ai_provider_changed)
        # アプリ全体の focusChanged から外す（閉じた窓の席を覚え続けない）。
        # 2 度目の close（テストの finally 等）で外し直すと PySide が
        # SystemError を投げるので、外したことをフラグで 1 回に限る。
        if getattr(self, "_focus_hook_connected", False):
            self._focus_hook_connected = False
            try:
                QApplication.instance().focusChanged.disconnect(
                    self._on_app_focus_changed
                )
            except (RuntimeError, TypeError, SystemError):
                pass  # 既に外れている
        plugin_host = getattr(self, "_plugin_host", None)
        if plugin_host is not None:
            plugin_host.deactivate_all()
        self._autosave_timer.stop()
        self._state_save_debounce.stop()
        # 窓の下の :class:`GuardedStream` を**1 つ残らず**降ろす:
        # リネーム追従のウォーク（最大 20,000 フォルダの NAS
        # ``os.scandir``）も代表画像の BFS も、止めないと閉じた後まで走り続け、
        # 前者はその間 ``resolve_moved_entries`` が close 済みストアへ書きに
        # 行き得る。有界ドレイン（``_drain_loader_pools``）が吸収できるのは
        # 「待ち」だけで、キャンセルされていない走査は予算を使い切るまで走る
        # ので、cancel はドレインより**前**に置く。列挙は ``findChildren`` な
        # ので、新しいストリームを足しても片側欠落にならない。
        # 閉じる窓で母集合（_user_meta_map）を読み直す意味は無いので、降りた
        # リネーム追従の部分結果は引き取らずに捨てる — 下の一括 cancel は直結
        # 接続なので、捨てずに残すと ``load_all()`` の同期 sqlite 読みが予算
        # 付き teardown の**外側**で GUI スレッドを塞ぐ経路になる。
        self._rename_follow_pending = False
        for stream in self.findChildren(GuardedStream):
            stream.cancel()
        # post.md メタの再読込予約も破棄 — close 後に off-thread read を
        # 蒔く意味はない（着地しても bridge の teardown ガードで握られるが、
        # 無駄な I/O を出さない）。
        self._info_meta_retry_timer.stop()
        # フリーズ監視も止める: 閉じた窓に監視すべき GUI は無い。止めないと
        # 200 ms の PreciseTimer が隠れた窓の寿命いっぱい回り続け、閉じた窓を
        # 破棄しないホスト（テストワーカー）では窓の数だけ同じ [diag] 警告が
        # 複製される（CI 実測: 1 回の停止が 25 行）。
        monitor = getattr(self, "_main_thread_monitor", None)
        if monitor is not None:
            monitor._timer.stop()
        # tags.db 監視も止める: 閉じかけのウィンドウで再読込
        # （sqlite の開き直し + detail window の show_path）を走らせる意味は
        # ない。in-flight の probe は上の一括 ``cancel`` が既に無効化している
        # （ワーカー自体は止められないが、着地は ``bind`` の選別で捨てられる）。
        # getattr: AI パック無効時は watcher ごと作られない。
        tags_timer = getattr(self, "_tags_reload_timer", None)
        if tags_timer is not None:
            tags_timer.stop()
        # 「自走タイマーを止める」は**再武装の経路も断つ**こと:
        # このあと closeEvent 後半は data/ へ書く（viewer_state.json / WAL
        # チェックポイント）ので、watcher を生かしたままだと自分の書き込みで
        # ``directoryChanged`` が飛び、止めたばかりのデバウンスが再び回り出す。
        # ``removePath`` ではなく切断を採るのは、``_rearm_tags_db_watch`` が
        # 動的に足したパスぶんも一度に切れるから。
        watcher = getattr(self, "_tags_watcher", None)
        if watcher is not None and getattr(self, "_tags_watch_connected", False):
            self._tags_watch_connected = False
            for signal in (watcher.fileChanged, watcher.directoryChanged):
                try:
                    signal.disconnect(self._on_tags_db_changed)
                except (RuntimeError, TypeError, SystemError):  # pragma: no cover
                    pass
        # ---- 予算付き teardown ---------------------------------------------
        # ここから下の永続化（state 書き込み + 全 sqlite ストアの close）は
        # **フェーズごとに独立した絶対期限**を持つ。到達不能な SMB 共有では
        # 1 回の同期 I/O が数十〜数百秒ブロックし、GUI スレッドで待つ形では
        # 「× を押しても 200 秒経っても閉じない・共有を復旧しても回復しない」
        # （VM 実測 2/2）になる。「閉じる」はユーザーが決めた後の
        # 後始末なので、死んだストレージのために無限に待つ理由が無い。
        #
        # ``path_probe`` を「data/ に触れる前のゲート」へ引き上げる案は採らな
        # かった: プローブは「今この瞬間到達可能か」という**別の真実**を増や
        # すだけで、書き込みが始まる頃には陳腐化しうる（健全に見えて書き込み
        # 中に落ちる共有は救えない）。予算は実際の操作そのものを縛るので、
        # 状態も増えず取りこぼしも無い。
        #
        # **予算はフェーズごとに切り直す**。1 本の ``Deadline`` を両フェーズで
        # 共有すると、**その間に予算を持たないドレイン群（`_zip_drill` 5s +
        # `_cache_ctrl` 5s + ローダープール 2s = 最大 12 秒）が挟まる**ので、
        # 「健全な保存先でキャッシュビルド中に閉じた」だけで第 2 フェーズが
        # 常に ``join(0)`` になり、user_meta.db の WAL チェックポイントが毎回
        # 落ちる（死んだ共有だけの degradation のつもりが、正常系の既定挙動に
        # なる）。
        # 予算はあくまで「1 回の同期 I/O が無限に伸びる」ことへの上限なので、
        # ドレインを挟んだ後の別フェーズには新しい上限を配る。
        #
        # 積算の心配（元の症状に戻らないか）: GUI スレッドが待つ上限は
        # フェーズ 1（3 秒）＋ ドレイン（最大 12 秒・従来から自前タイムアウト
        # 持ち）＋ フェーズ 2（3 秒）= 最悪 18 秒で、共有する形の 15 秒
        # より 3 秒長い。有界であることは変わらず、引き換えに「終了のたびに
        # WAL チェックポイントを落とす」正常系の劣化が消える。
        state_save_budget = Deadline(_CLOSE_PERSIST_BUDGET_S)
        # 収集はウィジェット読み取りなので GUI スレッド必須。ディスク突き
        # 合わせ（load_state）と書き込みはワーカー側の保存タスクが持つ。
        #
        # 稼働中のオートセーブ（``_persist_state_snapshot``）が放棄されたまま
        # 残っていても、ここは在庫フラグ（``_state_save_idle``）を見ずに保存
        # する — 終了時の状態が最も新しく、詰まった前回を待って予算を溶かす
        # 意味が無いため。**だから `_persist_state_snapshot` は通さない**
        # （在庫待ちと自前 ``Deadline`` の両方を迂回する必要がある）。共通化
        # されているのは「収集 = ``_collect_state`` / 突き合わせ + 書き込み =
        # ``_save_merged_state``」の 2 本。
        #
        # 両者が同時に飛んでも**内容**が壊れない:
        # スナップショットには世代番号が付き、``_save_merged_state`` が
        # ``os.replace`` の直前（``state.py::save_state`` の ``should_land``）
        # まで世代を再確認するので、後から着地しようとした古いオートセーブは
        # そこで捨てられる。**I/O は直列化しない** — 書き込みロックが守るのは
        # ``_state_written_gen`` の read-modify-write だけで、詰まった
        # オートセーブがこの保存を待たせることは無い（ロックで I/O ごと
        # 囲うと、3 秒の予算をロック待ちで溶かして保存を試みることすら
        # できなくなる）。``_write_json_atomic`` の原子性はファイル整合性しか
        # 守らないので、順序はこの世代確認だけが守る。
        self._collect_state()
        close_snapshot = self._state.model_copy(deep=True)
        # 状態を取った後は先に隠す（無応答の共有で予算いっぱい待つ間も「閉じた」と見える）。
        self.hide()
        close_generation = self._next_state_generation()

        def _save_close_state() -> None:
            self._save_merged_state(close_snapshot, close_generation)

        unfinished = run_tasks_before_deadline(
            [
                ("viewer_state.json", _save_close_state),
                # 投げっぱなしで出した ``shared_prefs.json`` の書き込み
                # （テーマ / ライブラリ登録）を
                # 同じ予算の**末尾**で拾う。閉じる直前のテーマ切替が着地
                # しないまま窓が消えるのを普通のディスクでは防ぎ、死んだ
                # 共有では予算超過として諦める（ワーカーは daemon なので
                # プロセス終了は止めない）。順序は「再生成不能な
                # viewer_state.json が先」。
                (
                    "shared_prefs.json",
                    lambda: self._shared_prefs_flush.wait_idle(
                        state_save_budget.remaining
                    ),
                ),
            ],
            state_save_budget,
            thread_name="close-state-save",
        )
        # 予算内に終わらなかったラベルの警告は再生成不能なものだけ
        # （``shared_prefs.json`` は落としても次回起動時に伝播しないだけ）。
        unfinished = [x for x in unfinished if x != "shared_prefs.json"]
        if unfinished:
            # 再生成不能: 失敗を黙らせない。この警告自体が GUI スレッドを
            # 塞がないことは ``common/logging.py`` の ``_BoundedSink`` が
            # 保証する（保存先が死んでいれば黙って捨てられる — 待たない）。
            logger.warning(
                "viewer_state.json の保存を打ち切りました（{} 秒の予算超過 —"
                " 保存先が応答していない可能性があります）。この終了ぶんの"
                " ウィンドウ状態は保存されません。", _CLOSE_PERSIST_BUDGET_S,
            )
        # J02: close the modeless 健全性チェック dialog before tearing down —
        # its ``done()`` cancels the in-flight off-thread scan (and any bulk
        # delete) so no worker keeps walking the NAS tree past this window.
        if self._health_dialog is not None:
            self._health_dialog.close()
        # Close the lightbox first (it must never outlive this window), with
        # signals blocked — selection-sync into a tearing-down pane tree is
        # pointless and could fire scans mid-teardown.
        if self._lightbox is not None:
            self._lightbox.blockSignals(True)
            self._lightbox.close()
        # 残りのモードレス子窓（詳細情報 / 計測結果 / 操作ガイド / tagger 導入
        # ガイド）も本窓と一緒に畳む。Qt は親の hide を子**ウィンドウ**へ伝播
        # しないので、閉じないと親の無いトップレベル窓が生きている限り本窓が
        # 消えたあとも画面に残り、計測結果ダイアログは 500ms ポーリングを回し
        # 続ける（自分の hideEvent でしか止まらない）。個別に名指しすると
        # 6 つ目の子窓で同じ片側欠落が起きるので、上の 2 つと同じ「閉じる」を
        # ウィジェット木から導出する（``close()`` は QDialog でも closeEvent を
        # 通るので、各窓の後始末はそのまま効く）。
        for child in self.findChildren(
            QWidget, options=Qt.FindDirectChildrenOnly,
        ):
            if child.isWindow() and child.isVisible():
                child.close()
        # Stop both panes' background scanners before tearing down widgets so
        # no scanner callback fires into a partially-destroyed widget tree —
        # and so QThreadPool teardown doesn't block on an in-flight NAS scan.
        #
        # ``shutdown()`` はスキャナ（ChildrenScanner / RecursiveSearchScanner）
        # を**協調キャンセル**で止める。かつては「スキャナはドレイン待ちの
        # 対象外」という裁定だったが、``_drain_loader_pools`` が種類の列挙を
        # やめて窓の ``findChildren(QThreadPool)`` から導出するようになった
        # ので、スキャナ自身のプールも**同じ 1 本の予算**で掃かれる（健全系の
        # 実測コストは 0ms — キャンセル済みの仕事は即座に降りる）。ここで
        # 先にキャンセルしておくのが要点で、汎用パスが吸収できるのは「待ち」
        # だけ: キャンセルされていない仕事は予算いっぱい待つことになる。
        self._post_grid.shutdown()
        self._file_list.shutdown()
        # Stop media playback before tearing down widgets to avoid native
        # crashes from QMediaPlayer accessing a destroyed QVideoWidget.
        self._content.clear_media_playback()
        # Stop an in-flight ZIP extraction BEFORE the sweep below: a worker
        # still writing members would race the rmtree and re-create the
        # directory it was handed, leaking a ``snappix-viewer-zip-*`` tree
        # into ``data/tmp``.  Bounded drain, same
        # contract as the cache builder / loader pools further down.
        self._zip_drill.shutdown(5000)
        # Remove every temp directory created by ZIP extraction.  Best-
        # effort: an external app the user launched from the viewer may
        # still hold a file open, in which case rmtree leaves the lock
        # behind for the OS to reclaim later.
        #
        # **実行はここではなく下の予算付きフェーズの末尾**（予算付き
        # teardown の契約）: 削除対象は ``data/tmp`` 配下 = 死んだ共有では
        # 1 回の同期 I/O が数十〜数百秒ブロックするボリュームで、木のファイル
        # 数ぶんの ``unlink`` を GUI スレッドで直列に撃つと「× を押しても
        # 閉じない」が予算の外側で復活する（``ignore_errors=True`` は例外を
        # 握るだけでブロックは止めない）。諦めても残るのは ``data/tmp`` 配下の
        # 残骸だけで、``_zip_temp_base`` の docstring が既に許容している。
        zip_temp_dirs = list(self._zip_temp_dirs)
        self._zip_temp_dirs.clear()

        def _sweep_zip_temp() -> None:
            for temp_dir in zip_temp_dirs:
                shutil.rmtree(temp_dir, ignore_errors=True)
        # Tear down a background cache build BEFORE closing the caches it
        # writes to, or a still-running decode worker would call
        # ``disk_cache.store`` after ``close`` (details on the cancel /
        # bounded-wait contract in CacheBuildController.shutdown).
        self._cache_ctrl.shutdown(5000)
        # Drain both thumbnail loaders' decode pools before closing the disk
        # cache they write to.  An in-flight decode worker calls
        # ``disk_cache.store`` when it finishes; if the cache is already closed
        # the blob (WebP) still hits disk but the sqlite INSERT throws on the
        # dead connection (swallowed), leaving orphan blobs — and the pool's
        # destructor would otherwise block teardown on a NAS decode.  Clearing
        # the queue + a bounded ``waitForDone`` mirrors the cache builder's
        # ``wait_for_pools`` so no writer survives past the close below.
        # フォルダプレビューのキャンセル（タイマー停止 + トークン回転）は
        # 汎用ドレインより**前**に出す — 汎用パスが吸収できるのは待ちだけで、
        # キャンセルしていない仕事は予算いっぱい待つことになる（フォルダ
        # プレビュー自身の有界待ちはその汎用パスに吸収される）。
        self._content.shutdown_folder_preview(2000)
        self._drain_loader_pools(2000)
        # リネーム追従の専用プールを含む窓配下の全ワーカープールは
        # ``_drain_loader_pools`` の ``findChildren(QThreadPool)`` が 1 本の予算で
        # 掃く — ここに個別の待ちを並べ直さないこと（新しい葉が増えるたびに
        # 同じ片側欠落が起きる）。走行中のウォークを実際に止めるための
        # ``token.cancel()`` は closeEvent の先頭で済んでいる。
        # Flush + close the persistent stores (best-effort, 予算付き).
        #
        # 順序が「再生成不能 → 再生成可能」なのはフェーズ内の優先順位:
        # 1 本のワーカーが宣言順に閉じるので、諦められるのは**後ろ側 =
        # 作り直せるキャッシュ**になる。user_meta.db（唯一の再生成不能
        # データ）だけは最初に閉じる。
        # 予算は第 1 フェーズと**別勘定** — 間に挟まる無予算の
        # ドレイン（最大 12 秒）に食い潰されると、健全な保存先でも毎回
        # join(0) になって WAL チェックポイントが落ちるため。
        # 書き手のドレイン（_cache_ctrl.shutdown / _drain_loader_pools）は
        # この呼び出しより前に完了している必要がある（scanning.md の順序
        # 契約）— close をワーカーへ逃がしてもその前提は変えていない。
        # 閉じる対象は**ストア単位**（= sqlite ファイル単位）。3 つの
        # path キーキャッシュは ``viewer_cache.db`` に同居するので 1 本で閉じ、
        # その ``close`` が同居キャッシュのフラッシュも順に行う。
        stores: list[tuple[str, object]] = [
            ("user_meta.db", self._user_meta),
            ("viewer_cache.db", self._cache_store),
            ("search_index", self._search_index),
            ("tags.db (tag index)", self._tag_index),
            ("tags.db (vector index)", self._vector_index),
        ]

        def _closer(store) -> Callable[[], None]:
            def _close() -> None:
                try:
                    store.close()
                except Exception as exc:  # pragma: no cover (defensive)
                    logger.warning("cache close failed: {}", exc)
            return _close

        unfinished = run_tasks_before_deadline(
            [(label, _closer(store)) for label, store in stores
             if store is not None]
            # 順序契約の末尾（= 諦めてよい側）へ ZIP 展開先の掃引を置く。
            + [("zip temp dirs", _sweep_zip_temp)],
            Deadline(_CLOSE_PERSIST_BUDGET_S),
            thread_name="close-stores",
        )
        if unfinished:
            # キャッシュは再生成可能なので黙って諦めてよい。落として
            # 困るのは user_meta.db だけなので、そこだけ警告を立てる（残りは
            # 「何が閉じ切らなかったか」を後追いできる debug に留める）。
            if "user_meta.db" in unfinished:
                logger.warning(
                    "user_meta.db を閉じ切れませんでした（{} 秒の予算超過 —"
                    " 保存先が応答していない可能性があります）。コミット済みの"
                    " ユーザーデータは WAL に残り次回起動時に回収されます。",
                    _CLOSE_PERSIST_BUDGET_S,
                )
            logger.debug("close abandoned after budget: {}", unfinished)
        # 「closeEvent は自走タイマーを 1 つ残らず止める」を**木からの導出**で
        # 満たす: 上の個別 stop は窓が直接持つタイマーしか知らず、葉ウィジェット
        # （右一覧のスピナー・グリッドの遅延リレイアウト…）が自分で足した
        # タイマーは列挙の外に居た。窓配下のタイマーは全て ``QTimer(<親>)`` =
        # 窓の子孫なので、ここで一括 stop すれば新しい葉も自動で載る。
        # **位置は closeEvent の末尾**であること（tags.db watcher の再武装の罠と同型）:
        # 前半のドレイン / ストア close の中で再武装されるタイマー（ヒント表示
        # 等）があるため、途中に置くと止め切れない。
        for timer in self.findChildren(QTimer):
            timer.stop()
        # 予算超過を検知したときだけログ出口を有界化する（旧
        # ``limit_exit_flush``）という後始末は**持たない**。
        # ログ機構そのものが有界になったため: ``common/logging.py`` の
        # ``_BoundedSink`` は呼び出しスレッドを絶対に塞がず、loguru の
        # ``atexit.register(logger.remove)`` が呼ぶ ``stop()`` も 1 秒で
        # 打ち切る。「超過を検知したときだけ」という条件付きの有界化は、
        # そもそも**その検知の警告を吐く時点で既に塞がっている**（GUI
        # スレッドは 9 レコード目で永久ブロックする — 実測）ので成立して
        # いなかった。ここに条件付きの後始末を戻さないこと。
        self._teardown_done = True
        super().closeEvent(event)
