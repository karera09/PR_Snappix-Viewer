"""Right pane — the "information panel".

The right seat is the **detail area** for whatever the centre pane has as its
main selection (中央がメインのファイル選択、右がその詳細表示エリア).  It stacks, top → bottom:

* a **file-detail card** — shown when a concrete *file* (image / video / PDF /
  … ) is selected.  A thumbnail + name + 種別 / サイズ / 更新日時 / 解像度 /
  スター + the full path (elided, full value in the tooltip).  This is what
  makes "中央で画像を選択したら右にその画像の詳細" work instead of mirroring
  the same folder listing on both sides.
* a **post meta-card** — a compact key-value view of the selected *post's*
  ``post.md`` metadata (投稿日 / 作者 / プラン / タグ / ♡お気に入り / 🔒未取得 /
  投稿ページリンク).  Wording + row order mirror the central
  :class:`~snappix.viewer.markdown_view.MarkdownView` meta card.  Shown when a
  folder / post tile is selected (its "detail" is the post metadata).  Hidden
  when the current selection has no ``post.md``, and collapsed while the centre
  is already showing that same ``post.md`` on the stage (中央 markdown
  ヘッダと右メタカードの 7 行二重表示を避ける).
* the existing **file list** (passed in and reparented here), under its own
  :class:`~snappix.common.ui.PanelHeader`.  Kept below both cards as the
  selection-synced navigation strip.

The file-detail card and the post meta-card are **mutually exclusive**: a file
selection shows the former (and hides the latter); a folder/post selection
shows the latter.  :meth:`set_file_detail` drives the file card,
:meth:`set_post_meta` the meta card, and an internal ``_apply_visibility``
keeps the "only one card at a time" invariant no matter which arrives first.

``InfoPanel`` is a dumb view: it never touches the filesystem.  The window
feeds it an already-parsed :class:`~snappix.viewer.post_md.ParsedPost` via
:meth:`set_post_meta` and an already-statted :class:`FileDetail` (+ an
already-decoded thumbnail) via :meth:`set_file_detail` — all read off the GUI
thread — keeping filesystem I/O in the window where the off-thread machinery
lives.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ..common.i18n import t
from ..common.post_meta import KEY_CREATOR, KEY_POST_ID, KEY_SERVICE, KEY_URL
from ..common.ui import ElidedLabel, PanelHeader, hint_style
from ..common.ui.tokens import FONT_CAPTION_PT
from ._indicator import badge_name, badge_pixmap
from .context_menus import copy_path_to_clipboard
from .curation_strip import CurationStrip
from .post_md import ParsedPost

#: Fixed height of the file-detail thumbnail box (a layout dimension, not a
#: colour / font-size — those go through tokens).  The thumbnail is scaled to
#: fit inside a square of this edge, aspect preserved.
_DETAIL_THUMB_EDGE = 200
#: 印ストリップの節見出しに出す対象名の最大幅（省略付き・レイアウト寸法）。
_TARGET_NAME_MAX_PX = 150


@dataclass
class FileDetail:
    """Off-thread-computed detail payload for a selected file (dumb-view input).

    The window fills every field before handing it to :meth:`InfoPanel.set_file_detail`
    (name / path are known synchronously; ``kind`` / ``size_text`` /
    ``mtime_text`` / ``resolution_text`` come from an off-thread stat; ``star``
    from the in-memory user-meta map).  Text fields hold display-ready strings —
    ``""`` for a row that should be omitted, ``"…"`` for a value still loading.
    """

    path: Path
    name: str
    kind: str = "…"
    size_text: str = "…"
    mtime_text: str = "…"
    resolution_text: str = ""
    star: int = 0
    #: The user's own tags for this file — curation's third dimension needs a
    #: display surface like ★ and 「あとで見る」, not only the edit dialog.
    #: Empty = the row is omitted.
    user_tags: tuple[str, ...] = ()
    #: 「あとで見る」 flag, so the three curation dimensions are shown in the
    #: same amount on every surface.  ``False`` = the row is omitted.
    later: bool = False


def curation_rows(
    star: int, user_tags: tuple[str, ...] | list[str], later: bool,
) -> list[tuple[str, str]]:
    """The ★ / あとで見る / ユーザータグ rows, in the one canonical order.

    Every surface that shows user curation as
    text rows (詳細情報ウィンドウ ``detail_window._update_curation``) must show
    the **same three rows with the same labels and the same "hide an empty
    row" rule**.  Labels are the existing catalog keys (no new wording); rows
    with no value are simply absent from the returned list.  この情報パネルの
    2 カードは E2 以降、行ではなく最上段の :class:`CurationStrip` で印を見せる。
    """
    rows: list[tuple[str, str]] = []
    if star > 0:
        rows.append((
            t("viewer.post_grid.star_menu"),
            t("viewer.info_panel.detail_star_value", n=star),
        ))
    if later:
        rows.append((
            t("viewer.post_grid.watch_later"),
            t("viewer.detail_window.later_yes"),
        ))
    if user_tags:
        rows.append((
            t("viewer.detail_window.user_tags"),
            t("common.sep.comma").join(user_tags),
        ))
    return rows


def _meta_rows(parsed: ParsedPost) -> list[tuple[str, str, str | None, str | None]]:
    """Ordered ``(label, value, href, badge)`` rows for *parsed*.

    Deliberately mirrors ``markdown_pipeline._post_header_html`` — same i18n keys,
    same field order — so the compact panel card and the central document card
    stay visually consistent.  ``href`` is non-``None`` only for the 投稿ページ
    link row (its ``value`` is the link text).  ``badge`` names a
    :mod:`._indicator` badge kind to draw **in front of** the value: an emoji
    baked into the i18n value would follow neither the theme nor the installed
    fonts and would disagree with the vector artwork the tiles draw.  Values are plain text (the caller
    escapes when building the link).
    """
    rows: list[tuple[str, str, str | None, str | None]] = []
    meta = parsed.meta

    if parsed.posted_at is not None:
        dt = parsed.posted_at
        shown = (
            dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute)
            else dt.strftime("%Y-%m-%d")
        )
        rows.append((t("viewer.markdown_view.meta_posted"), shown, None, None))

    creator = meta.get(KEY_CREATOR, "").strip()
    if creator:
        rows.append((t("viewer.markdown_view.meta_creator"), creator, None, None))

    plan_bits = [p for p in (parsed.plan_name.strip(), parsed.plan_price.strip()) if p]
    if plan_bits:
        rows.append(
            (t("viewer.markdown_view.meta_plan"), " ".join(plan_bits), None, None)
        )

    if parsed.tags:
        rows.append((
            t("common.label.tag"),
            t("common.sep.comma").join(parsed.tags),
            None,
            None,
        ))

    if parsed.favorites is not None:
        rows.append((
            t("viewer.markdown_view.meta_favorites"),
            t("viewer.markdown_view.meta_favorites_value", n=parsed.favorites),
            None,
            "favorites",
        ))

    if parsed.locked_count > 0:
        rows.append((
            t("viewer.markdown_view.meta_locked"),
            t("viewer.markdown_view.meta_locked_value", n=parsed.locked_count),
            None,
            "locked",
        ))

    url = meta.get(KEY_URL, "").strip()
    service = meta.get(KEY_SERVICE, "").strip()
    post_id = meta.get(KEY_POST_ID, "").strip()
    if url:
        link_text = " / ".join(x for x in (service, post_id) if x) or url
        rows.append((t("viewer.markdown_view.meta_page"), link_text, url, None))
    elif service:
        rows.append((t("viewer.markdown_view.meta_service"), service, None, None))

    return rows


class _FileDetailCard(QWidget):
    """The file-detail card (thumbnail + name + fields + path).

    A dumb widget: :meth:`populate` fills it from a :class:`FileDetail`;
    :meth:`set_thumbnail` swaps the thumbnail image when a late async decode
    lands.  Reuses the same ``PanelHeader`` + right-aligned muted label column
    grammar as the post meta card so the two cards read as one design.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path: Path | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = PanelHeader(t("viewer.info_panel.detail_title"))
        layout.addWidget(self._header)
        self.header = self._header

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 8, 10, 10)
        body_layout.setSpacing(8)

        # Thumbnail — centred, aspect-preserved inside a fixed box.  Supplied by
        # the window (resident pane pixmap, or an async strip-loader decode);
        # blank until one arrives.
        self._thumb = QLabel()
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setMinimumHeight(_DETAIL_THUMB_EDGE)
        body_layout.addWidget(self._thumb)

        # File name (bold, wraps — long CJK names are common).
        self._name = QLabel()
        self._name.setStyleSheet("font-weight: bold;")
        self._name.setWordWrap(True)
        self._name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 幅の要求には参加させない（Ignored + 最小幅 0）: wordWrap の QLabel
        # でも折り返し位置の無い名前（空白の無い booru 形式など）は
        # minimumSizeHint が文字列全幅になり、外殻 QSplitter がそれを情報
        # パネルの最小幅として扱うため、中央席が潰れ窓ごと画面外へ広がって
        # いた（text_view のタイトル・detail_window の名前行と同じ手当て）。
        # 折り返せる名前は従来どおり折り返し、折れない分は右で切れる —
        # 全文はツールチップ（populate）と選択コピーで読める。
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name.setMinimumWidth(0)
        body_layout.addWidget(self._name)

        # Field grid (種別 / サイズ / 更新日時 / 解像度 / スター) — same
        # muted-right-label + value layout as the meta card.
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(4)
        self._grid.setColumnStretch(1, 1)
        body_layout.addLayout(self._grid)

        # Full path — muted, middle-elided, full value in the tooltip.
        # A read-only ``QLineEdit`` would cut the tail mid-character with no
        # ellipsis — the reason the shared ``ElidedLabel`` part exists (the
        # ``detail_window`` path row uses it too).  The
        # part deliberately does not set ``TextSelectableByMouse``, so the
        # QLineEdit's built-in copy affordance is replaced by an explicit
        # context menu reusing ``context_menus.copy_path_to_clipboard``.
        self._path_label = ElidedLabel(elide=Qt.ElideMiddle)
        self._path_label.setStyleSheet(hint_style())
        self._path_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self._path_label.customContextMenuRequested.connect(self._on_path_menu)
        body_layout.addWidget(self._path_label)

        layout.addWidget(body)

    def populate(self, detail: FileDetail) -> None:
        self._path = detail.path
        self._name.setText(detail.name)
        self._name.setToolTip(detail.name)
        # ``ElidedLabel.setText`` already installs the full value as the
        # tooltip (its documented contract).
        self._path_label.setText(str(detail.path))
        self._rebuild_grid(detail)

    def path_context_menu(self) -> QMenu | None:
        """The path row's right-click menu — フルパスをコピー.

        ``ElidedLabel`` paints over the whole widget and therefore does not
        set ``TextSelectableByMouse``, so the copy affordance a read-only
        ``QLineEdit`` gets from Qt for free has to be provided
        explicitly.  Reuses ``context_menus.copy_path_to_clipboard`` (the same
        action the grid / file-list menus offer).
        """
        if self._path is None:
            return None
        menu = QMenu(self._path_label)
        act = QAction(t("viewer.context_menus.copy_full_path"), menu)
        act.triggered.connect(
            lambda _=False, p=self._path: copy_path_to_clipboard(p, self.window())
        )
        menu.addAction(act)
        return menu

    def _on_path_menu(self, pos) -> None:
        menu = self.path_context_menu()
        if menu is not None:
            menu.exec(self._path_label.mapToGlobal(pos))
            # ``QMenu(self._path_label)`` is parented to the (long-lived) path
            # label, so exec() only hides it — without this the menu + its
            # QAction pile up on the label, one per right-click (same pattern
            # as file_list/_on_context_menu).
            menu.deleteLater()

    def set_thumbnail(self, pixmap: QPixmap | None) -> None:
        if pixmap is None or pixmap.isNull():
            self._thumb.clear()
            return
        # The supplied pixmap carries a devicePixelRatio (thumbnail_loader.py
        # sets it from the requesting window's DPR), so scaling to the
        # logical _DETAIL_THUMB_EDGE box in device-independent pixels leaves
        # it under-filling the box on a >100% Windows scale factor.  Scale in
        # physical pixels instead, then restore the source DPR on the result
        # so Qt still renders it at the intended logical size.
        dpr = pixmap.devicePixelRatio()
        edge = int(_DETAIL_THUMB_EDGE * dpr)
        scaled = pixmap.scaled(
            edge,
            edge,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self._thumb.setPixmap(scaled)

    def current_path(self) -> Path | None:
        return self._path

    def _rebuild_grid(self, detail: FileDetail) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        rows: list[tuple[str, str]] = [
            (t("common.label.type"), detail.kind),
            (t("common.label.size"), detail.size_text),
            (t("common.label.modified"), detail.mtime_text),
        ]
        if detail.resolution_text:
            rows.append((t("viewer.detail_window.resolution"), detail.resolution_text))
        # 印の 3 行はここには出さない — 同じペイン最上段の印ストリップが
        # 見せて操作もする（E2）。``detail.star`` 等は詳細情報ウィンドウが使う。
        for r, (label, value) in enumerate(rows):
            label_lbl = QLabel(label)
            label_lbl.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
            label_lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
            # Untrusted file metadata is display-only text; force plain text.
            value_lbl = QLabel(value)
            value_lbl.setTextFormat(Qt.PlainText)
            value_lbl.setWordWrap(True)
            value_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._grid.addWidget(label_lbl, r, 0)
            self._grid.addWidget(value_lbl, r, 1)


class InfoPanel(QWidget):
    """Right-pane container: file-detail card / post meta card + the file list."""

    #: 「本文を読む」 on the post meta card (split-view redesign 2026-07):
    #: the uniform folder click no longer auto-opens the post body, so the
    #: meta card carries a one-click route to it.  The window shows the
    #: selected post's ``post.md`` in the centre preview.
    post_body_requested = Signal()
    #: 印ストリップの要求 (path, kind, value) — ウィンドウが左ペインの単一
    #: funnel ``PostGrid.request_curation`` へ渡す（このペインは書かない）。
    curation_requested = Signal(object, str, object)

    def __init__(self, file_list: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.file_list = file_list

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- 印ストリップ: 最上段に常設。post.md の有無に従属しない（投稿情報
        # カードの中に置くと、post.md の無いフォルダでは印が面ごと消える）。
        # 店が無ければ節ごと隠す。
        self._curation_section = QWidget()
        # 幅の要求には参加させない（Ignored）: ★×5 + あとで見る + タグ列の自然幅
        # （数百 px）がパネルの最小幅になると、分割バーの記憶幅を侵食し、
        # 狭いパネルで畳めなくなる。足りない幅の扱いは**部品側**が持つ
        # （自分で畳む — 放置すると「右端から切れる」のではなく左の子が右の子に
        # 踏まれる）ので、ここは Ignored のままでよい。
        self._curation_section.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        cur_layout = QVBoxLayout(self._curation_section)
        cur_layout.setContentsMargins(0, 0, 0, 0)
        cur_layout.setSpacing(0)
        self._curation_header = PanelHeader(t("viewer.nav_rail.section_curation"))
        cur_layout.addWidget(self._curation_header)
        self.curation_strip = CurationStrip(mode="full")
        self.curation_strip.curation_requested.connect(self.curation_requested)
        cur_layout.addWidget(self.curation_strip)
        self._curation_section.setVisible(False)
        layout.addWidget(self._curation_section)

        # --- File-detail card (hidden until a file is selected).
        self._file_card = _FileDetailCard()
        self._file_card.setVisible(False)
        layout.addWidget(self._file_card)
        self._file_detail: FileDetail | None = None
        # NOTE: このペインは「空状態を今どう出しているか」の状態を持たない。
        # ウィンドウ（空状態オーケストレータ）の割当が ``NONE`` の間は
        # :meth:`apply_empty_guidance` が一切触らないので、フォルダ固有の
        # 空状態（スキャン失敗の ⚠+[再試行] /「このフォルダにはファイルが
        # ありません」）は自然に生き残る（ペイン側に空状態のフラグを持たずに
        # 済むのはこのため）。

        # --- Meta-card section (hidden until a post with metadata is shown).
        self._meta_section = QWidget()
        section_layout = QVBoxLayout(self._meta_section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(0)
        self._meta_header = PanelHeader(t("viewer.info_panel.section_title"))
        section_layout.addWidget(self._meta_header)
        self._meta_body = QWidget()
        self._meta_grid = QGridLayout(self._meta_body)
        self._meta_grid.setContentsMargins(10, 8, 10, 10)
        self._meta_grid.setHorizontalSpacing(10)
        self._meta_grid.setVerticalSpacing(4)
        self._meta_grid.setColumnStretch(1, 1)
        section_layout.addWidget(self._meta_body)
        self._meta_section.setVisible(False)
        self._has_meta_rows = False
        layout.addWidget(self._meta_section)

        # --- The file list (owns its own PanelHeader) fills the rest.
        layout.addWidget(file_list, 1)

    # ------------------------------------------------------------------ API

    def focus_band_targets(self) -> list[tuple[QWidget, PanelHeader]]:
        """フォーカス帯の ``(節, 見出し)`` — 印ストリップ / ファイル詳細 / 投稿情報 /
        ファイル一覧の 4 節。ファイル一覧の見出しは一覧自身が持つ。"""
        targets: list[tuple[QWidget, PanelHeader]] = [
            (self._curation_section, self._curation_header),
            (self._file_card, self._file_card.header),
            (self._meta_section, self._meta_header),
        ]
        header = getattr(self.file_list, "header", None)
        if isinstance(header, PanelHeader):
            targets.append((self.file_list, header))
        return targets

    def set_store_available(self, available: bool) -> None:
        """印ストリップの節を出す / 隠す（user_meta 店の有無 — 右クリックと同じ劣化）."""
        self.curation_strip.set_store_available(available)
        self._curation_section.setVisible(bool(available))

    def set_curation_target(
        self,
        path: Path | None,
        star: int = 0,
        later: bool = False,
        tags: tuple[str, ...] = (),
        *,
        display_name: str | None = None,
    ) -> None:
        """印ストリップの対象と現在値（ウィンドウが選択変更 / 印の変更ごとに呼ぶ）.

        対象名は節見出しの右端（``PanelHeader`` の件数ラベルの席）に省略付きで
        出す — 「この印はどれに付くか」を面が名乗る（ストリップ内に置くと
        パネル幅 310px では潰れる）。ツールチップはフルパス。

        *display_name* は窓が場所の名乗りの正本（``locations.location_label``）
        から渡す表示名 — 既定ライブラリの「ライブラリ」や ZIP 展開先の
        アーカイブ名。省略時は ``path.name``（パネル単体で使うとき）。
        """
        self.curation_strip.set_target(path, star, later, tags)
        header = self._curation_header
        if path is None:
            header.set_count_text("")
            header.setToolTip("")
            return
        fm = QFontMetrics(header.font())
        name = display_name if display_name is not None else path.name
        header.set_count_text(fm.elidedText(name, Qt.ElideMiddle, _TARGET_NAME_MAX_PX))
        header.setToolTip(str(path))

    def set_file_detail(
        self, detail: FileDetail | None, thumbnail: QPixmap | None = None,
    ) -> None:
        """Show the file-detail card for *detail*, or hide it when ``None``.

        A non-``None`` *detail* takes over the top of the panel and hides the
        post meta card (the two are mutually exclusive — a file's detail is not
        the containing post's metadata).  Passing the SAME path again (e.g. the
        async stat landing to fill 種別 / サイズ) keeps whatever thumbnail was
        already set unless a new *thumbnail* is supplied — so a late thumbnail
        request and a late field read never clobber each other.
        """
        self._file_detail = detail
        if detail is None:
            self._file_card.setVisible(False)
            self._apply_visibility()
            return
        same_path = self._file_card.current_path() == detail.path
        self._file_card.populate(detail)
        if thumbnail is not None:
            self._file_card.set_thumbnail(thumbnail)
        elif not same_path:
            # New file, no thumbnail yet — blank until one is supplied.
            self._file_card.set_thumbnail(None)
        self._file_card.setVisible(True)
        self._apply_visibility()

    def apply_empty_guidance(self, text: str | None) -> None:
        """空状態オーケストレータの割当をこの面へ描く（提案3）.

        *text* はウィンドウの :mod:`empty_state` リゾルバが決めた ``SECONDARY``
        の 1 行、``None`` は割当 ``NONE`` = 「この面については何も主張しない」。
        このペインは主案内（``PRIMARY``）を持たないので、アイコンもボタンも
        付けない（従属面は「アイコン無し・1 行・控えめ」）。

        **``NONE`` では一切書かない**のが要点: 呼び出し元 (``_on_counts_changed``) は絞り込み 1 文字ごとに
        走るため、無条件に既定文言を書き戻すと**このペインが自分で出した**
        フォルダ固有の空状態（スキャン失敗の ⚠+[再試行] /「このフォルダには
        ファイルがありません」）を踏み潰す。リゾルバは選択中のとき必ず
        ``NONE`` を割り当てる（＝ペイン自身の空状態が正しい局面）ので、
        ここは黙るだけでよい。

        **状態を持たない**のも意図的: ``set_empty_state`` 自身が同値なら no-op
        なので、「前に何を書いたか」を覚える必要がない。覚えると「割当が変わって
        いないのにペイン側だけ書き換わった」局面（選択→解除の往復）で書き戻しが
        効かなくなる — 旧 ``set_search_empty`` の遷移ガードが抱えていた順序
        依存を、``NONE`` では黙るという 1 規則へ畳んである。

        描くのは一覧側の公開入口
        （:meth:`children_grid.ChildrenGrid.set_orchestrated_hint`）に委ねる —
        席の空状態を決める条件（走査失敗カードが勝つ / タイルがある面には
        書かない）はあちらが唯一の適用点として持っている。``getattr`` ガード
        はメソッドの有無で残るので、スタブ / 非 GalleryView の一覧では
        従来どおり no-op になる。
        """
        if text is None:
            return
        hint = getattr(self.file_list, "set_orchestrated_hint", None)
        if hint is not None:
            hint(text)

    def set_file_thumbnail(self, path: Path, pixmap: QPixmap | None) -> None:
        """Swap the detail card's thumbnail when a late async decode lands.

        No-op unless the card is still showing *path* (the selection may have
        moved on before the loader answered).
        """
        if self._file_detail is not None and self._file_detail.path == path:
            self._file_card.set_thumbnail(pixmap)

    def set_post_meta(self, parsed: ParsedPost | None) -> None:
        """Populate the meta card from *parsed*, or hide it when ``None``.

        ``None`` collapses the section entirely so a plain folder / file
        selection shows just the file list — the pre-redesign look.  Any
        non-``None`` *parsed* shows the card: its meta rows plus a trailing
        「本文を読む」 link row (split-view redesign 2026-07 — the uniform
        folder click previews the representative image, so the post body
        needs this one-click route; clicking emits
        :attr:`post_body_requested`).  The card is additionally kept hidden
        whenever the file-detail card is active (mutual exclusivity).

        印（★ / あとで見る / ユーザータグ）はこのカードには載せない — 最上段の
        印ストリップ（:class:`CurationStrip`）が post.md の有無に関係なく同じ
        席で見せて操作もできるので、同じペインに二重に出さない（「各面で同じ
        3 行」は詳細情報ウィンドウ側の :func:`curation_rows` が担う）。
        """
        self._clear_grid()
        rows = _meta_rows(parsed) if parsed is not None else []
        self._has_meta_rows = parsed is not None
        for r, (label, value, href, badge) in enumerate(rows):
            label_lbl = QLabel(label)
            label_lbl.setStyleSheet(hint_style(font_pt=FONT_CAPTION_PT))
            label_lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
            value_lbl = QLabel()
            value_lbl.setWordWrap(True)
            value_lbl.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
            )
            if href:
                # Only this row is rich text (an anchor); both the href and the
                # link text are escaped so post.md values can't inject markup.
                value_lbl.setTextFormat(Qt.RichText)
                value_lbl.setText(
                    f'<a href="{html.escape(href, quote=True)}">'
                    f"{html.escape(value)}</a>"
                )
                value_lbl.setOpenExternalLinks(False)
                value_lbl.linkActivated.connect(self._open_link)
            else:
                # post.md meta values are untrusted (external-tool input) — force
                # plain text so HTML tags in a value can't be rendered as rich
                # text.  Mirrors ``markdown_pipeline._post_header_html`` (all-escaped).
                value_lbl.setTextFormat(Qt.PlainText)
                value_lbl.setText(value)
            self._meta_grid.addWidget(label_lbl, r, 0)
            if badge is None:
                self._meta_grid.addWidget(value_lbl, r, 1)
            else:
                self._meta_grid.addWidget(
                    self._badge_value_row(badge, value_lbl), r, 1
                )
        if parsed is not None:
            # Trailing 「本文を読む」 link row (value column, no label).  An
            # internal action link, not an external URL — routed through
            # ``post_body_requested`` so the window can show the body in the
            # centre preview.  The href is a fixed sentinel; nothing from
            # post.md reaches this markup.
            body_lbl = QLabel()
            body_lbl.setTextFormat(Qt.RichText)
            body_lbl.setText(
                '<a href="#read-body">'
                f"{html.escape(t('viewer.info_panel.read_post_body'))}</a>"
            )
            body_lbl.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
            body_lbl.setOpenExternalLinks(False)
            # Signal-to-signal connect (the str href is dropped) — a lambda
            # closing over ``self`` here forms a GC cycle through the
            # deleteLater'd label and crashed interpreter teardown (segfault
            # reproduced in the offscreen suite).
            body_lbl.linkActivated.connect(self.post_body_requested)
            self._meta_grid.addWidget(body_lbl, len(rows), 1)
        self._apply_visibility()

    @staticmethod
    def _badge_value_row(kind: str, value_lbl: QLabel) -> QWidget:
        """A value cell prefixed with the real badge artwork (提案1 の情報設計).

        The badge is the same pixmap the grid tile draws (``badge_pixmap``), so
        「タイル上のバッジと同じ絵が同じ意味で並ぶ」 — and no pictograph has to be
        spelled out as an emoji inside an i18n value.
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        chip = QLabel()
        dpr = float(row.devicePixelRatioF())
        # ``text=""`` = 図像だけのチップ。実数は隣の値ラベルが持つので、凡例用の
        # 見本文字（"N"）まで描くと「♥N 128 件」と二重に読める。
        # UI 面（カード）に載るので画像用スクリム配色ではなく面用の配色で描く
        # （スクリム配色のままだとライトテーマで黒く浮く）。
        chip.setPixmap(badge_pixmap(kind, dpr=max(1.0, dpr), text="", on_surface=True))
        chip.setToolTip(badge_name(kind))
        chip.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lay.addWidget(chip, 0, Qt.AlignTop)
        lay.addWidget(value_lbl, 1)
        return row

    # -------------------------------------------------------------- internals

    def _apply_visibility(self) -> None:
        """Enforce "at most one card visible": file card wins over the meta card.

        The meta card shows only when there are meta rows AND no file-detail
        card is active — so ``set_post_meta`` / ``set_file_detail`` can arrive
        in any order (the async post.md read may land after the file selection)
        without the two cards ever showing together.
        """
        file_active = self._file_detail is not None
        self._meta_section.setVisible(self._has_meta_rows and not file_active)

    def _clear_grid(self) -> None:
        while self._meta_grid.count():
            item = self._meta_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _open_link(url: str) -> None:
        # External post-page link — opened the same way the central meta card /
        # markdown body links open (QDesktopServices, no confirmation dialog).
        # The ``- url:`` value is untrusted (external-tool input): restrict to
        # http/https so a malicious post.md can't smuggle a ``file://`` / UNC
        # launch vector through the default handler.  Anything else is ignored.
        qurl = QUrl(url)
        if qurl.scheme().lower() in ("http", "https"):
            QDesktopServices.openUrl(qurl)


__all__ = ["FileDetail", "InfoPanel", "curation_rows"]
