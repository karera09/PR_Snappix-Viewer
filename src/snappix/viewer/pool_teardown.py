"""窓じまいで予算内にドレインできなかった ``QThreadPool`` の退避と、終了の有界化.

``QThreadPool`` のデストラクタは走行中のタスクを**無制限に**待つ
（``waitForDone(-1)`` 相当）。窓の ``closeEvent`` は配下の全プールを有界
ドレイン（``ViewerWindow._drain_loader_pools``）するが、それは「待ちを打ち
切る」だけで、死んだ共有の ``os.scandir`` / ヘッダ読みで止まっているタスクは
捨てられない。そのままだと待ちは予算の**外側**へ逃げる:

1. ``app.main()`` が戻って ``ViewerWindow`` が破棄されると、子のプールの
   デストラクタが GUI スレッドで I/O タイムアウト（15〜195 秒）まで止まる。
2. 親から外して Python 側で持ち続けても、インタプリタ終了時に PySide が
   Python 所有の QObject を全て破棄する（``destroyQCoreApplication``）ので、
   同じデストラクタが終了処理の中で走る — ``Py_IncRef`` で参照を漏らしても
   止まらないことを実測で確かめてある。

そこで 2 段で有界化する:

* :func:`drain_or_strand` — 予算内に空かなかったプールを親から外して
  モジュールの退避リストへ移す（窓の破棄 = 1. がプールのデストラクタを
  呼ばない）。
* :func:`exit_if_pools_stranded` — イベントループを抜けた後、退避したプールが
  1 つでも残っていれば、ログを有界に閉じてから ``os._exit`` で終わる（2. を
  通らない）。ユーザーの状態・ストアの保存は ``closeEvent`` の予算付き
  フェーズで済んでおり、ここで飛ばすのはインタプリタの後始末だけ。退避が
  無い通常の終了は従来どおり ``return`` で抜ける。
"""

from __future__ import annotations

import os
import sys

from loguru import logger
from PySide6.QtCore import QThreadPool

__all__ = ["drain_or_strand", "exit_if_pools_stranded", "stranded_pools"]

#: 予算内にドレインできず親から外したプール（破棄させないために保持する）。
_STRANDED: list[QThreadPool] = []


def drain_or_strand(pool: QThreadPool, timeout_ms: int) -> bool:
    """未着手のキューを捨て、走行中のタスクを最大 *timeout_ms* 待つ.

    空いたら ``True``。空かなければプールを親から外して退避し ``False`` —
    親（窓の QObject 木）の破棄がプールのデストラクタの無制限待ちに
    巻き込まれないようにする。
    """
    pool.clear()
    if pool.waitForDone(max(0, int(timeout_ms))):
        return True
    pool.setParent(None)
    if not any(p is pool for p in _STRANDED):
        _STRANDED.append(pool)
    return False


def stranded_pools() -> list[QThreadPool]:
    """退避中のプール（走行中のタスクが既に終わったものは除く）."""
    return [p for p in _STRANDED if p.activeThreadCount() > 0]


def exit_if_pools_stranded(code: int) -> None:
    """退避したプールがまだ走っていれば、終了処理を飛ばして *code* で終わる.

    イベントループを抜けた**後**にだけ呼ぶこと。走行中のタスクが既に
    終わっていれば何もしない（通常の ``return`` 経路で終われる）。
    """
    alive = stranded_pools()
    if not alive:
        return
    logger.warning(
        "exiting without waiting for {} worker pool(s) still blocked on I/O",
        len(alive),
    )
    try:
        # シンクは有界（``common/logging.py`` の ``_BoundedSink``）なので
        # 保存先が死んでいても戻る。``os._exit`` は atexit を走らせないので
        # ここで閉じておく。
        logger.remove()
    except Exception:  # pragma: no cover (defensive)
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # pragma: no cover (defensive)
            pass
    os._exit(code)
