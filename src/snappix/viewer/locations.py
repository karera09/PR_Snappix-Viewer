"""場所の名乗り（表示名）と、永続化してよい場所かの判定 — 窓の各面が共有する純関数.

**表示名**: 利用者に場所を名乗るとき、内部パスではなく利用者が知っている名前を
出す — ZIP ドリルインの展開先はアーカイブ名（``normal.zip (ZIP)``）、既定
ライブラリは「ライブラリ」、登録ライブラリは末尾フォルダ名（衝突時のみ
``親/名前``）。以前はこの変換を窓タイトル / 履歴 / パンくず / ステージヘッダー /
ステータスバー / 情報パネルの印の対象名が面ごとに手書きしており、対応範囲が
面ごとにばらばらで片側欠落を繰り返した（ZIP 展開先でプレビューヘッダーと
印の対象名だけが生の一時フォルダ名を出す、など）。正本は
:func:`location_bases` の「基準パス → 友好ラベル」表 1 枚で、パンくずの
基準（``breadcrumb.path_segments``）もこの表を使う。各面は
:func:`location_label` / :func:`base_label` を引くだけにする。

**永続化の可否**: ZIP ドリルインの展開先（``data/tmp/snappix-viewer-zip-*``）は
窓を閉じると消える一時ディレクトリで、そこを持続識別子として書くと失われる
（★ / あとで見る / タグ、ブックマーク、ライブラリ登録、``last_root``）。判定は
:func:`is_zip_temp_path` 1 本 — ``_zip_temp_dirs`` の表ではなく**場所**で
判定するので、掃除に失敗して次回起動に残った展開先（表は空）も弾ける。

どちらも I/O なし（純パス演算）なので GUI スレッドから NAS-free に呼べる。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from ..common.fsutil import tail_display_labels
from ..common.i18n import t
from ..common.paths import get_paths

#: ZIP ドリルインの展開先ディレクトリ名の接頭辞（``tempfile.mkdtemp`` の prefix）。
ZIP_TEMP_PREFIX = "snappix-viewer-zip-"


def zip_temp_parent() -> Path:
    """ZIP 展開先の親（``data/tmp``）。作成はしない — 作るのは展開側。"""
    return get_paths().data / "tmp"


def is_zip_temp_path(path: Path | str | None) -> bool:
    """*path* が ZIP ドリルインの一時展開先（またはその配下）か.

    ``data/tmp`` 直下の ``snappix-viewer-zip-*`` を根とする木に含まれるかで
    判定する。閉じると消える場所なので、ここに該当するパスを印・ブックマーク・
    ライブラリ・``last_root`` として永続化してはならない。
    """
    if path is None or path == "":
        return False
    p = Path(path)
    parent = zip_temp_parent()
    try:
        rel = p.relative_to(parent)
    except ValueError:
        try:
            rel = p.absolute().relative_to(parent.absolute())
        except ValueError:
            return False
    parts = rel.parts
    return bool(parts) and parts[0].lower().startswith(ZIP_TEMP_PREFIX)


def location_bases(
    zip_temp_dirs: Mapping[Path, Path],
    default_library: Path,
    library_roots: Iterable[str],
) -> list[tuple[Path, str]]:
    """「基準パス → 友好ラベル」の表（場所の名乗りの正本）.

    ZIP 展開先（アーカイブ名 ``… (ZIP)``）→ 既定ライブラリ（「ライブラリ」）→
    登録ライブラリ（末尾フォルダ名、衝突時のみ ``親/名前`` —
    :func:`~snappix.common.fsutil.tail_display_labels`）の順。既定ライブラリを
    登録していても日本語ラベルを保つようパスで重複を除く。パンくずの基準にも
    そのまま使うので、ZIP 展開先が「そのアーカイブが境界」になる（↑ で
    ``data/tmp`` → ``data/`` へ降りない）。
    """
    bases: list[tuple[Path, str]] = [
        (temp_dir, t("viewer.main_window.zip_history_label", name=zip_path.name))
        for temp_dir, zip_path in zip_temp_dirs.items()
    ]
    bases.append((default_library, t("viewer.main_window.library_menu")))
    seen = {default_library}
    roots = list(library_roots)
    labels = tail_display_labels(roots)
    for raw in roots:
        p = Path(raw)
        if p in seen:
            continue
        seen.add(p)
        bases.append((p, labels[raw]))
    return bases


def base_label(path: Path | None, bases: Iterable[tuple[Path, str]]) -> str | None:
    """*path* が表の基準パスそのものならその友好ラベル、そうでなければ ``None``."""
    if path is None:
        return None
    for base, label in bases:
        if path == base:
            return label
    return None


def location_label(path: Path, bases: Iterable[tuple[Path, str]]) -> str:
    """*path* の表示名 — 基準パスなら友好ラベル、それ以外は末尾名（無ければ全文）."""
    label = base_label(path, bases)
    if label is not None:
        return label
    return path.name or str(path)
