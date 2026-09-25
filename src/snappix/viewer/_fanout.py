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

入口は 2 つ。項目が最初から揃っているなら :func:`map_unordered`、処理中に
項目が増える（ウォークのフロンティアのように、結果を見て次を投入する）なら
:class:`DaemonExecutor` — ``ThreadPoolExecutor`` と同じ ``submit`` →
``Future`` の形で、``shutdown`` が**待たない**ことだけが違う。
``DaemonExecutor`` の ``Future`` を完了順に受け取る側は :class:`CompletionQueue`
（1 ステップずつ）か :func:`iter_completed`（投入済みの全件）を使う。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
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


class CompletionQueue:
    """``Future`` を完了順に受け取るキュー（完了 1 件 = キュー操作 1 回）。

    ``as_completed`` は次の完了まで戻らず止まった I/O の最中のキャンセルを
    拾えない。``wait(FIRST_COMPLETED)`` は呼ぶたびに未完了の全件へウェイタを
    付け外しする。:meth:`take` はタイムアウトごとに呼び出し側へ制御を返す。
    """

    def __init__(self) -> None:
        self._q: queue.SimpleQueue = queue.SimpleQueue()

    def watch(self, fut: Future) -> None:
        """*fut* の完了をこのキューへ積ませる（完了済みならその場で積まれる）。"""
        fut.add_done_callback(self._q.put)

    def take(self, timeout: float) -> list[Future]:
        """最初の 1 件を最大 *timeout* 秒待ち、溜まっている分もまとめて返す（無ければ空）。"""
        try:
            done = [self._q.get(timeout=timeout)]
        except queue.Empty:
            return []
        while True:
            try:
                done.append(self._q.get_nowait())
            except queue.Empty:
                return done


def iter_completed(
    futures: Iterable[Future],
    *,
    should_cancel: Callable[[], bool],
    poll_s: float = 0.1,
) -> Iterator[Future]:
    """*futures* を完了順に返す。*should_cancel* は *poll_s* 秒ごとと各 yield の
    直前に見て、真なら打ち切る（返さなかった分の後始末は呼び出し側が持つ）。"""
    done = CompletionQueue()
    remaining = 0
    for fut in futures:
        done.watch(fut)
        remaining += 1
    while remaining and not should_cancel():
        for fut in done.take(poll_s):
            remaining -= 1
            if should_cancel():
                return
            yield fut


class DaemonExecutor:
    """動的投入のデーモン版 executor（``ThreadPoolExecutor`` の置き換え）。

    ``submit`` は :class:`concurrent.futures.Future` を返すので、
    ``add_done_callback`` / ``cancel`` / ``as_completed`` など呼び出し側の
    コードはそのまま使える。違いは 2 点:

    * ワーカーは**デーモン**スレッド（最大 *workers* 本、投入に応じて遅延起動）
      — インタプリタ終了時に join されない。
    * :meth:`shutdown` は**待たない** — まだ始まっていない ``Future`` を
      キャンセルし、ワーカーに終了を告げて即座に返る。走行中の項目（死んだ
      共有の ``os.scandir`` など）は放棄され、終われば黙って消える。
      ``with`` の ``__exit__`` も同じ（``ThreadPoolExecutor`` の
      ``shutdown(wait=True)`` のように止まっている I/O を待たない）。
    """

    def __init__(self, workers: int, *, name: str) -> None:
        self._max_workers = max(1, int(workers))
        self._name = name
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._started = 0
        self._closed = False

    def submit(self, fn: Callable[..., Any], /, *args: Any) -> Future:
        """``fn(*args)`` を投入し、その ``Future`` を返す。"""
        fut: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot submit after shutdown")
            self._q.put((fut, fn, args))
            if self._started < self._max_workers:
                self._started += 1
                threading.Thread(
                    target=self._worker, name=self._name, daemon=True,
                ).start()
        return fut

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fut, fn, args = item
            # キャンセル済み（shutdown が先に来た / 呼び出し側が cancel した）
            # なら呼ばない。以降は RUNNING になり cancel できない。
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 (reported via future)
                fut.set_exception(exc)
            else:
                fut.set_result(result)

    def shutdown(self) -> None:
        """未着手の項目をキャンセルし、ワーカーへ終了を告げる（待たない）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()
        for _ in range(started):
            self._q.put(None)

    def __enter__(self) -> "DaemonExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
