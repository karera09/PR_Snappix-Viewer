"""Navigation-history primitives for the viewer window.

:class:`NavEntry` is one remembered position in the browser-style back /
forward navigation stacks, and :class:`HistoryMenus` hosts the long-press
←/→ history dropdowns (B-6).  Split out of ``main_window.py`` (#96).

The stack *semantics* (push on drill-down, shuttle the current position
between the two stacks on 戻る/進む, hard reset on ルート変更) intentionally
stay on :class:`~snappix.viewer.main_window.ViewerWindow`: they are
inseparable from ``set_root`` and form the window's directly-tested
navigation contract (``tests/test_viewer_navigation.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ..common.i18n import t

if TYPE_CHECKING:  # import for annotations only — no runtime dependency
    from .post_grid import SearchSnapshot


class SetRootResult(Enum):
    """``ViewerWindow.set_root`` の 3 つの結末.

    かつては ``bool`` で、``False`` が「行き先が消えていたので**何も変えて
    いない**」と「I07 プロンプトの『別のフォルダを開く』が内側で**別ルートへ
    遷移し、トレイルをハードリセットし終えた**」の 2 つを兼ねていた。判別は
    呼び出し側の ``self._root == root_before`` 比較へ外出しされ、規約は
    docstring にしか無かった（レビュー 2026-09-03 項目 #107 で実際に 1 回
    踏んでいる: ← が有効なまま残り、押すたび同じ「見つかりません」に落ちた）。

    ``__bool__`` が ``LANDED`` だけを真にするので ``if not set_root(...)``
    形の呼び出しは無改造のまま通り、「変えていない」を要求する側だけが
    ``is UNCHANGED`` で明示できる。
    """

    #: 新しいルートへ着地した。
    LANDED = "landed"
    #: 行き先が無く、窓の状態・トレイル・ペインは一切動いていない。
    UNCHANGED = "unchanged"
    #: 着地はしていないが、内側で**別のルートへ遷移済み**（トレイルはリセット済み）。
    REROUTED = "rerouted"

    def __bool__(self) -> bool:
        return self is SetRootResult.LANDED


class _SearchTarget(Protocol):
    """The slice of ``PostGrid`` a :class:`SearchTransition` drives."""

    def restore_search_state(self, snapshot: "SearchSnapshot") -> None: ...
    def clear_search_state(self) -> None: ...


@dataclass(frozen=True)
class SearchTransition:
    """The search post-processing to apply after a ``set_root`` re-root.

    ``set_root`` used to carry two correlated keyword args (``restore_search`` /
    ``clear_search``) plus inline precedence rules ("restore wins over clear";
    "a drill-down under clear-on-navigate forces a clear").  Bundling the
    *intent* here (#81) keeps ``set_root`` focused on swapping the root and
    refreshing the panes, moves the precedence into one tested place, and gives
    future post-navigation behaviours a typed home instead of yet another
    ``set_root`` boolean.

    Exactly one of the two intents takes effect, resolved at construction:

    * ``restore`` — a remembered :class:`SearchSnapshot` to re-apply (used by
      戻る/進む); wins over ``clear`` when both are requested.
    * ``clear`` — drop any active search so the destination shows its own
      contents (a drill-down away from a search, or a history hop that carried
      no search under clear-on-navigate).

    When neither is set the transition is a no-op and search persists — the
    historical behaviour when the clear-on-navigate setting is off.
    """

    restore: "SearchSnapshot | None" = None
    clear: bool = False

    def apply(self, target: _SearchTarget) -> None:
        """Apply the resolved intent to *target* (restore precedes clear).

        Call *after* the pane's own ``set_root`` has re-scoped any persistent
        search against the new root, so this runs last and wins.
        """
        if self.restore is not None:
            target.restore_search_state(self.restore)
        elif self.clear:
            target.clear_search_state()


@dataclass
class NavEntry:
    """One remembered position in the back / forward navigation stacks.

    ``root`` is the left-pane root; ``selected`` is the sub-folder that was
    selected at that root (restored on 戻る so the selection — not just the
    root — comes back); ``search`` is the search state that was active there,
    or ``None`` when no search was managed for this hop (either nothing was
    searched, or the clear-on-navigate setting is off); ``scroll`` is the
    left pane's vertical scroll offset, restored so 戻る/進む returns to the
    exact position (B-13); ``mode`` is the centre-stack UI mode
    (``"browse"``/``"stage"``) at that position — a stage entry pushes the
    browse position it left, so the first 「←」 after entering the stage
    returns to the grid instead of skipping a level (UIレビュー High #3 /
    リデザイン提案3), and 「→」 re-enters the stage symmetrically; ``curation`` is
    the cross-library list (``"starred"`` / ``"later"``) that was showing there,
    or ``None`` for an ordinary folder position.

    ``curation`` exists because those lists used to be **use-once**
    (UIレビュー 07-25 #58): drilling into one item dropped the overlay, and 「戻る」
    landed on the plain folder grid — so 「印を付けた項目を上から処理する」, the
    whole reason the lists exist, became a menu round-trip per item (and the
    filter typed on top of the list was lost each time).  Recording it here makes
    the list an ordinary history position: the ``search`` field alongside carries
    whatever narrowed it, so both come back together.

    ``recent`` is the folder whose 「最近追加されたファイル」 listing was showing
    there (``None`` for an ordinary position).  Same reasoning as ``curation``:
    the listing is a whole-grid overlay, so without recording it a single
    drill-down out of the listing would make it use-once.  The two are mutually
    exclusive — at most one of them is ever non-``None``.
    """

    root: Path
    selected: Path | None
    search: "SearchSnapshot | None"
    scroll: int = 0
    mode: str = "browse"
    curation: str | None = None
    recent: Path | None = None


class HistoryMenus:
    """The long-press history dropdowns for the ←/→ chrome buttons (B-6).

    Builds the two :class:`QMenu` instances and populates each lazily on
    ``aboutToShow`` from the corresponding stack, so they always reflect the
    current history without per-navigation rebuilds.  The stacks themselves
    live on the window and are read through callables; navigation is
    delegated back through ``navigate_steps`` so a bulk jump keeps every
    single-step invariant (opposite-stack push, search clear/restore, scroll
    restore, button sync) — a jump of *n* is just *n* single hops.

    The window attaches the menus to the grid's ←/→ buttons via
    ``PostGrid.attach_history_menus`` (``QToolButton.DelayedPopup`` keeps a
    short click firing the normal navigate signal; only a long press opens
    the menu).
    """

    MAX_ITEMS = 15  # newest N entries shown in the long-press dropdown

    def __init__(
        self,
        parent: QWidget,
        *,
        back_stack: Callable[[], list[NavEntry]],
        forward_stack: Callable[[], list[NavEntry]],
        entry_label: Callable[[NavEntry], tuple[str, str]],
        navigate_steps: Callable[..., None],
    ) -> None:
        """``entry_label`` maps an entry to ``(display name, tooltip)``;
        ``navigate_steps(n, forward=...)`` performs an *n*-step bulk jump.
        """
        self._back_stack = back_stack
        self._forward_stack = forward_stack
        self._entry_label = entry_label
        self._navigate_steps = navigate_steps
        self.back_menu = QMenu(parent)
        self.back_menu.aboutToShow.connect(
            lambda: self._populate(self.back_menu, forward=False)
        )
        self.forward_menu = QMenu(parent)
        self.forward_menu.aboutToShow.connect(
            lambda: self._populate(self.forward_menu, forward=True)
        )

    def _populate(self, menu: QMenu, *, forward: bool) -> None:
        # Newest-first: for 戻る the top item is the most recent back entry (one
        # step); for 進む the top item is the next forward entry.  Clicking an
        # item N rows down jumps that many steps at once.
        menu.clear()
        stack = self._forward_stack() if forward else self._back_stack()
        if not stack:
            act = menu.addAction(t("viewer.common.no_history"))
            act.setEnabled(False)
            return
        # Enumerate from the top of the stack (the next hop) downward, capped.
        count = 0
        previous: str | None = None
        for i in range(len(stack) - 1, -1, -1):
            steps = len(stack) - i  # 1-based distance from the current position
            label, tip = self._entry_label(stack[i])
            # (UIレビュー 08-28 N-25) 同じ見出しが連続する区間は 1 行へ畳む。
            # ``_enter_stage_mode`` は最大化のたびに同一 root を積むので、
            # 見分けの付かない行が並ぶのは日常的に起きる。残すのは**手前
            # （現在地に近いほう）**で、押せば同じ景色の最寄りへ戻る。
            if label == previous:
                continue
            previous = label
            act = menu.addAction(label)
            act.setToolTip(tip)
            act.triggered.connect(
                lambda _checked=False, n=steps, fwd=forward:
                self._navigate_steps(n, forward=fwd)
            )
            count += 1
            if count >= self.MAX_ITEMS:
                break
        menu.setToolTipsVisible(True)


# ---------------------------------------------------------------- mouse nav


@runtime_checkable
class MouseNavTarget(Protocol):
    """マウスのサイドボタンでナビゲートできるトップレベル窓の口."""

    def go_back(self) -> None: ...
    def go_forward(self) -> None: ...


def _active_window() -> object | None:
    """いまアクティブなトップレベル窓（テストが差し替える継ぎ目）."""
    app = QApplication.instance()
    return app.activeWindow() if app is not None else None


class _BackForwardRouter(QObject):
    """マウスの戻る/進むボタンを、アクティブなビューア窓へ配るアプリ級フィルタ.

    XButton1 / XButton2 は子ウィジェットへ配送されるので ``QShortcut`` にも
    窓自身の ``installEventFilter`` にも載らず、**アプリ級のフィルタが唯一の
    手段**。だからといって窓ごとに 1 本ずつ張ると、``QCoreApplication::notify``
    が配送する**全イベント × 生存窓数**の C++→Python 遷移になる（実測: 窓 1 つ
    あたり全イベントに +1.2〜1.9us / 5 窓で 0.36 → 11.9us）。しかも
    ``removeEventFilter`` の対が無かったため、閉じた（が破棄されていない）窓の
    ぶんまで課金され続けていた（レビュー 2026-09-03 項目#62）。

    ここは ``common/ui/wheelguard.py::_guard`` と同型の**プロセス singleton**
    で、何窓開いても張るのは 1 本だけ。窓の判定は
    ``QApplication.activeWindow()`` — 旧実装の ``self.isActiveWindow()`` ガード
    と同じ意味（ダイアログや別のトップレベルがアクティブなときは配らない）を、
    窓を跨いで 1 か所で行う。
    """

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if event.type() != QEvent.Type.MouseButtonPress:
            return False
        button = event.button()
        if button not in (
            Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton,
        ):
            return False
        window = _active_window()
        if not isinstance(window, MouseNavTarget):
            return False
        if button == Qt.MouseButton.BackButton:
            window.go_back()
        else:
            window.go_forward()
        return True


#: プロセスに 1 本だけ張るルータと、それを張った ``QApplication``。
#: アプリを作り直すテスト環境（新しい ``QApplication`` インスタンス）では
#: 張り直す — フィルタは古いアプリにしか載っていないため。
_mouse_nav_router: _BackForwardRouter | None = None
_mouse_nav_app: object | None = None


def install_mouse_nav() -> None:
    """マウスのサイドボタン・ルータを **1 本だけ** 張る（冪等）.

    ``ViewerWindow._install_shortcuts`` が窓を作るたびに呼ぶが、実際に
    ``installEventFilter`` するのは最初の 1 回だけ。
    """
    global _mouse_nav_router, _mouse_nav_app
    app = QApplication.instance()
    if app is None:
        return
    if _mouse_nav_router is not None and _mouse_nav_app is app:
        return
    _mouse_nav_router = _BackForwardRouter()
    _mouse_nav_app = app
    app.installEventFilter(_mouse_nav_router)


__all__ = [
    "HistoryMenus", "MouseNavTarget", "NavEntry", "SearchTransition",
    "SetRootResult", "install_mouse_nav",
]
