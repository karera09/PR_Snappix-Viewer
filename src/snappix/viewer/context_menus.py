"""右クリックメニューの**動詞レジストリ** — 4 席が同じ表から同じ並びで組む。

席（左グリッド / 右一覧 / プレビュー列の各ビュー / 全画面の画像）ごとに
builder を別々に育てると、キュレーション節が無い席・コピー項目の有無が違う席・
類似検索の位置が違う席が生まれる。そこで :data:`VERBS` の 1 表が「どの動詞が・どの条件で・どの節に」を持ち、
:func:`append_entry_verbs` がその順で足す:

    [対象名の見出し]
    開く      — 開く（フォルダのみ / このビューアで中へ）
                このファイルの場所を開く（ファイルのみ / 親をこのビューアで）
                既定アプリで開く / エクスプローラで開く
    コピー    — フルパスをコピー / ファイルをコピー
    探す      — この画像で類似検索（画像のみ）/ 最近追加されたファイル（フォルダのみ）
    印        — スター ▸ / あとで見る / ユーザータグを編集…（キュレーション店があるとき）
    プラグイン寄稿（``register_context_menu_contributor``）

席固有の項目（画像ビューのフィット / 回転 / 画像をコピー等）は builder が
**先に**足し、その後ろに共通ブロックが同じ順で続く — 「完全に同じでなくても
似た配置」の規則。見出しは ``QMenu`` の先頭へ差し込むので、席固有の項目が
先にあっても一番上に来る（キーボードで開いたメニューでも「どれに効くか」が
読める）。

書き手は 1 本のまま: 印の動詞は :class:`CurationHooks.request` へ
``(path, kind, value)`` を渡すだけで、永続化・トースト・3 面再描画は
``PostGrid.request_curation`` が持つ。

メニュー構築は NAS-free（ファイルシステム I/O をしない）— プラグイン寄稿にも
同じ規約が掛かる。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QAction, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import QMenu, QWidget

from ..common.i18n import t
from ..common.ui import icon, show_toast
from .folder_scan import IMAGE_SUFFIXES
from .view_prefs import _reveal_in_explorer, open_with_default

# ---------------------------------------------------------- plugin registry
#
# プラグイン（plugin_host/context.py::PluginContext.add_context_menu_entry）
# からの右クリックメニュー寄稿。owner はプラグイン id で、プラグインの
# deactivate 時に owner 単位でまとめて解除される。寄稿関数はメニュー構築の
# たびに GUI スレッドで呼ばれるため、本モジュールの規約（NAS-free =
# ファイルシステム I/O 禁止）に従うこと。例外は隔離される。

_MENU_CONTRIBUTORS: list[tuple[str, Callable[[QMenu, Path, bool], None]]] = []


def register_context_menu_contributor(
    owner: str, contributor: Callable[[QMenu, Path, bool], None]
) -> None:
    """*owner*（プラグイン id）名義でメニュー寄稿関数を登録する。"""
    _MENU_CONTRIBUTORS.append((owner, contributor))


def unregister_context_menu_contributors(owner: str) -> None:
    """*owner* 名義の寄稿をすべて解除する。"""
    _MENU_CONTRIBUTORS[:] = [
        (o, fn) for (o, fn) in _MENU_CONTRIBUTORS if o != owner
    ]


def _append_contributor_actions(menu: QMenu, path: Path, is_dir: bool) -> None:
    if not _MENU_CONTRIBUTORS:
        return
    # 区切り線は「最初の寄稿が実際に項目を追加したとき」に遅延挿入する —
    # 全寄稿がフィルタで何も足さなかったタイル（例: フォルダ）に宙ぶらりんの
    # separator を残さない。
    separator_added = False
    for owner, contributor in list(_MENU_CONTRIBUTORS):
        count_before = len(menu.actions())
        try:
            contributor(menu, path, is_dir)
        except Exception:  # noqa: BLE001 — プラグイン隔離（本体メニューを守る）
            logger.exception(
                "context-menu contributor from plugin {!r} failed", owner
            )
        if not separator_added and len(menu.actions()) > count_before:
            menu.insertSeparator(menu.actions()[count_before])
            separator_added = True


# ------------------------------------------------------------ clipboard


def copy_file_to_clipboard(path: Path, window=None) -> None:
    """Put *path* on the clipboard as a file (Explorer paste target).

    Uses ``QMimeData.setUrls`` — the same payload Explorer itself writes for
    Ctrl+C (CF_HDROP equivalent), so pasting into Explorer, chat clients, or
    file pickers works.  Works for every entry kind, not just images.

    成功トーストは内側のここで出す (画像コピーと同じ
    方針: クリップボードは不可視なので通知が唯一の成功確認手段)。
    """
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    QGuiApplication.clipboard().setMimeData(mime)
    _notify_copied(window, t("viewer.context_menus.copy_file_done"))


def copy_path_to_clipboard(path: Path, window=None) -> None:
    """Put *path* on the clipboard as plain text（フルパスをコピー）.

    画像 / ファイルのコピーと同じく、成功トーストを内側で出す。
    """
    QGuiApplication.clipboard().setText(str(path))
    _notify_copied(window, t("viewer.context_menus.copy_path_done"))


def _notify_copied(window, message: str) -> None:
    """コピー成功を *window* のトーストで知らせる（ホスト不明なら黙る）。

    呼び出し側が渡すのはメニューの親（ペインの子ウィジェット）なので、まず
    トップレベルへ辿ってウィンドウの通知ファンネル（``_show_toast``）へ流す
    — 成功/情報通知の呼び口を 1 本に保つための規約（``ViewerWindow._show_toast``
    の docstring）。ファンネルを持たないホスト（単体テストの素の QWidget 等）
    でだけ ``show_toast`` へ直接落とす。
    """
    if window is None:
        return
    try:
        host = window.window()
        notify = getattr(host, "_show_toast", None)
        if callable(notify):
            notify(message, "success")
        else:
            show_toast(host, message, kind="success")
    except Exception:  # pragma: no cover (defensive — ホスト無しの単体呼び出し)
        logger.debug("copy toast skipped for {}", message)


# ------------------------------------------------------------- registry


@dataclass(frozen=True)
class CurationHooks:
    """印の動詞が読む / 書くための 2 口。

    ``provider(path) -> (star, later)`` は同期のインメモリ参照（左ペインの
    ``_user_meta_map``）— 描画のたびに呼ばれるので sqlite / NAS へ触らない。
    ``request(path, kind, value)`` は単一書き手 ``PostGrid.request_curation`` か、
    そこへ転送するシグナル emit（右一覧 / 全画面は左ペインを知らない）。
    """

    provider: Callable[[Path], tuple[int, bool]]
    request: Callable[[Path, str, object], object]


def reveal_hook_from_ancestors(widget: QWidget | None) -> Callable[[Path], None] | None:
    """*widget* の祖先が持つ ``reveal_in_app`` を返す（無ければ ``None``）。

    「このファイルの場所を開く」は**席の性格に依らない**動詞で、席が渡した口
    （左グリッド / 右一覧は自分のシグナルを渡す）が無ければ祖先の窓に訊く。
    プレビュー列の葉ビューと全画面の画像ビューは自前の配線を持たないが、
    そこでこそ要る — 全画面の投稿横断（別フォルダの画像へ進む）や横断一覧の
    プレビューでは、画面の項目がグリッドの選択と別フォルダになり得る。
    :func:`curation_hooks_from_ancestors` と同じ形。
    """
    w = widget
    while w is not None:
        hook = getattr(w, "reveal_in_app", None)
        if callable(hook):
            return hook
        w = w.parentWidget()
    return None


def curation_hooks_from_ancestors(widget: QWidget | None) -> CurationHooks | None:
    """*widget* の祖先が持つ ``_curation_hooks`` を返す（無ければ ``None``）。

    プレビュー列の各ビュー（画像 / PDF / ZIP / テキスト / メディア / post.md）
    は ``ContentView`` の子、全画面の画像ビューは ``Lightbox`` の子なので、
    席の親 1 箇所に注入すれば葉のビューは自分で配線を持たなくて済む。
    """
    w = widget
    while w is not None:
        hooks = getattr(w, "_curation_hooks", None)
        if hooks is not None:
            return hooks
        w = w.parentWidget()
    return None


@dataclass(frozen=True)
class EntryMenuContext:
    """1 回のメニュー構築の入力（対象 + 席が提供できる動詞の口）。"""

    path: Path
    is_dir: bool
    seat: str = ""
    #: フォルダを**このビューアで**開く（左グリッドの活性化と同じ着地）。
    #: 渡せるのは移動を持つ席（左グリッド / 右一覧）だけ。
    navigate: Callable[[Path], None] | None = None
    #: ファイルのある**フォルダをこのビューアで開いて選択**する。
    #: 横断一覧・検索ヒットから実体の
    #: 場所へ戻る唯一のアプリ内導線。``navigate`` と違い**席の制限は無い** —
    #: 渡さなかった席は :func:`reveal_hook_from_ancestors` が祖先の窓から
    #: 拾うので、自前の配線を持たない葉ビュー（プレビュー列 / 全画面）でも
    #: 出る。
    reveal_in_app: Callable[[Path], None] | None = None
    similar_search: Callable[[], None] | None = None
    recent_files: Callable[[Path], None] | None = None
    curation: CurationHooks | None = None
    #: 実体に到達できなかった行のプレースホルダ（横断一覧のゴースト）。
    #: 「開く / エクスプローラ / 印」系は全部空振りするので、この軸が真の
    #: ときは通常の動詞を全部落とし、救済の動詞だけを出す。軸を
    #: :data:`VERBS` の側に持たせるのが要点 — 席で手書きに分岐すると
    #: 対象名見出しも ``verb:`` 命名も付いてこない。
    ghost: bool = False
    #: ゴースト行の張り替え（「現在の場所を指定…」）。
    rebind: Callable[[Path], None] | None = None
    #: 同じ根を共有するゴーストの**一括**張り替え。失敗の単位はボリューム
    #: なので、行単位だけでは数百回のモーダル往復になる。口を渡すのは席が
    #: 「まとめて指定する意味がある」と判断したとき（2 件以上）だけで、
    #: 件数は項目名に出る。
    rebind_prefix: Callable[[Path], None] | None = None
    #: その一括の対象件数（項目名に埋める。口を渡す席が数える）。
    rebind_prefix_n: int = 0
    #: ゴースト行をこの一覧から外す。**確実に消えた行にだけ**渡すこと
    #: （読めなかっただけの行は実体が生きているかもしれない）。
    ghost_remove: Callable[[Path], None] | None = None
    include_copy_file: bool = True
    header: bool = True
    #: トースト等の親。``None`` なら ``menu.parentWidget()``。
    host: QWidget | None = None


@dataclass(frozen=True)
class VerbSpec:
    """動詞 1 つ = id / 節 / 出す条件 / メニューへ足す手。"""

    id: str
    section: str
    applies: Callable[[EntryMenuContext], bool]
    add: Callable[[QMenu, EntryMenuContext], None]


#: 節の並び。builder はこの順で節を区切る（表駆動テストが参照する）。
SECTIONS: tuple[str, ...] = ("open", "copy", "find", "curation")


def _host(menu: QMenu, ctx: EntryMenuContext) -> QWidget | None:
    return ctx.host if ctx.host is not None else menu.parentWidget()


def _add_action(
    menu: QMenu, verb_id: str, text: str, *, icon_name: str | None = None,
) -> QAction:
    act = QAction(icon(icon_name), text, menu) if icon_name else QAction(text, menu)
    act.setObjectName(f"verb:{verb_id}")
    menu.addAction(act)
    return act


def _add_open_here(menu: QMenu, ctx: EntryMenuContext) -> None:
    # フォルダの右クリックが「既定アプリで開く」「エクスプローラで開く」
    # = どちらもアプリ**外**だけだと、ダブルクリック
    # (= このビューアで中を開く) と同じことをメニューから頼めない。
    # 「まずアプリ内、次に OS」の並びで先頭に立てる。図像を持つのは OS へ出る
    # 2 動詞だけ、という既存の暗黙規則に従い icon_name は渡さない。
    cb = ctx.navigate
    assert cb is not None  # applies() が保証（型絞り込みのみ）
    act = _add_action(menu, "open_here", t("common.action.open"))
    act.setToolTip(t("viewer.context_menus.open_here_hint"))
    act.triggered.connect(lambda _=False, p=ctx.path, cb=cb: cb(p))


def _reveal_hook(ctx: EntryMenuContext) -> Callable[[Path], None] | None:
    """席が渡した「場所を開く」の口、無ければ祖先の窓が持つもの。"""
    if ctx.reveal_in_app is not None:
        return ctx.reveal_in_app
    return reveal_hook_from_ancestors(ctx.host)


def _add_reveal_in_app(menu: QMenu, ctx: EntryMenuContext) -> None:
    # 横断一覧・検索ヒットの行から「実体がどこにあるか」へアプリ内で戻る。
    # 親フォルダをこのビューアで開いて当の項目を選ぶ。``open_in_explorer``
    # との差はツールチップで言う。ここでは FS I/O をしない
    # （メニュー構築は NAS-free）。
    cb = _reveal_hook(ctx)
    assert cb is not None  # applies() が保証（型絞り込みのみ）
    act = _add_action(menu, "reveal_in_app", t("viewer.context_menus.reveal_in_app"))
    act.setToolTip(t("viewer.context_menus.reveal_in_app_hint"))
    act.triggered.connect(lambda _=False, p=ctx.path, cb=cb: cb(p))


def _add_open_default(menu: QMenu, ctx: EntryMenuContext) -> None:
    # openUrl の戻り値を捨てると失敗が握りつぶされるので、共通ヘルパ
    # ``open_with_default`` に集約し、どの入口でも失敗が通知される。
    act = _add_action(
        menu, "open_default", t("common.action.open_with_default"),
        icon_name="external-link",
    )
    host = _host(menu, ctx)
    act.triggered.connect(
        lambda _=False, p=ctx.path, h=host: open_with_default(p, h)
    )


def _add_open_in_explorer(menu: QMenu, ctx: EntryMenuContext) -> None:
    # OS のファイラ起動は folder-output（箱から出る矢印）。
    # 右クリックで図像を持つのは OS へ出る 2 動詞だけ、という暗黙規則。
    act = _add_action(
        menu, "open_in_explorer", t("common.action.open_in_explorer"),
        icon_name="folder-output",
    )
    host = _host(menu, ctx)
    act.triggered.connect(
        lambda _=False, p=ctx.path, h=host: _reveal_in_explorer(p, h)
    )


def _add_copy_path(menu: QMenu, ctx: EntryMenuContext) -> None:
    act = _add_action(menu, "copy_path", t("viewer.context_menus.copy_full_path"))
    # 「ファイルをコピー」との差がラベルから読めないので、両方に用途を
    # ツールチップで添える（メニューの可視化は theme の全域フィルタが立てる）。
    act.setToolTip(t("viewer.context_menus.copy_full_path_hint"))
    host = _host(menu, ctx)
    act.triggered.connect(
        lambda _=False, p=ctx.path, h=host: copy_path_to_clipboard(p, h)
    )


def _add_copy_file(menu: QMenu, ctx: EntryMenuContext) -> None:
    act = _add_action(menu, "copy_file", t("viewer.context_menus.copy_file"))
    act.setToolTip(t("viewer.context_menus.copy_file_hint"))
    host = _host(menu, ctx)
    act.triggered.connect(
        lambda _=False, p=ctx.path, h=host: copy_file_to_clipboard(p, h)
    )


def _add_similar_search(menu: QMenu, ctx: EntryMenuContext) -> None:
    cb = ctx.similar_search
    assert cb is not None  # applies() が保証（型絞り込みのみ）
    act = _add_action(
        menu, "similar_search", t("viewer.common.similar_search_image")
    )
    act.triggered.connect(lambda _=False, cb=cb: cb())


def _add_recent_files(menu: QMenu, ctx: EntryMenuContext) -> None:
    # 「このフォルダに最近何が増えた?」— フラットな新しい順一覧への直接導線。
    # フォルダのみ（一覧はフォルダの中身が主題）。走査はクリック時。
    cb = ctx.recent_files
    assert cb is not None  # applies() が保証（型絞り込みのみ）
    act = _add_action(menu, "recent_files", t("viewer.post_grid.recent_files_action"))
    act.setToolTip(t("viewer.main_window.recent_files_hint"))
    act.triggered.connect(lambda _=False, p=ctx.path, cb=cb: cb(p))


#: ★ピッカーの行ラベルが並べるグリフ（``post_grid`` の確定トーストと同じ形）。
#: 文字形であってカタログの文言ではないので、i18n の値には入れない
#: （``tests/test_i18n_no_pictographs.py`` の絵文字禁止とは別の理由）。
_STAR_GLYPH = "★"


def _curation_state(ctx: EntryMenuContext) -> tuple[int, bool]:
    assert ctx.curation is not None  # applies() が保証
    try:
        star, later = ctx.curation.provider(ctx.path)
    except Exception:  # pragma: no cover (defensive — プロバイダは同期の辞書引き)
        return 0, False
    return int(star or 0), bool(later)


def _add_star_submenu(menu: QMenu, ctx: EntryMenuContext) -> None:
    cur_star, _ = _curation_state(ctx)
    request = ctx.curation.request  # type: ignore[union-attr]
    # 見出しは現在値を出す（サブメニューを開いてチェック位置を探さずに
    # 今いくつか分かる）。0 のときは値なしの文言。
    star_menu = menu.addMenu(
        t("viewer.post_grid.star_menu_keys_value", n=cur_star) if cur_star
        else t("viewer.post_grid.star_menu_keys")
    )
    star_menu.menuAction().setObjectName("verb:star")
    for n in (5, 4, 3, 2, 1):
        # 行ラベルは★グリフ + 数値（生グリフだけでは 4 と 5 を目で数える
        # ことになる）。グリフ自体は i18n の値ではなく**数の表現**なので
        # ここで組み、書式だけをカタログが持つ。
        act = QAction(
            t("viewer.post_grid.star_menu_item", stars=_STAR_GLYPH * n, n=n),
            star_menu,
        )
        act.setCheckable(True)
        act.setChecked(cur_star == n)
        act.triggered.connect(
            lambda _=False, p=ctx.path, v=n, r=request: r(p, "star", v)
        )
        star_menu.addAction(act)
    star_menu.addSeparator()
    none_act = QAction(t("viewer.post_grid.star_none"), star_menu)
    none_act.setCheckable(True)
    none_act.setChecked(cur_star == 0)
    none_act.triggered.connect(
        lambda _=False, p=ctx.path, r=request: r(p, "star", 0)
    )
    star_menu.addAction(none_act)


def _add_later(menu: QMenu, ctx: EntryMenuContext) -> None:
    _, cur_later = _curation_state(ctx)
    request = ctx.curation.request  # type: ignore[union-attr]
    act = _add_action(menu, "later", t("viewer.post_grid.watch_later_menu"))
    act.setCheckable(True)
    act.setChecked(cur_later)
    act.triggered.connect(
        lambda checked=False, p=ctx.path, r=request: r(p, "later", bool(checked))
    )


def _add_edit_tags(menu: QMenu, ctx: EntryMenuContext) -> None:
    request = ctx.curation.request  # type: ignore[union-attr]
    act = _add_action(menu, "edit_tags", t("viewer.post_grid.edit_user_tags"))
    act.triggered.connect(
        lambda _=False, p=ctx.path, r=request: r(p, "edit_tags", None)
    )


def _add_rebind(menu: QMenu, ctx: EntryMenuContext) -> None:
    rebind = ctx.rebind  # type: ignore[assignment]
    act = _add_action(
        menu, "rebind", t("viewer.post_grid.curation_rebind_action"),
    )
    act.triggered.connect(lambda _=False, p=ctx.path, r=rebind: r(p))


def _add_rebind_prefix(menu: QMenu, ctx: EntryMenuContext) -> None:
    rebind = ctx.rebind_prefix  # type: ignore[assignment]
    act = _add_action(
        menu, "rebind_prefix",
        t(
            "viewer.post_grid.curation_rebind_prefix_action",
            n=ctx.rebind_prefix_n,
        ),
    )
    act.triggered.connect(lambda _=False, p=ctx.path, r=rebind: r(p))


def _add_ghost_remove(menu: QMenu, ctx: EntryMenuContext) -> None:
    remove = ctx.ghost_remove  # type: ignore[assignment]
    act = _add_action(
        menu, "ghost_remove", t("viewer.post_grid.curation_ghost_remove"),
    )
    act.triggered.connect(lambda _=False, p=ctx.path, r=remove: r(p))


def _is_image(ctx: EntryMenuContext) -> bool:
    return not ctx.is_dir and ctx.path.suffix.lower() in IMAGE_SUFFIXES


#: 動詞の表 — 並びがそのままメニューの並び。席は「口を渡すか」だけを決める。
#:
#: ``ghost``（実体に到達できなかった行）は**軸**として表に持たせる: 実体が
#: 無い行では「開く / エクスプローラ / 印」は全部空振りするので落ち、代わりに
#: 救済の動詞（張り替え / 同じ根の一括張り替え / フルパスをコピー /
#: この一覧から外す）が出る。席側で
#: 手書きに分岐すると、対象名見出しも ``verb:`` 命名も付いてこない。
VERBS: tuple[VerbSpec, ...] = (
    # アプリ内で動く 2 動詞が先（フォルダ = 中を開く / ファイル = 場所を開く。
    # ``is_dir`` で排他なので 1 つの対象に両方は出ない）、その後ろに OS へ
    # 出る 2 動詞が続く。
    VerbSpec(
        "open_here", "open",
        lambda c: c.is_dir and c.navigate is not None and not c.ghost,
        _add_open_here,
    ),
    VerbSpec(
        "reveal_in_app", "open",
        lambda c: not c.is_dir and _reveal_hook(c) is not None and not c.ghost,
        _add_reveal_in_app,
    ),
    VerbSpec(
        "rebind", "open",
        lambda c: c.ghost and c.rebind is not None,
        _add_rebind,
    ),
    VerbSpec(
        "rebind_prefix", "open",
        lambda c: c.ghost and c.rebind_prefix is not None,
        _add_rebind_prefix,
    ),
    VerbSpec(
        "open_default", "open", lambda c: not c.ghost, _add_open_default,
    ),
    VerbSpec(
        "open_in_explorer", "open", lambda c: not c.ghost, _add_open_in_explorer,
    ),
    # フルパスのコピーはゴーストでも意味を持つ唯一の共通動詞（旧パスを
    # 手掛かりに実体を探すための材料）。
    VerbSpec("copy_path", "copy", lambda _c: True, _add_copy_path),
    VerbSpec(
        "copy_file", "copy",
        lambda c: c.include_copy_file and not c.ghost,
        _add_copy_file,
    ),
    VerbSpec(
        "similar_search", "find",
        lambda c: c.similar_search is not None and _is_image(c) and not c.ghost,
        _add_similar_search,
    ),
    VerbSpec(
        "recent_files", "find",
        lambda c: c.recent_files is not None and c.is_dir and not c.ghost,
        _add_recent_files,
    ),
    VerbSpec(
        "star", "curation",
        lambda c: c.curation is not None and not c.ghost, _add_star_submenu,
    ),
    VerbSpec(
        "later", "curation",
        lambda c: c.curation is not None and not c.ghost, _add_later,
    ),
    VerbSpec(
        "edit_tags", "curation",
        lambda c: c.curation is not None and not c.ghost, _add_edit_tags,
    ),
    VerbSpec(
        "ghost_remove", "curation",
        lambda c: c.ghost and c.ghost_remove is not None,
        _add_ghost_remove,
    ),
)


def verb_ids_for(ctx: EntryMenuContext) -> list[str]:
    """*ctx* で出る動詞 id の並び（表駆動テストの期待集合の導出元）。"""
    return [spec.id for spec in VERBS if spec.applies(ctx)]


def menu_verb_ids(menu: QMenu) -> list[str]:
    """実際に組まれた *menu* から動詞 id を読む（``objectName`` = ``verb:<id>``）。

    見出しは ``verb:target``、席固有の項目（objectName 無し）は含まれない。
    """
    ids: list[str] = []
    for act in menu.actions():
        name = act.objectName()
        if name.startswith("verb:"):
            ids.append(name[len("verb:"):])
    return ids


_HEADER_MAX_PX = 320


def _insert_target_header(menu: QMenu, ctx: EntryMenuContext) -> None:
    """対象名の無効見出しをメニューの**先頭**へ差し込む。

    右クリックは選択を動かさない（選択がプレビューのデコードを駆動するため
    意図した設計）ので、押した先がどれかはメニュー自身が名乗るしかない。
    キーボードで開いたメニュー・編集メニューでも同じ 1 行が効く。
    """
    name = ctx.path.name or str(ctx.path)
    text = QFontMetrics(menu.font()).elidedText(
        name, Qt.TextElideMode.ElideMiddle, _HEADER_MAX_PX
    )
    head = QAction(text, menu)
    head.setObjectName("verb:target")
    head.setEnabled(False)
    head.setToolTip(str(ctx.path))
    actions = menu.actions()
    if actions:
        menu.insertAction(actions[0], head)
        menu.insertSeparator(actions[0])
    else:
        menu.addAction(head)
        menu.addSeparator()


def _ends_with_separator(menu: QMenu) -> bool:
    actions = menu.actions()
    return bool(actions) and actions[-1].isSeparator()


def append_entry_verbs(menu: QMenu, ctx: EntryMenuContext) -> None:
    """*ctx* の対象に効く共通ブロックを *menu* に足す（節ごとに区切り線）。

    席固有の項目は呼び出し側が**先に**足しておく。見出し（``ctx.header``）は
    それらより上、メニュー先頭に差し込まれる。
    """
    if ctx.header:
        _insert_target_header(menu, ctx)
    section: str | None = None
    for spec in VERBS:
        if not spec.applies(ctx):
            continue
        if spec.section != section:
            if menu.actions() and not _ends_with_separator(menu):
                menu.addSeparator()
            section = spec.section
        spec.add(menu, ctx)
    _append_contributor_actions(menu, ctx.path, ctx.is_dir)


def append_common_entry_actions(
    menu: QMenu,
    path: Path,
    *,
    is_dir: bool,
    similar_search: Callable[[], None] | None = None,
    include_copy_file: bool = False,
) -> None:
    """見出し・印なしの共通ブロック（旧 API 互換 — プラグインのテストが使う）。

    新しい席は :func:`append_entry_verbs` に :class:`EntryMenuContext` を渡す
    こと（見出し・キュレーション節・「最近追加」まで表から出る）。
    """
    append_entry_verbs(
        menu,
        EntryMenuContext(
            path=path, is_dir=is_dir, similar_search=similar_search,
            include_copy_file=include_copy_file, header=False,
        ),
    )


__all__ = [
    "SECTIONS",
    "VERBS",
    "CurationHooks",
    "EntryMenuContext",
    "VerbSpec",
    "append_common_entry_actions",
    "append_entry_verbs",
    "copy_file_to_clipboard",
    "copy_path_to_clipboard",
    "curation_hooks_from_ancestors",
    "menu_verb_ids",
    "register_context_menu_contributor",
    "reveal_hook_from_ancestors",
    "unregister_context_menu_contributors",
    "verb_ids_for",
]
