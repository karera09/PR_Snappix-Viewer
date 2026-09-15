"""ステータスバーの合成 / 走査ライフサイクル / 空状態の裁定 :class:`WindowStatus`.

``ViewerWindow`` から 3 つの近い関心をまとめて切り出した部品:

* **ステータスバー** — 6 セグメントの構築（:meth:`build_status_bar`）と、
  読み込み / 検索 / サムネイル生成の状態ラベル合成（優先順位は
  :meth:`refresh_label`）、件数、プレビュー中画像の解像度。左端の現在パス
  表示と「ファイル名 · サイズ」への**書き込み**は選択追従ファネル側
  （``ViewerWindow._set_path_status`` / ``_update_file_info_label``）に残る;
* **走査ライフサイクル** — 左ペイン走査の立ち上がり / 着地
  （:meth:`on_loading_changed`）と、その着地で消費する 2 つの one-shot
  （起動フォーカス :meth:`settle_startup_focus` / ステージ維持の保険
  :meth:`settle_stage_after_scan`）、部分走査・走査失敗の受け口;
* **席の畳み判定と空状態オーケストレータ** — 3 席（グリッド / プレビュー列 /
  右情報パネル）が畳まれているかの述語、``empty_state.resolve_empty_state``
  への入力収集、返ってきた役割割当を各ペインへ描かせる適用点。

部品はタイマーもスレッドも持たない（すべて同期のラベル更新）。

**状態ラベルの素材と走査の one-shot はこの部品が所有する**（``load_status`` /
``search_status`` / ``thumb_status`` / ``scan_loading`` / ``load_failed`` /
``load_partial`` / ``startup_focus_pending`` / ``stage_settle_pending``）。
このうち**外から名指しされる 6 本だけ**、ウィンドウ側に同名 ``_`` 付きの
**透過プロパティ**を残してある（``_load_status`` / ``_thumb_status`` /
``_scan_loading`` / ``_load_failed`` / ``_startup_focus_pending`` /
``_stage_settle_pending`` — テストと他クラスタがその名前で読み書きするため）。
``search_status`` / ``load_partial`` は窓側に名前を持たない（この部品の中でしか
読み書きしない）ので、``win._search_status = ...`` と書いても誰も読まない
シャドウ属性が生えるだけ — 窓側から触りたくなったらプロパティを足すこと。
値の置き場はいずれもここ 1 か所。席の裁定だけは 3 ペインとスプリッタという構築物
そのものを読むので、引き続きウィンドウ側の属性を直に触る。読み書きする窓側の
属性は以下で全部:

読むもの
    ``_loading_label`` / ``_counts_label`` / ``_resolution_label`` /
    ``_path_label`` / ``_file_info_label`` / ``_cache_build_status``
    （ステータスバーの各セグメント — :meth:`build_status_bar` が組んで
    ウィンドウ属性として公開し、以降は双方が読む）、``_loader`` /
    ``_file_thumb_loader``（サムネ残件の合算）、``_ui_mode`` /
    ``_center_split`` / ``_splitter`` / ``_preview_visible`` / ``_state``
    （席の畳み判定）、``_post_grid`` / ``_content`` / ``_info_panel`` /
    ``_nav_rail`` / ``_root`` / ``_default_library`` / ``_history`` /
    ``_current_folder``（空状態の入力・走査着地の適用先）、
    ``_preview_fallback_pending``（代表画像フォールバック中の 1 回抑止）。

書くもの
    ``_preview_fallback_pending``（上の 1 回抑止の消費）、
    ``_pending_restore_preview``（走査の失敗着地 / ステージ維持の取りやめで
    復元予約を捨てる — 残すと ``_collect_state`` が「復元が飛行中」と判定し
    続ける）、および :meth:`build_status_bar` が組む 6 セグメント。

呼ぶもの
    ``_sync_centre_placeholder`` / ``_update_stage_header`` /
    ``_sync_nav_rail_curation`` / ``_apply_tab_order`` / ``_can_go_up`` /
    ``_settle_startup_focus``（いずれもウィンドウ側の委譲メソッド経由 —
    テストと UI レビューハーネスが差し替える口をそのまま通す）、
    ``_reset_preview_panes`` / ``_enter_browse_mode``（ステージ維持を
    取りやめるときの後始末と態の切替 — どちらも窓の持ち物）、
    ``statusBar()``（Qt の口。6 セグメントの親）、
    ``_on_thumb_pending_changed``（両ローダーの ``pending_changed`` を
    受ける先として :meth:`build_status_bar` が接続する — 受け手を窓側の
    委譲スロットにしておくのは、接続先を ``self`` のメソッドに保つ規約）。

``getattr`` ガードが多いのは ``ViewerWindow.__init__`` を通さないテスト
ハーネス（``ViewerWindow.__new__`` + ``QMainWindow.__init__`` + 必要な属性
だけスタブ）向けで、ウィンドウ側に元々あった規約をそのまま引き継いでいる。
この部品は窓を親に持つ ``QObject`` なので、``QMainWindow.__init__`` まで
通っていない殻からは構築自体ができない（``ViewerWindow._status`` の遅延
生成が shiboken の ``RuntimeError`` になる）。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QTextEdit,
)

from ..common.i18n import t
from .cache_build_status import CacheBuildStatusWidget
from .empty_state import (
    EmptyState,
    EmptyStateInput,
    Role,
    resolve_empty_state,
)
from .gallery_view import GalleryView


class WindowStatus(QObject):
    """``ViewerWindow`` のステータスバー合成 + 席裁定を持つ部品."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        # プレビュー中画像の解像度 (W, H)。(0, 0) は「画像なし」。
        self._image_dims: tuple[int, int] = (0, 0)
        # 直前の裁定がどの席配置だったか（ドラッグ追従の差分検出用）。
        self._seat_collapse_last: tuple[bool, bool, bool] | None = None
        # --- 状態ラベルの素材（窓側に同名の透過プロパティがあるのは
        # ``load_status`` / ``thumb_status`` / ``scan_loading`` /
        # ``load_failed`` の 4 本。残る 2 本はここでしか読み書きしない）
        # The folder-load status is usually idle (loads are near-instant), so
        # the recursive-search status takes over the same label while a
        # "サブフォルダも検索" walk is active and falls back when it clears.
        self.load_status: str = t("common.status.waiting")
        self.search_status: str = ""
        # サムネイル生成の進捗チャネル。ディレクトリスキャンが終わった時点で
        # 「読み込み完了」と出るのに、冷えた NAS ではそこから数分間タイルが
        # 灰色のままだった（まだ待つべきかを答える表示がどこにも無かった）。
        # 両ローダーの pending_changed に載せて合成する。
        self.thumb_status: str = ""
        # ``load_status`` と別に持つのは、サムネ残件のセグメントが「走査が
        # 着地してから」でないとラベルを取ってはいけないため。
        self.scan_loading: bool = False
        # Sticky failure flag: once the current root's scan failed, the loading
        # close-out must not overwrite the failure status.
        self.load_failed: bool = False
        # 同じく sticky な「穴あき」フラグ: 一覧は立ったが分類できなかった子が
        # 居た走査は、閉じるときに「読み込み完了」だけを名乗らない。
        self.load_partial: bool = False
        # --- 走査着地で消費する one-shot 2 本（同上）
        # 最初のスキャン着地で「選択が無ければ先頭タイルを選択 + グリッドへ
        # フォーカス」を一度だけ行う（ウィンドウの ``__init__`` が True を置く）。
        self.startup_focus_pending: bool = False
        # 同一ルートの再スキャンで最大化を維持したまま非同期スキャンを待つ
        # ときのフラグ。着地時に保持対象が消えていたら分割へフォールバック。
        self.stage_settle_pending: bool = False

    # --------------------------------------------------- ステータスバー構築

    def build_status_bar(self) -> None:
        """ステータスバーの 6 セグメントを組む（``_build_ui`` から 1 回）.

        明示の ``setStatusBar(QStatusBar())`` は不要（最初の ``statusBar()``
        呼び出しが同じものを遅延生成する）— 以降の ``statusBar()`` が唯一の
        生成点。各セグメントはウィンドウ属性として公開する（テストと他クラスタ
        がこの名前で参照する）。「ファイル名 · サイズ」の非同期 stat ブリッジ
        （``ViewerWindow._file_info_stream``）はウィンドウ側が持つ。
        """
        win = self._window
        bar = win.statusBar()
        # Selected/previewed item's path.  A read-only, frameless QLineEdit
        # (not a QLabel) so the text can be drag-selected and copied — a
        # selectable QLabel draws a persistent text caret ("|") in the status
        # bar, while a read-only QLineEdit shows no caret yet still supports
        # mouse selection + Ctrl+C.  Styled transparent so it reads as a
        # plain label.  Added on the left via addWidget; temporary messages
        # overlay it and it reappears when they clear.
        win._path_label = QLineEdit("")
        win._path_label.setReadOnly(True)
        win._path_label.setFrame(False)
        win._path_label.setFocusPolicy(Qt.ClickFocus)
        win._path_label.setStyleSheet(
            "QLineEdit { background: transparent; border: none; }"
        )
        bar.addWidget(win._path_label, 1)
        # Background cache-build progress strip.  Sits to the LEFT of the other
        # permanent widgets (added first) and stays hidden until a background
        # build starts.  Pause / cancel clicks are wired by the
        # CacheBuildController (constructed right after _build_ui in __init__).
        win._cache_build_status = CacheBuildStatusWidget()
        bar.addPermanentWidget(win._cache_build_status)
        # Permanent (right-aligned) widgets, in visual order:
        #   [cache-build] · [counts] · [selected file · size] · [W×H] · [load/search status]
        # Counts: folder / file split of whatever the left grid currently shows
        # (direct children, recursive walk, or advanced-search results).
        win._counts_label = QLabel("")
        bar.addPermanentWidget(win._counts_label)
        # Selected file's name + size (populated by _set_path_status; blank for
        # folders / when nothing is selected).
        win._file_info_label = QLabel("")
        bar.addPermanentWidget(win._file_info_label)
        # Previewed image resolution "W×H" (blank when not previewing an image).
        win._resolution_label = QLabel("")
        bar.addPermanentWidget(win._resolution_label)
        win._loading_label = QLabel(t("common.status.waiting"))
        bar.addPermanentWidget(win._loading_label)
        # 状態ラベルの素材（``load_status`` / ``search_status`` /
        # ``thumb_status`` / ``scan_loading`` / ``load_failed`` /
        # ``load_partial``）はこの部品が ``__init__`` で持つ — ここでは組まない。
        win._loader.pending_changed.connect(win._on_thumb_pending_changed)
        win._file_thumb_loader.pending_changed.connect(
            win._on_thumb_pending_changed
        )

    # ------------------------------------------------------- 状態ラベル合成

    def refresh_label(self) -> None:
        # Priority, highest first:
        #   1. 再帰検索の進捗 — an explicitly-started, bounded operation.
        #   2. サムネイル生成中… N 件 — only once the directory scan has
        #      settled, so the folder-named 「読み込み中…」 keeps the floor
        #      while the scan itself is the wait.
        #   3. フォルダ読み込みの状態（待機中 / 読み込み中 / 完了 / 失敗）.
        # スキャン失敗も 3 のまま — 失敗の告知を進捗で覆わない。
        win = self._window
        if self.search_status:
            win._loading_label.setText(self.search_status)
            return
        if self.thumb_status and not self.scan_loading and not self.load_failed:
            win._loading_label.setText(self.thumb_status)
            return
        win._loading_label.setText(self.load_status)

    def on_search_status_changed(self, text: str) -> None:
        self.search_status = text
        self.refresh_label()

    def on_thumb_pending_changed(self, _count: int) -> None:
        """Recompute the 「サムネイル生成中… N 件」 segment.

        The payload is one loader's own count; the label reports the **sum**
        across the grid and file-list loaders (both feed this slot), so the
        message tracks all the tiles the user can actually see waiting.  It
        clears itself the moment the total reaches 0 — no timer, no polling.
        """
        win = self._window
        total = (
            win._loader.pending_count()
            + win._file_thumb_loader.pending_count()
        )
        self.thumb_status = (
            t("viewer.main_window.thumbs_pending", n=total) if total else ""
        )
        self.refresh_label()

    def on_scan_partial(self, unclassified: int) -> None:
        """左ペインの走査に穴が空いた: 一覧は出るが「完了」とは言わない。

        走査失敗 (:meth:`on_scan_failed`) と違い結果は有効なので、グリッドは
        そのまま並び、ステータスだけが穴を名乗る。
        """
        del unclassified  # 件数はログにある（ステータスは一定の文言）
        self.load_partial = True
        self.refresh_label()

    def on_scan_failed(self, message: str) -> None:
        """Left-pane scan failure: status shows failure, not 完了."""
        del message  # detail is in the log; the grid shows the error card
        self.load_failed = True
        self.load_status = t("viewer.main_window.load_failed")
        self.refresh_label()

    # --------------------------------------------------- 走査ライフサイクル

    def settle_startup_focus(self) -> None:
        """初回スキャン着地の一手（UIレビュー 07-25 #4-①）— one-shot。

        * 選択が無ければ**先頭タイルを選択**する（初回起動でプレビュー列 +
          情報パネル = 中央の約 45% がプレースホルダのまま空白になるのを解消。
          復元起動では既に選択が入っているので何もしない）。
        * どちらの場合も**キーボードフォーカスをグリッドへ**移す。既定の初期
          フォーカスはツールバー先頭のナビボタンで、最初の Space/Enter が
          「上の階層へ」を発火してライブラリ外へ出てしまっていた
          （#4-② のフォーカスポリシーと合わせて根治）。

        ユーザーが着地前に**自分でフォーカスを移していた**場合は奪わない
        （入力中の横取りは事故）。判定は「初期フォーカスの居場所（ツール
        バーのボタン）」と「ユーザーが意図して置いた操作対象」の線引きで、
        入力欄だけでなくリスト / グリッド / スライダ / コンボも後者に含める
        — ナビレールを ↑↓ で歩いている最中の着地でも横取りしない。先頭
        タイルの選択は表示内容の話（フォーカスを動かさない）なので、この
        ガードとは独立に行う。
        """
        if not self.startup_focus_pending:
            return
        self.startup_focus_pending = False
        win = self._window
        if win._post_grid.current_path() is None:
            win._post_grid.select_first()
        focus = QApplication.focusWidget()
        if focus is not None and focus is not win._post_grid._view and isinstance(
            focus,
            (
                QLineEdit,
                QAbstractSpinBox,
                QAbstractItemView,
                QAbstractSlider,
                QComboBox,
                QTextEdit,
                GalleryView,
            ),
        ):
            return
        win._post_grid.focus_grid()

    def on_loading_changed(self, loading: bool) -> None:
        win = self._window
        # Tracked separately from ``load_status`` because the thumbnail
        # segment (#75) must only take the label once the scan has settled.
        self.scan_loading = loading
        if loading:
            self.load_failed = False
            self.load_partial = False
        if self.load_failed and not loading:
            # A scan failure already set the status (I01) — don't let the
            # loading close-out overwrite it with 「読み込み完了」.
            #
            # 初回スキャンが失敗した場合もここで one-shot を消費する: 残した
            # ままだと、後で別フォルダを開いて着地したときに「起動直後の一手」
            # がフォーカスを奪いに来る（そのころユーザーは既にどこかを操作して
            # いる）。フォーカス移動は行わず、フラグだけ落とす。
            self.startup_focus_pending = False
            # 復元予約も同じ理由で**失敗着地で**消費する: 消さないと
            # ``_collect_state`` が「復元がまだ飛行中」と判定し続け、以後の
            # オートセーブと closeEvent がこのセッションの選択 / プレビュー /
            # スクロールを 1 度も保存しなくなる（次回起動の復元がセッション
            # 開始時点へ巻き戻る）。着地しなかった予約は次の着地まで持ち越さ
            # ない — ``startup_focus_pending`` と同じ規律。
            win._pending_restore_preview.clear()
            # ステージ維持の保険も**失敗着地で**消費する（レビュー 2026-09-03
            # 項目 #78）。失敗時こそグリッドはエラーカード + [再試行] を出して
            # いるのに、最大化中はグリッド席が幅 0 でそれが 1px も見えない —
            # ここで分割へ落とさないと行き止まりになる（保険も True のまま
            # 次の別ルート set_root まで残る）。
            self.settle_stage_after_scan()
            return
        if loading:
            # I06(2): name the folder being scanned so a long cold-NAS scan
            # reads as progress ("読み込み中… (フォルダ名)"), not a freeze.  The
            # metadata-count variant was deferred to keep post_grid untouched.
            name = win._root.name if win._root is not None else ""
            self.load_status = (
                t("common.status.loading_name", name=name)
                if name
                else t("common.status.loading")
            )
        else:
            self.load_status = t("viewer.main_window.load_complete")
            if self.load_partial:
                # 穴を黙ると「読み込み完了」が全件の顔をする — 再帰検索の
                # ``walk_incomplete`` / 最近追加一覧の ``incomplete`` と同じ
                # 「結果は見せる、穴は言う」規律。
                self.load_status = " / ".join([
                    self.load_status,
                    t("viewer.main_window.load_partial"),
                ])
        self.refresh_label()
        # Scan finished → the current root's direct-child posts are now in the
        # postref index.  Re-classify the open post's body links so a sibling
        # post that landed in the index during this scan becomes clickable.
        if not loading:
            win._content.refresh_markdown_links()
            # 委譲メソッド経由で呼ぶ — UI レビューのスクリーンショット
            # ハーネス (``tools/ui_review/shoot.py``) が窓のこの名前を
            # 差し替えて「初回着地の 1 枚」を撮る。
            win._settle_startup_focus()
            self.settle_stage_after_scan()

    def settle_stage_after_scan(self) -> None:
        """再スキャン着地でステージ維持の保険を消費する（項目9 / 項目 #78）。

        ``set_root(keep_mode=True)`` は最大化を維持したまま非同期スキャンを
        投げ、``stage_settle_pending`` を立てる。着地時点で保持対象が消えて
        いたら（投稿/ファイルの削除、スキャン失敗）分割へ戻す — 最大化中は
        グリッド席が幅 0 なので、そこに出た空状態やエラーカード + [再試行] が
        1px も見えないまま行き止まりになるため。**スキャン失敗も「保持対象が
        消えた」の一種**として同じ出口を通す（レビュー 2026-09-03 項目 #78 —
        以前は ``load_failed`` の早期 return がこの処理の手前で抜けていた）。

        旧表示の後始末は ``ViewerWindow._reset_preview_panes``（「再ルートは
        選択を捨てる」規約の唯一の実体）へ委ねる — 手写しすると
        ``invalidate_image_sibling_cache`` / ``_refresh_info_meta(None)`` /
        ``_refresh_file_detail(None)`` が落ち、消えた投稿のメタカードと
        「本文を読む」導線が残ったままになる（レビュー 2026-09-03 項目 #50）。
        """
        if not self.stage_settle_pending:
            return
        self.stage_settle_pending = False
        win = self._window
        # getattr ガードは __init__ を通さないテストハーネス向け（``set_root``
        # 系の既存規約と同じ）。
        if getattr(win, "_ui_mode", "browse") != "stage":
            return
        if not self.load_failed and win._post_grid.current_path() is not None:
            return  # 保持対象は健在 — ステージのまま。
        # ステージ維持のため保っていた旧表示（中央プレビュー / 右ペイン /
        # 現在フォルダ / 情報パネルのメタカードとファイル詳細）も、通常の
        # set_root と同じ空白状態へここで落とす（妥当な劣化 — 保持対象消失
        # 時のみ）。
        win._reset_preview_panes()
        # pending-restore は「維持」側の一時状態なので、維持をやめる以上ここで
        # 捨てる（``_reset_preview_panes`` は起動時復元 B01 の種を消さないよう
        # 意図的にこれを含まない）。
        win._pending_restore_preview.clear()
        win._enter_browse_mode()

    # --------------------------------------------------------- 件数・解像度

    def on_counts_changed(self, folders: int, files: int) -> None:
        # 同じ「ファイル」件数が同一画面で 0 と 8 に割れて見える（ここ = 現
        # フォルダの内訳 / 情報パネル = 選択投稿の中身）。文言に「表示中:」の
        # 限定語を付けて、どちらの母数かを明示する。
        win = self._window
        win._counts_label.setText(
            t("viewer.main_window.counts", folders=folders, files=files)
        )
        win._sync_centre_placeholder()
        # グリッドの母集団が変わった = 送りボタンの可否とレールの横断ビュー
        # 表示の両方が変わりうる。
        win._update_stage_header()
        # 件数は ``_user_meta_map`` 由来で、この経路（絞り込み 1 打鍵ごと・
        # 走査の進捗ごと）では変わらない — 現在地だけ書き直す。
        win._sync_nav_rail_curation(counts=False)
        # 右パネル・プレビュー列の空状態文言は ``_sync_centre_placeholder``
        # （空状態オーケストレータ）が 1 か所で裁定する。
        # パンくずは移動のたびにセグメントボタンを作り直すため、視覚順の
        # Tab 巡回をここで貼り直す。ただしこの経路は絞り込み 1 文字ごとにも
        # 走り、貼り直しは 3 ペイン分の再帰 findChildren を伴う — パンくず /
        # ルートが実際に変わったときだけ歩かせる。
        if getattr(win, "_nav_rail", None) is not None:
            win._apply_tab_order(only_if_changed=True)

    def on_image_info_changed(self, width: int, height: int) -> None:
        # (0, 0) means "no image" (non-image page / load failure) → clear.
        self._image_dims = (width, height)
        win = self._window
        if width <= 0 and getattr(win, "_preview_fallback_pending", False):
            # 代表画像のフォールバック探索を始めた**直後の 1 回**だけ
            # 「画像なし」を握り潰す。これは次候補を試す前の過渡状態でしか
            # なく、ここでラベルを消すと候補ごとに W×H が明滅する。1 回で
            # 消費するので、以降の（ページ切替や明示の）クリアは従来どおり
            # 効く。
            win._preview_fallback_pending = False
            return
        self.update_resolution_label()

    def update_resolution_label(self) -> None:
        """プレビュー中画像の解像度 W×H を表示する（常時 — 分割ビュー化で
        プレビューは常に見えているため、ブラウズ中の抑止は撤去済み）."""
        width, height = self._image_dims
        if width > 0 and height > 0:
            self._window._resolution_label.setText(f"{width}×{height}")
        else:
            self._window._resolution_label.setText("")

    # --------------------------------------------------------- 席の畳み判定

    def is_grid_seat_collapsed(self) -> bool:
        """グリッド席が畳まれている（= 中央が唯一の面）か.

        プレビュー最大化（``_ui_mode == "stage"``）と、ハンドルを手で 0 まで
        引いた状態の両方を 1 つの述語にまとめる。``getattr`` ガードは
        ``__init__`` を通さないテストハーネス向け（他の同種判定と同じ）。
        """
        win = self._window
        if getattr(win, "_ui_mode", "browse") == "stage":
            return True
        split = getattr(win, "_center_split", None)
        if split is None:
            return False
        sizes = split.sizes()
        return bool(sizes) and sizes[0] <= 0

    def is_preview_seat_collapsed(self) -> bool:
        """プレビュー列が畳まれている（F6 OFF / ハンドルを 0 まで引いた）か。

        グリッド側 (:meth:`is_grid_seat_collapsed`) の対。空状態オーケストレータ
        が「見えない席に主案内を置かない」判定に使う。``getattr`` ガードは
        ``__init__`` を通さないテストハーネス向け（他の同種判定と同じ）。
        """
        win = self._window
        if not getattr(win, "_preview_visible", True):
            return True
        split = getattr(win, "_center_split", None)
        if split is None:
            return False
        sizes = split.sizes()
        return len(sizes) > 1 and sizes[1] <= 0

    def is_info_seat_collapsed(self) -> bool:
        """右情報パネルの席が畳まれている（F8 OFF / ハンドルを 0 まで引いた）か。

        中央 2 席 (:meth:`is_grid_seat_collapsed` /
        :meth:`is_preview_seat_collapsed`) の対。永続フラグ
        ``_state.info_panel_visible`` だけを見ていると、ハンドルドラッグで
        幅 0 まで畳んだ席（``_on_outer_split_moved`` が「トグル OFF と同一視」
        する状態）へ空状態オーケストレータが役割を割り当ててしまう。
        **読み側だけ**を直すこと — 永続フラグは ``_collect_state`` が保存時に
        同じ「幅 0 = OFF」規約で計算し直す設計。
        """
        win = self._window
        state = getattr(win, "_state", None)  # __init__ を通さないハーネス
        if not bool(getattr(state, "info_panel_visible", True)):
            return True
        split = getattr(win, "_splitter", None)
        if split is None:
            return False
        sizes = split.sizes()
        return len(sizes) > 2 and sizes[2] <= 0

    def seat_collapse_state(self) -> tuple[bool, bool, bool]:
        """3 席の「畳まれているか」— 空状態オーケストレータの席側の入力."""
        return (
            self.is_grid_seat_collapsed(),
            self.is_preview_seat_collapsed(),
            self.is_info_seat_collapsed(),
        )

    def resync_placeholder_on_seat_change(self) -> None:
        """席の畳み状態が前回の裁定から変わっていたら裁定し直す.

        スプリッタのハンドルドラッグは**ドラッグ中の全サンプル**で飛ぶので、
        トグル 3 本のように無条件で :meth:`sync_centre_placeholder` を呼ぶと
        1 ジェスチャで数十回回ることになる。席が 0 を跨いだサンプルだけを
        通す（畳んだ席は主案内を持てない = 裁定の入力が変わる）。
        """
        if self.seat_collapse_state() != self._seat_collapse_last:
            self._window._sync_centre_placeholder()

    # ------------------------------------------------- 空状態オーケストレータ

    def empty_state_input(self) -> EmptyStateInput:
        """3 ペインの空状態を裁定するための観測値を集める（純関数への入力）.

        ここは**観測だけ**を行い、判断は :func:`empty_state.resolve_empty_state`
        が持つ。``getattr`` / 型ガードは ``__init__`` を通さないテスト
        ハーネス向け（他の同種経路と同じ流儀）。
        """
        win = self._window
        grid = win._post_grid
        kind = ""
        kind_fn = getattr(grid, "empty_state_kind", None)
        if callable(kind_fn):
            value = kind_fn()
            if isinstance(value, str):
                kind = value
        error_fn = getattr(grid, "has_scan_error", None)
        panel = getattr(win, "_info_panel", None)
        return EmptyStateInput(
            library_empty=bool(grid.is_library_empty()),
            grid_kind=kind,
            has_selection=getattr(win, "_current_folder", None) is not None,
            preview_is_placeholder=bool(win._content.showing_placeholder()),
            grid_seat_collapsed=self.is_grid_seat_collapsed(),
            preview_seat_collapsed=self.is_preview_seat_collapsed(),
            info_panel_visible=(
                panel is not None and not self.is_info_seat_collapsed()
            ),
            has_history=bool(win._history),
            scan_error=error_fn() is True if callable(error_fn) else False,
        )

    def sync_centre_placeholder(self) -> None:
        """空状態オーケストレータ — 3 ペインの空状態を 1 か所で裁定する.

        空状態の判定は従来 3 ウィジェットに分散していて互いを見られず、
        **アクション**（ボタン）だけを 1 枚に絞ったため**メッセージの重複と
        文体の不揃い**が残っていた。ここは観測値を集めて純関数
        :func:`empty_state.resolve_empty_state` に渡し、返ってきた役割割当を
        各ペインへ**描かせるだけ**にする — 判定は 1 か所、各ペインは判定しない。

        リゾルバの不変条件（``tests/test_viewer_empty_state_resolver.py`` が
        組み合わせで固定）:

        * ``PRIMARY``（見出し + ボタンを持つ案内カード）は多くとも 1 席。
          3 席すべてがここで消費される（``plan.grid`` はグリッドの
          ``refresh_empty_state(allowed=...)`` へ）ので、不変条件は適用点でも
          成立する。
        * 畳まれた席は ``PRIMARY`` になれない。よって最大化中はグリッドでなく
          中央が主案内を持ち、行き止まりの代わりに [◧ 分割ビューに戻す (G)]
          を出す。
        * 従属席は「アイコン無し 1 行」で、選べる物が無い局面では命令形を
          使わない。
        * 選択中は右情報パネルへ書き込まない — ペイン自身の空状態
          （スキャン失敗 / 「このフォルダにはファイルがありません」）が正しい。

        実プレビューを踏み潰さない従来のガードは ``preview_is_placeholder``
        入力として残っている（プレビュー席が ``NONE`` になるので触らない）。
        """
        win = self._window
        # ドラッグ追従（:meth:`resync_placeholder_on_seat_change`）が「前回の
        # 裁定はどの席配置だったか」を見るための記録。裁定はここ 1 か所なので
        # 記録もここで取る。
        self._seat_collapse_last = self.seat_collapse_state()
        plan = resolve_empty_state(self.empty_state_input())

        # --- グリッド席: 11 種の分類はグリッドが持ったまま。ウィンドウが
        # 教えるのは「これは初回起動か」だけ（履歴・既定ライブラリはここしか
        # 知らない）。
        at_default_library = (
            getattr(win, "_root", None) is not None
            and win._root == getattr(win, "_default_library", None)
        )
        set_first_run = getattr(win._post_grid, "set_first_run", None)
        if callable(set_first_run):
            set_first_run(
                plan.state is EmptyState.FIRST_RUN,
                default_library=at_default_library,
            )
        # 席の裁定そのものもグリッドへ渡す。``plan.grid`` を渡すことで
        # 「``PRIMARY`` は多くとも 1 席」が**適用点で**保証される — 12 番目の
        # 「カードを描く分類」が足されても案内カードは 2 枚にならない。走査
        # 失敗カードはグリッド側で裁定より優先される。
        refresh_empty = getattr(win._post_grid, "refresh_empty_state", None)
        if callable(refresh_empty):
            refresh_empty(allowed=plan.grid.role is Role.PRIMARY)

        # --- プレビュー列
        if plan.preview.role is Role.PRIMARY:
            if plan.state is EmptyState.FIRST_RUN:
                win._content.show_welcome(default_library=at_default_library)
            elif plan.state is EmptyState.EMPTY_FOLDER:
                # カードの [上の階層へ] も ↑ ボタンと同じ可否で出す — 境界
                # （登録ライブラリ直下）では ``_on_go_up`` が黙って return
                # するので死にボタンになる。
                win._content.show_empty_folder(can_go_up=win._can_go_up())
            elif self.is_grid_seat_collapsed():
                # グリッドが幅 0 の間、主案内をグリッドへ向けると「見えない
                # 面から選べ」という行き止まりになる。走査失敗がここへ退避
                # してきたときは、そう名乗らせる（⚠ カードは幅 0 のグリッド席
                # に居るので、この面が唯一の告知になる）。
                win._content.show_empty_maximized(
                    scan_error=plan.state is EmptyState.SCAN_ERROR,
                )
            else:
                win._content.show_empty()
        elif plan.preview.role is Role.SECONDARY:
            win._content.show_empty_secondary(t(plan.preview.message_key))

        # --- 右情報パネル（従属面固定 — ``PRIMARY`` にはならない）
        panel = getattr(win, "_info_panel", None)
        if panel is not None:
            panel.apply_empty_guidance(
                t(plan.info.message_key)
                if plan.info.role is not Role.NONE
                else None
            )


__all__ = ["WindowStatus"]
