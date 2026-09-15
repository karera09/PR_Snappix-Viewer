"""AI 検索の設定往復（``ViewerState`` ↔ コントローラ）.

**契約は「検索は揮発、設定は永続」**: クエリの*入力*（タグ語 / 精度 / 種別 /
表示単位）は保存して復元するが、**起動している状態**は復元しない。起動時は
常に OFF で、保存されたクエリは「見えているが効いていない」まま — ユーザーが
触れば揮発フラグが立つ。3 択モードと参照画像も同じ揮発側で、設定復元は常に
中立の ``"and"`` から始める（履歴復元だけがスナップショットの値を後から
明示適用する）。

繰り延べ復元が 2 つある: 種別と精度は、本番の起動順では**まだ provider が
登録されておらず tags.db も開かれていない**時点で復元が走る。ここで捨て切ると
正常な有償環境でも毎起動で設定が消えるので、適用だけを点灯
（``refresh_tag_index_ui``）まで繰り延べ、その間に**ユーザーが明示操作したら
繰り延べを破棄する**（ハンドラ側）。
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QComboBox

from .. import ai_pack
from ..filter_query import _parse_query


def select_combo_data(combo: QComboBox, key) -> None:
    """``itemData`` が *key* の項目を（signals を止めて）選ぶ。

    添字ではなく鍵で引くのが規律 — 選択肢は条件次元レジストリ由来で、並びが
    変われば同じ添字が別の選択肢を指す。
    """
    for i in range(combo.count()):
        if combo.itemData(i) == key:
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            return


def restore(ctl, state, *, restore_enabled: bool = False) -> None:
    """永続設定をコントロールへ当て、必要なら走査を蹴る。

    *restore_enabled* はセッション内のナビ履歴復元（「戻る」）専用で、そこ
    だけは捕まえた検索を**そのまま再実行する**のが目的なので、起動フラグ・
    年齢区分・投稿日も復元する。
    """
    host = ctl.host
    text = state.tag_search_query or ""
    ctl.tag_input.blockSignals(True)
    ctl.tag_input.setText(text)
    ctl.tag_input.blockSignals(False)

    # 3 択モードは ``ViewerState`` に載らない揮発の選択なので、設定復元は常に
    # 中立の ``"and"`` から始める。残留値を持ち越すと、AND 検索を復元したのに
    # セグメントだけ「類似画像検索」になり、検索実体・ステータスと食い違う。
    ctl._set_ai_mode("and")

    # メディア種別は provider（＝スキャナ構築可否）が揃っているときだけ復元
    # する。``ai_pack.available()`` だけのゲートでは、有効化記録があるのに
    # activate が失敗した劣化シーム（フラグ ON・provider 不在）で保存済みの
    # 「動画」等が復元され、起動直後にメディアウォークが走るが scanner 不在で
    # 「詳細検索中…」に固着してグリッドが空になる。
    provider_ready = ai_pack.available() and ai_pack.provider() is not None
    saved_media = state.tag_search_media_type or "all"
    ctl._deferred_media_restore = (
        saved_media if (not provider_ready and saved_media != "all") else None
    )
    select_combo_data(
        ctl.tag_media_combo,
        state.tag_search_media_type if provider_ready else "all",
    )
    # 年齢区分: 保存はするが起動時は復元しない — 起動フラグが揮発なので、
    # 非中立の帯を復元すると「グリッドが適用していない条件チップ」が残る
    # （Esc でも消せない）。投稿日も同じ規則。
    select_combo_data(
        host.rating_combo, state.tag_search_rating if restore_enabled else "all",
    )
    select_combo_data(
        host.date_combo, state.tag_date_preset if restore_enabled else "all",
    )
    ctl.set_query(replace(
        ctl.query,
        text=text,
        media_type=ctl.tag_media_combo.currentData() or "all",
        rating_key=host.rating_combo.currentData() or "all",
        date_preset=host.date_combo.currentData() or "all",
        folder_mode=bool(state.tag_search_folder_mode),
        coverage_mode=bool(state.tag_search_folder_coverage),
    ))
    ctl._update_display_unit_combo()

    if host.tag_index is not None:
        ctl.tag_threshold_slider.blockSignals(True)
        floor = ctl.tag_threshold_slider.minimum()
        ctl.tag_threshold_slider.setValue(max(state.tag_search_threshold, floor))
        ctl.tag_threshold_slider.blockSignals(False)
        ctl._store_threshold(ctl.tag_threshold_slider.value())
        ctl._deferred_threshold_restore = None
    else:
        # tags.db は provider 点灯後に開かれる（本番順序）ため、保存精度の
        # 適用も点灯まで繰り延べる（メディア種別の繰り延べと同型）。
        ctl._deferred_threshold_restore = float(state.tag_search_threshold)

    for which, iso in (
        (host.date_from, state.tag_date_start),
        (host.date_to, state.tag_date_end),
    ):
        if iso:
            qd = QDate.fromString(iso, "yyyy-MM-dd")
            if qd.isValid():
                which.blockSignals(True)
                which.setDate(qd)
                which.blockSignals(False)
    ctl._sync_date_range_visibility()

    # ``state.tag_search_panel_expanded`` は撤去された折り畳みパネルの遺物で、
    # 往復互換のため ``ViewerState`` には残るが適用しない（ポップオーバーには
    # 復元すべき展開状態が無い）。

    # 揮発の起動: 起動時（restore_enabled=False）は、上でチップを復元しても
    # AI タグ検索は必ず OFF から始まる。履歴復元だけが再武装し、その値は
    # 復元されたクエリ / 年齢区分から導く。
    if restore_enabled and host.tag_index is not None:
        includes, excludes = _parse_query(text)
        enabled = bool(
            includes or excludes or ctl.query.rating_key != "all"
            or bool(state.tag_search_enabled)
        )
    else:
        enabled = False
    ctl.set_query(replace(ctl.query, enabled=enabled))
    ctl._update_tag_controls_enabled()
    ctl._maybe_start_tag_scan()


def save(ctl, state) -> None:
    """いま効いているコントロールの値を *state* へ書く。

    ``tag_search_enabled`` は**わざと書かない** — 起動フラグは揮発（起動時に
    復元されない）なので、保存してもディスクに誤解を招く値が残るだけ。ナビ
    履歴のスナップショットは自分のスクラッチ state へフラグを直に立てる。
    """
    host = ctl.host
    state.tag_search_query = ctl.tag_input.text().strip()
    # 精度は tags.db があるときだけ復元される（無ければスライダは既定値の
    # まま）ので、無条件に書き戻すと非対称になる — tags.db の無いライブラリで
    # 開いたセッションが、以前保存した精度を既定値で潰してしまう。復元側と
    # 同じ条件でだけ永続化する。
    if (
        host.tag_index is not None
        and ctl._deferred_threshold_restore is None
    ):
        state.tag_search_threshold = float(ctl.tag_threshold_slider.value())
    # provider が一度も点灯しなかったセッション（素の配布・activate 失敗）では
    # 繰り延べ復元が未適用のままなので、コンボの "all" ではなく保存値をその
    # まま保全する（毎起動の恒久消失防止）。
    deferred_media = ctl._deferred_media_restore
    state.tag_search_media_type = (
        deferred_media
        if deferred_media is not None
        else (ctl.tag_media_combo.currentData() or "all")
    )
    state.tag_search_folder_mode = ctl.query.folder_mode
    state.tag_search_folder_coverage = ctl.query.coverage_mode
    state.tag_search_rating = host.rating_combo.currentData() or "all"
    state.tag_date_preset = host.date_combo.currentData() or "all"
    state.tag_date_start = host.date_from.date().toString("yyyy-MM-dd")
    state.tag_date_end = host.date_to.date().toString("yyyy-MM-dd")
