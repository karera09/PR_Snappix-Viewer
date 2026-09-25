"""Modeless detail window for the currently selected image.

Shows, for whatever file the user last selected/previewed:

* basic filesystem info (name / type / size / on-disk dimensions when readable),
* the tagger's scanned record from ``tags.db`` (dominant rating + per-rating
  scores, model / floor / scan time), and
* every stored AI tag with its score, in a sortable, filterable table.

It's a read-only consumer of the AI plugin's ``TagIndex``
(``plugins/snappix_ai/engine/tag_db.py`` — injected duck-typed, never
imported: the engine lives in the paid plugin) — it never writes and degrades
gracefully: no ``tags.db`` / no plugin → only filesystem info; a file that was
never scanned → filesystem info plus a "no tag data" notice.

Modeless (like :class:`~snappix.viewer.perf_dialog.PerfDialog`) so the user can
keep navigating the grid and watch the panel follow the selection.  The owning
:class:`~snappix.viewer.main_window.ViewerWindow` pushes each new selection in
via :meth:`show_path`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from loguru import logger
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.format import format_bytes
from ..common.i18n import t
from ..common.ui import (
    FONT_CAPTION_PT,
    ElidedLabel,
    align_header,
    demote_close_default,
    hint_style,
    localize_buttons,
)
from .folder_scan import IMAGE_SUFFIXES
from .image_metadata import ImageMetadata, extract_image_metadata
from .qimage_decode import enable_clear_button, read_image_size
from ._runnable import GuardedStream
from .context_menus import copy_path_to_clipboard

# tags.db category codes (mirrors tagger/engine.py CAT_*; re-declared
# here so the viewer stays decoupled from the tagger tree).  Values are i18n
# keys resolved via ``t()`` at the point of display (the integer codes are
# data and must never change).
_CAT_LABELS = {0: "viewer.detail_window.cat_general", 9: "viewer.detail_window.rating"}

# Image extensions whose dimensions we attempt to read off disk when the file
# isn't in tags.db (so the resolution row is still useful for unscanned images).
#
# 正本は ``folder_scan.IMAGE_SUFFIXES`` 1 つ（``content_view`` /
# ``children_grid`` / ``file_list`` / ``advanced_search`` / ``cache_builder``
# も同じ集合を使う）。ここだけローカルに ``.tiff`` / ``.tif`` / ``.jxl`` を
# 足していたため、その 3 種を選ぶと詳細窓だけが「画像」とみなして
# 「まだスキャンされていません」の案内と（ベクトル索引があれば）類似検索
# ボタンを出す一方、中央プレビューはファイル情報へ落ち、タガーもスキャン
# しないので、どちらの導線も成立しなかった。増やすなら folder_scan 側へ
# 1 箇所足して全席で揃える。
_IMAGE_SUFFIXES = IMAGE_SUFFIXES

# 埋め込みメタデータ節（PNG テキストチャンク）のスクロール領域の高さ上限。
# チャンク 1 件ぶんの箱 (140px 上限) + 見出し行が
# 収まり、2 件目が見えて「続きがある」と分かる程度。これを超えた分は窓では
# なくスクロールバーが受け持つ。
_META_BOX_MAX_H = 260

# 同じ節の高さ**下限**。上限だけがあって下限が
# 無かったため、縦の余りをタグ表側がストレッチで総取りし、250 字級の SD
# プロンプトが「1 行分の覗き窓」に潰れていた（M05 の主目的が未達）。
# 140 = `_build_text_chunk` の 1 チャンク箱の上限と同値＝チャンク 1 件が
# そのまま読める高さ。
_META_BOX_MIN_H = 140

# テキストチャンク 1 件ぶんの箱の高さ上限（`_build_text_chunk`）。複数チャンクを
# 積んだときに 1 件が節を専有しないための上限で、`_META_BOX_MIN_H` と同値。
# チャンクが 1 件しか無く節が縦のストレッチを受け取っているときは、この上限が
# 「渡された高さを使えない」原因になるので `_sync_meta_stretch` が外す。
_META_CHUNK_MAX_H = 140

# Qt の「上限なし」番兵（QWIDGETSIZE_MAX）。タグ表が空ページの間はメタ節へ
# 縦のストレッチを渡すので、そのときだけ上限を外す（`_sync_meta_stretch`）。
_UNBOUNDED_H = 16777215


def _human_size(n: int | None) -> str:
    """Missing-size guard around the shared byte formatter.

    Formatting itself goes through ``common.format.format_bytes`` so this
    window shows the same "1.5 MiB" the status bar / grid captions show
    (this used to be a divergent local copy printing "1.50 MB").
    """
    if not n or n < 0:
        return "—"
    return format_bytes(n)


def _human_time(ts: float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "—"


def _pct(score: float | None) -> str:
    if score is None:
        return "—"
    return f"{float(score) * 100:.1f}%"


class _ScoreItem(QTableWidgetItem):
    """Table item that sorts numerically by an attached float, not by text."""

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # noqa: D401
        if isinstance(other, _ScoreItem):
            return self._value < other._value
        return super().__lt__(other)


def _read_fs_info(path: Path) -> tuple[str, str, str]:
    """Read filesystem info (kind / size / on-disk dimensions) off the GUI thread.

    ``DetailWindow`` follows the grid selection modeless, so every keystroke
    that changes the selection would otherwise run ``path.stat()`` +
    ``path.is_dir()`` + a header-only image decode (``read_image_size``,
    which opens the file) synchronously on the GUI thread.  On cold NAS a
    header read is 10–50 ms+, so holding an arrow key stuttered the whole
    UI.  This mirrors the async pattern the text / file-info views use.
    """
    suffix = path.suffix.lower().lstrip(".")
    is_dir = False
    try:
        is_dir = path.is_dir()
    except OSError:
        pass
    kind = (
        t("common.label.folder")
        if is_dir
        else (suffix.upper() if suffix else t("common.label.file"))
    )
    try:
        size_text = _human_size(path.stat().st_size)
    except OSError:
        size_text = "—"
    dims_text = "—"
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        wh = read_image_size(path)
        if wh is not None:
            dims_text = f"{wh[0]} × {wh[1]}"
    return (kind, size_text, dims_text)


def _read_tag_detail(path: Path, tag_index) -> dict | None:
    """Read one image's tags.db row off the GUI thread.

    ``TagIndex.image_detail`` は完全一致で見つからないと
    ``WHERE LOWER(path) = LOWER(?)`` のフォールバックへ落ち、``LOWER(path)``
    の式索引が無いので **images 全表スキャン**になる（20 万行で実測 41.6ms）。
    しかもそのフォールバックは「tags.db に無いものを選んだとき」= 未スキャン
    フォルダ・テキスト・ZIP といった日常的な選択のたびに必ず通る。この窓は
    選択追従なので、:func:`_read_fs_info` / ``extract_image_metadata`` と同じく
    専用 1 スレッドのストリームへ逃がさないと、↓キーを押し続けるだけで GUI が
    数十 ms 単位で刻まれる（:func:`_read_fs_info` の docstring が言う「holding
    an arrow key stuttered the whole UI」と同じ経路の片側欠落だった）。
    """
    try:
        return tag_index.image_detail(path)
    except Exception as exc:  # pragma: no cover (defensive)
        logger.debug("image_detail failed for {}: {}", path, exc)
        return None


class DetailWindow(QDialog):
    """Modeless window showing the selected image's info + AI tags."""

    #: One or more tags from the table's "このタグで検索" context menu (C-10).
    #: Emitted once per selected tag; the window wires it to the left pane's
    #: ``PostGrid.add_search_tag``.  Only reachable when a tags.db is present.
    tag_search_requested = Signal(str)

    #: The current image path, for a similar-image search seeded from this
    #: window (item E15).  Wired to the left pane's ``PostGrid.set_similar_seed``.
    #: Only offered when semantic vectors are available (``similar_available``).
    similar_search_requested = Signal(object)  # Path

    def __init__(
        self, tag_index, parent=None,
        *, similar_available: bool = False, user_meta=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("viewer.detail_window.title"))
        self.setModal(False)
        self._tag_index = tag_index
        # ユーザーキュレーション（★ / あとで見る / ユーザータグ）の読み取り元。
        # user_meta.db は本体所有なので AI パック無効（素の配布）でも表示できる。
        # ``None`` なら 3 行とも出さない。
        self._user_meta = user_meta
        # AI 機能パック（有償プラグイン）の可用性。False = 素の配布では
        # tags.db 由来の行・AI タグ表・案内文を UI に出さない（構築時に確定、
        # セッション中は不変）。
        from . import ai_pack

        self._ai_ui = ai_pack.available()
        # 素の配布はタグ表が無く内容が半分程度 — 既定サイズも詰めて
        # 情報グループ下の大きな余白を残さない。
        # 460px でもなお内容量（情報グループ + ボタン行）に対して下 2/3 が
        # 空白だったので内容相当まで詰める。
        # adjustSize は選択追従（メタデータ節の出入り）で高さが跳ねるため不可。
        self.resize(420, 720 if self._ai_ui else 260)
        # Whether semantic (vector) search is usable — gates the 「この画像で
        # 類似検索」 button (item E15).  A no-op seed without vectors is confusing,
        # so the button is hidden entirely when False.
        self._similar_available = bool(similar_available)
        self._path: Path | None = None
        self._all_tags: list[tuple[str, float, int]] = []
        # 選択追従の読みは窓所有の :class:`GuardedStream`（既定の
        # 1 スレッド専用プール）へ。共有グローバルプールだと高速選択移動で
        # 古いタスクが無制限に滞留して共有枠を食い、窓を閉じても捨てられない。
        # 専用プールなら同時実行は常に 1 本で、``submit`` が未着手キューを
        # 本当に捨て、親付きなので窓破棄時に Qt が回収する。
        #
        # **用途ごとに 1 本**（3 本）であること: 追い越し（superseding）は
        # ストリーム単位なので、1 本に相乗りさせると FS 読みの投入が
        # メタ読み / tags.db 読みのキューを道連れに捨ててしまう。3 本とも
        # ``show_path`` が選択の変わり目で揃って捨てる（用途間で捨て合わない）。
        self._fs_stream = GuardedStream(self)
        self._fs_stream.bind(self._on_fs_info)
        # Embedded metadata (PNG generation info / EXIF — M05).
        self._meta_stream = GuardedStream(self)
        self._meta_stream.bind(self._on_metadata)
        # tags.db 参照。行が無い / 読めない着地は ``None`` なので
        # ``bind_failed`` が受け、FS 情報だけの表示へ落とす。
        self._tag_stream = GuardedStream(self)
        self._tag_stream.bind(self._on_tag_detail)
        self._tag_stream.bind_failed(self._on_tag_detail_missing)
        self._build_ui()
        self._show_placeholder()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # Header: file name (bold) + full path (selectable, frameless).
        self._name_label = QLabel("")
        self._name_label.setStyleSheet("font-weight: bold;")
        self._name_label.setWordWrap(True)
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 幅の要求には参加させない（下のパス行と同じ Ignored + 最小幅 0）:
        # wordWrap でも折り返し位置の無い名前（英数字と _ だけ）は
        # minimumSizeHint が文字列全幅になり、QDialog がそれを窓の最小幅として
        # 強制するので窓が画面外まで広がり縮められなかった。折り返せる名前は
        # 従来どおり折り返し、折れない分は右で切れる — 全文はツールチップ。
        self._name_label.setMinimumWidth(0)
        self._name_label.setSizePolicy(
            QSizePolicy.Ignored, self._name_label.sizePolicy().verticalPolicy()
        )
        outer.addWidget(self._name_label)

        # パスは共有部品で中央省略（全文はツールチップ）。選択の代わりに
        # 情報パネルと同じ「フルパスをコピー」を右クリックに置く。
        self._path_label = ElidedLabel()
        self._path_label.setStyleSheet(hint_style())
        self._path_label.setContextMenuPolicy(Qt.ActionsContextMenu)
        copy_path = QAction(t("viewer.context_menus.copy_full_path"), self._path_label)
        copy_path.triggered.connect(
            lambda: self._path and copy_path_to_clipboard(self._path, self)
        )
        self._path_label.addAction(copy_path)
        outer.addWidget(self._path_label)

        # File / scan info grid.
        info_box = QGroupBox(t("viewer.detail_window.info_group"))
        self._info_form = QFormLayout(info_box)
        self._info_form.setLabelAlignment(Qt.AlignRight)
        self._lbl_kind = QLabel("—")
        self._lbl_size = QLabel("—")
        self._lbl_dims = QLabel("—")
        # ユーザーキュレーション 3 行 — 値のある行だけ
        # 出す（情報パネルの詳細カードと同じ流儀）。
        self._lbl_star = QLabel("—")
        self._lbl_later = QLabel("—")
        self._lbl_user_tags = QLabel("—")
        self._lbl_rating = QLabel("—")
        self._lbl_model = QLabel("—")
        self._lbl_floor = QLabel("—")
        self._lbl_scanned = QLabel("—")
        self._lbl_ntags = QLabel("—")
        for lbl in (
            self._lbl_kind, self._lbl_size, self._lbl_dims,
            self._lbl_star, self._lbl_later, self._lbl_user_tags,
            self._lbl_rating,
            self._lbl_model, self._lbl_floor, self._lbl_scanned, self._lbl_ntags,
        ):
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # ユーザータグは任意長なので折り返す（横に伸びてダイアログを広げない）。
        self._lbl_user_tags.setWordWrap(True)
        self._info_form.addRow(t("common.label.type"), self._lbl_kind)
        self._info_form.addRow(t("common.label.size"), self._lbl_size)
        self._info_form.addRow(t("viewer.detail_window.resolution"), self._lbl_dims)
        self._info_form.addRow(t("viewer.post_grid.star_menu"), self._lbl_star)
        self._info_form.addRow(t("viewer.post_grid.watch_later"), self._lbl_later)
        self._info_form.addRow(
            t("viewer.detail_window.user_tags"), self._lbl_user_tags
        )
        self._info_form.addRow(t("viewer.detail_window.rating"), self._lbl_rating)
        self._info_form.addRow(t("viewer.detail_window.model"), self._lbl_model)
        # 「記録しきい値」+ 意味を補うツールチップ。ラベル側にも同じツールチップを付ける。
        floor_label = QLabel(t("viewer.detail_window.floor"))
        floor_label.setToolTip(t("viewer.detail_window.floor_tooltip"))
        self._lbl_floor.setToolTip(t("viewer.detail_window.floor_tooltip"))
        self._info_form.addRow(floor_label, self._lbl_floor)
        self._info_form.addRow(t("viewer.detail_window.scanned_at"), self._lbl_scanned)
        self._info_form.addRow(t("viewer.detail_window.tag_count"), self._lbl_ntags)
        # キュレーション行は値が入ったときだけ出す（初期は非表示）。
        for lbl in (self._lbl_star, self._lbl_later, self._lbl_user_tags):
            self._info_form.setRowVisible(lbl, False)
        # AI 由来の 5 行も同じ流儀で「値があるときだけ」出す（初期は非表示）。
        self._set_ai_rows_visible(False)
        outer.addWidget(info_box)

        # Embedded metadata (PNG generation info / EXIF — M05).  Hidden until a
        # selection actually carries metadata; the inner widgets are rebuilt on
        # each result (``_populate_metadata``).
        self._meta_box = QGroupBox(t("viewer.detail_window.metadata_group"))
        meta_outer = QVBoxLayout(self._meta_box)
        meta_outer.setContentsMargins(0, 0, 0, 0)
        # チャンク数ぶん縦に積む節なので、中身はスクロール領域へ入れる。
        # `_read_png_text` は img.info の文字列値を
        # 全部拾う契約なので、NovelAI 産 PNG では 6 チャンク・ComfyUI では 2
        # と可変で、素の QVBoxLayout に積むと窓の minimumSizeHint が
        # チャンク数に比例して伸びる（QDialog はレイアウト由来の最小サイズを
        # 強制するので 1000px 超になると窓を縮められず、1366x768 では下端の
        # ボタン列が画面外に出て押せなくなっていた）。スクロール領域の最小高は
        # 中身に依存しないので、この節がいくつチャンクを持っても窓は自由に
        # 縮められる。設定ダイアログのタブが同じ理由で採っている型
        # (`settings_dialog._scrollable`) に揃える。
        self._meta_scroll = QScrollArea()
        self._meta_scroll.setWidgetResizable(True)
        self._meta_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._meta_scroll.setFrameShape(QFrame.NoFrame)
        self._meta_scroll.setMaximumHeight(_META_BOX_MAX_H)
        # 上限だけあって下限が無い＝対の欠落だった。スクロール領域の
        # sizeAdjustPolicy は既定の AdjustIgnored なので、最小高を与えても
        # 中身のチャンク数で窓の minimumSizeHint が動くことはない。
        self._meta_scroll.setMinimumHeight(_META_BOX_MIN_H)
        meta_inner = QWidget()
        self._meta_box_layout = QVBoxLayout(meta_inner)
        self._meta_box_layout.setContentsMargins(8, 8, 8, 8)
        # 現在積んでいるテキストチャンクの箱と末尾ストレッチ。
        # `_clear_metadata` と必ず同じ場所で捨てる（破棄済みウィジェットへ
        # `setMaximumHeight` すると RuntimeError）。
        self._meta_chunks: list[QPlainTextEdit] = []
        self._meta_tail: QSpacerItem | None = None
        self._meta_scroll.setWidget(meta_inner)
        meta_outer.addWidget(self._meta_scroll)
        self._meta_box.setVisible(False)
        outer.addWidget(self._meta_box)
        # 縦ストレッチの付け替え先（`_sync_meta_stretch`）としてレイアウトを保持。
        self._outer_layout = outer

        # Tag filter + count.
        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(t("viewer.common.filter_tags_placeholder"))
        enable_clear_button(self._filter_edit)
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(hint_style())
        filter_row.addWidget(self._count_label)
        outer.addLayout(filter_row)
        if not self._ai_ui:
            self._filter_edit.setVisible(False)
            self._count_label.setVisible(False)

        # Tag table.
        self._tag_table = QTableWidget()
        self._tag_table.setColumnCount(3)
        self._tag_table.setHorizontalHeaderLabels([
            t("common.label.tag"),
            t("viewer.detail_window.score"),
            t("common.label.type"),
        ])
        # 「見出しの揃え = 内容の揃え」: スコア列は
        # 右寄せ（``_ScoreItem``）、種別列は中央寄せのセルなのに、見出しだけ
        # QTableWidget 既定の中央のままで管理系 6 表と食い違っていた。
        align_header(self._tag_table, right=(1,), center=(2,))
        self._tag_table.verticalHeader().setVisible(False)
        self._tag_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._tag_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._tag_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._tag_table.setSortingEnabled(True)
        # 「選択したタグをコピー」の活性は選択に追随する — フィルタ
        # 再構築と同じ funnel（``_sync_copy_buttons``）へ流す。
        self._tag_table.itemSelectionChanged.connect(self._sync_copy_buttons)
        # Right-click → "このタグで検索" on the selected tag rows (C-10).  Only
        # useful with a tags.db (the same DB backs both this window and the
        # search), so the menu item is offered only when one is present.
        self._tag_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tag_table.customContextMenuRequested.connect(self._on_tag_context_menu)
        # Ctrl+C copies the currently-selected tag rows.
        copy_sc = QShortcut(QKeySequence.Copy, self._tag_table)
        copy_sc.setContext(Qt.WidgetShortcut)
        copy_sc.activated.connect(self._copy_selected_tags)
        hdr = self._tag_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # タグが無いとき空のテーブルを大きく残さない:
        # スタックで「表 / 中央寄せの案内」を切り替える（案内文は従来の
        # 下端注記と同じ文言を昇格させる — _sync_tag_stack）。
        self._tag_stack = QStackedWidget()
        self._tag_stack.addWidget(self._tag_table)
        self._tag_empty_label = QLabel("")
        self._tag_empty_label.setWordWrap(True)
        self._tag_empty_label.setAlignment(Qt.AlignCenter)
        self._tag_empty_label.setStyleSheet(hint_style())
        self._tag_stack.addWidget(self._tag_empty_label)
        outer.addWidget(self._tag_stack, 1)
        if not self._ai_ui:
            self._tag_table.setVisible(False)
            self._tag_stack.setVisible(False)

        # Notice shown when there's no tag data for the current selection.
        self._notice_label = QLabel("")
        self._notice_label.setWordWrap(True)
        self._notice_label.setStyleSheet(hint_style())
        outer.addWidget(self._notice_label)

        if not self._ai_ui:
            # 素の配布（AI パック無効）: タグ表（唯一の縦ストレッチ持ち,
            # ``addWidget(self._tag_stack, 1)``）が非表示のため、その
            # ストレッチが失われて可視要素間に余白が分散し間延びする。
            # ここで明示的なストレッチを入れて上詰めにする。
            outer.addStretch(1)

        buttons = QDialogButtonBox()
        # "この画像で類似検索" (item E15): seed a similar-image search in the left
        # pane from the currently-shown image.  Only shown when semantic vectors
        # exist; enabled only while an image is selected (set in show_path).
        self._similar_btn = QPushButton(t("viewer.common.similar_search_image"))
        self._similar_btn.setToolTip(t("viewer.detail_window.similar_search_tooltip"))
        self._similar_btn.clicked.connect(self._on_similar_clicked)
        self._similar_btn.setEnabled(False)
        buttons.addButton(self._similar_btn, QDialogButtonBox.ActionRole)
        # setVisible は必ず addButton の後に呼ぶ: QDialogButtonBox はレイアウト
        # 時に追加ボタンを show() するため、先に隠しても復活してしまう
        # （素配布 / ベクトル索引なしで AI ボタンが露出してしまう）。
        self._similar_btn.setVisible(self._similar_available and self._ai_ui)
        self._copy_selected_btn = QPushButton(t("viewer.detail_window.copy_selected_tags"))
        self._copy_selected_btn.clicked.connect(self._copy_selected_tags)
        buttons.addButton(self._copy_selected_btn, QDialogButtonBox.ActionRole)
        self._copy_all_btn = QPushButton(t("viewer.detail_window.copy_all_tags"))
        self._copy_all_btn.clicked.connect(self._copy_tags)
        buttons.addButton(self._copy_all_btn, QDialogButtonBox.ActionRole)
        if not self._ai_ui:
            self._copy_selected_btn.setVisible(False)
            self._copy_all_btn.setVisible(False)
        close_btn = buttons.addButton(QDialogButtonBox.Close)
        close_btn.clicked.connect(self.close)
        localize_buttons(buttons)
        # 閲覧専用ウィンドウ — 肯定アクション（Accept/Yes）が
        # 無いので、どのボタンも「閉じる」の代わりにアクセント化された既定
        # ボタンとして目立たないようにする。
        demote_close_default(buttons)
        outer.addWidget(buttons)

    # ----------------------------------------------------------- public API

    def show_path(self, path: Path | None) -> None:
        """Populate the window for *path* (``None`` clears it)."""
        self._path = path
        # 前の選択のために飛んでいる 3 本の読みを全部降ろす（キュー
        # 待ちは破棄、実行中は着地が ``bind`` の選別で捨てられる）。この後
        # 実際に投げ直すのは選択の種類で決まる一部だけなので、投入側の
        # 追い越しだけに頼らず**ここで揃えて**降ろす。
        self._fs_stream.cancel()
        self._meta_stream.cancel()
        self._tag_stream.cancel()
        if path is None:
            self._show_placeholder()
            return
        # The similar-image button only makes sense for an image file.
        self._update_similar_button(path)
        self._name_label.setText(path.name)
        self._name_label.setToolTip(path.name)
        self._path_label.setText(str(path))
        # ★ / あとで見る / ユーザータグ。
        self._update_curation(path)
        # Embedded metadata (PNG/EXIF — M05): hide the section until the async
        # read lands, and only bother probing an image file (EXIF/PNG chunks
        # never exist on other kinds).
        self._meta_box.setVisible(False)
        # 節を隠すだけでは中身が残る: 画像 → 非画像の遷移では埋め込みメタの読みを
        # 出さないので `_on_metadata` が来ず、前の選択のチャンクがレイアウトに
        # 積まれたままになる（次に画像を選ぶと `_populate_metadata` が
        # `_clear_metadata` するので露見しないが、節の高さ計算には効く）。
        # `_show_placeholder` が「隠す + 空にする」を対で行っているのと同じ並び。
        self._clear_metadata()
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            self._meta_stream.submit(
                lambda p=path: extract_image_metadata(p)
            )

        # tags.db の行はワーカーで引く: ``image_detail`` は完全一致で
        # 外すと ``LOWER(path)`` の全表スキャンへ落ちるため、未スキャンの選択を
        # ↓キーで流すだけで GUI が刻まれていた。着地までは「…」で待ち、行の
        # 有無で ``_on_tag_detail`` / ``_on_tag_detail_missing`` へ分岐する。
        # 行があるときに FS 読みを出さない従来の性質
        # （``_populate_from_detail`` の docstring）も保つ。
        if self._tag_index is None:
            self._populate_filesystem_only(path)
            return
        self._populate_pending()
        self._tag_stream.submit(
            lambda p=path, idx=self._tag_index: _read_tag_detail(p, idx)
        )

    def refresh_curation(self, path: Path | None = None) -> None:
        """★ / あとで見る / ユーザータグ の 3 行だけを読み直す.

        この 3 行は ``show_path``（= 選択変更）でしか更新されないため、
        選択を動かさずに★を付け替える経路（0-5 キー / 右クリック /
        ライトボックス）ではウィンドウを開いたまま古い値が残っていた。
        ホストの ``_on_curation_changed`` がグリッド・情報パネル・
        ライトボックス・ヘッダーを塗り直すのと同じ列にこの窓も並べる。

        *path* を渡すと、表示中のパスと一致するときだけ更新する（別の
        ファイルの変更でこの窓を触らない）。
        """
        if path is not None and path != self._path:
            return
        self._update_curation(self._path)

    # --------------------------------------------------------------- helpers

    def _update_curation(self, path: Path | None) -> None:
        """★ / あとで見る / ユーザータグ の 3 行を同期.

        ``user_meta`` は本体所有なので AI パック無効（素の配布）でも表示できる。
        値の無い行は情報パネルの詳細カードと同じく行ごと隠す。
        """
        meta = None
        if path is not None and self._user_meta is not None:
            try:
                meta = self._user_meta.get(path)
            except Exception:  # pragma: no cover (defensive — store degraded)
                meta = None
        star = int(getattr(meta, "star", 0) or 0)
        later = bool(getattr(meta, "later", False))
        tags = list(getattr(meta, "tags", ()) or ())
        self._lbl_star.setText(
            t("viewer.info_panel.detail_star_value", n=star) if star else "—"
        )
        self._lbl_later.setText(
            t("viewer.detail_window.later_yes") if later else "—"
        )
        self._lbl_user_tags.setText(
            t("common.sep.comma").join(tags) if tags else "—"
        )
        self._info_form.setRowVisible(self._lbl_star, star > 0)
        self._info_form.setRowVisible(self._lbl_later, later)
        self._info_form.setRowVisible(self._lbl_user_tags, bool(tags))

    def _set_ai_rows_visible(self, visible: bool) -> None:
        """tags.db 由来の 5 行（年齢区分 / モデル / 記録しきい値 / スキャン日時
        / タグ数）の可視を **1 箇所で** 切り替える.

        未スキャン画像でも 5 行が「—」で並び続けていた。キュレーション 3 行が
        既に採っている「値が無い行は出さない」流儀へ揃える。**必ずこの funnel
        を通すこと** — 値の代入経路（``_populate_from_detail`` /
        ``_populate_filesystem_only`` / ``_show_placeholder``）の片方だけを
        直すと、スキャン済み → 未スキャンへ選択が変わったとき行が残る。
        AI パック無効（素の配布）では常に非表示のまま。
        """
        show = bool(visible) and self._ai_ui
        for lbl in (
            self._lbl_rating, self._lbl_model, self._lbl_floor,
            self._lbl_scanned, self._lbl_ntags,
        ):
            self._info_form.setRowVisible(lbl, show)

    def set_tag_indexes(self, tag_index, vector_index) -> None:
        """tags.db 再読込後の新しい索引を受け取る（K01 の再注入の受け口）。

        以前は窓側が ``_tag_index`` / ``_similar_available`` / ``_similar_btn``
        を直に書いていたため、可視条件から ``_ai_ui`` 項が落ち（素の配布でも
        ベクトル索引があればボタンが現れうる）、``_update_similar_button`` が
        持つ活性条件も更新されないままだった。表示規則はこの 1 箇所に閉じる。

        索引はビューアが型を知らない不透明オブジェクト（AI パックの provider
        が返したもの）で、``None``（provider 未登録 / tags.db 不在）でも
        劣化シームへ落ちるだけ。
        """
        self._tag_index = tag_index
        self._similar_available = vector_index is not None
        btn = getattr(self, "_similar_btn", None)
        if btn is not None:
            # 構築時 (:522) と同じ規則 — AI パック無効なら常に非表示。
            btn.setVisible(self._similar_available and self._ai_ui)
        self._update_similar_button(self._path)

    def _update_similar_button(self, path: Path | None) -> None:
        """Enable 「この画像で類似検索」 only for an image while vectors exist."""
        btn = getattr(self, "_similar_btn", None)
        if btn is None:
            return
        ok = (
            self._similar_available
            and path is not None
            and path.suffix.lower() in _IMAGE_SUFFIXES
        )
        btn.setEnabled(ok)

    def _on_similar_clicked(self) -> None:
        """Seed a similar-image search in the left pane from the current image."""
        if self._path is not None and self._similar_available:
            self.similar_search_requested.emit(self._path)

    def _show_placeholder(self) -> None:
        self._update_similar_button(None)
        self._name_label.setText(t("viewer.detail_window.no_selection"))
        self._name_label.setToolTip("")
        self._path_label.setText("")
        self._update_curation(None)
        if hasattr(self, "_meta_box"):
            self._meta_box.setVisible(False)
            self._clear_metadata()
        for lbl in (
            self._lbl_kind, self._lbl_size, self._lbl_dims, self._lbl_rating,
            self._lbl_model, self._lbl_floor, self._lbl_scanned, self._lbl_ntags,
        ):
            lbl.setText("—")
        self._set_ai_rows_visible(False)
        self._set_tags([])
        if not self._ai_ui:
            # 素の配布: tags.db への言及（タガー導線）は出さず中立の案内のみ。
            # 共通キーを使い回すと「ここに詳細とAIタグが表示されます」と、
            # 存在しない面を予告してしまう（対称の ``_populate_filesystem_only``
            # も中立の案内を出す）。
            self._notice_label.setText(
                t("viewer.detail_window.select_image_hint_free")
            )
        elif self._tag_index is None:
            self._notice_label.setText(t("viewer.detail_window.no_tagsdb_full"))
        else:
            self._notice_label.setText(t("viewer.detail_window.select_image_hint"))
        self._sync_tag_stack()

    def _populate_from_detail(self, path: Path, detail: dict) -> None:
        # A file recorded in tags.db is always a scanned image file (never a
        # directory), so the kind can come from the suffix alone — no
        # GUI-thread ``is_dir()`` NAS round-trip on every selection change.
        suffix = path.suffix.lower().lstrip(".")
        self._lbl_kind.setText(suffix.upper() if suffix else t("common.label.file"))
        self._lbl_size.setText(_human_size(detail.get("size")))
        w, h = detail.get("width"), detail.get("height")
        self._lbl_dims.setText(f"{w} × {h}" if w and h else "—")
        rating = detail.get("rating") or "—"
        self._lbl_rating.setText(f"{rating}  ({_pct(detail.get('rating_score'))})")
        self._lbl_model.setText(str(detail.get("model") or "—"))
        floor = detail.get("floor")
        self._lbl_floor.setText(f"{float(floor):.2f}" if floor is not None else "—")
        self._lbl_scanned.setText(_human_time(detail.get("scanned_at")))
        self._set_ai_rows_visible(True)

        tags = detail.get("tags") or []
        # 「タグ数」は一般タグだけを数えるが、すぐ下の絞り込み行の「N 件」は
        # 年齢区分込みの表の行数を出す。素の数字を 2 つ縦に並べると、どちらが
        # 「このファイルのタグ数」なのか画面からは判別できない（コメントに
        # 書いてあった意図が UI に現れていなかった）。母集団を値の中で明示する。
        n_general = sum(1 for _, _, c in tags if c == 0)
        n_rating = len(tags) - n_general
        self._lbl_ntags.setText(
            t(
                "viewer.detail_window.tag_count_value",
                general=n_general, rating=n_rating,
            )
        )
        self._set_tags(tags)
        if tags:
            self._notice_label.clear()
        else:
            self._notice_label.setText(t("viewer.detail_window.no_tags_recorded"))
        self._sync_tag_stack()

    def _populate_filesystem_only(self, path: Path) -> None:
        # stat() + is_dir() + header-only image read all touch the disk, so
        # they run on a worker (``_fs_stream``) — the labels show "…" until the
        # result lands, keeping arrow-key navigation smooth on cold NAS.
        self._lbl_kind.setText("…")
        self._lbl_size.setText("…")
        self._lbl_dims.setText("…")
        self._fs_stream.submit(lambda p=path: _read_fs_info(p))
        self._lbl_rating.setText("—")
        self._lbl_model.setText("—")
        self._lbl_floor.setText("—")
        self._lbl_scanned.setText("—")
        self._lbl_ntags.setText("—")
        self._set_ai_rows_visible(False)
        self._set_tags([])
        if not self._ai_ui:
            self._notice_label.clear()
        elif self._tag_index is None:
            self._notice_label.setText(t("viewer.detail_window.no_tagsdb"))
        elif path.suffix.lower() in _IMAGE_SUFFIXES:
            self._notice_label.setText(t("viewer.detail_window.not_scanned"))
        else:
            self._notice_label.setText(t("viewer.detail_window.not_image"))
        self._sync_tag_stack()

    def _populate_pending(self) -> None:
        """tags.db 参照の着地待ち表示.

        値は ``_on_tag_detail`` が行の有無で確定させる。ここで前の選択の値を
        残すと「別のファイルの情報が出ている」ように読めるので伏せる。
        """
        for label in (
            self._lbl_kind, self._lbl_size, self._lbl_dims,
            self._lbl_rating, self._lbl_model, self._lbl_floor,
            self._lbl_scanned, self._lbl_ntags,
        ):
            label.setText("…")
        self._set_tags([])
        self._notice_label.clear()
        self._sync_tag_stack()

    def _on_tag_detail(self, detail: object) -> None:
        """tags.db の行が着地した — 表示を行の内容で確定させる."""
        path = self._path
        if path is None or not isinstance(detail, dict):
            return
        self._populate_from_detail(path, detail)

    def _on_tag_detail_missing(self) -> None:
        """tags.db に行が無い（または読めなかった）— FS 情報だけの表示へ。

        読みをここで初めて出すのは元の形のまま（行があるときは
        ``_populate_from_detail`` が値を持っているので FS 読みを出さない）。
        """
        path = self._path
        if path is not None:
            self._populate_filesystem_only(path)

    def _on_fs_info(self, payload: object) -> None:
        """Async filesystem info landed (kind / size / on-disk dims)."""
        if not isinstance(payload, tuple):
            return  # the worker raised
        kind, size_text, dims_text = payload
        self._lbl_kind.setText(kind)
        self._lbl_size.setText(size_text)
        self._lbl_dims.setText(dims_text)

    # ----------------------------------------------------- embedded metadata

    def _on_metadata(self, meta: object) -> None:
        """Async PNG/EXIF metadata landed (M05) — populate or hide the section."""
        if not isinstance(meta, ImageMetadata) or meta.is_empty:
            self._meta_box.setVisible(False)
            self._clear_metadata()
            self._sync_meta_stretch()
            return
        self._populate_metadata(meta)
        self._meta_box.setVisible(True)
        self._sync_meta_stretch()

    def _sync_meta_stretch(self) -> None:
        """縦の余りを「中身がある側」へ渡す.

        既定では ``_tag_stack`` がストレッチ 1 を総取りするが、タグ表が空ページ
        （未スキャン / 0 件 / AI パック無効）の間はメタデータ節が最小高のまま
        潰れ、その直下に背景色だけの大きな空白が残っていた。空ページの間だけ
        ストレッチをメタ節へ移し、併せて ``_META_BOX_MAX_H`` の上限も外して
        渡された高さを実際に使えるようにする（上限は「タグ表と同居している
        ときに節が窓を専有しない」ためのもので、同居していない間は不要）。

        **窓の再フィットは行わない**: 節が現れるたびに ``adjustSize`` で高さを
        合わせ直す案は「選択追従で高さが跳ねる」ので採らない（`__init__` の resize コメント）。最小高 140 は
        ``QLayout`` が top-level の minimumSize として自動的に効かせるので、
        窓が小さすぎる場合は Qt 側が必要なぶんだけ広げる。
        """
        if not hasattr(self, "_outer_layout"):
            return
        tags_present = self._ai_ui and bool(self._all_tags)
        # ``isVisible`` は親（ダイアログ）が未表示だと常に False なので、
        # この節自身の明示的な hide 状態だけを見る。
        give_meta = not self._meta_box.isHidden() and not tags_present
        self._meta_scroll.setMaximumHeight(
            _UNBOUNDED_H if give_meta else _META_BOX_MAX_H
        )
        self._outer_layout.setStretchFactor(self._meta_box, 1 if give_meta else 0)
        self._outer_layout.setStretchFactor(self._tag_stack, 0 if give_meta else 1)
        self._sync_meta_chunk_heights(give_meta)

    def _sync_meta_chunk_heights(self, give_meta: bool) -> None:
        """節が縦を独り占めできるときは 1 件だけのチャンク箱の上限も外す.

        節の外枠（``_meta_scroll``）の上限を外しても、内側の箱が
        ``_META_CHUNK_MAX_H`` で止まっていると渡された高さは空白になる。
        チャンクが 1 件のときだけ内側も解き、複数あるときは 1 件が節を専有
        しないよう上限を保つ（そのときは末尾ストレッチで上詰めにする）。
        窓の再フィット（``adjustSize``）は行わない。
        """
        chunks = getattr(self, "_meta_chunks", [])
        unbounded = give_meta and len(chunks) == 1
        for box in chunks:
            box.setMaximumHeight(_UNBOUNDED_H if unbounded else _META_CHUNK_MAX_H)
        tail = getattr(self, "_meta_tail", None)
        if tail is not None:
            tail.changeSize(
                0, 0, QSizePolicy.Minimum,
                QSizePolicy.Minimum if unbounded else QSizePolicy.Expanding,
            )
            self._meta_box_layout.invalidate()

    def _clear_metadata(self) -> None:
        """Remove every widget currently in the metadata box's layout."""
        layout = getattr(self, "_meta_box_layout", None)
        if layout is None:
            return
        self._meta_chunks = []
        self._meta_tail = None
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
            else:
                child = item.layout()
                if child is not None:
                    self._clear_sublayout(child)

    @staticmethod
    def _clear_sublayout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _populate_metadata(self, meta: ImageMetadata) -> None:
        """Rebuild the metadata box from *meta* (EXIF form + PNG text chunks)."""
        self._clear_metadata()
        # EXIF capture fields first (compact label/value form).
        if meta.exif:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignRight)
            for label_key, value in meta.exif:
                val_lbl = QLabel(value)
                val_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                val_lbl.setWordWrap(True)
                form.addRow(t(label_key), val_lbl)
            self._meta_box_layout.addLayout(form)
        # PNG text chunks (potentially long SD prompts) — a header + wrapped,
        # read-only box + a コピー button per chunk.
        for name, value in meta.text_chunks:
            container, box = self._build_text_chunk(
                name, value,
                full_value=meta.full_text(name),
                truncated=name in meta.truncated_chunks,
            )
            self._meta_box_layout.addWidget(container)
            self._meta_chunks.append(box)
        # 末尾ストレッチ: スクロール領域は `setWidgetResizable(True)` なので、
        # 上限付きの箱しか無いと余りが行間へ散って下詰めに見える。伸ばす対象が
        # 1 件だけのときは `_sync_meta_chunk_heights` がこのストレッチを畳む。
        self._meta_tail = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self._meta_box_layout.addItem(self._meta_tail)

    def _build_text_chunk(
        self, name: str, value: str, *,
        full_value: str = "", truncated: bool = False,
    ) -> tuple[QWidget, QPlainTextEdit]:
        """1 チャンクぶんの見出し + [コピー] + 本文箱を作る。

        *value* は表示用（長いチャンクは読み取り側で切り詰め済み）、
        *full_value* は [コピー] に渡す全文。``QPlainTextEdit`` の初期レイア
        ウトは GUI スレッドのコストなので、折り返し位置の無い長大な 1 行を
        丸ごと入れると秒〜分単位で止まる。
        """
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        header = QHBoxLayout()
        title = QLabel(name)
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title, 1)
        if truncated:
            note = QLabel(t("viewer.detail_window.meta_chunk_truncated"))
            note.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
            header.addWidget(note)
        copy_btn = QPushButton(t("common.action.copy"))
        # コピーは全文（切り詰めるのは表示だけ）。
        copy_value = full_value or value
        copy_btn.clicked.connect(
            lambda _=False, v=copy_value: self._copy_text(v)
        )
        header.addWidget(copy_btn)
        col.addLayout(header)
        box = QPlainTextEdit(value)
        box.setReadOnly(True)
        box.setMaximumHeight(_META_CHUNK_MAX_H)
        box.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        col.addWidget(box)
        return container, box

    @staticmethod
    def _copy_text(text: str) -> None:
        QGuiApplication.clipboard().setText(text)

    def _set_tags(self, tags: list[tuple[str, float, int]]) -> None:
        self._all_tags = list(tags)
        self._apply_filter(self._filter_edit.text())

    def _sync_tag_stack(self) -> None:
        """タグ無しでは空テーブルの代わりに案内を中央表示.

        呼び出しは案内文（``_notice_label``）確定後 — 下端の小さな注記を
        中央へ昇格させ、二重表示を避けるため下端側は隠す。フィルタで 0 行に
        なっただけ（``_all_tags`` は非空）のときは表のまま。

        タグが 0 件（未スキャンを含む）のときは、絞り込む対象が無い
        ``_filter_edit`` / ``_count_label`` も一緒に隠す —
        空案内の上に無意味な入力欄が浮くのを防ぐ。
        """
        # タグの有無が変わればメタ節へのストレッチ配分も変わる。
        # `_ai_ui` が False のときも通す（タグ表は常に空 = メタ節が受け取る側）。
        self._sync_meta_stretch()
        if not self._ai_ui:
            return
        has_tags = bool(self._all_tags)
        self._filter_edit.setVisible(has_tags)
        self._count_label.setVisible(has_tags)
        if has_tags:
            self._tag_stack.setCurrentWidget(self._tag_table)
            self._notice_label.setVisible(True)
            return
        self._tag_empty_label.setText(self._notice_label.text())
        self._tag_stack.setCurrentWidget(self._tag_empty_label)
        self._notice_label.setVisible(False)

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        rows = [
            t for t in self._all_tags
            if not needle or needle in t[0].lower()
        ]
        # Repopulate; disable sorting while filling to avoid row reshuffling.
        self._tag_table.setSortingEnabled(False)
        self._tag_table.setRowCount(len(rows))
        for r, (tag, score, cat) in enumerate(rows):
            name_item = QTableWidgetItem(tag)
            score_item = _ScoreItem(_pct(score), score)
            cat_key = _CAT_LABELS.get(cat)
            cat_item = QTableWidgetItem(t(cat_key) if cat_key is not None else str(cat))
            cat_item.setTextAlignment(Qt.AlignCenter)
            self._tag_table.setItem(r, 0, name_item)
            self._tag_table.setItem(r, 1, score_item)
            self._tag_table.setItem(r, 2, cat_item)
        self._tag_table.setSortingEnabled(True)
        self._sync_copy_buttons()
        if self._all_tags:
            shown = len(rows)
            total = len(self._all_tags)
            self._count_label.setText(
                t("viewer.detail_window.count_filtered", shown=shown, total=total)
                if needle
                else t("viewer.detail_window.count_total", total=total)
            )
        else:
            self._count_label.setText("")

    def _sync_copy_buttons(self) -> None:
        """2 つのコピーボタンの活性を **同じ 1 箇所で** 決める.

        「タグ 0 件でクリップボードを空文字に上書きする」副作用を防ぐ無効化を
        「タグをすべてコピー」だけに掛けると、どちらも実行できる内容が無い状態で
        2 つの活性が食い違って見える。表の再構築（フィルタ）と
        選択変更の両方からここへ集約する。
        """
        copy_all = getattr(self, "_copy_all_btn", None)
        if copy_all is not None:
            copy_all.setEnabled(self._tag_table.rowCount() > 0)
        copy_selected = getattr(self, "_copy_selected_btn", None)
        if copy_selected is not None:
            copy_selected.setEnabled(bool(self._tag_table.selectedItems()))

    def _copy_tags(self) -> None:
        # Copy all currently-shown (filtered) tag names as a comma-separated
        # list — handy for pasting into a prompt or another tool.
        rows = self._tag_table.rowCount()
        names = [
            self._tag_table.item(r, 0).text()
            for r in range(rows)
            if self._tag_table.item(r, 0) is not None
        ]
        # タグ 0 件でクリップボードを空文字で上書きしない（別作業のコピー内容が
        # 無言で消える回復不能な副作用）。ボタン自体も
        # 無効化しているが、ショートカット等の別経路のための最終ガード。
        if not names:
            return
        QGuiApplication.clipboard().setText(", ".join(names))

    def _copy_selected_tags(self) -> None:
        # Copy only the tag names of the selected rows. Falls back silently
        # (no clipboard change) when nothing is selected.
        names = self._selected_tag_names()
        if names:
            QGuiApplication.clipboard().setText(", ".join(names))

    def _selected_tag_names(self) -> list[str]:
        """Tag names of the currently-selected rows, in row order."""
        selected_rows = sorted(
            idx.row() for idx in self._tag_table.selectionModel().selectedRows()
        )
        return [
            self._tag_table.item(r, 0).text()
            for r in selected_rows
            if self._tag_table.item(r, 0) is not None
        ]

    def _on_tag_context_menu(self, pos) -> None:
        # "このタグで検索" injects each selected tag into the left pane's AI-tag
        # search (C-10).  Suppressed entirely without a tags.db — the search it
        # would drive can't run, and copy actions remain via Ctrl+C / buttons.
        if self._tag_index is None:
            return
        names = self._selected_tag_names()
        if not names:
            # Fall back to the row under the cursor when nothing is selected yet.
            row = self._tag_table.rowAt(pos.y())
            item = self._tag_table.item(row, 0) if row >= 0 else None
            if item is not None:
                names = [item.text()]
        if not names:
            return
        menu = QMenu(self)
        label = (
            t("viewer.detail_window.search_this_tag")
            if len(names) == 1
            else t("viewer.detail_window.search_these_tags")
        )
        search_act = QAction(label, menu)
        search_act.triggered.connect(
            lambda _=False, ns=list(names): [
                self.tag_search_requested.emit(n) for n in ns
            ]
        )
        menu.addAction(search_act)
        menu.exec(self._tag_table.viewport().mapToGlobal(pos))
        # This window is modeless and long-lived (it follows the selection while
        # left open), so a menu parented to it would linger for the whole
        # session — one per right-click.  Same disposal as the other views'
        # context menus (media_view / markdown_view).
        menu.deleteLater()
