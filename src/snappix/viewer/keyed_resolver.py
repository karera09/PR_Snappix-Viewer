"""可視タイル限定の遅延解決が持つ「何を投げ、何が答えられたか」の帳簿。

NSFW 代表レーティングの解決と横断一覧の ``post.md`` 後追い解決は、どちらも
「ビューポートに見えているタイルの属性を、見えたぶんだけ off-thread で埋め、
同じ鍵は二度投げない」という**同じ意図**の実装で、受付済み集合 / 加算投入 /
バッチ分割 / 着地での併合 / リセットの 5 点を別々に手書きしていた。規約
（:meth:`~._runnable.GuardedStream.submit_batch` の docstring）が守っていたのは
**キャンセル**経路だけで、**失敗**経路（ワーカー例外・部分失敗・空応答）では
答えの返らない鍵が受付済みのまま残り、そのタイルはセッション中二度と解決
されなかった。上限で打ち切ったバッチの続きを蹴り直す経路も無かった。

ここはその帳簿を 1 本にする薄い層で、:class:`~._runnable.GuardedStream` の上に
載る:

* :meth:`KeyedResolver.request` は未受付の鍵だけを受け取り、``batch`` 件ずつ
  加算投入する。残りはバックログに積み、着地のたびに次を投げる（上限は
  「1 tick の投入量」であって「可視範囲の上限」ではない）。
* ワーカー呼び出しは resolver 側が ``try`` で包む。``_GuardedTask`` が例外を
  ``None`` へ畳む**前**に捕まえるので、「どの鍵のバッチだったか」を失わない。
  答えの返らなかった鍵は受付から**解放**され、次に可視範囲が動いたときに
  再要求される（同じ鍵を無条件に投げ直す輪は作らない）。
* :meth:`KeyedResolver.reset` が「解決器のリセット」の 1 実装（中断 + 帳簿の
  破棄）。

呼び出し側が持つのは「どの鍵が要るか」と「答えをどう併合するか」だけになる。
ワーカー本体は Qt 非依存の純関数のまま（``grid_tasks``）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, Signal

from ._runnable import GuardedStream, StreamJob


class KeyedResolver(QObject):
    """鍵つきバッチ解決の帳簿（1 つの :class:`GuardedStream` を駆動する）。

    *work* はワーカースレッドで ``work(values, job)`` として呼ばれ、答えを
    返す（例外は ``None`` 扱い）。*batch* は 1 回の投入で渡す最大件数
    （``0`` = 分割しない）。*answered* を渡すと、答えに含まれない鍵を
    「解決できなかった」とみなして受付から解放する — NSFW レーティングの
    ように「答えに無い = 未タグと確定」が正しい解決器では省く。
    """

    #: ワーカーの戻り値（バッチ 1 本ぶん）。受け取り側は併合だけを行う。
    landed = Signal(object)

    def __init__(
        self,
        stream: GuardedStream,
        work: Callable[[list[Any], StreamJob], object],
        *,
        batch: int = 0,
        answered: Callable[[object], Iterable[Any]] | None = None,
    ) -> None:
        super().__init__(stream)
        self._stream = stream
        self._work = work
        self._batch = max(0, int(batch))
        self._answered = answered
        self._requested: set[str] = set()
        self._backlog: list[tuple[str, Any]] = []
        self._inflight: dict[int, list[str]] = {}
        self._next_id = 0
        # ここだけは生の ``done`` + ``accepts``（``bind`` ではない）—
        # 理由は :meth:`_on_done` の docstring。
        stream.done.connect(self._on_done)

    # ---- 呼び出し側の口 -------------------------------------------------

    def requested(self, key: str) -> bool:
        """*key* が受付済み（投入済み / バックログ）か。"""
        return key in self._requested

    def mark_requested(self, keys: Iterable[str]) -> None:
        """*keys* を「答えは要らない」として受付済みにする（投入はしない）。

        **帳簿への直接書き込みで、本番の呼び出し元は無い** — テスト /
        フィクスチャが「この鍵はもう解決済み」という状態を、ワーカーを実際に
        走らせずに作るための口。本番側は「答えを既に持っているか」を自分の
        地図（``PostGrid._nsfw_rating_map`` / ``FolderEntry.metadata_loaded``）
        で判断してから :meth:`request` に渡す鍵を選ぶので、ここを通らない
        （帳簿は「投げた鍵」だけを持ち、答えの所有権は呼び出し側にある）。
        """
        self._requested.update(keys)

    def request(self, items: Sequence[tuple[str, Any]]) -> None:
        """未受付の ``(鍵, 値)`` を受け付けて、上限ぶんずつ投入する。"""
        for key, value in items:
            if key in self._requested:
                continue
            self._requested.add(key)
            self._backlog.append((key, value))
        self._pump()

    def pending(self) -> bool:
        """投入済み（未着地）またはバックログが残っているか。"""
        return bool(self._inflight or self._backlog)

    def reset(self) -> None:
        """中断して帳簿を捨てる（入場 / 退場 / 索引差し替え / 窓じまい）。

        受付済み集合を空にすると同時に、走行中・キュー待ちのタスクが共有する
        中断トークンを切る（対で動かす — 片方だけでは「受付済みのまま答えが
        来ない」鍵が残る）。
        """
        self._stream.cancel()
        self._requested.clear()
        self._backlog.clear()
        self._inflight.clear()

    # ---- 内部 -----------------------------------------------------------

    def _pump(self) -> None:
        if not self._backlog:
            return
        size = self._batch or len(self._backlog)
        chunk, self._backlog = self._backlog[:size], self._backlog[size:]
        batch_id = self._next_id
        self._next_id += 1
        self._inflight[batch_id] = [key for key, _value in chunk]
        values = [value for _key, value in chunk]
        self._stream.submit_batch(
            lambda job, b=batch_id, vs=values: (b, self._call(vs, job))
        )

    def _call(self, values: list[Any], job: StreamJob) -> object:
        try:
            return self._work(values, job)
        except Exception:  # noqa: BLE001 — 失敗は「答え無し」として扱う
            logger.debug("keyed resolve failed for {} items", len(values))
            return None

    def _release(self, keys: Iterable[str]) -> None:
        self._requested.difference_update(keys)

    def _on_done(self, token: int, payload: object) -> None:
        """バッチ 1 本の着地（``bind`` を使わない**唯一の例外**）。

        :meth:`~._runnable.GuardedStream.bind` は「着地ガードを受け側から
        消す」ための口で、そのぶん token を捨てる。ここは加算投入
        （``submit_batch``）なので同じ世代の着地が複数回あり、どのバッチの
        答えかは**ペイロードに載る ``batch_id``** で引く — 世代だけでは
        帳簿（``_inflight``）を引けない。ガードは
        :meth:`~._runnable.GuardedStream.accepts` を素で書く。
        """
        if not self._stream.accepts(token):
            return  # reset 済み（帳簿はそこで捨てている）
        if not (isinstance(payload, tuple) and len(payload) == 2):
            return  # 想定外の形 — 帳簿には触らない（防御）
        batch_id, result = payload
        keys = self._inflight.pop(batch_id, [])
        if result is None:
            # ワーカーが落ちた = このバッチについて何も答えられていない。
            # 受付から外して、次に可視範囲が動いたときに再要求させる。
            self._release(keys)
        else:
            if self._answered is not None:
                got = {str(key) for key in self._answered(result)}
                self._release([key for key in keys if key not in got])
            self.landed.emit(result)
        # 上限で打ち切った残りがあれば続きを投げる（着地がドレインを回す）。
        self._pump()


__all__ = ["KeyedResolver"]
