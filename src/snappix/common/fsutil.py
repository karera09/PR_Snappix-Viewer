"""Tiny generic filesystem predicates + pure path arithmetic for the shared layer.

Nothing here knows about any particular tool — these are plain helpers any
``common``-consumer may use.  Everything below is either a single ``stat`` or
pure ``.parts`` arithmetic; nothing imports Qt, so Qt-free layers (the viewer's
``user_meta`` store, the scan workers) may use it from a worker thread.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def existing_nonempty(path: Path) -> bool:
    """True when *path* exists as a non-empty file.

    Generic skip-if-exists predicate: a zero-byte file is treated as an
    incomplete / interrupted write and reported as absent, so callers that
    use it to skip work must only ever leave *complete* files at the checked
    destination (write to a temp name and ``replace()`` on success).

    That contract is load-bearing: consumers outside this module (optional
    plugins that write files use it as their skip-if-exists gate)
    depend on the exact "zero byte == absent" semantics, so do not change or
    remove this helper on the assumption that it is unused.
    """
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False


def relative_parts(root: Path, base: Path) -> tuple[str, ...] | None:
    """Path components of *root* below *base*, or ``None`` when *base* is not
    an ancestor-or-self of *root*.

    Pure ``.parts`` comparison (no filesystem I/O) so it stays NAS-free and
    cross-platform testable — a ``PureWindowsPath`` base only matches a
    ``PureWindowsPath`` root because their anchors differ otherwise.  Returns
    an empty tuple when *base == root*.

    照合は ``os.path.normcase`` 済みの成分同士で行う: Windows では大小文字が同一視されるので、外部ツール由来やコマンド
    ライン起動で casing のずれた root（``d:\\photos\\sub`` と登録ライブラリ
    ``D:\\Photos``）でもライブラリ基準が外れない。ケース非区別はこのリポジトリ
    の他のパス照合（``main_window._subtree_overlaps`` / ``nav_rail`` /
    ``folder_preview_cache`` の COLLATE NOCASE）と同じ方針で、ずれると
    「レールはライブラリ内と言うのにパンくずはライブラリ外の祖先まで出す」と
    いう非対称が起きる。返す ``rel`` は元の（正規化していない）成分のまま。
    """
    rp = root.parts
    bp = base.parts
    if not bp or len(bp) > len(rp):
        return None
    if [os.path.normcase(p) for p in rp[: len(bp)]] != [
        os.path.normcase(p) for p in bp
    ]:
        return None
    return rp[len(bp):]


def pick_library_base(
    root: Path,
    library_bases: list[tuple[Path, str]] | None,
) -> tuple[Path, str, tuple[str, ...]] | None:
    """Pick the registered library *root* should be displayed relative to.

    Returns ``(base, display_label, relative_parts)`` for the **shallowest
    (outermost)** base that is an ancestor-or-self of *root*, or ``None`` when
    *root* sits outside every library (最も深い基準を採ると入れ子登録で
    再ルート化してしまう）。

    この規則は関数 1 つに集約する。パンくずと全文検索ダイアログの「検索対象」
    表記が別々に基準を選ぶと、入れ子登録のライブラリでは 2 つの画面が別の
    ライブラリ名を名乗ってしまう。両者ともここを通す。

    横断キュレーション一覧のタイル説明
    (``viewer.user_meta_parts.resolve._curation_display_name``) も同じ基準を使う
    ため、この 2 つの純パス演算は Qt を引き込まない ``common`` 側に置く
    (``viewer.breadcrumb`` は再エクスポートするだけ)。
    ``user_meta`` はワーカースレッドで動く Qt 非依存層なので、PySide6 を import
    する ``breadcrumb`` へは依存させられない。

    Pure ``.parts`` arithmetic (no filesystem I/O) so it stays NAS-safe and
    cross-platform testable.
    """
    if not library_bases:
        return None
    best: tuple[Path, str, tuple[str, ...]] | None = None
    for base, label in library_bases:
        rel = relative_parts(root, base)
        if rel is None:
            continue
        if best is None or len(base.parts) < len(best[0].parts):
            best = (base, label, rel)
    return best


def tail_display_labels(raws: "list[str]") -> dict[str, str]:
    """Raw path strings → short display labels (last segment, disambiguated).

    表示名を持たないライブラリの見え方は全画面で「末尾フォルダ名」に揃える。
    生パスの全文はツールチップの仕事で、ラベルは場所の**名前**を名乗る
    （ライブラリ管理ダイアログ / ナビレール / パンくず基点 / ファイル ▸
    ライブラリ submenu が同じ規則）。

    末尾セグメントが他の登録ルートと衝突するときだけ ``親/名前`` へ 1 段
    伸ばす（``D:/A/photos`` と ``E:/B/photos`` → ``A/photos`` / ``B/photos``）。
    親まで同名ならそれ以上は伸ばさない — 弁別はツールチップに委ねる（無限に
    伸ばすとメニュー幅で生パス全文と変わらなくなる）。ドライブ直下などで
    末尾名が取れないパスは生の文字列のまま。純パス演算のみ（NAS-free）。
    """
    tails: dict[str, str] = {}
    counts: dict[str, int] = {}
    for raw in raws:
        tail = Path(raw).name or raw
        tails[raw] = tail
        counts[tail] = counts.get(tail, 0) + 1
    labels: dict[str, str] = {}
    for raw in raws:
        tail = tails[raw]
        if counts[tail] > 1 and Path(raw).name:
            parent = Path(raw).parent.name
            labels[raw] = f"{parent}/{tail}" if parent else raw
        else:
            labels[raw] = tail
    return labels


def write_text_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* through a sibling temp file + ``os.replace``.

    A direct ``write_text`` truncates the existing file first, so a kill /
    disk-full / NAS drop mid-write leaves an empty or half-written file.
    ``os.replace`` is atomic on every filesystem this product targets, so a
    reader always sees either the old bytes or the new ones — never a
    truncated file.  Use it for any file whose *existing* content matters
    (an ownership marker, a settings snapshot, a baseline other runs read).

    The temp name is **per call** (``tempfile.mkstemp``), not per process: a
    fixed ``.tmp`` name lets two writers write and ``os.replace`` the *same*
    temp file, so one can publish the other's half-written bytes — and a
    PID-based name is exactly as shared whenever the two writers live in the
    same process, which they do here (an in-process plugin updates metadata
    from a worker thread while the GUI thread writes its own files).  Same
    scheme, and the same reasoning, as ``common/shared_prefs`` and
    ``viewer/state``.  A failed write removes its own temp (best effort) and
    re-raises; the caller decides what an unwritable destination means.

    Unique names are never overwritten by the next write, so an abandoned
    temp (a process killed between write and replace) would otherwise
    accumulate: :func:`reap_stale_tmps` sweeps the siblings by TTL right
    after a successful replace.
    """
    tmp_name = _mkstemp_sibling(path)
    try:
        with open(tmp_name, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # best-effort cleanup; the caller reports the real failure
        raise
    reap_stale_tmps(path)


def _mkstemp_sibling(path: Path) -> str:
    """Create an empty ``<name>.<rand>.tmp`` next to *path* and close its fd.

    ``dir=`` is mandatory (ポータビリティ規約): the temp must land next to the
    destination, never in the OS temp directory under the user's home.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    os.close(fd)
    return tmp_name


#: Grace period before :func:`reap_stale_tmps` treats a temp sibling as debris.
#: A healthy write takes milliseconds; the margin keeps a concurrent writer's
#: in-flight file (or one stalled on a dead share — SMB の I/O タイムアウトは
#: VM 実測で最大 195 秒) alone.
STALE_TMP_AGE_S = 600.0


def reap_stale_tmps(path: Path, max_age_s: float = STALE_TMP_AGE_S) -> None:
    """Remove old ``<name>.<rand>.tmp`` siblings left by abandoned writes.

    Per-call unique names are never reused, so a writer killed (or a budgeted
    teardown that abandons its worker without raising) between ``mkstemp`` and
    ``os.replace`` leaves a file nothing will ever overwrite.

    Call it **only right after a successful replace**: at that moment the
    destination is demonstrably responsive, so this adds no I/O to a dead
    share.  Anything newer than *max_age_s* is left alone — it may be a
    concurrent writer's in-flight temp.  Failures are ignored (on Windows a
    temp another process still holds open simply cannot be unlinked, which
    protects live writers).
    """
    cutoff = time.time() - max_age_s
    try:
        stale = [
            p
            for p in path.parent.glob(f"{path.name}.*.tmp")
            if p.is_file() and p.stat().st_mtime < cutoff
        ]
    except OSError:  # pragma: no cover (defensive)
        return
    for leftover in stale:
        try:
            leftover.unlink()
        except OSError:
            pass
