"""グリッド全面を占有する「一覧」の状態（Qt 非依存の 1 つの値）。

左ペインには **グリッド全体を奪う一覧**が 2 種類ある — 横断キュレーション一覧
（★ / あとで見る / ユーザータグ）と「最近追加されたファイル」一覧。
どちらも同じ形をしている:

* グリッド全体を所有する（互いに**相互排他**）
* 母集合をメモリに持つ（``entries`` + タイル説明の ``rel_paths``）
* 入場前の並び順を退避し、退場で返す（``saved_sort``）
* パンくずを単一クラムへ差し替える（``crumb``）
* 解決 / 走査が in-flight のあいだ「読み込み中」を名乗る（``pending``）
* 読み取り失敗を「まだありません」と言わない（``failed``）
* 入場・退場・再ルート・窓じまいの 4 経路で同じ片付けをする

2 種類の一覧のフィールドを ``PostGrid`` に平置きし、ライフサイクルを 4 経路
× 2 の 8 か所へ手書きで複製すると、チップ名の片側欠落や片方だけキャンセル
トークンを持たない、といった「二重実装のどちらか一方にしか手が入らない」
不具合が必ず出る。

このモジュールはその形を **1 つの値オブジェクト** に閉じ込める。
``PostGrid`` が持つのは ``self._overlay: OverlayList | None`` の 1 本だけで、
入場の入口は「解決の投げ方（キュレーションの stat 解決か、再帰走査か）」だけが
違う薄い呼び出しになり、片付けは :meth:`OverlayList.teardown` の 1 行になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..common.i18n import t
from .curation_list import CurationList

if TYPE_CHECKING:  # pragma: no cover — 型注釈だけ（Qt も folder_scan も引かない）
    from .folder_scan import FolderEntry

#: 0 タイルの分類（``grid_empty_state.overlay_zero_kind`` の後半）→ 文言キーの台帳。
#: 一覧の種類 × 状態の 2 軸をここ 1 か所で解決するので、**新しい一覧を足す
#: ときはこの表に 1 列足すだけ**で 4 面（空状態 / 状態行 / チップ / クラム）へ
#: 届く。キーは文字列リテラルのまま置く（``tests/test_i18n.py`` の「参照され
#: ていないカタログキー」検査が動的組み立てを見通せないため）。
_EMPTY_KEYS: dict[tuple[str, str], str] = {
    ("curation", "loading"): "viewer.post_grid.curation_loading",
    ("curation", "error"): "viewer.post_grid.curation_error",
    ("recent", "loading"): "viewer.post_grid.recent_loading",
    ("recent", "error"): "viewer.post_grid.recent_error",
    ("recent", "empty"): "viewer.post_grid.recent_empty",
}

#: 条件チップの見出し（``× で一覧を退場``）。
_CHIP_KEYS = {
    "curation": "viewer.post_grid.dim_curation_view",
    "recent": "viewer.post_grid.dim_recent_view",
}


@dataclass
class OverlayList:
    """グリッド全面を占有している一覧 1 つ分の状態。

    *kind* が一覧の同一性 — :class:`~.curation_list.CurationList`（横断
    キュレーション）か ``Path``（そのフォルダの最近追加一覧）。名前 / 空状態
    文言 / 入場時のソート軸はここから導き、``PostGrid`` 側に二値分岐を作らない。

    *cancel* は解決 / 走査を止める呼び出し（実体は所有ストリームの
    ``GuardedStream.cancel``）。:meth:`teardown` が必ず通るので、「片方の一覧
    だけキャンセルを配線し忘れる」が構造的に起こせない。
    """

    kind: "CurationList | Path"
    #: 入場時に奪う前の並び順（退場で返す）。
    saved_sort: str | None = None
    #: 解決 / 走査の中止（所有ストリームの ``cancel``）。
    cancel: Callable[[], None] | None = None

    #: 解決 / 走査が返した母集合と、そのタイル説明（ライブラリ基準の相対パス）。
    entries: "list[FolderEntry]" = field(default_factory=list)
    rel_paths: dict[str, str] = field(default_factory=dict)
    #: 読めずに落ちた行のプレースホルダ (横断一覧のみ)。
    ghosts: "list[FolderEntry]" = field(default_factory=list)
    ghost_keys: set[str] = field(default_factory=set)
    #: そのうち **確実に消えた**（missing）行の鍵。「この一覧から外す」は
    #: 無確認で印を恒久削除するので、読めなかっただけの行（共有が落ちて
    #: いる / 権限が外れている）には出さない。
    ghost_missing_keys: set[str] = field(default_factory=set)
    #: 確実に消えた行 / 読めなかった行の数（分けて数える）。
    missing: int = 0
    unreadable: int = 0
    #: 解決 / 走査が**全滅**した（「まだありません」と言ってはいけない）。
    failed: bool = False
    #: 解決 / 走査が in-flight（空状態は「読み込み中」を名乗る）。
    pending: bool = False
    #: 走査中の 「N 件走査」 表示用（最近追加一覧のみ）。
    scanned: int = 0
    #: 一覧が **自分について** 語っている状態行（「最近追加されたファイル —
    #: N 件」/「N 件は見つかりませんでした（移動または削除済み）」など）。
    #: 出したときの文言をそのまま覚えておく **復元の単一情報源** — 一覧の上に
    #: 載せた条件の解除 (``clear_search_state``) は状態行を無条件に空へ倒すので、
    #: これが無いと「一覧に留まったまま件数行だけ消えて二度と戻らない」。
    #: 母集合の再解決で
    #: 作り直されるので :meth:`reset_population` は触らない。
    status: str = ""

    # ---- 同一性 -------------------------------------------------------

    @property
    def curation(self) -> CurationList | None:
        """横断キュレーション一覧なら種別、そうでなければ ``None``。"""
        return self.kind if isinstance(self.kind, CurationList) else None

    @property
    def recent(self) -> Path | None:
        """最近追加一覧なら対象フォルダ、そうでなければ ``None``。"""
        return self.kind if isinstance(self.kind, Path) else None

    @property
    def prefix(self) -> str:
        """空状態分類 / 文言キーの接頭辞（``"curation"`` / ``"recent"``）。"""
        return "recent" if self.recent is not None else "curation"

    # ---- 表示（4 面の単一情報源） --------------------------------------

    def name(self) -> str:
        """一覧そのものの名前（チップ / クラムが共通で使う中身）。"""
        cur = self.curation
        if cur is not None:
            return cur.label()
        folder = self.recent
        assert folder is not None
        return folder.name or str(folder)

    def crumb(self) -> str:
        """入場中のパンくずに差し替える単一クラム。"""
        cur = self.curation
        if cur is not None:
            return cur.label()
        return t("viewer.post_grid.recent_crumb", name=self.name())

    def chip_label(self) -> str:
        """条件バーのチップ見出し（× で退場する最上位の次元）。"""
        return t(_CHIP_KEYS[self.prefix], name=self.name())

    def empty_message(self, state: str) -> str:
        """0 タイルの案内文言。*state* は ``grid_empty_state.overlay_zero_kind`` の後半。

        ``"filtered"``（母集合はあるのに一覧の上の絞り込みが 0 にした）は
        平常グリッドと同じ文言 — 「このフォルダにファイルはありません」では
        事実と逆になる。``"empty"`` の横断一覧だけは軸ごとに「印の付け方」が
        違うので :meth:`CurationList.empty_message` へ委ねる。
        """
        if state == "filtered":
            return t("viewer.post_grid.empty_filtered")
        cur = self.curation
        if state == "empty" and cur is not None:
            return cur.empty_message()
        key = _EMPTY_KEYS.get((self.prefix, state))
        return t(key) if key is not None else ""

    def sort_axis(self) -> str:
        """入場時に奪う並び順 — 一覧が *about* にしている軸。"""
        cur = self.curation
        return cur.sort_mode() if cur is not None else "mtime_desc"

    # ---- ライフサイクル -----------------------------------------------

    def reset_population(self) -> None:
        """母集合と失敗の記録だけ捨てる（同じ一覧の**再解決**の前段）。

        ``kind`` / ``saved_sort`` は残す — 張り替え後の再解決
        (``_rebind_curation_ghost``) はユーザーが一覧の上に載せた条件も
        入場前の並び順も巻き添えにしない。
        """
        self.entries = []
        self.rel_paths = {}
        self.ghosts = []
        self.ghost_keys = set()
        self.ghost_missing_keys = set()
        self.missing = 0
        self.unreadable = 0
        self.failed = False
        self.scanned = 0

    def teardown(self) -> str | None:
        """退場の 1 行。解決 / 走査を切り、母集合を捨て、返す並び順を答える。

        入場 / 退場 / 再ルート / 窓じまいの 4 経路 × 2 一覧 = 8 か所へ手書きで
        複製されていた片付けは、すべてこれ 1 つを通る。戻り値は
        ``_set_sort_silently`` へそのまま渡す「入場前の並び順」（``None`` =
        返すものが無い）。
        """
        if self.cancel is not None:
            self.cancel()
        self.reset_population()
        self.pending = False
        saved, self.saved_sort = self.saved_sort, None
        return saved


__all__ = ["OverlayList"]
