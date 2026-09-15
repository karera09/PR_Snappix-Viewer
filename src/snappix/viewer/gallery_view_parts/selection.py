"""単一選択モデルを**値**として持つ層。

``GalleryView`` は単一選択なので状態は添字 1 つだが、「変わったか」「どの席を
描き直すか」「範囲外は不発」の 3 つが選択を触る全経路（クリック / キー /
``select_key`` / ``step_selection``）で同じでなければならない。ここに寄せて、
ビュー側は :class:`SelectionMove` を見て再描画とシグナルを出すだけにする。

Qt 非依存。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 選択なしを表す添字。
NO_SELECTION = -1


@dataclass(frozen=True)
class SelectionMove:
    """選択の遷移（:meth:`SelectionState.select` の戻り）。"""

    #: 直前の添字（``-1`` = 選択なし）。
    previous: int
    #: 適用後の添字。
    current: int

    @property
    def changed(self) -> bool:
        """実際に席が移ったか（同じ添字への再選択は ``False``）。"""
        return self.previous != self.current


@dataclass
class SelectionState:
    """いま選ばれている添字と、その純粋な遷移規則。"""

    index: int = NO_SELECTION

    @property
    def is_set(self) -> bool:
        return self.index >= 0

    def reset(self) -> int:
        """選択を落として直前の添字を返す（タイル入れ替え / ``clear``）。"""
        previous, self.index = self.index, NO_SELECTION
        return previous

    def select(self, index: int, count: int) -> SelectionMove | None:
        """*index* を選ぶ。範囲外なら ``None``（＝不発）。

        同じ添字への再選択も ``SelectionMove``（``changed`` が偽）を返す —
        呼び出し側は「見える位置へ送る」だけを行い、シグナルは出さない。
        """
        if not (0 <= index < count):
            return None
        previous = self.index
        self.index = index
        return SelectionMove(previous=previous, current=index)

    def step_target(self, delta: int, count: int) -> int | None:
        """線形に *delta* ずらした先の添字。動けないなら ``None``。

        選択が無いときは進行方向の端（前進なら先頭・後退なら末尾）から始める。
        """
        if count == 0:
            return None
        current = self.index
        if current < 0:
            target = 0 if delta > 0 else count - 1
        else:
            target = current + delta
        if target < 0 or target >= count or target == current:
            return None
        return target


__all__ = ["NO_SELECTION", "SelectionMove", "SelectionState"]
