"""単発デバウンスタイマーの共通型（``QTimer`` の手組みを 1 本に畳む）.

GUI スレッドの「連続して届く出来事を 1 回の処理へまとめる」タイマーは、
``QTimer(owner)`` + ``setSingleShot(True)`` + ``setInterval`` +
``timeout.connect`` の 4 行と、武装の作法（無条件に張り直すのか、既に走って
いれば触らないのか）を**呼び出し側が覚えている**形で各所に手組みされていた。
4 行は複製できるが作法は複製できない — 同じ 4 行に見えて意味が逆の 2 種類が
混ざり、どちらのつもりで書かれたかはコメントを読むまで分からない。
:class:`Debouncer` はその作法を :class:`DebounceMode` として**構築時に宣言**
させ、以後の武装を :meth:`Debouncer.trigger` 1 本に寄せる。

**なぜ ``QTimer`` を継承するのか**

窓じまいの不変条件「自走タイマーを 1 つ残らず止める」は、種類の列挙ではなく
``findChildren(QTimer)`` で **QObject 木そのものから導出**して守られている。
包含（``QTimer`` を属性に持つただの ``QObject``）にすると、この掃引から
デバウンサだけが外れて片側欠落が生まれる。継承なら掃引にそのまま載り、
既存の ``isActive()`` / ``stop()`` / ``interval()`` の読み書きも
（テストのものを含めて）1 行も変えずに動く。

**対象外**

* 反復タイマー（``setSingleShot`` しない周期処理 — オートセーブの心拍・
  スピナー・スライドショー・統計の再描画など）。デバウンスではないので
  この型には載せない。
* ワーカースレッドのイベントループ上に張るタイマー。所有スレッドが GUI
  スレッドではなく、窓の QObject 木にも載らないため、上の掃引の前提が
  成り立たない。
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from PySide6.QtCore import QObject, QTimer


class DebounceMode(Enum):
    """武装の作法。取り違えると症状が「遅い」ではなく「出ない」になる。"""

    #: 末尾側に寄せる: :meth:`Debouncer.trigger` のたびに締切を張り直す。
    #: 「入力が止まってから走らせたい」もの（検索の再走査・状態の保存・
    #: オートハイド・リサイズの追従）に使う。連続して trigger され続ける
    #: 限り発火しないので、**着地の取りこぼしが許されない用途では使わない**。
    TRAILING = "trailing"

    #: 先頭側の窓: 予約が無いときだけ張り、走っている間は延長しない。
    #: 「バーストの間も一定間隔で必ず流したい」もの（サムネ着地のフラッシュ・
    #: 可視範囲の要求・遅延レイアウト）に使う。TRAILING にすると、着地が
    #: 間隔より速く続く間ずっと発火せず、面が更新されないまま固まる。
    LEADING_WINDOW = "leading_window"

    #: 1 回きり: 一度武装したら :meth:`Debouncer.rearm` まで二度と武装しない。
    #: 「1 エピソードにつき 1 回」が契約のもの（走査開始から一定時間で出す
    #: 読み込みヒント・失敗着地からの 1 回限りの再読込）に使う。
    ONE_SHOT = "one_shot"


class Debouncer(QTimer):
    """単発デバウンスタイマー（``owner`` に親付けされ、一緒に死ぬ）。

    :param owner: 親 ``QObject``（ウィジェットならその破棄で一緒に消える）。
    :param interval_ms: 既定の待ち時間。
    :param slot: 発火時に呼ぶもの。``timeout`` へ繋ぐだけで**保持はしない**
        （下記の寿命の注記）。
    :param mode: :class:`DebounceMode`。**既定値は無い** — 呼び出し側に
        毎回どちらの作法かを書かせるための必須キーワード。

    ``QTimer`` そのものなので ``isActive()`` / ``stop()`` / ``interval()`` /
    ``start()`` はこれまでどおり使える。``start()`` は「作法を無視して今すぐ
    張り直す」意味で残してある（LEADING_WINDOW の面でも、意図して締切を
    延ばしたい 1 箇所だけがそれを使う）。

    **``slot`` を属性に持たないこと**（:meth:`flush_now` も ``timeout`` を
    自分で発火させて済ませる）。束縛メソッドを持つと子（このタイマー）から
    親への強参照ができ、親は Qt の親子関係でこの子を持っているので
    **Python の参照カウントでは決して 0 にならない循環**になる。所有者が
    ワーカープールを持つ型（サムネローダー等）だと、破棄がスコープ末尾の
    決定的なタイミングから任意の GC 実行時点へずれ、走行中のワーカーの足元
    で C++ オブジェクトが解放されてプロセスごと落ちる。
    """

    def __init__(
        self,
        owner: QObject,
        interval_ms: int,
        slot: Callable[[], None],
        *,
        mode: DebounceMode,
    ) -> None:
        super().__init__(owner)
        self._mode = mode
        #: ONE_SHOT で「もう武装した」ことを覚える札（:meth:`rearm` で降ろす）。
        self._spent = False
        # 共通型そのものなので素書きする（規約ガードの allowlist）。
        self.setSingleShot(True)
        self.setInterval(int(interval_ms))
        # スロットは**そのまま**繋ぐ（自分のメソッドを挟まない）: 受け手が
        # ``owner`` とは別の QObject のことがあり、Qt の「受け手が死んだら
        # 接続も消える」保護をこちらの都合で外さないため。
        self.timeout.connect(slot)

    # ------------------------------------------------------------- 操作

    def trigger(self, interval_ms: int | None = None) -> None:
        """作法に従って武装する。

        ``interval_ms`` を渡すと、その回の待ち時間を明示する（``QTimer.start``
        と同じく間隔そのものも更新される）。渡さなければ構築時の間隔。

        **武装しなかった回は間隔も更新しない**: LEADING_WINDOW が予約中のとき
        と ONE_SHOT が消費済みのときは、``interval_ms`` を渡しても走っている
        予約には触れず何もしない（``setInterval`` は走行中のタイマーを新しい
        間隔で**再始動**するので、ここで先に書き換えると「延長しない」という
        作法そのものが崩れる）。
        """
        if self._mode is DebounceMode.LEADING_WINDOW and self.isActive():
            return
        if self._mode is DebounceMode.ONE_SHOT:
            if self._spent:
                return
            self._spent = True
        if interval_ms is None:
            self.start()
        else:
            self.start(int(interval_ms))

    def flush_now(self, force: bool = False) -> bool:
        """予約を今すぐ消化する。消化したなら ``True``。

        既定では**予約があるときだけ**動く（無ければ何もしない）ので、
        「これから読む値が保留のレイアウトに依存する」直前に 1 回呼ぶ、という
        使い方ができる。``force=True`` は予約の有無に関わらず発火させる。

        呼ぶのは ``timeout`` の発火であってスロットの直接呼び出しではない
        （クラス docstring の寿命の注記 — スロットは保持しない）。
        """
        armed = self.isActive()
        if not armed and not force:
            return False
        self.stop()
        if self._mode is DebounceMode.ONE_SHOT:
            self._spent = True
        self.timeout.emit()
        return True

    def rearm(self) -> None:
        """予約を捨て、ONE_SHOT の札も降ろす（次のエピソードの始まり）。"""
        self.stop()
        self._spent = False


__all__ = ["DebounceMode", "Debouncer"]
