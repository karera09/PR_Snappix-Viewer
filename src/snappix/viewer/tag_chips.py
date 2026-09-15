"""Chip-style multi-tag input for the viewer's advanced-search panel.

The AI-tag / semantic search box is a whitespace-separated AND query
(``-word`` excludes).  A plain ``QLineEdit`` makes it hard to see where one
tag ends and the next begins, and editing an earlier tag means fiddling in the
middle of a long string.  :class:`TagChipsInput` renders each confirmed tag as
a rounded, removable chip and keeps a single ``QLineEdit`` for typing the next
one — the tokens flow left-to-right and wrap to new rows when the width runs
out.

Unlike a naive "edit always trails the chips" layout, the editor can be moved
*between* chips: clicking the empty space of the flow (or a chip) re-seats the
editor at that position, so a confirmed tag lands where the caret is rather
than always at the end.  Chips can be reordered by drag-and-drop and removed /
toggled between include/exclude via a right-click context menu.

The public API is deliberately a drop-in superset of the ``QLineEdit`` methods
PostGrid used on the old ``tag_input`` so the existing persistence
(``save_tag_settings`` / ``restore_tag_settings``), the nav-history
``SearchSnapshot`` round-trip and the debounced ``_kick_tag_scan`` path all
keep working against a plain space-joined string:

* :meth:`text` — chips + the in-progress edit text, in visual order, joined by
  single spaces.
* :meth:`set_text` — split on whitespace and re-chip (the inverse of ``text``).
* ``textChanged(str)`` — emitted on every chip add / remove / reorder and every
  edit keystroke, carrying the same string :meth:`text` returns.
* ``textEdited(str)`` — emitted only for user edits of the trailing line edit
  (mirrors ``QLineEdit.textEdited``), carrying just the in-progress token — the
  tag completer keys off this.
* :meth:`set_completer` — attach a ``QCompleter`` to the inner line edit.
* ``setEnabled`` — inherited unchanged: Qt propagates the disabled state to
  the child chips and the inner edit, so the whole widget greys out.
* :meth:`setPlaceholderText` — forwarded to the inner edit (shown only when
  there are no chips, like the original single field).

Nothing here reads or writes ``ViewerState`` — the string representation is the
only contract, so ``ViewerState.tag_search_query`` stays byte-compatible.

Duplicate tags ARE allowed (a user may add the same tag twice to double its
weight in the semantic query — see the dedupe note in ``scan_worker.py`` for
how the strict-AND tag path guards against the resulting inflated tag count).
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from ..common.i18n import t
from ..common.ui import current_tokens, rgba

#: MIME type carrying the source chip's flow index during a reorder drag.
_CHIP_MIME = "application/x-snappix-tagchip"


class FlowLayout(QLayout):
    """A left-to-right layout that wraps its items onto new rows.

    Qt ships no flow layout, so this is the well-known minimal implementation
    (mirrors the Qt "Flow Layout" example): lay items out horizontally, break
    to a new line when the next item would overflow the available width, and
    report a height-for-width so the containing widget resizes to fit the wrap.

    Shared across modules — this file's chip input and ``tag_browser``'s
    「現在の検索条件」 strip both lay out with it — so the name is public: an
    underscore-private name imported from another module reads as a leak of
    this widget's internals rather than the shared part it actually is.
    """

    def __init__(
        self, parent: QWidget | None = None, *, spacing: int = 4,
    ) -> None:
        super().__init__(parent)
        self._items: list = []
        self._hspace = spacing
        self._vspace = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # type: ignore[override]
        self._items.append(item)

    def insertItem(self, index: int, item) -> None:
        """Insert *item* at *index* (used to re-seat the editor between chips)."""
        index = max(0, min(index, len(self._items)))
        self._items.insert(index, item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, index: int):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # type: ignore[override]
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # type: ignore[override]
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        m = self.contentsMargins()
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        line_height = 0
        right = rect.right() - m.right()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width()
            if next_x > right and line_height > 0:
                # Wrap: the item would overflow this row, start a new one.
                x = rect.x() + m.left()
                y = y + line_height + self._vspace
                next_x = x + hint.width()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + self._hspace
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class _TagChip(QWidget):
    """A single rounded, removable tag chip.

    ``exclude`` (``-``-prefixed tokens) get a red-tinted background so a
    negative filter reads differently from a positive one at a glance.  The
    ``×`` button emits :sig:`removed` carrying this chip so the container can
    drop it.  A right-click asks the container (:sig:`context_requested`) for a
    delete / toggle-exclude menu, and a left-drag emits :sig:`drag_started` so
    the container can start a reorder drag.
    """

    removed = Signal(object)
    context_requested = Signal(object, QPoint)  # (chip, global pos)
    drag_started = Signal(object)  # chip
    clicked = Signal(object)  # chip (seat the editor next to it)

    def __init__(
        self, token: str, *, exclude: bool, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.token = token
        self.exclude = exclude
        self._press_pos: QPoint | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(2)
        # Show the bare tag on the chip; the leading '-' is conveyed by colour.
        display = token[1:] if exclude and token.startswith("-") else token
        self._label = QLabel(display)
        self._label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(self._label)
        close = QToolButton()
        close.setText("×")
        close.setAutoRaise(True)
        close.setCursor(Qt.PointingHandCursor)
        close.setFocusPolicy(Qt.NoFocus)
        close.setStyleSheet("QToolButton { border: none; padding: 0 2px; }")
        close.clicked.connect(lambda: self.removed.emit(self))
        layout.addWidget(close)
        self._apply_style()
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)
        # 掴めることをカーソルで予告する（N-70）: チップの主操作は「ドラッグで
        # 並べ替え」で、``PointingHandCursor`` は押下（リンク / ボタン）の図像
        # なのでそれが読めなかった。掴む前 = OpenHand / 掴んでいる間 =
        # ClosedHand（``image_view`` のパンと同じ対）。× ボタンは押下対象なので
        # ``PointingHandCursor`` のまま。
        self.setCursor(Qt.OpenHandCursor)
        # 隠れ機能（ドラッグ並べ替え / 右クリックメニュー / 空欄 Backspace）を
        # 1 行で予告する。内容は状態に依らないので ``set_token`` では触らない。
        self.setToolTip(t("viewer.tag_chips.chip_tooltip"))

    def _apply_style(self) -> None:
        # Rounded pill; exclude chips read as a red-tinted "not".  Colours are
        # translucent token overlays (accent = include, danger = exclude) so
        # they follow the design-system palette on both themes.
        tok = current_tokens()
        bg = rgba(tok.danger, 0.28) if self.exclude else rgba(tok.accent, 0.22)
        css = f"_TagChip {{ background: {bg}; border-radius: 8px; }}"
        # 同一文字列の再設定は StyleChange を無駄撃ちするだけなので避ける。
        if css != self.styleSheet():
            self.setStyleSheet(css)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        # #191: チップは ``ViewerState.tag_search_query`` から復元され、
        # ポップオーバーを閉じている間も生き続けるので "transient" ではない。
        # インライン stylesheet に焼き込んだ色はテーマ切替で取り残されるため、
        # 兄弟の ``tag_browser._ReadOnlyChip`` と同じくパレット変更で塗り直す
        # （``StyleChange`` は setStyleSheet 自身が発火させるので拾わない —
        # 無限再帰になる）。
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.ThemeChange,
        ):
            self._apply_style()
        super().changeEvent(event)

    def set_token(self, token: str) -> None:
        """Update the chip's token (used by the include/exclude toggle)."""
        self.token = token
        self.exclude = token.startswith("-") and len(token) > 1
        display = token[1:] if self.exclude and token.startswith("-") else token
        self._label.setText(display)
        self._apply_style()

    # -- interaction: click to seat, right-click menu, left-drag to reorder --

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._press_pos is not None
            and event.buttons() & Qt.LeftButton
            and (event.pos() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._press_pos = None
            self.drag_started.emit(self)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            # A press+release without exceeding the drag threshold is a click:
            # seat the editor next to this chip.
            self._press_pos = None
            self.clicked.emit(self)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        self.context_requested.emit(self, event.globalPos())
        event.accept()


class _ChipLineEdit(QLineEdit):
    """The inline editor; Backspace on an empty field removes the chip to its
    left (nav uses the editor's current flow position, not "the last chip")."""

    backspace_on_empty = Signal()
    #: Emitted when the editor loses focus, so the container can commit a
    #: half-typed token (the "focus-out confirms" contract).  Fires on the inner
    #: line edit — the container QWidget itself never receives focus, so a
    #: container-level ``focusOutEvent`` would never fire.
    lost_focus = Signal()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Backspace and not self.text():
            self.backspace_on_empty.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        super().focusOutEvent(event)
        self.lost_focus.emit()


class TagChipsInput(QWidget):
    """Space-separated AND tag query rendered as reorderable chips + an editor.

    See the module docstring for the ``QLineEdit``-compatible public API this
    exposes; PostGrid swaps this in for the old ``tag_input`` line edit without
    changing the string-based persistence / search plumbing.

    The editor widget lives *inside* the flow at :attr:`_edit_index`, so a
    confirmed token is inserted at the caret position rather than always at the
    end.  Chips are reorderable (drag-and-drop) and support a right-click
    delete / include-exclude-toggle menu.
    """

    #: Emitted whenever the effective query string changes (chip add / remove /
    #: reorder or an edit keystroke).  Carries :meth:`text`.
    textChanged = Signal(str)
    #: Emitted only for user edits of the inner line edit (like
    #: ``QLineEdit.textEdited``); carries the in-progress token so the tag
    #: completer can suggest against it.
    textEdited = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: Chips in visual order (index-aligned with the flow's chip items).
        self._chips: list[_TagChip] = []
        #: Optional provider of known-tag candidates for a prefix, used to
        #: auto-chip an exact unique match while typing.  Returns a list of
        #: ``(tag, count)`` (only the tag names are consulted).  ``None`` until
        #: :meth:`set_suggestion_provider` is called.
        self._suggest: Callable[[str], list[tuple[str, int]]] | None = None
        #: The provider's result cap.  When a prefix returns exactly this many
        #: candidates the window is *possibly truncated* — a longer tag sharing
        #: the prefix could be sunk below the cap (the count-ordered suggest
        #: window), so :meth:`_should_auto_chip` refuses to auto-chip on such a
        #: saturated window to avoid stealing a still-in-progress token (#177).
        self._suggest_window_cap = 20
        self._layout = FlowLayout(self, spacing=4)
        self._edit = _ChipLineEdit()
        self._edit.setFrame(False)
        self._edit.setMinimumWidth(80)
        self._edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._edit.textEdited.connect(self._on_edit_text_edited)
        self._edit.returnPressed.connect(self._commit_current_token)
        self._edit.backspace_on_empty.connect(self._remove_chip_before_edit)
        self._edit.lost_focus.connect(self._commit_current_token)
        self._layout.addWidget(self._edit)
        #: Flow index of the editor (always == len(chips) initially; moves when
        #: the user clicks between chips).
        self._edit_index = 0
        # Accept internal chip-reorder drops.
        self.setAcceptDrops(True)
        # フォーカスはこのコンテナで止まらず内側のエディタへ抜ける（N-85）。
        # 素の ``QWidget`` は ``Qt.NoFocus`` なので ``setFocus()`` が無視され、
        # Ctrl+Shift+T / ツールバーの AIタグチップ / チップのクリックといった
        # 全入口が「ポップオーバーは開くがカーソルは入らない」になっていた。
        # ``_edit`` は ``__init__`` で 1 度だけ作られ ``_rebuild_flow`` でも
        # 差し替わらないので、プロキシは全経路で生き続ける。
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocusProxy(self._edit)

    # --------------------------------------------------------- QLineEdit shim

    def setPlaceholderText(self, text: str) -> None:
        self._edit.setPlaceholderText(text)

    def setClearButtonEnabled(self, enabled: bool) -> None:
        # No clear button on a chips widget (per-chip × already removes tags);
        # accepted for API compatibility with the QLineEdit it replaces.
        pass

    def setCompleter(self, completer) -> None:
        self._edit.setCompleter(completer)

    def completer(self):
        return self._edit.completer()

    def line_edit(self) -> QLineEdit:
        """The inner ``QLineEdit`` (needed to seat a custom completer on it)."""
        return self._edit

    def set_suggestion_provider(
        self,
        provider: Callable[[str], list[tuple[str, int]]] | None,
        *,
        window_cap: int | None = None,
    ) -> None:
        """Install a known-tag lookup used for auto-chipping exact matches.

        When the in-progress token exactly matches a single known tag (and that
        tag is NOT a strict prefix of a longer candidate — otherwise the user
        couldn't keep typing, e.g. ``cat`` when ``cats`` also exists), the token
        is auto-committed to a chip.  ``None`` disables auto-chipping (only
        space / Enter / focus-out / completer selection commit a token).

        ``window_cap`` is the maximum number of candidates *provider* returns
        for a prefix (its ``LIMIT``).  Auto-chipping is suppressed when a prefix
        returns exactly that many candidates, since a longer prefix-sharing tag
        may have been truncated out of the count-ordered window (#177).
        """
        self._suggest = provider
        if window_cap is not None and window_cap > 0:
            self._suggest_window_cap = int(window_cap)

    # -------------------------------------------------------------- text API

    def text(self) -> str:
        """The full query: chip tokens then the in-progress edit, space-joined.

        Tokens are emitted in visual order — the editor's position within the
        flow is respected, so a partial token typed *between* two chips lands in
        the middle.  This is the exact string PostGrid persists and re-parses,
        so it stays whitespace-AND compatible with ``_parse_query``.
        """
        tail = self._edit.text().strip()
        parts: list[str] = []
        for i, chip in enumerate(self._chips):
            if i == self._edit_index and tail:
                parts.append(tail)
            parts.append(chip.token)
        if self._edit_index >= len(self._chips) and tail:
            parts.append(tail)
        return " ".join(parts)

    def setText(self, text: str) -> None:
        """Alias so callers using the ``QLineEdit`` name keep working."""
        self.set_text(text)

    def set_text(self, text: str) -> None:
        """Replace the whole query: split on whitespace, chip every token.

        The trailing edit is cleared — a restored / persisted query is fully
        confirmed, so nothing is left half-typed.  The editor is re-seated at
        the end.
        """
        self._clear_chips()
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)
        for token in text.split():
            self._add_chip_at(len(self._chips), token)
        self._seat_edit_at(len(self._chips))
        self._emit_changed()

    # ------------------------------------------------------------- internals

    def add_tag(self, tag: str) -> None:
        """Add *tag* as a confirmed chip at the editor position and emit.

        Used by the tag-browser dialog / detail window to inject a chosen tag
        into the query.  Keeps ``-word`` exclusion tokens intact (so
        ``add_tag("-cat")`` chips an exclusion) and emits ``textChanged`` so the
        debounced search re-runs.  Duplicates ARE allowed — adding the same tag
        twice is meaningful (it doubles that tag's weight in the semantic query),
        so double-clicking a browser row stacks a second chip on purpose.
        """
        tag = tag.strip()
        if not tag or tag == "-":
            return
        # _add_chip_at already advances the editor index past the new chip, so
        # the caret stays after the just-inserted tag.
        self._add_chip_at(self._edit_index, tag)
        self._emit_changed()

    def _on_edit_text_edited(self, text: str) -> None:
        # Space confirms the current token into a chip (like Enter); otherwise
        # the keystroke just extends the in-progress token.
        if " " in text:
            head, _, tail = text.rpartition(" ")
            for token in head.split():
                # _add_chip_at inserts at the editor slot and shifts the editor
                # index forward, so the committed chip lands just before the
                # caret — no manual index bump here.
                self._add_chip_at(self._edit_index, token)
            self._edit.blockSignals(True)
            self._edit.setText(tail)
            self._edit.blockSignals(False)
        else:
            # Auto-chip an exact, unambiguous match against the known tags so a
            # completed tag becomes a chip without needing a space.  Guarded so
            # a tag that is a prefix of a longer candidate ("cat" vs "cats")
            # stays editable.
            token = text.strip()
            if token and self._should_auto_chip(token):
                self._add_chip_at(self._edit_index, token)
                self._edit.blockSignals(True)
                self._edit.clear()
                self._edit.blockSignals(False)
        self.textEdited.emit(self._edit.text())
        self._emit_changed()

    def _should_auto_chip(self, token: str) -> bool:
        """Whether *token* should auto-convert to a chip while typing.

        True only when a suggestion provider is installed and *token* (its bare
        form, minus any ``-`` exclusion / ``~`` OR prefix) matches exactly one
        known tag AND is not a strict prefix of any other candidate — otherwise
        the user could not keep typing a longer tag (``cat`` while ``cats``
        exists).  The prefix is restored on the chip because ``_add_chip_at``
        receives the original *token* (item 25 — ``~cat`` previously never
        auto-chipped because the provider saw the raw ``~cat``).
        """
        if self._suggest is None:
            return False
        bare = token[1:] if token[:1] in ("-", "~") and len(token) > 1 else token
        if not bare:
            return False
        low = bare.casefold()
        try:
            candidates = [t for t, _c in self._suggest(bare)]
        except Exception:  # pragma: no cover (best-effort)
            return False
        # A saturated window may hide a longer prefix-sharing tag below its cap
        # (candidates are ordered by count, not length), so refuse to auto-chip
        # — the user can still commit with space/Enter.  Errs toward NOT
        # stealing an in-progress token (#177).
        if len(candidates) >= self._suggest_window_cap:
            return False
        exact = False
        for cand in candidates:
            cl = cand.casefold()
            if cl == low:
                exact = True
            elif cl.startswith(low):
                # A longer candidate shares this prefix — keep typing enabled.
                return False
        return exact

    def _commit_current_token(self) -> None:
        token = self._edit.text().strip()
        if token:
            self._add_chip_at(self._edit_index, token)
            self._edit.blockSignals(True)
            self._edit.clear()
            self._edit.blockSignals(False)
            self._emit_changed()

    def _add_chip_at(self, index: int, token: str) -> None:
        """Create a chip for *token* and insert it into ``_chips`` at *index*.

        ``self._chips`` is the single source of truth for order; the flow is
        rebuilt from it (chips + editor at ``_edit_index``) via
        :meth:`_rebuild_flow`, which sidesteps fragile incremental
        insert/take bookkeeping.  The editor index shifts right when a chip is
        inserted at or before it so the caret keeps its logical position.
        """
        token = token.strip()
        if not token or token == "-":
            return
        index = max(0, min(index, len(self._chips)))
        exclude = token.startswith("-") and len(token) > 1
        chip = _TagChip(token, exclude=exclude)
        chip.removed.connect(self._on_chip_removed)
        chip.context_requested.connect(self._on_chip_context)
        chip.drag_started.connect(self._on_chip_drag_started)
        chip.clicked.connect(self._on_chip_clicked)
        self._chips.insert(index, chip)
        if index <= self._edit_index:
            self._edit_index += 1
        self._rebuild_flow()

    def _rebuild_flow(self) -> None:
        """Re-lay the flow items from ``_chips`` + the editor at ``_edit_index``.

        Detaches every current layout item (without deleting the widgets — they
        are re-added) then appends chips and the editor in visual order.  Called
        after any structural change so a single, well-tested routine owns the
        flow order.
        """
        while self._layout.count():
            self._layout.takeAt(0)
        self._edit_index = max(0, min(self._edit_index, len(self._chips)))
        for i, chip in enumerate(self._chips):
            if i == self._edit_index:
                self._layout.addWidget(self._edit)
            self._layout.addWidget(chip)
        if self._edit_index >= len(self._chips):
            self._layout.addWidget(self._edit)
        self._layout.invalidate()
        self.updateGeometry()

    def _seat_edit_at(self, index: int) -> None:
        """Move the inline editor to flow *index* (0..len(chips))."""
        index = max(0, min(index, len(self._chips)))
        if index == self._edit_index:
            return
        self._edit_index = index
        self._rebuild_flow()

    def _on_chip_clicked(self, chip: _TagChip) -> None:
        # Clicking a chip seats the editor just after it, so the next confirmed
        # token lands there.
        if chip in self._chips:
            self._seat_edit_at(self._chips.index(chip) + 1)
            self._edit.setFocus()

    def _on_chip_removed(self, chip: _TagChip) -> None:
        self._detach_chip(chip)
        self._emit_changed()
        self._edit.setFocus()

    def _remove_chip_before_edit(self) -> None:
        # Backspace on the empty editor removes the chip immediately to its left.
        idx = self._edit_index - 1
        if 0 <= idx < len(self._chips):
            self._detach_chip(self._chips[idx])
            self._emit_changed()

    def _detach_chip(self, chip: _TagChip) -> None:
        if chip not in self._chips:
            return
        idx = self._chips.index(chip)
        self._chips.remove(chip)
        self._layout.removeWidget(chip)
        chip.setParent(None)
        chip.deleteLater()
        if idx < self._edit_index:
            self._edit_index -= 1
        self._rebuild_flow()

    def _clear_chips(self) -> None:
        for chip in list(self._chips):
            self._layout.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        self._edit_index = 0
        self._rebuild_flow()

    # ----------------------------------------------------- context menu (C-3)

    def _on_chip_context(self, chip: _TagChip, global_pos: QPoint) -> None:
        if chip not in self._chips:
            return
        menu = QMenu(self)
        # 「削除」(``common.action.delete``) はブックマーク等の**実体削除**と
        # 同じキーで、検索条件から 1 語外すだけの操作には強すぎた（N-103）。
        remove_act = menu.addAction(t("viewer.tag_chips.remove"))
        if chip.exclude:
            toggle_act = menu.addAction(t("viewer.tag_chips.toggle_to_include"))
        else:
            toggle_act = menu.addAction(t("viewer.tag_chips.toggle_to_exclude"))
        chosen = menu.exec(global_pos)
        # ``QMenu(self)`` is parented to this (long-lived) chips editor, so
        # exec() only hides it — without this the menu + its QAction set pile
        # up on the editor, one per right-click (same pattern as
        # file_list/_on_context_menu).  ``chosen`` stays usable: deleteLater
        # only posts a DeferredDelete, and the branches below compare Python
        # identity rather than touching the C++ object.
        menu.deleteLater()
        if chosen is remove_act:
            self._detach_chip(chip)
            self._emit_changed()
        elif chosen is toggle_act:
            self._toggle_chip_exclude(chip)

    def _toggle_chip_exclude(self, chip: _TagChip) -> None:
        if chip.exclude and chip.token.startswith("-"):
            chip.set_token(chip.token[1:])
        else:
            chip.set_token("-" + chip.token)
        self.updateGeometry()
        self._emit_changed()

    # --------------------------------------------- drag-and-drop reorder (C-3)

    def _on_chip_drag_started(self, chip: _TagChip) -> None:
        if chip not in self._chips:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_CHIP_MIME, str(self._chips.index(chip)).encode("ascii"))
        drag.setMimeData(mime)
        drag.setPixmap(chip.grab())
        chip.setCursor(Qt.ClosedHandCursor)
        drag.exec(Qt.MoveAction)
        # 並べ替えが成立したチップは ``_reorder_chip`` が作り直しているので、
        # まだ生きているとき（＝移動しなかったとき）だけ掴む前の図像へ戻す。
        if chip in self._chips:
            chip.setCursor(Qt.OpenHandCursor)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(_CHIP_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(_CHIP_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        mime = event.mimeData()
        if not mime.hasFormat(_CHIP_MIME):
            super().dropEvent(event)
            return
        try:
            src = int(bytes(mime.data(_CHIP_MIME)).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return
        dst = self._drop_index_at(event.position().toPoint())
        self._reorder_chip(src, dst)
        event.acceptProposedAction()

    def _drop_index_at(self, pos: QPoint) -> int:
        """Logical chip index the drop at *pos* should land before.

        Finds the first chip whose horizontal+vertical centre is past *pos*;
        dropping past every chip appends at the end.
        """
        for i, chip in enumerate(self._chips):
            geo = chip.geometry()
            if pos.y() < geo.bottom() and pos.x() < geo.center().x():
                return i
            if pos.y() < geo.top():
                return i
        return len(self._chips)

    def _reorder_chip(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self._chips)):
            return
        dst = max(0, min(dst, len(self._chips)))
        if dst == src or dst == src + 1:
            return  # no-op move
        chip = self._chips[src]
        token = chip.token
        # Rebuild by detaching the source then re-inserting at the adjusted slot
        # (dst shifts left by one when the source was before it).
        insert_at = dst - 1 if dst > src else dst
        self._detach_chip(chip)
        self._add_chip_at(insert_at, token)
        self._emit_changed()

    def _emit_changed(self) -> None:
        self.textChanged.emit(self.text())


__all__ = ["TagChipsInput"]
