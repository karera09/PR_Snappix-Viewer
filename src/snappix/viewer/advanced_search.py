"""AI 検索（AIタグ / 意味 / 類似）のコントローラ.

:class:`AdvancedSearchController` は AI 検索ポップオーバーの席・クエリ状態・
タグ / 意味検索スキャナの投入と着地を持つ ``QObject`` 部品で、``PostGrid`` は
``self._ai = AdvancedSearchController(host=self, …)`` として**所有する**
（継承しない）。

**なぜ部品なのか**: ここはかつて ``AdvancedSearchMixin`` — ``PostGrid`` に
継承される mixin で、ホストの平置き属性を 20 個直読みし、ホストのメソッドを
14 本呼び、さらにライフサイクルが ``__init__`` の分割契約（状態の初期化は
``super().__init__`` の**前**、スキャナの生成は**後**）に依存していた。
双方向に完全結合しているので「mixin はファイル分割の構文糖」でしかなく、
どちらを触るとどちらが壊れるかがコードを全部読むまで分からない。部品にして
ホスト参照を :class:`~.advanced_search_parts.host.SearchHost` の 1 本に
絞ったことで、境界は宣言になり、生成は ``PostGrid.__init__`` の末尾 1 回に
なった。

**読む / 呼ぶ**（ホストの面）: :class:`~.advanced_search_parts.host.SearchHost`
の docstring が一覧を持つ（コントローラはホストの ``_`` 付き属性を触らない）。

分割:

* :mod:`~.advanced_search_parts.query` — クエリの値型と純粋な導出（Qt 非依存）
* :mod:`~.advanced_search_parts.host` — ホスト境界の ``Protocol``
* :mod:`~.advanced_search_parts.popover` — ポップオーバーの席と配線
* :mod:`~.advanced_search_parts.scan` — スキャナの生成 / 投入 / 着地
* :mod:`~.advanced_search_parts.persistence` — 設定往復
* :mod:`~.advanced_search_parts.facade` — ``PostGrid`` に残す薄い委譲

不変条件（散らさないこと）:

* :meth:`AdvancedSearchController.set_query` が**クエリ状態を書く唯一の口**。
  再描画同期（:meth:`_sync_ai_segment` / ホストの条件チップ）はそれを**読む
  だけ**で、可用性による降格は再描画ではなく
  :meth:`_revalidate_ai_mode` という明示の state 操作が行う（索引の差し替えと
  ベクトルクエリの着地 = 答えが変わり得る 2 地点だけが呼ぶ）。
* :meth:`_tag_query_signature` が、ワーカーに効くパラメータを署名へ畳む
  **唯一**の場所。ルートはわざと含めない — ルート変更はスキャナの cancel +
  再キックで表現する。
* :meth:`_land_tag_results` が 4 つの着地スロット（タグ / 意味 × 結果 / 失敗）
  共通の**唯一**の着地経路。着地の**結末**は署名と同時に書かれる
  （``outcome=`` → ``_landed_outcome``）ので、クエリを編集して署名が変われば
  古い結末も自動的に無効になる。
* :meth:`_advanced_phase` が「inactive / searching / landed / error」の 4 値を
  答える**唯一**の式。
* スキャンの投入記録は :class:`~.advanced_search_parts.query.ScanRequest` の
  1 スロット（``_pending``）で、書くのは :func:`scan.kick`（
  :meth:`AdvancedSearchController.set_pending` 経由）だけ。4 着地スロットは
  署名の一致を**読む**だけで降ろさない（次の投入が上書きするのが唯一の更新）
  ので、これは保留値 :mod:`~snappix.viewer.pending` ではなく「飛行中の要求を
  控えるラベル」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import QCompleter, QStyle

from ..common.i18n import t
from . import ai_pack, grid_empty_state
from .advanced_search_parts import persistence, popover, scan
from .advanced_search_parts.host import SearchHost
from .advanced_search_parts.persistence import select_combo_data
from .advanced_search_parts.popover import (
    AI_MODES,
    PrecisionSlider,
    SeedDropArea,
    TagCountDelegate,
)
from .advanced_search_parts.query import (
    AI_MODE_KEYS,
    NEUTRAL_DISPLAY_UNIT,
    RATING_BANDS,
    AiQuery,
    ScanRequest,
    display_unit_key,
    normalize_mode,
    query_mode,
    signature,
    tag_terms_narrow_query,
    with_display_unit,
)
from .dialogs import pick_open_file
from .empty_state import EmptyAction
from .filter_query import _parse_query, _parse_tag_groups, split_tag_terms
from .folder_scan import IMAGE_SUFFIXES, FolderEntry
from .qimage_decode import _QT_READER_LOCK
from .search_dimensions import DEFAULT_TAG_THRESHOLD, chip_text, token_expression
from .search_dimensions import get as _dim_get
from .search_dimensions import value_label as dim_value_label
from .tag_filter import date_matches, preset_range

# 互換の再輸出: 旧 mixin と同居していた小部品は
# :mod:`~.advanced_search_parts.popover` へ移ったが、名前でここを参照する
# 呼び出し側が残っている。
_PrecisionSlider = PrecisionSlider
_SeedDropArea = SeedDropArea
_TagCountDelegate = TagCountDelegate
_RATING_BANDS = RATING_BANDS
_NEUTRAL_DISPLAY_UNIT = NEUTRAL_DISPLAY_UNIT

# 1 打鍵あたりに補完が要求するタグ候補の上限。これは provider の ``LIMIT``
# **かつ**チップ入力の自動チップ化ウィンドウ上限で、両者は一致していなければ
# ならない: 飽和ウィンドウのガードは「ある接頭辞がちょうど ``window_cap`` 件
# 返したら自動チップ化を抑制する」（件数順ウィンドウから、より長い前方一致の
# タグが切り落とされたかもしれないため）ので、provider の LIMIT が cap より
# 大きいとそのガードが黙って効かなくなる。定数 1 本で 3 つの呼び出し側
# （provider / window_cap / 補完リフレッシュ）を歩調させる。
_TAG_SUGGEST_LIMIT = 20

# 0 件カードの緩和のうち、条件チップの × と同じ「1 軸だけを中立化する」もの
# → その × の次元 id（``condition_chips.ACTION_IDS`` の鍵）。
_RELAX_TO_CONDITION_DIMENSION: dict[str, str] = {
    "relax_locked_only": "locked",
    "relax_name_filter": "filter",
    "relax_ai_tags": "ai_tags",
    "relax_threshold": "ai_precision",
    "relax_coverage": "ai_unit",
    "relax_rating": "rating",
    "relax_date": "date",
}


def _memoized_suggest(tag_index):
    """直近 1 件の ``(prefix, limit)`` を覚える ``suggest_tags`` ラッパ.

    1 打鍵はチップ入力の自動チップ化判定と補完リフレッシュの 2 か所から
    **同一の** ``suggest_tags(prefix, limit)`` を GUI スレッドで発行していた。
    両者がこの単一エントリ・メモ化を共有することで 1 打鍵 1 クエリへ戻る。
    キャッシュは 1 件だけ（次の打鍵で必ずキーが変わる）なので、tags.db の
    再読込ごとに :meth:`AdvancedSearchController._install_tag_completer` が
    新リーダーへ束ね直すまで古い結果を保持し続けることもない。``tag_index``
    **だけ**を閉包する（``self`` を握らない — 参照循環の禁止パターン）。
    失敗は従来の両呼び出し側と同じく ``[]`` に劣化する。
    """
    last: list = [None, []]

    def suggest(prefix: str, limit: int) -> list[tuple[str, int]]:
        key = (prefix, limit)
        if last[0] == key:
            return last[1]
        try:
            result = tag_index.suggest_tags(prefix, limit)
        except Exception:  # pragma: no cover (best-effort)
            result = []
        last[0], last[1] = key, result
        return result

    return suggest


class AdvancedSearchController(QObject):
    """AI 検索のクエリ状態・ポップオーバー・スキャナ駆動を持つ部品。

    ライフサイクルは 1 段: ``PostGrid.__init__`` の末尾で 1 回生成すると、
    ポップオーバーを組み、（provider が居れば）スキャナも作る。以後の入口は
    :meth:`refresh_tag_index_ui`（索引の差し替え）/
    :meth:`_advanced_search_cancel` + :meth:`_advanced_search_drop_results`
    （ルート変更・検索解除）/ :meth:`_shutdown_advanced_search`（窓じまい）。
    """

    def __init__(self, host: SearchHost, *, folder_cache) -> None:
        super().__init__(host.widget())
        self.host = host
        # クエリ状態（値型）。書き込み口は :meth:`set_query` 1 本。
        self._query = AiQuery()
        # 投げているスキャン 1 件（``None`` = 投入なし）。書くのは
        # :func:`scan.kick` だけ、読むのは 4 着地スロットだけ。
        self._pending: ScanRequest | None = None
        # 現在のクエリに対するワーカー結果と、それが対応する署名（stale ガード）。
        self._tag_results: list[tuple[FolderEntry, str]] | None = None
        self._tag_results_query: tuple | None = None
        self._tag_rel_paths: dict[str, str] = {}
        # 着地済み / 実行中の結果が ``posted_at`` のシードつきで要求されたか
        # （ワーカーは投稿日フィルタに境界があるときだけ posted_at を解決する）。
        # 後から境界が有効になったとき、日付コンボのスロットが引き直せる。
        self._tag_results_posted_seeded = False
        # グリッドへ実際に載った AI 検索の件数。**ホストが**パイプラインの末端で
        # 1 回だけ書く（:meth:`_apply_advanced_search` の後にビュー次元と NSFW
        # 抑止が重なるため）。ステータス行の「N 件」がこれを読む。0 件カードを
        # 出すかの判定材料**ではない**（それは画面に載ったタイル数で決める）。
        self._advanced_match_count = 0
        # 直近の着地の**結末** — ``"ok"`` / ``"tag_db"`` / ``"vector"`` /
        # ``"engine"``。``_tag_results`` / ``_tag_results_query`` と**同時に**
        # :meth:`_land_tag_results` だけが書く。
        #
        # 以前は「失敗した」が独立した 2 フィールドで、4 着地スロットが呼ぶ前に
        # 自分で立て、成功着地 2 箇所と初期化だけが降ろしていた。降ろす経路が
        # 着地側にしか無いため、失敗の後にクエリを編集すると署名は変わったのに
        # フラグが True のまま残り、空状態が「検索中…」を出さずに無地のグリッド
        # を作っていた。着地記録の属性にしたことで、署名が変わった瞬間
        # （= 未着地）に古い結末も自動的に無効になる。
        self._landed_outcome = "ok"
        # 意味検索の結果は関連度順で届くので並べ直さない、を伝えるフラグ。
        self._tag_results_ranked = False
        # 外部から渡された参照画像プレビュー ``(seed path, pixmap)``。シード
        # 画像がこのペインのタイルでないとき（右ペインの「この画像で類似検索」）
        # に使う。シード行の更新はこのペインのキャッシュから毎回引き直すので、
        # 外部 pixmap を 1 度描くだけでは即座に上書きされてしまう。
        self._similar_seed_pixmap: tuple[str, QPixmap] | None = None
        # 「AIタグ類似検索」が引けるか（:meth:`_rank_mode_available` の memo）。
        # 素の述語はプラグイン側の ``has_tag_vectors`` で、行列ロード前は
        # ``Path.exists()`` 2 発 = GUI スレッドの disk I/O。判定はグリッド再構築
        # のたびに走るので、答えが変わり得る地点（索引差し替え / ベクトルクエリ
        # の着地）でだけ ``None`` へ捨てて引き直す。
        self._rank_mode_cache: bool | None = None
        # 繰り延べ復元（provider / tags.db が点くまで適用を待つ保存値）。
        self._deferred_media_restore: str | None = None
        self._deferred_threshold_restore: float | None = None
        # 遅延生成のトップレベル窓（窓じまいで必ず閉じる）。
        self._tag_browser = None
        self._search_cheatsheet_popup = None
        self._tag_completer: QCompleter | None = None
        self._tag_completer_model = None
        self._tag_count_delegate = None
        self._tag_suggest: Callable | None = None
        popover.build_popover(self)
        scan.create(self, folder_cache)

    # ------------------------------------------------------- クエリ状態

    @property
    def query(self) -> AiQuery:
        """現在のクエリ状態（不変値）。"""
        return self._query

    def set_query(self, query: AiQuery) -> None:
        """クエリ状態を書く**唯一の口**。

        ここを 1 本にしたことで「表示を合わせたつもりの経路がクエリを書き換え
        ていた」が構造的に起こせなくなる。再描画同期は :attr:`query` を読む
        だけで、モードの可用性による降格は :meth:`_revalidate_ai_mode` という
        明示の state 操作だけが行う。
        """
        self._query = query

    def _store_threshold(self, value: float) -> None:
        """精度だけを差し替える（スライダの実値をクエリへ反映する）。"""
        self.set_query(replace(self._query, threshold=float(value)))

    def set_pending(self, request: ScanRequest | None) -> None:
        """投入記録を差し替える。

        production で呼ぶのは :func:`scan.kick` だけ（投入と同時に書く）。
        公開しているのは、スキャナを実際に走らせずに投入だけを模して着地
        ガードを検査するテストのため。
        """
        self._pending = request

    def pending_signature(self, kind: str) -> tuple | None:
        """*kind*（``"tag"`` / ``"vector"``）で投げた署名（無ければ ``None``）。"""
        pending = self._pending
        if pending is None or pending.kind != kind:
            return None
        return pending.signature

    def pending_want_posted_at(self, kind: str) -> bool:
        """*kind* の投入が ``posted_at`` のシードを頼んでいたか。"""
        pending = self._pending
        return bool(
            pending is not None
            and pending.kind == kind
            and pending.want_posted_at
        )

    # ------------------------------------------------------ ライフサイクル

    def _ensure_advanced_search_scanners(self) -> None:
        """provider が居ればスキャナ 2 本を作って配線する（冪等）。"""
        scan.ensure(self)

    def _advanced_search_cancel(self) -> None:
        """デバウンスを止め、**両方**のスキャナを cancel する。"""
        scan.cancel(self)

    def _drop_advanced_search_scanners(self) -> None:
        """スキャナ 2 本を破棄する（:meth:`_ensure_advanced_search_scanners` の対）。"""
        scan.drop(self)

    def _advanced_search_drop_results(self) -> None:
        """着地済みの結果を忘れる。

        rel パスの見出し map とランク済みフラグも道連れにする（残すと、再表示
        された行が古い相対パスの見出しを持ち続け、並べ替えも飛ばされる）。
        """
        self._tag_results = None
        self._tag_results_query = None
        self._tag_rel_paths = {}
        self._tag_results_ranked = False

    def _shutdown_advanced_search(self) -> None:
        """窓じまい（ワーカー + このパネルが開いた全ての窓）。

        ``Qt.Popup`` / モードレスダイアログは**トップレベル窓**なので、ペインを
        閉じてもペインが隠れるだけで彼らは残る。だからこのパネルが画面に出せる
        面は全部ここで閉じる — AI ポップオーバーとタグ補完のドロップダウンも
        含めて（候補が出ている最中にキーボードで窓を閉じると、クリックアウェイ
        の相手が居ないまま画面に残る）。
        """
        self._advanced_search_cancel()
        # モードレスのタグ一覧がペインより長生きしないよう閉じる。
        if self._tag_browser is not None:
            self._tag_browser.close()
            self._tag_browser = None
        if self._search_cheatsheet_popup is not None:
            self._search_cheatsheet_popup.close()
            self._search_cheatsheet_popup = None
        # 参照は残す（再表示できる子ウィジェットで、閉じるのは表示だけ）。
        pop = getattr(self, "ai_popover", None)
        if pop is not None:
            pop.close()
        comp = self._tag_completer
        if comp is not None:
            popup_view = comp.popup()
            if popup_view is not None:
                popup_view.hide()

    # --------------------------------------------------- ポップオーバー

    def open_ai_popover(self, anchor=None) -> None:
        """AI 検索ポップオーバーを *anchor* の下に出す。

        既定のアンカーはツールバーの AIタグ チップ（正規の入口）で、条件バーの
        AI チップは自分自身を渡すので編集対象のチップの隣に開く。素の配布では
        no-op（``ai_pack.available()`` のゲート — ポップオーバーは存在するが
        入口を持たず、決して到達されない）。
        """
        if not ai_pack.available():
            return
        pop = getattr(self, "ai_popover", None)
        if pop is None:
            return
        # 共有条件（フィルター側の適用中の軸）は開くたびに詰め直す — もう
        # 一方のポップオーバーで変えた値をこの面が古いまま見せない。
        self._update_shared_conditions()
        if anchor is None:
            anchor = self.host.ai_chip_anchor()
        pop.adjustSize()
        if anchor is not None:
            from ..common.ui import popover_position

            pop.move(popover_position(anchor, pop.size()))
        pop.show()
        pop.raise_()

    #: 共有条件行に載せる軸 = フィルターポップオーバーが編集する軸のうち、
    #: AI 検索の母集合にも効くもの。「サブフォルダも検索」は AI 経路が読まない
    #: （常に再帰）ので載せない。絞り込み欄そのものは面をまたいで見えている
    #: （ツールバー）ので重複させない。
    _SHARED_CONDITION_DIMS = ("media", "rating", "date", "star", "later",
                              "usertag", "locked")

    def _shared_condition_values(self) -> dict[str, object]:
        """共有条件の現在値 ``{軸 id: 値}``（適用中の軸だけ）.

        値は全て素の状態から読む（ウィジェットの現在テキストではなく）—
        フィルターポップオーバーの行が存在しない構成（``user_meta`` なし /
        素の配布）でも同じコードで正しく空になる。
        """
        host = self.host
        live: dict[str, object] = {
            "media": host.filterbar_media,
            "date": self._query.date_preset,
            "star": host.filterbar_star_min,
            "later": host.filterbar_later,
            "usertag": host.filterbar_user_tag,
            "locked": host.filter_locked_only,
        }
        if ai_pack.available():
            live["rating"] = self._query.rating_key
        out: dict[str, object] = {}
        for dim_id in self._SHARED_CONDITION_DIMS:
            if dim_id not in live:
                continue
            row = _dim_get(dim_id)
            value = live[dim_id]
            if isinstance(value, bool):
                if value:
                    out[dim_id] = value
                continue
            if row is not None and str(value) == str(row.neutral):
                continue
            if not value:
                continue
            out[dim_id] = value
        return out

    def _update_shared_conditions(self) -> None:
        """共有条件チップ + 構文プレビューの 2 行を詰め直す.

        「読み取り専用」なのが要点 — 同じ軸を 2 面で編集できると、どちらが
        効いているのかが読めなくなる。編集はフィルターポップオーバー 1 箇所で、
        AI 側は**今どんな条件と AND されるのか**を見せるだけ。どちらも値が無い
        ときは行ごと消える（空の見出しを残さない）。
        """
        line = getattr(self, "tag_shared_conditions", None)
        syntax = getattr(self, "tag_shared_syntax", None)
        if line is None or syntax is None:  # pragma: no cover (構築途中)
            return
        values = self._shared_condition_values()
        chips: list[str] = []
        for dim_id, value in values.items():
            if dim_id == "star":
                chips.append(chip_text("star", n=int(value)))
            elif dim_id in ("later", "locked"):
                chips.append(chip_text(dim_id))
            elif dim_id == "usertag":
                chips.append(chip_text("usertag", value=str(value)))
            else:
                shown = dim_value_label(dim_id, str(value))
                if shown is None:  # pragma: no cover (中立値は既に落ちている)
                    continue
                chips.append(chip_text(dim_id, value=shown))
        if chips:
            line.setText(t(
                "viewer.advanced_search.shared_conditions",
                items=t("common.sep.middot").join(chips),
            ))
        line.setVisible(bool(chips))
        expr = token_expression(values)
        if expr:
            syntax.setText(t("viewer.advanced_search.shared_syntax", expr=expr))
        syntax.setVisible(bool(expr))

    # ------------------------------------------------------------ 補完

    def _install_tag_completer(self) -> None:
        """``tag_stats`` を源にしたタグ名補完を入力欄へ束ねる.

        チップ入力の末尾エディタは常に 1 トークンしか持たないので、素の
        ``QCompleter`` が「いま打っている語」に正しく一致する。候補を選ぶと
        裸のタグが入り、空白 / Enter がチップへ確定させる。
        """
        # tags.db の再読み込みごとに再入する（ファイル監視はタガーのスキャンの
        # たびに再読み込みする）。前回の設置を先に解く: disconnect しないと
        # 再読み込みのたびに ``textEdited`` 接続が 1 本ずつ積み上がり（N 回
        # 再読み込み後は 1 打鍵あたり N+1 回の sqlite クエリとモデル再構築）、
        # 旧 QCompleter とデリゲートが ``tag_input`` の未破棄の子として溜まる。
        old = self._tag_completer
        if old is not None:
            try:
                self.tag_input.textEdited.disconnect(self._refresh_tag_completer)
            except (RuntimeError, TypeError):  # pragma: no cover (defensive)
                pass
            old.deleteLater()
        completer = QCompleter(self.tag_input)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self._tag_completer = completer
        # 候補モデルは completer ごとに 1 つだけ作り、打鍵ごとには**行を詰め
        # 直す**。以前は打鍵のたびに新しいモデルを ``setModel`` していたため、
        # 旧モデルが completer の子として残り、tags.db を再読み込みするまで
        # セッション中ずっと積み上がっていた。
        from PySide6.QtGui import QStandardItemModel

        self._tag_completer_model = QStandardItemModel(completer)
        completer.setModel(self._tag_completer_model)
        self.tag_input.setCompleter(completer)
        # ポップアップはタグごとの存在件数を右に出すが、モデルが持つのはタグ名
        # だけなので、候補を選んでも件数が入力欄へ入ることはない。
        self._tag_count_delegate = TagCountDelegate(completer)
        popup = completer.popup()
        if popup is not None:
            popup.setItemDelegate(self._tag_count_delegate)
        # 打っている最中の語で候補を引き直す。
        self.tag_input.textEdited.connect(self._refresh_tag_completer)
        # 完全一致かつ唯一のときだけ自動でチップ化させるための provider。
        # provider は ``tag_index`` **だけ**を閉包する（``self`` を握らない）—
        # PostGrid ↔ 子ウィジェットの参照循環を作ると、グリッド（と
        # その QThreadPool / ベクトル索引）が破棄後も生き残る。
        tag_index = self.host.tag_index
        suggest = _memoized_suggest(tag_index)
        self._tag_suggest = suggest

        def _provider(prefix: str) -> list[tuple[str, int]]:
            if tag_index is None:
                return []
            return suggest(prefix, _TAG_SUGGEST_LIMIT)

        # 自動チップ化のウィンドウ上限を provider と同じ LIMIT に束ねる
        # （飽和ウィンドウのガードが成立し続けるため — _TAG_SUGGEST_LIMIT 参照）。
        self.tag_input.set_suggestion_provider(
            _provider, window_cap=_TAG_SUGGEST_LIMIT
        )

    def _ensure_tag_completer_model(self, comp):
        """補完候補を載せる唯一のモデル（無ければ作って束ねる）."""
        model = self._tag_completer_model
        if model is None:  # pragma: no cover (completer wired without install)
            from PySide6.QtGui import QStandardItemModel

            model = QStandardItemModel(comp)
            self._tag_completer_model = model
            comp.setModel(model)
        return model

    def _refresh_tag_completer(self, text: str) -> None:
        comp = self._tag_completer
        if comp is None or self.host.tag_index is None:
            return
        # チップ入力の末尾エディタは既に 1 トークンしか持たないので *text* が
        # その語だが、念のため末尾の空白区切り片を取り、先頭の ``-`` 除外 /
        # ``~`` OR マーカーを剥がしてから照合する（剥がさないと
        # ``suggest_tags`` が生の ``~cat`` を見て何も返さない）。
        term = text.split(" ")[-1]
        if term[:1] in ("-", "~"):
            term = term[1:]
        model = self._ensure_tag_completer_model(comp)
        if len(term) < 2:
            # 候補なしは「モデルを外す」ではなく「行を空にする」で表す。
            model.removeRows(0, model.rowCount())
            popup = comp.popup()
            if popup is not None:
                popup.hide()
            return
        # 直前の自動チップ化判定と同じクエリはメモ化で共有される（飽和
        # ウィンドウ判定は同一の結果リストを読むので成立したまま）。
        suggest = self._tag_suggest
        if suggest is not None:
            suggestions = suggest(term, _TAG_SUGGEST_LIMIT)
        else:  # pragma: no cover (completer wired without install — defensive)
            try:
                suggestions = self.host.tag_index.suggest_tags(
                    term, _TAG_SUGGEST_LIMIT
                )
            except Exception:
                suggestions = []
        # モデルはタグ名だけを持つ（照合と挿入される補完が常にタグそのものに
        # なる）。件数は描画のためだけに ``Qt.UserRole`` へ相乗りする。
        # ``suggest_tags`` は既に ``cnt DESC`` 順なので、よく使うタグが先に出る。
        from PySide6.QtGui import QStandardItem

        model.removeRows(0, model.rowCount())
        for tag, cnt in suggestions:
            item = QStandardItem(tag)
            item.setData(int(cnt), Qt.UserRole)
            model.appendRow(item)
        self._size_completer_popup(suggestions)
        # 内側の QLineEdit は completer の completionPrefix を自分の**生の**
        # テキスト（``-cat`` / ``~cat``）に同期させ続ける。MatchContains は
        # 候補 ``cat`` を ``-cat`` と照合して全行を隠してしまうので、剥がした
        # 語を prefix に据えて、作り直したモデルが実際に一致するようにする。
        comp.setCompletionPrefix(term)
        # モデルが変わったので再表示する（QLineEdit は この textEdited が
        # 発火した時点で既に**前の**モデルに対して complete() を走らせている）。
        comp.complete()

    #: 1 つの異常に長いタグが画面を横断しないためのポップアップ幅上限。
    _COMPLETER_POPUP_MAX_W = 640

    def _size_completer_popup(self, suggestions: list) -> None:
        """最長タグ + 件数が収まるようポップアップを広げる.

        ``QCompleter`` はポップアップを「アンカーしている行編集の幅」に合わせる
        が、チップ入力のインラインエディタはかなり狭くなり得るので、長いタグが
        件数の溝の下で省略されていた。``QCompleterPrivate::showPopup`` は
        ``setGeometry`` で配置し、これはポップアップの最小幅を尊重するので、
        リフレッシュごとに ``minimumWidth`` を上げ下げするのが正規の梃子。
        """
        comp = self._tag_completer
        popup = comp.popup() if comp is not None else None
        if popup is None or not suggestions:
            return
        fm = QFontMetrics(popup.font())
        margin = TagCountDelegate._COUNT_MARGIN
        widest = max(
            fm.horizontalAdvance(str(tag))
            + fm.horizontalAdvance(f"{int(cnt):,}") + 2 * margin
            for tag, cnt in suggestions
        )
        # 枠 + スクロールバー + アイテムビュー自身のテキストマージン。
        extra = (
            2 * popup.frameWidth()
            + popup.verticalScrollBar().sizeHint().width()
            + 16
        )
        popup.setMinimumWidth(min(widest + extra, self._COMPLETER_POPUP_MAX_W))

    # ------------------------------------------------------- 可否と文言

    @staticmethod
    def _set_focusable_enabled(widget, enabled: bool) -> None:
        """*widget* の有効 / 無効を切り替え、無効なら Tab チェーンからも外す.

        操作できない灰色のコントロールが Tab の停留点であるべきではない。
        最初に無効化するとき、設計時のフォーカスポリシーを退避して再有効化で
        戻す。
        """
        if widget is None:
            return
        if not enabled:
            if not hasattr(widget, "_saved_focus_policy"):
                widget._saved_focus_policy = widget.focusPolicy()
            widget.setEnabled(False)
            widget.setFocusPolicy(Qt.NoFocus)
        else:
            widget.setEnabled(True)
            saved = getattr(widget, "_saved_focus_policy", None)
            if saved is not None:
                widget.setFocusPolicy(saved)

    def _update_tag_controls_enabled(self) -> None:
        """tags.db がある限り AIタグ系コントロールを有効にする.

        起動用のチェックボックスは無い: タグ入力 / 精度 / 年齢区分 / 一覧は
        tags.db がある瞬間から使え、チップや年齢区分がクエリを武装させる。
        DB が無ければ無効化し、Tab チェーンからも落とす。表示単位コンボの
        カバレッジ項目だけは :meth:`_update_display_unit_combo` が別に管理する。
        """
        has_db = self.host.tag_index is not None
        widgets = (
            getattr(self, "tag_input", None),
            getattr(self, "tag_threshold_slider", None),
            self.host.rating_combo,
            getattr(self, "tag_browse_btn", None),
        )
        for widget in widgets:
            self._set_focusable_enabled(widget, has_db)

    def _panel_title_text(self, has_index: bool) -> str:
        """ポップオーバーの太字見出し — AI の軸が inert な*理由*を名乗る.

        見出しとその下のバナーは同じシームを語るので、同じ判定で分岐する:
        「要スキャン」が真なのはエンジンが居てまだ ``tags.db`` が無いときだけ。
        エンジンが居ないならスキャン自体が不可能（スキャナは読み込みに失敗した
        まさにそのプラグインに入っている）なので、実行できないツールへ案内する
        代わりにその旨を言う。
        """
        if has_index:
            return t("viewer.advanced_search.panel_title")
        if self._engine_is_missing():
            return t("viewer.advanced_search.panel_title_no_engine")
        return t("viewer.advanced_search.panel_title_needs_scan")

    def _missing_db_banner_text(self) -> str:
        """使える ``tags.db`` が開けていない間に出るインラインバナー.

        判定は 3 つで、いずれも :mod:`~.ai_pack` が最後に索引を開いたときに
        記録した状態から読む:

        * 壊れていて読めない — かつては「まだ作られていない」と 1 つの文言を
          共有し、スキャンを勧めていた: 壊れたファイルには無意味な助言で、
          しかも破損そのものを隠していた。
        * エンジンが居ない（provider 未登録 / パックのエンジン import 失敗）—
          スキャナは同じプラグインなので「タガーでスキャンしてください」は
          ユーザーが従えない助言になる。
        * まだ作られていない — 残りのケース（元の文言）。
        """
        if self._tag_db_is_broken():
            return t("viewer.advanced_search.broken_db_banner")
        if self._engine_is_missing():
            return t("viewer.advanced_search.error_hint_engine")
        return t("viewer.advanced_search.missing_db_banner")

    def _sync_missing_db_banner(self) -> None:
        """バナーの文言と、その下の再読み込みボタンの可否を引き直す.

        ⚠ カードの規則（:meth:`_advanced_error_actions`）は「エンジンのシームに
        ［AIタグDBを再読み込み］は出さない」— 居ない provider 越しに
        ``tags.db`` を開き直すので、ファイルが在っても後続のトーストが
        「tags.db が見つかりません」と主張してしまう。バナーは同じボタンを
        持つので同じ規則に従う（2 つの面は 1 つの決定）。
        """
        banner = getattr(self, "tag_missing_banner", None)
        if banner is not None:
            banner.setText(self._missing_db_banner_text())
        btn = getattr(self, "tag_reload_btn", None)
        if btn is not None:
            btn.setVisible(not self._engine_is_missing())

    def _on_reload_tag_db_clicked(self) -> None:
        """``tags.db`` の再オープンをホストへ頼む（「再読み込み」ボタン）."""
        self.host.request_tag_db_reload()

    def refresh_tag_index_ui(self, tag_index, vector_index) -> None:
        """開き直した索引をパネルへ向け直す.

        ホストが ``tags.db`` を開き直した後（再読み込みボタン / ファイル監視）に
        呼ばれ、ビューアを再起動せずに新しい — あるいは差し替わった — データ
        ベースを拾う。スキャナを向け直し、初回構築が当てた has-index /
        has-vectors のゲートを全部当て直し（タイトル / バナー / 可否 / 補完 /
        精度の下限 / セグメント / プレースホルダ）、モード表示を同期する。
        実行中のスキャンの cancel と stale 結果の破棄は呼び出し側が済ませている。
        """
        self.host.set_search_indexes(tag_index, vector_index)
        # 索引が差し替わった = ``has_tag_vectors`` の答えが変わり得る。
        self._rank_mode_cache = None
        # 古いリーダーに対して開かれたモードレスのタグ一覧は、閉じた
        # ``TagIndex`` を問い続けることになる — ここで落とし、次の「タグ一覧…」
        # が新しいリーダーで組み直す。
        if self._tag_browser is not None:
            self._tag_browser.close()
            self._tag_browser.deleteLater()
            self._tag_browser = None
        # provider が取り下げられた（セッション中にプラグインが無効化された）:
        # スキャナを落として、後の再有効化が新しく読み込まれたプラグイン
        # モジュールから作り直せるようにする（ホストは再有効化時にプラグイン
        # モジュールを purge するので、**古い**モジュール由来のインスタンスを
        # 抱えたままではその契約が破れる）。
        if ai_pack.provider() is None and self._tag_scanner is not None:
            self._drop_advanced_search_scanners()
        # 遅れて provider が登録された場合（AI プラグインの activate が
        # このウィンドウの構築後）も、この再読み込み経路がパネルへ届く。
        self._ensure_advanced_search_scanners()
        # 以後のリクエストは新しいリーダーを引かなければならない。公開された
        # ``set_index`` 契約で向け直す（プラグインの private 属性を突くと、
        # プラグインが名前を変えた瞬間に再読み込みが黙って no-op になる）。
        # ``set_index`` を持たないスキャナ（古いプラグインビルド）は、更新済みの
        # 索引に対して作り直す — provider 取り下げ分岐と同じ破棄シーム。
        scanner = self._tag_scanner
        vscanner = self._vector_scanner
        try:
            if scanner is not None:
                set_tag = getattr(scanner, "set_index", None)
                set_vec = (
                    getattr(vscanner, "set_index", None)
                    if vscanner is not None else None
                )
                if set_tag is None or (vscanner is not None and set_vec is None):
                    self._drop_advanced_search_scanners()
                    self._ensure_advanced_search_scanners()
                else:
                    set_tag(tag_index)
                    if set_vec is not None:
                        set_vec(vector_index)
        except Exception:  # pragma: no cover (defensive)
            # プラグイン側の再ポイントが死んでもこのリロードを中断しない
            # （後続のバナー / タイトル更新へ必ず到達し、スキャナ不在の劣化
            # シームに落とす）。
            from loguru import logger

            logger.exception(
                "AI scanner re-point failed — エンジン不在の劣化シームへ落とします"
            )
            self._drop_advanced_search_scanners()
        has_index = tag_index is not None
        self.ai_popover_title.setText(self._panel_title_text(has_index))
        widget = getattr(self, "tag_missing_widget", None)
        if widget is not None:
            widget.setVisible(not has_index)
        # 再読み込みのたびに「不在」/「破損」/「エンジン不在」を引き直す —
        # 壊れた tags.db を再スキャン案内で放置しない。
        self._sync_missing_db_banner()
        if has_index:
            floor = 0.0
            try:
                floor = float(tag_index.recorded_floor())
            except Exception:  # pragma: no cover (best-effort)
                floor = 0.0
            # signals を止めて広げる: floor が現在値より大きいと内側 QSlider が
            # 値をクランプして valueChanged を発火し、ハンドラが先頭で繰り延べ
            # 復元を破棄してしまう。結果、保存精度が floor まで落ち、保存側が
            # その値を書き戻すので次回起動用の保存値まで恒久的に失われる。
            self.tag_threshold_slider.blockSignals(True)
            self.tag_threshold_slider.setRange(max(0.0, floor), 1.0)
            self.tag_threshold_slider.blockSignals(False)
            self.tag_threshold_slider.setToolTip(
                t("viewer.advanced_search.precision_tooltip", floor=floor)
            )
            # 補完 + 自動チップ provider を新しいリーダーへ束ね直す（provider は
            # ``tag_index`` を閉包するので、古いままでは旧語彙を補完する）。
            self._install_tag_completer()
        # 起動時に provider 未登録で繰り延べた保存設定（メディア種別・精度）を
        # 点灯のこのタイミングで適用する（本番の起動順ではウィンドウ構築時点で
        # provider が居ないため、ここが保存値が実際に効く最初の機会。ユーザーが
        # 既に触っていれば繰り延べはハンドラ側で破棄済みなので上書きしない）。
        if ai_pack.provider() is not None:
            deferred_media = self._deferred_media_restore
            if deferred_media is not None:
                self._deferred_media_restore = None
                select_combo_data(self.tag_media_combo, deferred_media)
                self.set_query(replace(
                    self._query,
                    media_type=self.tag_media_combo.currentData() or "all",
                ))
        if has_index:
            deferred_thr = self._deferred_threshold_restore
            if deferred_thr is not None:
                self._deferred_threshold_restore = None
                self.tag_threshold_slider.blockSignals(True)
                self.tag_threshold_slider.setValue(
                    max(deferred_thr, self.tag_threshold_slider.minimum())
                )
                self.tag_threshold_slider.blockSignals(False)
            # 上の setRange による floor クランプでもスライダは動き得るのに
            # signals を止めた分ハンドラ経由の同期が走らない — クエリが読む
            # 精度を実値へ揃える。
            self._store_threshold(self.tag_threshold_slider.value())
        self.tag_input.setPlaceholderText(self._dynamic_tag_placeholder())
        self._refresh_tag_examples_hint()
        self._revalidate_ai_mode()
        self._sync_ai_segment_enabled()
        self._update_tag_controls_enabled()
        self._update_display_unit_combo()
        self._update_similar_button_state()
        self._update_ai_mode_ui()

    def _dynamic_tag_placeholder(self) -> str:
        """AIタグ入力欄のプレースホルダ.

        ポップオーバーの欄幅で絶対に切れないよう短くする（以前は具体的な
        タグ例を埋め込んでいて、切れると「例: no_h…」と読めた）。例と構文の
        要点は欄の下の muted キャプションへ移した。tags.db が無いときは、
        なぜ使えないのかを代わりに言う。
        """
        if self.host.tag_index is None:
            return t("viewer.advanced_search.placeholder_no_db")
        return t("viewer.advanced_search.placeholder_short")

    def _refresh_tag_examples_hint(self) -> None:
        """AIタグ入力欄の下の例示 / 構文キャプションを更新する.

        tags.db があれば ``tag_stats`` からよく使うタグを具体例として引く
        （``image_tags`` の GROUP BY は GUI スレッドの地雷なので使わない）。
        ``tag_stats`` が未構築なら ``[]`` が返るので構文の要点へ落とす。
        tags.db が無いときは行ごと隠す（欄は無効で、不在バナーが理由を語る）。
        """
        hint = getattr(self, "tag_examples_hint", None)
        if hint is None:
            return
        tag_index = self.host.tag_index
        if tag_index is None:
            hint.setText("")
            hint.setVisible(False)
            return
        examples: list[str] = []
        try:
            examples = [tag for tag, _cnt in tag_index.top_tags(limit=3)]
        except Exception:  # pragma: no cover (best-effort)
            examples = []
        if examples:
            hint.setText(t(
                "viewer.advanced_search.examples_hint",
                sample=" ".join(examples),
            ))
        else:
            hint.setText(t("viewer.advanced_search.examples_hint_syntax"))
        hint.setVisible(True)

    def _display_unit_key(self) -> str:
        """表示単位コンボの現在鍵（2 bool から導出）."""
        return display_unit_key(self._query)

    def _update_display_unit_combo(self) -> None:
        """コンボの選択を 2 bool に合わせ、カバレッジ項目をゲートする.

        「フォルダ（別々の画像で全タグ可）」は include タグが 2 群以上ない限り
        結果を変えないので、それ未満では理由つきで無効化する。単一の項目の
        無効化はモデル item のフラグ + ツールチップで行う。
        """
        combo = getattr(self, "tag_display_unit_combo", None)
        if combo is None:
            return
        select_combo_data(combo, self._display_unit_key())
        # カバレッジに意味があるのは include **群**が 2 つ以上のときだけ —
        # OR 群は 1 つと数えるので ``~a ~b`` だけでは有効にならない。
        groups, _excludes = _parse_tag_groups(self._query.text)
        cov_ok = len(groups) >= 2
        model = combo.model()
        # 添字ではなく鍵で引く — 選択肢は台帳由来で、並びが変われば添字 2 は
        # 別の選択肢を指す。
        idx = combo.findData(NEUTRAL_DISPLAY_UNIT)
        item = model.item(idx) if model is not None and idx >= 0 else None
        if item is not None:
            flags = item.flags()
            if cov_ok:
                item.setFlags(flags | Qt.ItemIsEnabled)
                item.setToolTip("")
            else:
                item.setFlags(flags & ~Qt.ItemIsEnabled)
                item.setToolTip(
                    t("viewer.advanced_search.coverage_disabled_tooltip")
                )
        note = getattr(self, "tag_display_unit_note", None)
        if note is not None:
            # 灰色の選択肢が生まれている間だけ理由を常設で出す。
            note.setVisible(not cov_ok)

    def _update_similar_button_state(self) -> None:
        """ベクトルがある限り「画像を選ぶ…」を有効にする（無ければ理由つきで無効）.

        このボタンはファイル選択を開くだけで現在の選択に依存しないので、可否は
        「ベクトルが在るか」だけ。何をするかは構築時の静的ツールチップが語る。
        """
        btn = getattr(self, "tag_similar_btn", None)
        if btn is None:
            return
        if self.host.vector_index is None:
            self._set_focusable_enabled(btn, False)
            btn.setToolTip(t("viewer.advanced_search.no_vectors_short"))
            return
        self._set_focusable_enabled(btn, True)
        btn.setToolTip(t("viewer.advanced_search.pick_image_tooltip"))

    def _update_relevance_legend(self) -> None:
        """◆ の凡例はランク済み結果が画面に出ている間だけ出す."""
        legend = getattr(self, "tag_relevance_legend", None)
        if legend is not None:
            legend.setVisible(bool(self._tag_results_ranked))

    def _update_reset_link(self) -> None:
        """「詳細条件のみリセット」は AI の軸が効いているときだけ出す."""
        link = getattr(self, "tag_reset_link", None)
        if link is not None:
            link.setVisible(self._advanced_only_engaged())

    def _advanced_only_engaged(self) -> bool:
        """AI パネル側の軸が 1 つでも非中立か（絞り込み欄・検索範囲は数えない）.

        軸の集合は**ビュー次元表**から採る（台帳の ``panel_reset`` 列 1 か所が
        宣言し、リセットと同じ行集合を読む）。以前はここが 6 軸の手書きで、
        リセット側は別の 6 軸、0 件カードの緩和は 9 軸…と面ごとに数え直して
        いたため、精度と表示単位だけが落ちる同型欠陥が繰り返し出ていた。

        「中立か」は「効いているか」の裏返しとは限らない: 精度・表示単位は
        AIタグ検索中でなければチップは点かないが、値は非既定のまま次の検索へ
        持ち越されるので、リンクは出し続ける。
        """
        return any(
            not dim.is_neutral() for dim in self.host.panel_reset_dimensions()
        )

    def _on_reset_advanced_only(self) -> None:
        """AI パネルの全次元をリセットする（絞り込み欄と検索範囲は残す）.

        実装は**表駆動**: ビュー次元表の ``panel_reset`` 行を順に ``clear()``
        する。各 ``clear`` は条件チップの × と同じ実装なので、コントロール
        トークン（``rating:`` / ``score:`` / ``type:``）の除去も、コンボの
        アクセント同期も、その軸の正規手段が 1 実装で面倒を見る — ここで手書き
        し直さない。engaged で絞らず**全行**を呼ぶのは、精度・表示単位が
        「非既定なのにチップは点かない」状態を取り得るため。
        """
        host = self.host
        host.preserve_selection_for_rebuild(ancestor_fallback=True)
        with host.batched_condition_clear():
            for dim in host.panel_reset_dimensions():
                dim.clear()
        # 年齢区分 / 投稿日 の席はフィルターポップオーバー側なので、アクセント
        # 同期もここで踏む（チップは再構築で消えるのにコンボだけ「効いている」
        # 表示が残っていた）。
        host.update_filter_bar()
        self._update_tag_controls_enabled()
        self._update_display_unit_combo()
        self._update_similar_clear_enabled()
        self._maybe_start_tag_scan()
        host.rebuild_grid()

    def focus_tag_search(self) -> None:
        """AI 検索ポップオーバーを開いて AIタグ入力欄へフォーカスする.

        ツールバーの AIタグ チップ、Ctrl+Shift+T、検索メニューの項目が全部
        ここへ着地する。
        """
        self.open_ai_popover()
        tag_input = getattr(self, "tag_input", None)
        if tag_input is not None and tag_input.isEnabled():
            tag_input.setFocus(Qt.ShortcutFocusReason)

    def _show_search_cheatsheet(self) -> None:
        """1 画面の検索チートシートを開く / 閉じる."""
        popover.show_cheatsheet(self)

    # -------------------------------------------------- 3 択モード（AI）

    #: セグメントの台帳（ラベル / ツールチップ）。
    _AI_MODES = AI_MODES
    #: 3 択モードの鍵（``_AI_MODES`` と同じ順序・同じ値）。
    _AI_MODE_KEYS = AI_MODE_KEYS

    def _build_ai_segment(self, body) -> None:
        """排他セグメントを組む（席の実体は :mod:`~.advanced_search_parts.popover`）."""
        popover.build_mode_segment(self, body)

    def _revalidate_ai_mode(self) -> bool:
        """保持しているモードを**可用性で検証し直す**（state の操作）.

        呼ぶのは答えが変わり得る地点だけ: 索引の差し替え
        (:meth:`refresh_tag_index_ui`) とベクトルクエリの着地
        (:meth:`_regate_after_vector_load`) の 2 つで、どちらも直前に
        ``_rank_mode_cache`` を捨てる。**再描画からは呼ばない** — かつては
        表示同期がクエリ状態を書き換える経路になっており、
        「セグメントを暗くするだけのつもり」が署名を動かし、誰も引き直さない
        まま「検索中…」に固着する形を作っていた。

        戻り値は**保持値が実際に変わったか**。モードは署名そのものを変えるので、
        降格を知れるのは呼び出し側が結果を引き直す唯一の手がかりになる。
        """
        return self._set_ai_mode(self._query.mode)

    def _sync_ai_segment_enabled(self) -> None:
        """類似セグメントの可否を現在のベクトル索引に合わせる（表示だけ）.

        「AIタグ類似検索」も「類似画像検索」も ``image_vectors`` を要る。前者は
        さらにタグ→ベクトル行列（``tag_vectors_f16.npy`` / ``tag_names.json``）
        を要り、画像ベクトルがあっても欠けていることがある — 黙って何も並ばない
        ので、理由つきで無効化する。

        **この関数はクエリ状態を書かない**（降格は :meth:`_revalidate_ai_mode`
        の仕事）。グリッド再構築のたびに走る表示同期だからで、ここでスキャンや
        再構築を始めることも無い。
        """
        buttons = getattr(self, "_ai_mode_buttons", None)
        if not buttons:
            return
        has_vec = self.host.vector_index is not None
        # インラインの「なぜ暗いのか」— tags.db はあるがベクトル索引がまるごと
        # 無いとき（意味検索 2 つが両方暗い）だけ出す。部分的なケース（画像
        # ベクトルはあるが行列が無い）は類似検索が使えるので、代わりにボタン
        # ごとのツールチップが理由を持つ。
        hint = getattr(self, "tag_no_vectors_hint", None)
        if hint is not None:
            hint.setVisible(self.host.tag_index is not None and not has_vec)
        # 有効時は**元の説明**を明示的に戻す: 空文字を書くと、構築時に付けた
        # 説明がこのゲートの初回実行で消えたまま復元されず、紛らわしい 2 つの
        # 「類似」だけが無説明になる。「無効なら理由 / 有効なら本来の説明」の
        # 形は :meth:`_update_similar_button_state` と同じ。
        tooltips = {key: tip for key, _label, tip in self._AI_MODES}
        for key in ("rank", "similar"):
            btn = buttons.get(key)
            if btn is None:
                continue
            btn.setEnabled(has_vec)
            btn.setToolTip(
                t(tooltips[key]) if has_vec
                else t("viewer.advanced_search.no_vectors_short")
            )
        if has_vec and not self._rank_mode_available():
            rank = buttons.get("rank")
            if rank is not None:
                rank.setEnabled(False)
                rank.setToolTip(t("viewer.advanced_search.no_vectors_short"))

    def _regate_after_vector_load(self) -> None:
        """ベクトルクエリが着地した後にモードを検証し直す.

        ``has_tag_vectors`` は行列が実際に読まれるまで**ファイルプローブ**で
        答えるので、ベクトルの着地（結果でも失敗でも）はその答えが反転し得る
        瞬間。memo を捨てるのはこの経路ではここだけ。

        降格は ``_query_mode`` の分岐を変える = クエリの**署名**を変えるので、
        たった今着地した結果はもう現在のクエリを説明しない。セグメントを暗く
        するだけでは「詳細検索中…」のまま蹴り直す者が居なくなるので、降格は
        明示のセグメント操作と同じ後始末（引き直し + 再構築）を踏む。そして
        それは着地の**後**に走るので、着地そのものは語られる。
        """
        self._rank_mode_cache = None
        demoted = self._revalidate_ai_mode()
        self._sync_ai_segment_enabled()
        if not demoted:
            return
        self._update_similar_clear_enabled()
        self._update_tag_controls_enabled()
        self._update_ai_mode_ui()
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _set_ai_mode(self, key: str) -> bool:
        """AI モードを書く**唯一の口** — ここで可用性を 1 回だけ検証する.

        検証を書き込み側へ集約したことで、「押せないモードが選択状態になる」
        経路が構造的に消える: 従来は表示導出・表示同期・入力ハンドラの 3 者が
        互いを補正し合っていたので、どれか 1 つが取りこぼした組み合わせ
        （ベクトル索引はあるがタグ→ベクトル行列が無い環境の ``rank`` 等）で
        セグメント・本文・実クエリが別々のモードを名乗っていた。

        倒し先はいずれも ``"and"``（判定は
        :func:`~.advanced_search_parts.query.normalize_mode`）。``"similar"``
        以外へ移るときは参照画像も落とす: シードは similar モードのデータで
        あって、他モードでは表示も検索もされない。

        戻り値は**保持値が実際に変わったか**。
        """
        resolved = normalize_mode(
            key,
            has_vectors=self.host.vector_index is not None,
            rank_available=self._rank_mode_available(),
        )
        query = self._query
        changed = resolved != query.mode
        if resolved != "similar":
            # モードとシードは対（片方だけ落とすと、外部プレビューの QPixmap が
            # 窓の寿命ぶん常駐する）。
            query = replace(query, similar_seed=None)
            self._similar_seed_pixmap = None
        self.set_query(replace(query, mode=resolved))
        return changed

    def _clear_similar_seed(self) -> None:
        """参照画像を落とす**唯一の口** — シードと外部プレビューは対."""
        self.set_query(replace(self._query, similar_seed=None))
        self._similar_seed_pixmap = None

    def _rank_mode_available(self) -> bool:
        """「AIタグ類似検索」が実際に並べられるか.

        ``image_vectors`` **と**タグ→ベクトル行列が要る。後者が無いと
        ``rank_by_tags`` は空リストを返す（ファイル不在）か例外を投げる
        （ファイルはあるが使えない）ので、セグメントは無効化され、自動の
        フォールバック先にもならない。``getattr`` は素のスタブ索引（テスト）を
        従来どおり「使える」側に残す。

        答えは memo する: ``has_tag_vectors`` は行列ロード前はファイル 2 本の
        存在確認で、この述語はグリッド再構築のたびに評価されるため、GUI
        スレッドの stat がフォルダを開くたびに積み上がっていた。捨てるのは
        答えが変わり得る 3 地点だけ（索引差し替え / ベクトル結果・失敗の着地）。
        """
        cached = self._rank_mode_cache
        if cached is not None:
            return cached
        vector_index = self.host.vector_index
        if vector_index is None:
            available = False
        else:
            has_tv = getattr(vector_index, "has_tag_vectors", None)
            available = True if has_tv is None else bool(has_tv())
        self._rank_mode_cache = available
        return available

    def _current_ai_mode(self) -> str:
        """表示するモード鍵 — 保持している ``query.mode`` そのもの.

        唯一の例外が ``"media"``: 「すべて/画像」以外の種別走査は
        :meth:`_query_mode` の最優先分岐で、AI 条件を一切使わない拡張子
        ウォークになる。対応するセグメントが無いので中立化し、理由を注記で語る。
        """
        if self._query_mode() == "media":
            return "media"
        return self._query.mode

    def _on_ai_mode_selected(self, key: str) -> None:
        """セグメントの明示操作を適用する.

        書き込みは :meth:`_set_ai_mode` 1 本（可用性の検証もそこ）。similar は
        既存のシードを保つので、右ペイン発のシードの後に選び直しても落ちない。
        シードが無いときは走査せず空状態の案内（D&D の CTA）を出す。
        """
        if self.host.vector_index is None and key != "and":
            return
        self.host.preserve_selection_for_rebuild(ancestor_fallback=True)
        self._set_ai_mode(key)
        self._update_similar_clear_enabled()
        self._update_tag_controls_enabled()
        self._update_ai_mode_ui()
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _sync_ai_segment(self) -> None:
        """セグメントボタンを現在のモードへ合わせる（**表示だけ**）.

        ``"media"``（種別ウォーク）に対応するセグメントは無い — クエリが AI
        条件を全部無視するので、どれかを点灯させるのは走っていないモードを
        主張することになる。排他グループごと中立化し、理由は注記が語る。

        無効なボタンは決して点灯させない: 保持モードが可用性を失った状態
        （索引が入れ替わった直後など）でも、再描画は state を書き換えずに
        「どれも選ばれていない」を描く — 修復は :meth:`_revalidate_ai_mode` の
        仕事で、それは答えが変わり得る地点からだけ呼ばれる。
        """
        buttons = getattr(self, "_ai_mode_buttons", None)
        if not buttons:
            return
        # 可否を先に更新して、下のボタン状態が**現在の**索引に対して判断される
        # ようにする（高々ファイルプローブ 1 回、行列ロード後は無料）。
        self._sync_ai_segment_enabled()
        mode = self._current_ai_mode()
        btn = buttons.get(mode)
        if btn is not None and not btn.isEnabled():
            btn = None
        self.ai_mode_group.blockSignals(True)
        if btn is not None:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)
        else:  # mode == "media"（対応セグメントなし）/ 押せないモード
            # QButtonGroup(exclusive) は「全解除」を直接許さないので、一時的に
            # exclusive を外して全ボタンを落とす。
            self.ai_mode_group.setExclusive(False)
            for b in buttons.values():
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
            self.ai_mode_group.setExclusive(True)
        self.ai_mode_group.blockSignals(False)
        self._update_ai_mode_ui()

    def _update_ai_mode_ui(self) -> None:
        """セグメントごとの補助コントロールを出し入れする.

        * AIタグ検索 → 精度 + 表示単位を出す。シード行 / 注記は隠す。
        * AIタグ類似検索 → 精度と表示単位を隠し、ランキング種の注記を出す。
        * 類似画像検索 → 精度と表示単位を隠し、「画像を選ぶ…」とシード行
          （CTA つきのドロップ先 / シードのプレビュー）を出す。
        * 種別走査 → タグ / シードのコントロールを隠し、「種別走査中は…
          使われません」の注記を出す。表示単位だけは**出す**: 種別走査の署名も
          リクエストも ``folder_mode`` を運ぶので、結果がフォルダ単位で出るか
          ファイル単位で出るかはこの行が決める。

        可否の基準は**セグメントの見た目ではなく実クエリ**。保持モードが
        rank / similar でも、引くものが無いとき（除外語だけ / シード未設定）は
        クエリが ``"tags"`` へ落ち、精度と表示単位が署名に載って実際に効く —
        見た目だけで隠すと、効いている条件を見ることも変えることもできなくなる。
        """
        mode = self._current_ai_mode()
        qmode = self._query_mode()
        # 「タグとして引いている」= 何も立っていない (None) か tags 分岐。
        tag_query = qmode in (None, "tags")
        is_similar = mode == "similar"
        media_note = getattr(self, "tag_media_note", None)
        if media_note is not None:
            media_note.setVisible(mode == "media")
        for w in (
            getattr(self, "tag_precision_label", None),
            getattr(self, "tag_threshold_slider", None),
        ):
            if w is not None:
                w.setVisible(tag_query)
        unit_row = getattr(self, "tag_display_unit_row", None)
        if unit_row is not None:
            unit_row.setVisible(tag_query or qmode == "media")
        note = getattr(self, "tag_rank_note", None)
        if note is not None:
            note.setVisible(mode == "rank")
        btn = getattr(self, "tag_similar_btn", None)
        if btn is not None:
            btn.setVisible(is_similar)
        # シード行は類似画像検索の間ずっと出る（シードが無くてもドロップ先と
        # して）。中身（CTA かプレビューか）は下で詰める。
        row = getattr(self, "tag_seed_row", None)
        if row is not None:
            row.setVisible(is_similar)
        self._update_similar_seed_display()

    # ----------------------------------------------------- クエリの導出

    def _query_mode(self) -> str | None:
        """いま引く検索源（``None`` = AI 検索は立っていない）."""
        return query_mode(self._query, has_tag_index=self.host.tag_index is not None)

    def _tag_query_signature(self) -> tuple | None:
        """ワーカーに効くパラメータの安定署名（畳むのはここ 1 箇所）."""
        return signature(self._query, self._query_mode())

    def _advanced_search_active(self) -> bool:
        return self._query_mode() is not None

    def _advanced_phase(self) -> str:
        """AI 検索が今どの段にいるか — 4 値の**唯一の判定式**.

        * ``"inactive"`` — クエリが立っていない（署名が ``None``）
        * ``"searching"`` — 現署名の結果がまだ着地していない
        * ``"landed"`` — 現署名で成功着地した
        * ``"error"`` — 現署名で**失敗**着地した（種別は
          :meth:`_landed_error_kind`）

        この 3 値判定はかつて 5 箇所で微妙に違う組み合わせで書かれており、
        グリッド側だけが「着地 **or** 失敗フラグ」と OR で見ていたため、失敗の
        後にレーティングを変えると署名は変わった（＝未着地）のに「検索中…」が
        出ず無地のグリッドになっていた。判定材料を「署名付きの着地記録」1 つに
        寄せ、式もここ 1 本にする。
        """
        sig = self._tag_query_signature()
        if sig is None:
            return "inactive"
        if self._tag_results is None or self._tag_results_query != sig:
            return "searching"
        return "landed" if self._landed_outcome == "ok" else "error"

    def _landed_error_kind(self) -> str:
        """現署名の失敗着地の種別（``"tag_db"`` / ``"vector"`` / ``"engine"``）.

        :meth:`_advanced_phase` が ``"error"`` のときだけ意味を持つ。既定は
        ``"tag_db"``。
        """
        outcome = self._landed_outcome
        return "tag_db" if outcome == "ok" else outcome

    def _current_date_bounds(self) -> tuple[datetime | None, datetime | None]:
        preset = self._query.date_preset
        start = end = None
        if preset == "range":
            d_from = self.host.date_from.date()
            d_to = self.host.date_to.date()
            start = datetime(d_from.year(), d_from.month(), d_from.day())
            end = datetime(d_to.year(), d_to.month(), d_to.day())
        return preset_range(preset, start=start, end=end)

    def _date_bounds_active(self) -> bool:
        """投稿日フィルタに境界が 1 つでもあるか。"""
        lo, hi = self._current_date_bounds()
        return lo is not None or hi is not None

    def _apply_advanced_search(self) -> list[FolderEntry]:
        """現在のタグ / 種別 / 意味クエリの結果からグリッドの中身を組む.

        現署名のワーカークエリがまだ実行中（または何も返さなかった）ときは
        ``[]`` を返すので、古い結果が一瞬映ることがない。GUI 側では順に、
        絞り込み欄の名前フィルタ・投稿日フィルタ・🔒 のみ抑止を当て、最後に
        いつものフォルダ優先ソートをかける。

        **件数はここで書かない**: ここは AI パイプラインの中間で、呼び出し側が
        まだ「advanced」スコープのビュー次元（種別 / ★ / あとで見る /
        ユーザータグ）と NSFW 抑止を上に重ね、実際にグリッドへ到達した件数を
        記録する。ここで書くと、ステータス行が画面から消えたヒットを数える。
        """
        host = self.host
        if (
            self._tag_results is None
            or self._tag_results_query != self._tag_query_signature()
        ):
            return []
        candidates = [entry for entry, _rel in self._tag_results]

        # タグ / 種別 / 意味のヒットは post.md のメタを持たない（タグ / プランは
        # 未読）ので、ここで効くのは素の名前オーバーレイだけ — 分野限定トークン
        # は空の干し草を探すことになる。
        includes, excludes, or_pool = host.general_filter_terms()
        if includes or excludes or or_pool:
            def _match(e: FolderEntry) -> bool:
                rel = self._tag_rel_paths.get(str(e.path), "")
                hay = " ".join([e.path.name, e.title, rel]).casefold()
                if any(exc in hay for exc in excludes):
                    return False
                if not all(inc in hay for inc in includes):
                    return False
                # ``~`` の OR プール: どれか 1 つ当たれば通す。
                return not or_pool or any(alt in hay for alt in or_pool)

            candidates = [e for e in candidates if _match(e)]

        lo, hi = self._current_date_bounds()
        if lo is not None or hi is not None:
            candidates = [
                e for e in candidates if date_matches(e.posted_at, lo, hi)
            ]

        if host.filter_locked_only:
            # タグ / 種別の行は locked_count を持たない（post.md を読んでいない）
            # — 一貫しない結果を見せるより抑止する（再帰の子孫経路と同じ）。
            candidates = []

        # ``#thumb#`` 除外のトグルをファイルモードのヒットに効かせる（フォルダ
        # 結果はトグル込みのプレビューサムネをそのまま保つ）。
        candidates = host.drop_thumb_markers(candidates)

        # 意味検索の結果は関連度順でワーカーから届く（上の GUI 側フィルタは
        # 行を落とすだけなので順序は保たれる）。
        if self._tag_results_ranked:
            return candidates
        return host.sorted_dir_first(candidates)

    def _update_advanced_badge(self) -> None:
        """AI 検索の補助面を再同期する（再構築のチョークポイント）.

        ホストの ``_rebuild_grid``（表示の単一チョークポイント）と
        :meth:`_maybe_start_tag_scan` から呼ばれるので、デバウンスされた結果が
        着地する前でも最初の編集にこれらの面が反応する。
        """
        self._update_reset_link()
        self._update_relevance_legend()
        self._update_similar_button_state()
        # 0 件カードはグリッド側の空状態 1 本が持つ — ここでは触らない。
        # ステータス行の件数を画面の実体と調停する: GUI 側の名前 / 日付
        # フィルタの変更は着地済み結果を絞り直すだけでワーカーを再実行しない
        # ので、着地スロットだけでは「✓ … — N 件」が古いまま残る。
        self._refresh_advanced_status()
        # パンくずの件数の母数合わせ（ホストが自分の件数を書いた**後**に走る）。
        self._refresh_advanced_count_text()
        # 3 択セグメントを現在のモードへ追従させる。
        self._sync_ai_segment()
        # クエリが立った瞬間にツールバーの AIタグ チップを点け、名前 / 本文を
        # 消す（デバウンスされた結果の着地を待たない）。
        self.host.sync_search_mode_chips()

    # ---------------------------------------------- 空状態 / ⚠ カード

    def _advanced_empty_inputs(self) -> grid_empty_state.AdvancedEmptyInput:
        """AI 検索の 0 件 / ⚠ カードを決める観測値を 1 つに束ねる。

        判定そのものは Qt 非依存の :mod:`.grid_empty_state`（純関数）が持つ。
        ここはウィジェットとクエリ状態から**値**を読み出すだけの層で、文言・
        ⚠ の見出し・緩和ボタンの 3 面が同じ 1 束を読む。
        """
        host = self.host
        query = self._query
        filter_text = host.filter_text()
        groups, excludes = _parse_tag_groups(query.text)
        mode = self._query_mode()
        return grid_empty_state.AdvancedEmptyInput(
            locked_only=bool(host.filter_locked_only),
            name_filter_text=filter_text,
            name_filter_engaged=bool(
                filter_text and any(host.general_filter_terms())
            ),
            nsfw_hidden_count=host.nsfw_hidden_count,
            mode=mode,
            tag_terms=query.text,
            tag_terms_narrow=self._tag_terms_narrow_query(mode),
            tag_group_count=len(groups),
            has_excludes=bool(excludes),
            threshold=query.threshold,
            precision_neutral=self._precision_neutral(),
            folder_mode=bool(query.folder_mode),
            coverage_mode=bool(query.coverage_mode),
            rating_key=query.rating_key,
            date_preset=query.date_preset,
            error_kind=self._landed_error_kind(),
            tag_db_broken=self._tag_db_is_broken(),
        )

    def _relaxation_callbacks(self) -> dict[str, Callable[[], None]]:
        """緩和 / ⚠ カードの動作 id → 実際の呼び出し先。

        **不変**: 鍵は :data:`~.grid_empty_state.ADVANCED_ACTION_IDS` と集合
        一致すること（片側だけ増えると「押せるのに何も起きないボタン」が戻る）。

        1 軸だけを中立化する緩和は条件チップの × と同じ操作なので、
        :data:`_RELAX_TO_CONDITION_DIMENSION` 経由でホストの × 実装
        （``clear_condition_dimension``）へ戻す — 手書きの複製を持つと、選択
        保持や「チップが見せている語だけを消す」規律が片側だけずれる。
        ここに実装を持つのは × に対応が無い固有の動作だけ。
        """
        callbacks: dict[str, Callable[[], None]] = {
            action: partial(self.host.clear_condition_dimension, dim_id)
            for action, dim_id in _RELAX_TO_CONDITION_DIMENSION.items()
        }
        callbacks.update({
            "relax_hide_nsfw": self._relax_hide_nsfw,
            "relax_excludes": self._relax_excludes,
            "open_ai_popover": self._relax_open_ai_popover,
            "reload_tag_db": self._on_reload_tag_db_clicked,
        })
        return callbacks

    def _build_empty_actions(
        self, specs: list[grid_empty_state.ActionSpec],
    ) -> list[EmptyAction]:
        """``ActionSpec`` の列 → 実際に押せる :class:`EmptyAction` の列."""
        callbacks = self._relaxation_callbacks()
        return [
            EmptyAction(
                t(spec.label_key, **dict(spec.label_params)),
                callbacks[spec.action],
                t(spec.tooltip_key) if spec.tooltip_key else "",
            )
            for spec in specs
        ]

    def _advanced_empty_message(self, kind: str) -> str:
        """AI 検索の 0 タイル文言（``advanced_zero`` / ``advanced_error``）."""
        return "\n".join(
            t(key)
            for key in grid_empty_state.advanced_message_keys(
                kind, self._advanced_empty_inputs()
            )
        )

    def _advanced_error_texts(self) -> tuple[str, str]:
        """⚠ カードの ``(見出し, 説明)``（キーの選択は純関数側）."""
        title, hint = grid_empty_state.error_text_keys(
            self._advanced_empty_inputs()
        )
        return t(title), t(hint) if hint else ""

    def _advanced_error_actions(self) -> list[EmptyAction]:
        """⚠ カードに載せる操作ボタン."""
        return self._build_empty_actions(
            grid_empty_state.plan_error_actions(self._advanced_empty_inputs())
        )

    @staticmethod
    def _tag_db_is_broken() -> bool:
        """直近の ``tags.db`` オープンが**実在ファイル**で失敗したか.

        かつて ``ai_pack.open_tag_index`` は「ファイルが無い」/「開けたが空」/
        「壊れていて開けない」を単一の ``None`` へ潰していたので、ビューアは
        壊れたデータベースにも「タガーでスキャンしてください」という、決して
        直らない助言を出していた。状態は provider のシームで記録されるように
        なり、``"error"`` が破損の判定（``getattr`` は古い ai_pack を通す）。
        """
        status = getattr(ai_pack, "tag_index_status", None)
        return status is not None and status() == "error"

    @staticmethod
    def _engine_is_missing() -> bool:
        """AI パックは有効なのにエンジンへ到達できないか.

        ``ai_pack`` はそのシームの 2 つの形（provider 未登録 = プラグインが
        activate しなかった / provider はあるが遅延 import が失敗する = 部分
        展開・隔離）をどちらも ``"unavailable"`` として記録する。どちらもスキャン
        や tags.db の再読み込みでは直らないので、それらを勧める面は全部これで
        分岐する。
        """
        if not ai_pack.available():
            return False
        status = getattr(ai_pack, "tag_index_status", None)
        return status is not None and status() == "unavailable"

    def _empty_state_relaxations(self) -> list[EmptyAction]:
        """効いている軸ごとに 1 つ、条件をゆるめるボタン。

        並びの決定は :func:`grid_empty_state.plan_relaxations`（Qt 非依存）。
        ここは観測値を渡し、返ってきた動作 id を押下先（:meth:`_relaxation_callbacks`）
        へ戻すだけ。
        """
        return self._build_empty_actions(
            grid_empty_state.plan_relaxations(self._advanced_empty_inputs())
        )

    def _tag_terms_narrow_query(self, mode: str | None) -> bool:
        """AIタグ欄が *mode* のクエリに実際に効いているか（判定は純関数側）."""
        return tag_terms_narrow_query(self._query, mode)

    def _relax_open_ai_popover(self) -> None:
        # 0 件カードのフォールバック動作 — ``open_ai_popover`` そのものでは
        # なく束縛メソッドにして、Qt の ``clicked(bool)`` 引数が ``anchor``
        # 引数へ着地しないようにする。
        self.open_ai_popover()

    def _precision_neutral(self) -> float:
        """精度の中立点 = ``max(スライダ下限, DEFAULT_TAG_THRESHOLD)``.

        条件チップの点灯判定と × の戻し先が使う基準と同じもの。0 件カードの
        緩和候補・その押下先もここを通す。
        """
        return max(self.tag_threshold_slider.minimum(), DEFAULT_TAG_THRESHOLD)

    def _relax_hide_nsfw(self) -> None:
        # 永続のビュー設定を「隠さない」へ戻す。``set_hide_nsfw`` がメニューの
        # ラジオ同期と再構築まで面倒を見る唯一の入口なので、ここはそれを呼ぶ
        # だけ（他の緩和と同じ「正規の経路で 1 次元だけ中立化する」作法）。
        self.host.set_hide_nsfw("off")

    def _relax_excludes(self) -> None:
        # 除外を**外した** include テキストを組み直す（OR 群は保つ — 複数
        # メンバーの群は ``~`` 付きで再出力）。
        groups, _excludes = _parse_tag_groups(self._query.text)
        tokens: list[str] = []
        for group in groups:
            if len(group) > 1:
                tokens.extend("~" + tok for tok in group)
            else:
                tokens.append(group[0])
        self.tag_input.set_text(" ".join(tokens))

    # ------------------------------------------------------ スキャン駆動

    def _maybe_start_tag_scan(self) -> None:
        """状態を見てワーカーを蹴る（または片付ける）."""
        scan.maybe_start(self)

    def _kick_tag_scan(self) -> None:
        """デバウンスの着地点 — 実際にワーカーへ投げる."""
        scan.kick(self)

    def _advanced_status_text(self) -> str | None:
        """現在の結果に対する「✓ … — N 件」の確定ステータス行.

        「ステータスバーに触るな」を意味する ``None`` を返すのは、件数行が
        間違いになるとき: AI クエリが立っていない / 現署名の結果がまだ着地して
        いない（まだ「詳細検索中…」）/ 直近の着地がスキャナの失敗（失敗の行は
        失敗スロットが持つ）。それ以外では**着地したクエリ**と生きた件数から
        組む（GUI 側の名前 / 日付フィルタとビュー次元 / NSFW を畳んだ後に
        実際にグリッドへ届いた件数）ので、ワーカーを引き直さない絞り込み変更でも
        件数が更新される。
        """
        if self._advanced_phase() != "landed":
            return None
        sig = self._tag_query_signature()
        count = self._advanced_match_count
        if self._tag_results_ranked:
            kind = (
                t("viewer.advanced_search.mode_similar") if sig[1] == "similar"
                else t("viewer.advanced_search.mode_rank")
            )
            return t(
                "viewer.advanced_search.status_result_ranked",
                kind=kind, count=count,
            )
        label = (
            t("viewer.advanced_search.result_kind_tags") if sig[0] == "tags"
            else t("viewer.advanced_search.result_kind_media")
        )
        return t("viewer.advanced_search.status_result", label=label, count=count)

    def _refresh_advanced_status(self) -> None:
        """再構築のチョークポイントで確定した件数行を押し直す.

        ``None`` は「確定した件数の状態ではない」（非活性 / 実行中 / 失敗）を
        意味するのでバーには触らない — 「詳細検索中…」やエラー行が、それぞれの
        持ち主が消すまで生き残る。
        """
        text = self._advanced_status_text()
        if text is not None:
            self.host.set_search_status(text)

    def _refresh_advanced_count_text(self) -> None:
        """AI 経路のパンくず件数に平常ブラウズと同じ母数を与える.

        ホストは平常の絞り込みが直下子を絞ったとき「(N 件中 M 件)」を書くが、
        AI 検索の結果は直下子の部分集合ではないので裸の「(M 件)」に落ちる。
        だが**何かの**部分集合ではある: ワーカーが着地させたヒット集合だ。
        それを母数にすれば「0 件」が両経路で同じに読める — 「9 件のヒットのうち
        名前フィルタを通ったのは 0 件」であって、検索自体が何も見つけられなかった
        ように見える裸の 0 ではなくなる。

        :meth:`_update_advanced_badge` の末尾、つまりホストが自分のテキストを
        書いた後に走り、確定した（着地・非エラー）結果集合を GUI 側の
        オーバーレイが実際に絞ったときだけ手を出す。
        """
        if self._advanced_phase() != "landed":
            return
        results = self._tag_results
        assert results is not None  # "landed" の定義
        host = self.host
        if (
            host.root_or_folder is None
            or not host.breadcrumb_has_trail()
        ):
            return
        shown = host.shown_tile_count()
        if shown is None:
            shown = self._advanced_match_count
        total = len(results)
        if shown >= total:
            return
        # ここはホストが直前に書いた件数表記を**丸ごと**置き換えていたため、
        # ホストが付ける「(… N 件を非表示中)」が消えていた — しかも消えるのは
        # shown < total、つまり数が合わない印がいちばん要る状況。ホストが書いた
        # 「(M 件)」の後ろに続く付記だけを取り出して引き継ぐ。
        base = t("viewer.post_grid.count_suffix", n=shown)
        current = host.breadcrumb_count_text()
        extra = current[len(base):] if current.startswith(base) else ""
        host.set_breadcrumb_count_text(
            t("viewer.post_grid.count_filtered", total=total, shown=shown) + extra
        )

    def _land_tag_results(
        self,
        results: list[tuple[FolderEntry, str]],
        *,
        query: tuple,
        posted_seeded: bool,
        ranked: bool,
        outcome: str = "ok",
    ) -> None:
        """4 つのタグ / 意味スロット共通の着地経路.

        結果を「計算されたときの署名」と一緒に記録し、再構築を跨いで選択を保ち
        （ワーカーは 1 クエリにつき最大 2 回 emit する: 即時集合 → 実在確認で
        間引いた集合）、グリッドを組み直す。1 箇所にまとめてあることが、
        ハンドラが互いにドリフトするのを止めている。

        *outcome* はその着地の**結末**（``"ok"`` か失敗の種別）。署名と**同時
        に**ここで書くので「失敗した」は独立したラッチではなく**その着地記録の
        属性**になる — クエリを編集して署名が変われば結末ごと無効になるので、
        降ろし忘れる場所が構造的に存在しない。
        """
        self._tag_results = results
        self._tag_results_query = query
        self._landed_outcome = outcome
        self._tag_results_posted_seeded = posted_seeded
        self._tag_rel_paths = {str(entry.path): rel for entry, rel in results}
        self._tag_results_ranked = ranked
        self.host.preserve_selection_for_rebuild()
        self.host.rebuild_grid()

    def _on_tag_results(self, generation: int, results) -> None:
        scan.on_tag_results(self, generation, results)

    def _on_tag_failed(self, generation: int) -> None:
        scan.on_tag_failed(self, generation)

    def _on_vector_results(self, generation: int, results) -> None:
        scan.on_vector_results(self, generation, results)

    def _on_vector_failed(self, generation: int) -> None:
        scan.on_vector_failed(self, generation)

    # ------------------------------------------------------ 入力ハンドラ

    def _on_tag_input_changed(self, text: str) -> None:
        new = text.strip()
        query = self._query
        was_active = query.enabled
        enabled = was_active
        includes, excludes = _parse_query(new)
        has_terms = bool(includes or excludes)
        # 揮発の起動フラグを自動 arm / disarm: チップが着地した瞬間に検索が
        # 走り、最後のチップを消せば（年齢区分も中立なら）AIタグ検索は止まる。
        # 年齢区分だけでもタグモードは生き続ける（``_query_mode`` と同じ式）。
        if self.host.tag_index is not None:
            enabled = has_terms or query.rating_key != "all"
        self.set_query(replace(query, text=new, enabled=enabled))
        # 非活性化の分岐: タグ / 意味の結果がグリッドから去るので、選択（または
        # その包含フォルダ）を再構築を跨いで保つ。
        if was_active and not enabled:
            self.host.preserve_selection_for_rebuild(ancestor_fallback=True)
        # タグを打った時点で「参照画像で引く」形は走らないので、モードを移す。
        # 倒し先の妥当性検証は書き込み口 1 本が持つので、ここは意図（rank へ）を
        # 渡すだけ — rank が使えない環境では ``_set_ai_mode`` が自動で "and" へ
        # 落とし、打ったタグが実際に効くモードになる。シードも同時に落ちる。
        if self._query.mode == "similar" and self._query.text:
            self._set_ai_mode("rank")
            self._update_similar_clear_enabled()
        # カバレッジ表示単位の可否は include タグの数に依存する。
        self._update_display_unit_combo()
        # 開いているタグ一覧の「現在の検索条件:」も追従させる。
        self._sync_tag_browser_terms()
        self._maybe_start_tag_scan()

    # -------------------------------------------------------- タグ一覧

    def _open_tag_browser(self) -> None:
        """モードレスの AI タグ一覧を開く（既に在れば前面へ）.

        1 つのダイアログを使い回すので、窓の位置 / 絞り込みが再オープンを跨いで
        残る。開くたびに現在のタグ条件を流し込む（使い回す実装なのに種を蒔く
        経路が無いと、「現在の検索条件:」はこのダイアログ経由で追加した分だけを
        積算してしまう）。tags.db が無ければ no-op。
        """
        tag_index = self.host.tag_index
        if tag_index is None:
            return
        dlg = self._tag_browser
        if dlg is None:
            from .tag_browser import TagBrowserDialog

            dlg = TagBrowserDialog(tag_index, parent=self.host.widget())
            dlg.tag_selected.connect(self._on_browser_tag_selected)
            dlg.tag_excluded.connect(self._on_browser_tag_excluded)
            self._tag_browser = dlg
        self._sync_tag_browser_terms()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _sync_tag_browser_terms(self) -> None:
        """タグ一覧の「現在の検索条件:」を実際のタグ欄に合わせる.

        開くたびと、開いている間のチップ変更の両方から呼ぶので、ポップオーバー
        側の追加・削除・リセットが即座に反映される。ダイアログ未生成なら何も
        しない。

        表示は平坦化（casefold + ``~`` 剥がし）を通さず元表記のまま組む:
        ``~cat ~dog`` を通常 include チップ 2 個に均すと AND に見えてしまうので、
        OR プールは「cat / dog」の 1 チップへ畳む。分解そのものは共有の
        :func:`~.filter_query.split_tag_terms`（かつてここに 3 つ目の手書き
        パーサがあり、先頭 1 文字しか剥がさないため ``-~cat`` が「除外: ~cat」と
        表示されていた）。
        """
        dlg = self._tag_browser
        if dlg is None:
            return
        includes, or_terms, excludes = split_tag_terms(self._query.text)
        if or_terms:
            includes.append(" / ".join(or_terms))
        dlg.set_current_terms(includes, excludes)

    def _on_browser_tag_selected(self, tag: str) -> None:
        """一覧で選ばれた include タグを足し、検索を武装する."""
        self._ensure_tag_search_on()
        self.tag_input.add_tag(tag)

    def _on_browser_tag_excluded(self, tag: str) -> None:
        """一覧で選ばれた除外（``-tag``）チップを足す."""
        self._ensure_tag_search_on()
        self.tag_input.add_tag("-" + tag)

    def _ensure_tag_search_on(self) -> None:
        """注入されたチップが実際に結果を出すよう AIタグ検索を武装する.

        起動用チェックボックスは無く、``add_tag`` による注入は既に
        ``textChanged`` → :meth:`_on_tag_input_changed` を発火して揮発フラグを
        立てる。これはタグ一覧 / 詳細窓からの注入経路のための順序非依存な
        保険（冪等）。
        """
        if self.host.tag_index is None or self._query.enabled:
            return
        self.set_query(replace(self._query, enabled=True))
        self._update_tag_controls_enabled()
        self._maybe_start_tag_scan()

    def add_search_tag(self, tag: str) -> None:
        """*tag* を AIタグ検索へ足して武装する（公開入口）.

        詳細窓の「このタグで検索」コンテキストメニューのための入口。チップと
        して注入し、AIタグ検索を ON にする（有効化の帳簿は
        :meth:`_ensure_tag_search_on` の 1 実装を使い回す）。適用されたクエリは
        条件チップバーが見せる。tags.db が無ければ no-op。
        """
        if self.host.tag_index is None:
            return
        tag = tag.strip()
        if not tag:
            return
        self.tag_input.add_tag(tag)
        self._ensure_tag_search_on()

    # ------------------------------------------------------ 類似シード

    def _on_pick_similar_image(self) -> None:
        """参照画像を選ぶファイルダイアログを開く.

        事前選択は要らない — 任意の画像を選べることがこの導線の要点（シード行
        への D&D がもう一方の経路）。開始位置は選択中のファイル、無ければ
        現在のルート。
        """
        host = self.host
        if host.vector_index is None:
            return
        start = ""
        root = host.root_or_folder
        if root:
            start = str(root)
        tile_path = host.current_tile_path()
        if tile_path is not None:
            start = str(tile_path)
        patterns = " ".join(f"*{ext}" for ext in sorted(IMAGE_SUFFIXES))
        # 静的 getOpenFileName ではなく共有の :func:`dialogs.pick_open_file` —
        # ネイティブでない環境へ落ちたときにラベルがカタログ文言になる。
        chosen = pick_open_file(
            host.widget(), t("viewer.advanced_search.pick_image_dialog_title"),
            start, t("viewer.advanced_search.image_file_filter", patterns=patterns),
        )
        if chosen:
            self._seed_from_path(Path(chosen))

    def _seed_from_path(self, path: Path) -> None:
        """*path* が画像であることを確かめて類似検索の種にする.

        ファイル選択とシード行の D&D が共有する。
        """
        if self.host.vector_index is None:
            return
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            self.host.set_search_status(
                t("viewer.advanced_search.status_not_image")
            )
            return
        self.set_similar_seed(path)

    def set_similar_seed(self, path: Path, pixmap: QPixmap | None = None) -> None:
        """類似画像検索を *path* に錨づける（公開入口）.

        意味検索を有効にし、*path* をシードとして記録し、シードプレビュー行を
        更新し（このペインがタイルを解決できないときは *pixmap* を使う — 例:
        右ペインのコンテキストメニュー発）、ベクトル走査を蹴る。
        """
        if self.host.vector_index is None:
            return
        # モードとシードは対 — 書き込み口を通してから seed を載せる
        # （``_set_ai_mode("similar")`` は既存のシードを落とさない）。
        self._set_ai_mode("similar")
        self.set_query(replace(self._query, similar_seed=str(path)))
        # 種別走査（すべて / 画像 以外）は ``query_mode`` の最優先分岐なので、
        # 残したままではシードが署名にもワーカー要求にも載らず、利用者の直近の
        # 命令が無言で捨てられる。シード要求は明示操作なので AI 側の種別を
        # 中立へ戻す（条件チップ × の ``_clear_dim_ai_media`` と同じ行き先）。
        # 未適用の繰り延べ復元も、後から点灯して種別走査へ戻さないよう落とす。
        self._neutralise_media_walk_for_seed()
        # 外部プレビューの記憶（または忘却）は、どの更新よりも**先**に —
        # シード行の表示は再構築のたびにサムネを引き直すので、pixmap はそれが
        # 見つけられる場所に居なければならない。
        self._similar_seed_pixmap = (
            (str(path), pixmap)
            if pixmap is not None and not pixmap.isNull()
            else None
        )
        self._update_tag_controls_enabled()
        self._update_similar_clear_enabled()
        # 非同期の走査着地を待たず、いま類似モードとシード行を見せる（右ペイン
        # 発 / ファイル選択発のシードが即座に映る）。
        self._sync_ai_segment()
        self.host.set_search_status(
            t("viewer.advanced_search.status_searching_similar")
        )
        self._maybe_start_tag_scan()

    def _neutralise_media_walk_for_seed(self) -> None:
        """種別走査中なら AI 側の種別を「すべて」へ戻す（シード要求の前提）."""
        deferred = self._deferred_media_restore
        if deferred is not None and deferred not in ("all", "image"):
            self._deferred_media_restore = None
        if self._query.media_type in ("all", "image"):
            return
        select_combo_data(self.tag_media_combo, "all")
        self.set_query(replace(self._query, media_type="all"))

    def _on_similar_clear_clicked(self) -> None:
        """参照画像だけを外す（意味検索モードには留まる）.

        タグ欄に語があればタグ親和度ランキングへ戻り、無ければクエリが非活性に
        なってグリッドは通常の直下子表示へ帰る。
        """
        if self._query.similar_seed is None:
            return
        self._clear_similar_seed()
        self._update_similar_clear_enabled()
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _update_similar_clear_enabled(self) -> None:
        btn = getattr(self, "tag_similar_clear_btn", None)
        if btn is not None:
            btn.setEnabled(
                self._query.similar_seed is not None
                and self.host.vector_index is not None
            )
        # シードプレビュー行も同じシード状態を追うのでここで更新する（シードを
        # 設定 / 解除する経路は全部これを呼んでいる）。
        self._update_similar_seed_display()

    def _update_similar_seed_display(self) -> None:
        """シード行の**中身**を ``query.similar_seed`` に合わせる.

        行の*外側*の可視性は :meth:`_update_ai_mode_ui` が持つ（類似画像検索
        モードの間はドロップ先として出しっぱなし）。ここで入れ替えるのは中身
        だけ: シード未設定なら D&D の CTA、設定済みなら約 40px のサムネ +
        省略した名前 + 「×」。
        """
        row = getattr(self, "tag_seed_row", None)
        if row is None:
            return
        seed = self._query.similar_seed
        has_seed = bool(seed)
        for w in (
            getattr(self, "tag_seed_thumb", None),
            getattr(self, "tag_seed_name", None),
            getattr(self, "tag_seed_clear", None),
        ):
            if w is not None:
                w.setVisible(has_seed)
        cta = getattr(self, "tag_seed_cta", None)
        if cta is not None:
            cta.setVisible(not has_seed)
        if not has_seed:
            return
        path = Path(seed)
        pixmap = self.host.pixmap_for_key(self.host.thumb_key_prefix + str(path))
        if pixmap is None or pixmap.isNull():
            # このペインが解決できないシード（右ペイン発の類似検索）のために
            # 呼び出し側が渡した pixmap へ落とす。
            external = self._similar_seed_pixmap
            if external is not None and external[0] == seed:
                pixmap = external[1]
        self._set_seed_thumb_pixmap(pixmap)
        self.tag_seed_name.setText(path.name)
        self.tag_seed_name.setToolTip(str(path))

    def _set_seed_thumb_pixmap(self, pixmap: QPixmap | None) -> None:
        """*pixmap* を 40px に縮めてシードサムネのラベルへ描く.

        pixmap が無いとき（シードのタイルがこのペインに居ない / まだデコード
        されていない）はプラットフォームの汎用ファイルアイコンへ落とす。
        """
        label = getattr(self, "tag_seed_thumb", None)
        if label is None:
            return
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(
                QSize(40, 40), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            label.setPixmap(scaled)
            return
        # 標準アイコンの pixmap はスタイルの PNG リソースを Qt の画像プラグイン
        # ファクトリ経由でデコードする。ワーカー側の QImageReader フォールバック
        # と直列化しないと、ロック順の交差でプロセスが固まる。
        with _QT_READER_LOCK:
            fallback = self.host.widget().style().standardIcon(QStyle.SP_FileIcon)
            pm = fallback.pixmap(32, 32)
        label.setPixmap(pm)

    # ------------------------------------------------- コンボのハンドラ

    def _on_tag_threshold_changed(self, value: float) -> None:
        # ユーザーの明示操作は起動時の繰り延べ復元より優先。
        self._deferred_threshold_restore = None
        self._store_threshold(value)
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _on_tag_media_changed(self) -> None:
        # ユーザーの明示操作は起動時の繰り延べ復元より優先。
        self._deferred_media_restore = None
        self.set_query(replace(
            self._query, media_type=self.tag_media_combo.currentData() or "all",
        ))
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _on_tag_rating_changed(self) -> None:
        rating_key = self.host.rating_combo.currentData() or "all"
        query = replace(self._query, rating_key=rating_key)
        # 中立でない帯はそれ自身でタグモードを武装する（起動用チェックボックスは
        # もう無い）。
        if self.host.tag_index is not None:
            includes, excludes = _parse_query(query.text)
            query = replace(query, enabled=bool(
                includes or excludes or rating_key != "all"
            ))
        self.set_query(query)
        # 席がフィルターポップオーバーへ移ったので、他のフィルタ軸と同じく
        # アクセントを同期する。
        self.host.update_filter_bar()
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _on_tag_display_unit_changed(self) -> None:
        """3 択の表示単位コンボを永続化される 2 bool へ写す."""
        key = self.tag_display_unit_combo.currentData() or NEUTRAL_DISPLAY_UNIT
        self.set_query(with_display_unit(self._query, key))
        self._maybe_start_tag_scan()
        self.host.rebuild_grid()

    def _sync_date_range_visibility(self) -> None:
        """投稿日「範囲」のインラインエディタを現在のプリセットに合わせる."""
        show_range = self._query.date_preset == "range"
        host = self.host
        host.date_from.setVisible(show_range)
        host.date_separator.setVisible(show_range)
        host.date_to.setVisible(show_range)

    def _on_tag_date_preset_changed(self) -> None:
        self.set_query(replace(
            self._query, date_preset=self.host.date_combo.currentData() or "all",
        ))
        self._sync_date_range_visibility()
        # 投稿日は着地済み結果への GUI 側フィルタなので引き直しは要らない —
        # ただし ``posted_at`` のシード無しで取った結果の上で境界が有効になった
        # ときだけは別（ワーカーは頼まれたときしか日付を解決しない）。
        self._maybe_requery_for_posted_at()
        self.host.rebuild_grid()

    def _on_tag_date_changed(self, _date) -> None:
        if self._query.date_preset == "range":
            self._maybe_requery_for_posted_at()
            self.host.rebuild_grid()

    def _maybe_requery_for_posted_at(self) -> None:
        """現在の結果が持っていない ``posted_at`` を投稿日フィルタが要るとき、
        AI 検索ワーカーを蹴り直す.

        検索結果の行は ``post.md`` を読まずに組まれる（ゼロ I/O のフェーズ 1
        契約）ので、ワーカーが ``posted_at`` を種として載せるのは「リクエストが
        日付境界は有効だと言ったとき」だけ。結果が着地した*後*にユーザーが境界を
        有効化すると、GUI 側の日付フィルタは黙って no-op になる — 日付つきで
        返るよう引き直す。
        """
        if not self._advanced_search_active():
            return
        if not self._date_bounds_active():
            return
        # シード済みフラグは**最後に着地した集合**を説明するので、着地した署名が
        # まだ一致している間だけ現在のクエリに答えられる。このガードが無いと、
        # 古い ``True``（前のクエリのシード済み結果）が、現署名のシード無し走査が
        # 実行中のあいだ引き直しを抑止する — その走査は ``posted_at=None`` で
        # 着地し、日付判定はそれを丸ごと通すので、投稿日チップは「効いているのに
        # 何も絞らない」状態になる。
        seeded = (
            self._tag_results_posted_seeded
            and self._advanced_phase() in ("landed", "error")
        )
        if not seeded:
            self._maybe_start_tag_scan()

    # ------------------------------------------------------- 設定往復

    def restore_tag_settings(self, state, *, restore_enabled: bool = False) -> None:
        """永続設定をコントロールへ当てて蹴る（実装は
        :mod:`~.advanced_search_parts.persistence`）."""
        persistence.restore(self, state, restore_enabled=restore_enabled)

    def save_tag_settings(self, state) -> None:
        """いま効いている値を *state* へ書く（同上）."""
        persistence.save(self, state)

    @staticmethod
    def _select_combo_data(combo, key) -> None:
        """``itemData`` が *key* の項目を（signals を止めて）選ぶ."""
        select_combo_data(combo, key)
