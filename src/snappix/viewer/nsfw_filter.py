"""年齢区分の抑制（「グリッドで年齢制限を隠す」）— Qt 非依存の純関数群。

``rating:`` が**検索の軸**（クエリの一部）なのに対し、こちらは**ビューの軸** =
「いま見ているものの見せ方」の永続設定で、素の閲覧グリッド・AI 検索結果・
最近追加一覧に等しく掛かる（横断キュレーション一覧だけが素通し — 母集合が
利用者自身の付けた印の再掲だから）。軸の性質が違うので
:class:`~.filter_predicates.FilterBarCriteria` の 1 行にはせず、同じ作法
（値を受け取り、列を**順序を保ったまま**絞り、ホスト属性を直読みしない）で
別モジュールに置く。

判定に要る材料は 3 つだけで、どれも同期のメモリ引きである契約:

* 抑制する区分の集合（:func:`hide_bands` — tags.db が無ければ常に空）
* ``path -> rating`` の既知表（``PostGrid._nsfw_rating_map``）
* その鍵を既に照会済みか（解決器の帳簿）

**不変**: 区分が判っていない行は **見えたまま**にして照会へ回す（「判るまで
出す → 判ったら隠す」）。逆にすると、グリッドが一瞬空になってから戻る。

テストは ``tests/test_viewer_nsfw_filter.py``（``QApplication`` 不要）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..common.i18n import t
from .folder_scan import FolderEntry

__all__ = [
    "HIDE_BANDS",
    "LABEL_KEYS",
    "NsfwPartition",
    "button_text",
    "gate_tooltip",
    "hide_bands",
    "normalize_band",
    "partition",
]

#: 抑制バンド → タイルが持っていると隠される ``images.rating`` の集合。
#: ``off`` = 抑制なし（空集合は呼び出し側が「全部出す」と読む）。
#: 強さは safe < questionable < explicit。
HIDE_BANDS: dict[str, set[str]] = {
    "off": set(),
    "explicit": {"explicit"},
    "questionable": {"questionable", "explicit"},
}

#: 席のラベル ⇄ メニュー項目の単一情報源（現在値の表示名）。
LABEL_KEYS = {
    "off": "viewer.post_grid.nsfw_off",
    "explicit": "viewer.post_grid.nsfw_explicit",
    "questionable": "viewer.post_grid.nsfw_questionable",
}


def normalize_band(value: object) -> str:
    """永続値 → 台帳にある区分（知らない値は中立 ``"off"``）。"""
    return value if isinstance(value, str) and value in HIDE_BANDS else "off"


def hide_bands(value: str, *, has_ratings: bool) -> set[str]:
    """抑制する区分の集合（*has_ratings* 偽 = tags.db 不在なら常に空）。

    「設定は永続するが、区分を答えられる索引が無ければ何も隠せない」を 1 か所
    に閉じる — 席の開閉 (:func:`gate_tooltip`) と同じ前提を述語側も読む。
    """
    if not has_ratings:
        return set()
    return HIDE_BANDS.get(value, set())


@dataclass(frozen=True)
class NsfwPartition:
    """1 回の抑制パスの結果。

    * ``kept`` — 画面に残る行（順序は入力のまま）。
    * ``unknown_folders`` / ``unknown_files`` — 区分が未知でまだ照会していない
      行（**見えたまま**なので、隠すためではなく判るために投げる）。
    * ``hidden_count`` — 実際に隠した件数。0 タイルになったときに犯人を名乗る
      空状態と、件数行の「(…N 件を非表示中)」が読む唯一の値。
    """

    kept: "list[FolderEntry]" = field(default_factory=list)
    unknown_folders: "list[Path]" = field(default_factory=list)
    unknown_files: "list[Path]" = field(default_factory=list)
    hidden_count: int = 0


def partition(
    entries: "Sequence[FolderEntry]",
    bands: "Iterable[str]",
    rating_of: Callable[[str], str | None],
    is_requested: Callable[[str], bool],
) -> NsfwPartition:
    """*entries* を「残す行」と「まだ区分が判らない行」に分ける。

    *bands* が空（抑制オフ / tags.db 不在）なら入力をそのまま残し、隠蔽数も
    照会も 0 — 呼び出し側は分岐を持たずに済む。*rating_of* と *is_requested*
    はどちらも同期のメモリ引きであること（描画経路から呼ばれる）。
    """
    hide = set(bands)
    if not hide:
        return NsfwPartition(kept=list(entries))
    unknown_folders: "list[Path]" = []
    unknown_files: "list[Path]" = []
    kept: "list[FolderEntry]" = []
    for e in entries:
        key = str(e.path)
        rating = rating_of(key)
        if rating is None:
            if not is_requested(key):
                (unknown_folders if e.is_dir else unknown_files).append(e.path)
            kept.append(e)  # 未知 → いまは見えたまま
        elif rating not in hide:
            kept.append(e)
        # else: 既知の該当行 → 隠す
    return NsfwPartition(
        kept=kept,
        unknown_folders=unknown_folders,
        unknown_files=unknown_files,
        hidden_count=len(entries) - len(kept),
    )


def button_text(value: str) -> str:
    """年齢区分ボタンのラベル（中立値では素の文言へ戻す）。

    設定は永続するのに席のラベルが常に同じだと、いま何が効いているかをメニュー
    を開かないと確かめられない。逆に中立（``off``）で「隠さない」を出し続けると
    条件が効いているように見えるので、そこは素の文言に戻す。
    """
    label_key = LABEL_KEYS.get(value)
    if value == "off" or label_key is None:
        return t("viewer.post_grid.nsfw_menu")
    return t("viewer.post_grid.nsfw_menu_active", value=t(label_key))


def gate_tooltip(has_ratings: bool) -> str:
    """席とメニューを 1 対で開閉するときの理由ツールチップ（有効なら空）。"""
    return "" if has_ratings else t("viewer.post_grid.nsfw_needs_tagsdb")
