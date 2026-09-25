"""横断キュレーション一覧の *種別* を表す値オブジェクト（Qt 非依存）。

「スター付き」「あとで見る」「ユーザータグ: <名前>」の 3 軸は、``PostGrid``
から見ると **同じ 1 つの状態**（``_curation_view``）に載る同型の一覧である。
種別を裸の ``str`` で持つと、そこから

* 表示名（パンくず / 条件チップ / レール）
* 空状態の文言
* 入場時に奪うソート軸

を **使う場所ごとに導出**することになり、軸を 1 つ足すたびに導出点の取りこぼしが
起きる（名前とソートだけが追随し、空状態と条件チップが ``== "later"`` の
二値分岐のまま取り残される）。本モジュールはその 3 つの導出を **1 つの値オブジェクト**
に閉じ込め、軸の追加が「ここに 1 行足す」だけで全導出点へ届くようにする。
母集合述語（入場ガード・件数）・「この一覧から外す」の印・
レール / 編集メニューの常設行の列挙も同じ台帳の行が持つ（台帳の外に手書きの
``"starred"`` / ``"later"`` 分岐が残ると、軸を足しても
入場が拒否されメニューにも行が出ない）。

永続化・履歴（``NavEntry.curation``）・条件チップの識別子は今までどおり
:attr:`CurationList.key` の **文字列** のままなので、``ViewerState`` や
``PostGrid.enter_curation_view("tag:X")`` の公開 API は変わらない。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..common.i18n import t

#: ユーザータグ横断一覧の *kind* 接頭辞。``"tag:<タグ名>"`` の形で
#: 「スター付き」「あとで見る」と同じ 1 つの状態変数に載るので、履歴
#: (``NavEntry.curation``) / 条件チップ / レールの現在地表示は無変更で効く。
TAG_PREFIX = "tag:"


def _starred(meta: Any, tag: str | None) -> bool:
    return meta.star >= 1


def _later(meta: Any, tag: str | None) -> bool:
    return bool(meta.later)


def _has_tag(meta: Any, tag: str | None) -> bool:
    if tag is None:
        return False
    needle = tag.casefold()
    return any(x.casefold() == needle for x in meta.tags)


def _unmark_star(tag: str | None, tags: Sequence[str]) -> tuple[str, object]:
    return ("star", 0)


def _unmark_later(tag: str | None, tags: Sequence[str]) -> tuple[str, object]:
    return ("later", False)


def _unmark_tag(tag: str | None, tags: Sequence[str]) -> tuple[str, object]:
    needle = (tag or "").casefold()
    return ("tags", [x for x in tags if x.casefold() != needle])


@dataclass(frozen=True, slots=True)
class _Axis:
    """台帳 1 行 — 軸に関する知識の全部。

    * ``label_key`` / ``hint_key`` / ``empty_key`` — 表示名・レール行の補足・
      空状態の文言キー（タグ軸は ``{tag}`` を埋める）
    * ``sort`` — 入場時に奪う並び順
    * ``pool`` — 母集合述語 ``(meta, tag) -> bool``（``meta`` は ``star`` /
      ``later`` / ``tags`` を持つ ``user_meta`` の行）
    * ``unmark`` — 「この一覧から外す」で書く印 ``(tag, 現在のタグ列) ->
      (field, value)``（その軸だけを外し、他の軸の表明は巻き添えにしない）
    * ``fixed`` — パラメータを持たず常設の行になる軸（レール / 編集メニューが
      列挙する）。``False`` の軸は ``<axis>:<値>`` の形でだけ種別になる
    """

    label_key: str
    hint_key: str
    empty_key: str
    sort: str
    pool: Callable[[Any, "str | None"], bool]
    unmark: Callable[["str | None", Sequence[str]], tuple[str, object]]
    fixed: bool = True


#: 軸の台帳。**新しいキュレーション軸を足すときはここに 1 行足す** — 表示名・
#: 補足・空状態・ソート・母集合述語・外す印・常設行の列挙はすべてこの表を
#: 引くので、片側だけ追随する事故が起きない。
_AXES: dict[str, _Axis] = {
    # スター付き一覧は★が主題なので、入場時に★の高い順を奪う。
    "starred": _Axis(
        label_key="viewer.main_window.curation_starred_list",
        hint_key="viewer.main_window.curation_starred_list_hint",
        empty_key="viewer.post_grid.curation_empty_starred",
        sort="star_desc",
        pool=_starred,
        unmark=_unmark_star,
    ),
    # あとで見る / ユーザータグには評価軸が無いので「最近印を付けた順」を
    # 近似する更新日時の新しい順（off-thread の stat が出せる唯一の日付）。
    "later": _Axis(
        label_key="viewer.main_window.curation_later_list",
        hint_key="viewer.main_window.curation_later_list_hint",
        empty_key="viewer.post_grid.curation_empty_later",
        sort="mtime_desc",
        pool=_later,
        unmark=_unmark_later,
    ),
    "tag": _Axis(
        label_key="viewer.main_window.curation_tag_list",
        hint_key="viewer.main_window.curation_tag_list_hint",
        empty_key="viewer.post_grid.curation_empty_tag",
        sort="mtime_desc",
        pool=_has_tag,
        unmark=_unmark_tag,
        fixed=False,
    ),
}

def _fixed_kinds() -> tuple[str, ...]:
    """そのまま種別として成立する軸（台帳の順 = レール / メニューの行順）。

    ``fixed=False`` の ``tag`` は ``tag:<名前>`` の形でしか成立しないので入ら
    ない。:meth:`CurationList.from_kind` の受理集合でもある。毎回台帳から導く
    （別の定数へ写すと、台帳だけ差し替えた検査が素通りする）。
    """
    return tuple(axis for axis, row in _AXES.items() if row.fixed)


@dataclass(frozen=True, slots=True)
class CurationList:
    """1 つの横断キュレーション一覧（``"starred"`` / ``"later"`` / ``"tag:X"``）。

    ``kind`` は永続化・履歴・チップ識別子がそのまま使う文字列で、
    :meth:`from_kind` が唯一の入口（未知の種別は ``None`` を返すので、呼び出し
    側の入場ガードは今までどおり「作れなければ入場しない」で書ける）。
    """

    kind: str

    # ---- 生成 ---------------------------------------------------------

    @classmethod
    def from_kind(cls, kind: str | None) -> "CurationList | None":
        """*kind* 文字列 → 値オブジェクト。未知の種別は ``None``。

        ``_AXES`` は**軸**の台帳（キーは ``starred`` / ``later`` / ``tag``）で、
        受理する**種別**の集合とは違う: 軸名 ``"tag"`` そのものは種別ではなく、
        通すと表示名が ``tag`` を埋めない未展開テンプレートのまま出る。
        """
        if not isinstance(kind, str):
            return None
        if kind.startswith(TAG_PREFIX) or kind in _fixed_kinds():
            return cls(kind)
        return None

    @staticmethod
    def tag_of(kind: str | None) -> str | None:
        """``"tag:お気に入り"`` → ``"お気に入り"``（それ以外は ``None``）。"""
        if isinstance(kind, str) and kind.startswith(TAG_PREFIX):
            return kind[len(TAG_PREFIX):]
        return None

    @staticmethod
    def axes() -> tuple[str, ...]:
        """台帳が知っている軸名（テストが全導出点を横断検査するのに使う）。"""
        return tuple(_AXES)

    @staticmethod
    def fixed_kinds() -> tuple[str, ...]:
        """常設行になる種別（``"starred"`` / ``"later"`` …・台帳の順）。"""
        return _fixed_kinds()

    @staticmethod
    def kinds(user_tags: Iterable[str] = ()) -> list[str]:
        """レール / 編集メニューが並べる種別の全列 — 常設行 + タグごとの行。"""
        return [*_fixed_kinds(), *(TAG_PREFIX + tag for tag in user_tags)]

    # ---- 同一性 -------------------------------------------------------

    @property
    def key(self) -> str:
        """永続化 / 履歴 / チップが使う文字列識別子（= ``kind``）。"""
        return self.kind

    @property
    def tag(self) -> str | None:
        """ユーザータグ一覧ならタグ名、そうでなければ ``None``。"""
        return self.tag_of(self.kind)

    @property
    def is_tag(self) -> bool:
        return self.tag is not None

    @property
    def axis(self) -> str:
        """台帳の行キー（``"starred"`` / ``"later"`` / ``"tag"``）。"""
        return "tag" if self.is_tag else self.kind

    # ---- 導出（台帳を引くのはここだけ） ------------------------------

    @property
    def _row(self) -> _Axis:
        return _AXES[self.axis]

    def _format(self, key: str) -> str:
        tag = self.tag
        return t(key, tag=tag) if tag is not None else t(key)

    def label(self) -> str:
        """一覧の表示名 — パンくず / 条件チップ / レールの単一情報源。"""
        return self._format(self._row.label_key)

    def hint(self) -> str:
        """レール行の補足（ツールチップ）— 何を集めた一覧か。"""
        return self._format(self._row.hint_key)

    def empty_message_key(self) -> str:
        """母集合が空のときの案内文言キー（軸ごとに別の「付け方」を案内する）。"""
        return self._row.empty_key

    def empty_message(self) -> str:
        """母集合が空のときの案内文言（:meth:`empty_message_key` の適用形）。"""
        return self._format(self.empty_message_key())

    def sort_mode(self) -> str:
        """入場時に奪う並び順 — 一覧が *about* にしている軸。"""
        return self._row.sort

    def contains(self, meta: Any) -> bool:
        """*meta*（``star`` / ``later`` / ``tags`` を持つ行）がこの一覧の母集合か。"""
        return self._row.pool(meta, self.tag)

    def unmark(self, tags: "Sequence[str] | None" = None) -> tuple[str, object]:
        """「この一覧から外す」で書く印 ``(field, value)``（この軸だけ）。

        タグ一覧では *tags*（その行の現在のタグ列）からそのタグだけを大文字
        小文字無視で抜いた残りを返す。
        """
        return self._row.unmark(self.tag, tuple(tags or ()))


__all__ = ["TAG_PREFIX", "CurationList"]
