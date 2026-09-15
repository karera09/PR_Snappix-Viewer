"""AI 検索スキャナの投入と着地（``AdvancedSearchController`` の内側部品）.

スキャナ 2 本（タグ / 種別走査の ``TagSearchScanner`` と意味検索の
``VectorSearchScanner``）の生成・破棄・投入・着地ガードをここへ集める。
スキャナの**クラス**は有償 AI プラグインに住み、ここは ``ai_pack`` の
provider レジストリ越しに実体を受け取るだけ（プラグイン未登録なら両方
``None`` のままで、パネルは tags.db 不在の骨組みと同じ「表示されるが無効 +
案内」に劣化する）。

**不変（着地）**: 各着地スロットは「世代ガード → 投入記録との署名一致 →
着地」の順で判定し、着地そのものは :meth:`AdvancedSearchController.
land_results` 1 本を通る。意味検索の 2 スロットは再ゲートを ``finally`` で
踏むが、それは**着地の後** — 先に再ゲートすると保持モードが降格して署名が
動き、その着地自身が「古い署名の結果」として捨てられる。

**不変（投入記録）**: 投入は :func:`kick` だけが書き、消費は 4 スロットだけ。
記録は :class:`~.query.ScanRequest` の 1 スロットで、同時に有効な投入は常に
片方だけ（投げる前にもう一方のスキャナを ``cancel`` する）。
"""

from __future__ import annotations

from loguru import logger

from ...common.i18n import t
from ...common.ui.timers import DebounceMode, Debouncer
from .. import ai_pack
from ..folder_scan import media_suffixes_for
from ..filter_query import _parse_query, _parse_tag_groups
from .query import RATING_BANDS, ScanRequest


def create(ctl, folder_cache) -> None:
    """デバウンスを張り、可能ならスキャナも作る。

    スキャナの実体は provider が登録されて初めて作れる（プラグインの
    ``activate`` はこのウィンドウの構築後に走る）ので、ここでは
    :func:`ensure` を 1 回試すだけ。遅れて登録された場合は
    ``refresh_tag_index_ui`` が同じ :func:`ensure` を踏む。
    """
    ctl._advanced_folder_cache = folder_cache
    ctl._tag_scanner = None
    ctl._vector_scanner = None
    ctl._tag_debounce = Debouncer(
        ctl, 250, ctl._kick_tag_scan, mode=DebounceMode.TRAILING
    )
    ensure(ctl)


def ensure(ctl) -> None:
    """provider が居れば 2 本を作って配線する（冪等）。

    生成は**例外ガードする**: パックの部分展開・AV 隔離等で engine/ が欠けると
    ``create_*_scanner`` の遅延 import が ``ModuleNotFoundError`` で死ぬ。ここで
    例外を上へ逃がすと provider 登録コールバック（``refresh_tag_index_ui`` の
    途中）ごと落ち、バナー / タイトル更新に到達しないまま status と表示が
    食い違う。失敗はスキャナ不在（= provider 未登録と同じ劣化シーム。
    :func:`kick` がエンジン不在エラーとして着地させる）に落とす。
    """
    if ctl._tag_scanner is not None:
        return
    provider = ai_pack.provider()
    if provider is None:
        return
    scanner = vscanner = None
    try:
        # タグ / 種別走査のワーカー。フォルダプレビューキャッシュがあるので、
        # 投稿日フィルタが効いているときも暖かいフォルダなら post.md を
        # 読み直さずに posted_at を種として載せられる。
        scanner = provider.create_tag_scanner(
            tag_index=ctl.host.tag_index,
            folder_cache=ctl._advanced_folder_cache,
            parent=ctl,
        )
        scanner.results_ready.connect(ctl._on_tag_results)
        scanner.failed.connect(ctl._on_tag_failed)
        # 意味検索はタグ側と同じ表示チャネル / デバウンスを共有し、プールだけ
        # 別（それぞれ単一スレッド）。
        vscanner = provider.create_vector_scanner(
            vector_index=ctl.host.vector_index,
            folder_cache=ctl._advanced_folder_cache,
            parent=ctl,
        )
        vscanner.results_ready.connect(ctl._on_vector_results)
        vscanner.failed.connect(ctl._on_vector_failed)
    except Exception:
        logger.exception(
            "AI search scanner construction failed — "
            "エンジン不在の劣化シームへ落とします"
        )
        for obj in (scanner, vscanner):
            drop_later = getattr(obj, "deleteLater", None)
            if drop_later is not None:
                drop_later()
        ctl._tag_scanner = None
        ctl._vector_scanner = None
        return
    ctl._tag_scanner = scanner
    ctl._vector_scanner = vscanner


def cancel(ctl) -> None:
    """デバウンスを止め、**両方**のスキャナを cancel する。

    意味検索側を必ず道連れにするのが要点: その署名はルートを持たないので、
    後から着地した旧ルートのランク結果はどのガードも通ってしまう。
    """
    ctl._tag_debounce.stop()
    if ctl._tag_scanner is not None:
        ctl._tag_scanner.cancel()
    if ctl._vector_scanner is not None:
        ctl._vector_scanner.cancel()


def drop(ctl) -> None:
    """2 本を破棄する — :func:`ensure` の対。

    破棄は「実行中を止める → 双方 ``deleteLater`` → 双方 None」の 4 手が揃って
    1 つの手順: 参照を切るだけでは QObject が生き残り、まだ走っている検索が
    同じ世代で ``results_ready`` を emit すると着地スロットの世代ガードを
    素通りし得る。手順を 1 実装にしておけば、次の破棄地点を足すときに片方を
    落とせない。
    """
    cancel(ctl)
    if ctl._tag_scanner is not None:
        ctl._tag_scanner.deleteLater()
    if ctl._vector_scanner is not None:
        ctl._vector_scanner.deleteLater()
    ctl._tag_scanner = None
    ctl._vector_scanner = None


def maybe_start(ctl) -> None:
    """状態を見てワーカーを蹴る（または片付ける）。"""
    if ctl.host.requery_suspended:
        # 次元をまとめて中立化している最中 — 途中の中途半端な条件で蹴らない。
        return
    ctl._update_advanced_badge()
    sig = ctl._tag_query_signature()
    if sig is None or ctl.host.root_or_folder is None:
        cancel(ctl)
        # 結果が画面に出ていたなら、AI クエリはたった今非活性になった
        # （チップが空になった / 年齢区分が「すべて」へ戻った）— 通常の直下子
        # 表示へ戻さないと、古いタグ / 種別タイルが居座る。
        had_results = ctl._tag_results is not None
        ctl._advanced_search_drop_results()
        # 消したいのは AI 検索が**自分について**出した行だけ。占有一覧が
        # 上がっているあいだ状態行はその一覧のもので — 一覧への入場は
        # ``clear_search_state`` を通り、逆に AI 検索の起動は下の分岐で一覧を
        # 退場させるので、両者が同時に状態行を名乗ることはない — ここで空へ
        # 倒すと、一覧が自分について語る唯一の行が再導出の手段なく消える。
        if not ctl.host.overlay_active:
            ctl.host.set_search_status("")
        # AI クエリが非活性になった瞬間は、再帰の子孫走査が再び適格になる
        # 瞬間でもある: AI 検索中は ``maybe_start_recursive_scan`` が非活性
        # 分岐に落ちて走査結果を捨てており、この遷移で発火する呼び出し元は
        # 他に無い（チップが空になった / 種別が「すべて」へ戻った）。
        ctl.host.maybe_start_recursive_scan()
        if had_results and ctl.host.root_or_folder is not None:
            ctl.host.rebuild_grid()
        return
    # 横断キュレーション一覧と「最近追加されたファイル」は、表示中グリッドと
    # ステータス行を占有する。AI タグクエリは独立した検索面（一覧をメモリ内で
    # 絞る絞り込み欄ではなく、自前の入力）なので、明示的な指定は一覧を
    # 引き取る — そうしないとステータスが「AIタグ検索: N件」と言いながら
    # グリッドは一覧の母集合を出す矛盾になり、しかも一覧分岐が優先されるので
    # 結果は隠れるのではなく**到達不能**になる。
    if ctl.host.overlay_active:
        ctl.host.exit_overlay()
    # 楽観的なステータス: 250ms 後ではなく最初の編集でバーが反応する。
    ctl.host.set_search_status(t("viewer.advanced_search.status_searching"))
    ctl._tag_debounce.trigger()


def kick(ctl) -> None:
    """現在のクエリで実際にワーカーへ投げる（デバウンスの着地点）。"""
    host = ctl.host
    if host.root_or_folder is None:
        return
    # provider 不在（プラグイン未 activate / 素の配布で有効化記録だけがある）
    # → スキャナも無い。ここで黙って return すると検索が**凍る**:
    # :func:`maybe_start` が既に「詳細検索中…」を出してデバウンスを張った後
    # なのに、スキャナが居ないので結果も失敗も永遠に着地せず、AI 検索は
    # 活性のままグリッドが空に固定される（種別走査は tags.db 不要なので、
    # ユーザーは手作業でここへ到達できる）。失敗を明示的に着地させ、
    # 「表示されるが無効 + 案内」の劣化シームに合わせる。
    if ctl._tag_scanner is None or ctl._vector_scanner is None:
        sig = ctl._tag_query_signature()
        if sig is None:
            return
        # 「エンジンが居ない」も 0 件ではない — 空着地と同時に失敗の種別を
        # 記録し、グリッドには専用エラーカードを出す。
        ctl._land_tag_results(
            [], query=sig,
            posted_seeded=ctl._date_bounds_active(), ranked=False,
            outcome="engine",
        )
        host.set_search_status(
            t("viewer.advanced_search.status_engine_unavailable")
        )
        return
    sig = ctl._tag_query_signature()
    if sig is None:
        return
    query = ctl.query
    # 投稿日フィルタに境界があるときだけ posted_at の解決へワーカー I/O を
    # 使う（さもなくば posted_at=None のままで、GUI 側の日付フィルタは
    # 「絞らない」として扱う）。
    want_posted_at = ctl._date_bounds_active()
    if sig[0] == "semantic":
        # タグ側スキャナはまだ古いクエリを走らせているかもしれない（世代は
        # **自分の**次のリクエストでしか進まない）— 遅いタグ走査がランク結果の
        # 後に着地して上書きしないよう cancel する。下の分岐も対称。
        ctl._tag_scanner.cancel()
        ctl.set_pending(ScanRequest("vector", sig, want_posted_at))
        ratings = RATING_BANDS.get(query.rating_key)
        if sig[1] == "similar":
            ctl._vector_scanner.request(
                host.root_or_folder, mode="similar",
                seed_path=query.similar_seed, ratings=ratings,
                want_posted_at=want_posted_at,
            )
        else:
            includes, _excludes = _parse_query(query.text)
            ctl._vector_scanner.request(
                host.root_or_folder, mode="tags", includes=includes,
                ratings=ratings, want_posted_at=want_posted_at,
            )
        return
    ctl._vector_scanner.cancel()
    ctl.set_pending(ScanRequest("tag", sig, want_posted_at))
    media_suffixes = media_suffixes_for(query.media_type)
    if sig[0] == "tags":
        # OR 群つきの include（``~a ~b c`` ⇒ ``(a OR b) AND c``）。
        groups, excludes = _parse_tag_groups(query.text)
        ctl._tag_scanner.request(
            host.root_or_folder,
            mode="tags",
            include_groups=groups,
            excludes=excludes,
            threshold=query.threshold,
            ratings=RATING_BANDS.get(query.rating_key),
            folder_mode=query.folder_mode,
            coverage=query.coverage_mode,
            media_suffixes=media_suffixes,
            want_posted_at=want_posted_at,
        )
    else:
        ctl._tag_scanner.request(
            host.root_or_folder,
            mode="media",
            folder_mode=query.folder_mode,
            media_suffixes=media_suffixes,
            want_posted_at=want_posted_at,
        )


# ------------------------------------------------------------------ 着地


def on_tag_results(ctl, generation: int, results) -> None:
    # provider がクエリ実行中に外れた: スキャナは破棄済みでも、キューに
    # 載った結果シグナルはまだ着地し得る。
    if ctl._tag_scanner is None:
        return
    if generation != ctl._tag_scanner.latest_generation():
        return
    # この走査が投げられた署名が**今も**現クエリであるときだけ消費する —
    # デバウンス窓の中で編集されたクエリの結果と、意味検索が表示を引き取った
    # 後に着地した古いタグ走査（放置すればランク結果を上書きし、署名チェックを
    # 偽の署名で通過する）を捨てる。
    query = ctl.pending_signature("tag")
    if query is None or query != ctl._tag_query_signature():
        return
    # 成功着地: 結末（``outcome="ok"`` の既定）は着地記録の一部として
    # ``land_results`` が書く — 呼び出し元の後始末は無い。再構築時の再調停が
    # その記録を読んで「✓ … — N 件」を組む。
    ctl._land_tag_results(
        results, query=query,
        posted_seeded=ctl.pending_want_posted_at("tag"), ranked=False,
    )


def on_tag_failed(ctl, generation: int) -> None:
    if ctl._tag_scanner is None:
        return  # provider がクエリ実行中に外れた
    if generation != ctl._tag_scanner.latest_generation():
        return
    query = ctl.pending_signature("tag")
    if query is None or query != ctl._tag_query_signature():
        return  # 古い投入 / ユーザーが途中でクエリを降ろした
    # 失敗の種別も着地記録へ（グリッドは ⚠ カードを出し、ここでは回復しない
    # 「条件をゆるめて再検索」を見せない）。
    ctl._land_tag_results(
        [], query=query,
        posted_seeded=ctl.pending_want_posted_at("tag"), ranked=False,
        outcome="tag_db",
    )
    ctl.host.set_search_status(
        t("viewer.advanced_search.status_tag_db_error")
    )


def on_vector_results(ctl, generation: int, results) -> None:
    # クエリが行列のロードを強制したので、``has_tag_vectors`` は「ファイルが
    # ある」から「行列が読めた」へ答えを変えている — セグメントのゲートを
    # 当て直さないと、壊れたタグ→ベクトル行列のまま「AIタグ類似検索」が
    # セッション中ずっと点灯する。``finally`` に置くのは全ての return 経路で
    # 踏むためだが、**着地の後**であることが要点: 先に再ゲートすると保持モードが
    # 降格して下のガードが比較する署名が動き、着地が stale として捨てられる。
    try:
        if ctl._vector_scanner is None:
            return  # provider がクエリ実行中に外れた
        if generation != ctl._vector_scanner.latest_generation():
            return
        query = ctl.pending_signature("vector")
        if query is None or query != ctl._tag_query_signature():
            return  # 意味検索が降りた / 実行中にクエリが編集された
        ctl._land_tag_results(
            results, query=query,
            posted_seeded=ctl.pending_want_posted_at("vector"), ranked=True,
        )
    finally:
        ctl._regate_after_vector_load()


def on_vector_failed(ctl, generation: int) -> None:
    # 成功着地と同じ再ゲートを同じ場所で: この失敗が**まさに**壊れた
    # タグ→ベクトル行列かもしれず、ガードより前に再ゲートするとモードが降格して
    # 署名が動き、パネルは自分の失敗着地を捨ててしまう（⚠ カードも出ず、
    # 引き直す手段も無いまま「詳細検索中…」に固着する）。
    try:
        if ctl._vector_scanner is None:
            return  # provider がクエリ実行中に外れた
        if generation != ctl._vector_scanner.latest_generation():
            return
        query = ctl.pending_signature("vector")
        if query is None or query != ctl._tag_query_signature():
            return  # 古い投入 / 意味検索が実行中に降りた
        ctl._land_tag_results(
            [], query=query,
            posted_seeded=ctl.pending_want_posted_at("vector"), ranked=False,
            outcome="vector",
        )
        ctl.host.set_search_status(
            t("viewer.advanced_search.status_vector_error")
        )
    finally:
        ctl._regate_after_vector_load()
