"""GUI スレッドを凍らせないパス種別プローブ（B05 / L02 / 項目#109）.

オフライン / スリープ中のネットワーク共有に対する ``Path.is_dir()`` は
SMB タイムアウト（数十秒）まで呼び出しスレッドをブロックする。GUI
スレッドから存在確認したい呼び出し側は、この「デーモンワーカースレッド +
ハードタイムアウト」プローブを使うこと。

**同期的に見えるナビゲーションの存在確認は、例外なくここを通すこと**。
利用者は起動経路（``viewer/app.py`` — 引数起動 / ``last_root`` 復帰）、
ブックマークジャンプ（``main_window._jump_to_bookmark`` — 項目#109）、
そして ``main_window`` の同期ゲート一式 — ``set_root`` / ↑Up /
パンくずの祖先クリック / ドロップ受け / 投稿本文の 📁 リンク（いずれも
``_is_reachable_dir`` 経由、ドロップだけは dir/file の判別が要るので
:func:`probe_path_kind` を直接呼ぶ）— で、issue #132 で MRU クリックの
15.75 秒フリーズが実測されたのを機に合流した。健全性チェックの
「エクスプローラで開く」（``health_dialog._open_path`` — 渡るのは走査が
「読めない」と判定した行そのもので、最も止まりやすい）も同じ理由でここを
通る（レビュー 2026-09-03 項目 #105）。Qt 非依存・純 threading。
"""

from __future__ import annotations

import threading
from pathlib import Path

#: 進行中のプローブ（``path_str`` → ``(thread, result)``）。同じパスへの連投が
#: スレッドと SMB ハンドルを積み上げないよう、走っているものへ相乗りする。
#: :data:`_INFLIGHT_LOCK` は辞書の出し入れだけを守る（``join`` は外で行う）。
_INFLIGHT: dict[str, tuple[threading.Thread, list[str]]] = {}
_INFLIGHT_LOCK = threading.Lock()


def probe_path_kind(path_str: str, timeout: float = 2.0) -> str | None:
    """Classify *path_str* as ``"dir"`` / ``"file"`` / ``"missing"``.

    Runs the stat on a daemon worker thread so an offline network share can
    never hang the caller: returns ``None`` when the probe times out (share
    unreachable *right now* — likely waking from sleep) so the caller can
    assume a directory, proceed immediately and let the async scan surface
    any real failure with the 再試行 card.

    The single probe helper serves every synchronous-feeling navigation
    entry (レビュー 2026-08-27 項目#110 / #109 — the thread / timeout /
    "empty result means timeout" skeleton must not be re-duplicated per
    caller):

    * the positional-argument / association launch, where the argument may
      be a *file* (open its parent folder with the file pre-selected), and
      the in-app **drop** (``ViewerWindow.dropEvent``), which needs the same
      dir-vs-file split;
    * the ``last_root`` resume and every ``main_window`` navigation gate
      (``set_root`` / ↑Up / breadcrumb / 📁 post link, all via
      ``ViewerWindow._is_reachable_dir``), which only distinguish
      "definitely not a directory" from everything else — a timeout means
      "assume a directory" so the async scan reports the failure with the
      同じ [再試行] card;
    * the bookmark jump, which shows its 「このブックマークを削除」 dialog
      only on a definite miss.

    **同一パスへの連投は相乗りする**: タイムアウトしたワーカーはデーモンなので
    終了は妨げないが、放置すると撃つたびに新しいスレッド（と掴まれたままの
    SMB ハンドル）が積み上がる。死んだ共有のブックマークを 5 回叩けば 5 本
    増える形で、呼び出し側の規約（``main_window._navigate_history_steps`` が
    自分でだけ持っている「一括ジャンプは最終到達点だけ判定する」）に頼らずに
    済むよう、まだ走っている同じパスのプローブがあればその結果を待つ。

    **相乗りは「そのスレッドがまだ生きている間」だけ**。放棄されたプローブが
    後から完了しても表からは外れないので、生死を見ずに相乗りすると次の呼び
    出しは ``join`` が即返った**古い**結果を確定答として配ることになる
    （共有が復旧しているのにブックマークへ「削除しますか」を出す形）。生きて
    いなければ表から外して新しいプローブを起こす — スレッド 1 本 / パスの上限
    は保たれ、完走しないパスのエントリが単調増加する問題も同じ分岐で消える。
    「直近の否定結果を短い TTL でメモ化する」形を採らないのも同じ理由で、この
    関数の答えは「今この瞬間到達できない」であって「消えた」ではない。
    """
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(path_str)
        if existing is not None and existing[0].is_alive():
            th, result = existing
        else:
            result = []
            th = threading.Thread(
                target=_run_probe, args=(path_str, result),
                name="path-probe", daemon=True,
            )
            _INFLIGHT[path_str] = (th, result)
            th.start()
    th.join(timeout)
    if not result:
        # まだ返っていない — 表に残したままにして、次の呼び出しがこの 1 本へ
        # 相乗りできるようにする（新しいスレッドを増やさない）。
        return None
    with _INFLIGHT_LOCK:
        current = _INFLIGHT.get(path_str)
        if current is not None and current[1] is result:
            del _INFLIGHT[path_str]
    return result[0]


def _run_probe(path_str: str, result: list[str]) -> None:
    """プローブ本体（ワーカースレッド）。``result`` へ 1 件 append して終わる。"""
    try:
        p = Path(path_str)
        if p.is_dir():
            result.append("dir")
        elif p.is_file():
            result.append("file")
        else:
            result.append("missing")
    except OSError:
        result.append("missing")
