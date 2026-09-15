"""閲覧モードのプレイリスト列挙と投稿横断スキャン（Qt 非依存）.

``lightbox.LightboxWindow`` が表示する画像・動画の**母集合を決める単一定義**と、
末尾/先頭から隣の投稿へ渡るときの探索を持つ。ここは Qt に触れない純関数だけで、
呼び出しは全て**ワーカースレッド**から行う（GUI スレッドで NAS の ``os.scandir``
をしない — 冷えた SMB では 1 回の列挙が秒〜分になる）。

母集合の規約:

* 対象は**表示可能メディアのみ** = 画像 + 動画（:data:`PLAYLIST_SUFFIXES`）。
  ``post.md`` や ``#thumb#…`` のマーカーは列挙しない。判定は
  ``folder_scan.is_meta_or_marker_name`` の 1 か所だけを呼び、述語をここで
  再実装しない — マーカー規約が 1 つ増えたときに ``ChildrenGrid.tile_paths()``
  と母集合がズレるのを構造的に防ぐ。
* 投稿横断は **pre-order DFS**（:func:`scan_adjacent_image_folder`）。次の投稿が
  同じ階層に無くてもサブフォルダ・親経由の別階層へ連続して降りていく。
  メディアを持たないフォルダは自動でスキップする。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from ..folder_scan import (
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    dir_identity,
    is_meta_or_marker_name,
)

# 閲覧モードのプレイリストに含める拡張子: 画像 + 動画。
# 静止画は内部 ImageView、動画は遅延生成した MediaView で再生する。
PLAYLIST_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES

# 深さ優先の投稿横断で連続する画像なしフォルダを辿る上限（symlink ループ等の
# 暴走防止。実運用で隣接投稿の間にこれだけ空フォルダが並ぶことはない）。
_MAX_DFS_STEPS = 50000


def list_images_sorted(folder: Path) -> list[Path]:
    """*folder* 直下の画像ファイルを名前順（casefold）で列挙する。

    閲覧モードのプレイリスト構築の単一定義。``IMAGE_SUFFIXES`` のみを対象に
    し（動画・PDF 等はスキップ — モジュール docstring の設計判断）、
    ``#thumb#`` マーカーは**除外**する — UI のどこにもタイルとして出ない内部
    マーカーが ← で突然現れるのを避け、n/m・画像トラック・‹ › の母集合
    （``ChildrenGrid.tile_paths``）と同じ「表示可能メディアのみ」に揃える。
    判定は :func:`folder_scan.is_meta_or_marker_file` の 1 か所 — 走査ループは
    ``os.DirEntry.name`` しか持たないので名前版
    :func:`folder_scan.is_meta_or_marker_name` を呼ぶ。述語をここで再実装
    すると、マーカー規約が 1 つ増えた瞬間にステージ側だけが追従して母集合
    がズレる。
    ワーカースレッドから呼ぶこと（GUI スレッドで NAS scandir をしない）。
    """
    try:
        with os.scandir(folder) as it:
            names = [
                e.name
                for e in it
                if e.is_file()
                and os.path.splitext(e.name)[1].lower() in IMAGE_SUFFIXES
                and not is_meta_or_marker_name(e.name)
            ]
    except OSError:
        return []
    names.sort(key=str.casefold)
    return [folder / n for n in names]


def list_playlist_sorted(folder: Path) -> list[Path]:
    """*folder* 直下の画像 **と動画** を名前順（casefold）で列挙する。

    閲覧モードのプレイリストの単一定義。:func:`list_images_sorted` の一般化で、
    ``PLAYLIST_SUFFIXES``（画像 + 動画）を対象にする — 画像と動画が混在する
    投稿でも、フィルムストリップの枚数がフォルダの実ファイル数と一致し、動画が
    静かに飛ばされない。``#thumb#`` マーカーは :func:`list_images_sorted` と
    同じ理由で除外する。動画は
    :meth:`LightboxWindow._show_index` が MediaView へ
    切り替えて再生する。ワーカースレッドから呼ぶこと（GUI スレッドで NAS
    scandir をしない）。
    """
    try:
        with os.scandir(folder) as it:
            names = [
                e.name
                for e in it
                if e.is_file()
                and os.path.splitext(e.name)[1].lower() in PLAYLIST_SUFFIXES
                and not is_meta_or_marker_name(e.name)
            ]
    except OSError:
        return []
    names.sort(key=str.casefold)
    return [folder / n for n in names]


def is_video_path(path: Path) -> bool:
    """*path* が動画ファイル（``VIDEO_SUFFIXES``）か。"""
    return path.suffix.lower() in VIDEO_SUFFIXES


def scan_adjacent_post(
    folders: Sequence[Path], start: int, direction: int,
) -> tuple[int, list[Path]] | None:
    """*start* から *direction* 方向で「メディアを持つ最初の投稿」を探す。

    メディア（画像 + 動画）が 1 つもない投稿（テキストのみ等）は自動でスキップ
    し、リストの端まで見つからなければ ``None``。列挙は
    :func:`list_playlist_sorted`（画像 + 動画）で、直接オープン時のプレイリストと
    同一意味論に統一する — 動画のみの投稿にも横断で到達でき、混在投稿を跨いだ
    後も動画がプレイリストに残る。戻り値は
    ``(folders 内 index, その投稿のメディアリスト)``。ワーカースレッドで実行する
    前提（フォルダごとに 1 回の scandir）。
    """
    i = start + direction
    while 0 <= i < len(folders):
        images = list_playlist_sorted(folders[i])
        if images:
            return i, images
        i += direction
    return None


def _subdirs_sorted(folder: Path) -> list[Path]:
    """*folder* 直下のサブフォルダを casefold 名前順で列挙する（ワーカースレッド用）.

    symlink / ジャンクションも**除外しない** — 循環ガードは
    :func:`scan_adjacent_image_folder` 内の ``cycle_free``（``dir_identity``
    の祖先鎖照合）が担う。かつてここは ``follow_symlinks=False`` で
    リンクを弾いていたが、Windows のジャンクション（``mklink /J``）は
    ``is_dir(follow_symlinks=False) == True`` / ``is_symlink() == False`` を
    返すので循環対策としては一切効かず（他のウォーカーも同じ判断を済ませて
    いる）、一方で symlink 配下の投稿へは横断で到達できなくなる。
    """
    names: list[str] = []
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.is_dir():
                        names.append(e.name)
                except OSError:  # 消えた / 読めない子は分類不能 — スキップ
                    continue
    except OSError:
        return []
    names.sort(key=str.casefold)
    return [folder / n for n in names]


def scan_adjacent_image_folder(
    root: Path,
    current: Path,
    direction: int,
    top_level_order: Sequence[Path] | None = None,
) -> tuple[Path, list[Path]] | None:
    """*root* 部分木を pre-order DFS で辿り、*current* の隣でメディアを持つ最初の
    フォルダを返す（**深さ優先の投稿横断**）.

    フラットな兄弟走査（:func:`scan_adjacent_post`）の一般化。次の投稿が同じ
    ディレクトリ階層に無い場合でも、``current`` のサブフォルダ（より深い階層）
    や親経由の別階層へ pre-order で連続して入っていく — 深さ優先探索の順で
    画像を流し見できる。

    * **前方 (direction>0)** = pre-order の後続。``current`` にサブフォルダが
      あればまずそこへ降り（深い階層）、無ければ次の兄弟／親の次の兄弟へ上る。
    * **後方 (direction<0)** = pre-order の先行。前の兄弟の最深部（右端を降り
      切ったフォルダ）へ、兄弟が無ければ親へ。
    * メディア（画像 + 動画）を 1 つも持たないフォルダ（テキストのみ・中間
      ディレクトリ）は自動でスキップし、``root`` 部分木を出たら ``None``。列挙は
      :func:`list_playlist_sorted` で直接オープンと同一意味論（動画のみの投稿にも
      到達し、混在投稿を跨いでも動画が残る）。

    ``root`` 直下の子は *top_level_order*（左ペインのタイル表示順 = ソート／
    フィルタ反映）で並べ、それより深い階層はファイルシステムの casefold 名前順
    （:func:`_subdirs_sorted`）で辿る。フィルタで隠れた等で *top_level_order*
    に該当ノードが無いときはその階層だけファイルシステム順に退避する。
    ``current`` が ``root`` の外にある場合は ``None``（境界外は辿らない）。
    戻り値は ``(フォルダ, その画像リスト)``。ワーカースレッドで実行する前提。

    循環ガードは :func:`folder_scan.dir_identity` の **祖先鎖照合** で行う
    — 子リストからは「自分と同じ identity の祖先を持つ」
    ノードだけを落とす。祖先を指すジャンクション / symlink があっても同じ
    投稿を 1 段深い別名パスとして無限に掘り続けず（``_MAX_DFS_STEPS`` は
    最後の安全網であって循環対策ではない）、かつ **DFS の木構造を壊さない**:
    走査は 1 歩ずつ別呼び出しで進むのではなく ``preorder_next`` /
    ``preorder_prev`` が同じ列挙を何度も参照するため、「一度列挙した子を
    訪問済みにする」型の visited 集合では降りなかった兄弟まで既訪問扱いに
    なり、以降の横移動が丸ごと落ちてしまう（ノードごとに独立して判定できる
    祖先鎖ガードにはこの副作用が無い）。
    """
    if direction == 0:
        return None
    if current != root and root not in current.parents:
        return None  # 境界外 — root 部分木の外は辿らない

    order_map: list[Path] | None = (
        list(top_level_order) if top_level_order else None
    )

    # 1 回の呼び出しの中では木は不変として扱う（``preorder_next`` /
    # ``preorder_prev`` / ``siblings_and_index`` は同じフォルダの列挙を 1 歩の
    # 中で何度も参照する — docstring が明言しているとおり）。メモ化が無いと
    # 深さ d・子 n 件のフォルダ 1 つを列挙するのに scandir 1 回と
    # ``n*(d+1)`` 回の stat が要り、冷えた SMB では 1 歩が秒〜分単位になる。
    ancestor_idents: dict[Path, frozenset[object]] = {}
    children_cache: dict[Path, list[Path]] = {}
    fs_children_cache: dict[Path, list[Path]] = {}

    def ancestor_identities(folder: Path) -> frozenset[object]:
        """*folder* から ``root`` までの祖先 identity 集合（親ごとに 1 回だけ）。"""
        cached = ancestor_idents.get(folder)
        if cached is not None:
            return cached
        own = dir_identity(folder)
        if folder == root or root not in folder.parents:
            parents: frozenset[object] = frozenset()
        else:
            parents = ancestor_identities(folder.parent)
        idents = set(parents)
        if own is not None:
            idents.add(own)
        result = frozenset(idents)
        ancestor_idents[folder] = result
        return result

    def cycle_free(p: Path) -> bool:
        """*p* の identity が自分の祖先（``root`` まで）と一致するなら降りない。

        判定は「自分の identity が親の祖先集合に含まれるか」の 1 回の集合
        帰属で済む（祖先鎖を毎回 stat で登り直さない）。
        """
        ident = dir_identity(p)
        if ident is None:  # stat 失敗 — 過剰走査の方がマシ（folder_scan と同方針）
            return True
        return ident not in ancestor_identities(p.parent)

    def fs_children(folder: Path) -> list[Path]:
        cached = fs_children_cache.get(folder)
        if cached is None:
            cached = [p for p in _subdirs_sorted(folder) if cycle_free(p)]
            fs_children_cache[folder] = cached
        return cached

    def children_of(folder: Path) -> list[Path]:
        cached = children_cache.get(folder)
        if cached is not None:
            return cached
        if order_map is not None and folder == root:
            result = [p for p in order_map if cycle_free(p)]
        else:
            result = fs_children(folder)
        children_cache[folder] = result
        return result

    def siblings_and_index(node: Path) -> tuple[list[Path], int]:
        """*node* の兄弟リストと自身の位置。表示順に無ければ FS 順へ退避。"""
        sibs = children_of(node.parent)
        try:
            return sibs, sibs.index(node)
        except ValueError:
            fs = fs_children(node.parent)
            try:
                return fs, fs.index(node)
            except ValueError:
                return fs, -1

    def preorder_next(node: Path) -> Path | None:
        ch = children_of(node)
        if ch:
            return ch[0]  # 最初の子へ降りる（深い階層）
        cur = node
        while cur != root:
            sibs, idx = siblings_and_index(cur)
            if idx < 0:
                return None
            if idx + 1 < len(sibs):
                return sibs[idx + 1]
            cur = cur.parent
        return None

    def preorder_prev(node: Path) -> Path | None:
        if node == root:
            return None
        sibs, idx = siblings_and_index(node)
        if idx < 0:
            return None
        if idx == 0:
            return node.parent  # 先行は親
        cur = sibs[idx - 1]
        while True:  # 前の兄弟の最深部（右端を降り切る）
            ch = children_of(cur)
            if not ch:
                return cur
            cur = ch[-1]

    step = preorder_next if direction > 0 else preorder_prev
    node: Path | None = current
    for _ in range(_MAX_DFS_STEPS):
        node = step(node)
        if node is None:
            return None
        images = list_playlist_sorted(node)
        if images:
            return node, images
    return None


__all__ = [
    "PLAYLIST_SUFFIXES",
    "is_video_path",
    "list_images_sorted",
    "list_playlist_sorted",
    "scan_adjacent_image_folder",
    "scan_adjacent_post",
]
