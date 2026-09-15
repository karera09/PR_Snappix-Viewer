"""Clickable breadcrumb path widget for the PostGrid header.

Replaces the old elided ``QLabel`` path display with a row of flat
``QToolButton`` segments joined by ``>`` separators.  Clicking any ancestor
segment emits :data:`BreadcrumbBar.navigate` with that path so the window can
jump there; the trailing (current) segment is non-clickable bold text.  When
there isn't room for every segment, the leading ancestors collapse into a
single ``…`` button whose menu lists them.

The width-sensitive collapse decision (how many leading segments to fold) is
kept in the Qt-free :func:`plan_breadcrumb` so it can be unit-tested without a
display, mirroring how ``justified_layout`` isolates the pane layout maths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..common.fsutil import pick_library_base, relative_parts
from ..common.i18n import t


@dataclass(frozen=True)
class Segment:
    """One breadcrumb segment: the label to show and the path it navigates to.

    ``label`` is the display text (a path component, or the drive/root for the
    first segment); ``path`` is the absolute path that clicking it opens.
    """

    label: str
    path: Path


#: パンくず / ナビレール / 全文検索ダイアログ / 横断キュレーション一覧が共有する
#: 「ライブラリ基準の選び方」と相対成分計算。実体は Qt 非依存の
#: :mod:`snappix.common.fsutil` にあり（UIレビュー 2026-08-28 N-49 —
#: ``user_meta`` がワーカースレッドから同じ規則を使うため）、ここは歴史的な
#: import 経路を保つための再エクスポート。
_relative_parts = relative_parts


def path_segments(
    root: Path,
    library_bases: list[tuple[Path, str]] | None = None,
) -> list[Segment]:
    """Split *root* into an ordered list of navigable :class:`Segment`.

    When *library_bases* (each ``(base_path, display_label)``) is given and one
    of them is an ancestor-or-self of *root*, the trail is shown **relative to
    that library root**: the first segment is the library root itself (rendered
    with *display_label* — e.g. the Japanese 「ライブラリ」 for the default
    library, UIレビュー #6) and the following segments descend to *root*.  The
    **shallowest (outermost)** matching base wins when several nest
    (UIレビュー 07-25 #54): 登録ライブラリのサブフォルダもライブラリ登録
    されている構成で最も深い基準を採ると、そこへ入った瞬間にパンくずが
    「ここが最上位」へ再ルート化し、外側のライブラリへ戻る道筋が画面から
    消えていた — 外側基準なら道筋が連続する（閉じ込め方針は下記のまま）。
    This keeps
    the out-of-library system ancestors (``/`` … ``/home`` …) out of the trail
    and the ``…`` overflow menu, so a click can never wander outside the
    library (UIレビュー #5).

    When no base matches (the current folder sits outside every library root),
    it falls back to the absolute trail: the first segment is the filesystem
    anchor (drive on Windows, ``/`` on POSIX) and each subsequent segment adds
    one path component.  The last segment is always *root* itself.

    Operates on *root*'s own flavour (its ``.parts`` / ``/`` operator) rather
    than re-wrapping in the OS-native ``Path`` — keeping it cross-platform
    testable (a ``PurePosixPath`` stays POSIX even when run on Windows).
    """
    if library_bases:
        # 基準の選び方は :func:`pick_library_base` に一本化（UIレビュー07-25
        # 追修 — 検索ダイアログの「検索対象」表記と規則を共有する）。
        best = pick_library_base(root, library_bases)
        if best is not None:
            base, label, rel = best
            segments = [Segment(label, base)]
            accum = base
            for part in rel:
                accum = accum / part
                segments.append(Segment(part, accum))
            return segments
    parts = root.parts
    if not parts:
        return [Segment(str(root), root)]
    segments = []
    # ``parts[0]`` is the anchor: ``"C:\\"`` / ``"\\\\srv\\share\\"`` on Windows,
    # ``"/"`` on POSIX.  Label it verbatim (trailing separator trimmed for
    # readability unless it IS the separator, e.g. the POSIX root "/").
    anchor = parts[0]
    anchor_label = anchor.rstrip("\\/") or anchor
    accum = root.__class__(anchor)
    segments.append(Segment(anchor_label, accum))
    for part in parts[1:]:
        accum = accum / part
        segments.append(Segment(part, accum))
    return segments


@dataclass(frozen=True)
class BreadcrumbPlan:
    """How to render a breadcrumb given the available width.

    * ``collapsed`` — the leading ancestor segments folded behind a ``…``
      button (empty when everything fits).  Listed in path order.
    * ``visible`` — the segments to render inline, in path order.  Always
      contains at least the final (current-folder) segment.
    """

    collapsed: list[Segment]
    visible: list[Segment]


def plan_breadcrumb(
    segments: list[Segment],
    seg_widths: list[int],
    available: int,
    *,
    ellipsis_width: int,
    sep_width: int,
) -> BreadcrumbPlan:
    """Decide which leading segments to fold behind a ``…`` button.

    Pure width arithmetic (no Qt) so it's unit-testable.  *seg_widths* is the
    pixel width of each segment button (same length / order as *segments*);
    *sep_width* is the width of one ``>`` separator; *ellipsis_width* is the
    width of the ``…`` button.  We always keep the final segment (current
    folder) and drop leading segments — one at a time — until the row fits, or
    only the last segment remains.

    Returns the full set visible (no ``…``) when everything fits.
    """
    n = len(segments)
    if n == 0:
        return BreadcrumbPlan([], [])
    if n == 1:
        return BreadcrumbPlan([], list(segments))

    def total(count_visible: int, with_ellipsis: bool) -> int:
        # Width of the *count_visible* trailing segments plus the separators
        # between them, optionally prefixed by the ``…`` button + a separator.
        vis = seg_widths[n - count_visible:]
        width = sum(vis) + sep_width * (count_visible - 1)
        if with_ellipsis:
            width += ellipsis_width + sep_width
        return width

    # First: does the whole trail fit with no collapsing?
    if total(n, with_ellipsis=False) <= available:
        return BreadcrumbPlan([], list(segments))

    # Otherwise fold leading segments.  Keep as many trailing segments as fit
    # once the ``…`` button is accounted for; never fewer than 1 (the current
    # folder always shows even if it must overflow).
    keep = 1
    for count in range(n - 1, 0, -1):
        if total(count, with_ellipsis=True) <= available:
            keep = count
            break
    split = n - keep
    return BreadcrumbPlan(list(segments[:split]), list(segments[split:]))


class BreadcrumbBar(QWidget):
    """A clickable path breadcrumb that collapses to fit its width.

    Emits :data:`navigate` with the target :class:`Path` when the user clicks
    an ancestor segment (or picks one from the ``…`` overflow menu).  The
    trailing segment (current folder) is bold, non-clickable.  A trailing
    ``count`` label (e.g. ``(42 件)``) is kept separate so it survives the
    collapse logic.  The whole widget reports a tiny minimum width so it never
    forces the left pane wider (mirrors the old ``_ElidedLabel`` contract).
    """

    navigate = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[Segment] = []
        self._count_text = ""
        self._full_path = ""
        # Library roots to relativise the trail against (UIレビュー #5/#6).
        # Each ``(base_path, display_label)``; the window keeps this in sync via
        # :meth:`set_library_bases`.  ``_root_path`` is the last path handed to
        # :meth:`set_path`, so a bases change can re-render the current trail.
        self._library_bases: list[tuple[Path, str]] = []
        self._root_path: Path | None = None
        # Non-path "current location" label (UIレビュー 07-25 #60).  When set, the
        # bar renders this single crumb instead of a filesystem trail — see
        # :meth:`set_virtual_crumb`.
        self._virtual_label: str | None = None
        # Cached width measurements for the current segment set + font.  These
        # depend only on the segments and font metrics (not the widget width),
        # so they're computed once per ``set_path`` and reused on every resize
        # instead of re-creating throwaway QToolButtons per resize event
        # (splitter drags fire resizeEvent continuously).
        self._seg_widths: list[int] = []
        self._ellipsis_width = 0
        self._sep_width = 0
        # Signature of what the row currently renders (#68).  ``_relayout`` is
        # called on every resizeEvent tick of a splitter drag, but the rendered
        # row only changes when the collapse plan / elided label / count / font
        # / palette changes — when the signature is unchanged the existing
        # widgets are kept instead of destroying and rebuilding all of them.
        # ``_clear_row`` invalidates it, so any other teardown path is safe.
        self._render_key: tuple | None = None
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        # Report a tiny minimum width so a long path can't widen the pane; the
        # collapse logic in resizeEvent adapts to whatever width we're given.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    # ------------------------------------------------------------- public API

    def set_path(self, root: Path, count_text: str = "") -> None:
        """Show *root* as breadcrumb segments with an optional *count_text*.

        *count_text* (e.g. ``"(42 件)"``) is appended after the last segment
        and is not part of the clickable trail.
        """
        root = Path(root)
        self._virtual_label = None
        self._root_path = root
        self._segments = path_segments(root, self._library_bases)
        self._count_text = count_text
        self._full_path = str(root)
        self.setToolTip(self._full_path)
        self._remeasure()
        self._relayout()

    def set_virtual_crumb(self, label: str, count_text: str = "") -> None:
        """Show *label* as the sole, non-navigable crumb (UIレビュー 07-25 #60).

        For a "current location" that is not a folder — the cross-library
        スター付き一覧 / あとで見る一覧.  Those overlays used to leave the trail of
        whatever folder the user happened to be standing in on screen, so the
        breadcrumb (the primary answer to 「いまどこ？」) actively lied while a
        list was showing.  The crumb is styled like any trailing current segment
        (bold, disabled) and :meth:`set_count_text` keeps working on it, so the
        count stays live.  :meth:`set_path` / :meth:`set_plain_text` clear it.
        """
        self._virtual_label = label
        self._segments = []
        self._root_path = None
        self._count_text = count_text
        self._full_path = label
        self.setToolTip(label)
        self._relayout()

    def set_library_bases(self, bases: list[tuple[Path, str]]) -> None:
        """Set the library roots the trail is shown relative to (UIレビュー #5/#6).

        Each entry is ``(base_path, display_label)``.  Re-renders the current
        trail in place so a change (registering / managing libraries) takes
        effect immediately, not only on the next folder load.
        """
        self._library_bases = list(bases)
        if self._root_path is not None:
            self.set_path(self._root_path, self._count_text)

    def library_bases(self) -> list[tuple[Path, str]]:
        """The library roots currently in effect (copy).

        The trail is the single owner of this list on the pane side — anything
        else that needs to render a path *relative to a library* (the
        cross-library curation tile captions, UIレビュー 2026-08-28 N-49) reads
        it from here instead of keeping a second copy that the next
        「ライブラリを管理…」 would silently leave stale.
        """
        return list(self._library_bases)

    def has_trail(self) -> bool:
        """True once a real location is rendered (a trail, or a virtual crumb).

        ``set_plain_text`` (transient 「読み込み中…」 states) clears both, so
        callers can use this to avoid stomping the transient text with a
        count-only update.  A virtual crumb (UIレビュー 07-25 #60) counts: it is a
        settled current location, and its count must stay live.
        """
        return bool(self._segments) or self._virtual_label is not None

    def count_text(self) -> str:
        """The trailing count suffix currently rendered (``""`` when none).

        UIレビュー07-25 追修: 詳細検索側が件数表記を書き直すとき、ホストが
        付けた「(… N 件を非表示中)」等の付記を取りこぼさずに引き継げるように
        読み取り口を公開する（以前は非公開属性を覗くしかなかった）。
        """
        return self._count_text

    def set_count_text(self, count_text: str) -> None:
        """Update only the trailing count suffix of the current location."""
        if not self.has_trail() or count_text == self._count_text:
            return
        self._count_text = count_text
        self._relayout()

    def set_plain_text(self, text: str) -> None:
        """Show *text* verbatim (no segmentation) — used for transient states
        like ``読み込み中: …`` where the path isn't a navigable trail yet."""
        self._virtual_label = None
        self._segments = []
        self._count_text = ""
        self._full_path = text
        self._root_path = None
        self.setToolTip(text)
        self._clear_row()
        label = QLabel(text)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._layout.addWidget(label, 1)

    # ------------------------------------------------------------- internals

    def _clear_row(self) -> None:
        self._render_key = None
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _seg_button(self, seg: Segment, *, current: bool) -> QToolButton:
        btn = QToolButton()
        btn.setText(seg.label)
        btn.setAutoRaise(True)
        btn.setToolTip(str(seg.path))
        if current:
            # Trailing (current-folder) segment: bold, non-clickable.
            font = btn.font()
            font.setBold(True)
            btn.setFont(font)
            btn.setEnabled(False)
            btn.setStyleSheet(
                "QToolButton { border: none; }"
                "QToolButton:disabled { color: palette(text); }"
            )
        else:
            btn.setStyleSheet("QToolButton { border: none; }")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, p=seg.path: self.navigate.emit(p))
        return btn

    def _remeasure(self) -> None:
        """Recompute + cache the width of each segment button, ``…`` and ``>``.

        Buttons are sized off their sizeHint so the plan matches what will
        actually be laid out.  Called only when the segment set or font
        changes (``set_path`` / font-change event) — NOT on every resize, so
        splitter drags don't churn through throwaway QToolButtons.  Results
        land in ``self._seg_widths`` / ``_ellipsis_width`` / ``_sep_width``.
        """
        fm = self.fontMetrics()
        seg_widths: list[int] = []
        for i, seg in enumerate(self._segments):
            btn = self._seg_button(seg, current=(i == len(self._segments) - 1))
            w = btn.sizeHint().width()
            btn.deleteLater()
            seg_widths.append(w)
        self._seg_widths = seg_widths
        # +6 accounts for the separator label's own left/right content
        # margins (see ``_sep_label``) so the collapse plan doesn't
        # under-estimate the row's actual width and let it overflow.
        self._sep_width = fm.horizontalAdvance(" > ") + 6
        self._ellipsis_width = fm.horizontalAdvance(" … ") + 8

    def _style_key(self) -> tuple:
        """Font / palette inputs the rendered row depends on (#68).

        Part of ``_render_key`` so a theme or font change still rebuilds the
        row (the current segment pins a bold font and the separators bake the
        blended colour into a stylesheet — neither follows a later change).
        """
        return (
            self.font().toString(),
            self.palette().color(QPalette.Text).rgba(),
        )

    def _relayout(self) -> None:
        if self._virtual_label is not None:
            key = ("virtual", self._virtual_label, self._count_text,
                   self._style_key())
            if key == self._render_key:
                return
            self._clear_row()
            # 単一クラム（横断一覧など）— 末尾セグメントと同じ「現在地」の
            # 見た目にし、クリックできないことも同じ手段で示す (#60)。
            btn = self._seg_button(
                Segment(self._virtual_label, Path(self._virtual_label)),
                current=True,
            )
            btn.setToolTip(self._virtual_label)
            self._layout.addWidget(btn)
            if self._count_text:
                self._layout.addWidget(QLabel(self._count_text))
            self._layout.addStretch(1)
            self._render_key = key
            return
        if not self._segments:
            self._clear_row()
            return
        # Uses the cached measurements from the last ``_remeasure`` — only the
        # available width changes on a resize, so the plan is recomputed but
        # the segment widths aren't.
        fm = self.fontMetrics()
        count_w = fm.horizontalAdvance(self._count_text) + 6 if self._count_text else 0
        available = max(0, self.width() - count_w)
        plan = plan_breadcrumb(
            self._segments,
            self._seg_widths,
            available,
            ellipsis_width=self._ellipsis_width,
            sep_width=self._sep_width,
        )
        final = self._segments[-1]
        # When even the kept trailing segments overflow (the plan's keep-1
        # fallback), the row used to clip at the widget edge and the current
        # folder name became unreadable (UIレビュー #2).  Middle-elide the
        # final segment's label instead so its head and tail stay visible;
        # the tooltip still carries the full path.
        used = self._ellipsis_width + self._sep_width if plan.collapsed else 0
        n_vis = len(plan.visible)
        used += sum(self._seg_widths[len(self._segments) - n_vis : -1])
        used += self._sep_width * (n_vis - 1)
        room_for_final = available - used
        final_text = final.label
        if self._seg_widths[-1] > room_for_final:
            pad = self._seg_widths[-1] - fm.horizontalAdvance(final.label)
            final_text = fm.elidedText(
                final.label, Qt.ElideMiddle, max(40, room_for_final - pad),
            )
        # Nothing visible would change → keep the existing widgets instead of
        # destroying and rebuilding the whole row (#68): resizeEvent fires per
        # tick of a splitter drag and only the width feeds the plan.
        key = (
            "trail",
            tuple((s.label, str(s.path)) for s in plan.collapsed),
            tuple((s.label, str(s.path)) for s in plan.visible),
            final_text,
            self._count_text,
            self._style_key(),
        )
        if key == self._render_key:
            return
        self._clear_row()
        if plan.collapsed:
            self._layout.addWidget(self._ellipsis_button(plan.collapsed))
            self._layout.addWidget(self._sep_label())
        for seg in plan.visible:
            # A visible segment is "current" only if it's the final one overall.
            is_current = seg is final
            btn = self._seg_button(seg, current=is_current)
            if is_current:
                btn.setText(final_text)
            self._layout.addWidget(btn)
            if not is_current:
                self._layout.addWidget(self._sep_label())
        if self._count_text:
            count_label = QLabel(self._count_text)
            self._layout.addWidget(count_label)
        self._layout.addStretch(1)
        self._render_key = key

    def _sep_label(self) -> QLabel:
        sep = QLabel(">")
        # ``palette(mid)`` alone can end up close to the window background
        # in both themes (Fusion derives Mid from Button, which is dim in
        # the dark palette and only mildly darker than Window in the light
        # one) — see theme.py's dark/light palettes.  Blending the current
        # Text colour toward Window at a fixed alpha instead guarantees a
        # legible, theme-independent contrast level in both directions
        # without hardcoding an actual colour value.
        sep.setStyleSheet(f"QLabel {{ color: {self._separator_color_css()}; }}")
        sep.setContentsMargins(3, 0, 3, 0)
        return sep

    def _separator_color_css(self) -> str:
        """Text colour blended toward Window at ~55% opacity, as ``rgba()``.

        Recomputed from the *current* palette each time a separator is
        built (breadcrumbs are rebuilt on every resize/relayout and on
        theme switches, which reapply the app palette before the next
        ``set_path``/resize), so light/dark/system all get an appropriate
        value without any hardcoded colour.
        """
        pal = self.palette()
        text = pal.color(QPalette.Text)
        return f"rgba({text.red()}, {text.green()}, {text.blue()}, 140)"

    def _ellipsis_button(self, collapsed: list[Segment]) -> QToolButton:
        btn = QToolButton()
        btn.setText("…")
        btn.setAutoRaise(True)
        btn.setToolTip(t("viewer.breadcrumb.ancestor_folders"))
        menu = QMenu(btn)
        # QMenu の toolTipsVisible は既定 false — これが無いと下の
        # setToolTip は 1 つも表示されない（折り畳まれた祖先はパス成分 1 個
        # ずつでしか区別できないので、フルパスのツールチップが要る）。
        menu.setToolTipsVisible(True)
        for seg in collapsed:
            act = menu.addAction(seg.label or str(seg.path))
            act.setToolTip(str(seg.path))
            act.triggered.connect(lambda _=False, p=seg.path: self.navigate.emit(p))
        btn.setMenu(menu)
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setStyleSheet("QToolButton { border: none; } QToolButton::menu-indicator { image: none; }")
        return btn

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        # Re-plan the collapse on width changes (splitter drag / window resize).
        # Reuses the cached segment widths — no button churn per resize.
        if self.has_trail():
            self._relayout()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if not self._segments:
            return
        # The cached segment widths are font-dependent, so re-measure when the
        # widget font changes (e.g. a theme switch reapplying the app font).
        if event.type() == QEvent.Type.FontChange:
            self._remeasure()
            self._relayout()
            return
        # テーマ切替はパレット・QSS を差し替えるがアプリフォントは変えないので
        # FontChange は飛ばない（レビュー 2026-08-27 #113）。``_sep_label`` は
        # 区切り記号の色をその時点のパレットから rgba へ焼き込むため、再構築が
        # 走らないとダークの地色の上に旧テーマ（ライト）の色が残り、コントラスト
        # 比 1.17:1 で事実上見えなくなっていた。幅は変わらないので ``_remeasure``
        # は不要 — ``_style_key`` がパレットを含むので ``_relayout`` だけで行が
        # 作り直される。
        if event.type() in (
            QEvent.Type.PaletteChange, QEvent.Type.StyleChange,
        ):
            self._relayout()


__all__ = [
    "BreadcrumbBar",
    "BreadcrumbPlan",
    "Segment",
    "path_segments",
    "pick_library_base",
    "plan_breadcrumb",
    "relative_parts",
]
