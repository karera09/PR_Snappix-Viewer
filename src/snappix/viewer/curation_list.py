"""横断キュレーション一覧の *種別* を表す値オブジェクト（Qt 非依存）。

「スター付き」「あとで見る」「ユーザータグ: <名前>」の 3 軸は、``PostGrid``
から見ると **同じ 1 つの状態**（``_curation_view``）に載る同型の一覧である。
種別が裸の ``str`` だった頃は、そこから

* 表示名（パンくず / 条件チップ / レール）
* 空状態の文言
* 入場時に奪うソート軸

を **使う場所ごとに導出**していたため、軸を 1 つ足すたびに導出点の取りこぼしが
起きた（N-71 でユーザータグ軸が入ったとき、名前とソートだけが追随し、空状態と
条件チップは ``== "later"`` の二値分岐のまま取り残された = レビュー 2026-09-03
項目 #1 / #5 / #53）。本モジュールはその 3 つの導出を **1 つの値オブジェクト**
に閉じ込め、軸の追加が「ここに 1 行足す」だけで全導出点へ届くようにする。

永続化・履歴（``NavEntry.curation``）・条件チップの識別子は今までどおり
:attr:`CurationList.key` の **文字列** のままなので、``ViewerState`` や
``PostGrid.enter_curation_view("tag:X")`` の公開 API は変わらない。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..common.i18n import t

#: ユーザータグ横断一覧の *kind* 接頭辞 (N-71)。``"tag:<タグ名>"`` の形で
#: 「スター付き」「あとで見る」と同じ 1 つの状態変数に載るので、履歴
#: (``NavEntry.curation``) / 条件チップ / レールの現在地表示は無変更で効く。
TAG_PREFIX = "tag:"

#: 軸の台帳。``axis`` → (表示名キー, 空状態キー, 入場時のソート軸)。
#: **新しいキュレーション軸を足すときはここに 1 行足す** — 表示名・空状態・
#: ソートの 3 導出点はすべてこの表を引くので、片側だけ追随する事故が起きない。
_AXES: dict[str, tuple[str, str, str]] = {
    # スター付き一覧は★が主題なので、入場時に★の高い順を奪う (07-25 #59)。
    "starred": (
        "viewer.main_window.curation_starred_list",
        "viewer.post_grid.curation_empty_starred",
        "star_desc",
    ),
    # あとで見る / ユーザータグには評価軸が無いので「最近印を付けた順」を
    # 近似する更新日時の新しい順（off-thread の stat が出せる唯一の日付）。
    "later": (
        "viewer.main_window.curation_later_list",
        "viewer.post_grid.curation_empty_later",
        "mtime_desc",
    ),
    "tag": (
        "viewer.main_window.curation_tag_list",
        "viewer.post_grid.curation_empty_tag",
        "mtime_desc",
    ),
}

#: そのまま種別として成立する軸（``tag`` は ``tag:<名前>`` の形でしか成立
#: しないので入らない）。:meth:`CurationList.from_kind` の受理集合。
_NON_TAG_KINDS: frozenset[str] = frozenset(
    axis for axis in _AXES if axis != "tag"
)


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
        if kind.startswith(TAG_PREFIX) or kind in _NON_TAG_KINDS:
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

    # ---- 導出（この 3 つが全導出点） ----------------------------------

    def label(self) -> str:
        """一覧の表示名 — パンくず / 条件チップ / レールの単一情報源。"""
        key = _AXES[self.axis][0]
        tag = self.tag
        return t(key, tag=tag) if tag is not None else t(key)

    def empty_message_key(self) -> str:
        """母集合が空のときの案内文言キー（軸ごとに別の「付け方」を案内する）。"""
        return _AXES[self.axis][1]

    def empty_message(self) -> str:
        """母集合が空のときの案内文言（:meth:`empty_message_key` の適用形）。"""
        key = self.empty_message_key()
        tag = self.tag
        return t(key, tag=tag) if tag is not None else t(key)

    def sort_mode(self) -> str:
        """入場時に奪う並び順 — 一覧が *about* にしている軸 (07-25 #59)。"""
        return _AXES[self.axis][2]


__all__ = ["TAG_PREFIX", "CurationList"]
