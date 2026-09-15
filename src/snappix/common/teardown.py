"""到達不能なストレージのために GUI スレッドを塞がない後始末（issue #132）.

SMB 共有が落ちた状態では ``data/`` への同期 I/O が 1 回あたり**数十〜数百秒**
ブロックする（VM 実測: 終了処理が GUI を 187〜195 秒凍らせ、共有を復旧しても
窓が閉じない = タスクマネージャー以外に逃げ道が無い）。sqlite の
``busy_timeout`` はロック待ちにしか効かず、OS の I/O タイムアウトには効かない。

「閉じる」はユーザーが決めた**後**の後始末なので、到達不能なストレージの
ために無限に待つ理由が無い。ここで提供するのは *絶対期限付きのベスト
エフォート実行* — 期限を過ぎたら呼び出し側（GUI スレッド）は待つのをやめて
先へ進み、ワーカーは走るだけ走らせて放棄する。

スケルトンは :mod:`snappix.viewer.path_probe` と同じ「デーモンワーカー +
ハードタイムアウト」（GUI スレッドから触れない I/O を隔離する既存の作法）で、
違いは**複数タスクを 1 本のワーカーで順に実行し、期限までに終わらなかった
ものを呼び出し側へ返す**こと:

* **順序を保つ** — 後始末には順序契約がある（フラッシュしてから close、
  再生成不能なデータを先に、など）。タスクごとにスレッドを立てると順序が
  崩れる上、同じ死んだ共有に対してスタックしたハンドルを本数分増やすだけで
  1 つも速くならない。
* **諦めたタスクが何かを呼び出し側が正確に知れる** — 再生成できるキャッシュ
  は黙って落としてよいが、再生成不能なデータ（``user_meta.db`` /
  ``viewer_state.json``）は警告ログを残す必要がある。

.. note::

   かつてここには 3 つ目の理由として「放棄したワーカーが全タスクのクロージャ
   を掴んだままになるので、未着手ストアの dealloc がメインスレッドを塞ぐのを
   防げる」と書いてあったが、**これは事実ではない**（2026-08-30 の実測で否定
   された）。生きているデーモンスレッドが 1 本でもあると CPython は最終の
   モジュールクリア / GC パスを走らせないので、``__del__`` はどのオブジェクト
   についても呼ばれない — クロージャ保持の有無は無関係。加えて
   ``win.close()`` 後に最後の Python 参照を落として ``gc.collect()`` しても
   ``ViewerWindow`` は PySide 側の保持で生き残るため、ストアはそもそも解放
   されない。**この誤った理由を復活させないこと**（「タスクごとにスレッドを
   立てる」改修を検討する人が、実在しない安全装置を根拠に判断してしまう）。
   1 本に束ねる理由は上の 2 つ ＋「死んだ共有に張り付くスレッド / ハンドルを
   本数分に増やさない」で十分。

**放棄しても壊れない**タスクにだけ使うこと。想定している 2 種は:

* sqlite の ``close`` — 落としても失うのは WAL のチェックポイントだけで、
  コミット済みデータは ``-wal`` に残り次回 open 時に回収される。
* tmp + ``os.replace`` の原子的な JSON 書き込み — 途中で放棄されても既存
  ファイルは壊れない（中間 tmp が 1 つ残る — 掃除は
  ``viewer/state.py::_write_json_atomic`` の残骸リーパーが次の成功書き込みで
  行う）。

**ログもプロセス終了をブロックし得る**点に注意（issue #132 F1 / R4）。放棄
したワーカーは daemon なので終了を止めないが、ログの実体も**同じ死んだ共有
上**（``data/logs/viewer_<pid>.log``）にあるため、かつては (a) 予算超過の警告を
吐こうとした GUI スレッドが loguru の enqueue パイプが埋まった時点（実測 9
レコード）で永久ブロックし、(b) 生き残った場合も loguru の atexit ハンドラが
ライタースレッドを無制限に join して「窓は閉じたのにプロセスが終わらない」へ
症状が移っていた。

これは**呼び出し側の後始末では直せない**（症状を検知した警告そのものが
塞がるので、「超過したときだけ有界化する」が成立しない）ため、
:mod:`snappix.common.logging` 側で機構ごと直してある — シンクは有界バッファ
＋専用ライタースレッド（``_BoundedSink``）で、``logger`` の呼び出しは保存先が
何をしていても返り、``logger.remove`` / atexit も 1 秒で打ち切る。**ここで
特別なログ後始末を呼ぶ必要は無い**（呼ぶ形へ戻さないこと）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from loguru import logger

__all__ = ["Deadline", "run_tasks_before_deadline"]


class Deadline:
    """単調時計で測る**絶対**期限（経過ぶんだけ減り、補充されない）.

    後始末は「ドレイン → 書き込み → close」のように**フェーズに分かれる**。
    1 つの ``Deadline`` を複数の :func:`run_tasks_before_deadline` へ渡せば
    それらの**合計**が上限になり（2 本目は :attr:`remaining` しか待たない）、
    フェーズごとに作れば各フェーズが満額を持つ。**どちらを選ぶかは呼び出し側
    の判断**で、``ViewerWindow.closeEvent`` は「予算を持たないドレインが間に
    挟まる」ため後者を選んでいる（issue #140 — 共有だと健全な保存先でも
    フェーズ 2 が ``join(0)`` になった。docs/claude/viewer/scanning.md）。
    """

    __slots__ = ("_at",)

    def __init__(self, budget_s: float) -> None:
        self._at = time.monotonic() + max(0.0, budget_s)

    @property
    def remaining(self) -> float:
        """期限までの残り秒（0 で下げ止まる）."""
        return max(0.0, self._at - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0


def run_tasks_before_deadline(
    tasks: Sequence[tuple[str, Callable[[], None]]],
    deadline: Deadline,
    *,
    thread_name: str = "bounded-teardown",
) -> list[str]:
    """*tasks* を 1 本のデーモンワーカーで順に実行し、期限まで待つ.

    Returns
    -------
    list[str]
        **期限までに終わらなかった**タスクのラベル（宣言順）。空リストなら
        全部完走している。呼び出し側はこの戻り値を見て、再生成不能なデータの
        取りこぼしだけを警告ログに落とすこと。

        報告は**保守的**（未完了側に寄る）: 予算が尽きた状態で呼ばれると
        ``join(0)`` になるので、ワーカーが 1 つも走らないうちに戻る。呼び出し
        側が「戻った直後に完了した」タスクまで未完了ラベルで受け取ることが
        あるが、この関数が答えられるのは「**期限内に完了を確認できたか**」
        だけなので、それで正しい（予算を使い切ったということは、直前の
        フェーズが既に「保存先が死んでいる」ことを実証している）。

    個々のタスクが送出した例外はここで握って警告ログにし、後続タスクは続行
    する（後始末は 1 つの失敗で残り全部を落としてよい処理ではない）。例外を
    握るのは *ワーカー側* である点が重要 — 未捕捉例外を
    ``threading.excepthook`` へ抜けさせると、終了直前の stderr にトレース
    バックが出るだけで呼び出し側は何も知れない。

    ただし例外で終わったタスクは「完了」として扱う（放棄ではない）ので、
    **この戻り値は「失敗」を報告できない** — 死んだ共有が block ではなく即
    ``OSError`` を返す種類なら、取りこぼしはここではなく各タスク側のログに
    出る。呼び出し側は「未完了ラベル = 期限切れ」とだけ読むこと。
    """
    remaining = [label for label, _ in tasks]
    lock = threading.Lock()

    def _run() -> None:
        for label, func in tasks:
            try:
                func()
            except Exception as exc:  # pragma: no cover (defensive)
                logger.warning("teardown task {} failed: {}", label, exc)
            finally:
                # 完了したものから外す — 期限切れ時に「どこで止まったか」を
                # 呼び出し側が正確に言えるようにするため（件数ではなくラベル）。
                with lock:
                    remaining.remove(label)

    # ``daemon=True`` は必須（外すと ``threading._shutdown`` が放棄した
    # ワーカーを join し、プロセスが永久に終わらなくなる = 元の症状が
    # 「窓は閉じたのにプロセスが残る」へ移るだけ）。
    worker = threading.Thread(target=_run, name=thread_name, daemon=True)
    worker.start()
    # 予算が尽きていれば ``join(0)``（Returns 節の「保守的な報告」参照）。
    worker.join(deadline.remaining)
    with lock:
        return list(remaining)
