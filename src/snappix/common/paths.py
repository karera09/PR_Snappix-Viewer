"""Portable base directory resolution.

All runtime state (viewer state, caches, logs, tags.db) lives under the base
directory so the tool stays self-contained and never touches the user's home
directory or %APPDATA%.

Layout (frozen / portable build)::

    snappix-viewer/
    ├── SnappixViewer.exe        <- sys.executable
    ├── _internal/               <- PyInstaller bundle
    ├── data/                    <- runtime state (created on first run)
    └── library/                 <- default browse root (optional; any folder
                                    can be opened instead)

Layout (development run via ``python -m snappix``)::

    <repo root>/
    ├── src/snappix/...
    ├── data/                    <- runtime state
    └── library/                 <- default browse root

OS の一時ディレクトリについて（裁定）
------------------------------------

**OS temp は「ユーザーホーム配下」なので使わない。** Windows の
``tempfile.gettempdir()`` は ``%TEMP%`` ＝ ``%LOCALAPPDATA%\\Temp`` を返し、
これはユーザープロファイル配下 ＝ ポータブル運用が「一切書かない」と定めた
場所そのもの（ポータブル運用では、USB のツールを使ったつもりが痕跡は PC 側に
残る）。したがって:

* ``tempfile.mkdtemp`` / ``mkstemp`` / ``TemporaryDirectory`` /
  ``NamedTemporaryFile`` は **必ず ``dir=`` を渡す**。行き先は
  ``get_paths().data`` 配下（残骸をフォルダごと消せるよう ``data/tmp`` へ
  集める — ``viewer/zip_drill.py::_zip_temp_base``）か、書き込み対象の隣
  （原子的置換用の一時ファイル — ``viewer/state._write_json_atomic``）。
* ``tempfile.gettempdir()`` は用途を問わず使わない。

この裁定は ``tests/test_repo_portability.py`` が静的に強制する
（プラグイン側は各 ``plugins/<id>/tests/`` の同名ガード）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


#: ビューアの統合ディスクキャッシュ（アスペクト比 / サムネイル索引 / フォルダ
#: プレビューの 3 表を 1 ファイルに持つ）。接続・WAL・剪定・破損復旧・close を
#: 1 系統にするための単一ファイル — 詳細は ``viewer/viewer_cache.py``。
VIEWER_CACHE_DB_NAME = "viewer_cache.db"

#: デコード済みサムネイル（WebP）の本体ツリー。索引は上の DB の ``thumb`` 表。
THUMB_BLOB_DIR_NAME = "thumb_cache"

#: 統合前に 3 ファイルへ分かれていた頃の DB 名。**移行コード**
#: （``viewer/viewer_cache.py::discard_legacy_cache_dbs``）だけが参照する —
#: 読み込みはせず、存在すれば削除して統合 DB を作り直す。
LEGACY_VIEWER_CACHE_DB_NAMES = (
    "viewer_aspect_cache.db",
    "viewer_thumb_cache.db",
    "viewer_folder_cache.db",
)


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _resolve_base_dir() -> Path:
    if _is_frozen():
        # PyInstaller --onedir: the EXE sits at the base.
        return Path(sys.executable).resolve().parent
    # Development: walk up from this file to the repo root
    # (src/snappix/common/paths.py -> repo root is parents[3]).
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class AppPaths:
    """Every runtime path, resolved once (see :func:`get_paths`).

    All fields are tool-neutral (the viewer and any companion tool read
    ``data`` / ``logs``); nothing here may point outside the portable base
    directory.
    """

    base: Path
    data: Path
    logs: Path
    log_file: Path
    #: Default browse root used when no previous root is recorded.  A plain
    #: folder next to the EXE — the user can (and usually will) open any
    #: other local / network folder instead.
    library: Path

    def ensure(self) -> None:
        for d in (self.data, self.logs, self.library):
            d.mkdir(parents=True, exist_ok=True)

    # ``data`` 配下の名前付きの居場所。フィールドではなくプロパティなのは、
    # ``AppPaths`` の構成要素（base / data / logs / library）を増やさずに
    # 「どのファイルがどこに在るか」の単一ソースだけを共有するため。

    @property
    def viewer_cache_db(self) -> Path:
        """統合ディスクキャッシュ（:data:`VIEWER_CACHE_DB_NAME`）のパス。"""
        return self.data / VIEWER_CACHE_DB_NAME

    @property
    def thumb_blob_dir(self) -> Path:
        """サムネイル WebP 本体のツリー（:data:`THUMB_BLOB_DIR_NAME`）。"""
        return self.data / THUMB_BLOB_DIR_NAME

    @property
    def legacy_viewer_cache_dbs(self) -> tuple[Path, ...]:
        """統合前の 3 DB のパス（移行コード専用 — 中身は読まない）。"""
        return tuple(
            self.data / name for name in LEGACY_VIEWER_CACHE_DB_NAMES
        )


_paths: AppPaths | None = None

#: The :class:`AppPaths` whose directories this process already materialised.
#: Identity, not a bare flag, so swapping ``_paths`` (tests point the tree at a
#: temp directory) re-runs ``ensure`` for the new tree.
_ensured: AppPaths | None = None


def _ensure_once(paths: AppPaths) -> None:
    """Create *paths*' directories the first time they are handed out.

    ``mkdir(exist_ok=True)`` is not free: CPython issues the ``mkdir`` syscall,
    catches ``FileExistsError`` and then ``stat``\\s to confirm it is a
    directory — two syscalls per directory, on *every* call, on whatever
    volume the portable base lives on (a NAS share, for callers on the GUI
    thread).  Nothing depends on re-creating them mid-session either: every
    writer already mkdirs its own parent before opening a file.
    """
    global _ensured
    if _ensured is paths:
        return
    paths.ensure()
    _ensured = paths


def get_paths(*, ensure: bool = True) -> AppPaths:
    """Return the cached :class:`AppPaths`.

    ``ensure=True`` (the default, preserving the historical behaviour) creates
    the ``data`` / ``logs`` / ``library`` directories so the frozen build
    materialises its runtime tree next to the EXE without the entrypoint
    having to remember to.  A caller that needs the directories therefore
    never depends on who happened to resolve the paths first — but the
    creation runs once per resolved tree rather than on every call (see
    :func:`_ensure_once`).

    Pass ``ensure=False`` for pure path *resolution* with no filesystem
    side-effects — e.g. read-only contexts or tests that only need to know
    where things live.
    """
    global _paths
    if _paths is not None:
        if ensure:
            _ensure_once(_paths)
        return _paths
    base = _resolve_base_dir()
    data = base / "data"
    paths = AppPaths(
        base=base,
        data=data,
        logs=data / "logs",
        log_file=data / "logs" / "viewer.log",
        library=base / "library",
    )
    if ensure:
        _ensure_once(paths)
    _paths = paths
    return paths
