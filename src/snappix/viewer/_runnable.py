"""Generic "guarded off-thread task" helper for the viewer.

Across the viewer, a recurring pattern moves a small blocking call (a single
``os.stat`` on a cold NAS folder, a ``find_first_image`` BFS, a
``post.md``/PDF/text read) off the GUI thread and delivers the result back
via a Qt signal — with a *generation / token guard* so that when the user
navigates on before the worker finishes, the stale result is dropped instead
of overwriting the current view.  Every such site hand-rolled its own
``_XxxSignals(QObject)`` + ``_XxxTask(QRunnable)`` pair (see #134); this module
factors out the common "one integer token + one payload object" shape so the
call sites shrink to a single :class:`GuardedStream` field plus one
``submit`` call.

Scope (拡張 — レビュー 2026-09-03 項目 #56).  当初この helper は「1 つの
callable が 1 つの ``(token, payload)`` を返す」場合だけを担い、*複数シグナル*
（進捗つき）や *協調キャンセル* を要するタスクは bespoke に逃がしていた。その
判断は ``post_grid`` の 4 組（body / nsfw / recent / curation-meta）が全部
「例外」側に落ち、そのうち 1 組が実際に shutdown 配線を落としたところで破綻した
（項目 #2 の実害）。そこで :class:`GuardedStream` に

* **ストリーム所有の協調キャンセル** — :class:`~.cancel_token.SessionOwner` を
  内側に持ち、世代（token）と ``CancelToken`` が常に一緒に動く（項目#35 で
  スキャナ側が学んだ不変条件をそのまま再利用する）。
* **進捗シグナル** ``progress(token, payload)`` — ワーカーは :class:`StreamJob`
  の ``report()`` から投げる（分オーダーの走査が「探しています…」で固まらない）。
* **加算的な投入** :meth:`GuardedStream.submit_batch` — 「先行を捨てずに積む」
  規律（受付済みとして記録した鍵の答えを、後続の投入が黙って捨てない）を
  保つための口。その帳簿そのものは
  :class:`~.keyed_resolver.KeyedResolver` が持つ。

を足し、bespoke の除外条件を無くした。**新しい off-thread タスクは
:class:`GuardedStream` を使うこと** — 窓の ``_drain_loader_pools`` が
``findChildren(GuardedStream)`` で列挙して有界ドレインに載せるので、
「``shutdown()`` への配線を足し忘れる」が原理的に起こせない。

投入の 4 形（規律ごとに 1 つ）:

===================== ============ =========== ==================================
メソッド              先行を捨てる token        使いどころ
===================== ============ =========== ==================================
``submit``            はい         新規        キャンセル不要の単発プローブ
``submit_job``        はい         新規        最新だけが要る重い仕事（走査・判定）
``submit_batch``      いいえ       現行を共有  受付済みを取りこぼせない加算バッチ
``submit_detached``   はい         新規        放棄してよい長大な FS 仕事（プール外）
===================== ============ =========== ==================================

:class:`GuardedStream` は 1 つの関心（ウィジェットの 1 用途）につき 1 本作り、
専用の単一スレッド ``QThreadPool`` を内側に持つ。連続した ``submit()`` は
未着手のキュー（``pool.clear()``）を捨て、1 ストリームは 1 ワーカースレッドに
制限されるので、要求の突風（フォルダツリーで ↓ を押しっぱなし・プレイリストの
ホイールスクロール）が BFS / scandir をグローバルプールへ積み上げることも、
他の全ウィジェットが共有する短命プローブを飢えさせることも無い。

Migration recipe (bespoke pair → :class:`GuardedStream`)::

    # in __init__ — one long-lived stream per concern, parented to the
    # owning widget so the pool dies with it:
    self._info_stream = GuardedStream(self)
    self._info_stream.bind(self._on_info)  # slot: (payload,)

    # on each request (replaces the hand-written token bump + dispatch):
    self._info_stream.submit(lambda: _stat_label(path))

    # the slot receives only the landings that are still current:
    def _on_info(self, payload: object) -> None:
        ...

**着地ガードは書かない** — :meth:`GuardedStream.bind` を使う。``bind`` 系
（:meth:`~GuardedStream.bind` / :meth:`~GuardedStream.bind_progress` /
:meth:`~GuardedStream.bind_failed`）は :meth:`GuardedStream.accepts` を通った
着地だけを slot へ渡すので、token もガードの 2 行も受け側から消える
（:meth:`~GuardedStream.bind_cancelled` だけは選別の軸が違う — ストリームが
止まったときだけ呼ぶ。:meth:`GuardedStream._relay_cancelled` 参照）。生の ``done.connect`` + :meth:`accepts` を
書いてよいのは :class:`~.keyed_resolver.KeyedResolver` だけ（ペイロードに
載る ``batch_id`` で帳簿を引くため、token を捨てられない）。

``token != latest_token()`` 型の比較は**どこにも書かない**。token は
:class:`~.cancel_token.SessionOwner` の世代であり、``cancel()`` は現行
セッションが無ければ何もしない冪等な操作（世代を進めない）なので、
「一度もキャンセルしていない窓」と「idle 中にキャンセルした窓」で意味が
変わる。:meth:`accepts` は「セッションが生きている **∧** 世代一致」で、
キャンセル済みの窓では常に False になる。

The payload is delivered as ``object`` (Qt has no generic signal), so the
receiver casts/narrows it.  Exceptions raised by ``work`` are swallowed and
reported as a ``None`` payload — callers that need to distinguish "no result"
from "error" should return a sentinel from ``work`` instead of raising.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from pathlib import Path
from types import MethodType
from typing import Any, Callable, NamedTuple

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..common.teardown import Deadline, run_tasks_before_deadline
from .cancel_token import ScanSession, SessionOwner


class StreamJob(NamedTuple):
    """1 リクエストの実行文脈（ワーカースレッドが受け取る唯一の引数）。

    * ``token`` — このリクエストの世代。着地の選別は
      :meth:`GuardedStream.bind` が行うので、受け側がこれを読むことは無い。
    * ``cancel`` — このリクエストの :class:`~.cancel_token.CancelToken`。
      ループの刻みで ``is_cancelled()`` を見て早期に抜けること（見ない
      ワーカーでも正しく動くが、死んだ共有では窓じまいが待たされる）。
    * ``report`` — 途中経過を ``progress(token, payload)`` へ流す。キャンセル
      済みなら黙って捨てる。
    """

    token: int
    cancel: ScanSession
    report: Callable[[object], None]


class StreamOutcome(NamedTuple):
    """多値の着地を 1 本の ``done`` へ畳むための型（``kind`` + ``value``）。

    ``done(token, payload)`` はペイロードを 1 つしか運べない。結末が複数ある
    仕事（読めた / 上限超過 / 壊れている / 対象なし …）を bespoke なペアは
    シグナルを増やして表していたが、増えたシグナルは片方だけ配線される。
    :class:`GuardedStream` へ寄せるときは結末を ``kind`` の値に落として
    1 本に畳むこと。

    受け側は ``kind`` を ``Literal`` で閉じ、``match`` で分岐し、``case _:``
    に ``typing.assert_never`` を置く — 結末を足したときに、分岐を足し忘れた
    受け側を型検査が指す::

        _ReadKind = Literal["text", "too_large", "unreadable"]

        def _on_read(self, payload: object) -> None:
            if not isinstance(payload, StreamOutcome):
                return
            kind = cast(_ReadKind, payload.kind)
            match kind:
                case "text":
                    self._show(cast(str, payload.value))
                case "too_large":
                    self._show_limit(cast(int, payload.value))
                case "unreadable":
                    self._show_error()
                case _:
                    assert_never(kind)

    ``value`` は既定 ``None``（値を持たない結末のため）。
    """

    kind: str
    value: object = None


class GuardedSignals(QObject):
    """Signal bridge for :func:`run_detached`: ``done(token, payload)``.

    プール**外**（デーモンスレッド）で走る仕事の着地口。世代も専用プールも
    持たないので、off-thread の仕事を GUI へ返す通常の経路には使わない —
    そちらは :class:`GuardedStream`。残っているのは「窓が終わっても走って
    よいが、誰も待たない」FS プローブ（:func:`dir_exists_probe`）専用の
    ティアで、ダイアログを開き直すたびにブリッジごと作り直される。

    Construct **one** per logical stream and keep it referenced on the owning
    widget for the widget's lifetime — a fresh instance per request would risk
    a queued cross-thread ``emit`` landing on a garbage-collected QObject.
    """

    done = Signal(int, object)  # (token, payload — meaning is caller-defined)


class GuardedStream(QObject):
    """A logical stream of guarded off-thread requests with its own pool.

    Owns (1) a private ``QThreadPool`` capped at ``max_threads`` (default 1),
    (2) the ``done(token, payload)`` signal, and (3) the token counter — the
    three pieces every guarded call site previously assembled by hand.
    :meth:`submit` supersedes any earlier request on the stream: the pool's
    not-yet-started backlog is dropped (``QThreadPool.clear``), the token is
    bumped so an in-flight worker's result arrives stale, and the new work is
    dispatched.  Consequences:

    * each stream occupies at most ``max_threads`` (=1) OS thread, so heavy
      per-selection work (``find_first_image`` BFS, playlist enumeration,
      curation-wide stat loops) never crowds the global ``QThreadPool``'s
      short-lived probes;
    * under a request burst only the newest not-yet-started work runs — the
      backlog is genuinely discarded, where a plain pool ran every queued
      task to completion and merely dropped the results.

    Construct one per concern and parent it to the owning widget (the pool is
    a child, so it is torn down with the widget).  着地は :meth:`bind` 系で
    受ける — 古い着地の選別はストリーム側で済み、受け側には payload しか
    来ない（着地ガードを手で書かない）。

    **投入したタスクはストリームが強参照で保持する**（:meth:`_start` で
    足し、``_GuardedTask.run`` の ``finally`` で外す）。
    ``QThreadPool.start()`` は所有権を C++ へ移すので、投入側が参照を
    持たないとラッパの寿命は Qt の実装依存になる。**症状**として
    ``AttributeError: ... object has no attribute ...`` が ``run()`` の外へ
    抜けるのを全体 ``-n 4`` で断続的に観測しており（走り出したワーカーが
    属性を失った殻を見ている形）、保持はそこへの保険として置いている。
    **因果は未確定**（PySide6 6.11 の ``start()`` は Python 参照を 1 本
    握るので、GC ストーム下でも単体では再現しない）ため、この保持を
    「不変条件の充足」として他の手組みワーカー対へ横展開しないこと —
    それらの移行はフェーズ3 の機構移行で行う。
    参照は ``run()`` の ``finally`` で手放すので、完了したタスクは滞留しない。

    **窓の teardown では :meth:`request_shutdown` を通ること** — 親付けの
    「ウィジェットと一緒に片付く」は、``QThreadPool`` のデストラクタが
    in-flight タスクを無制限に待つぶんだけ危険でもある（詳細はそちらの
    docstring）。``ViewerWindow._drain_loader_pools`` が窓の下の
    ``GuardedStream`` を漏れなく列挙して呼ぶので、新しいストリームを足す
    ときに呼び出し側へ足す作業は無い。
    """

    done = Signal(int, object)  # (token, payload — meaning is caller-defined)
    #: 進捗の途中報告 ``(token, payload)`` — :attr:`StreamJob.report` が投げる。
    #: 分オーダーで走る仕事（NAS の再帰走査）が「探しています…」のまま固まって
    #: 見えないようにするための 2 本目。使わないストリームは誰も接続しない。
    progress = Signal(int, object)
    #: 降りたセッションの token。:meth:`cancel` と、走行後にキャンセルを見て
    #: 降りたタスクの両方が撃つので、**同じ token が複数回届きうる**（受け側は
    #: 冪等に書くこと）。逆に、降りた世代が必ず 1 回撃つわけでもない —
    #: :meth:`submit` の追い越しでキューごと捨てられた仕事は ``run`` に入らない
    #: ので何も撃たない。**世代ごとの台帳にはならない**（そこは :attr:`done` の
    #: token の仕事）。「進行中の表示を畳む」「一時ファイルを片付ける」といった
    #: ストリーム単位の後始末を :meth:`bind_cancelled` で受けるための 3 本目で、
    #: そちらは追い越しでは呼ばれない（:meth:`_relay_cancelled`）。
    cancelled = Signal(int)

    def __init__(
        self, parent: QObject | None = None, *, max_threads: int = 1,
    ) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_threads)))
        # 世代 + 協調キャンセルは対で動かす（項目#35 の不変条件をそのまま
        # 再利用 — 片方だけ進めた手書きが本レビュー項目#2 の配線漏れを産んだ）。
        self._owner = SessionOwner()
        # 投入済みで未完了のタスク（強参照。理由はクラス docstring）。
        # GUI スレッドが ``add`` / ``clear``、ワーカースレッドが ``discard``
        # するが、set の 3 操作はどれも GIL 下で不可分なのでロックは要らない。
        self._live: set["_GuardedTask"] = set()
        # bind 系が保持する slot（弱参照 — 理由は :meth:`bind` の docstring）。
        self._done_slot: weakref.WeakMethod[Any] | None = None
        self._progress_slot: weakref.WeakMethod[Any] | None = None
        self._failed_slot: weakref.WeakMethod[Any] | None = None
        self._cancelled_slot: weakref.WeakMethod[Any] | None = None

    # ---- 投入（規律ごとに 1 つ） --------------------------------------

    def submit(self, work: Callable[[], object]) -> int:
        """Dispatch *work*, superseding any earlier request on this stream.

        Returns the token the eventual ``done`` emission will carry.  Queued
        (not yet started) predecessors are discarded outright; an in-flight
        predecessor keeps running but its result fails the token guard (and
        its session is cancelled, so a *cooperative* worker stops early).
        """
        return self.submit_job(lambda _job: work())

    def submit_job(self, work: Callable[["StreamJob"], object]) -> int:
        """:meth:`submit` の協調キャンセル / 進捗つき版。

        *work* は :class:`StreamJob` を受け取り、``job.cancel.is_cancelled()``
        を刻みで見て早期に抜け、``job.report(payload)`` で途中経過を投げる。
        先行リクエストは（キュー済みは破棄、走行中はセッションを cancel して）
        追い越される。
        """
        self._drop_queued()
        session = self._owner.start()
        self._start(_GuardedTask(self._job(session), work, self, self._live))
        return session.generation

    def submit_batch(self, work: Callable[["StreamJob"], object]) -> int:
        """**先行を捨てずに積む** 投入（加算的バッチ）。

        現行セッションの token / ``CancelToken`` をそのまま共有するので、
        既に投げたバッチの答えは捨てられない。「要求済み集合に入れた時点で
        二度と再要求しない」規約（:class:`~.keyed_resolver.KeyedResolver`）
        を持つ呼び出し元は**必ずこちら**を使うこと — :meth:`submit` で積むと、
        受付済みのまま答えの返らないパスが残る（A→B→A で二度と解決されない）。
        一括で捨てたいときは :meth:`cancel` が世代ごと切る。
        """
        session = self._owner.current() or self._owner.start()
        self._start(_GuardedTask(self._job(session), work, self, self._live))
        return session.generation

    def submit_detached(
        self, work: Callable[["StreamJob"], object], *, label: str,
    ) -> int:
        """**プールに載せない**投入 — 放棄してよい長大な FS 仕事のための 4 本目。

        世代・協調キャンセル・着地の選別（:meth:`bind` 系）は :meth:`submit_job`
        と同じで、走る場所だけが違う: ``QThreadPool`` ではなく
        :func:`run_detached` の専用デーモンスレッド。``QThreadPool`` は
        （グローバルでも専用でも）デストラクタが in-flight な ``QRunnable`` を
        **無制限に**待つので、「死んだ共有を分オーダーで歩く」仕事を載せると
        窓を閉じてもプロセスが終われない — 予算の外側に待ちが移るだけになる。
        デーモンスレッドならどちらの待ちにも刺さらない。

        したがって :meth:`request_shutdown` / :meth:`wait_for_done` は**この
        投入で走っている仕事を待たない**（プールは空なので即座に返る）。
        止める手段は :meth:`cancel` の協調キャンセルだけなので、*work* は
        ``job.cancel.is_cancelled()`` を刻みで見ること。
        """
        session = self._owner.start()
        task = _GuardedTask(self._job(session), work, self)
        # :attr:`_live` へは入れない（プール外なので :meth:`_drop_queued` の
        # 「実行中スレッド 0」判定の外側）。参照は :func:`run_detached` が
        # 束縛メソッド ``task.run`` をクロージャに閉じ込めて持つので、
        # デーモンスレッドが走っている間ずっと生きている。
        run_detached(session.generation, task.run, label=label)
        return session.generation

    # ---- 投入済みタスクの保持 ------------------------------------------

    def _start(self, task: "_GuardedTask") -> None:
        """タスクを保持してからプールへ渡す（順序が契約）。

        ``pool.start`` へ渡してから参照を取ると、``start`` が返るまでの間に
        ワーカーが走り出して GC が挟まりうる。保持が先。
        """
        self._live.add(task)
        self._pool.start(task)

    def _drop_queued(self) -> None:
        """未着手のキューを捨て、走りようがなくなった参照を回収する。

        ``QThreadPool.clear()`` はキュー済みの ``QRunnable`` を（autoDelete
        なら delete して）捨てるが、捨てられたタスクは ``run()`` に入らない
        ので ``_GuardedTask.run`` の ``finally`` を通らず :attr:`_live` に
        残る。回収してよいのは
        **``clear()`` の直後に実行中スレッドが 0 のとき**だけ: Qt はキューから
        取り出す時点（``run()`` 突入の前）で active を数えるので、0 ならば
        「キューにも居ない ∧ 誰も走っていない」= 残っている参照は全て捨てら
        れた仕事だと確定する。実行中が居る間は、dequeue 済みで ``run()`` に
        入る直前のタスクと区別できないので保持したままにする。掃ける機会は
        **プールがアイドルの状態で次の :meth:`cancel` / :meth:`submit` が
        来たとき**で、アイドルへ落ちただけでは掃けない（このメソッドを呼ぶ
        のはその 2 つだけ）。連投の間は捨てた仕事の殻が溜まるが、C++ 側は
        ``clear()`` が既に delete しているので残るのは Python ラッパだけ、
        しかもどこからも列挙しないので、次の一掃までの過渡的な滞留で済む。
        """
        self._pool.clear()
        if self._pool.activeThreadCount() == 0:
            self._live.clear()

    # ---- 受け取り（着地ガードを受け側から消す） ------------------------

    def bind(self, slot: Callable[[object], None]) -> None:
        """``done`` の着地のうち **:meth:`accepts` を通ったものだけ**を渡す。

        *slot* が受け取るのは payload 1 つで、token も
        ``if not stream.accepts(token): return`` の 2 行も受け側から消える
        （着地ガードの書き忘れ・書き違いが起こせなくなる）。1 ストリームに
        1 つ — 2 度目の ``bind`` は差し替えになる。

        :meth:`bind_failed` を併せて束縛したときに限り、``None`` ペイロード
        はそちらへ回り、ここへは来ない。

        **渡せるのは束縛メソッドだけ**（ラムダ・自由関数は ``TypeError``）。
        保持は :class:`weakref.WeakMethod` で、*slot* の所有者が生きている間
        だけ着地が届く。強参照で持つと「親ウィジェット → 子（ストリーム）→
        親の束縛メソッド」の循環ができ、Qt の親子関係と噛み合って参照カウント
        だけでは解けなくなる（GC を待つ間に C++ 側だけが先に解放され、走行中の
        ワーカーの着地が落ちる）。*slot* の所有者は別に生かしておくこと —
        ストリームを所有ウィジェットへ親付けする通常の使い方では、その
        ウィジェット自身がそれにあたる。
        """
        weak = self._weak_slot(slot)  # 先に検証（TypeError で配線を残さない）
        self._wire_done_relay()
        self._done_slot = weak

    def bind_progress(self, slot: Callable[[object], None]) -> None:
        """``progress`` 版の :meth:`bind`（payload 1 つ・同じ寿命の作法）。"""
        weak = self._weak_slot(slot)  # 先に検証（TypeError で配線を残さない）
        if self._progress_slot is None:
            self.progress.connect(self._relay_progress)
        self._progress_slot = weak

    def bind_failed(self, slot: Callable[[], None]) -> None:
        """``None`` ペイロードの着地だけを渡す。

        ワーカーが例外で落ちた着地はこれだが、**ワーカーが正常に ``None`` を
        返した着地も同じ口へ来る**（:meth:`_relay_done` はペイロードだけを見る）。
        両者を区別したいなら :class:`StreamOutcome` を返して kind で分ける。

        束縛すると ``None`` は :meth:`bind` の slot へは行かない — 「読めた」
        と「読めなかった」を受け側で ``if payload is None:`` に分けて書く形が
        消え、失敗の扱いが 1 箇所に集まる。*slot* は引数を取らない。

        「結果が無い」と「失敗した」を区別したいだけなら、:class:`StreamOutcome`
        を返して kind で分けるほうが強い（失敗も値として型に載る）。
        """
        weak = self._weak_slot(slot)  # 先に検証（TypeError で配線を残さない）
        self._wire_done_relay()
        self._failed_slot = weak

    def bind_cancelled(self, slot: Callable[[], None]) -> None:
        """``cancelled`` を渡す（*slot* は引数なし）。

        呼ばれるのは**ストリームが止まったとき**（:meth:`cancel` /
        :meth:`request_shutdown`）だけ。:meth:`submit` の追い越しでも
        :attr:`cancelled` 自体は撃たれるが、そちらは現行セッションが生きて
        いるので slot へは来ない（:meth:`_relay_cancelled`）— 引数なしの slot
        に「どの世代が降りたか」は表現できないため。

        同じキャンセルで**複数回**呼ばれうる（:meth:`cancel` と、降りた各
        タスクが撃つ）ので冪等に書くこと。:meth:`cancel` からの通知は同期で
        （直接接続）、走行中タスクからの通知はキュー越しに届く — slot の中で
        同じストリームへ投げ直さないこと。
        """
        weak = self._weak_slot(slot)  # 先に検証（TypeError で配線を残さない）
        if self._cancelled_slot is None:
            self.cancelled.connect(self._relay_cancelled)
        self._cancelled_slot = weak

    def _wire_done_relay(self) -> None:
        """``done`` → :meth:`_relay_done` を**一度だけ**繋ぐ。

        ``bind`` と ``bind_failed`` はどちらも ``done`` の着地を必要とする
        ので、どちらが先に呼ばれても二重接続にならないよう 1 箇所に集める。

        **接続より先に :meth:`_weak_slot` を評価すること**（4 本の ``bind``
        系がそうしている）。判定材料が slot の保持だけなので、検証が
        ``TypeError`` で落ちた後に接続だけ残ると、次の正しい ``bind`` で
        条件が再び真になり relay が 2 本張られる = 以後の着地が二重に届く。
        """
        if self._done_slot is None and self._failed_slot is None:
            self.done.connect(self._relay_done)

    @staticmethod
    def _weak_slot(slot: Callable[..., object]) -> "weakref.WeakMethod[Any]":
        if not isinstance(slot, MethodType):
            raise TypeError(
                "bind に渡せるのは束縛メソッドだけです"
                "（ラムダ・自由関数・functools.partial は不可）"
            )
        return weakref.WeakMethod(slot)

    def _relay_done(self, token: int, payload: object) -> None:
        if not self._owner.accepts(token):
            return
        if payload is None and self._failed_slot is not None:
            failed = self._failed_slot()
            if failed is not None:
                failed()
            return
        slot = self._done_slot() if self._done_slot is not None else None
        if slot is not None:
            slot(payload)

    def _relay_progress(self, token: int, payload: object) -> None:
        if not self._owner.accepts(token):
            return
        slot = self._progress_slot() if self._progress_slot is not None else None
        if slot is not None:
            slot(payload)

    def _relay_cancelled(self, _token: int) -> None:
        """``cancelled`` を「ストリームが止まった」ときだけ slot へ渡す。

        :attr:`cancelled` は**降りた世代**を告げるので、:meth:`submit` の
        追い越し（``SessionOwner.start`` が前セッションを畳む）でも撃たれる。
        だが :meth:`bind_cancelled` の slot は引数を取らない = 世代を区別
        できないので、そのまま流すと「新しい仕事が走り出した直後に、前の
        仕事ぶんの後始末（進行中表示を畳む・一時ファイルを消す）が現行世代へ
        効く」。現行セッションが生きているなら降りたのは追い越された世代なの
        で渡さない — :meth:`cancel` / :meth:`request_shutdown` の後だけ通る。
        """
        if self._owner.current() is not None:
            return  # 追い越しで降りただけ — ストリームはまだ走っている
        slot = self._cancelled_slot() if self._cancelled_slot is not None else None
        if slot is not None:
            slot()

    # ---- 破棄 / ガード -------------------------------------------------

    def cancel(self) -> None:
        """Drop queued work, trip the session and invalidate in-flight results.

        Idempotent (``SessionOwner.cancel``): with no live session this is a
        no-op, the generation does **not** advance and no ``cancelled`` is
        emitted — so receive landings through :meth:`bind`, never through a
        hand-written ``token != latest_token()`` comparison.
        """
        session = self._owner.current()
        self._drop_queued()
        self._owner.cancel()
        if session is not None:
            emit_or_drop(self.cancelled, session.generation)

    def latest_token(self) -> int:
        """The most recently issued token (投入側の帳簿・テスト用).

        **着地の選別には使わないこと**: :meth:`cancel` の後これは「どの
        タスクも持っていない世代」になり、idle なストリームへの ``cancel``
        では動きさえしないので、``token != latest_token()`` は見た目より
        弱い。受け取りは :meth:`bind` 系を通す。
        """
        return self._owner.latest_generation()

    def accepts(self, token: int) -> bool:
        """世代一致 ∧ 未キャンセル — 着地の単一述語。

        通常は :meth:`bind` 系が内側で呼ぶので、呼び出し側がこれを書く必要は
        無い。生の ``done.connect`` と組で書いてよい**唯一の例外**は
        :class:`~.keyed_resolver.KeyedResolver` — ペイロードに載る
        ``batch_id`` で帳簿を引くため token を捨てられない。
        """
        return self._owner.accepts(token)

    def _job(self, session: ScanSession) -> "StreamJob":
        token = session.generation

        def report(payload: object) -> None:
            if not session.is_cancelled():
                emit_or_drop(self.progress, token, payload)

        return StreamJob(token, session, report)

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        """Block until the stream's pool drains (tests / teardown only)."""
        return self._pool.waitForDone(timeout_ms)

    def active_count(self) -> int:
        """Workers currently running on this stream's pool (tests only)."""
        return self._pool.activeThreadCount()

    def request_shutdown(self, timeout_ms: int) -> bool:
        """Drop queued work and bounded-wait for the in-flight one (teardown).

        **必ず closeEvent の予算内から呼ぶこと**（レビュー 2026-09-03 項目#54）。
        ``QThreadPool`` のデストラクタは in-flight な ``QRunnable`` を**無制限
        に**待つ — プールは所有ウィジェットの子なので、この待ちが起きるのは
        ``closeEvent`` が終わったあとのウィジェット木の破棄中、つまり
        issue #132 が入れた予算付き teardown の**外側**。死んだ SMB 共有では
        1 回の I/O が 15〜195 秒ブロックする（VM 実測）ので、窓を閉じても
        プロセスがその分だけ終われなかった。

        :meth:`cancel` は「キュー済みを捨ててトークンを進める」だけで走行中
        は止められない（Qt に走行中 ``QRunnable`` のキャンセル API は無い）
        ため、ここでも保証できるのは**待ちの有界化**まで — 予算内に返らな
        かった場合は ``False`` を返し、呼び出し元が記録する。``ThumbnailLoader``
        の ``request_shutdown()`` + ``wait_for_done(timeout)`` と同じ契約を
        1 メソッドにまとめた形（そちらは動画デコードの待ちだけ協調的に解ける）。
        """
        self.cancel()
        return self._pool.waitForDone(timeout_ms)


class _GuardedTask(QRunnable):
    """Run one callable off-thread and emit its result through the bridge.

    ``setAutoDelete(True)`` so the pool reclaims it after ``run``.  Any
    exception from ``work`` is caught and reported as a ``None`` payload so a
    single failing probe never tears down the worker thread.  A task whose
    session was cancelled while it sat in the queue returns before doing any
    work (``SessionRunnable`` と同じ規律 — キュー済みの ``QRunnable`` は
    プールから引き戻せないので、走り出す側で降りるしかない)。

    ``signals`` は着地先の :class:`GuardedStream`（``done(int, object)`` /
    ``cancelled(int)`` を持つ）。``live`` はそのストリームの保持集合
    （:attr:`GuardedStream._live`）**そのもの**を持つ — QObject を経由せずに
    自分を外せるようにするため。窓ごと消えたストリームでは C++ 側の破棄で
    インスタンス属性が読めなくなりうるが、素の ``set`` はそれと無関係に生きる。
    """

    __slots__ = ("_job", "_work", "_signals", "_live")

    def __init__(
        self, job: "StreamJob", work: Callable[["StreamJob"], object],
        signals: "GuardedStream", live: set["_GuardedTask"] | None = None,
    ) -> None:
        super().__init__()
        self._job = job
        self._work = work
        self._signals = signals
        self._live = live
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D401 (Qt API)
        try:
            self._run()
        finally:
            # 投入側の強参照を手放す。``finally`` に置くのは、例外で抜けた
            # 走行が参照を残さないため。
            if self._live is not None:
                self._live.discard(self)

    def _run(self) -> None:
        if self._job.cancel.is_cancelled():
            self._emit_cancelled()
            return
        try:
            payload: object = self._work(self._job)
        except Exception:  # noqa: BLE001 — a probe failure must not kill the pool
            payload = None
        if self._job.cancel.is_cancelled():
            self._emit_cancelled()
            return
        emit_or_drop(self._signals.done, self._job.token, payload)

    def _emit_cancelled(self) -> None:
        """降りたことを ``cancelled(token)`` で知らせる。

        撃つのは ``run`` に入れたタスクだけ — :meth:`GuardedStream.cancel` /
        :meth:`~GuardedStream.submit_job` はどちらもセッションを畳む前に
        ``pool.clear()`` を呼ぶので、キューに積まれたまま捨てられた仕事は
        ここへ来ない（``cancelled`` は世代ごとの台帳ではない）。同じ token が
        :meth:`GuardedStream.cancel` からも届くので、受け側は冪等に書く。
        """
        emit_or_drop(self._signals.cancelled, self._job.token)


def emit_or_drop(signal, *args) -> bool:
    """ワーカースレッドから *signal* を emit する。ブリッジが既に破棄されて
    いれば黙って捨てて ``False`` を返す。

    所有ウィジェットに親付けされたブリッジ QObject（``_XSignals(self)``）は
    ウィジェットの破棄と一緒に C++ 側が消える。その時点でまだ走っている
    ワーカーが emit すると ``RuntimeError: Signal source has been deleted`` が
    ``run()`` の外へ抜け、PySide が "Error calling Python override of
    QRunnable::run()" を stderr に吐く — 結果を受け取る相手はもう居ないので
    **捨てるのが正しい**。閉じた窓を破棄するテストハーネス（2026-09-03）で、
    このガードが無いタスク（detail_window / image_view / content_view /
    health_dialog / markdown_view）が一斉に顕在化した。ワーカー側の emit は
    必ずこれを通すこと（``thumbnail_loader._ThumbnailTask._emit_failed_safe``
    と同じ判断の共通形）。

    ``RuntimeError`` 以外は握らない — それは本物のバグ。
    """
    try:
        signal.emit(*args)
    except RuntimeError as exc:
        if "Signal source has been deleted" not in str(exc):
            raise
        return False
    return True


def run_detached(
    token: int,
    work: Callable[[], object],
    signals: "GuardedSignals | None" = None,
    *,
    label: str = "detached-probe",
) -> None:
    """**プール無し**の投入 — 放棄できる仕事を専用の
    デーモンスレッドで走らせ、待たない。

    ``QThreadPool`` は（グローバルでも専用でも）デストラクタが in-flight な
    ``QRunnable`` を**無制限に**待つ。死んだ SMB 共有では 1 回の I/O が
    15〜195 秒ブロックする（VM 実測）ので、放棄してよい FS プローブを
    プールへ載せると「窓は閉じたのにプロセスが残る」へ症状が移るだけになる
    — 専用プールならダイアログ破棄時に GUI スレッドで、グローバルプールなら
    プロセス終了時に、どちらも予算の外で待つ。窓の有界ドレイン
    （``_drain_loader_pools``）が届かないのはグローバルプールだけだが、
    そこへ載せた時点で待ちの場所が移っただけで消えてはいない。

    デーモンスレッドならどちらの待ちにも刺さらない（``common/teardown`` が
    同じ理由で選んだ形をそのまま使う）。**放棄されても壊れない**仕事にだけ
    使うこと — このモジュールでの想定は「存在を見るだけ」の FS プローブ。

    結果の届け方は ``signals.done(token, payload)``
    で、*work* が送出した例外は ``None`` ペイロードになる。*signals* を省く
    と完了通知は出ない — *work* が**自分で**（``emit_or_drop`` 経由で）進捗と
    結末を送る仕事のための形（``QRunnable.run`` をそのまま渡す口）。
    """
    def _run() -> None:
        try:
            payload: object = work()
        except Exception:  # noqa: BLE001 — プローブの失敗でスレッドを殺さない
            payload = None
        if signals is not None:
            emit_or_drop(signals.done, token, payload)

    # 予算 0 = 起動して待たない（``run_tasks_before_deadline`` は daemon=True
    # のワーカーを立て、``join(0)`` で即座に戻る）。戻り値（未完了ラベル）は
    # 常に 1 件なので読まない。
    run_tasks_before_deadline([(label, _run)], Deadline(0.0), thread_name=label)


def _dir_exists_map(paths: list[str]) -> dict[str, bool]:
    """``{path: is_dir()}`` — ワーカースレッドで走る純関数。"""
    result: dict[str, bool] = {}
    for p in paths:
        try:
            result[p] = Path(p).is_dir()
        except OSError:
            result[p] = False
    return result


def dir_exists_probe(
    paths: Sequence[str],
    signals: GuardedSignals,
    *,
    token: int = 0,
) -> None:
    """フォルダ群の実在を **1 本のワーカー**で判定し ``{path: bool}`` を返す。

    到達不能な UNC / NAS 共有では ``is_dir()`` が 1 件あたり数十秒ブロック
    しうる（「NAS を外した後に古い項目を整理する」= 管理ダイアログが存在する
    理由そのものの局面）ので、GUI スレッドでは絶対に撃たない。着地は
    ``signals.done(token, {path: bool})``。

    *signals* は**呼び出し側が長生きさせる**ブリッジ（``GuardedSignals(self)``）
    を渡すこと。破棄済みのブリッジへの emit は :func:`emit_or_drop` が黙って
    捨てるので、プローブ走行中にダイアログが閉じられても安全。1 回きりの
    プローブなら *token* は既定の 0 でよい（ダイアログを開き直すたびに
    ブリッジごと作り直されるため）。

    走らせるのは :func:`run_detached` = **プールの外のデーモンスレッド**。
    専用プール（:class:`GuardedStream`）はダイアログの子になり破棄時に死んだ
    NAS のプローブを GUI スレッドで待ってしまい、グローバルプールは窓の有界
    ドレインが届かないぶん同じ待ちがプロセス終了時（``~QThreadPool``）へ移る
    だけ — どちらも予算の外で待つ。ここで欲しいのは「窓が終わっても走ってよい
    が、誰も待たない」なので、プールを使わないのが正しい。
    """
    wanted = [str(p) for p in paths]
    run_detached(
        token, lambda: _dir_exists_map(wanted), signals,
        label="dir-exists-probe",
    )


__all__ = [
    "GuardedSignals",
    "GuardedStream",
    "StreamJob",
    "StreamOutcome",
    "dir_exists_probe",
    "emit_or_drop",
    "run_detached",
]
