"""「動詞 × 席」の対象解決を 1 本にする。

★ / あとで見る / ユーザータグ / コピー / 開く … の**動詞**が効く対象は、
ユーザーがいまどの**席**で操作しているかで決まる:

| 席 (``seat``) | 対象 |
|---|---|
| ``grid`` — 左グリッド | 選択中のタイル（投稿フォルダ or ファイル） |
| ``file_list`` — 右一覧 | 選択中のファイル |
| ``preview`` — プレビュー列（分割 / 最大化） | 表示中の項目（post.md 本文ならその投稿フォルダ） |
| ``lightbox`` — 全画面 | 表示中の画像 |

以前は判定が 3 本（グリッド選択 / 表示中パス / 「グリッド優先・無ければ表示中」）
に散り、``L`` と 0-5 が別の項目に書く・キーボードで開いたメニューが別のタイルに
効く、という「無言で別の項目にユーザーデータを書く」欠陥が席ごとに出ていた
（UIレビュー 2026-09-11 N-16 / N-88、リデザイン E4）。以後この関数が唯一の判定点で、
席を足すときは :data:`SEATS` と :func:`_seat_target` に 1 分岐足す。

Qt には焦点ウィジェットの取得だけで触る（同期・I/O なし — メニュー構築の
NAS-free 規約と同じ）。``is_dir`` は ``stat`` ではなくグリッドのタイルと
「現在のフォルダ自身か」から推定する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QApplication

from .folder_scan import POST_MD_NAME

if TYPE_CHECKING:
    from .main_window import ViewerWindow

SEAT_GRID = "grid"
SEAT_FILE_LIST = "file_list"
SEAT_PREVIEW = "preview"
SEAT_LIGHTBOX = "lightbox"
#: 席の一覧（表駆動テスト ``tests/test_viewer_focus_target.py`` が全席を回す）。
SEATS: tuple[str, ...] = (SEAT_GRID, SEAT_FILE_LIST, SEAT_PREVIEW, SEAT_LIGHTBOX)


@dataclass(frozen=True)
class Target:
    """動詞の対象 — パス・フォルダか否か・どの席で解決したか。

    ``PostGrid.populate_entry_menu`` はタイルと同じ形（``path`` / ``is_dir``）で
    受け取るので、右クリックのタイルと編集メニューの解決結果を同じ builder に
    渡せる。
    """

    path: Path
    is_dir: bool
    seat: str

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


def focused_seat(window: ViewerWindow) -> str | None:
    """フォーカスから席を決める。席の外なら直前の席、履歴が無ければ ``None``。

    全画面は別トップレベル窓なので、**全画面がアクティブなら**フォーカス
    ウィジェットに関係なくその席。ただし全画面は非アクティブ化で閉じも隠れも
    しないため、``Alt+Tab`` で本窓へ戻る / マルチモニタで並べると「全画面は
    可視だが操作しているのは本窓」が成立する — そのときまで全画面の席にすると
    ``L`` や編集メニューが**見えていない**画像へ効く（本窓側には何の告知も
    出ない無音の書き込み）ので、本窓がアクティブな間は通常のフォーカス判定へ
    落とす。どちらの窓もアクティブでないとき（判定材料が無いとき）は全画面の
    席のまま — 告知（中央オーバーレイ）を持つ側へ委ねる従来の裁定を保つ。
    """
    lb = getattr(window, "_lightbox", None)
    if lb is not None and lb.isVisible():
        if lb.isActiveWindow() or not window.isActiveWindow():
            return SEAT_LIGHTBOX
    focus = QApplication.focusWidget()
    seats = (
        (SEAT_FILE_LIST, getattr(window, "_file_list", None)),
        (SEAT_PREVIEW, getattr(window, "_content", None)),
        # グリッドは絞り込み欄やツールバーも同じ ``PostGrid`` に載るので、
        # ビュー本体だけを席とみなす（欄にフォーカスがあるとき L は
        # ShortcutOverride で届かないが、編集メニューからは撃てる）。
        (SEAT_GRID, getattr(getattr(window, "_post_grid", None), "_view", None)),
    )
    if focus is not None:
        for seat, widget in seats:
            if widget is None:
                continue
            if widget is focus or widget.isAncestorOf(focus):
                return seat
    # 席の外（メニューバーのキーボードモード・絞り込み欄・ツールバー）や、窓が
    # 非アクティブで焦点が無い間は、直前に席が持っていたフォーカスを使う
    # （ウィンドウが ``focusChanged`` で覚える ``_last_focus_seat``）。生の焦点で
    # 判定すると「Esc 1 回で閉じて開き直す」「絞り込み欄から Alt+E」のように、
    # 同じ画面状態・同じジェスチャで 1 回目と 2 回目の対象が変わる
    # （PR #183 レビュー）。履歴が無い起動直後だけ ``None`` = 呼び出し側の既定。
    return getattr(window, "_last_focus_seat", None)


def resolve_target(window: ViewerWindow, seat: str | None = None) -> Target | None:
    """いま動詞を撃ったら効く対象。*seat* を指定すればその席で解決する。

    席の外（ツールバー・ナビレール）から撃たれたときの既定は「最大化中は
    プレビュー、分割ビューではグリッドの選択、無ければ表示中の項目」— 分割
    ビューでグリッドの選択が「いま操作している対象」であるという従来の考え方を
    残しつつ、席にフォーカスがあるときはその席が必ず勝つ。メニューバー経由
    （編集メニュー）は :func:`focused_seat` が直前の席を引き継ぐので、マウスで
    開いてもキーボードで開いても同じ対象になる。
    """
    if seat is None:
        seat = focused_seat(window)
    if seat is not None:
        return _seat_target(window, seat)
    stage = getattr(window, "_ui_mode", "browse") == "stage"
    order = (SEAT_PREVIEW, SEAT_GRID) if stage else (SEAT_GRID, SEAT_PREVIEW)
    for fallback in order:
        target = _seat_target(window, fallback)
        if target is not None:
            return target
    return None


def curation_subject(path: Path) -> Path:
    """印の対象として扱うパス — ``post.md`` はその投稿フォルダに読み替える。

    投稿本文（post.md）をプレビューしているとき、表示中パスは ``<投稿>/post.md``
    になるが、ユーザーが印を付けたいのはメタデータファイルではなく投稿
    （= フォルダ）。読み替えないと「本文を読む」で投稿の★が消え、★を押すと
    タイルに現れない post.md へ user_meta 行ができる（UIレビュー 2026-09-11
    N-51 / PR #184 レビュー）。全席に効く（右一覧の post.md 行を選んで 0-5 を
    押しても投稿へ書く — ストリップが見せている対象と食い違わせない）。
    """
    # 判定はスキャナと同じ定義（大文字小文字を区別しない — ``is_meta_or_marker_name``）。
    if path.name.lower() == POST_MD_NAME:
        return path.parent
    return path


def _seat_target(window: ViewerWindow, seat: str) -> Target | None:
    if seat in (SEAT_GRID, SEAT_FILE_LIST):
        pane = window._post_grid if seat == SEAT_GRID else window._file_list
        tile = pane._view.current_tile()
        if tile is None:
            return None
        subject = curation_subject(tile.path)
        if subject != tile.path:
            return Target(subject, True, seat)  # post.md 行 → その投稿フォルダ
        return Target(tile.path, bool(tile.is_dir), seat)
    if seat == SEAT_PREVIEW:
        path = window._current_preview_path
        if path is None:
            return None
        path = curation_subject(path)
        return Target(path, _is_dir_hint(window, path), seat)
    if seat == SEAT_LIGHTBOX:
        lb = getattr(window, "_lightbox", None)
        path = lb.current_image() if lb is not None else None
        if path is None:
            return None
        return Target(path, False, seat)
    raise ValueError(f"unknown seat: {seat!r}")


def _is_dir_hint(window: ViewerWindow, path: Path) -> bool:
    """``stat`` せずにフォルダか否かを推定する（NAS-free）。

    グリッドに載っていればタイルが走査時の答えを持つ。載っていなければ
    「現在のフォルダ自身（何も選んでいないときの表示中パス）」だけをフォルダ
    とみなす — それ以外の表示中パスはプレビューが開いたファイル。
    """
    grid = window._post_grid
    idx = grid._view.index_of_key(grid._key_prefix + str(path))
    if idx is not None:
        tile = grid._view.tile_at(idx)
        if tile is not None:
            return bool(tile.is_dir)
    return path in (
        getattr(window, "_current_folder", None), getattr(window, "_root", None)
    )


__all__ = [
    "SEATS",
    "SEAT_FILE_LIST",
    "SEAT_GRID",
    "SEAT_LIGHTBOX",
    "SEAT_PREVIEW",
    "Target",
    "curation_subject",
    "focused_seat",
    "resolve_target",
]
