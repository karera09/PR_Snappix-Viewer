"""閲覧モード — 没入型フルスクリーンライトボックスの窓本体.

メインウィンドウの 3 ペインから独立したトップレベルウィンドウで、選択中の
画像を全画面表示し、←/→・ホイールで投稿内の兄弟メディアを、末尾/先頭では
「もう一度押すと隣の投稿へ」の確認ラッチを挟んで**投稿横断**で連続閲覧できる。
投稿横断は**深さ優先探索（pre-order DFS）**で、次の画像が同じディレクトリ階層
に無くても ``current`` のサブフォルダ（より深い階層）や親経由の別階層へ連続
して降りていく。Space でスライドショー（投稿横断は自動継続、最後の投稿で停止）。

**部品は :mod:`~snappix.viewer.lightbox_parts` にある**（このモジュールに残る
のは窓 :class:`LightboxWindow` だけ）:

| 置き場 | 中身 |
|---|---|
| ``lightbox_parts/scan.py`` | プレイリスト列挙 / 投稿横断 DFS（Qt 非依存の純関数） |
| ``lightbox_parts/auto_hide.py`` | :class:`EdgeLatch` / :class:`AutoHideEngine`（純ロジック） |
| ``lightbox_parts/overlays.py`` | 固定オーバーレイ色とクローム部品（カウンタ / ヒント / 中央メッセージ / 上部バー / 操作カプセル / 空プレイリスト） |
| ``lightbox_parts/filmstrip.py`` | 下端のサムネイル帯 :class:`FilmstripView` |

このモジュールは分割前の公開名を全て re-export するので、外から見た import 面
（``from .lightbox import FilmstripView`` 等）は分割前と変わらない。

設計判断（非明示の契約を含む）:

* **プレイリストは画像 + 動画**（``lightbox_parts.scan.PLAYLIST_SUFFIXES``）。
  静止画とアニメ GIF/WebP は内部 :class:`ImageView`（既存の ``QMovie`` 再生）、
  動画は遅延生成した ``MediaView`` が再生する。PDF・音声はナビゲーション /
  スライドショーの対象から除外する。メディアを持たない投稿・中間ディレクトリ
  は DFS 投稿横断時に自動でスキップされる。
* **フィルムストリップ（下端の画像リスト）は上部バーと独立してオートハイド**
  する。上部バーはマウス操作で出るが、ストリップは**画像を移動したときだけ**
  一瞬出て自動消灯し（中央での拡大・パン中は出さない）、下端の「召喚帯」に
  カーソルを入れたときのみ手動で呼び出せる（サムネのクリック用）。上部バーは
  :class:`AutoHideEngine`、ストリップは専用の単発タイマーで制御する。待ち時間は
  両者共通で ``ViewerState.lightbox_chrome_hide_ms``（設定で調節可）。
* **中央プレビューの ImageView は再親化しない**。ライトボックスは自前の
  :class:`ImageView` インスタンスを持ち、3 段階表示・LANCZOS・プリフェッチ・
  ``BoundedImageCache``・ズーム率ピル・ミニマップ・ダブルクリックの
  フィット⇔実寸トグルをそのまま使う（メインウィンドウの表示状態と
  キャッシュ予算に一切触れない）。
* **背景は最暗トークン** ``DARK_TOKENS.bg_surface``。ライトボックスは常時
  ダークな画像鑑賞サーフェスなので、ライトテーマ中でもダークトークンを使う。
  上部バー・フィルムストリップ・タイトルオーバーレイは「画像上のオーバーレイ
  は固定色可」（design.md の例外）に従い固定の暗スクリム + 明るい文字で描く
  （色の定義は ``lightbox_parts/overlays.py`` に集約）。
* **NAS 規約**: 画像デコードは ImageView の既存非同期経路のみ。隣接投稿の
  画像列挙（``os.scandir``）も GUI スレッドでは行わず、``_runnable.GuardedStream``
  （**この窓が所有する単一スレッドプール** + トークンガード）で off-thread
  実行にする — 冷えた NAS の DFS はスレッドを分単位で握るので、グローバル
  プールの短命な probe と同居させない。フィルムストリップのサムネは
  注入されたプロバイダ（右ペインの常駐 pixmap）を優先し、無いものだけ
  ``ThumbnailLoader``（ワーカーは ``QImage`` のみ、``QPixmap`` 変換は
  メインスレッド）へ可視範囲限定で要求する。
* **キー処理は ``eventFilter``**（子ウィジェット全体に install）。内部の
  QScrollArea が矢印キーを消費するため、``QShortcut`` ではなくフィルタで
  Esc / F11 / ←→ / Home / End / Space を先取りする（ショートカット一覧
  ダイアログではドキュメント行として扱う）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QImage,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import DARK_TOKENS, overlay, overlay_chrome
from ..common.ui.timers import DebounceMode, Debouncer
from ._runnable import GuardedStream
from .context_menus import CurationHooks
from .edge_nav import WheelNavGate
from .folder_scan import IMAGE_SUFFIXES
from .pending import Pending
from .image_view import (
    ImageView,
    apply_state as apply_image_view_state,
    connect_state_writeback as connect_image_view_writeback,
)
from .lightbox_parts.auto_hide import (
    AutoHideEngine,
    EdgeLatch,
    _AUTO_HIDE_MIN_MS,
    _AUTO_HIDE_MS,
    _STRIP_REVEAL_MARGIN,
)
from .lightbox_parts.filmstrip import FilmstripView
from .lightbox_parts.overlays import (
    CenterMessageOverlay,
    EmptyPlaylistView,
    LightboxControlCapsule,
    LightboxTopBar,
    _CounterOverlay,
    _HintOverlay,
)
from .lightbox_parts.scan import (
    PLAYLIST_SUFFIXES,
    is_video_path,
    list_images_sorted,
    list_playlist_sorted,
    scan_adjacent_image_folder,
    scan_adjacent_post,
)
from .thumbnail_loader import ThumbnailLoader

if TYPE_CHECKING:
    from .state import ViewerState

# スライドショーの動画ウォッチドッグの余裕: 再生中の動画は「残り尺 + この
# 余裕」で間隔タイマーを張り直す。正常なら playback_finished が先に来て
# 前倒しで進み、来ない未知の沈黙経路でもこのタイマーが有限時間で次へ送る。
_SLIDESHOW_VIDEO_SLACK_MS = 3000


class MediaResume(NamedTuple):
    """動画の再生位置の引き継ぎ 1 件（中央プレビュー ⇄ 全画面の往復。N-142）.

    ``position_ms`` の ``0`` は番兵ではなく「先頭へ戻す」という正当な位置で、
    「引き継ぎ無し」は**値そのものが無いこと**（``Pending`` が armed でない /
    :meth:`LightboxWindow.media_playback_position` が ``None`` を返す）で表す。
    以前は ``path | None`` + ``ms`` の 2 フィールドで、``0`` が「先頭」と
    「引き継ぎ無し」を兼ねていた。
    """

    path: Path
    position_ms: int


class LightboxWindow(QWidget):
    """閲覧モードのトップレベルウィンドウ（ボーダレス全画面）.

    メインウィンドウに ``Qt.Window`` フラグ付きで親付けされ（親より長生き
    しない）、``open_at()`` が呼び出し元ウィンドウのスクリーンで
    ``showFullScreen()`` する。ナビゲーション・スライドショー・オートハイドの
    決定ロジックは :mod:`~snappix.viewer.lightbox_parts` の純ロジック
    （:class:`EdgeLatch` / :class:`AutoHideEngine` / :func:`scan_adjacent_post`）
    に委譲する。
    """

    #: 閉じたとき (folder: Path | None, image: Path | None) — メインウィンドウが
    #: 最後に見ていた投稿/ファイルへ選択を追従させる。
    closed = Signal(object, object)
    #: 投稿横断遷移が確定したとき (folder, 最初に表示した画像)。
    post_changed = Signal(Path, Path)
    #: 数字キー 0–5 が押されたとき (star, 対象の現在ファイル)。ホストが
    #: user_meta へ書き込む。0 = 解除。
    star_key_requested = Signal(int, Path)
    #: 全画面の印の要求 (path, kind, value) — ``L`` キーと画像の右クリック。
    #: ホストが左ペインの単一書き手（``PostGrid.request_curation``、
    #: ``notify=False``）へ渡す。確認はこちらの中央オーバーレイで出す。
    curation_requested = Signal(Path, str, object)
    #: 遅延生成した MediaView のループボタンをユーザーが切り替えたとき。ホストが
    #: ``ViewerState.media_loop`` へ永続化する（中央ペインの
    #: ``ContentView.media_loop_toggled`` と揃える — item 8）。
    media_loop_toggled = Signal(bool)
    #: 遅延生成した MediaView の音量スライダをユーザーが動かしたとき (0–100)。
    #: ホストが ``ViewerState.media_volume`` へ永続化する（同上）。
    media_volume_changed = Signal(int)
    #: 再生速度 (N-136) — loop / volume と同じ再送出。
    media_playback_rate_changed = Signal(float)
    #: 内部 ImageView の右クリックメニューで「ズーム維持」を切り替えたとき。
    #: ホストが ``ViewerState.image_zoom_persist`` へ永続化する
    #: （中央ペインの ``ContentView.image_zoom_persist_toggled`` と揃える —
    #: UIレビュー 2026-08-28 N-78。メニューは両インスタンス共用なので全画面でも
    #: 押せるのに、配線が中央ペインにしか無く黙って捨てられていた）。
    image_zoom_persist_toggled = Signal(bool)
    #: 同じく「ミニマップ表示」トグル。``ViewerState.image_minimap_enabled`` へ。
    image_minimap_toggled = Signal(bool)

    def __init__(
        self,
        loader: ThumbnailLoader | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(t("viewer.common.lightbox_mode"))
        self._loader = loader
        self._strip_prefix = f"lightbox:{id(self)}:"
        self._images: list[Path] = []
        self._index = -1
        self._folder: Path | None = None
        self._post_provider: Callable[[], list[Path]] | None = None
        self._root_provider: Callable[[], Path | None] | None = None
        self._thumb_provider: Callable[[Path], QPixmap | None] | None = None
        # 現在ファイルの star を返すプロバイダ（数字キー確認オーバーレイ用）。
        self._star_provider: Callable[[Path], int] | None = None
        # (star, later) プロバイダと、画像ビューの右クリックが祖先から読む印の口
        # （``curation_hooks_from_ancestors``）。``set_curation_provider`` が揃える。
        self._curation_provider: Callable[[Path], tuple[int, bool]] | None = None
        self._curation_hooks: CurationHooks | None = None
        # 上部バーの印ストリップがタグ件数を出すための読み手（同期の辞書引き）。
        self._user_tags_provider: Callable[[Path], tuple[str, ...]] | None = None
        self._latch = EdgeLatch()
        self._nav_gate = WheelNavGate()
        self._closed_emitted = False
        self._filters_installed = False
        # 明示プレイリスト（左ペインの検索/絞り込み結果由来 — G07）で開いたか。
        # True の間は投稿横断（DFS）を無効化し、フラットな結果列だけを流し見する。
        self._playlist_locked = False
        # 動画再生用の MediaView は初回動画表示時に遅延生成する（QtMultimedia の
        # モジュールレベル import 禁止 = media_view.py の遅延 import 契約を踏襲）。
        self._media: QWidget | None = None
        # 遅延生成前に届いたメディア設定（音量 / 自動再生 / ループ）を退避し、
        # MediaView 構築時に apply_settings で注入する（中央ペインと同じ遅延
        # 構築ハンドオフ — item 8）。
        self._pending_media_state: Pending[ViewerState] = Pending()
        # セッション初回のみ操作ヒントを出す（インスタンスは遅延生成 1 個を使い
        # 回すので、このフラグは「このセッションで初めて開いたか」を表す — G05）。
        self._hint_shown = False

        # 投稿横断スキャン（off-thread、トークンガード）— 深さ優先で root
        # 部分木を辿る。root 境界と root 直下の表示順は dispatch 時に snapshot
        # してワーカーへ渡すため、方向だけを結果コールバック用に保持する。
        # 走査は**専用の単一スレッドストリーム**で行う（項目#81 / #46）:
        # 深さ優先の投稿横断もプレイリスト列挙も、コールドな NAS では 1 本の
        # スレッドを分単位で握る。グローバルプールへ載せると、アプリ全体が
        # 共有する短命な stat / decode probe を飢えさせる（post_grid の
        # ``_recent_pool`` が同じ判断をコメント付きで持つ。GuardedStream は
        # プール + トークン + 完了シグナルを 1 つにまとめた共通形で、
        # ``ViewerWindow._drain_loader_pools`` の findChildren 列挙にも自動で
        # 乗る = 閉じるときの有界ドレインが片側欠落しない（項目#54））。
        self._cross_inflight = False
        self._cross_direction = 0
        self._cross_stream = GuardedStream(self)
        self._cross_stream.bind(self._on_cross_scanned)
        # 兄弟リスト未供給時の初期フォルダスキャン（off-thread）
        self._open_stream = GuardedStream(self)
        self._open_stream.bind(self._on_open_scanned)
        # open_folder が渡した 1 回きりの予告文（N-21）。着地時に表示して消す。
        self._open_notice = ""
        # メディアビュー構築失敗のスタックトレースは 1 セッション 1 回だけ出す
        # （環境要因なので毎回同じ結果になる — ContentView._lazy_view_unavailable
        # と同じ抑制。項目#76）。
        self._media_unavailable_logged = False

        # --- 背景 = 最暗トークン --------------------------------------
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(DARK_TOKENS.bg_surface))
        self.setPalette(pal)

        # --- 内部 ImageView（新インスタンス — 中央ペインは再親化しない）
        self._view = ImageView(self)
        self._view.setFrameShape(QFrame.NoFrame)
        # F02 のオンスクリーンコントロールバーは抑止する（C7）— ライトボックスは
        # 自前クローム（前後・カウンタ・フィルムストリップ）を持つため、内部
        # ImageView のホバーバーが没入表示に二重に重なるのを防ぐ。
        self._view.set_control_bar_enabled(False)
        # 右クリックにこの態からの**出口**を出す (N-142)。Esc / F11 / 上部バーの
        # 閉じるしか出口が無く、そのどれも右クリックからは見えなかった。
        self._view.set_fullscreen_exit_mode(True)
        self._view.fullscreen_requested.connect(self.close)
        vpal = self._view.palette()
        vpal.setColor(QPalette.ColorRole.Window, QColor(DARK_TOKENS.bg_surface))
        vpal.setColor(QPalette.ColorRole.Base, QColor(DARK_TOKENS.bg_surface))
        self._view.setPalette(vpal)
        self._view.viewport().setAutoFillBackground(True)
        self._view.navigate_requested.connect(self._on_view_navigate)
        # プリフェッチは ImageView の既存機構を流用 — 兄弟リストは自前保持。
        self._view.set_siblings_provider(self._sibling_provider)
        # 静止画（ImageView）と動画（遅延生成 MediaView）を切り替えるスタック。
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)  # page 0 = 静止画
        self._media_placeholder = QWidget(self._stack)
        self._media_placeholder.setAutoFillBackground(False)
        self._stack.addWidget(self._media_placeholder)  # page 1 = 動画（遅延差替）
        # page 2 = 空プレイリストの常設カード（N-86）。1.5 秒で消えるタイトル
        # オーバーレイと違い、出したままにする面。
        self._empty_view = EmptyPlaylistView(self._stack)
        self._empty_view.close_requested.connect(self.close)
        self._stack.addWidget(self._empty_view)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._stack)

        # --- クローム（オートハイド対象） ------------------------------
        self._top_bar = LightboxTopBar(self)
        self._top_bar.strip.curation_requested.connect(self._request_curation)
        self._top_bar.slideshow_toggled.connect(self.toggle_slideshow)
        self._top_bar.close_requested.connect(self.close)
        self._strip = FilmstripView(self)
        self._strip.clicked.connect(self._on_strip_cell_clicked)
        self._strip.request_thumb.connect(self._on_strip_thumb_requested)
        if self._loader is not None:
            self._loader.loaded.connect(self._on_strip_thumb_loaded)
            # 失敗も必ず受ける（項目#128）: 未接続だと mark_failed が呼ばれず、
            # 一度 request したパスは set_images（＝投稿の移動）まで再要求
            # されないので、破損 JPEG / 0 バイトファイルのセルが「読み込み中」
            # に見える平坦なプレースホルダのまま残る。ステージ帯
            # （main_window._on_stage_thumb_failed）と同じ劣化に揃える。
            self._loader.failed.connect(self._on_strip_thumb_failed)
        self._title_overlay = CenterMessageOverlay(self)
        # 常時表示の位置カウンタ（G02）とセッション初回ヒント（G05）。
        self._counter = _CounterOverlay(self)
        self._hint = _HintOverlay(self)
        # 常設オンスクリーン操作カプセル（前後送り・フィット⇄実寸・ズーム表示
        # — UIレビュー #25）。上部バーと同じオートハイドに連動する。
        self._capsule = LightboxControlCapsule(self)
        self._capsule.prev_clicked.connect(lambda: self._nav_key(-1))
        self._capsule.next_clicked.connect(lambda: self._nav_key(1))
        self._capsule.fit_toggle_clicked.connect(self._on_capsule_fit_toggle)
        # 内部 ImageView のあらゆるズーム変化（Ctrl+ホイール / ± キー /
        # ダブルクリック / 非同期デコードのフィット確定）に%表示を追従させる。
        self._view.zoom_changed.connect(self._on_view_zoom_changed)
        # 右クリックメニューのビュー設定トグル（ズーム維持 / ミニマップ）を
        # ホストへ返す。中央ペインと同じ image_view 側の集約点を通す（N-78）。
        connect_image_view_writeback(
            self._view,
            zoom_persist=self.image_zoom_persist_toggled.emit,
            minimap=self.image_minimap_toggled.emit,
        )

        # --- スライドショー -------------------------------------------
        self._slideshow_interval_ms = 5000
        # ホストが渡す「中央プレビューで一時停止した動画と、その位置」
        # (N-142)。一致するパスの初回表示で 1 度だけ消費する。
        self._resume_media: Pending[MediaResume] = Pending()
        self._slideshow_active = False
        self._slideshow_timer = QTimer(self)
        self._slideshow_timer.timeout.connect(self._on_slideshow_tick)

        # --- オートハイド（上部バー + 操作カプセル + カーソル） -----------
        # 実効の待ち時間。ホストが ``ViewerState.lightbox_chrome_hide_ms`` を
        # :meth:`set_chrome_hide_ms` で流し込む（設定ダイアログからライブ反映）。
        # タイマーは ``trigger(self._chrome_hide_ms)`` で待ち時間を明示して
        # 張る — ``_on_hide_timeout`` が残り時間で短く張り直すことがあるので、
        # 引数無しの ``trigger()``（直前の間隔を引き継ぐ）は使わない。
        self._chrome_hide_ms = _AUTO_HIDE_MS
        self._hide_engine = AutoHideEngine()
        self._hide_timer = Debouncer(
            self, _AUTO_HIDE_MS, self._on_hide_timeout, mode=DebounceMode.TRAILING
        )
        # 「クローム上にカーソルがある間は隠さない」ホバー保持を効かせる条件:
        # **最後の操作がポインタの移動 / クリックだったか**。キーやホイールで
        # 画像を送っている間はポインタは置いてあるだけなので、たまたま下端の
        # カプセル / 画像リストの上にあっても保持しない（保持すると、操作が
        # 止まっても下端の表示がいつまでも消えない）。
        self._pointer_live = False
        # 直前に見たポインタのグローバル座標。位置が変わらない MouseMove
        # （ウィジェットの表示 / 非表示やカーソル形状の切替に伴う合成イベント）
        # は操作ではないので活動に数えない — 数えると「隠す → 合成 Move →
        # 再表示」の往復で永久に消えなくなる。
        self._last_pointer_pos: QPoint | None = None

        # --- フィルムストリップ専用オートハイド ------------------------
        # 上部バーとは独立。画像移動（_show_index）と下端ホバーでのみ表示し、
        # このタイマーで自動消灯する（中央での拡大操作では出さない）。待ち時間
        # は上部バーと共通の ``_chrome_hide_ms``。
        self._strip_hide_timer = Debouncer(
            self, _AUTO_HIDE_MS, self._on_strip_hide_timeout, mode=DebounceMode.TRAILING
        )

    # ------------------------------------------------------------- wiring

    def set_thumbnail_provider(
        self, provider: Callable[[Path], QPixmap | None] | None,
    ) -> None:
        """右ペイン常駐サムネの注入（段0 プレースホルダ + ストリップ供給）."""
        self._thumb_provider = provider
        self._view.set_thumbnail_provider(provider)

    def set_post_provider(
        self, provider: Callable[[], list[Path]] | None,
    ) -> None:
        """投稿横断（DFS）の root 直下の並び順の注入.

        「左ペインのフォルダタイル一覧（表示順 = ソート／フィルタ反映）」を
        返すコールバック。深さ優先走査では root 直下の子の順序付けにのみ使い、
        より深い階層はファイルシステムの名前順で辿る（:func:`scan_adjacent_image_folder`）。
        """
        self._post_provider = provider

    def set_root_provider(
        self, provider: Callable[[], Path | None] | None,
    ) -> None:
        """投稿横断（DFS）の探索境界となる「左ペインの現在ルート」の注入.

        深さ優先走査はこの root 部分木の内側だけを辿る（root の外＝別ライブラリ
        へは出ない）。未注入または ``None`` を返すときは *post_provider* の
        先頭要素の親を暫定 root とする（root 直下の兄弟はすべて同じ親を持つ）。
        """
        self._root_provider = provider

    def set_star_provider(
        self, provider: Callable[[Path], int] | None,
    ) -> None:
        """現在ファイルの star を返すプロバイダの注入.

        常時表示カウンタの ★N 併記（UIレビュー 07-25 #46）と数字キー押下時の
        確認表示に使う。同期のインメモリ参照（左ペインの ``_user_meta_map``）で
        あることが前提 — 描画のたびに呼ぶので sqlite / NAS へは触らないこと。
        未注入なら ★ は出ない（素の閲覧はそのまま動く）。
        """
        self._star_provider = provider
        # 下端ストリップの兄弟セルにも★の有無マークを出す (N-72) —
        # カウンタは「現在の 1 枚」しか映さないので、どれを評価済みか
        # 一望する手段が無かった。値は描かず有無だけ。
        self._strip.set_curation_provider(
            None if provider is None
            else (lambda path: (provider(path), False))
        )
        self._sync_chrome()

    def set_curation_provider(
        self, provider: Callable[[Path], tuple[int, bool]] | None,
    ) -> None:
        """(star, later) プロバイダの注入 — ★カウンタ・ストリップ・右クリックの印の節。

        :meth:`set_star_provider` の上位互換: 同じ同期のインメモリ参照から
        ★も「あとで見る」も読めるようにし、画像ビューの右クリックに印の節を
        出す口（``_curation_hooks``）も同時に揃える（UIレビュー 2026-09-11
        N-16 / N-19 — 全画面だけ ``L`` が死に、右クリックに印が無かった）。
        """
        self._curation_provider = provider
        self._top_bar.strip.set_store_available(provider is not None)
        if provider is None:
            self._curation_hooks = None
            self.set_star_provider(None)
            return
        self._curation_hooks = CurationHooks(
            provider=provider, request=self._request_curation,
        )
        self.set_star_provider(lambda path, p=provider: int(p(path)[0] or 0))

    def set_user_tags_provider(
        self, provider: Callable[[Path], tuple[str, ...]] | None,
    ) -> None:
        """上部バーの印ストリップに出すユーザータグの読み手（同期・I/O なし）."""
        self._user_tags_provider = provider
        self._sync_chrome()

    def _sync_strip(self, path: Path | None) -> None:
        """上部バーの印ストリップへ現在画像の印を配る（``_update_top_bar`` から）."""
        strip = self._top_bar.strip
        provider = self._curation_provider
        if path is None or provider is None:
            strip.set_target(None)
            return
        try:
            star, later = provider(path)
        except Exception:  # pragma: no cover (defensive — 同期の辞書引き)
            star, later = 0, False
        tags: tuple[str, ...] = ()
        if self._user_tags_provider is not None:
            try:
                tags = tuple(self._user_tags_provider(path))
            except Exception:  # pragma: no cover (defensive)
                tags = ()
        strip.set_target(path, int(star or 0), bool(later), tags)

    def _request_curation(self, path: Path, kind: str, value: object) -> None:
        """印の要求をホストへ渡し、結果を中央オーバーレイで告げる。

        ホストのトーストは親ウィンドウに出て全画面の裏に隠れるので、
        ``_set_star`` と同じ「書いた後にプロバイダを読み直して判定する」流儀
        （#53 残り）でここが告知する。プロバイダ未注入なら判定できないので黙る。
        """
        self.curation_requested.emit(path, kind, value)
        self._sync_chrome()
        self._strip.update()
        if kind == "later":
            self._show_later_result(path, bool(value))
        elif kind == "star":
            self._show_star_result(int(value or 0))

    def _read_later(self, path: Path) -> bool | None:
        provider = self._curation_provider
        if provider is None:
            return None
        try:
            return bool(provider(path)[1])
        except Exception:  # pragma: no cover (defensive — 同期の辞書引き)
            return None

    def _toggle_later(self) -> None:
        """``L`` — 現在画像の「あとで見る」を反転する（席 = 全画面）。"""
        current = self.current_image()
        if current is None:
            return
        want = not bool(self._read_later(current))
        self._request_curation(current, "later", want)

    def _show_later_result(self, path: Path, want: bool) -> None:
        later = self._read_later(path)
        if later is None:
            return
        if later != want:
            self._title_overlay.show_message(
                t("viewer.lightbox.later_write_failed")
            )
        elif want:
            self._title_overlay.show_message(t("viewer.lightbox.later_set"))
        else:
            self._title_overlay.show_message(t("viewer.lightbox.later_cleared"))

    def refresh_curation(self) -> None:
        """キュレーション変更後の再描画フック（カウンタの ★N を取り直す）."""
        # ImageView 上に star バッジは描かない（没入表示を汚さない）が、
        # 左下の常時カウンタは現在画像の評価を映す (UIレビュー 07-25 #46)。
        self._sync_chrome()
        # 下端ストリップの★マークも取り直す (N-72)。
        self._strip.update()

    def set_resume_media(self, resume: "MediaResume | None") -> None:
        """中央プレビューで一時停止した動画の位置を引き継ぐ (N-142).

        ``None`` は「引き継ぎ無し」で、armed な保留があれば捨てる（入場の
        たびにホストが必ず呼ぶので、前のセッションの保留は持ち越さない）。
        """
        self._resume_media.set(
            None if resume is None
            else MediaResume(resume.path, max(0, int(resume.position_ms)))
        )

    def media_playback_position(self) -> "MediaResume | None":
        """いま動画ページで見ている引き継ぎ — 動画ページでなければ ``None``."""
        media = self._media
        if media is None or self._stack.currentWidget() is not media:
            return None
        path = self.current_image()
        if path is None:
            return None
        return MediaResume(path, media.playback_position())

    def set_chrome_hide_ms(self, ms: int) -> None:
        """操作 UI（上部バー・操作カプセル・画像リスト・カーソル）を隠すまでの時間.

        ``ViewerState.lightbox_chrome_hide_ms`` のライブ反映口
        （:meth:`set_slideshow_interval` と同じく、ホストが生成時と設定確定時に
        呼ぶ）。走っているタイマーは新しい間隔で仕切り直す。
        """
        ms = max(_AUTO_HIDE_MIN_MS, int(ms))
        self._chrome_hide_ms = ms
        self._hide_engine.timeout_sec = ms / 1000.0
        for timer in (self._hide_timer, self._strip_hide_timer):
            # setInterval は走行中のタイマーを新しい間隔で再始動する。
            timer.setInterval(ms)

    def chrome_hide_ms(self) -> int:
        return self._chrome_hide_ms

    def set_slideshow_interval(self, seconds: int) -> None:
        self._slideshow_interval_ms = max(1, int(seconds)) * 1000
        # 再生中の動画に対して止めてあったタイマーを復活させない（項目#27）—
        # 現在ページの判断は _sync_slideshow_timer に一元化してある。
        self._sync_slideshow_timer()

    def _nudge_slideshow_interval(self, delta_sec: int) -> None:
        """[ / ] で間隔を ±1 秒し、中央メッセージで新しい値を告げる (N-139).

        値の永続化はしない（``ViewerState.slideshow_interval_sec`` は設定
        ダイアログが唯一の書き手のまま）— ここは「いま見ている流れの速さを
        その場で直す」揮発の調整。告知は既にスライドショー終了通知で使って
        いる :class:`CenterMessageOverlay` を再利用する。
        """
        seconds = max(1, self._slideshow_interval_ms // 1000 + int(delta_sec))
        self._slideshow_interval_ms = seconds * 1000
        self._sync_slideshow_timer()
        self._title_overlay.show_message(
            t("viewer.lightbox.slideshow_interval", n=seconds)
        )

    def current_image(self) -> Path | None:
        if 0 <= self._index < len(self._images):
            return self._images[self._index]
        return None

    def current_folder(self) -> Path | None:
        return self._folder

    # ------------------------------------------------------------- opening

    def open_at(
        self,
        path: Path,
        siblings: Sequence[Path] | None = None,
        playlist: Sequence[Path] | None = None,
    ) -> None:
        """*path* を起点に全画面表示する。

        *siblings* は既知の兄弟画像リスト（右ペインのタイル由来 — I/O なし）で、
        **即時表示のシード**にのみ使う。実際のプレイリストは常にフォルダを
        off-thread で完全列挙（:func:`list_playlist_sorted` — 画像 + 動画）して
        確定する。右ペインはフィルタ／populate 途中などで画像を欠くことがあり、
        そのままだと playlist が不完全になって「全画像を
        見る前に末尾（=投稿横断の縁）に達し、深いフォルダへ早期に降りてしまう」
        ため。投稿横断先のフォルダも同じ列挙関数を使うので、これで
        **開いたフォルダも横断先も常にフォルダの全メディア**という一貫した意味論に
        なる。

        母集合の包含契約 (UIレビュー 07-25 #52 / #137 — 追修で「一致」から
        訂正): 列挙 (:func:`list_playlist_sorted`) は ``#thumb#…`` を除いた
        **``PLAYLIST_SUFFIXES``（画像 + 動画）のみ**。分割・最大化側の
        ``n/m``・画像トラック・‹ › が歩く ``ChildrenGrid.tile_paths`` は
        ``post.md`` / ``#thumb#…`` を落とすだけなので、``.pdf`` / ``.zip`` /
        ``.txt`` やフォルダのタイルはそちらにだけ残る。したがって両者は同じ
        集合ではなく ``playlist ⊆ tile_paths`` の一方向の包含が成り立つ —
        これで十分で、閲覧モードを閉じたときの着地ファイルは必ず右ペインの
        タイルとして存在し、選択同期が黙って失敗しない。

        *playlist* を渡すと（左ペインの検索/絞り込み結果からの起動 — G07）、
        それを**確定プレイリスト**として使い、フォルダ再列挙も投稿横断も行わない
        （検索結果の流し見）。
        """
        self._begin_open()
        self._folder = path.parent
        if playlist is not None:
            # G07: 明示プレイリスト（左ペインの表示順）をそのまま使う。
            self._playlist_locked = True
            self._images = list(playlist)
            self._index = (
                self._images.index(path) if path in self._images else 0
            )
        else:
            self._playlist_locked = False
            sibs = list(siblings) if siblings else []
            if path in sibs:
                self._images = sibs
                self._index = sibs.index(path)
            else:
                self._images = [path]
                self._index = 0
            # プレイリストは完全列挙で確定する（siblings は即時表示のシード）。
            self._open_stream.submit(
                lambda f=path.parent: list_playlist_sorted(f)
            )
        self._strip.set_images(self._images)
        self._show_fullscreen()
        self._show_index(self._index)

    def open_folder(self, folder: Path, notice: str = "") -> None:
        """*folder* の先頭メディアから全画面表示する（G05）.

        プレビュー中の画像が無い状態（フォルダを選択しているだけ）から F11 /
        メニューで閲覧モードへ入るための入口。即時シードが無いので、off-thread の
        完全列挙（:func:`list_playlist_sorted`）が landing してから先頭に着地する
        （:meth:`_on_open_scanned` の folder-open 経路）。

        *notice* を渡すと着地後に中央オーバーレイで一言告げる（N-21 — 非メディア
        表示中の F11 が「別のファイルを無言で開いた」ように見えるのを防ぐ）。
        着地前に出すと ``_show_index`` の描画に上書きされるので、表示は
        :meth:`_on_open_scanned` まで遅らせる。
        """
        self._begin_open()
        # _begin_open が前セッションの予告を消すので、代入はその後。
        self._open_notice = notice
        self._playlist_locked = False
        self._folder = folder
        self._images = []
        self._index = -1
        self._open_stream.submit(lambda f=folder: list_playlist_sorted(f))
        self._strip.set_images([])
        # クロームは _show_index 経由でしか更新されず、ここは列挙が着地する
        # まで（空フォルダなら永久に）_show_index に到達しない。ライトボックス
        # は使い回されるので、明示的に落とさないと前セッションの
        # 「2 / 3 ・ b.png」「post」が空案内カードの上に常設で残る（項目#74）。
        # 状態（空リスト・index -1）から導出する 1 本へ寄せてある（項目#75）。
        self._sync_chrome()
        self._show_fullscreen()

    def _begin_open(self) -> None:
        """open_at / open_folder 共通の状態リセット。"""
        self._closed_emitted = False
        # 前セッションの着地予告（open_folder の notice）を持ち越さない。着地が
        # 空フォルダだったり着地前に閉じたりすると消費されずに残り、
        # _maybe_show_hint がそれを「予告と重なる」と読んで初回ヒントを
        # 二度と出さなくなる（PR #189 レビュー）。
        self._open_notice = ""
        self._latch.reset()
        self._nav_gate.reset()
        self._stop_slideshow()
        self._invalidate_cross()

    def _invalidate_cross(self) -> None:
        """in-flight の投稿横断スキャン結果を無効化する（open/close を跨いで
        古い結果が `_apply_cross` に着地しないようセッションを切る）。

        ``GuardedStream.cancel`` は「現行セッションを cancel して世代を進める」
        であって「常に世代を進める」ではない（``SessionOwner.cancel`` は現行が
        無ければ何もしない = 冪等）。着地の選別は
        :meth:`GuardedStream.bind` が「セッションが生きている ∧ 世代一致」で
        行うので、この cancel の後に古い結果が ``_apply_cross`` へ届くことは
        無い（``token != latest_token()`` を手で書くと、走査を一度も投げて
        いない窓で素通りする — 項目#56 でトークンが単なる int からセッション
        世代になったときの意味論の差。項目#81 追補）。
        """
        self._cross_stream.cancel()
        self._cross_inflight = False

    def _show_fullscreen(self) -> None:
        """呼び出し元ウィンドウのスクリーンで全画面表示する（マルチモニタ対応）。"""
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self._view.setFocus()
        self._install_filters()
        self._maybe_show_hint()
        # 開いた直後は必ずクロームを点灯させる (UIレビュー 07-25 #2)。
        # ``_poke_activity`` の条件（``changed or not top_bar.isVisible()``）は
        # 初回だけ両方 False になる（AutoHideEngine.visible の初期値 True +
        # top_bar は一度も hide されていない = 論理状態と実可視の乖離）ため、
        # ここで明示しないと「マウスを動かしても操作カプセルが出ない」状態から
        # 始まってしまう。``force_show_chrome``（撮影用フック）と同じ点灯経路で、
        # こちらはオートハイドのタイマーは止めない。
        # 前回セッションの「ポインタが生きている」判定は持ち越さない — 置いた
        # ままのポインタがクロームの上に居ても保持が効かないところから始める。
        self._pointer_live = False
        self._last_pointer_pos = None
        self._set_chrome_visible(True)
        self._poke_activity()

    def _maybe_show_hint(self) -> None:
        """セッション初回のみ操作ヒントを数秒表示する（G05）。

        予告（``_open_notice`` — 「先頭の画像から再生します」）が待っている回は
        見送る（N-140）。ヒントは 5 秒・予告は 1.5 秒で 2 面が重なり、状況固有で
        今しか意味を持たない予告のほうが先に消えてしまう。``_hint_shown`` は
        **消費しない**ので、次に全画面へ入ったときにヒントが出る。
        """
        if self._hint_shown or self._open_notice:
            return
        self._hint_shown = True
        # 予約量は resizeEvent が更新しているが、初回表示が resize より先に
        # 来る経路でも重ならないよう、出す直前にも引き直す。
        self._hint.set_bottom_reserved(self._hint_bottom_reserved())
        self._hint.show_hint()

    def _hint_bottom_reserved(self) -> int:
        """ヒントが避ける下端の高さ = ストリップ帯 + 操作カプセル + マージン。"""
        return (
            self._strip.height() + self._capsule.height()
            + overlay_chrome.CAPSULE_MARGIN
        )

    def _on_open_scanned(self, payload: object) -> None:
        images = payload if isinstance(payload, list) else []
        current = self.current_image()
        if current is None:
            # folder-open 経路（open_folder）: 先頭メディアへ着地する。
            if not images:
                # N-86: 1.5 秒で消えるタイトルオーバーレイだけだと、以降は
                # マウスを動かすまで手掛かりが自発的に出ない（常設カウンタも
                # ``total <= 0`` で自分を隠す）。消えないカード + [終了] を
                # 出して行き止まりにしない。
                self._stack.setCurrentWidget(self._empty_view)
                # 着地先が無いので予告も消費先が無い — 残すと次セッションの
                # 初回ヒントを黙らせる。
                self._open_notice = ""
                # 空で確定した状態からクロームを導出し直す（項目#75）—
                # open_folder 側で落としてあるが、着地でこの分岐だけ
                # 塗り直しを欠く形にはしない。
                self._sync_chrome()
                return
            self._images = images
            self._strip.set_images(self._images)
            self._show_index(0)
            if self._open_notice:
                # 着地後に予告を出す（N-21）。_show_index の描画を上書き
                # しないよう順序はこの通り。1 回きりなので即クリアする。
                self._title_overlay.show_message(self._open_notice)
                self._open_notice = ""
            return
        if current not in images:
            return  # フォルダにメディアが見つからない — シード表示のまま
        if images == self._images:
            return  # 既にシードが完全一致 — 差し替え不要
        self._images = images
        self._index = images.index(current)
        self._strip.set_images(self._images)
        self._sync_chrome()

    # ---------------------------------------------------------- navigation

    def _sibling_provider(self, path: Path) -> tuple[list[Path], int]:
        # ImageView のプリフェッチ専用 provider（画像先読み）。self._images は
        # list_playlist_sorted 由来で動画（VIDEO_SUFFIXES）を含むため、ここで
        # IMAGE_SUFFIXES のみへ絞る — 絞らないと ImageView._schedule_prefetch が
        # 隣接動画を decode_pil→read_file_bytes でファイル全体読み込みしては
        # 捨てる（NAS で毎ステップ数百 MiB〜GiB の無駄 I/O。中央ペイン provider
        # FileListView.image_siblings と同じ規律）。表示・ナビは self._images を
        # 歩く別経路なので画像のみへ絞っても影響しない。現在パスが動画で画像列に
        # 無い場合は ([], -1)（既存の ValueError 経路と同じ = 先読みしない）。
        images = [p for p in self._images if p.suffix.lower() in IMAGE_SUFFIXES]
        try:
            i = images.index(path)
        except ValueError:
            return [], -1
        return images, i

    def _on_strip_cell_clicked(self, index: int) -> None:
        """フィルムストリップのセルクリック = 手動送り（N-83②で停止する）."""
        self._stop_slideshow_for_manual_nav()
        self._show_index(index)

    def _show_index(self, index: int, step: int = 0) -> None:
        """*index* のページを出す。

        *step* は「そのページを出せなかったときに読み進める向き」（0 = 進め
        ない）。動画は ``MediaView`` を構築できない環境（Qt6Multimedia.dll
        欠落 / AV 隔離）では出せないので、巻き戻すだけだとその位置から先へ
        二度と進めなくなる: → を何度押しても index が動かず、スライドショー
        は同じ動画を間隔ごとに試し続ける無限リトライになる。読み進める先が
        無ければ（あるいは進めない呼び出しなら）スライドショーは終わらせる。
        """
        if not (0 <= index < len(self._images)):
            return
        prev_index = self._index
        prev_folder = self._folder
        while True:
            if not (0 <= index < len(self._images)):
                # 飛ばせる先が尽きた — 位置は動かさず、自動送りは終わらせる。
                self._index = prev_index
                self._folder = prev_folder
                self._abort_unplayable_slideshow()
                return
            self._index = index
            path = self._images[index]
            if self._playlist_locked:
                # G07（検索 / 絞り込み結果の明示プレイリスト）は**複数フォルダ
                # に跨る**ファイル列なので、現在フォルダを 1 歩ごとに追随させる
                # （項目#23）。追随しないと上部バーが最初の投稿名を出し続け、
                # closeEvent の closed(folder, image) が「フォルダと画像が矛盾
                # した ペア」をホストへ渡す — ホストは古いフォルダを選択した
                # うえで別フォルダの画像を選ぼうとして必ず失敗し、pending が
                # 永久に解決しないまま無関係なフォルダに着地する。非 G07 経路
                # ではプレイリストが常に単一フォルダ由来（open_at /
                # open_folder / _apply_cross がその都度 _folder を設定する）
                # なので影響しない。
                self._folder = path.parent
            if not is_video_path(path):
                self._show_still(path)
                break
            if self._show_video(path):
                break
            # 遅延構築（QtMultimedia）に失敗 = 動画ページは出せない。巻き戻さ
            # ないと「表示は前の画像のまま、index だけ 1 つ進んだ」状態が残り、
            # 次の → が 2 枚先へ飛ぶ（項目#76）。
            if not step:
                self._index = prev_index
                self._folder = prev_folder
                self._abort_unplayable_slideshow()
                return
            index += step
        # クローム（カプセルのフィット表示 / ズーム% / ストリップ選択 /
        # 題名 / カウンタ）は状態から一括導出する（項目#75）。
        self._sync_chrome()
        # 画像を移動したので下端の画像リストを一瞬表示（その後自動消灯）。
        self._show_strip()
        # ページが変わったので自動送りタイマーを張り直す（動画の残り尺
        # ウォッチドッグへの張り直しも _sync_slideshow_timer の一元判断 —
        # 項目#26）。**2026-08-28 のユーザー裁定で 07-25 #26 の
        # 「手動ナビでもタイマーは止めない」は改定された**（N-83②）: 手動送りは
        # ``_stop_slideshow_for_manual_nav`` が入口側で完全停止させるので、
        # ここへ到達する時点で ``_slideshow_active`` は False = 早期 return。
        # つまりこの呼び出しはスライドショー自身の送り（_slideshow_advance /
        # _apply_cross）専用の張り直しになった。
        self._sync_slideshow_timer()

    def _abort_unplayable_slideshow(self) -> None:
        """出せないページで止まったときに自動送りを終わらせる。

        ``_slideshow_timer`` はリピートなので、``_show_index`` の早期 return
        が ``_sync_slideshow_timer`` に届かなくても回り続ける — 止めないと
        同じ動画を間隔ごとに試し、そのたびに中央オーバーレイと warning が
        1 行ずつ増える。
        """
        if not self._slideshow_active:
            return
        self._stop_slideshow()
        self._title_overlay.show_message(
            t("viewer.lightbox.slideshow_finished")
        )

    def _show_still(self, path: Path) -> None:
        """静止画ページ（ImageView）へ切り替えて表示する。"""
        if self._media is not None:
            self._media.clear_media()  # 前の動画を停止
        self._stack.setCurrentWidget(self._view)
        self._view.show_image(path)

    def _show_video(self, path: Path) -> bool:
        """動画ページ（MediaView）へ切り替えて再生する（G08 / F06）。

        構築できたら True。``MediaView.__init__`` は QtMultimedia を遅延
        import するので、Qt6Multimedia.dll 欠落 / AV 隔離環境では構築自体が
        失敗する — 中央ペインの ``ContentView.show_media`` と同じく降格させ、
        呼び出し元（:meth:`_show_index`）が位置を巻き戻せるよう False を返す
        （項目#76）。ガードが無いと例外がスロット境界へ抜けて握り潰され、
        「画面は前の画像のまま・index だけ進む」無言の不整合になる。
        """
        media = self._ensure_media()
        if media is None:
            # 構築失敗のトレースバックは工場関数（build_media_view）が
            # プロセス 1 回だけ出す。ここは経路とパスだけ記録する。
            first = not self._media_unavailable_logged
            self._media_unavailable_logged = True
            if first:
                logger.error(
                    "閲覧モードのメディアビューを初期化できません: {}", path
                )
            else:
                logger.warning("メディアプレビューは利用できません: {}", path)
            self._title_overlay.show_message(
                t("viewer.lightbox.media_unavailable")
            )
            return False
        # スライドショー中はループ再生設定を一時停止する（#13）: ネイティブ
        # 無限ループでは EndOfMedia が発生せず、playback_finished の前倒し
        # 次送りが永久に来ない。設定・ボタン状態は変えず、スライドショー
        # 停止で復帰。
        media.set_loop_suppressed(self._slideshow_active)
        # 静止画ページを離れる = アニメ GIF / WebP の QMovie を止める（項目#130）。
        # 隠れた QMovie は停止しない限り GUI スレッドでフレームをデコードし
        # 続け、全画面の動画再生と同じスレッドを奪い合う。静止画へ戻る経路は
        # 必ず show_image を通って QMovie を作り直すので resume は不要
        # （中央ペインの ContentView._on_page_changed と同じ扱い。_show_still
        # 側の _media.clear_media() と対になる後始末）。
        self._view.pause_animation()
        self._stack.setCurrentWidget(media)
        # 中央プレビューから引き継いだ再生位置があれば、その動画の
        # **1 回目の表示にだけ**当てる (UIレビュー 2026-08-28 N-142)。
        # 従来は全画面が必ず 0 から流し直していた。パスが違う表示では
        # 保留を**降ろさない** — 引き継ぎ先はその動画の初回表示だけで、
        # 途中に別のページを挟んでも食い潰されない。
        resume = self._resume_media.take_if(lambda r: r.path == path)
        media.show_media(path, 0 if resume is None else resume.position_ms)
        return True

    def _on_capsule_fit_toggle(self) -> None:
        """カプセルの「フィット / 実寸」ボタン（UIレビュー #25）.

        内部 ImageView の非公開トグルを直接呼ぶ — このウィンドウは既に
        ``self._view`` を専有しており、動画ページの再生 / 一時停止でも同様に
        ``self._media._on_play_pause()`` を直接呼んでいる（本ファイル内の
        既存パターン）のと同じ結線方針。``image_view.py`` 側に公開 API を
        新設せず、自己完結を保つ。%表示は ``zoom_changed`` 経由で追従する。
        """
        self._view._toggle_fit_actual()

    def _on_view_zoom_changed(self, percent: float) -> None:
        """内部 ImageView のズーム変化をカプセルの%表示へ反映する."""
        self._capsule.set_zoom(percent)

    def _refresh_capsule_zoom(self) -> None:
        """カプセルのズーム%表示を内部 ImageView の現在値と同期する.

        ``zoom_changed`` が発火しない場面（画像切替直後のシード表示・
        ウィンドウリサイズによるフィット倍率変化）で明示的に呼ぶ。
        """
        try:
            percent = self._view._effective_zoom() * 100.0
        except Exception:  # pragma: no cover (defensive)
            return
        self._capsule.set_zoom(percent)

    def _ensure_media(self):
        """MediaView を遅延生成してスタックの動画ページへ差し込む（構築失敗は ``None``）.

        構築とホストへの配線は :func:`~snappix.viewer.media_view.build_media_view`
        が一手に負う（項目#71）— 中央ペイン ``ContentView._ensure_media`` と
        手書きで重複していた配線（遅延 import / navigate_requested / loop /
        volume / rate の再送出 / 保留設定の当て込み / 構築失敗の降格）を
        1 箇所へ寄せた。ここに残るのはこのウィンドウ固有の差分だけ:
        プレースホルダとのスタック差し替え、終端 / 尺の購読（スライドショー）、
        遅延生成した部分木へのイベントフィルタ設置。media_view.py の
        **公開シグナル / 公開 API のみ**を使う（private ``_player`` の購読は
        項目#26 で撤去）。
        """
        if self._media is None:
            from .media_view import build_media_view  # noqa: PLC0415 (lazy QtMultimedia)

            media = build_media_view(
                self,
                # 動画ページで ←/→ を効かせる（C9）: MediaView の QShortcut が
                # 発火する navigate_requested を 1 ステップナビへ配線する。
                on_navigate=self._on_media_navigate,
                # ループ / 音量 / 速度のユーザー変更をホストへ再送出し、
                # ViewerState へ永続化させる（item 8 / N-136）。
                on_loop=self.media_loop_toggled.emit,
                on_volume=self.media_volume_changed.emit,
                on_rate=self.media_playback_rate_changed.emit,
                # 再生終端（EndOfMedia / InvalidMedia — MediaView 側で正規化、
                # 項目#26）→ スライドショーの前倒し次送り。
                on_finished=self._on_playback_finished,
                # 尺が判明したらウォッチドッグを「残り尺 + 余裕」へ張り直す。
                on_duration=self._on_media_duration_changed,
                # 構築前に届いていたメディア設定（音量 / 自動再生 / ループ）。
                # 無ければハードコード既定（音量 70 / 自動再生 ON）になる。
                pending_state=self._pending_media_state.peek(),
            )
            if media is None:
                return None
            # 構築が失敗した枝では降ろさない（次の試行へ持ち越す）。
            self._pending_media_state.clear()
            idx = self._stack.indexOf(self._media_placeholder)
            self._stack.insertWidget(idx, media)
            self._stack.removeWidget(self._media_placeholder)
            self._media_placeholder.deleteLater()
            self._media_placeholder = None
            self._media = media
            # 遅延生成した MediaView 部分木にもキー先取り + 活動検出フィルタを仕掛ける。
            if self._filters_installed:
                for w in [media, *media.findChildren(QWidget)]:
                    w.setMouseTracking(True)
                    w.installEventFilter(self)
        return self._media

    def apply_media_settings(self, state: "ViewerState") -> None:
        """ユーザーのメディア設定（音量 / 自動再生 / ループ）を MediaView へ反映.

        MediaView は遅延生成なので、未構築なら state を退避し :meth:`_ensure_media`
        の構築時に適用する。中央ペインの ``ContentView.apply_media_settings`` と
        同じ遅延構築ハンドオフで、閲覧モードの動画がハードコード既定値
        （音量 70 / 自動再生 ON）で再生されるのを防ぐ（item 8）。
        """
        if self._media is None:
            self._pending_media_state.set(state)
            return
        self._media.apply_settings(state)

    def apply_view_state(self, state: "ViewerState") -> None:
        """ImageView 系のユーザー設定を内部 ImageView へ一括反映する（項目#29）.

        閲覧モードは中央ペインとは**別インスタンス**の ImageView を持つため、
        中央ペインと同じ fan-out（``image_view.apply_state``）をここでも通さ
        ないと、設定ダイアログの ImageView 設定（キャッシュ予算 3 種 /
        先読み枚数 / ズーム維持 / ミニマップ）が閲覧モードにだけ届かず、
        モジュール既定のまま動き続ける（項目#21 / #34 で実測）。個別項目の
        手配線は繰り返し取りこぼしを生んだので、ImageView への設定は必ず
        共通関数側に足すこと。
        """
        apply_image_view_state(self._view, state)

    def _on_media_navigate(self, delta: int, immediate: bool) -> None:
        """動画ページの ←/→（MediaView.navigate_requested）を 1 ステップナビへ（C9）.

        MediaView は ``immediate=True`` で発火するので WheelNavGate は通さず、
        符号だけを見て :meth:`_nav_key` に委ねる（エッジの投稿横断ラッチも
        通常経路と同じく効く）。
        """
        self._nav_key(1 if delta > 0 else -1)

    def _on_playback_finished(self) -> None:
        """MediaView の正規化済み再生終端 → スライドショーを前倒しで次へ。

        終端判定（EndOfMedia / InvalidMedia / ループ継続の除外）は MediaView
        側の :attr:`~snappix.viewer.media_view.MediaView.playback_finished`
        が一手に負う（項目#26）。ここでは「スライドショー中の動画ページか」
        だけを見る。ウォッチドッグ（間隔タイマー）はこの通知が来なくても
        有限時間で次へ送る保険として常時武装している。
        """
        if not self._slideshow_active:
            return
        cur = self.current_image()
        if cur is not None and is_video_path(cur):
            self._slideshow_advance()

    def _on_media_duration_changed(self, _dur: int) -> None:
        """動画の尺が判明 → ウォッチドッグを「残り尺 + 余裕」へ張り直す（項目#26）."""
        self._sync_slideshow_timer()

    def _sync_chrome(self) -> None:
        """クロームの**内容**を現在状態から一括で導出する（項目#75）.

        表示される文字・選択・アイコンは全て ``(self._images, self._index,
        self._folder, self._slideshow_active, 現在ファイルの★)`` から一意に
        決まる純粋な派生値なのに、導出関数が無かったため「状態を変える 9 つの
        入口が、それぞれどのクロームを塗り直すかを個別に選ぶ」形になっており、
        **呼び忘れが正しさの単一障害点**になっていた（``open_folder`` が前
        セッションの「2 / 3 ・ b.png」を残した項目#74 はその発現）。状態を
        変える入口は末尾でこの 1 本を呼ぶ。

        **可視（setVisible）には一切触れない** — 上部バー / カプセルの
        1.5 秒オートハイドと下端ストリップの独立タイマーは
        :meth:`_set_chrome_visible` / :meth:`_show_strip` の専管で、内容の
        同期がそこへ割り込むと「消えているはずの面が表示側の都合で点く」。
        カウンタは ``set_info`` が ``total <= 0`` で自分を隠す既存契約に乗る
        （空状態はこの経路で自然に畳まれる）。

        ``_strip.set_images`` はここに含めない: パス列が変わったときだけ呼ぶ
        （サムネイルのキャッシュを捨てるので毎回は呼べない）。
        """
        path = self.current_image()
        # フィット⇄実寸・ズーム表示は静止画専用の概念 — 動画ページと空状態
        # では隠す（UIレビュー #25 のカプセル。隠れた ImageView の古い状態を
        # 誤って読ませないための内容切替で、オートハイドとは別軸）。
        self._capsule.set_fit_controls_visible(
            path is not None and not is_video_path(path)
        )
        self._refresh_capsule_zoom()
        self._strip.set_current(self._index)
        # 上部バーの再生/一時停止アイコン（常設カウンタ側の実行中表示は
        # _update_counter が同じ _slideshow_active から描く — N-83①）。
        self._top_bar.set_slideshow_running(self._slideshow_active)
        # 歩ける先（= プレイリストが空でない）の有無を送り / 再生ボタンの活性へ
        # （N-21）。``setEnabled`` は可視ではないので上の「setVisible に触れない」
        # 契約には抵触しない。
        walkable = bool(self._images)
        self._capsule.set_step_enabled(walkable, walkable)
        self._top_bar.set_slideshow_enabled(walkable)
        # 題名（上部バー）→ 末尾で _update_counter（n/m・ファイル名・★）。
        self._update_top_bar()

    def _update_top_bar(self) -> None:
        path = self.current_image()
        self._sync_strip(path)
        if path is None:
            self._top_bar.set_text("")
            self._update_counter()
            return
        # 上部バーは**投稿タイトルのみ**（+ 印ストリップ — E2）。n/m とファイル名は
        # 常時表示カウンタへ一本化した（オートハイドで消える面と常設面で同じ情報を
        # 二重に持たない — UIレビュー 07-25 #91）。
        self._top_bar.set_text(
            self._folder.name if self._folder is not None else ""
        )
        self._update_counter()

    def _read_star(self) -> int | None:
        """現在ファイルのスター、**読めなければ** ``None``.

        ``None`` は「0 だった」ではなく「分からなかった」— プロバイダ未注入か
        現在ファイル無し、あるいはプロバイダが例外を投げた場合。表示は 0 に
        丸めてよい（:meth:`_current_star`）が、書き込み結果の照合
        （:meth:`_set_star`）は丸めてはいけない: 0 に丸めると star=0 の要求
        だけが「読めなかった」を「要求どおり 0 になった」と取り違える。
        """
        provider = self._star_provider
        path = self.current_image()
        if provider is None or path is None:
            return None
        try:
            return int(provider(path))
        except Exception:  # pragma: no cover (defensive)
            return None

    def _current_star(self) -> int:
        """現在ファイルのスター（プロバイダ未注入・失敗時は 0）(UIレビュー 07-25 #46)."""
        value = self._read_star()
        return 0 if value is None else value

    def _update_counter(self) -> None:
        """常時表示カウンタ（G02）を現在位置 + ★N で更新する.

        上部バーが隠れていても n/N・ファイル名・スター評価が読める唯一の面
        (UIレビュー 07-25 #91 / #46)。
        """
        path = self.current_image()
        if path is None:
            self._counter.set_info(0, 0, "")
            return
        # 幅上限を先に与えてから組み立てる（省略は set_info の中で 1 回だけ）。
        self._counter.set_width_limit(self._counter_width_budget())
        self._counter.set_info(
            self._index + 1,
            len(self._images),
            path.name,
            self._current_star(),
            self._slideshow_active,
        )
        self._position_counter()
        self._counter.raise_()

    def _nav_key(self, delta: int) -> None:
        """←/→・ホイール 1 ステップぶんのナビゲーション（エッジでラッチ）.

        手動送りなのでスライドショーは完全停止する（N-83② — 2026-08-28 の
        ユーザー裁定。以前は間隔を仕切り直すだけで走り続けていた）。
        """
        if not self._images:
            return
        self._stop_slideshow_for_manual_nav()
        new = self._index + delta
        if 0 <= new < len(self._images):
            self._latch.reset()
            # 出せないページ（構築できない動画）は同じ向きへ読み飛ばす。
            self._show_index(new, delta)
            return
        # G07: 検索/絞り込み結果のフラットなプレイリストでは投稿横断しない
        # （結果列の端に達したら一言だけ知らせる）。
        if self._playlist_locked:
            self._title_overlay.show_message(
                t("viewer.lightbox.no_next_post") if delta > 0
                else t("viewer.lightbox.no_prev_post")
            )
            return
        # プレイリストのエッジ: 1 回目は予告、同方向 2 回目で投稿横断。
        now = time.monotonic()
        if self._latch.try_cross(delta, now):
            self._start_cross(delta)
        else:
            self._title_overlay.show_message(
                t("viewer.lightbox.press_again_next") if delta > 0
                else t("viewer.lightbox.press_again_prev")
            )

    def _on_view_navigate(self, delta: int, immediate: bool) -> None:
        # 内部 ImageView のスクロール端ホイール。中央プレビューと同一の
        # grace 機構（WheelNavGate）を通してから 1 ステップ進める。
        if self._nav_gate.check(delta, immediate):
            self._nav_key(1 if delta > 0 else -1)

    # ------------------------------------------------------- cross-post

    def _resolve_cross_root(self, top_level: list[Path]) -> Path | None:
        """DFS の探索境界（root）を決める: root プロバイダ優先、無ければ
        root 直下の兄弟の親（表示リストの先頭要素の親）を暫定 root にする。"""
        if self._root_provider is not None:
            try:
                root = self._root_provider()
            except Exception:  # pragma: no cover (defensive)
                root = None
            if root is not None:
                return root
        if top_level:
            return top_level[0].parent
        return None

    def _start_cross(self, direction: int) -> None:
        """隣接投稿への遷移を開始する（off-thread で深さ優先に画像列挙）."""
        if self._cross_inflight:
            # 押し直しは無視するが、無言にはしない — 大きなライブラリでは
            # 探索に数秒かかり、着地まで何も出ないと「効いていない」に見える。
            self._title_overlay.show_message(
                t("viewer.lightbox.cross_searching")
            )
            return
        folder = self._folder
        # ホストから注入されたコールバックは必ず包む（``_resolve_cross_root``
        # の ``_root_provider`` / ``_read_star`` / ``_sync_strip`` 等と同じ
        # 防御）。左ペイン再構築中の例外がここから ``_nav_key`` →
        # ``eventFilter`` を貫くと、Qt のスロット境界で握り潰されて利用者には
        # 「→ が効かない」だけが残る。
        top_level: list[Path] = []
        if self._post_provider is not None:
            try:
                top_level = list(self._post_provider())
            except Exception:  # pragma: no cover (defensive)
                top_level = []
        root = self._resolve_cross_root(top_level)
        if folder is None or root is None:
            self._on_cross_unavailable(direction)
            return
        self._cross_inflight = True
        self._cross_direction = direction
        # 探索中の告知（着地で別のメッセージに置き換わる）。
        self._title_overlay.show_message(t("viewer.lightbox.cross_searching"))
        self._cross_stream.submit(
            lambda r=root, c=folder, d=direction, tl=top_level: (
                scan_adjacent_image_folder(r, c, d, tl)
            )
        )

    def _on_cross_scanned(self, payload: object) -> None:
        self._cross_inflight = False
        if not isinstance(payload, tuple):
            self._on_cross_unavailable(self._cross_direction)
            return
        folder, images = payload
        self._apply_cross(folder, images, self._cross_direction)

    def _on_cross_unavailable(self, direction: int) -> None:
        if self._slideshow_active and direction > 0:
            # ループはしない: 最後の投稿まで行ったらスライドショー停止。
            self._stop_slideshow()
            self._title_overlay.show_message(
                t("viewer.lightbox.slideshow_finished")
            )
        else:
            self._title_overlay.show_message(
                t("viewer.lightbox.no_next_post") if direction > 0
                else t("viewer.lightbox.no_prev_post")
            )

    def _apply_cross(
        self, folder: Path, images: list[Path], direction: int,
    ) -> None:
        """投稿横断遷移を確定する（→は先頭、←は末尾の画像へ）."""
        self._folder = folder
        self._images = list(images)
        self._index = 0 if direction > 0 else len(images) - 1
        self._latch.reset()
        # 前の投稿のプリフェッチ済み PIL は兄弟リスト外 — 予算を新投稿へ返す。
        self._view.invalidate_sibling_cache()
        self._strip.set_images(self._images)
        # 横断の向きへ読み進める（先頭 / 末尾が出せない動画でも止まらない）。
        self._show_index(self._index, 1 if direction > 0 else -1)
        # 文脈切替の可視化: 投稿タイトルを 1.5 秒オーバーレイ表示。
        self._title_overlay.show_message(folder.name)
        self.post_changed.emit(folder, self._images[self._index])

    # -------------------------------------------------------- slideshow

    def slideshow_active(self) -> bool:
        return self._slideshow_active

    def toggle_slideshow(self) -> None:
        if self._slideshow_active:
            self._stop_slideshow()
        else:
            self._start_slideshow()

    def _start_slideshow(self) -> None:
        if not self._images:
            return
        self._slideshow_active = True
        # 上部バーの再生/一時停止アイコンと、消えない面（常設カウンタ）の
        # 実行中表示（N-83①）— どちらも _slideshow_active からの派生値なので
        # 導出 1 本に任せる（項目#75）。
        self._sync_chrome()
        # 既に動画を再生中に S で開始したケースも次送りを保証する（#13）。
        if self._media is not None:
            self._media.set_loop_suppressed(True)
        # 開始時点のページも _show_index と同じ判断を通す（項目#27）— 動画を
        # 見ながら S を押す＝最も自然な開始操作で、その 1 本目だけが
        # slideshow_interval_sec（既定 5 秒）で打ち切られていた。
        self._sync_slideshow_timer()

    def _sync_slideshow_timer(self) -> None:
        """自動送りタイマーを現在ページに合わせて張り直す（項目#26 / #25 / #27）.

        スライドショー中の唯一のタイマー制御点で、**タイマーは決して止めない**
        — 「どの再生状態でも有限時間で次へ進む」を構造的に保証するウォッチ
        ドッグとして常時武装する。以前は動画ページで止めて ``EndOfMedia`` に
        一点依存し、沈黙経路（ネイティブループ #13 / InvalidMedia C10 /
        自動再生 OFF #25）が見つかるたびに条件を 1 つ足していた。

        * 静止画 / 再生していない動画（自動再生 OFF・一時停止・見終わった
          動画で S）: 固定間隔で送る（項目#25 / #27 の挙動を保存）。
        * 実再生中の動画: ``max(間隔, 残り尺 + 余裕)`` で武装 — 尺の長い動画
          を間隔で打ち切らない（項目#27）。正常なら ``playback_finished`` が
          先に来て前倒しで進み、来ない未知の経路でもこのタイマーが送る。
          尺が未判明の間は固定間隔のフロアで待ち、判明した時点で
          ``duration_changed`` → ここが呼ばれて張り直される。
        """
        if not self._slideshow_active:
            return
        interval = self._slideshow_interval_ms
        media = self._media
        if media is not None and self._stack.currentWidget() is media:
            remaining = media.expected_remaining_ms()
            if remaining is not None:
                interval = max(interval, remaining + _SLIDESHOW_VIDEO_SLACK_MS)
        self._slideshow_timer.start(interval)

    def _stop_slideshow(self) -> None:
        self._slideshow_active = False
        self._slideshow_timer.stop()
        # ループ再生設定の一時停止を解除（ユーザー設定へ復帰、#13）。
        if self._media is not None:
            self._media.set_loop_suppressed(False)
        # 上部バーのアイコンと常設カウンタの実行中表示を落とす（N-83①）。
        self._sync_chrome()

    def _stop_slideshow_for_manual_nav(self) -> None:
        """手動送りでスライドショーを**完全停止**する（N-83②）.

        **2026-08-28 ユーザー裁定で 07-25 #26 の「タイマーは止めない」を改定**:
        手動で送ったのに数秒後に自動送りが割り込むのは、一般的なビューアの
        慣習（手動操作で自動送りは止まる）から外れており、「止めたつもりが
        止まっていない」体験になっていた。再開は S / 上部バーの再生ボタン。
        Esc は従来どおり**全画面終了**であって停止ではない。

        呼び出し元は手動ナビの入口だけ（``_nav_key`` / Home / End /
        フィルムストリップのセルクリック）。スライドショー自身の送り
        （``_slideshow_advance`` → ``_show_index`` / ``_start_cross``）は
        ``_nav_key`` を通らないので巻き込まれない。
        """
        if not self._slideshow_active:
            return
        self._stop_slideshow()
        self._title_overlay.show_message(
            t("viewer.lightbox.slideshow_stopped_manual")
        )

    def _on_slideshow_tick(self) -> None:
        self._slideshow_advance()

    def _slideshow_advance(self) -> None:
        """スライドショーを 1 枚進める（タイマー tick・動画の再生終了の両方から）。"""
        if not self._images:
            self._stop_slideshow()
            return
        if self._index + 1 < len(self._images):
            self._show_index(self._index + 1, 1)
        elif self._playlist_locked:
            # G07: 検索結果の末尾ではループ/横断せず停止する。
            self._stop_slideshow()
            self._title_overlay.show_message(
                t("viewer.lightbox.slideshow_finished")
            )
        else:
            # スライドショー中の投稿横断は確認エッジなしで自動継続。
            self._start_cross(1)

    # --------------------------------------------------------- auto-hide

    def _install_filters(self) -> None:
        """キー先取り + アクティビティ検出のイベントフィルタを子全体へ.

        内部 QScrollArea が矢印キーを消費し、MouseMove は深い子にしか届かない
        ため、子ウィジェット全員に filter + mouseTracking を仕掛ける。
        """
        if self._filters_installed:
            return
        self._filters_installed = True
        for w in [self, *self.findChildren(QWidget)]:
            w.setMouseTracking(True)
            w.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        etype = event.type()
        if etype == QEvent.Type.MouseMove:
            pos = QCursor.pos()
            if pos == self._last_pointer_pos:
                # 位置の変わらない Move は操作ではない（合成イベント）。
                return super().eventFilter(obj, event)
            self._last_pointer_pos = pos
            self._pointer_live = True
            self._poke_activity()
            # 下端リビール帯にカーソルが入ったら画像リストを召喚する
            # （中央での拡大・パンでは出さない — 画像移動時のみの表示が原則）。
            if self._cursor_in_strip_zone():
                self._show_strip()
        elif etype == QEvent.Type.MouseButtonPress:
            self._pointer_live = True
            self._poke_activity()
        elif etype == QEvent.Type.Wheel:
            # ホイールは画像送り — ポインタは置いてあるだけ（狙っていない）。
            self._pointer_live = False
            self._poke_activity()
            # ただし下端帯の中のホイールは画像リストの横スクロール
            # （``FilmstripView.wheelEvent``）そのもの — サムネを探している
            # 最中に帯が消えて、続くティックが背面の画像送りに落ちないよう、
            # MouseMove と同じく消灯タイマーを仕切り直す。
            if self._cursor_in_strip_zone():
                self._show_strip()
        elif etype == QEvent.Type.KeyPress:
            self._pointer_live = False
            self._poke_activity()
            if self._handle_key(event):
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._handle_key(event):
            return
        super().keyPressEvent(event)

    def _handle_key(self, event) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            self.close()
            return True
        if key == Qt.Key.Key_Left:
            self._nav_key(-1)
            return True
        if key == Qt.Key.Key_L and event.modifiers() in (
            Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier
        ):
            # 別トップレベル窓なのでメインウィンドウの ``L`` は届かない —
            # 0-5 と同じくここで受けて全画面の席で解決する（N-16）。
            self._toggle_later()
            return True
        if key == Qt.Key.Key_Right:
            self._nav_key(1)
            return True
        if key == Qt.Key.Key_Home:
            self._stop_slideshow_for_manual_nav()  # 手動送り (N-83②)
            self._latch.reset()
            self._show_index(0)
            return True
        if key == Qt.Key.Key_End:
            self._stop_slideshow_for_manual_nav()  # 手動送り (N-83②)
            self._latch.reset()
            self._show_index(len(self._images) - 1)
            return True
        if key == Qt.Key.Key_Space:
            # 動画ページ表示中の Space は再生/一時停止（MediaView 単体・
            # ショートカット一覧と同じ割り当て）。「止めようとしたら次へ
            # 飛ぶ」誤爆を防ぐ（UIレビュー #18）。フォーカスが静止画ページに
            # 残ったままでもここで委譲されるので確実に効く。
            if (
                self._media is not None
                and self._stack.currentWidget() is self._media
            ):
                self._media._on_play_pause()
                return True
            if self._stack.currentWidget() is self._empty_view:
                # 空プレイリストのカード表示中は Space に意味が無い
                # （``_nav_key`` は画像ゼロで即 return する）のに、消費だけは
                # していた。フィルタは自分と全子ウィジェットに掛かっているので
                # フォーカスのある [終了] ボタンへ KeyPress が届かず、
                # N-86 が「行き止まりにしない」ために置いた唯一の可視アクション
                # がマウス専用になっていた。素通しして押せるようにする。
                return False
            # G04: Space = 次の画像（一般ビューア慣習）。旧 Space=スライドショーは
            # S へ移動した。末尾では ←/→ と同じく投稿横断ラッチに従う。
            self._nav_key(1)
            return True
        if key == Qt.Key.Key_S:
            # G04: S = スライドショー開始 / 停止。
            self.toggle_slideshow()
            return True
        if key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
            # [ / ] = スライドショー間隔を ±1 秒 (UIレビュー 2026-08-28 N-139)。
            # 従来は設定ダイアログ（実行できない画面）が唯一の変更点で、
            # 「遅すぎる / 速すぎる」と気づいた場所から直せなかった。
            self._nudge_slideshow_interval(
                -1 if key == Qt.Key.Key_BracketLeft else 1
            )
            return True
        if (
            Qt.Key.Key_0 <= key <= Qt.Key.Key_5
            and event.modifiers()
            in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)
        ):
            # 数字 0–5 で現在ファイルのスターを設定 / 解除する。GalleryView と
            # 同じく素（またはテンキーのみ）の修飾キーでガードし、Ctrl+2〜5 や
            # Alt+数字といったショートカットが黙ってスターを書き換えて user_meta
            # に永続化されるのを防ぐ（レビュー項目 20）。
            self._set_star(key - Qt.Key.Key_0)
            return True
        return False

    def _set_star(self, star: int) -> None:
        """数字キー 0–5 で現在ファイルにスターを設定 / 解除する。

        書き込みが**永続化できなかったとき**は要求値のオーバーレイを出さない
        (#53 残り)。ホスト側の単一書き手（``PostGrid._apply_curation``）は失敗時
        にインメモリマップを据え置くので、スタープロバイダ（同じマップの同期
        読み取り）の値が要求値と食い違うことが「ディスクに届かなかった」の判定に
        なる。ホストの警告トーストは親ウィンドウに出て全画面の裏に隠れるため、
        ここでも中央オーバーレイで知らせる。プロバイダ未注入の単体構成では
        判定できないので従来どおり要求値を出す。
        """
        current = self.current_image()
        if current is None:
            return
        self.star_key_requested.emit(star, current)
        # ホストの書き込み（同期）後に常時カウンタの ★N を取り直す — 0-5 が
        # 即時に読める面へ反映される (UIレビュー 07-25 #46)。ホストからは
        # ``refresh_curation`` でも同じ更新が届くが、プロバイダだけ注入された
        # 単体構成でも即時反映されるようここでも更新する（項目#75 の導出 1 本）。
        self._sync_chrome()
        self._show_star_result(star)

    def _show_star_result(self, star: int) -> None:
        # 照合は丸めない読み取り（``_read_star``）で行う: 例外で読めなかった
        # ときに 0 へ丸めると、star=0 の要求だけが「書けたか分からない」を
        # 「要求どおり 0 になった」と取り違えて成功の体裁になる（#53 残り）。
        if self._star_provider is not None and self._read_star() != star:
            self._title_overlay.show_message(
                t("viewer.lightbox.star_write_failed")
            )
            return
        if star == 0:
            self._title_overlay.show_message(
                t("viewer.lightbox.star_cleared")
            )
        else:
            self._title_overlay.show_message("★" * star)

    def _poke_activity(self) -> None:
        changed = self._hide_engine.activity(time.monotonic())
        if changed or not self._top_bar.isVisible():
            self._set_chrome_visible(True)
        self._hide_timer.trigger(self._chrome_hide_ms)

    def _pointer_over_chrome(self) -> bool:
        """ポインタが表示中のクローム（上部バー / 画像リスト / カプセル）上にあるか。"""
        pos = QCursor.pos()
        return any(
            w.isVisible() and w.rect().contains(w.mapFromGlobal(pos))
            for w in (self._top_bar, self._strip, self._capsule)
        )

    def _on_hide_timeout(self) -> None:
        if not self._hide_engine.visible:
            return  # 既に隠れている（保持の張り直しだけが遅れて着いた）
        # クローム上にカーソルが乗っている間は隠さない（クリック直前に
        # ボタンが消える事故の防止）。表示中のフィルムストリップ / 操作カプセル
        # にホバー中もカーソル / 上部バーを消さない（クリックしにくくなるため）。
        # ただし**ポインタで狙っているとき**（最後の操作がポインタ）だけ —
        # キー / ホイールで送っている間に置いたままのポインタは保持しない。
        if self._pointer_live and self._pointer_over_chrome():
            self._hide_timer.trigger(self._chrome_hide_ms)
            return
        now = time.monotonic()
        if self._hide_engine.should_hide(now):
            self._hide_engine.mark_hidden()
            self._set_chrome_visible(False)
            return
        # タイマーが予定より早く着いた（CoarseTimer の許容ずれ）。捨てると
        # 次の操作までクロームが消えないので、残り時間で張り直す。
        remaining_ms = math.ceil(self._hide_engine.remaining_sec(now) * 1000)
        self._hide_timer.trigger(max(1, remaining_ms))

    def _set_chrome_visible(self, on: bool) -> None:
        # 上部バー・操作カプセル + カーソルを制御する（フィルムストリップは
        # 独立管理 — :meth:`_show_strip` / :meth:`_on_strip_hide_timeout`）。
        self._top_bar.setVisible(on)
        self._capsule.setVisible(on)
        if on:
            self.unsetCursor()
            self._view.viewport().unsetCursor()
        else:
            # ImageView のラベルが独自カーソル（パン用オープンハンド等）を
            # 持つときはそちらが勝つが、フィット表示（通常の閲覧）では
            # ラベル未設定なのでカーソルも消える。
            self.setCursor(Qt.CursorShape.BlankCursor)
            self._view.viewport().setCursor(Qt.CursorShape.BlankCursor)

    def force_show_chrome(self) -> None:
        """Pin the full chrome (top bar / capsule / filmstrip) on screen.

        Screenshot-harness hook (tools/ui_review — the offscreen platform has
        no hover, and the cursor is parked at (0,0), so the auto-hide timers
        would blank everything before ``grab()``).  Mirrors
        ``ContentView.force_show_stage_capsule``: stop both auto-hide timers
        and show every chrome layer so a review shot can capture the lightbox
        the way a user sees it right after moving the mouse.  Not used by
        product code paths.
        """
        self._hide_timer.stop()
        self._strip_hide_timer.stop()
        self._set_chrome_visible(True)
        if len(self._images) > 1:
            self._strip.setVisible(True)
            self._strip.set_current(self._index)

    # ------------------------------------------------- filmstrip auto-hide

    def _cursor_in_strip_zone(self) -> bool:
        """カーソルが下端の「ストリップ召喚帯」内にあるか（ホバー判定）.

        帯 = ストリップの高さ + :data:`_STRIP_REVEAL_MARGIN`。全画面なので
        ウィンドウ座標へ写像して下端からの距離で判定する。
        """
        y = self.mapFromGlobal(QCursor.pos()).y()
        zone = self._strip.height() + _STRIP_REVEAL_MARGIN
        return y >= self.height() - zone

    def _show_strip(self) -> None:
        """下端の画像リストを表示し、自動消灯タイマーを仕切り直す。"""
        if not self._strip.isVisible():
            self._strip.setVisible(True)
            self._strip.raise_()
        self._strip_hide_timer.trigger(self._chrome_hide_ms)

    def _on_strip_hide_timeout(self) -> None:
        if not self._strip.isVisible():
            return
        # カーソルが帯内（ストリップ上 or 直上）にある間は消さない
        # — サムネをクリックしようとしている最中に消える事故を防ぐ。上部バー
        # と同じく、ポインタで狙っているとき（``_pointer_live``）だけ保持する。
        if self._pointer_live and self._cursor_in_strip_zone():
            self._strip_hide_timer.trigger(self._chrome_hide_ms)
            return
        self._strip.setVisible(False)

    # -------------------------------------------------------- strip thumbs

    def _on_strip_thumb_requested(self, path_obj: object) -> None:
        path = path_obj if isinstance(path_obj, Path) else None
        if path is None:
            return
        # 右ペインが既にデコード済みならメモリ常駐 pixmap をそのまま使う
        # （NAS 往復ゼロ）。無いものだけローダーへ（ワーカーは QImage のみ）。
        provider = self._thumb_provider
        if provider is not None:
            try:
                pm = provider(path)
            except Exception:  # pragma: no cover (defensive)
                pm = None
            if pm is not None and not pm.isNull():
                self._strip.set_thumb(path, pm)
                return
        if self._loader is None:
            return
        key = self._strip_prefix + str(path)
        # ローダーが同じキーのデコードを保持しているなら、要求ではなく
        # **そこから再シードする**。``ThumbnailLoader.request`` は「ソースが
        # 箱より小さい」キーの再要求に対して無言で return する契約
        # （呼び出し側が既にその画像を持っている前提の churn 防止）なので、
        # ``set_images`` が ``_pixmaps`` を捨てた後の 2 回目の要求では
        # ``loaded`` が二度と届かず、64px 未満の画像セルが「読み込み中」に
        # 見えるプレースホルダのまま固定される（同じ帯・同じフォルダで
        # 全画面を開き直すたびに再現する）。children_grid / folder_preview_view
        # が持つ再シード（#10）の 3 番目のホスト。
        img = self._loader.cached_image(key)
        if img is not None and not img.isNull():
            self._strip.set_thumb(path, QPixmap.fromImage(img))
            return
        edge = FilmstripView.THUMB_EDGE
        self._loader.request(
            key,
            path,
            QSize(edge, edge),
            dpr=self.devicePixelRatioF(),
        )

    def _on_strip_thumb_loaded(self, key: str, image: QImage) -> None:
        if not key.startswith(self._strip_prefix):
            return
        path = Path(key[len(self._strip_prefix):])
        # QPixmap 変換はメインスレッド（このスロット）でのみ行う。
        self._strip.set_thumb(path, QPixmap.fromImage(image))

    def _on_strip_thumb_failed(self, key: str) -> None:
        """デコード失敗セルを静的グリフで確定させる（項目#128）.

        ``FilmstripView`` の空セルは平坦なプレースホルダ塗りで、失敗しても
        「読み込み中」と区別が付かない。``mark_failed`` で pixmap を常駐させて
        「読み込み中に見え続ける」のを止めつつ失敗として記録し、``reset_failed``
        （ファイル修復後の再試行入口）で戻せる状態にする — ステージ帯の
        ``ViewerWindow._on_stage_thumb_failed`` と同じ扱い。GUI スレッド専用
        （QPixmap 生成のため）。
        """
        if not key.startswith(self._strip_prefix):
            return
        path = Path(key[len(self._strip_prefix):])
        self._strip.mark_failed(path, self._strip_failed_glyph())

    def _strip_failed_glyph(self) -> QPixmap:
        """失敗確定セル用の「壊れた画像」グリフを 1 枚描く（項目#128）.

        色は帯と同じ固定オーバーレイパレット（``common/ui/overlay.py``）から
        取る — ライトボックスの帯は画像コンテンツ上のオーバーレイなので
        テーマ追従ではなく固定色（design.md の例外規定）。平坦な
        プレースホルダ地に中央の「×」を重ね、「読み込み中」と視覚的に区別する。
        """
        edge = FilmstripView.THUMB_EDGE
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, round(edge * dpr)), max(1, round(edge * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        mark = QColor(overlay.OVERLAY_TEXT)
        mark.setAlpha(120)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(0, 0, edge, edge, self._strip._placeholder_color())
        inset = max(3, edge // 4)
        pen = painter.pen()
        pen.setColor(mark)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(inset, inset, edge - inset, edge - inset)
        painter.drawLine(edge - inset, inset, inset, edge - inset)
        painter.end()
        return pm

    # ------------------------------------------------------------ layout

    def _counter_width_budget(self) -> int:
        """常時カウンタが伸びてよい幅 = 下端中央の操作カプセルの手前まで（N-110）.

        カウンタは左下、カプセルは下端中央に置かれるので、親幅いっぱいを許すと
        長いファイル名がカプセルの下へ潜り込む。カプセルは隠れていても
        ``width()`` を持つ（オートハイドは可視だけを切る）ので、可視状態に
        関わらず同じ上限になる。
        """
        capsule_w = self._capsule.width()
        capsule_left = (self.width() - capsule_w) // 2
        return max(1, capsule_left - _CounterOverlay._EDGE_MARGIN * 2)

    def _position_counter(self) -> None:
        """常時カウンタを左下（フィルムストリップ帯の上）に配置する（G02）。"""
        self._counter.set_width_limit(self._counter_width_budget())
        strip_h = self._strip.height()
        ch = self._counter.height()
        self._counter.move_clamped(
            _CounterOverlay._EDGE_MARGIN, self.height() - strip_h - ch - 8
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._top_bar.setGeometry(0, 0, self.width(), LightboxTopBar.HEIGHT)
        strip_h = self._strip.height()
        self._strip.setGeometry(0, self.height() - strip_h, self.width(), strip_h)
        self._title_overlay.reposition()
        self._position_counter()
        # ストリップの高さ分を常に確保して配置する — ストリップの表示/非表示に
        # 関わらず一定の位置になり、一瞬出てもカプセルと重ならない
        # （_position_counter と同じ考え方 — UIレビュー #25）。
        self._capsule.reposition(strip_h)
        # フィット中の実効ズームはビューポート寸法に依存する — %も追従させる。
        self._refresh_capsule_zoom()
        # 初回ヒントはカプセルの**上**に積む（カプセルはストリップ帯の上に
        # 置かれているので、その高さとマージンまで含めて予約量に足す）。
        self._hint.set_bottom_reserved(self._hint_bottom_reserved())
        self._hint.reposition()
        self._top_bar.raise_()
        self._strip.raise_()
        self._title_overlay.raise_()
        self._counter.raise_()
        self._capsule.raise_()
        self._hint.raise_()

    # ----------------------------------------------------------- teardown

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self._stop_slideshow()
        self._invalidate_cross()
        # open/close symmetry (#12): also invalidate any in-flight open scan.
        # ``open_folder`` issues an off-thread ``list_playlist_sorted`` — if
        # the user closes before it lands, ``_on_open_scanned`` would still
        # match the token, take the folder-open branch (``current_image()`` is
        # None) and ``_show_index(0)`` inside the now-hidden window: a full
        # decode + prefetch for a still image, or *audible autoplay with no
        # window* for a video.  Close = hide keeps the window alive, so the
        # session must be torn down here just like ``_begin_open`` does on
        # open (着地の選別は ``bind`` が持つ — ``_invalidate_cross`` の注記)。
        self._open_stream.cancel()
        self._hide_timer.stop()
        self._strip_hide_timer.stop()
        self._set_chrome_visible(True)  # カーソルを必ず復元して閉じる
        if not self._closed_emitted:
            self._closed_emitted = True
            self.closed.emit(self._folder, self.current_image())
        self._view.clear_image()
        # デコード済み原寸 PIL も返す（項目#28）: close = hide でこのウィンドウは
        # ViewerWindow._lightbox に生き続けるため、clear_image（表示中の
        # QPixmap/QImage を落とすだけ）では最大 IMAGEVIEW_CACHE_MAX_BYTES ぶんの
        # 原寸 PIL が非表示ウィンドウのキャッシュに残り、セッション中ずっと
        # 解放されない（中央ペインの ImageView も同じ予算を持つのでピーク常駐が
        # 実質 2 倍になり得る）。投稿横断（_apply_cross）で兄弟が総入れ替えに
        # なるときと同じ入口を使う — 登録 / 解除の対称性。
        self._view.invalidate_sibling_cache()
        if self._media is not None:
            self._media.clear_media()  # 動画再生を停止
        super().closeEvent(event)


__all__ = [
    "PLAYLIST_SUFFIXES",
    "AutoHideEngine",
    "CenterMessageOverlay",
    "EdgeLatch",
    "EmptyPlaylistView",
    "FilmstripView",
    "LightboxControlCapsule",
    "LightboxTopBar",
    "LightboxWindow",
    "MediaResume",
    "_CounterOverlay",
    "_HintOverlay",
    "is_video_path",
    "list_images_sorted",
    "list_playlist_sorted",
    "scan_adjacent_image_folder",
    "scan_adjacent_post",
]
