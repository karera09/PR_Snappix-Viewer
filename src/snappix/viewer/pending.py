"""保留して「着地できる時点でだけ」消費する 1 値の共通型.

ビューアには「今は当てられないが、当てられる時点が来たら 1 回だけ当てたい
値」が繰り返し現れる: ナビ履歴のスクロール位置、再構築を跨いで預ける選択、
遅延生成ウィジェットへ渡す設定、起動復元のプレビュー対象、``LoadedMedia`` を
待つ再生位置。どれも同じ形で、同じ 1 つの契約を守る必要がある——

    **暫定の再構築で保留値を食い潰さない。**

保留値を消費してよいのは「本当に当たる」時点だけで、途中に挟まる暫定の状態
（タイル 0 件の再構築・幅 0 の縮退レイアウト・未着地のスキャン・まだ生成して
いないウィジェット）では値を**残したまま**降りる。手書きの ``x | None`` と
``if x is None: return`` でこれを 10 箇所書くと、必ずどこかが「とりあえず
None にしておく」形で片側だけ壊れる（値は消えたが当たっていない）。

この型は保留を 1 つの語彙に畳む:

* :meth:`Pending.set` / :meth:`Pending.clear` — 積む / 捨てる
* :attr:`Pending.armed` / :meth:`Pending.peek` — 消費せずに見る
* :meth:`Pending.take_if` — *ready* が真のときだけ取り出す（偽なら残す）
* :meth:`Pending.consume` — *apply* が「当たった」と言ったときだけ降ろす

**``take()``（無条件消費）は用意しない。** 無条件で降ろしたい箇所は
``take_if(always)`` / ``consume(always)`` と綴る——「述語を書かなかった」と
「いつでも当たると判断した」が同じ字面になるのを避けるため。``always`` で
grep すれば無条件消費の全件が出る。

**載せないもの（保留ではない 1 値）**: AI 検索の投入記録
（``AdvancedSearchController._pending`` = ``ScanRequest`` 1 スロット）は
「飛行中の要求を控えるラベル」で、着地 4 スロットは署名の一致を**読む**だけ
で降ろさず（次の投入が上書きするのが唯一の更新）、消費の時点という概念その
ものが無いのでこの型には載せない。

**GUI スレッド専用**（ロックを持たない）。ワーカースレッドから積む / 降ろす
用途には使わないこと——off-thread の結果の受け渡しは ``_runnable`` の
``GuardedStream`` が担う（世代ガードと協調キャンセルが要る別の問題）。
"""

from __future__ import annotations

from typing import Callable, Final, Generic, TypeAlias, TypeVar

T = TypeVar("T")

__all__ = ["MARK", "Mark", "OneShot", "Pending", "always"]


def always(_value: object) -> bool:
    """「いつでも当たる」述語 — 無条件消費を明示で綴るための関数。

    ``take_if(always)`` / ``consume(always)`` は「この保留に ready 条件は
    無い」という宣言で、``take()`` という名前の穴を開けずに同じことをする。
    """
    return True


class Mark:
    """値を持たない one-shot の中身（「立っている」ことだけが情報）。

    ``Pending[bool]`` に ``True`` を積む形は ``set(False)`` が「降ろす」に
    化けるので採らない（``None`` = clear と同じ穴）。:data:`MARK` を積む。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - デバッグ表示のみ
        return "MARK"


#: 値を持たない one-shot に積む唯一の値。
MARK: Final[Mark] = Mark()

#: 値を持たない one-shot の型別名（``OneShot`` = ``Pending[Mark]``）。
OneShot: TypeAlias = "Pending[Mark]"


class Pending(Generic[T]):
    """保留中の 1 値。``None`` は「保留なし」を表すので値には使えない。

    生成直後は空（``armed`` は ``False``）。積み直しは上書きで、保留は常に
    高々 1 つ——「最後に積んだものが当たる」が全スロット共通の意味論。
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: T | None = None

    def set(self, value: T | None) -> None:
        """*value* を積む。``None`` は :meth:`clear` と同義（保留なし）。

        ``x if cond else None`` をそのまま渡せる形にしてあるのは、呼び出し元で
        「積む枝」と「捨てる枝」が分かれると片方だけ育つため。
        """
        self._value = value

    def clear(self) -> None:
        """保留を捨てる（当てずに降ろす）。"""
        self._value = None

    @property
    def armed(self) -> bool:
        """保留があるか（消費しない）。"""
        return self._value is not None

    def peek(self) -> T | None:
        """保留値を消費せずに読む（無ければ ``None``）。"""
        return self._value

    def take_if(self, ready: Callable[[T], bool]) -> T | None:
        """*ready* が真のときだけ保留値を取り出して降ろす。

        偽なら値は**残る**（次の機会に当たる）。取り出せなかったことと
        「保留が無かった」ことはどちらも ``None`` で返る——値に ``None`` を
        積めないので区別は要らない。
        """
        value = self._value
        if value is None or not ready(value):
            return None
        self._value = None
        return value

    def consume(self, apply: Callable[[T], bool]) -> bool:
        """*apply* が「当たった」（真）と言ったときだけ降ろす。

        戻り値は「当てて降ろしたか」。*apply* が偽を返したときは値が残るので、
        再構築・再レイアウトの次の機会にもう一度呼べばよい。

        **降ろすのは *apply* が真を返した後**なので、*apply* の中で同じスロット
        へ積み直した値は道連れに消える（当てた直後に次の保留を積む経路がある
        なら、``consume`` から戻った後に積むこと）。
        """
        value = self._value
        if value is None or not apply(value):
            return False
        self._value = None
        return True

    def __repr__(self) -> str:  # pragma: no cover - デバッグ表示のみ
        return f"Pending({self._value!r})"
