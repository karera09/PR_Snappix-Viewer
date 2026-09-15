"""``PostGrid`` の off-thread ワーカー本体（Qt 非依存の純関数）。

左ペインは 4 種類の重い仕事をワーカースレッドへ出す — ``body:`` 判定 /
NSFW レーティング解決 / 「最近追加されたファイル」の再帰走査 / 横断一覧の
可視タイル post.md 後追い解決。以前はそれぞれが ``_XxxSignals(QObject)`` +
``_XxxTask(QRunnable)`` の対を手書きし、①世代カウンタ ②``CancelToken`` の
生成と保持 ③無親ブリッジ ④専用 1 スレッドプール ⑤``shutdown()`` での停止配線
という同じ 5 点セットを 4 回再現していた（レビュー 2026-09-03 項目 #56）。
5 点のどれか 1 つが落ちても症状が出るのに、落ちたことを検出する共通の場所が
無く、実際に curation-meta 側が ②⑤ を落としていた。

今は Qt 側（世代・キャンセル・プール・シグナル・ドレイン）を
:class:`~._runnable.GuardedStream` が一手に持ち、**このモジュールには「何を
計算するか」だけ**が残る。どれも GUI スレッドから呼んではならない
（``os.stat`` / ``post.md`` 読み / sqlite が入る）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .cancel_token import CancelToken
from .filter_query import _entry_body_text
from .folder_scan import (
    FolderEntry,
    RecentFilesScan,
    apply_preview,
    read_folder_preview_cached,
    walk_recent_files,
)

if TYPE_CHECKING:  # pragma: no cover — 型注釈だけ
    from ._runnable import StreamJob
    from .filter_query import _FilterTerm


def body_filter_matches(
    entries: list[FolderEntry],
    terms: "list[_FilterTerm]",
    cancel: CancelToken,
) -> tuple[set[str], set[str]] | None:
    """``body:`` 項を post.md 本文に照合する（GUI スレッド禁止）。

    ``body:`` 照合は候補フォルダごとに post.md を読んで解析する — コールドな
    NAS で子が数百あれば数秒の直列 I/O で、以前は ``_apply_filter_and_sort``
    のインラインで走り最初の 1 キーストロークで UI が固まっていた。

    返すのは ``(and_matched, or_matched)`` のパス文字列集合の対 (#4):
    *and_matched* は ``~`` の付かない全 body 項を満たすエントリ（include は
    在り / exclude は無し — 旧同期実装と同じ意味論）、*or_matched* は
    ``~body:`` プールのいずれかに当たったエントリ。どちらも **現在の全
    エントリ**に対して計算するので、body 以外の項が変わっても着地した判定は
    有効なまま使える。読めない / 無い post.md は空本文として扱う。

    キャンセル済みなら ``None``（呼び出し元は「答えなし」として捨てる）。

    ``filter_query._BODY_CACHE``（mtime 検証つき）はここで参照・充填される。
    プールが単一スレッドで GUI スレッドが ``body:`` を評価しないので、この
    キャッシュは実質的に単一所有者のまま。
    """
    and_terms = [t for t in terms if not t.or_group]
    or_terms = [t for t in terms if t.or_group]
    and_matched: set[str] = set()
    or_matched: set[str] = set()
    for entry in entries:
        if cancel.is_cancelled():
            return None
        try:
            body = _entry_body_text(entry)
        except Exception:  # noqa: BLE001 — 1 件の壊れた post.md で判定全体を落とさない
            # 兄弟 2 本（``recent_files_walk`` / ``curation_metadata``）と
            # 同じ規律。読めない post.md は空本文として扱う契約なので、
            # 例外が出た 1 件も同じ扱いへ落とす（判定全体を ``None`` にすると
            # 打った ``body:`` が黙って効かなくなる）。
            logger.debug("body filter read failed for {}", entry.path)
            body = ""
        ok = True
        for term in and_terms:
            present = term.value in body
            if present if term.exclude else not present:
                ok = False
                break
        if ok:
            and_matched.add(str(entry.path))
        if any(t.value in body for t in or_terms):
            or_matched.add(str(entry.path))
    if cancel.is_cancelled():
        return None
    return and_matched, or_matched


def nsfw_ratings(
    tag_index,
    folders: list[Path],
    files: list[Path],
    cancel: CancelToken,
) -> dict[str, str]:
    """タイル群の代表レーティングを ``tags.db`` から引く（GUI スレッド禁止）。

    NSFW ビュー抑制 (項目 2-1) は描画 / 再構築のホットパスで sqlite を叩いて
    はならないので、ここで解決する: フォルダタイルは部分木の最も強い区分
    (:meth:`tag_db.TagIndex.folder_representative_ratings`)、ファイルタイルは
    自分の支配的区分 (:meth:`~tag_db.TagIndex.image_ratings`)。ホストは着地
    したぶんを in-memory マップへ併合し、次の再構築で隠す（「判るまで見せる →
    判ったら隠す」）。

    フォルダ側は 1 フォルダあたり部分木の範囲クエリなので、フォルダ数ぶん
    ループが回る。索引は「フォルダ間で ``is_cancelled()`` を poll し、中止
    されたバッチはそこまでの結果を返す」契約のキャンセル口を持つので、
    ``cancel`` をそのまま渡す（型注釈は付けない — 索引は provider 由来の
    オブジェクトで本体はそのクラスを import しない）。ファイル側の
    ``image_ratings`` は 1 クエリなのでキャンセル口を持たない。
    """
    out: dict[str, str] = {}
    if tag_index is None:
        return out
    # ここは**例外を握らない**: レーティングの解決器 (``KeyedResolver``) は
    # 「無評価も正当な答え」なので着地した結果からは鍵を解放せず、解放は
    # ワーカーが落ちた経路（結果 ``None``）だけが行う。ここで握って部分結果を
    # 返すと、答えられなかったパスが受付済みのまま固定され、次に可視範囲が
    # 動いても二度と再要求されない（＝そのタイルは一生隠れない）。
    if folders and not cancel.is_cancelled():
        out.update(
            tag_index.folder_representative_ratings(folders, cancel=cancel)
        )
    if files and not cancel.is_cancelled():
        out.update(tag_index.image_ratings([str(p) for p in files]))
    return out


def recent_files_walk(
    root: Path, limit: int, job: "StreamJob",
) -> RecentFilesScan | None:
    """「最近追加されたファイル」の木を走査する（GUI スレッド禁止）。

    コールドな NAS では分オーダーで走るので、走査器の（スロットルされた）
    件数を ``job.report`` で流し、ビューが「N 件走査」を出せるようにする —
    再帰検索の ``_on_recursive_progress`` と同じ生きたフィードバック。

    キャンセルされた走査は ``None`` を返す（部分結果は捨てる）。例外も
    ``None`` で、ホストは「ファイルはありません」ではなく読み取り失敗として
    見せる。
    """

    def _progress(scanned: int) -> None:
        job.report(scanned)

    try:
        scan = walk_recent_files(
            root,
            limit=limit,
            should_cancel=job.cancel.is_cancelled,
            on_progress=_progress,
        )
    except Exception:  # noqa: BLE001 — a walk failure must not kill the pool
        return None
    if job.cancel.is_cancelled():
        return None
    return scan


def curation_metadata(
    entries: list[FolderEntry], folder_cache, cancel: CancelToken,
) -> list[FolderEntry]:
    """横断一覧の**可視**タイルぶんだけ ``post.md`` を読む (N-49 後半)。

    横断一覧は軽量な索引として組む — 母集合がライブラリ横断（複数ボリューム
    にも及ぶ）なので、集める段階でメタデータを読むと最初のタイルが出る前に
    全件ぶんの NAS 往復が要る（``user_meta_parts.resolve.build_curation_entry``）。
    その代償
    として投稿フォルダはタイトル / 投稿日 / ♡ 抜きで届く — タイルが「どこの
    何か」を語るのに一番要る情報が。

    そこで解決は**後追い**で、**ビューポート内のタイルだけ**行う（サムネイル
    ローダー / アスペクトプローバと同じ規律）。各フォルダはスキャナのメタ
    パスと**同じ** ``read_folder_preview_cached`` → :func:`apply_preview` を
    通るので、通常閲覧で一度見たフォルダは ``folder_preview_cache`` の温かい
    ヒット（ファイルシステムアクセスゼロ）になり、得られる表示は平常グリッド
    と 1 バイト違わない（「投稿タイルが何を言うか」の第二実装を作らない）。
    """
    out: list[FolderEntry] = []
    for entry in entries:
        if cancel.is_cancelled():
            return []
        try:
            parsed, thumb_marker, non_thumb_marker, file_names = (
                read_folder_preview_cached(
                    entry.path, entry.mtime, folder_cache,
                    should_cancel=cancel.is_cancelled,
                )
            )
        except Exception:  # noqa: BLE001 — one bad folder must not kill the pool
            logger.debug("curation metadata read failed for {}", entry.path)
            # 読めなかったフォルダは payload に**載せない**。呼び出し側の
            # ``KeyedResolver`` は「答えに含まれない鍵 = 解決できなかった」と
            # 読んで受付から解放するので、次に可視範囲が動いたときに再挑戦
            # できる（無変更のまま返すと「答えた」ことになり、受付済みのまま
            # 二度と再要求されない = タイトル / 投稿日 / ♡ の無い表示で固定）。
            continue
        out.append(
            apply_preview(
                entry, parsed, thumb_marker, non_thumb_marker, file_names
            )
        )
    if cancel.is_cancelled():
        return []
    return out


__all__ = [
    "body_filter_matches",
    "curation_metadata",
    "nsfw_ratings",
    "recent_files_walk",
]
