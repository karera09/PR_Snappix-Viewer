"""左ペインの値型・台帳定数・純関数ヘルパー（``post_grid`` から切り出し）.

:mod:`.post_grid` は「クロームの所有・スキャン着地・グリッド再構築」を持つ
ウィジェットで、そこへ混ざっていた**モジュールレベルの値型と純関数**をここへ
分離した（移動のみ・振る舞いは同じ）。後方互換のため ``post_grid`` が全て
re-export するので、既存の ``from .post_grid import SearchSnapshot`` 等は
そのまま動く。

置いてあるもの:

* :class:`_ViewDim` — グリッドの絞り込み次元 1 行分の台帳型。
* :class:`SearchSnapshot` とその直列化（保存した検索 / ナビ履歴）。
* :func:`describe_search_payload` / :func:`saved_search_tooltip` — 保存した
  検索の 1 行要約（ペイロードを :func:`.condition_chips.state_from_payload`
  で観測値へ組み直し、ライブの条件チップと同じ表を通す）。
* キャプション組み立て（:func:`_format_subtitle` / :func:`split_rel_caption`）。
* ユーザータグ編集欄のトークン単位補完（:class:`_UserTagCompleter`）。
* 並び順ラベル表 / NSFW 抑制バンド表などの台帳定数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, NamedTuple

from PySide6.QtWidgets import QCompleter, QLineEdit

from ..common.format import format_bytes
from ..common.i18n import t
from . import ai_pack, condition_chips
from ._indicator import LOCKED_CAPTION_GLYPH
from .condition_chips import payload_num as _payload_num
from .folder_scan import FolderEntry
from .search_dimensions import token_fields
from .state import ViewerState


class _ViewDim(NamedTuple):
    """グリッドの絞り込み次元 1 つ分（ビュー次元表の 1 行）.

    「いまグリッドに何を出すか」は『母集合を 1 つ選び、適用中の次元で順に
    絞り、フォルダ先頭で並べる』の 1 文に集約される。この表が次元 1 つに
    つき 1 行で「判定・適用・チップ・適用範囲」を束ね、
    :meth:`~.post_grid.PostGrid._fold_view_dimensions`（適用）/
    :meth:`~.post_grid.PostGrid._condition_dimensions`（チップ化）/
    :meth:`~.post_grid.PostGrid._overlay_narrowing_engaged`（導出値）が
    同じ行を読む。母集合ごとに適用列を手書きすると、1 か所への足し忘れが
    「見えているのに効かない条件」「効いているのに見えない条件」になる。

    * ``engaged`` — この次元がいま効いているか。
    * ``apply`` — 汎用 fold で適用する述語。``None`` は「母集合構築側が
      自前で適用する」次元（AI クエリ・絞り込み欄・locked の平常/AI 適用
      など、母集合ごとに意味論が異なるもの）で、表はチップと engaged の
      唯一の情報源としてだけ働く。
    * ``chip`` — 条件バーのチップ ``(label, kind)``。
    * ``clear`` — **この次元だけを中立へ戻す**正規手段（``_clear_dim_*``）。
      列として出してあるので、リセット導線（AI パネルの「詳細条件のみ
      リセット」）は軸集合を手書きで数え直さずに表を読むだけで済む。
    * ``at_neutral`` — 「いま**中立値**か」。既定は ``not engaged()``。
      チップの点灯（= いまグリッドを絞っているか）と中立判定が食い違う軸
      だけが宣言する: AI の精度 / 表示単位は AIタグ検索中でなければチップは
      点かないが、値は非既定のまま次の検索へ持ち越される。
    * ``scopes`` — この次元が**絞り込みとして参加する**ビュー
      （``"plain"`` / ``"advanced"`` / ``"overlay"`` の部分集合）。
      オーバーレイに参加しない locked / 投稿日 はここから外れることで、
      チップだけ出て効かない矛盾が構造的に消える。
    * ``chip_scopes`` — チップを出すビュー。``None`` = ``scopes`` と同じ。
      現在使う行は無い（適用と表示をあえて分けたい将来の軸のために機構だけ
      残してある）。

    文言・書式・所有者（AI パック帰属）・選択肢は条件次元レジストリ
    :mod:`.search_dimensions`（``key`` で 1:1 対応）が持つ。
    """

    key: str
    engaged: Callable[[], bool]
    apply: Callable[[list[FolderEntry]], list[FolderEntry]] | None
    chip: Callable[[], tuple[str, str]]
    clear: Callable[[], None]
    scopes: frozenset[str]
    chip_scopes: frozenset[str] | None = None
    at_neutral: Callable[[], bool] | None = None

    def is_neutral(self) -> bool:
        """この次元がいま中立値か（= :attr:`clear` を呼んでも何も変わらないか）.

        既定は「効いていない」の裏返し。:attr:`at_neutral` を宣言した軸だけ
        が別の答えを返す。
        """
        if self.at_neutral is not None:
            return self.at_neutral()
        return not self.engaged()


#: 値の出所が post.md **ではない**分野トークンの ``TokenField.source``。
#: ``name`` はパス名、``title`` は post.md 不在なら ``FolderEntry.title`` =
#: フォルダ名へフォールバックする（``folder_scan`` の既定）ので、どちらも
#: post.md を必要としない。``match`` が ``curation`` / ``control`` の行
#: （``star:`` / ``mytags:`` / ``later:`` / ``type:`` …）は下の導出で外れる。
_NON_POST_MD_TOKEN_SOURCES = frozenset({"path_name", "title"})

#: post.md を情報源に持つ分野トークンの集合（条件次元レジストリからの導出）。
#: 台帳へ行を足せばここも自動で追随する — 「post.md が無いから 0 件です」と
#: 名乗ってよい条件の唯一の一覧。
_POST_MD_TOKEN_FIELDS = frozenset(
    tok.field for tok in token_fields()
    if tok.match in ("text", "numeric")
    and tok.source not in _NON_POST_MD_TOKEN_SOURCES
)

#: 印のフィードバックの表示時間（ms）。「効かなかった」知らせは成功より長く
#: 残す（見落としてはいけない知らせだから）。
_CURATION_TOAST_MS = 1500
_CURATION_FAIL_TOAST_MS = 3000

#: 全面占有一覧（横断キュレーション / 最近追加）の母集合でも**メモリだけで**
#: 答えられる分野トークン: コントロール行と post.md 由来の行を除いた残り
#: （``name:`` / ``title:`` / ``star:`` / ``mytags:`` / ``later:``）。
_OVERLAY_FIELD_TOKENS = frozenset(
    tok.field for tok in token_fields()
    if not tok.control and tok.field not in _POST_MD_TOKEN_FIELDS
)

#: scope 集合の略記（``_view_dimensions`` 用）。
_SCOPE_ALL = frozenset({"plain", "advanced", "overlay"})
_SCOPE_PLAIN_ADV = frozenset({"plain", "advanced"})
_SCOPE_ADV = frozenset({"advanced"})
_SCOPE_PLAIN = frozenset({"plain"})


def _user_tag_token_split(text: str) -> tuple[str, str]:
    """*text* を ``(確定済みの前半, 補完対象の末尾トークン)`` へ割る.

    区切り規則は :data:`~.user_meta.USER_TAG_SEPARATOR_RE`（= ``split_user_tags``
    と同一）を使う — 保存側とダイアログ側で「どこがタグの境目か」が食い違うと、
    補完で選んだ候補が保存時に別の切られ方をする。
    """
    from .user_meta import USER_TAG_SEPARATOR_RE

    end = 0
    for m in USER_TAG_SEPARATOR_RE.finditer(text):
        end = m.end()
    return text[:end], text[end:]


class _UserTagCompleter(QCompleter):
    """ユーザータグ編集欄の**トークン単位**補完.

    素の ``QCompleter`` はモデルを**行全体**と突き合わせるので、欄がタグを
    2 つ以上持った瞬間（既存タグを ``", ".join(meta.tags)`` で流し込む
    ダイアログ起動直後を含む）何にも一致しなくなり補完が黙って死ぬ。Qt の
    標準ペアを override して直す: :meth:`splitPath` が入力中のトークンだけを
    渡し、:meth:`pathFromIndex` が確定済みの前半を候補の前に戻す。
    """

    def splitPath(self, path: str) -> list[str]:  # noqa: N802 (Qt API)
        return [_user_tag_token_split(path)[1]]

    def pathFromIndex(self, index) -> str:  # noqa: N802 (Qt API)
        chosen = super().pathFromIndex(index)
        widget = self.widget()
        text = widget.text() if isinstance(widget, QLineEdit) else ""
        head, _tail = _user_tag_token_split(text)
        return head + chosen


#: 永続化される並び順キー → i18n カタログキー（``t()`` はコンボ構築側で引く）。
#: 1 要素目が永続値（``state.py`` の ``SortMode`` Literal はここの全キーを
#: 列挙すること）、2 要素目はカタログキーで表示文字列ではない。
_SORT_LABELS = [
    ("name_asc", "viewer.post_grid.sort_name_asc"),
    ("name_desc", "viewer.post_grid.sort_name_desc"),
    ("posted_desc", "viewer.post_grid.sort_posted_desc"),
    ("posted_asc", "viewer.post_grid.sort_posted_asc"),
    ("favorites_desc", "viewer.post_grid.sort_favorites_desc"),
    ("favorites_asc", "viewer.post_grid.sort_favorites_asc"),
    ("mtime_desc", "viewer.common.sort_mtime_desc"),
    ("mtime_asc", "viewer.post_grid.sort_mtime_asc"),
    # size_* は ``FolderEntry.size``（キャプションが出すのと同じ値。フォルダは
    # 0 なので安定したひと塊になる）。
    ("size_desc", "viewer.post_grid.sort_size_desc"),
    ("size_asc", "viewer.post_grid.sort_size_asc"),
    # ★（高 → 低）。未設定は末尾へ沈む。
    ("star_desc", "viewer.post_grid.sort_star_desc"),
    # セッション内で安定するシャッフル。F5（再読み込み）が
    # ``PostGrid.reshuffle_random_sort()`` で振り直す。
    ("random", "viewer.post_grid.sort_random"),
]


# キャプションのバイト表記は ``common.format.format_bytes`` を共有する
# （ステータスバー / 詳細ウィンドウと同じ "1.5 MiB" になるように）。
_format_size = format_bytes


#: 絞り込み構文ヘルプのポップアップ枠の ``objectName``（2 面共通）。
FILTER_HELP_POPUP_NAME = "filterSyntaxHelp"

#: キャプション 2 行目（サブタイトル）の項目区切り。親パスの前置
#: (:func:`split_rel_caption` の戻り) も同じ区切りで繋ぐ。
SUBTITLE_SEP = " · "

#: ユーザータグ編集欄の区切り。``user_meta.USER_TAG_SEPARATOR_RE``
#: が実際に割る文字であること — 表示用の読点 (``common.sep.comma``)
#: を使うと 1 語として取り込まれる。
_TAG_INPUT_SEP = ", "


def _park_cursor_at_end(line) -> None:
    """``QLineEdit`` の全選択を解いてカーソルを末尾へ置く。

    ``QInputDialog`` は show のたびに既存値を全選択するので、``exec()`` の
    前に ``QTimer.singleShot(0, ...)`` で予約して**後から**上書きする。
    窓ごと畳まれた後に着弾し得るので、C++ 側が先に消えていたら黙って諦める。
    """
    try:
        line.deselect()
        line.setCursorPosition(len(line.text()))
    except RuntimeError:  # pragma: no cover (defensive — 窓が先に畳まれた)
        pass


def _format_subtitle(
    entry: FolderEntry,
    *,
    show_posted: bool = True,
    show_locked: bool = True,
    show_size: bool = True,
    show_plan: bool = False,
) -> str:
    """エントリのメタデータからキャプション 2 行目を組む。

    ``show_*`` は項目ごとのゲート（``ViewerState`` 経由で
    ``PostGrid.apply_settings`` が渡す）。意味検索 / 類似画像の関連度はここに
    **入れない** — 長い結果パスが ``◆NN%`` をキャプションの外へ押し出して
    いたため、``entry.relevance`` を鍵にしたサムネのオーバーレイバッジ
    （席と幅はバッジ語彙レジストリ ``_indicator.badge_corner_layout``、適用は
    ``GalleryView``）として描く。あちらは長い名前に削られない。
    """
    parts: list[str] = []
    if entry.is_dir:
        if show_posted and entry.posted_at is not None:
            parts.append(entry.posted_at.strftime("%Y-%m-%d"))
        # お気に入り数は ``♡N`` のサムネオーバーレイバッジ（席は
        # ``_indicator.badge_corner_layout``）として出すので、ここには
        # **入れない** — タイトルの下に重ねて出すのは冗長。
        if show_locked and entry.locked_count > 0:
            # 南京錠の姿は語彙レジストリ（``_indicator``）が持つ。ここだけは
            # ベクタチップではなく文字形を使う — キャプションは省略処理へ
            # 渡す 1 本のプレーンテキストで、ピクスマップを差し込むには
            # キャプション行の描画そのものを作り替える必要があるため。
            # リテラルの置き場だけは 1 箇所に寄せる。
            parts.append(f"{LOCKED_CAPTION_GLYPH}{entry.locked_count}")
        if show_plan and entry.plan_name:
            plan = entry.plan_name
            if entry.plan_price:
                plan = f"{plan} {entry.plan_price}"
            parts.append(plan)
    else:
        if show_size and entry.size > 0:
            parts.append(_format_size(entry.size))
    return SUBTITLE_SEP.join(parts)


def split_rel_caption(rel: str) -> tuple[str, str]:
    """``"作家/投稿/img.jpg"`` → ``("img.jpg", "作家/投稿")``（親が無ければ ``""``）。

    横断一覧 / 再帰検索のタイルがライブラリ相対パスをそのままキャプション
    1 行目に置くと、``Qt.ElideRight`` の省略が**末尾 = 実体のファイル名**から
    先に削ってしまい、同じ一覧の複数行が「作家/投稿/2026-0…」で揃って区別
    不能になる。名前を 1 行目へ、親パスはサブタイトルの先頭へ回すと、省略
    されるのは「どこにあるか」の側になる — フルパスはタイルのツールチップが
    持つので情報は失われない。

    区切りは ``/`` だけを見る: rel を作る 3 経路（再帰検索の ``as_posix``、
    AIタグ検索、``user_meta_parts.resolve._curation_display_name``）はどれも
    posix 区切りで書く。
    """
    parent, found, name = rel.rpartition("/")
    if not found or not name:
        return rel, ""
    return name, parent


@dataclass(frozen=True)
class SearchSnapshot:
    """左ペインの検索状態まるごとの不変スナップショット。

    ナビ履歴が使う — 検索中のフォルダへドリルインすると検索を解除し、戻る
    で元どおり復元できるように。詳細検索の設定はスクラッチの
    :class:`ViewerState` に相乗りする（既存の ``save_tag_settings`` /
    ``restore_tag_settings`` の往復をそのまま再利用）。``frozen=True`` は
    フィールド単位の契約を強制するだけで、``tag_state`` 自体は可変な
    ``ViewerState`` のまま — ただしこれは :meth:`PostGrid.capture_search_state`
    が作る**私有のスクラッチ**で、読むのは ``restore_tag_settings`` だけ。
    外へ渡したり書き換えたりしないこと（履歴の復元が変更を再生してしまう）。

    3 択の AI モードと参照画像（``ai_mode`` / ``similar_seed``）は
    ``ViewerState`` の外（ディスクに残さない揮発）だが、ここには載せる —
    戻る がランキング / 類似画像のビューを復元できるように。ランク結果は
    ``_resolve_pending_select`` を通って流れ直すので、選択位置も戻る。
    """

    filter_text: str
    recursive: bool
    tag_state: ViewerState
    #: AI 検索の 3 択モード（``"and"`` / ``"rank"`` / ``"similar"``）。
    #: パネル側の唯一の保持状態 ``_ai_mode`` をそのまま運ぶ。
    ai_mode: str = "and"
    similar_seed: str | None = None
    #: フィルタバーの種別述語。揮発（ディスクに残さない）だが「いま離れる
    #: ビュー」の一部なので戻るで復元する。投稿日プリセットは ``tag_state``
    #: 側（``ViewerState.tag_date_preset``）に相乗りする。
    filterbar_media: str = "all"
    #: フィルタバーのキュレーション述語 — ★ 下限コンボと「あとで見る」。
    #: ``filterbar_media`` と同じ揮発値だが、戻るでフィルタバーのビューが
    #: まるごと戻るように載せる。
    filterbar_star_min: int = 0
    filterbar_later: bool = False
    #: ユーザータグ絞り込み（``""`` = 「すべて」）。★ / あとで見ると同じ
    #: 揮発の in-place 述語なので、履歴・保存した検索の両方へ載せる（載せ
    #: ないと ★-only の保存検索が復元で無音の no-op になる）。
    filterbar_user_tag: str = ""
    #: 「🔒 ロックありのみ」。``_search_engaged`` に数えられ
    #: ``clear_search_state`` で解除される検索次元なので、履歴・保存した
    #: 検索にも載せる。
    filter_locked_only: bool = False

    def is_active(self) -> bool:
        """検索次元が 1 つでも効いているか（でなければ no-op のスナップ）.

        グリッドが素の直下子を出すのをやめる条件と同じ: 絞り込み語・再帰
        ウォーク・AIタグ検索・パネル独立の種別フィルタ・意味検索 / 類似画像の
        ランキング。（投稿日は単独では効かない — 既に効いているタグ / 種別
        クエリを狭めるだけなので、それだけでは数えない。）
        """
        return bool(
            self.filter_text.strip()
            or self.recursive
            or self.tag_state.tag_search_enabled
            or self.tag_state.tag_search_media_type not in ("all", "image")
            or self.tag_state.tag_date_preset not in ("all", "")
            or self.filterbar_media != "all"
            or self.filterbar_star_min > 0
            or self.filterbar_later
            or self.filterbar_user_tag
            or self.filter_locked_only
            or self.ai_mode != "and"
            or self.similar_seed
        )


# ``save_tag_settings`` が書く詳細検索の ``ViewerState`` フィールド — 保存した
# 検索が往復させる必要のある部分集合ちょうど。``serialize_search_snapshot`` の
# 隣に置くことで、``save_tag_settings`` へ将来足すフィールドの追記先が 1 か所に
# なる。``tag_search_enabled`` を含めるのは、再適用した保存検索がクエリだけでは
# arm されない形でも arm されるようにするため（復元は arm 状態をクエリ / 年齢
# 区分 OR このフラグから導く）。
_SAVED_SEARCH_TAG_FIELDS = (
    "tag_search_enabled",
    "tag_search_panel_expanded",
    "tag_search_query",
    "tag_search_threshold",
    "tag_search_media_type",
    "tag_search_folder_mode",
    "tag_search_folder_coverage",
    "tag_search_rating",
    "tag_date_preset",
    "tag_date_start",
    "tag_date_end",
)


def serialize_search_snapshot(snap: SearchSnapshot) -> dict:
    """:class:`SearchSnapshot` を JSON 化しやすい ``dict`` へ平たくする。

    永続する次元だけを書く: 絞り込み語・サブフォルダ検索トグル・フィルタバーの
    種別述語・``save_tag_settings`` が書く詳細検索の入力。意味検索 / 類似画像の
    シード（``ai_mode`` / ``similar_seed``）は設計上の揮発値で**意図的に保存
    しない** — タグパネルの永続方針（「検索は揮発、設定は永続」）に揃え、保存
    したスマートフォルダが古い画像パスを再シードしないようにする。逆変換は
    :func:`deserialize_search_snapshot`。
    """
    data: dict = {
        "filter_text": snap.filter_text,
        "recursive": bool(snap.recursive),
        "filterbar_media": snap.filterbar_media or "all",
        # フィルタバーの残り 2 述語（★ 下限と「あとで見る」）。これらも
        # 直列化しないと、★ のみの保存検索が復元で無音の no-op に落ちる。
        "filterbar_star_min": int(snap.filterbar_star_min),
        "filterbar_later": bool(snap.filterbar_later),
        "filterbar_user_tag": str(snap.filterbar_user_tag or ""),
        # 🔒 ロックありのみ — 他の揮発検索次元と同じく保存する。
        "filter_locked_only": bool(snap.filter_locked_only),
    }
    for field in _SAVED_SEARCH_TAG_FIELDS:
        data[field] = getattr(snap.tag_state, field)
    return data


def deserialize_search_snapshot(data: dict) -> SearchSnapshot:
    """:func:`serialize_search_snapshot` の逆 — ``dict`` から組み直す。

    未知 / 欠落キーはスクラッチ ``ViewerState`` の既定へ落ちるので、新しい
    ビルドが書いたペイロード（や手編集された状態ファイル）でも raise せずに
    読める。``ai_mode`` / ``similar_seed`` は常に中立の「AIタグ検索」へ戻す
    （そもそも保存していない）ので、再適用した保存検索は純粋なタグ / 絞り込み
    クエリになる。
    """
    scratch = ViewerState()
    for field in _SAVED_SEARCH_TAG_FIELDS:
        if field in data:
            try:
                setattr(scratch, field, data[field])
            except (ValueError, TypeError):  # pragma: no cover (defensive)
                pass
    return SearchSnapshot(
        filter_text=str(data.get("filter_text", "")),
        recursive=bool(data.get("recursive", False)),
        tag_state=scratch,
        ai_mode="and",
        similar_seed=None,
        filterbar_media=str(data.get("filterbar_media", "all")) or "all",
        # 後方互換: フィルタバーの述語を直列化する前に書かれたペイロードは
        # キーを持たない → 効かない既定（★ 下限なし / 「あとで見る」OFF）へ。
        # 壊れた値（手編集 / 別インスタンスの書き込み）はその軸だけ既定へ
        # 落とす — 要約側 (:func:`describe_search_payload`) と同じ寛容さで、
        # 保存した検索 1 件が丸ごと適用不能にならないようにする。
        filterbar_star_min=_payload_num(
            data.get("filterbar_star_min"), int
        ) or 0,
        filterbar_later=bool(data.get("filterbar_later", False)),
        filterbar_user_tag=str(data.get("filterbar_user_tag", "") or ""),
        # 🔒 を載せる前のペイロードはキーを持たない → 既定 False。
        filter_locked_only=bool(data.get("filter_locked_only", False)),
    )


def describe_search_payload(data: dict, *, floor: float | None = None) -> str:
    """保存した検索の「実際に何を探すのか」1 行要約。

    ライブの条件チップバーと**同じ表**（:func:`.condition_chips.chips`）を
    通す — ペイロードを :func:`.condition_chips.state_from_payload` で観測値へ
    組み直すので、語彙・区切り・軸順だけでなく、AI 軸の可用性ゲート
    （``ai_pack.available()``）・絞り込み欄からの次元トークン除去・母集合
    scope（AI クエリ中は範囲チップを出さない / 種別走査が乗っ取るときは
    タグ・精度・表示単位を並べない）もライブと一致する。この 1 関数が 3 面を
    賄う: 管理ダイアログの条件列、行 / レールのツールチップ、保存時に埋める
    既定名。

    直列化されたペイロードだけを見る純関数（ウィジェットもストアも触らない）
    ので、別セッションで別ライブラリに対して保存された検索でも動く。認識でき
    る次元が 1 つも無ければ ``""`` を返す。

    精度チップの成立条件は「既定値（と記録 floor の高い方）より上」で、floor は
    tags.db 側の値 — ペイロードには無いため、呼び出し側が *floor* を渡せたとき
    だけ載せる（渡せない面 = 保存検索ダイアログ等では省略）。
    """
    state = condition_chips.state_from_payload(
        data, ai_available=ai_pack.available(), floor=floor,
    )
    return t("common.sep.middot").join(
        spec.label for spec in condition_chips.chips(state)
    )


def saved_search_tooltip(entry: dict) -> str:
    """保存した検索 1 件のホバー文 — 条件 + 適用範囲。

    保存した検索を差し出す 3 面（メニュー・左レール・管理ダイアログ）のどこ
    にも「**何に**一致するのか」と「**いまのフォルダから**適用される」の 2 つ
    が出ていなかった。どちらもペイロードだけから言えるのでここで述べ、全ての
    面がこの 1 本を使う。
    """
    summary = describe_search_payload(entry)
    scope = t("viewer.saved_search_dialog.scope_hint")
    return f"{summary}\n{scope}" if summary else scope
