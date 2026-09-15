"""キャンセル可能なデーモンワーカーのファンアウト（Qt 非依存・純 threading）。

``concurrent.futures.ThreadPoolExecutor`` の置き換え。CPython 3.9 以降、
``ThreadPoolExecutor`` のワーカーは**非デーモン**で、``threading._register_atexit``
に登録されたハンドラがインタプリタ終了時に全ワーカーを ``join`` する。つまり
「走らせるだけ走らせて放棄する」が成立せず、死んだ SMB 共有の I/O で止まって
いるワーカーが 1 本でもあれば、窓を閉じてもプロセスはその I/O タイムアウトぶん
終われない — ``common/teardown.py`` が冒頭で宣言する「後始末は絶対期限付きの
ベストエフォート、そのためにデーモンスレッドを使う」という設計と真っ向から
食い違い、症状は「窓は閉じたのにプロセスが終わらない」そのものになる。

ここは同じファンアウトを**デーモンスレッド**で組む:

* キャンセルはワーカー側でも見る — まだ始めていない項目は関数を呼ばずに
  飛ばす（``Future.cancel()`` と同じ効果）。
* 消費側は完了順に受け取り、いつ抜けてもよい。走行中のワーカーは放棄される
  が、デーモンなのでインタプリタ終了を妨げない。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Any

#: キャンセルで実行されなかった項目の結果値（``None`` と区別するための番兵）。
SKIPPED: Any = object()


def map_unordered(
    func: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    workers: int,
    name: str,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[tuple[Any, Any, BaseException | None]]:
    """*items* の各要素に *func* を並列適用し、``(item, result, exc)`` を完了順に返す。

    ``result`` が :data:`SKIPPED` なら、キャンセルにより *func* は呼ばれて
    いない。``exc`` が非 ``None`` なら *func* が送出した例外（呼び出し側が
    握る — 1 件の失敗でファンアウト全体を落とさない）。

    ワーカー数は ``min(workers, len(items))`` で、項目が 0 件ならスレッドを
    1 本も立てない。イテレータを最後まで回さずに抜けてよい（走行中のワーカー
    は放棄され、デーモンなのでプロセス終了を止めない）。
    """
    todo = list(items)
    if not todo:
        return
    n_workers = max(1, min(int(workers), len(todo)))
    in_q: queue.SimpleQueue = queue.SimpleQueue()
    out_q: queue.SimpleQueue = queue.SimpleQueue()
    for index in range(len(todo)):
        in_q.put(index)
    for _ in range(n_workers):
        in_q.put(None)

    def _worker() -> None:
        while True:
            index = in_q.get()
            if index is None:
                return
            if should_cancel is not None and should_cancel():
                out_q.put((index, SKIPPED, None))
                continue
            try:
                out_q.put((index, func(todo[index]), None))
            except BaseException as exc:  # noqa: BLE001 (reported to caller)
                out_q.put((index, SKIPPED, exc))

    for _ in range(n_workers):
        threading.Thread(target=_worker, name=name, daemon=True).start()

    for _ in range(len(todo)):
        index, result, exc = out_q.get()
        yield todo[index], result, exc
