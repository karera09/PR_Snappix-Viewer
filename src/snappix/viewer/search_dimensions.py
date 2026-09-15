"""条件次元レジストリ（UIレビュー 2026-08-28 提案2 第1段）.

検索・絞り込みの 1 つの条件軸（種別・年齢区分・★・精度・検索範囲など）の
知識 — 正式名・中立値・永続性・検索欄トークン接頭辞・保存ペイロードの
キー・チップ書式 — は、従来 条件チップ（``PostGrid._view_dimensions``）/
構文ヘルプ HTML / 検索要約（``describe_search_payload``）/ コンボ構築 に
手書きで複製されており、片側欠落（N-06 / N-30 / N-31 / N-99 …）の温床に
なっていた。本モジュールは **1 軸 = 1 行の台帳**として、表示系 3 面
（条件チップ・構文ヘルプ・検索要約）とコンボ選択肢の単一情報源になる。

Qt 非依存の純データ + 関数（依存は ``common.i18n.t`` のみ）で、単体テスト
は ``tests/test_viewer_search_dimensions.py``。

**第3段（``filter_query`` トークン統合）**: :data:`TOKEN_FIELDS` が
``match`` / ``source`` 列を持ち、``filter_query`` のフィールド表
（``_FILTER_FIELD_GETTERS`` / ``_NUMERIC_FIELDS`` / ``_CURATION_FIELDS`` /
``_CONTROL_FIELDS``）は**この台帳から導出**される
（``filter_query._rebuild_field_tables`` — モジュールロード時に 1 回組み
立て、パース毎の走査は増やさない）。第1段の「集合一致」テストは「同一
ソースからの導出」に置き換わり、補完・ヘルプ・パーサの対象がずれることは
構造的に起きない（台帳へ 1 行足すと全面に現れる — 受け入れテストあり）。

**第2段（2026-08-28 提案2）**: フィルターポップオーバーの行そのものを台帳
から生成する。1 行 = 1 軸で、ラベル・エディタ種別・ウィジェット属性名・
選択肢の出所・ツールチップ（AI 可用性で出し分け — N-51）を宣言し、
``PostGrid._build_filter_popover`` は :func:`filter_rows` を列挙するだけに
なる。行の順序は :data:`FILTER_POPOVER_ROWS`（チップ順 = ``DIMENSIONS``
の順とは別物なので独立して宣言する）。

役割分担:

* :data:`DIMENSIONS` — 条件軸の台帳。実行時の engaged / apply / scopes は
  ``PostGrid._view_dimensions``（軸ごとのライブ状態を閉包で読む実行時表）
  に残り、**文言・書式・所有者・選択肢・エディタ**がこちらに載る。両表は
  ``id`` で 1:1 対応（機械検証あり）。
* ``owner="ai"`` の行は AI パック有効時のみ存在する軸。
  ``ai_pack.available()`` の適用点は ``_view_dimensions`` の行フィルタと
  :func:`filter_rows` の 2 箇所に集約される（rating だけゲートを見忘れる、
  が構造的に起きない）。
* :data:`TOKEN_FIELDS` — 検索欄の ``field:`` トークン台帳（構文ヘルプの
  「分野を限定」「コントロール」表と接頭辞補完が共有する）。
* :func:`chip_text` — 条件チップ / 検索要約の共通書式チョークポイント。
  書式の統一（「ラベル: 値」）は ``chip_form`` 列で宣言し、テストが
  テンプレートの形を機械検証する（N-31 / N-99 の書式ドリフト再発防止）。
* :func:`token_expression` — 「この条件を構文で書くと」の等価表現。
  ``token_prefix`` + ``token_form`` から組むので、AI ポップオーバーの
  共有条件プレビューと検索欄の構文が同じ台帳を情報源にする（提案2 第2段
  — 07-25 #41 の二重存在を「重複コントロール」から「読み取り専用の共有
  条件表示」へ置き換えるための土台）。
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ..common.i18n import t

#: AIタグ精度（しきい値）の製品既定値。``state.ViewerState`` の初期値・
#: ``AiQuery`` の初期値・精度チップの engaged 判定（既定値の
#: ままなら「条件」ではない — N-31）が全てここを参照する（0.35 の手書き
#: 複製を残さない）。
DEFAULT_TAG_THRESHOLD = 0.35


@dataclass(frozen=True)
class SearchDimension:
    """条件軸 1 行。

    * ``id`` — ``PostGrid._view_dimensions`` の ``_ViewDim.key`` と 1:1。
    * ``label_key`` — 正式名の i18n キー。既存のフォーム用ラベルキーを
      再利用するため値は末尾コロンを含み得る（:func:`label` が剥がす）。
      ``None`` はラベル面（ヘルプ・チートシート）に出ない軸。
    * ``chip_key`` — 条件チップ / 検索要約のテンプレキー。``None`` は
      チップ文言を実行時にしか組めない軸（ai_semantic の 2 形態）。
    * ``chip_form`` — テンプレの宣言形。``"label_value"`` = 「ラベル: 値」、
      ``"label_only"`` = ラベル単独（値を持たないトグル軸）、``"custom"`` =
      例外形（宣言必須 — テストが例外集合の増加を検知する）。
    * ``neutral`` / ``neutral_key`` — 中立値（軸が効いていない保存値）と
      その表示ラベルキー。
    * ``persistent`` — 起動時に前セッションの値が**復元**されるか
      （書き込みはされるが起動時に中立へ落とす軸 — 投稿日・年齢区分
      (N-06) — は False。セッション内の履歴スナップショットは別経路で
      全軸を往復する）。``restore_tag_settings`` の実挙動との一致は
      ``tests/test_viewer_search_dimensions.py`` が突き合わせる（項目 #249
      — 宣言だけあって誰も検証していない飾りの列にしない）。
    * ``token_prefix`` — 検索欄の等価トークン（``type:`` 等、あれば）。
    * ``payload_keys`` — 保存ペイロード（SearchSnapshot / ViewerState）の
      キー（表示単位のように 2 キーへ写像される軸があるため tuple）。
      引くのは :func:`payload_key`（``describe_search_payload`` が使う）で、
      ``serialize_search_snapshot`` の実キー集合に含まれることをテストが
      機械検証する（項目 #249）。
    * ``owner`` — ``"ai"`` は AI パック有効時のみ存在する軸。
    * ``value_keys`` — コンボ軸の (保存値, 表示ラベルキー) 列。コンボの
      選択肢順そのもの（:func:`combo_items`）。
    * ``cheatsheet_desc_key`` — AI ポップオーバーのチートシートに 1 行
      持つ軸の説明キー（``None`` = 載らない）。

    第2段（フィルターポップオーバー行の生成）で足した列:

    * ``editor`` — 生成するエディタ種別。``"combo"`` / ``"check"`` /
      ``"none"``（= ポップオーバーに行を持たない軸）。
    * ``widget_attr`` — 生成したウィジェットを載せる属性名。**既存の名前を
      そのまま宣言する**（永続化・``AdvancedSearchController``・スナップショット
      復元・テストが全てこの名前で参照するため、生成化しても不変）。
    * ``form_label_key`` — チェック軸で先頭ラベルを持たせるときの見出し
      キー（検索範囲: ☐ サブフォルダも検索）。``None`` のチェック軸は
      ラベル列ぶん字下げして載る。:func:`form_label` はこちらを優先する。
    * ``combo_source`` — コンボ選択肢の出所。``"ledger"`` = ``value_keys``、
      ``"star"`` =「すべて + ★N以上」（保存値が int の例外形）、
      ``"user_tags"`` = ストアの現在値で実行時に詰め直す。
    * ``tooltip_key`` / ``tooltip_free_key`` — ツールチップ。``_free`` 側が
      あれば AI パック無効時にそちらを使う（N-51 — 素の配布で存在しない
      AI 検索の使用を指示しない。二本立ての規約を台帳の列にした）。
    * ``needs_user_meta`` — ``user_meta`` ストアがあるときだけ出す行
      （無いとキュレーションは常に空の map への絞り込みになる）。
    * ``token_form`` — :func:`token_expression` が組む値の形。``"value"``
      （そのまま / ``token_values`` で読み替え）/ ``"floor"``（``>=N``）/
      ``"flag"``（``yes``）。``None`` = 構文で等価に書けない軸（構文
      プレビューに出さない）。``mytags:`` は N-65（第3段）でコンボと同期
      するようになった — コンボ値（既存タグそのもの）は必ず完全一致で
      同期できるので ``"value"`` で等価に書ける。
    * ``token_values`` — 保存値 → トークン値の読み替え（``rating`` の
      ``sfw`` → ``r15`` など、:func:`token_expression` が書く正準形）。
    * ``token_aliases`` — (別名トークン, 保存値) 列。利用者が検索欄へ打てる
      **入力の語彙**（``動画`` / ``mp4`` / ``r-18`` …）で、軸の選択肢
      （``value_keys``）そのものではないので別列にする。``filter_query`` の
      逆写像表（``type:`` / ``rating:`` の値 → 保存値）は
      ``value_keys`` の恒等写像 + ``token_values`` の読み替え + この列から
      **導出**される（``filter_query._rebuild_token_alias_tables``）ので、
      選択肢を 1 行足したときに「コンボと構文プレビューは出すのにパーサは
      知らないトークン」が生まれない。

    書き込み面（リセット導線）を台帳から導くための列（レビュー 2026-09-03
    項目 #89）:

    * ``panel_reset`` — AI パネルの「詳細条件のみリセット」
      (:meth:`~.advanced_search.AdvancedSearchController._on_reset_advanced_only`)
      が中立化する軸か。``owner`` では表せない: 年齢区分・投稿日は席が
      フィルターポップオーバーへ移って ``owner`` が ``"ai"`` / ``"filter"``
      に分かれたが、どちらも AI クエリの一部なのでリンクは両方を落とす
      （ツールチップ ``reset_link_tooltip`` が名指ししている集合）。この列が
      無かった頃は同じ軸集合が engaged 判定・リセット・緩和候補で 3 回
      手書きされ、精度と表示単位だけが面ごとに落ちていた。
    """

    id: str
    label_key: str | None
    chip_key: str | None
    chip_form: str = "label_value"
    neutral: str | None = None
    neutral_key: str | None = None
    persistent: bool = False
    token_prefix: str | None = None
    payload_keys: tuple[str, ...] = ()
    owner: str = "core"
    value_keys: tuple[tuple[str, str], ...] = ()
    cheatsheet_desc_key: str | None = None
    editor: str = "none"
    widget_attr: str | None = None
    form_label_key: str | None = None
    combo_source: str = "ledger"
    tooltip_key: str | None = None
    tooltip_free_key: str | None = None
    needs_user_meta: bool = False
    token_form: str | None = None
    token_values: tuple[tuple[str, str], ...] = ()
    token_aliases: tuple[tuple[str, str], ...] = ()
    panel_reset: bool = False


#: メディア種別の選択肢（フィルターバーの「種別」と AI 検索の「対象種別」が
#: 同じ 6 択を共有する — 07-25 #41 の二重存在は残るが、選択肢はここ 1 箇所）。
_MEDIA_VALUE_KEYS: tuple[tuple[str, str], ...] = (
    ("all", "common.filter.all"),
    ("image", "common.media_type.image"),
    ("video", "common.media_type.video"),
    ("audio", "common.media_type.audio"),
    ("document", "common.media_type.document"),
    ("archive", "common.media_type.archive"),
)

#: 台帳本体。行順 = 条件チップバーのチップ順（``_view_dimensions`` と同順）。
DIMENSIONS: tuple[SearchDimension, ...] = (
    # AIタグ類似検索 / 類似画像検索 — チップはシード有無で 2 形態
    # （dim_ai_similar / dim_similar_image）を実行時に選ぶため custom。
    SearchDimension(
        id="ai_semantic",
        label_key=None,
        chip_key=None,
        chip_form="custom",
        owner="ai",
        panel_reset=True,
    ),
    SearchDimension(
        id="ai_tags",
        label_key="viewer.advanced_search.ai_tag_label",
        chip_key="viewer.post_grid.dim_ai_tags",
        persistent=True,
        payload_keys=("tag_search_query",),
        owner="ai",
        cheatsheet_desc_key="viewer.advanced_search.cheatsheet_desc_ai_tags",
        panel_reset=True,
    ),
    SearchDimension(
        id="ai_precision",
        label_key="viewer.advanced_search.precision_label",
        chip_key="viewer.advanced_search.badge_precision",
        persistent=True,
        token_prefix="score",
        payload_keys=("tag_search_threshold",),
        owner="ai",
        cheatsheet_desc_key="viewer.advanced_search.cheatsheet_desc_precision",
        token_form="floor",
        panel_reset=True,
    ),
    SearchDimension(
        id="ai_unit",
        label_key="viewer.advanced_search.display_unit_label",
        chip_key="viewer.post_grid.dim_unit",
        neutral="folder_coverage",
        neutral_key="viewer.advanced_search.unit_folder_coverage",
        persistent=True,
        payload_keys=("tag_search_folder_mode", "tag_search_folder_coverage"),
        owner="ai",
        value_keys=(
            ("file", "viewer.advanced_search.unit_file"),
            ("folder_strict", "viewer.advanced_search.unit_folder_strict"),
            ("folder_coverage", "viewer.advanced_search.unit_folder_coverage"),
        ),
        # 説明はこの軸だけ選択肢列から合成する（:func:`cheatsheet_axis_rows`）。
        panel_reset=True,
    ),
    # チップの語「種別(再帰)」とフォームラベル「対象種別 (AI 検索)」の呼び分け
    # は 07-25 #41 の判断を維持 — 台帳上は custom として宣言（第2段の
    # フィルター行生成で調停する候補）。
    SearchDimension(
        id="ai_media",
        label_key="viewer.advanced_search.media_type_label",
        chip_key="viewer.post_grid.dim_media_recursive",
        chip_form="custom",
        neutral="all",
        neutral_key="common.filter.all",
        persistent=True,
        payload_keys=("tag_search_media_type",),
        owner="ai",
        value_keys=_MEDIA_VALUE_KEYS,
        panel_reset=True,
    ),
    # 年齢区分 — 保存はされるが起動時は "all" へ落とす（N-06 案B。投稿日と
    # 同型の「検索は揮発・設定は永続」）。
    # 年齢区分は AI ポップオーバーではなく**フィルターポップオーバー**の行
    # （提案2 第2段 — AI 側の重複軸整理）。owner="ai" なので素の配布では
    # 行ごと出ない（ウィジェットは inert に存在する — advanced_search.py）。
    SearchDimension(
        id="rating",
        label_key="viewer.advanced_search.age_band_label",
        chip_key="viewer.post_grid.dim_rating",
        neutral="all",
        neutral_key="common.filter.all",
        token_prefix="rating",
        payload_keys=("tag_search_rating",),
        owner="ai",
        value_keys=(
            ("all", "common.filter.all"),
            ("safe", "viewer.advanced_search.rating_safe"),
            ("sfw", "viewer.advanced_search.rating_sfw"),
            ("explicit", "viewer.advanced_search.rating_explicit"),
        ),
        editor="combo",
        widget_attr="tag_rating_combo",
        tooltip_key="viewer.advanced_search.rating_tooltip",
        token_form="value",
        # 保存値 → 正準トークン（sfw 帯 = R-15）。
        token_values=(("sfw", "r15"), ("explicit", "r18")),
        # 入力の語彙 → 保存値。``sfw`` は「全年齢」の意（安全側）で、帯として
        # の sfw = safe+questionable は ``r15`` が正準トークン — 別名表は
        # 恒等写像より後に適用されるのでこの読み替えが勝つ。
        token_aliases=(
            ("any", "all"), ("すべて", "all"),
            ("sfw", "safe"), ("allages", "safe"), ("全年齢", "safe"),
            ("r-15", "sfw"), ("questionable", "sfw"),
            ("r-18", "explicit"), ("nsfw", "explicit"), ("adult", "explicit"),
        ),
        panel_reset=True,
    ),
    SearchDimension(
        id="media",
        label_key="common.label.type_colon",
        chip_key="viewer.post_grid.dim_media",
        neutral="all",
        neutral_key="common.filter.all",
        token_prefix="type",
        payload_keys=("filterbar_media",),
        value_keys=_MEDIA_VALUE_KEYS,
        editor="combo",
        widget_attr="filterbar_media_combo",
        tooltip_key="viewer.post_grid.filterbar_media_tooltip",
        # N-51: 素の配布に AI 検索は存在しないので、再帰の代替導線も違う。
        tooltip_free_key="viewer.post_grid.filterbar_media_tooltip_free",
        token_form="value",
        # 入力の語彙 → 保存値（正準値そのものは ``value_keys`` から導出）。
        token_aliases=(
            ("any", "all"), ("すべて", "all"),
            ("img", "image"), ("photo", "image"), ("画像", "image"),
            ("movie", "video"), ("mp4", "video"), ("動画", "video"),
            ("sound", "audio"), ("音声", "audio"),
            ("doc", "document"), ("pdf", "document"), ("文書", "document"),
            ("zip", "archive"), ("アーカイブ", "archive"),
        ),
    ),
    # ★ — チップは既存の「★{n}以上」（コンボ選択肢そのもの）を維持する
    # 意図的な例外形。保存値が int なのでコンボ選択肢も専用の出所。
    SearchDimension(
        id="star",
        label_key="viewer.post_grid.filterbar_star_label",
        chip_key="viewer.post_grid.filterbar_star_min",
        chip_form="custom",
        neutral="0",
        neutral_key="common.filter.all",
        token_prefix="star",
        payload_keys=("filterbar_star_min",),
        editor="combo",
        widget_attr="filterbar_star_combo",
        combo_source="star",
        tooltip_key="viewer.post_grid.filterbar_star_tooltip",
        needs_user_meta=True,
        token_form="floor",
    ),
    # 「あとで見る」は動詞（付ける）と条件（付いたものだけ）で別の語にする —
    # ``locked`` が「ロックありのみ」で先に採っている形（N-115）。
    SearchDimension(
        id="later",
        label_key="viewer.post_grid.filterbar_later_label",
        chip_key="viewer.post_grid.filterbar_later_label",
        chip_form="label_only",
        token_prefix="later",
        payload_keys=("filterbar_later",),
        editor="check",
        widget_attr="filterbar_later_check",
        tooltip_key="viewer.post_grid.filterbar_later_tooltip",
        needs_user_meta=True,
        token_form="flag",
    ),
    SearchDimension(
        id="usertag",
        label_key="viewer.post_grid.filterbar_usertag_label",
        chip_key="viewer.post_grid.dim_usertag",
        neutral="",
        neutral_key="common.filter.all",
        token_prefix="mytags",
        payload_keys=("filterbar_user_tag",),
        editor="combo",
        widget_attr="filterbar_usertag_combo",
        combo_source="user_tags",
        tooltip_key="viewer.post_grid.filterbar_usertag_tooltip",
        needs_user_meta=True,
        # N-65（第3段）: ``mytags:値`` は既存タグと casefold 完全一致する
        # ときだけコンボへ同期する（star:>=N と同じ「表現可能なときだけ
        # 同期」の規約 — post_grid._syncable_curation_value）。コンボ値は
        # 定義上その完全一致形なので、構文プレビューは等価に書ける。
        token_form="value",
    ),
    # 投稿日 — 保存はされるが起動時は "all"（state.py の注記参照）。
    SearchDimension(
        id="date",
        label_key="common.label.posted_colon",
        chip_key="viewer.post_grid.dim_date",
        neutral="all",
        neutral_key="common.filter.all",
        payload_keys=("tag_date_preset",),
        value_keys=(
            ("all", "common.filter.all"),
            ("today", "viewer.post_grid.date_today"),
            ("7d", "viewer.post_grid.date_7d"),
            ("30d", "viewer.post_grid.date_30d"),
            ("1y", "viewer.post_grid.date_1y"),
            ("range", "viewer.post_grid.date_range"),
        ),
        editor="combo",
        widget_attr="tag_date_combo",
        tooltip_key="viewer.post_grid.filterbar_date_tooltip",
        panel_reset=True,
    ),
    SearchDimension(
        id="locked",
        label_key="viewer.post_grid.locked_only",
        chip_key="viewer.post_grid.locked_only",
        chip_form="label_only",
        payload_keys=("filter_locked_only",),
        editor="check",
        widget_attr="locked_check",
        tooltip_key="viewer.post_grid.locked_only_tooltip",
    ),
    # 検索範囲 — フィルターポップオーバーの先頭行（UIレビュー 2026-09-11 E3:
    # 旧「検索オプション」ポップオーバーの唯一の軸だったが、条件次元として
    # ここに載った以上、別 UI に置く理由が無い — N-26 / N-124）。
    SearchDimension(
        id="recursive",
        label_key="viewer.post_grid.recursive_check",
        chip_key="viewer.post_grid.recursive_check",
        chip_form="label_only",
        payload_keys=("recursive",),
        editor="check",
        widget_attr="recursive_check",
        form_label_key="viewer.post_grid.scope_label",
        tooltip_key="viewer.post_grid.recursive_tooltip",
    ),
    # 絞り込み欄 — チップは値を引用符で囲む既存形（『絞り込み: "{text}"』）。
    SearchDimension(
        id="filter",
        label_key=None,
        chip_key="viewer.post_grid.dim_filter",
        chip_form="custom",
        payload_keys=("filter_text",),
    ),
)

_BY_ID: dict[str, SearchDimension] = {dim.id: dim for dim in DIMENSIONS}

#: フィルターポップオーバーの行順（提案2 第2段のモック記述「同一書式 6 行」）。
#: ``DIMENSIONS`` の順はチップ順なので、席の順序はここで独立に宣言する。
#: 年齢区分は AI パック有効時のみ（``owner="ai"`` を :func:`filter_rows` が
#: 落とす）、★ / あとで見る / ユーザータグは ``user_meta`` ストアがあるとき
#: のみ。「あとで見る」は N-99 の裁定で★と同居せず独立行。
FILTER_POPOVER_ROWS: tuple[str, ...] = (
    "recursive", "media", "rating", "date", "star", "later", "usertag", "locked",
)

#: :func:`token_expression` の出力順。フィルターポップオーバーの行順を先に
#: 置き、そこに席を持たない軸（精度など）を ``DIMENSIONS`` 順で後ろへ —
#: 「画面で上から読んだ順にトークンが並ぶ」ようにするため。
_TOKEN_EXPRESSION_ORDER: tuple[str, ...] = FILTER_POPOVER_ROWS + tuple(
    dim.id for dim in DIMENSIONS if dim.id not in FILTER_POPOVER_ROWS
)


@dataclass(frozen=True)
class TokenField:
    """検索欄 ``field:`` トークン 1 行（構文ヘルプ表・接頭辞補完の共有台帳）.

    * ``desc_key`` — 説明の i18n キー（補完ドロップダウンとヘルプ表が
      同じ文言を使う）。
    * ``desc_ai_key`` — AI パック有効時に差し替える説明（``tags:`` の
      「AIタグは AI 検索へ」誘導など。``None`` = 共通）。
    * ``owner`` — ``"ai"`` は AI パック有効時のみ補完・ヘルプに出す
      （``filter_query`` は無効時も受理するが inert）。

    第3段（``filter_query`` トークン統合）で足した列 — ``filter_query`` は
    この 2 列からフィールド表を導出する（値文法そのもの — ``>=N`` /
    ``yes|no`` — は挙動なので ``filter_query`` 側に残る。台帳が持つのは
    **列挙**で、行を足せば補完・ヘルプ・パーサの全対象に同時に現れる）:

    * ``match`` — 照合の種別。``"text"`` = casefold 部分一致（既定）、
      ``"numeric"`` = ``[op]数値`` 比較、``"curation"`` = 注入される
      user_meta ルックアップ経由（``star:`` / ``mytags:`` / ``later:``）、
      ``"control"`` = GUI コントロール等価トークン（照合しない — ヘルプ
      では別表に載る）。
    * ``source`` — ``text`` / ``numeric`` 行の値の出所（``FolderEntry``
      の属性名。擬似ソース ``"path_name"`` = ``path.name``、``"body"`` =
      post.md 本文の遅延読みは ``filter_query`` が特別扱いする）。
    """

    field: str
    desc_key: str
    desc_ai_key: str | None = None
    owner: str = "core"
    match: str = "text"
    source: str | None = None

    @property
    def control(self) -> bool:
        """コントロールトークンか（``match`` 列から導出）。"""
        return self.match == "control"


#: 行順 = 構文ヘルプの表示順（分野 → コントロール）。
TOKEN_FIELDS: tuple[TokenField, ...] = (
    TokenField("tags", "viewer.post_grid.prefix_tags",
               desc_ai_key="viewer.post_grid.prefix_tags_ai", source="tags"),
    TokenField("title", "viewer.post_grid.prefix_title", source="title"),
    TokenField("name", "viewer.post_grid.prefix_name", source="path_name"),
    TokenField("plan", "viewer.post_grid.prefix_plan", source="plan_name"),
    TokenField("plan_price", "viewer.post_grid.prefix_plan_price",
               source="plan_price"),
    TokenField("favorites", "viewer.post_grid.prefix_favorites",
               match="numeric", source="favorites"),
    TokenField("body", "viewer.post_grid.prefix_body", source="body"),
    TokenField("star", "viewer.post_grid.prefix_star", match="curation"),
    TokenField("mytags", "viewer.post_grid.prefix_mytags", match="curation"),
    TokenField("later", "viewer.post_grid.prefix_later", match="curation"),
    TokenField("type", "viewer.post_grid.prefix_type", match="control"),
    TokenField("rating", "viewer.post_grid.prefix_rating",
               owner="ai", match="control"),
    TokenField("score", "viewer.post_grid.prefix_score",
               owner="ai", match="control"),
)


# ------------------------------------------------------------------ 基本照会


def get(dim_id: str) -> SearchDimension | None:
    """台帳行を引く（未知 id は ``None`` — 呼び出し側の防御は不要にしない）。"""
    return _BY_ID.get(dim_id)


def payload_key(dim_id: str, index: int = 0) -> str:
    """軸の保存ペイロードキー（``SearchSnapshot`` / ``ViewerState`` の項目名）。

    ``payload_keys`` を**実際に消費する**唯一の口。これが無かったころ、保存
    ペイロード面だけが台帳を迂回して ``data.get("filterbar_media", …)`` の
    手書きリテラルで書かれており、台帳側の列は誰にも読まない飾りだった
    （レビュー 2026-09-03 項目 #249）。飾りのままだと「新しい軸を足す人が
    ``payload_keys`` を書いて満足し、要約側への追加を忘れる」= 保存した検索の
    条件要約から軸が丸ごと欠ける（項目 #164 と同型）を誘発する。

    *index* は表示単位のように 1 軸が 2 キーへ写像される場合の位置。
    """
    return _BY_ID[dim_id].payload_keys[index]


def dimensions(*, ai: bool = True) -> tuple[SearchDimension, ...]:
    """台帳の行（表示順）。*ai* が偽なら ``owner="ai"`` の軸を除く。"""
    if ai:
        return DIMENSIONS
    return tuple(dim for dim in DIMENSIONS if dim.owner != "ai")


def label(dim_id: str) -> str:
    """軸の正式名（表示用 — フォームラベルキーの末尾コロンは剥がす）。"""
    dim = _BY_ID[dim_id]
    if dim.label_key is None:
        return ""
    return t(dim.label_key).rstrip(":：")


def form_label(dim_id: str) -> str:
    """フォーム行の先頭ラベル（正式名 + ASCII コロン）.

    N-99: 2 面で「コロンあり 4 / なし 3」に割れていた。正式名は
    :func:`label` が末尾コロンを剥がしたものなので、ここで**必ず 1 つ**
    足す — カタログ値のコロン有無に関わらず書式が揃う（半角/全角の
    混在も起こせない）。
    """
    dim = _BY_ID[dim_id]
    name = t(dim.form_label_key) if dim.form_label_key else label(dim_id)
    return f"{name}:" if name else ""


def chip_text(dim_id: str, **params) -> str:
    """条件チップ / 検索要約の共通書式チョークポイント。

    書式そのもの（「ラベル: 値」）は i18n テンプレートに残す（翻訳可能・
    既存キー互換）が、**全チップがここを通る**ことと ``chip_form`` の宣言
    をテストが機械検証することで、軸ごとの書式ドリフト（N-31 の
    「精度0.35」だけコロン無し等）を再発不能にする。
    """
    dim = _BY_ID[dim_id]
    if dim.chip_key is None:  # pragma: no cover (ai_semantic は実行時組み立て)
        raise ValueError(f"dimension {dim_id!r} has no chip template")
    return t(dim.chip_key, **params)


def value_label(dim_id: str, stored: str) -> str | None:
    """コンボ軸の保存値 → 表示ラベル（中立値・未知値は ``None``）。

    「中立値は条件ではない（チップ・要約に載せない）」の判定を兼ねる —
    ``describe_search_payload`` の旧 ``_MEDIA_LABELS`` 等 3 つの手書き写像を
    置き換える。
    """
    dim = _BY_ID[dim_id]
    if stored == dim.neutral:
        return None
    for value, key in dim.value_keys:
        if value == stored:
            return t(key)
    return None


def combo_items(dim_id: str) -> tuple[tuple[str, str], ...]:
    """コンボ軸の (保存値, 表示ラベルキー) 列 — 選択肢順そのもの。"""
    return _BY_ID[dim_id].value_keys


# ------------------------------------------------ フィルターポップオーバー


def filter_rows(
    *, ai: bool = True, user_meta: bool = True,
) -> tuple[SearchDimension, ...]:
    """フィルターポップオーバーに出す行（:data:`FILTER_POPOVER_ROWS` 順）.

    *ai* が偽なら ``owner="ai"`` の軸（年齢区分）を、*user_meta* が偽なら
    ``needs_user_meta`` の軸（★ / あとで見る / ユーザータグ）を落とす。
    行の有無を決める条件はこの 1 関数だけが持つ — ``_build_filter_popover``
    と「その行が実際に存在するか」を見る側（アクセント同期・状態復元）が
    別々の条件を書いて食い違う、という第1段以前の形を作らない。
    """
    rows: list[SearchDimension] = []
    for dim_id in FILTER_POPOVER_ROWS:
        dim = _BY_ID[dim_id]
        if not ai and dim.owner == "ai":
            continue
        if not user_meta and dim.needs_user_meta:
            continue
        rows.append(dim)
    return tuple(rows)


def tooltip(dim_id: str, *, ai: bool = True) -> str:
    """軸のツールチップ（AI パック無効時は ``tooltip_free_key`` を優先）.

    N-51: 素の配布に存在しない AI 検索の使用を指示するツールチップが 1 件
    だけ無条件で出ていた。二本立ての規約を台帳の列にしたので、行の追加時に
    片側だけ書き忘れることが起きない（``_free`` が無い軸は共通文言）。
    """
    dim = _BY_ID[dim_id]
    if not ai and dim.tooltip_free_key is not None:
        return t(dim.tooltip_free_key)
    if dim.tooltip_key is None:
        return ""
    return t(dim.tooltip_key)


def token_expression(values: dict[str, object]) -> str:
    """「この条件を構文で書くと」の等価表現（``type:image rating:r18``）.

    *values* は ``{軸 id: 現在値}`` — **どの軸を載せるかは呼び出し側が決める**
    （AI ポップオーバーの共有条件プレビューは フィルター側の軸だけを渡す）。
    中立値・空値・``token_form`` を持たない軸は落とす。``mytags:`` は N-65
    （第3段）でコンボ完全一致形の同期が入ったので等価に書ける（含める）。
    値が 1 トークンに書けない形（空白入り — ユーザータグは
    ``split_user_tags`` の規約上あり得ないが防御）は落とす。出力順は
    :data:`_TOKEN_EXPRESSION_ORDER`（画面の行順 = 読み手が目で追う順）。
    """
    parts: list[str] = []
    for dim_id in _TOKEN_EXPRESSION_ORDER:
        dim = _BY_ID[dim_id]
        if dim.token_prefix is None or dim.token_form is None:
            continue
        if dim.id not in values:
            continue
        raw = values[dim.id]
        if dim.token_form == "flag":
            if raw:
                parts.append(f"{dim.token_prefix}:yes")
            continue
        if dim.token_form == "floor":
            try:
                num = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):  # pragma: no cover (防御)
                continue
            if num <= 0:
                continue
            parts.append(f"{dim.token_prefix}:>={num:g}")
            continue
        key = str(raw)
        if not key or key == dim.neutral:
            continue
        value = dict(dim.token_values).get(key, key)
        if any(ch.isspace() for ch in value):
            continue  # 1 トークンに書けない値は等価表現を持たない（防御）
        parts.append(f"{dim.token_prefix}:{value}")
    return " ".join(parts)


def token_fields(*, ai: bool = True) -> tuple[TokenField, ...]:
    """検索欄トークンの台帳行。*ai* が偽なら ``owner="ai"`` を除く。"""
    if ai:
        return TOKEN_FIELDS
    return tuple(tok for tok in TOKEN_FIELDS if tok.owner != "ai")


def token_description(tok: TokenField, *, ai: bool = True) -> str:
    """トークン 1 行の説明（AI 有効時は ``desc_ai_key`` を優先）。"""
    if ai and tok.desc_ai_key is not None:
        return t(tok.desc_ai_key)
    return t(tok.desc_key)


# ------------------------------------------------- 構文ヘルプ / チートシート


def _rows_html(rows: list[tuple[str, str]]) -> str:
    """(コード, 説明) 行を `<table>` に畳む（説明はプレーンテキスト扱い）。"""
    cells = "".join(
        f"<tr><td><code>{html.escape(code)}</code></td>"
        f"<td>{html.escape(desc)}</td></tr>"
        for code, desc in rows
    )
    return f"<table cellspacing='3'>{cells}</table>"


def filter_help_html(*, ai: bool) -> str:
    """絞り込み構文チートシートの本文 HTML（「?」ヘルプ / 初回自動表示）.

    静的な説明（構文表・見出し・脚注）は i18n に残し、**軸の一覧**
    （``分野:`` の各行と コントロールトークンの各行）だけを台帳
    :data:`TOKEN_FIELDS` から生成する — 接頭辞補完と同じ行・同じ説明文が
    並ぶことが構造的に保証される（提案2 第1段）。
    """
    fields = [
        (f"{tok.field}:", token_description(tok, ai=ai))
        for tok in token_fields(ai=ai) if not tok.control
    ]
    controls = [
        (f"{tok.field}:", token_description(tok, ai=ai))
        for tok in token_fields(ai=ai) if tok.control
    ]
    heading_key = (
        "viewer.post_grid.filter_help_controls_heading" if ai
        else "viewer.post_grid.filter_help_controls_heading_free"
    )
    footer_key = (
        "viewer.post_grid.filter_help_footer" if ai
        else "viewer.post_grid.filter_help_footer_free"
    )
    return (
        t("viewer.post_grid.filter_help_syntax_html")
        + t("viewer.post_grid.filter_help_fields_heading")
        + "<br>" + _rows_html(fields)
        + t(heading_key)
        + "<br>" + _rows_html(controls)
        + t(footer_key)
    )


def filter_help_brief_html() -> str:
    """初回フォーカスで自動表示する**短縮版**の構文ヘルプ（N-116）.

    絞り込み欄を初めてクリックしただけで 382×415px の全文パネルが中央
    グリッドに被さっていた。割り込み感の主因は「毎回出ること」ではなく
    「初回に大きすぎること」なので、**表示済みフラグの永続化ではなく減量**
    で解く（永続化は教示機会を恒久的に失う副作用がある）。

    残すのは全モード共通で post.md にも tags.db にも依存しない 3 行
    （AND / ``-除外`` / ``~OR``）だけ。分野限定表・コントロール表・脚注は
    「?」ボタンの :func:`filter_help_html` 側に残り、そこへの誘導と
    **閉じ方**（Esc または入力）を 1 行ずつ添える — 閉じ方が画面のどこにも
    書かれていなかったのが指摘の後半。
    """
    return (
        t("viewer.post_grid.filter_help_syntax_html")
        + t("viewer.post_grid.filter_help_brief_more")
        + t("viewer.post_grid.filter_help_brief_dismiss")
    )


def cheatsheet_axis_rows() -> list[tuple[str, str]]:
    """AI 検索チートシートの「軸の行」= (正式名, 説明) — 台帳由来。

    軸の名称がチップ・フォームラベルと同じ情報源（:func:`label`）から
    出ることが要点（N-99 のラベルばらけの再発防止）。表示単位の説明は
    選択肢列そのものから合成する。
    """
    rows: list[tuple[str, str]] = []
    for dim in DIMENSIONS:
        if dim.cheatsheet_desc_key is not None:
            rows.append((label(dim.id), t(dim.cheatsheet_desc_key)))
        elif dim.id == "ai_unit":
            choices = " / ".join(t(key) for _v, key in dim.value_keys)
            rows.append((label(dim.id), f"{choices}。"))
    return rows


def cheatsheet_html() -> str:
    """AI 検索ポップオーバーの「検索の使い方」本文 HTML.

    語彙対比（投稿タグ vs AIタグ）と 3 モード・関連度の説明は静的
    フラグメントのまま、**軸の行**（AIタグ / AIタグの精度 / 表示単位）を
    台帳から生成する。
    """
    axis_rows = "".join(
        f"<tr><td><b>{html.escape(name)}</b></td>"
        f"<td>{html.escape(desc)}</td></tr>"
        for name, desc in cheatsheet_axis_rows()
    )
    return (
        t("viewer.advanced_search.cheatsheet_title")
        + "<table cellspacing='4'>"
        + t("viewer.advanced_search.cheatsheet_row_post_tags")
        + axis_rows
        + t("viewer.advanced_search.cheatsheet_rows_modes")
        + "</table>"
        # (UIレビュー 2026-08-28 N-148) 2 枚のチートシートは互いを知らない。
        # AI 側は「投稿タグは絞り込み欄の tags: で」と片方向に参照する一方、
        # 種別 / 年齢区分 / 精度 が絞り込み欄の type: / rating: / score: と
        # **等価**であることに一切触れていなかった。
        + t("viewer.advanced_search.cheatsheet_equivalent_tokens")
    )
