"""プレビュー列の葉ビューが共有する小さなヘルパ。

右クリックの動詞レジストリ 1 表を :mod:`.pdf_view` / :mod:`.zip_view` /
:mod:`.text_view` が同じ形で呼ぶための窓口だけを持つ。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMenu, QWidget

from ..context_menus import (
    EntryMenuContext,
    append_entry_verbs,
    curation_hooks_from_ancestors,
)
from ..focus_target import SEAT_PREVIEW


def _popup_entry_menu(widget: QWidget, path: "Path | None", global_pos) -> None:
    """Show the pane-shared right-click menu for *path*.

    Offers 「既定アプリで開く / エクスプローラで開く / フルパスをコピー」 via the
    same :func:`append_entry_verbs` the two children panes use, so the
    centre-pane leaf views (PDF / ZIP / text / media) can't drift from them.
    No-op when nothing is loaded.
    """
    if path is None:
        return
    menu = _build_entry_menu(widget, path)
    menu.exec(global_pos)
    menu.deleteLater()


def _build_entry_menu(widget: QWidget, path: Path) -> QMenu:
    """プレビュー列の葉ビュー共通の右クリックメニュー（動詞レジストリ 1 表）。

    印の口は祖先の ``ContentView`` が持つ（``set_curation_hooks``）ので、
    PDF / ZIP / テキスト / メディア / post.md のどの席でも★・あとで見る・
    ユーザータグが同じ位置に出る。
    """
    menu = QMenu(widget)
    append_entry_verbs(
        menu,
        EntryMenuContext(
            path=path, is_dir=False, seat=SEAT_PREVIEW,
            curation=curation_hooks_from_ancestors(widget), host=widget,
        ),
    )
    return menu
