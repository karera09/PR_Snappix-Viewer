"""即時フラッシュ（設定ファイルの部分書き込み）の有界待ちラッパ.

issue #132 は ``viewer_state.json`` のフル保存を「GUI スレッドで値を確定 →
予算付きワーカーで突き合わせ + 書き込み」の 2 段へ分けたが（
``ViewerWindow._persist_state_snapshot``）、**即時フラッシュ**の 5 経路
（ブックマーク追加 / 解除 / 管理ダイアログ / 検索保存 / 検索管理）と
``shared_prefs.json`` の 3 経路（テーマ / ライブラリ登録 / ライブラリ管理）は
GUI スレッドから同期 read+write を呼んだままだった（レビュー 2026-09-03
項目#48 / #52）。到達不能な SMB では 1 回の I/O が 15〜195 秒ブロックする
（VM 実測）ので、「ブックマークを 1 個足す」で窓が数十秒固まる。

ここに置くのは**その 2 段化を 1 か所へ寄せた小さな機構**だけ:
:class:`BoundedFlusher` が「同時に 1 本まで」「予算内でだけ完了を待つ」を
持ち、呼び出し元は GUI スレッドで値を確定してから
:meth:`BoundedFlusher.run` へ渡す。``main_window.py`` は既に規約の上限
（6,000 行）を超えているので、この機構はそちらへ足さずここに置く
（CLAUDE.md「肥大ファイルの抑制」）。

**呼び出し規約**:

* *work* は**ワーカースレッドで走る** — ウィジェットに触らないこと
  （Qt のウィジェット API は GUI スレッド限定）。渡す値は GUI スレッドで
  確定した複製にする（走行中に ``self._state`` を書き換えられても、
  書き出す内容が中途半端に混ざらないようにするため）。
* *work* は**放棄されても壊れない**ものにすること（tmp + ``os.replace`` の
  原子的書き込み / sqlite の close）。``common/teardown`` と同じ制約。
* UI の即時反映（メニュー再構築・トースト）は呼び出し元が**先に**行う。
  ここで待つのは「ディスクに載ったか」を名乗る必要がある経路だけ
  （N-07 の成否表示契約）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from loguru import logger

from ..common.teardown import Deadline, run_tasks_before_deadline

__all__ = ["PENDING_LIMIT", "BoundedFlusher"]

#: 合流（*coalesce*）で待たせられる書き込みの上限。人手の設定操作の速度に
#: 対して十分に大きく、死んだ保存先で無制限に積まない程度に小さい値。
PENDING_LIMIT = 8


class BoundedFlusher:
    """1 本ずつ・予算内でだけ待つ「即時フラッシュ」の実行器.

    *label* は :func:`~snappix.common.teardown.run_tasks_before_deadline` へ
    渡すタスク名（ログに出る）。*thread_name* を省略すると
    ``"<label>-flush"`` を使う。

    インスタンスは**保存先ごとに 1 つ**持つこと（``viewer_state.json`` の
    ブックマーク / 保存済み検索 / ``shared_prefs.json``）。同じファイルへの
    フラッシュを 1 本に絞るのが「暇なら set」の :class:`threading.Event` で、
    前回が返っていないうちに**ワーカーを**積まないための門になる（再指摘
    M-1 と同型 — 保存先が応答していない証拠なので、積み増してもスタックした
    ワーカーとハンドルが増えるだけ）。

    *coalesce* を真にすると、門に弾かれた *work* を**捨てずに合流**させる:
    飛行中のワーカーが自分の仕事を終えたあと、**同じスレッドで**待ち行列を
    空になるまで続けて実行する。スレッドもハンドルも増えないので上の門の
    趣旨は保たれ、呼び出し元から見た待ち時間は 0 になる。合流させてよいのは
    「最新のディスク内容へ**差分を適用**する」冪等な書き込みだけ
    （``add_library_root`` / ``apply_library_roots`` / テーマの 1 キー更新）。
    全量スナップショットを書く経路は*古い方が後に着地して勝つ*ので合流させ
    ないこと（既定の ``coalesce=False`` = 中間を捨てるのが正しい）。

    待ち行列は :data:`PENDING_LIMIT` 件で頭打ちにし、溢れたら従来どおり
    「載せられなかった」を返す（死んだ保存先で無制限に積まないため）。
    """

    __slots__ = ("_coalesce", "_idle", "_label", "_pending", "_lock", "_thread_name")

    def __init__(
        self,
        label: str,
        *,
        thread_name: str | None = None,
        coalesce: bool = False,
    ) -> None:
        self._label = label
        self._thread_name = thread_name or f"{label}-flush"
        self._coalesce = coalesce
        self._idle = threading.Event()
        self._idle.set()
        self._pending: list[Callable[[], Any]] = []
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return self._label

    def wait_idle(self, timeout_s: float = 10.0) -> bool:
        """飛行中のフラッシュが返るまで待つ（返れば ``True``）.

        投げっぱなし（``wait_s <= 0``）で出したワーカーの完了を待ちたい
        呼び出し元のための口。実運用では使わない — **テストの決定化**と、
        放棄したワーカーが隔離解除後のパスへ書くのを防ぐためのもの。
        """
        return self._idle.wait(timeout_s)

    def run(
        self, work: Callable[[], Any], *, wait_s: float,
    ) -> tuple[bool, Any]:
        """*work* をワーカーで走らせ、*wait_s* 秒だけ完了を待つ.

        Returns
        -------
        tuple[bool, Any]
            ``(landed, value)``。*landed* は「**予算内に完走を確認できた**」で、
            *value* は *work* の戻り値（*landed* が偽なら ``None``）。

            *landed* が偽になるのは 3 通り — (a) 前回のフラッシュがまだ
            返っていない、(b) 予算を使い切った、(c) *work* が例外を送出した
            （``run_tasks_before_deadline`` がワーカー側で握ってログにする）。
            呼び出し元から見ればどれも「ディスクに載ったと名乗れない」で
            同じなので、区別せず失敗側へ寄せてよい。測るのは**自分の work が
            返ったか**だけで、合流した後続がまだ走っていることは (b) に
            数えない（後続は別の書き込みで、こちらの成否を左右しない）。

            合流する器（``coalesce=True``）で前回が飛行中だったときは
            ``(True, None)`` — *work* は**捨てられず**飛行中のワーカーが
            続けて実行する（値は返せないので ``None``）。呼び出し元は
            「載せる約束は取り付けた」として成功側へ寄せてよい。ただし
            ``work`` の戻り値を使う経路（``apply_library_roots`` の突き合わせ
            結果など）は ``value is None`` を自前で救うこと。

        *wait_s* は「**自分の** *work* が載るまで」の予算で、前回のフラッシュを
        待つぶんは**別に**測る（:class:`~snappix.common.teardown.Deadline` は
        絶対期限なので、1 本を両方で共有すると前回待ちが食った残りで自分の
        work を測ることになり、載った書き込みを「載らなかった」と報告して
        しまう — 常駐の警告を出す経路なので実害が大きい）。代償として、
        前回が遅い保存先で走っている最中に撃つと最悪 ``2 * wait_s`` 待つ
        ことがあるが、これは前回のフラッシュが返っていないという**保存先が
        既に遅いことを実証した**状況に限られる。

        ``wait_s <= 0`` は**投げっぱなし**（オートセーブと同じ扱い）:
        ワーカーは起動するが待たないので常に ``(False, None)`` が返る。
        戻り値を使わない経路（テーマの ``shared_prefs.json`` 反映など）だけ
        この形で呼ぶこと。
        """
        if self._coalesce and self._enqueue(work):
            return True, None
        if not self._idle.is_set() and not self._idle.wait(max(0.0, wait_s)):
            logger.debug(
                "{} flush skipped: the previous flush has not returned",
                self._label,
            )
            return False, None
        out: list[Any] = []
        # 「**自分の** work が返ったか」の合図。合流した後続は同じスレッドで
        # 続けて走るので、ワーカー全体の完了（``run_tasks_before_deadline`` の
        # 未完了ラベル）を成否に使うと、自分の書き込みは載ったのに遅い後続の
        # せいで「載せられませんでした」を名乗ることになる（常駐警告を出す
        # 経路なので実害が大きい）。
        own_done = threading.Event()
        self._idle.clear()

        def _run() -> None:
            try:
                try:
                    out.append(work())
                finally:
                    own_done.set()
            finally:
                self._drain_pending()

        deadline = Deadline(wait_s)
        run_tasks_before_deadline(
            [(self._label, _run)], deadline,
            thread_name=self._thread_name,
        )
        if not own_done.wait(deadline.remaining):
            # 投げっぱなし（予算 0）は「未完了」が正常なので警告にしない。
            if wait_s > 0.0:
                logger.warning(
                    "{} flush did not land within {}s (the store is not"
                    " responding); the value stays in memory",
                    self._label, wait_s,
                )
            return False, None
        if not out:
            # 例外で終わった（ワーカー側でログ済み）。
            return False, None
        return True, out[0]

    # ------------------------------------------------------------- 合流の実装

    def _enqueue(self, work: Callable[[], Any]) -> bool:
        """飛行中のワーカーへ *work* を引き継げたら ``True``.

        ``_idle`` の判定と待ち行列への追加を 1 つのロックで囲うのが要点 —
        :meth:`_drain_pending` は**同じロックの中で**「空なら ``_idle`` を
        set して抜ける」ので、「空と判定されたあとに積む」取りこぼしが
        構造的に起きない。
        """
        with self._lock:
            if self._idle.is_set():
                return False
            if len(self._pending) >= PENDING_LIMIT:
                logger.warning(
                    "{} flush queue is full ({} waiting); the value stays in"
                    " memory", self._label, PENDING_LIMIT,
                )
                return False
            self._pending.append(work)
            return True

    def _drain_pending(self) -> None:
        """合流した書き込みを同じスレッドで空になるまで実行する."""
        while True:
            with self._lock:
                if not self._pending:
                    self._idle.set()
                    return
                work = self._pending.pop(0)
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - 1 件の失敗で列を止めない
                logger.warning("{} coalesced flush failed: {}", self._label, exc)
