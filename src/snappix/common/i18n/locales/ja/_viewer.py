"""Japanese catalog fragment: ``viewer.*`` — viewer display strings.

Keyed ``viewer.<module>.<slug>`` (``<module>`` = source file stem);
``viewer.common.*`` are strings shared across several viewer files.
Strings reused across *both* tools live in ``_common.py``.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # -- viewer.about_dialog.* --------------------------------------------
    # UIレビュー 07-25 #95: 製品名を権利者として名乗る表記だったため、
    # 「作者」を補って読み替えを避ける（作者氏名の非掲載方針は維持）。
    "viewer.about_dialog.copyright": "© 2026 Snappix Viewer 作者",
    "viewer.about_dialog.show_terms": "利用規約を表示…",
    "viewer.about_dialog.version": "バージョン {version}",
    # -- viewer.advanced_search.* ----------------------------------------
    "viewer.advanced_search.age_band_label": "年齢区分:",
    "viewer.advanced_search.ai_tag_label": "AIタグ:",
    "viewer.advanced_search.ai_tag_tooltip": "AIタグでの絞り込み。空白でAND（すべて含む）、-語 で除外、~語 ~語 でOR（いずれか）。\n例: ~猫 ~犬 屋外 = (猫 または 犬) かつ 屋外",
    # (UIレビュー 2026-08-28 N-31) チップの書式を「ラベル: 値」へ統一 —
    # 旧「精度{value:.2f}」だけコロン無しだった。ラベルは precision_label と
    # 同語（条件次元レジストリ search_dimensions.py が形を機械検証）。
    "viewer.advanced_search.badge_precision": "AIタグの精度: {value:.2f}",
    # tags.db が「壊れている」ときの案内 — 「まだ作られていない」と同じ
    # スキャン案内を出さない (UIレビュー 07-25 #114)。
    "viewer.advanced_search.broken_db_banner": "AIタグの索引 (tags.db) を読み込めません。ファイルが壊れている可能性があります。［AIタグDBを再読み込み］で直らない場合は Snappix Tagger で再スキャンしてください。",
    # 「検索の使い方」チートシート — 軸の行（AIタグ / AIタグの精度 /
    # 表示単位）は条件次元レジストリ（search_dimensions.cheatsheet_html）が
    # 台帳から生成する。ここに残るのは語彙対比・モード説明の静的断片のみ。
    # 関連度の行は実描画どおり「NN%」（◆ は描かれない — N-98）。
    "viewer.advanced_search.cheatsheet_desc_ai_tags": "Snappix Tagger が画像から推定したタグ (tags.db)。このパネルの「AIタグ」欄で検索。",
    "viewer.advanced_search.cheatsheet_desc_precision": "小さいほどゆるく多く拾い、大きいほど確度の高いタグだけに絞る。",
    # (UIレビュー 2026-08-28 N-148) AI 側は絞り込み欄を片方向に参照する
    # だけで、種別 / 年齢区分 / 精度 の等価トークンに触れていなかった。
    "viewer.advanced_search.cheatsheet_equivalent_tokens": "<div style='margin-top:6px'>ここの「対象種別」「年齢区分」「AIタグの精度」は、絞り込み欄に <code>type:</code> / <code>rating:</code> / <code>score:</code> と書くのと同じです。</div>",
    "viewer.advanced_search.cheatsheet_row_post_tags": "<tr><td><b>投稿タグ</b></td><td>post.md に記録された投稿のタグ。上の絞り込み欄で <code>tags:</code> で検索。</td></tr>",
    "viewer.advanced_search.cheatsheet_rows_modes": "<tr><td><b>AIタグ一致</b></td><td>入力したタグをすべて含む画像/フォルダを絞り込み。~語 でOR（いずれか）。</td></tr><tr><td><b>AIタグで意味検索</b></td><td>入力タグへの関連度で並べ替え（一致絞り込みより柔軟）。</td></tr><tr><td><b>画像で類似検索</b></td><td>基準画像（ファイル選択 / D&amp;D / 右クリック）に見た目が近い画像を並べる。</td></tr><tr><td><b>NN%</b></td><td>関連度（数値が大きいほど条件に近い）。類似検索の結果に表示。</td></tr>",
    "viewer.advanced_search.cheatsheet_title": "<b>検索の使い方</b>",
    "viewer.advanced_search.coverage_disabled_tooltip": "AIタグを2つ以上指定すると「複数枚で分担可」が使えます。",
    "viewer.advanced_search.display_unit_label": "表示単位:",
    # (UIレビュー 2026-08-28 N-32) 既定の folder_coverage は include タグが
    # 2 群未満だと自身の無効化ルールに掛かり「開くと現在の選択肢が灰色」に
    # なる。結果は folder_strict と完全に同一なので実害は無いが、その理由が
    # 無効項目のツールチップにしか無かった — 灰色の間だけ常設で出す。
    "viewer.advanced_search.display_unit_note": "※ タグが 2 語以上のときだけ「別々の画像で全タグ可」が効きます（今は「1枚で全タグ」と同じ結果）。",
    "viewer.advanced_search.display_unit_tooltip": "検索ヒットの表示単位。\n・画像ごと: 一致した個別画像を表示。\n・フォルダ（1枚で全タグ）: 1枚の画像が全タグを同時に持つフォルダ。\n・フォルダ（複数枚で分担可）: フォルダ内の複数の画像が手分けして\n　全タグを満たせば表示（複数タグ AND のとき有効）。",
    # 緩和候補が 1 つも作れないときの行き止まり回避 (UIレビュー 07-25 #29)。
    "viewer.advanced_search.empty_edit_query": "AI 検索条件を編集…",
    "viewer.advanced_search.empty_hint": "条件をゆるめて再検索できます:",
    "viewer.advanced_search.empty_name_match_note": "AI 検索結果では、名前・タイトルのみで照合します（投稿タグ・本文は対象外）。",
    "viewer.advanced_search.empty_title": "条件に一致する項目がありません",
    # AI 検索がエンジン側で失敗したときの専用カード (UIレビュー 07-25 #9)。
    # 「0 件」ではないので、緩和提案ではなく再読み込み導線を出す。
    "viewer.advanced_search.error_hint_broken": "索引ファイルが壊れている可能性があります。再読み込みで直らない場合は Snappix Tagger で再スキャンしてください。",
    # (UIレビュー07-25 追修) engine 不在カードは再読み込みボタンを持たない
    # （リロードでは provider は登録されない）ので、案内もプラグイン管理 +
    # 再起動へ向ける。
    "viewer.advanced_search.error_hint_engine": "AI 検索プラグインの読み込みに失敗しています。「ファイル ▸ プラグイン…」で有効になっているか確認し、アプリを再起動してください。",
    "viewer.advanced_search.error_hint_tag_db": "読み込み直すと復帰することがあります。直らない場合は Snappix Tagger で再スキャンしてください。",
    "viewer.advanced_search.error_hint_vector": "Snappix Tagger で再スキャンするとベクトルが作り直され、意味検索・類似検索が使えるようになります。",
    "viewer.advanced_search.error_title_engine": "AI 検索エンジンを利用できません",
    "viewer.advanced_search.error_title_tag_db": "AIタグの索引を読み込めませんでした",
    "viewer.advanced_search.error_title_vector": "意味検索ベクトルを読み込めませんでした",
    "viewer.advanced_search.examples_hint": "例: {sample}",
    "viewer.advanced_search.examples_hint_syntax": "空白でAND（すべて含む）、-語で除外、~語 ~語でOR（いずれか）",
    "viewer.advanced_search.help_tooltip": "検索の使い方 (AIタグ・精度・表示単位・関連度) を表示",
    "viewer.advanced_search.image_file_filter": "画像ファイル ({patterns})",
    # 種別走査中はセグメント中立化 + この注記（項目#57/#156）。
    "viewer.advanced_search.media_note": "種別走査中はAIタグ・類似条件は使われません（種別を「すべて」か「画像」に戻すと再び有効になります）。",
    "viewer.advanced_search.media_tooltip": "結果を指定した種別のファイルに絞り込みます。\n「動画/音声/文書/アーカイブ」を選ぶとAIタグは使わず拡張子のみで\nフォルダ配下を再帰列挙します（タグは画像専用のため）。",
    # フィルタポップオーバーの「種別:」と同名・同 6 択で二重存在するため、
    # どちらの種別かを名乗らせる (UIレビュー 07-25 #41)。
    "viewer.advanced_search.media_type_label": "対象種別 (AI 検索):",
    # 呼称は「Snappix Tagger」1 本・パス表記 (tagger/) は廃止し、救済文は
    # 「AIタグの索引を作るには Snappix Tagger でスキャンしてください
    # （ヘルプ ▸ AIタグ検索のセットアップ…）」の定型へ統一 (UIレビュー 07-25 #30)。
    "viewer.advanced_search.missing_db_banner": "AIタグ・意味検索にはAIタグの索引 (tags.db) が必要です。AIタグの索引を作るには Snappix Tagger でスキャンしてください（ヘルプ ▸ AIタグ検索のセットアップ…）。",
    "viewer.advanced_search.mode_and": "AIタグ一致",
    "viewer.advanced_search.mode_and_tooltip": "指定したAIタグをすべて含む画像・フォルダを検索します（完全一致の絞り込み）。",
    "viewer.advanced_search.mode_rank": "AIタグで意味検索",
    "viewer.advanced_search.mode_rank_tooltip": "入力したAIタグに意味的に近い画像を関連度順に並べます（一致絞り込みではなくランキング。tags.db のベクトルを使用）。除外（-）はこのモードでは使われません。",
    "viewer.advanced_search.mode_similar": "画像で類似検索",
    "viewer.advanced_search.mode_similar_tooltip": "選んだ基準画像に見た目が似ている画像を関連度順に並べます。",
    # 同時に無効化される 2 モードを名指しする (UIレビュー 07-25 #65)。
    "viewer.advanced_search.no_vectors_short": "画像ベクトルがありません — 意味検索・類似検索には Snappix Tagger での再スキャンが必要です。",
    "viewer.advanced_search.panel_title": "詳細検索 (AIタグ・意味・類似)",
    "viewer.advanced_search.panel_title_needs_scan": "詳細検索 (AIタグは要スキャン)",
    "viewer.advanced_search.panel_title_no_engine": "詳細検索 (AI 検索プラグイン未読込)",
    "viewer.advanced_search.pick_image_btn": "画像を選ぶ…",
    "viewer.advanced_search.pick_image_dialog_title": "類似検索の基準にする画像を選ぶ",
    "viewer.advanced_search.pick_image_tooltip": "類似検索の基準にする画像をファイルから選びます。\n（下の「類似シード」欄に画像をドラッグ＆ドロップしてもOK）",
    "viewer.advanced_search.placeholder_no_db": "tags.db が無いため使用できません",
    "viewer.advanced_search.placeholder_short": "AIタグで検索",
    "viewer.advanced_search.precision_label": "AIタグの精度:",
    "viewer.advanced_search.precision_tooltip": "AIタグをどれだけ厳密に採用するか。小さいほどゆるく多く拾い、大きいほど確度の高いタグだけに絞ります（DB記録下限: {floor:.2f}）。",
    "viewer.advanced_search.rank_note": "タグ欄＝類似ランキングのシード（近い画像から順に表示）。除外（-）はランキングに反映されません",
    "viewer.advanced_search.rating_explicit": "R-18のみ",
    "viewer.advanced_search.rating_safe": "全年齢のみ",
    "viewer.advanced_search.rating_sfw": "R-15相当まで",
    # 「レーティング」→「年齢区分」へ統一 (UIレビュー 07-25 #94)。
    "viewer.advanced_search.rating_tooltip": "各画像の代表的な年齢区分（safe/questionable/explicit）で絞り込みます。",
    # AIタグ条件そのものを外す候補 (UIレビュー 07-25 #29)。
    "viewer.advanced_search.relax_ai_tags": "AIタグ条件「{terms}」を解除",
    "viewer.advanced_search.relax_coverage": "表示単位を『複数枚で分担可』に",
    "viewer.advanced_search.relax_date": "投稿日をすべてに",
    "viewer.advanced_search.relax_excludes": "除外タグを外す",
    # AI 検索結果にも「年齢制限を隠す」が効くようになったので、0 件の犯人候補
    # として解除ボタンを出す (UIレビュー 2026-08-28 N-04)。
    "viewer.advanced_search.relax_hide_nsfw": "「年齢制限を隠す」を解除",
    "viewer.advanced_search.relax_locked_only": "「ロックありのみ」を解除",
    "viewer.advanced_search.relax_name_filter": "名前フィルター「{query}」を解除",
    "viewer.advanced_search.relax_rating": "年齢区分をすべてに",
    "viewer.advanced_search.relax_threshold": "精度を {floor:.2f} まで下げる",
    "viewer.advanced_search.relevance_legend": "関連度 = 数値が大きいほど条件に近い",
    "viewer.advanced_search.reload_db_btn": "AIタグDBを再読み込み",
    "viewer.advanced_search.reload_db_tooltip": "Snappix Tagger でのスキャン後、ビューアを再起動せずに tags.db を読み込み直します。",
    "viewer.advanced_search.reset_link": "詳細条件のみリセット",
    "viewer.advanced_search.reset_link_tooltip": "AIタグ・精度・年齢区分・投稿日・表示単位・意味検索をリセットします（絞り込みテキスト・サブフォルダ検索は保持）。",
    "viewer.advanced_search.result_kind_media": "種別検索",
    "viewer.advanced_search.result_kind_tags": "タグ検索",
    "viewer.advanced_search.seed_clear_tooltip": "類似シードを解除します",
    "viewer.advanced_search.seed_cta": "画像をここにドラッグ＆ドロップ、または「画像を選ぶ…」で指定",
    "viewer.advanced_search.seed_label": "類似シード:",
    # (UIレビュー 2026-08-28 提案2 第2段) AI ポップオーバーの共有条件行。
    # 同じ軸を 2 面で編集できると「どちらが効いているのか」が読めなくなる
    # ので、AI 側は読み取り専用で「今なにと AND されるのか」だけを見せる。
    # 構文行は台帳（search_dimensions.token_expression）が組む等価表現。
    "viewer.advanced_search.shared_conditions": "フィルターの条件と AND: {items}",
    "viewer.advanced_search.shared_conditions_tooltip": "フィルターポップオーバーで適用中の条件です（ここでは変更できません）。変更はツールバーの「フィルター」から。",
    "viewer.advanced_search.shared_syntax": "構文で書くと: {expr}",
    "viewer.advanced_search.shared_syntax_tooltip": "同じ条件を絞り込み欄の構文で書いた場合の等価表現です（選択してコピーできます）。",
    "viewer.advanced_search.similar_clear_btn": "類似解除",
    # (UIレビュー 07-25 #115) ステータス行の ✓ / 🔍 / ⚠ 絵文字直書きを撤去。
    "viewer.advanced_search.status_engine_unavailable": "AI 検索を利用できません（プラグインの読み込みに失敗しています）",
    "viewer.advanced_search.status_not_image": "画像ファイルを指定してください",
    "viewer.advanced_search.status_result": "{label} — {count:,} 件",
    "viewer.advanced_search.status_result_ranked": "{kind} — {count:,} 件（近い順）",
    "viewer.advanced_search.status_searching": "詳細検索中…",
    "viewer.advanced_search.status_searching_similar": "類似画像を検索中…",
    "viewer.advanced_search.status_tag_db_error": "タグ DB を読み込めません",
    "viewer.advanced_search.status_vector_error": "意味検索ベクトルを読み込めません",
    # 入口（ボタン）と着地（ウィンドウタイトル）を同じ語に (UIレビュー 07-25 #103)。
    "viewer.advanced_search.tag_browse_btn": "AIタグ一覧…",
    "viewer.advanced_search.tag_browse_tooltip": "AIタグの一覧を開き、検索/除外に追加します。",
    "viewer.advanced_search.unit_file": "画像ごと",
    "viewer.advanced_search.unit_folder_coverage": "フォルダ（複数枚で分担可）",
    "viewer.advanced_search.unit_folder_strict": "フォルダ（1枚で全タグ）",
    # -- viewer.app.* -----------------------------------------------------
    # 項目#79: ベースフォルダに書き込めない起動失敗（Program Files 配下への
    # 展開・書込保護メディア等）の案内モーダル。ログも書けない状況なので、
    # これが唯一のユーザー向け診断になる。ログシンクの確立失敗（ディスク
    # 満杯・AV がログを掴んでいる等）も同じ受け皿へ来るため、「展開場所を
    # 変えろ」だけを案内しない（レビュー 2026-08-30 — その場合は展開先を
    # 変えても直らない）。
    "viewer.app.paths_error_body": "このフォルダに書き込めません。ディスクの空き容量とアクセス権を確認するか、書き込み可能な場所（デスクトップやドキュメントなど）へ展開してから、もう一度実行してください。\n\n{path}",
    "viewer.app.paths_error_title": "起動できません",
    # -- viewer.badge.* ---------------------------------------------------
    # バッジ語彙レジストリ（`viewer/_indicator.py` の `_BADGE_SPECS`）の
    # `i18n_name_key` — 図像そのものは同レジストリの `painter_fn` が唯一の
    # 情報源で、ここには**図像文字を書かない**（UIレビュー 08-28 提案1 /
    # `tests/test_i18n_no_pictographs.py` が機械検証）。他のバッジの正式名は
    # 既存キーを再利用している（お気に入り数 / スター / あとで見る / 未取得）。
    "viewer.badge.name_ghost": "到達できない行",
    "viewer.badge.name_relevance": "関連度",
    "viewer.badge.name_similar": "類似検索",
    "viewer.badge.name_thumb_fail": "サムネイルの警告",
    # -- viewer.bookmark_dialog.* ----------------------------------------
    "viewer.bookmark_dialog.add_hint": "追加はメニューの「ブックマーク ▸ ブックマークに追加」から行えます。名前はダブルクリックで変更できます。",
    "viewer.bookmark_dialog.empty_hint": "ブックマークはまだありません。メニューの「ブックマーク ▸ ブックマークに追加」から追加できます。",
    "viewer.bookmark_dialog.folder_not_found": "{path}\n(このフォルダは見つかりません)",
    "viewer.bookmark_dialog.title": "ブックマークを管理",
    # -- viewer.breadcrumb.* ---------------------------------------------
    "viewer.breadcrumb.ancestor_folders": "上位フォルダ",
    # -- viewer.cache_build_controller.* ---------------------------------
    "viewer.cache_build_controller.all_caches_disabled": "ディスクキャッシュ・アスペクト比キャッシュ・検索索引がすべて無効のため作成できません。先に有効化してください。",
    "viewer.cache_build_controller.and_search_index": "・検索索引",
    "viewer.cache_build_controller.aspect_disabled": "アスペクト比キャッシュが無効です。",
    # (UIレビュー 2026-09-11 N-29) モーダル側のチェックボックスは設定タブと
    # 同じ文言を貼っていたため「永続設定を編集している」と読めたが、実際は
    # 今回のビルドにしか効かない。専用の文言で射程を言い切る。
    "viewer.cache_build_controller.bg_check_once": "今回はバックグラウンドで実行",
    "viewer.cache_build_controller.bg_tooltip_once": "この選択は今回のビルドだけに効きます。既定値は 設定 ▸ キャッシュ の同名の設定です。",
    "viewer.cache_build_controller.build_cancelled_toast": "キャッシュ作成を中止しました（{done:,} 件処理済み）",
    "viewer.cache_build_controller.build_done_toast": "キャッシュ作成が完了しました（{ok:,} 件 / 対象 {total:,} 件）",
    "viewer.cache_build_controller.build_done_warn_toast": "キャッシュ作成完了: {ok:,} 件・失敗 {failed:,} 件（対象 {total:,} 件）。詳細はログを確認してください。",
    "viewer.cache_build_controller.build_running_body": "キャッシュ作成が実行中です。完了するか中断してから再度実行してください。",
    "viewer.cache_build_controller.build_title": "キャッシュ作成",
    "viewer.cache_build_controller.disk_disabled": "サムネイル ディスクキャッシュが無効のため、サムネイルは作成できません。アスペクト比{extra}のみ作成します。",
    "viewer.cache_build_controller.index_disabled": "検索索引が無効のため作成できません。",
    "viewer.cache_build_controller.modal_progress": "{phase}… {done} / {total}",
    # (UIレビュー 2026-09-11 N-50) 推奨を既定ボタンの「色」でしか示して
    # いなかったので、ラベル自身に書く。
    "viewer.cache_build_controller.mode_btn_aspect": "アスペクト比のみ（高速・推奨）",
    "viewer.cache_build_controller.mode_btn_full": "サムネイルも作成（完全）",
    "viewer.cache_build_controller.mode_btn_index": "検索索引のみ（最速）",
    # (UIレビュー 2026-09-11 N-50) 3 択の違い・速さ・効果を「モード名 —
    # 作るもの / 速さ / 効果」の 1 行 1 モードで並べる（旧値は 4 文の散文で
    # 内部用語を無定義のまま並べていた）。setInformativeText に入るので
    # 改行は \n（HTML は入れない = plain_text 契約）。
    "viewer.cache_build_controller.mode_info": "検索索引のみ（最速） — ファイル名と post.md の投稿リンクだけを索引化します。画像は一切読みません。\nアスペクト比のみ（高速・推奨） — 画像の先頭（ヘッダ）だけを読んで縦横比を記録します。タイルの配置が即座に決まり、サムネイルは初回表示時に作られます。\nサムネイルも作成（完全） — サムネイルまで作ってディスクに保存します。再訪時の表示が最速ですが、時間がかかります。\n※ どのモードでも検索索引は同時に作られます（post.md の投稿リンクは「完全」と「検索索引のみ」のみ）。",
    # (UIレビュー 2026-09-11 N-50) 対象フォルダがモーダルから読めなかった。
    "viewer.cache_build_controller.mode_prompt_with_target": "対象フォルダ: {path}\n作成する内容を選択してください。",
    "viewer.cache_build_controller.phase_aspect": "アスペクト比を解析",
    "viewer.cache_build_controller.phase_full": "サムネイルをキャッシュ",
    "viewer.cache_build_controller.phase_index": "検索索引を作成",
    "viewer.cache_build_controller.pick_folder_title": "キャッシュを作成するフォルダを選択",
    "viewer.cache_build_controller.scanning_folder": "フォルダを走査中…",
    # -- viewer.cache_build_status.* -------------------------------------
    "viewer.cache_build_status.paused_suffix": "（一時停止中）",
    "viewer.cache_build_status.progress": "{phase}中 {done:,} / {total:,}{suffix}",
    "viewer.cache_build_status.progress_indeterminate": "{phase}中…{suffix}",
    "viewer.cache_build_status.starting": "{phase}…",
    "viewer.cache_build_status.tooltip_pause": "一時停止",
    "viewer.cache_build_status.tooltip_resume": "再開",
    # -- viewer.common.* -------------------------------------------------
    "viewer.common.cache": "キャッシュ",
    # (UIレビュー 08-28 N-80) 同じ ``build_cache_interactive`` を呼ぶ 2 つの
    # 入口（診断メニュー / 設定 ▸ キャッシュ管理）が別名で並んでいた。
    # 呼称は 1 キーに統一する（別キーに複製すると必ず片方が腐る）。
    "viewer.common.cache_prebuild": "キャッシュを事前作成…",
    "viewer.common.fallback_thumb_note": (
        "代替サムネイル: このフォルダにはクリエイターアイコン以外の画像がありません"
    ),
    "viewer.common.filter_tags_placeholder": "タグを絞り込み…",
    "viewer.common.layout_justified": "ぴったり配置",
    # UIレビュー 07-25 #21: 「閲覧モード」を廃止し「全画面表示」へ一本化
    # （3 態を 分割ビュー / 最大化 / 全画面表示 の同一軸で呼ぶ）。キー名と
    # 内部識別子 ("lightbox") は不変 — 変えたのは表示文言だけ。F11 は
    # setShortcut でメニューのキー列に載るのでラベルには併記しない (#78)。
    "viewer.common.lightbox_mode": "全画面表示",
    "viewer.common.loading_folder": "フォルダを読み込んでいます…",
    "viewer.common.no_history": "(履歴なし)",
    "viewer.common.scan_error": (
        "フォルダを読み込めませんでした\n"
        "ネットワークドライブの接続やアクセス権を確認してください"
    ),
    "viewer.common.scan_error_reason": "理由: {error}",
    "viewer.common.similar_search_image": "この画像で類似検索",
    "viewer.common.sort_mtime_desc": "更新日時 (新しい順)",
    # (UIレビュー 2026-09-11 N-134) スライダが上限で止まる理由をその場に出す。
    # 設定への案内はリスト表示（上限 = 設定値）のときだけ — サムネイル表示の
    # 上限はペイン幅由来なので、設定を案内すると誤誘導になる。
    "viewer.common.thumb_size_tooltip": "{min}–{max} px。Ctrl+ホイールでも変えられます。",
    "viewer.common.thumb_size_tooltip_list": "{min}–{max} px。上限は 設定 ▸ 表示 で変更できます。Ctrl+ホイールでも変えられます。",
    # (UIレビュー 07-25 #136 / #13) ★・あとで見る・ユーザータグをホバーで
    # 言葉に展開する — ♡/🔒 と同じ扱い（ユーザータグはバッジすら無い）。
    "viewer.common.tooltip_favorites": "投稿のお気に入り {n} 件",
    "viewer.common.tooltip_later": "あとで見るに登録済み",
    "viewer.common.tooltip_locked": "未取得コンテンツ {n} 点（プラン外など）",
    "viewer.common.tooltip_star": "★ 自分のスター {n}",
    "viewer.common.tooltip_user_tags": "ユーザータグ: {tags}",
    "viewer.common.view_layout_tooltip": (
        "表示形式: ぴったり配置 (行を敷き詰め) / グリッド (正方形) / リスト"
    ),
    # (UIレビュー 2026-09-11 N-135) フィルターポップオーバーの
    # 「※ ここの条件は保存されません」の対。永続側にも同じ形の注記を置く。
    "viewer.common.view_settings_persistent_hint": "※ ここの設定は保存され、次回起動時も維持されます。",
    # -- viewer.content_view.* -------------------------------------------
    "viewer.content_view.col_filename": "ファイル名",
    "viewer.content_view.col_mtime": "更新日時",
    "viewer.content_view.col_ratio": "圧縮率",
    "viewer.content_view.edge_next_post_hint": "これが最後の画像です — Ctrl+→ で次の投稿へ",
    "viewer.content_view.edge_prev_post_hint": "これが最初の画像です — Ctrl+← で前の投稿へ",
    "viewer.content_view.empty_folder_body": "表示できるファイルやフォルダがありません。\n上の階層に戻るか、別のフォルダを開いてください。",
    "viewer.content_view.empty_folder_body_no_up": "表示できるファイルやフォルダがありません。\n別のフォルダを開いてください。",
    "viewer.content_view.empty_folder_heading": "このフォルダは空です",
    "viewer.content_view.empty_hint": "グリッドからフォルダやファイルを選んでください",
    "viewer.content_view.empty_quiet": "プレビューする項目がありません",
    "viewer.content_view.file_info_error": "ファイル情報を取得できません: {exc}",
    # 絶対パスは本文から外してツールチップ / 情報パネルへ寄せた（N-34）。
    "viewer.content_view.file_meta": "サイズ {size}・更新 {mtime:%Y-%m-%d %H:%M}",
    "viewer.content_view.image_error_heading": "画像を表示できません",
    # N-85: 最大化中は左のグリッドが幅 0 なので「グリッドから選んでください」が
    # 見えない面を指す行き止まりになる。主案内をここへ移し、戻り方を出す。
    "viewer.content_view.maximized_empty_body": "分割ビューに戻すと、左のグリッドからフォルダやファイルを選べます。",
    "viewer.content_view.maximized_empty_heading": "表示する項目が選ばれていません",
    # 走査失敗の主案内がプレビュー列へ退避したとき（最大化中）。「選ばれて
    # いません」では失敗を名乗れない — グリッドが持つ ⚠ カードと同じことを
    # 言う面が要る。
    "viewer.content_view.maximized_scan_error_body": "ネットワークドライブの接続やアクセス権を確認し、分割ビューに戻って再試行してください。",
    "viewer.content_view.maximized_scan_error_heading": "フォルダを読み込めませんでした",
    # 専用ビューを持たない形式のカード見出し（N-34 — 共通カード規格へ）。
    "viewer.content_view.no_preview_heading": "この形式はプレビューできません",
    # 失敗面はカード規格（見出し + 本文 + その場のボタン）。本文に逃げ道の
    # 場所を書かない — [既定アプリで開く] がカードの中にある。
    "viewer.content_view.pdf_error_heading": "PDFを表示できません",
    "viewer.content_view.pdf_load_error": "PDFを読み込めません: {name}",
    "viewer.content_view.pdf_load_error_code": "PDFを読み込めません: {name} ({code})",
    "viewer.content_view.pdf_reload": "PDFを再読み込み",
    "viewer.content_view.pdf_too_large": "サイズ: {size} — 上限（{limit_mib} MiB）を超えるためプレビューしません",
    "viewer.content_view.pdf_zoom_fit_width": "幅に合わせる",
    "viewer.content_view.pdf_zoom_fit_window": "表示領域に合わせる",
    "viewer.content_view.text_read_error": "ファイルを読み込めません: {exc}",
    "viewer.content_view.text_truncated": "\n\n… ({mib:g} MiB を超えるため切り捨てました — 全文は「既定アプリで開く」で確認してください)",
    "viewer.content_view.welcome_body": "フォルダを開いて画像の閲覧を始めましょう。\nローカルフォルダでもネットワーク（NAS）フォルダでも開けます。\nフォルダをこのウィンドウにドラッグ＆ドロップしても開けます。",
    "viewer.content_view.welcome_default_library": "この「library」フォルダは初期フォルダです。お好きなフォルダを開いて閲覧を始められます。",
    "viewer.content_view.welcome_heading": "Snappix Viewer へようこそ",
    "viewer.content_view.welcome_help": "操作の基本 (F1)",
    "viewer.content_view.zip_load_error": "ZIPを読み込めません: {message}",
    "viewer.content_view.zip_loading": "サイズ: {size} — 読み込み中…",
    "viewer.content_view.zip_open_button": "開いて閲覧",
    "viewer.content_view.zip_open_too_large_tooltip": "サイズが上限（{limit_mb} MB）を超えるため展開できません",
    "viewer.content_view.zip_open_tooltip": "展開してフォルダとして中身を閲覧します",
    "viewer.content_view.zip_summary": "{count} ファイル  合計: {total}{ratio}{omitted}",
    "viewer.content_view.zip_summary_omitted": "（先頭 {shown} 件のみ表示・他 {omitted} 件省略）",
    "viewer.content_view.zip_summary_ratio": " → {comp}",
    "viewer.content_view.zip_too_large": "サイズ: {size} — 大きすぎるため内容プレビューをスキップ",
    # -- viewer.context_menus.* ------------------------------------------
    "viewer.context_menus.copy_file": "ファイルをコピー",
    "viewer.context_menus.copy_file_done": "ファイルをコピーしました",
    # (UIレビュー 2026-09-11 N-90) 「ファイルをコピー」と「フルパスをコピー」の差が
    # ラベルから読めない — 用途をツールチップで添える。
    "viewer.context_menus.copy_file_hint": "ファイルそのものをクリップボードへ。エクスプローラに貼り付けると複製できます。",
    "viewer.context_menus.copy_full_path": "フルパスをコピー",
    "viewer.context_menus.copy_full_path_hint": "パスの文字列をクリップボードへ。チャットや検索欄に貼る用。",
    "viewer.context_menus.copy_path_done": "パスをコピーしました",
    # (UIレビュー 2026-09-11 N-84) フォルダの右クリックがアプリ外の 2 動詞
    # しか持たず、ダブルクリックと同じ「このビューアで中を開く」を頼めな
    # かった。項目名は共通の common.action.open を使い、OS へ出る 2 つとの
    # 差はツールチップで言う（N-90 の型）。
    "viewer.context_menus.open_here_hint": "このビューアでフォルダの中を開きます（エクスプローラは開きません）。",
    # (UIレビュー 08-28 N-64 / 2026-09-11 N-40) 横断一覧・検索ヒットの行から
    # 実体の置き場所へ戻るアプリ内導線。
    "viewer.context_menus.reveal_in_app": "このファイルの場所を開く",
    "viewer.context_menus.reveal_in_app_hint": "このファイルがあるフォルダをビューアで開いて選択します（エクスプローラは開きません）。",
    # -- viewer.curation_strip.* ----------------------------------------
    # (UIレビュー 2026-09-11 E2) ★ / あとで見る / ユーザータグ の操作面。
    "viewer.curation_strip.more_tags": "+{n}",
    "viewer.curation_strip.star_tooltip": "スター: 星をクリックで設定、同じ星をもう一度で解除（キー 0〜5）",
    "viewer.curation_strip.tags_tooltip": "ユーザータグ {n} 件 — クリックで編集…",
    "viewer.curation_strip.target_tooltip": "この印の対象: {path}",
    # -- viewer.detail_window.* ------------------------------------------
    "viewer.detail_window.cat_general": "一般",
    "viewer.detail_window.copy_all_tags": "タグをすべてコピー",
    "viewer.detail_window.copy_selected_tags": "選択したタグをコピー",
    "viewer.detail_window.count_filtered": "{shown} / {total} 件",
    "viewer.detail_window.count_total": "{total} 件",
    "viewer.detail_window.exif_datetime": "撮影日時",
    "viewer.detail_window.exif_exposure": "シャッター速度",
    "viewer.detail_window.exif_fnumber": "F値",
    "viewer.detail_window.exif_focal": "焦点距離",
    "viewer.detail_window.exif_iso": "ISO感度",
    "viewer.detail_window.exif_lens": "レンズ",
    "viewer.detail_window.exif_make": "メーカー",
    "viewer.detail_window.exif_model": "カメラ",
    # 値側の単位。ラベル（上の exif_*）がカタログなのに単位だけコード直書きで、
    # 「秒」は日本語ハードコードだった。数値の整形はコード、語はここ。
    "viewer.detail_window.exif_unit_fnumber": "F{n}",
    "viewer.detail_window.exif_unit_iso": "ISO {n}",
    "viewer.detail_window.exif_unit_mm": "{n} mm",
    "viewer.detail_window.exif_unit_seconds": "{n} 秒",
    # 「フロア」はタガー内部用語の直訳で日本語として意味が通らなかった
    # (UIレビュー 07-25 #44) — 短ラベル + ツールチップで意味を補う。
    "viewer.detail_window.floor": "記録しきい値",
    "viewer.detail_window.floor_tooltip": "このスコア以上のタグだけが記録されています。",
    "viewer.detail_window.info_group": "情報",
    "viewer.detail_window.later_yes": "登録済み",
    # 表示だけを切り詰めた合図（[コピー] は全文を渡す）。折り返し位置の無い
    # 長大な 1 行は QPlainTextEdit の初期レイアウトで GUI スレッドを止める。
    "viewer.detail_window.meta_chunk_truncated": "（長いため一部のみ表示）",
    "viewer.detail_window.metadata_group": "メタデータ",
    "viewer.detail_window.model": "モデル",
    "viewer.detail_window.no_selection": "項目が選択されていません",
    "viewer.detail_window.no_tags_recorded": "この画像にタグは記録されていません。",
    "viewer.detail_window.no_tagsdb": "tags.db が見つからないため、AIタグ情報は表示できません。",
    # 「タグスキャナー」呼称を廃し、救済文を定型へ (UIレビュー 07-25 #30)。
    "viewer.detail_window.no_tagsdb_full": "tags.db が見つからないため、AIタグ情報は表示できません。AIタグの索引を作るには Snappix Tagger でスキャンしてください（ヘルプ ▸ AIタグ検索のセットアップ…）。",
    "viewer.detail_window.not_image": "画像ファイルではないため、AIタグ情報はありません。",
    # 案内だけで次の一手が無かった (UIレビュー 07-25 #90) — 本体からプラグイン
    # （タガー）を起動する導線は持てないので、既存のヘルプ項目名で案内する。
    "viewer.detail_window.not_scanned": "この画像はまだスキャンされていないため、AIタグ情報がありません。AIタグの索引を作るには Snappix Tagger でスキャンしてください（ヘルプ ▸ AIタグ検索のセットアップ…）。",
    # 「レーティング」（AI 年齢帯）は利用者評価の「スター」と語感が衝突するため
    # 製品共通の「年齢区分」へ統一 (UIレビュー 07-25 #94)。
    "viewer.detail_window.rating": "年齢区分",
    "viewer.detail_window.resolution": "解像度",
    "viewer.detail_window.scanned_at": "スキャン日時",
    "viewer.detail_window.score": "スコア",
    "viewer.detail_window.search_these_tags": "これらのタグで検索",
    "viewer.detail_window.search_this_tag": "このタグで検索",
    "viewer.detail_window.select_image_hint": "グリッドや情報パネルで画像を選択すると、ここに詳細とAIタグが表示されます。",
    # 素の配布（AI パック無効）用の未選択案内 (UIレビュー 09-11 N-46) — 存在
    # しない面（AIタグ）を予告しない中立の文。``*_free`` 接尾の既存慣行に従う。
    "viewer.detail_window.select_image_hint_free": "グリッドや情報パネルで画像を選択すると、ここに詳細が表示されます。",
    # 「左ペイン」用語の最後の残存 (UIレビュー 07-25 #92 / 07-19 High#1)。
    "viewer.detail_window.similar_search_tooltip": "この画像に見た目が似た画像をグリッドに一覧表示します。",
    "viewer.detail_window.tag_count": "タグ数",
    # 値の中で母集団を明示する — すぐ下の絞り込み行の「N 件」は年齢区分込みの
    # 表の行数なので、素の数字だけだとどちらが何の数か画面から分からない。
    "viewer.detail_window.tag_count_value": "一般 {general} / 年齢区分 {rating}",
    "viewer.detail_window.title": "詳細情報",
    "viewer.detail_window.user_tags": "ユーザータグ",
    # -- viewer.file_list.* ----------------------------------------------
    "viewer.file_list.dblclick_tooltip": "ダブルクリックでプレビューを最大化",
    "viewer.file_list.empty_folder": "このフォルダにはファイルがありません",
    # (UIレビュー 07-25 #118) 検索 0 件では「選べるものが無いのに選べ」と言って
    # いた — 検索が空振りしている状況を名指しする専用文言へ分岐する。
    # 空状態オーケストレータの SECONDARY 文言 (N-119): 空フォルダ・空
    # ライブラリ・走査中など「そもそも選べる物が無い」局面で使う。命令形
    # （「選んでください」）を使わないのが要点 — 選べる物が無い席で「選べ」と
    # 指示しない。
    "viewer.file_list.empty_no_items": "ここに表示できる項目はありません",
    "viewer.file_list.empty_search_no_hits": "検索条件に一致する項目がないため、表示できるファイルがありません",
    "viewer.file_list.empty_unselected": "フォルダやファイルを選ぶと一覧が出ます",
    "viewer.file_list.header_contents": "内容",
    # 「配置」だけを名乗る陳腐化した記述だった — ⋯ ポップオーバーの実内容
    # （並び順・表示形式・サムネイルサイズ）へ同期 (UIレビュー 07-25 #103)。
    "viewer.file_list.options_tooltip": "表示オプション（並び順・表示形式・サムネイルサイズ）",
    # N-15: 実装（スキャン順・post.md を末尾へ降格）と正反対だったラベルを訂正。
    "viewer.file_list.sort_default": "既定 (スキャン順・post.md は末尾)",
    "viewer.file_list.sort_menu": "並び順",
    # N-131: 右一覧の並び順だけ方向表記が無く、左ペイン（全項目が方向つき）と
    # 書式が割れていた。「種別」単独は他所（行ラベル / 詳細情報）でも使うので
    # 値の変更ではなくこのキーを足す。実装は昇順固定。
    "viewer.file_list.sort_type_asc": "種別 (昇順)",
    # -- viewer.folder_preview_view.* ------------------------------------
    "viewer.folder_preview_view.children_heading": "中身（簡易一覧）",
    "viewer.folder_preview_view.children_heading_empty": "中身（簡易一覧） — 空のフォルダ",
    "viewer.folder_preview_view.children_heading_loading": "中身（簡易一覧） — 読み込み中…",
    "viewer.folder_preview_view.children_heading_scan_failed": "中身（簡易一覧） — 読み込み失敗: {message}",
    "viewer.folder_preview_view.children_heading_truncated": "中身（簡易一覧） — 先頭 {shown} 件のみ表示・他 {skipped} 件省略",
    "viewer.folder_preview_view.thumb_load_failed": "（読み込み失敗）",
    "viewer.folder_preview_view.thumb_non_image": "（{name}）",
    "viewer.folder_preview_view.thumb_none": "（サムネイルなし）",
    "viewer.folder_preview_view.thumb_unreadable": "（サムネイルを読み込めません）",
    # -- viewer.health_check.* -------------------------------------------
    "viewer.health_check.category_empty_folder": "空フォルダ",
    "viewer.health_check.category_missing_post_md": "post.md 欠落",
    "viewer.health_check.category_old_bloat": "old/ 肥大",
    "viewer.health_check.category_part": ".part 残骸",
    "viewer.health_check.category_unreadable": "未検査フォルダ",
    "viewer.health_check.category_zero_byte": "0 バイトファイル",
    "viewer.health_check.detail_empty_folder": "ファイルが 1 つもありません",
    "viewer.health_check.detail_link_not_followed": "リンク先は検査していません",
    "viewer.health_check.detail_missing_post_md": "コンテンツはあるが post.md がありません",
    "viewer.health_check.detail_old_bloat": "退避フォルダの合計サイズ {size}",
    "viewer.health_check.detail_part": "中断ダウンロードの .part 残骸",
    "viewer.health_check.detail_unreadable": "読み取れないため未検査です",
    "viewer.health_check.detail_zero_byte": "サイズ 0 バイト",
    "viewer.health_check.explain_empty_folder": "空フォルダ = ファイルが 1 つも無いフォルダ。削除して問題ありません。",
    "viewer.health_check.explain_empty_folder_plain": "空フォルダ = ファイルが 1 つも無いフォルダ。整理用に自分で作ったフォルダのこともあるため、一覧の表示のみで自動削除はしません。",
    "viewer.health_check.explain_missing_post_md": "post.md 欠落 = 画像などはあるのに投稿情報ファイル (post.md) が無いフォルダ。閲覧はできますが投稿メタは表示されません。",
    "viewer.health_check.explain_old_bloat": "old/ = 差分更新のときに旧版を退避するフォルダ。サイズ情報のみで自動削除はしません。不要なら手動で整理できます。",
    "viewer.health_check.explain_part": ".part = ダウンロード中断で残った書きかけファイル。削除して問題ありません。",
    "viewer.health_check.explain_unreadable": "未検査 = アクセス権限やオフラインの共有で一覧できなかったフォルダと、別の場所へのリンク（ジャンクション / シンボリックリンク）。この配下は検査していません（問題なしの保証はありません）。",
    "viewer.health_check.explain_zero_byte": "0 バイト = 中身が空のファイル。ダウンロード失敗の残骸のことが多く、削除して問題ありません。",
    # -- viewer.health_dialog.* ------------------------------------------
    "viewer.health_dialog.bulk_count_suffix": " ({n})",
    "viewer.health_dialog.cancelled_fmt": "中止（{n} フォルダ走査）",
    "viewer.health_dialog.category_head": "{label} ({n})",
    "viewer.health_dialog.category_head_explained": "{head} — {explanation}",
    "viewer.health_dialog.category_head_info": "{label} ({n})【情報】",
    "viewer.health_dialog.choose_target": "対象フォルダを選択…",
    "viewer.health_dialog.col_detected": "検出内容",
    "viewer.health_dialog.delete_all_empty": "空フォルダをすべて削除",
    "viewer.health_dialog.delete_all_parts": ".part をすべて削除",
    "viewer.health_dialog.delete_all_zero_byte": "0 バイトファイルをすべて削除",
    "viewer.health_dialog.delete_cancelled": "中止しました（{done} 件削除済み・残り {rest} 件）",
    "viewer.health_dialog.delete_confirm": "以下の {n} 件の{kind}を削除します。取り消せません。\n\n{examples}",
    "viewer.health_dialog.delete_failed_body": "{n} 件の削除に失敗しました:\n",
    "viewer.health_dialog.delete_failed_title": "削除に失敗しました",
    # (N-40) ごみ箱経由化は既決の見送り。せめて「戻せない」ことを言い切り、
    # 代わりに何を消したかの記録が残ることを同じ画面で伝える。
    "viewer.health_dialog.delete_no_trash": "削除したものはごみ箱には入りません（完全に削除されます）。削除したパスの一覧は data/logs の削除ログに記録されます。",
    "viewer.health_dialog.delete_title": "{label}の削除",
    "viewer.health_dialog.deleted_count": "{n} 件を削除しました",
    "viewer.health_dialog.deleted_errors_suffix": "（{n} 件は失敗）",
    "viewer.health_dialog.deleted_logged_suffix": "／削除ログに記録しました",
    "viewer.health_dialog.deleted_skipped_suffix": "（{n} 件はスキャン後に状態が変わった、または状態を確認できなかったためスキップ）",
    "viewer.health_dialog.deleting_label": "{label}を削除しています…",
    "viewer.health_dialog.detail_with_postref": "{detail}（{ref}）",
    "viewer.health_dialog.done_fmt": "完了（{n} フォルダ走査）",
    "viewer.health_dialog.more_examples": "\n  … 他 {n} 件",
    "viewer.health_dialog.more_rows": "… 他 {n} 件（一覧の表示は先頭 {shown} 件まで。一括削除はすべてが対象です）",
    "viewer.health_dialog.no_problems": "問題は見つかりませんでした。",
    "viewer.health_dialog.none_found": "{label}は見つかりませんでした",
    "viewer.health_dialog.open_log_folder": "削除ログを開く",
    "viewer.health_dialog.pick_target_title": "健全性チェックの対象フォルダを選択",
    "viewer.health_dialog.rescan": "再スキャン",
    "viewer.health_dialog.scan_failed_fmt": "走査失敗",
    "viewer.health_dialog.scan_failed_title": "健全性チェックに失敗しました",
    "viewer.health_dialog.scanning_fmt": "走査中… {n} フォルダ",
    "viewer.health_dialog.show_missing_post_md": "post.md が無いフォルダも表示",
    "viewer.health_dialog.show_missing_post_md_tooltip": "このライブラリには post.md が 1 つもありません。post.md 無しは本製品の基本の状態なので、「post.md 欠落」は既定で表示しません。",
    "viewer.health_dialog.summary_counts": "問題 {problems} 件・情報 {info} 件: ",
    "viewer.health_dialog.summary_part": "{label}: {n}",
    "viewer.health_dialog.summary_prefix": "{n} 件の問題: ",
    "viewer.health_dialog.target": "対象: {path}",
    "viewer.health_dialog.title": "ライブラリ健全性チェック",
    # -- viewer.image_view.* ---------------------------------------------
    "viewer.image_view.actual_size": "実寸表示 (Ctrl+0)",
    "viewer.image_view.copy_image": "画像をコピー (Ctrl+C)",
    "viewer.image_view.ctrl_fit_actual": "フィット / 実寸",
    "viewer.image_view.ctrl_fullscreen": "全画面 (F11)",
    # カプセルの ‹ › のラベルとツールチップ（ツールチップは UIレビュー
    # 2026-08-28 N-47 で追加）。ラベル ctrl_prev / ctrl_next はステージ
    # ヘッダーの項目送りボタンと**共有**しているので、そのままツールチップに
    # 流用すると「同形・同文言で別の軸」になる。カプセルは**ファイル送り**、
    # ヘッダーは**グリッドの項目送り**。
    "viewer.image_view.ctrl_next": "次へ",
    "viewer.image_view.ctrl_next_tooltip": "次のファイル（同じフォルダの画像・動画）へ移動します",
    "viewer.image_view.ctrl_prev": "前へ",
    "viewer.image_view.ctrl_prev_tooltip": "前のファイル（同じフォルダの画像・動画）へ移動します",
    "viewer.image_view.fit_to_window": "表示領域に合わせる (Ctrl+1)",
    # N-155: 回転 / 反転は非破壊（ファイルは書き換えない）。操作ガイド側は
    # 「（表示のみ）」と書いているのに右クリック側だけ無印だったので揃える。
    # トーストは足さない（R は連打される操作なので通知が連発する）。
    "viewer.image_view.flip_horizontal": "左右反転（表示のみ）(F)",
    # 右クリックの項目は正式名（キー併記）、ホバーカプセル / ステージヘッダーの
    # ボタンは短縮形「全画面 (F11)」— どちらも同じ機能の 1 軸の呼び分け
    # (UIレビュー 07-25 #21)。
    "viewer.image_view.fullscreen": "全画面表示 (F11)",
    # N-142: 全画面の中の ImageView は「入口」ではなく出口を出す
    # （set_fullscreen_exit_mode）。キー併記があるので
    # desc_exit_lightbox（「全画面表示を終了」）とは値が重ならない。
    "viewer.image_view.fullscreen_exit": "全画面表示を終了 (Esc)",
    "viewer.image_view.image_load_failed_error": "画像の読み込みに失敗: {error}",
    "viewer.image_view.image_load_failed_name": "画像の読み込みに失敗: {name}",
    # デコード失敗カードの主ボタン。押した結果は「この 1 枚を読み直す」なので、
    # F5（フォルダの再読み込み）の説明文とは別に持つ。
    "viewer.image_view.reload_image": "画像を再読み込み",
    "viewer.image_view.rotate_left": "左に回転（表示のみ）(Shift+R)",
    "viewer.image_view.rotate_right": "右に回転（表示のみ）(R)",
    "viewer.image_view.show_minimap": "ミニマップ表示",
    "viewer.image_view.zoom_persist": "ズーム維持",
    # -- viewer.info_panel.* ---------------------------------------------
    "viewer.info_panel.detail_star_value": "★ {n}",
    "viewer.info_panel.detail_title": "選択中のファイル",
    "viewer.info_panel.read_post_body": "本文を読む",
    "viewer.info_panel.section_title": "投稿情報",
    # -- viewer.library_dialog.* -----------------------------------------
    # (UIレビュー 2026-08-28 N-41) 同型の 3 ダイアログ（ブックマーク / 保存した
    # 検索 / ライブラリ）のうちライブラリだけ「名前を変更」を持たない。理由は
    # ``shared_prefs.library_roots`` がパスの一覧で表示名を持てない構造だから
    # だが、その制約がどこにも書かれていなかった — 下端ヒントに 1 文添える。
    "viewer.library_dialog.add_hint": "登録はメニューの「ファイル ▸ ライブラリ ▸ 現在のフォルダをライブラリに登録」から行えます。ライブラリの表示名はフォルダ名がそのまま使われます（名前の変更はできません）。",
    "viewer.library_dialog.empty_hint": "登録されたライブラリはまだありません。メニューの「ファイル ▸ ライブラリ ▸ 現在のフォルダをライブラリに登録」から登録できます。",
    "viewer.library_dialog.folder_not_found": "{path}\n(ライブラリのフォルダが見つかりません)",
    "viewer.library_dialog.title": "ライブラリを管理",
    # top_bar の書式キーは廃止 — 上部バーは投稿タイトル（フォルダ名）のみを
    # そのまま表示し、n/m + ファイル名は常時表示カウンタへ一本化した
    # (UIレビュー 07-25 #91)
    # -- viewer.lightbox.* -----------------------------------------------
    "viewer.lightbox.close_tooltip": "全画面表示を終了 (Esc / F11)",
    "viewer.lightbox.counter": "{index} / {total} ・ {name}",
    # スライドショー実行中の接頭（UIレビュー 2026-08-28 N-83①）。上部バーの
    # 再生 / 一時停止アイコンは 1.5 秒で自動消灯するので、消えない常設カウンタ
    # 側にも出す。**図像文字は使わず語で書く** — 図像の単一情報源は
    # ``_indicator`` のバッジ語彙レジストリで、カタログ値に絵文字・記号図像を
    # 書かない規約（test_i18n_no_pictographs）。
    "viewer.lightbox.counter_running": "スライドショー中 ・ {info}",
    # ★N 併記版（未評価 = star 0 のときは counter を使う）(UIレビュー 07-25 #46)
    "viewer.lightbox.counter_starred": "{index} / {total} ・ {name} ・ ★{star}",
    # 投稿横断の探索はワーカーで走る（NAS の大きなライブラリでは数秒かかる）。
    # 着地まで無表示だと「押したのに何も起きない」に見えるので予告を出す。
    "viewer.lightbox.cross_searching": "隣の投稿を探しています…",
    "viewer.lightbox.empty_folder": "このフォルダには表示できる画像・動画がありません",
    # N-86: 空プレイリストの常設カード本文（1.5 秒で消えるオーバーレイと違い
    # 出したままにする面 — 手掛かりが自発的に消えないようにする）。
    # N-52: 全画面のままフォルダを開く手段は無い（このページの可視アクションは
    # [終了] だけで、投稿横断も空プレイリストでは早期 return する）ので、
    # 実行できる順序どおりに言う。
    "viewer.lightbox.empty_folder_body": "全画面表示を終了して、別のフォルダを開いてください。",
    # 非メディア表示中の F11 は G05 のフォルダ流し見へフォールバックする。
    # 「別のファイルが無言で開いた」と読まれないよう着地時に一言告げる
    # （UIレビュー 2026-08-28 N-21）。
    "viewer.lightbox.folder_open_notice": "先頭の画像から再生します",
    # 1 項目 1 行のヒント: 前半 = キーボード / ホイール（既定は「ホイール =
    # 前後送り」で、ズームは Ctrl+ホイール — image_wheel_zoom 既定 False と
    # 整合）、後半 = マウスユーザー向けの再表示・一覧・評価
    # (UIレビュー 07-25 #8 / #93 / #46)。
    # N-122: 以前は「 ・ 」区切りの 2 行だったが、_HintOverlay は
    # setWordWrap(True) + adjustSize なので QLabel の優先幅で折り返され、
    # 1920px でも 1 項目が途中で折れていた（実測: 行の実幅 1168px に対して
    # ピル幅 664px）。改行で区切れば折り返しに委ねずに 1 項目 1 行になる。
    "viewer.lightbox.hint": (
        "Esc / F11 で全画面表示を終了\n"
        "←→ / Space で前後の画像\n"
        "ホイールで前後\n"
        "Ctrl+ホイールでズーム（設定で入替可）\n"
        "マウス移動で操作バー再表示\n"
        "画面下端で画像一覧\n"
        "S でスライドショー\n"
        "0〜5 でスター評価"
    ),
    # 項目#76: QtMultimedia の欠落で動画ページを構築できなかったときの降格
    # 告知（中央ペインの _lazy_view_unavailable と対）。無言だと「画面は前の
    # 画像のまま・index だけ進む」に見える。
    # (UIレビュー 2026-09-11 N-16) 全画面でも L / 右クリックで「あとで見る」を
    # 切り替えられる。トーストは裏に隠れるので ★ と同じ中央オーバーレイ。
    "viewer.lightbox.later_cleared": "「あとで見る」を解除しました",
    "viewer.lightbox.later_set": "「あとで見る」に追加しました",
    "viewer.lightbox.later_write_failed": "「あとで見る」を保存できませんでした",
    "viewer.lightbox.media_unavailable": "この動画は再生できません（メディア機能が利用できません）",
    "viewer.lightbox.no_next_post": "次の投稿はありません",
    "viewer.lightbox.no_prev_post": "前の投稿はありません",
    "viewer.lightbox.press_again_next": "もう一度 → で次の投稿へ",
    "viewer.lightbox.press_again_prev": "もう一度 ← で前の投稿へ",
    "viewer.lightbox.slideshow_finished": "スライドショーを終了しました",
    # 手動送りで自動送りを完全停止したときの告知（UIレビュー 2026-08-28
    # N-83② — 07-25 #26 の「タイマーは止めない」裁定をユーザー決定で改定）。
    # (UIレビュー 2026-08-28 N-139) [ / ] で間隔を ±1 秒。値は永続化せず、
    # スライドショー終了通知と同じ中央メッセージで新しい値だけ告げる。
    "viewer.lightbox.slideshow_interval": "スライドショー間隔 {n} 秒（この回だけ）",
    "viewer.lightbox.slideshow_stopped_manual": "手動で送ったためスライドショーを停止しました（S / 上部バーの再生ボタンで再開）",
    "viewer.lightbox.slideshow_tooltip": "スライドショー開始 / 停止 (S)",
    "viewer.lightbox.star_cleared": "スターを解除しました",
    # 書き込みが永続化できなかったとき (#53 残り): 全画面では親ウィンドウの
    # 警告トーストが裏に隠れて見えないので、中央オーバーレイで知らせる。
    "viewer.lightbox.star_write_failed": "スターを保存できませんでした",
    # -- viewer.main_window.* --------------------------------------------
    # UIレビュー 07-25 #124: 「同梱」は実際の導入形態（AI パック zip の展開）
    # と食い違う（本体には tagger 資産が同梱されない）ため訂正。なお、この
    # 文言自体が本体カタログに残るプラグイン依存文言の構造課題そのもの
    # （#123 — 棚卸しは報告のみ、本 PR では実装しない）。
    "viewer.main_window.about_body": "ローカル/ネットワークフォルダの大量の画像をスムーズに閲覧・検索・整理するためのビューアーです。\n\nAI パック付属の Snappix Tagger でライブラリをスキャンすると tags.db が作成され、「AI 検索」（ツールバーの AIタグチップ）で AIタグ・意味・類似画像による検索が使えます（「ヘルプ ▸ AIタグ検索のセットアップ…」を参照）。",
    "viewer.main_window.about_body_free": "ローカル/ネットワークフォルダの大量の画像をスムーズに閲覧・検索・整理するためのビューアーです。",
    "viewer.main_window.about_menu": "バージョン情報…",
    "viewer.main_window.about_title": "Snappix Viewer について",
    "viewer.main_window.ai_tag_search": "AIタグ検索（AI 検索ポップオーバー）",
    "viewer.main_window.back_tooltip_maximized": "分割ビュー（グリッド + プレビュー）に戻す (Alt+← / マウス戻るボタン)\n長押しで履歴一覧を表示",
    "viewer.main_window.bookmark_add_current": "ブックマークに追加",
    "viewer.main_window.bookmark_add_current_named": "ブックマークに追加（{name}）",
    "viewer.main_window.bookmark_added": "ブックマークに追加しました: {name}",
    "viewer.main_window.bookmark_already": "すでにブックマークに登録済みです: {name}",
    "viewer.main_window.bookmark_remove_current": "ブックマークから外す",
    "viewer.main_window.bookmark_removed": "ブックマークを削除しました",
    "viewer.main_window.cache_prebuild_hint": "NAS ライブラリを事前に走査してサムネイル等を作成し、初回閲覧を高速化します。",
    "viewer.main_window.copy_shown_image": "表示中の画像をコピー (画像表示中は Ctrl+C)",
    "viewer.main_window.counts": "表示中: フォルダ {folders} · ファイル {files}",
    "viewer.main_window.curation_later_list": "あとで見る一覧",
    "viewer.main_window.curation_later_list_hint": "ライブラリ全体の「あとで見る」を横断で一覧表示します。",
    "viewer.main_window.curation_starred_list": "スター付き一覧",
    "viewer.main_window.curation_starred_list_hint": "ライブラリ全体のスター付き項目を横断で一覧表示します。",
    # (UIレビュー 2026-08-28 N-71) キュレーション 3 軸のうちユーザータグにだけ
    # 横断の取り出し口が無かった — スター / あとで見ると同じ 1 つの面に載せる。
    "viewer.main_window.curation_tag_list": "ユーザータグ: {tag}",
    "viewer.main_window.curation_tag_list_hint": "ライブラリ全体からユーザータグ「{tag}」の項目を横断で一覧表示します。",
    # (UIレビュー 2026-08-28 N-62) 詳細情報ウィンドウの 2 つの検索導線は
    # 効果が背後の本窓にしか出ず、窓が重なっていると無反応に見えた。
    "viewer.main_window.detail_similar_search_toast": "「{name}」に似た画像を検索しています",
    "viewer.main_window.detail_tag_search_toast": "「{tag}」を検索条件に追加しました",
    "viewer.main_window.detail_window": "詳細情報…",
    # フォルダ選択中の代表画像プレビュー（UIレビュー 2026-08-28 N-104）。
    # 「代表:」の接頭は省略不可 — 無いと「このファイルが選択中」と誤読され、
    # 選択されているのはフォルダ、という事実と衝突する。
    "viewer.main_window.file_info_representative": "代表: {info}",
    "viewer.main_window.file_info_representative_tooltip": "フォルダの代表画像として表示中: {path}",
    "viewer.main_window.filter_left_pane": "絞り込み（検索ボックス）",
    "viewer.main_window.folder_not_found": "フォルダが見つかりません",
    "viewer.main_window.folder_not_found_body": "フォルダにアクセスできません。ネットワークドライブの接続やアクセス権、フォルダの移動・削除・名前変更をご確認ください。",
    "viewer.main_window.folder_not_found_open_other": "別のフォルダを開く",
    "viewer.main_window.folder_not_found_remove_bookmark": "このブックマークを削除",
    # ヘルプ先頭の「使い方」入口 (UIレビュー 08-28 N-120)。開くのは
    # ShortcutsDialog と同じ面 — 面を増やさず別名の導線だけを足す。
    "viewer.main_window.getting_started": "はじめに（操作の基本と一覧）…",
    "viewer.main_window.getting_started_hint": "マウス操作の決まり・画面の見かた・キーボードショートカット・バッジの意味をまとめた一覧を開きます。",
    "viewer.main_window.go_up": "上の階層へ",
    "viewer.main_window.health_check": "ライブラリ健全性チェック…",
    # 戻る/進む履歴ドロップダウンの見出し組み立て (UIレビュー 08-28 N-25)。
    # {name} には ZIP 名 / 「ライブラリ」/ フォルダ名、または一つ内側の
    # テンプレートの結果が入る（入れ子で組み立てる）。
    "viewer.main_window.history_filtered_label": "{name}（絞り込み中）",
    "viewer.main_window.history_recent_label": "{name} — 最近追加されたファイル",
    "viewer.main_window.history_search_label": "{name}（検索: {text}）",
    "viewer.main_window.history_selected_label": "{name} — {selected}",
    "viewer.main_window.history_stage_label": "{name}（最大化）",
    "viewer.main_window.image_copied": "画像をコピーしました",
    "viewer.main_window.info_panel_toggle": "情報パネルを表示",
    "viewer.main_window.info_panel_toggle_tooltip": "右の情報パネル（投稿メタ + ファイル一覧）の表示/非表示を切り替え (F8)",
    "viewer.main_window.library_menu": "ライブラリ",
    "viewer.main_window.library_menu_hint": "登録したライブラリ（閲覧の起点フォルダ）を切り替えます。最近開いたフォルダと違い、明示的に登録したものだけが並びます。",
    "viewer.main_window.library_register_current": "現在のフォルダをライブラリに登録",
    "viewer.main_window.library_registered": "ライブラリに登録しました",
    "viewer.main_window.load_complete": "読み込み完了",
    "viewer.main_window.load_failed": "読み込みに失敗しました",
    # 一覧は出せたが一部の項目を読み取れなかった（壊れたリンク等）。「完了」に
    # 添えて出す — 走査失敗と違い、読めた分はそのまま一覧に並んでいる。
    "viewer.main_window.load_partial": "一部の項目を読み取れませんでした",
    "viewer.main_window.menu_bookmarks": "ブックマーク(&B)",
    "viewer.main_window.menu_diagnostics": "診断(&D)",
    "viewer.main_window.menu_edit": "編集(&E)",
    "viewer.main_window.menu_file": "ファイル(&F)",
    "viewer.main_window.menu_search": "検索(&S)",
    "viewer.main_window.menu_view": "表示(&V)",
    # 「ナビゲーションレール」→「ナビレール」へ統一（design.md 用語表の正式名。
    # ショートカット一覧・docs・コードコメントの多数派に合わせる — #103）。
    "viewer.main_window.nav_rail_toggle": "ナビレールを表示",
    "viewer.main_window.nav_rail_toggle_tooltip": "左のナビレール（ライブラリ・スター/あとで見る・ブックマーク・保存した検索）の表示/非表示を切り替え (F7)",
    "viewer.main_window.no_bookmarks": "(ブックマークなし)",
    "viewer.main_window.no_copyable_image": "コピーできる画像がありません",
    "viewer.main_window.no_fullscreen_image": "全画面表示できる画像がありません（画像を選択してください）",
    "viewer.main_window.no_libraries": "(登録されたライブラリはありません)",
    "viewer.main_window.no_saved_searches": "(保存した検索はありません)",
    "viewer.main_window.open_default_failed": "既定のアプリで開けませんでした（ファイルの関連付けをご確認ください）",
    "viewer.main_window.open_folder": "フォルダを開く…",
    "viewer.main_window.open_logs_failed": "ログフォルダを開けませんでした",
    "viewer.main_window.open_logs_folder": "ログフォルダを開く",
    "viewer.main_window.perf_clear": "計測結果をクリア",
    "viewer.main_window.perf_measure_enable": "パフォーマンス計測を有効化",
    "viewer.main_window.perf_measure_off": "パフォーマンス計測: 無効",
    "viewer.main_window.perf_measure_on": "パフォーマンス計測: 有効",
    "viewer.main_window.perf_stats_show": "パフォーマンス統計を表示…",
    # UIレビュー 2026-08-28 N-07: 設定 / ブックマーク / 保存した検索の
    # 書き込みが落ちたときの常駐警告（``user_meta_unavailable`` と同じ様式）。
    "viewer.main_window.persist_failed": "設定・ブックマーク・保存した検索をディスクに書き込めません（変更はこのウィンドウを閉じると失われます）",
    # 本文中の投稿リンク（📁）を押したが索引の指すフォルダが既に無い場合。
    # 索引の postref 行は消されないので、リネーム / 削除後は押しても無反応に
    # 見えていた（項目#177）。
    "viewer.main_window.post_link_folder_missing": "リンク先のフォルダが見つかりません（移動または削除された可能性があります）",
    "viewer.main_window.preview_maximize_menu": "プレビューを最大化",
    "viewer.main_window.preview_split_menu": "分割ビューに戻す",
    "viewer.main_window.preview_toggle": "プレビューを表示",
    "viewer.main_window.preview_toggle_tooltip": "中央のプレビュー列の表示/非表示を切り替え (F6)",
    "viewer.main_window.recent_files": "最近追加されたファイル",
    "viewer.main_window.recent_files_hint": "選択中のフォルダ（未選択なら現在のフォルダ）配下のすべてのファイルを、ファイル自体の更新日時が新しい順に一覧表示します。投稿日ではないので、古い投稿に後から追加されたファイルも上に出ます。",
    "viewer.main_window.recent_folders": "最近開いたフォルダ",
    "viewer.main_window.reveal_failed": "エクスプローラで開けませんでした",
    "viewer.main_window.save_search": "この検索を保存…",
    "viewer.main_window.save_search_done": "検索を保存しました",
    "viewer.main_window.save_search_none": "保存できる検索条件がありません（先に検索を実行してください）。",
    "viewer.main_window.save_search_prompt": "この検索につける名前:",
    "viewer.main_window.saved_search_apply_failed": "保存した検索を適用できませんでした",
    "viewer.main_window.saved_search_unnamed": "(名称未設定)",
    "viewer.main_window.saved_searches": "保存した検索",
    "viewer.main_window.settings_applied": "設定を適用しました",
    # (UIレビュー 08-28 N-114) 同梱の手引き（配布フォルダ直下の同名テキスト）を
    # OS の既定アプリで開く。名前はファイル名そのもの — 配布物の中で探した人と
    # メニューで見つけた人が同じものだと分かるように。
    "viewer.main_window.shipped_readme": "はじめにお読みください…",
    "viewer.main_window.tagger_setup": "AIタグ検索のセットアップ…",
    "viewer.main_window.tagger_setup_title": "AIタグ検索のセットアップ",
    "viewer.main_window.tags_db_reload_gone": "AIタグDB (tags.db) が見つかりません",
    "viewer.main_window.tags_db_reloaded": "AIタグDBを再読み込みしました",
    "viewer.main_window.theme_extra_menu": "その他",
    "viewer.main_window.theme_menu": "テーマ",
    "viewer.main_window.theme_system": "システム（OS に追従）",
    # UIレビュー 07-25 #75: ディレクトリスキャン完了後もサムネイル生成は
    # 続く（冷えた NAS では数分）— 残件数を出して「まだ待つべきか」に答える。
    "viewer.main_window.thumbs_pending": "サムネイル生成中… {n} 件",
    # (#51) user_meta.db を開けないと★・ユーザータグ・「あとで見る」が無説明で
    # 消えていた（記録が全部失われたようにも見える）。理由つきで一度だけ知らせる。
    "viewer.main_window.user_meta_unavailable": "スター・ユーザータグ・「あとで見る」を保存できません（{reason}）",
    # (UIレビュー 2026-08-28 N-74) 「編集 ▸ あとで見る」(L) のツールチップ。
    "viewer.main_window.watch_later_hint": "操作中の面（グリッド / 右一覧 / プレビュー / 全画面）の項目の「あとで見る」を切り替えます。",
    "viewer.main_window.window_title": "Snappix Viewer",
    "viewer.main_window.window_title_root": "Snappix Viewer — {root}",
    # (UIレビュー 2026-08-28 N-149) セーフモード起動はログとプラグイン管理
    # ダイアログにしか出ておらず、自分で開かないと気づけなかった。
    "viewer.main_window.window_title_safe_mode": "{title}（セーフモード）",
    "viewer.main_window.window_title_zip": "Snappix Viewer — {name} (ZIP)",
    "viewer.main_window.zip_history_label": "{name} (ZIP)",
    # -- viewer.markdown_view.* ------------------------------------------
    "viewer.markdown_view.body_truncated": "… ({mib:g} MiB を超えるため切り捨てました — 全文は「既定アプリで開く」で確認してください)",
    "viewer.markdown_view.meta_creator": "作者",
    # UIレビュー #8: 指標はラベルではなく値側へ（ラベル列頭を揃える）。
    # 図像そのものは i18n に持たない — 情報パネルのメタカードが
    # `_indicator.badge_pixmap` の実物チップを値の前に貼る
    # (UIレビュー 08-28 N-52: 🔒 / ♡ の絵文字直書きはテーマにもフォントにも
    # 追従せず、light 系でコントラストが落ちていた)。
    "viewer.markdown_view.meta_favorites": "投稿のお気に入り数",
    "viewer.markdown_view.meta_favorites_value": "{n} 件",
    "viewer.markdown_view.meta_locked": "未取得",
    "viewer.markdown_view.meta_locked_value": "{n} 点",
    "viewer.markdown_view.meta_page": "投稿ページ",
    "viewer.markdown_view.meta_plan": "プラン",
    "viewer.markdown_view.meta_posted": "投稿日",
    "viewer.markdown_view.meta_service": "サービス",
    "viewer.markdown_view.no_headings": "(見出しなし)",
    "viewer.markdown_view.read_failed": "post.md の読み込みに失敗: {error}",
    "viewer.markdown_view.render_failed": "post.md の描画に失敗: {message}",
    "viewer.markdown_view.reset_font_size": "フォントサイズを既定に戻す",
    "viewer.markdown_view.toc_menu": "目次",
    # -- viewer.media_view.* ---------------------------------------------
    "viewer.media_view.loop_tooltip": "ループ再生",
    "viewer.media_view.mute_tooltip": "ミュート切替",
    "viewer.media_view.play_pause_tooltip": "再生 / 一時停止  (Space)",
    "viewer.media_view.playback_error": "再生エラー: {error}\n「既定アプリで開く」ボタンで外部プレイヤーをお試しください。",
    "viewer.media_view.playback_rate_tooltip": "再生速度",
    "viewer.media_view.volume_tooltip": "音量",
    # -- viewer.nav_rail.* -----------------------------------------------
    # (UIレビュー 2026-08-28 N-117) 行ラベルに「印を付けた件数」を併記する。
    # 実際に一覧へ並ぶ件数は、消えた / 読み取れなかった分だけ少なくなり得る
    # （N-09 の curation_error / curation_missing 参照）。
    "viewer.nav_rail.curation_row_count": "{label} {n}",
    "viewer.nav_rail.curation_row_count_hint": "印を付けた件数: {n}（実際に開ける件数は移動・削除・切断で減ることがあります）",
    # (UIレビュー 2026-08-28 N-106) 07-25 #57 で横断一覧はレールへ常設され、
    # N-70 でメニュー側も「編集 ▸ スター・あとで見る」へ移った。メニューは
    # レールを F7 で畳んだ利用者にとって唯一の入口なので**残す**が、常設の
    # 行き先がレールにあることは知らせる（同じ行き先の 2 入口だと分かる）。
    "viewer.nav_rail.curation_row_rail_hint": "同じ一覧は左のナビレール「スター・あとで見る」からも開けます (F7)",
    "viewer.nav_rail.empty_bookmarks": "ブックマークはまだありません",
    "viewer.nav_rail.empty_curation": "スターや「あとで見る」を付けると一覧できます",
    # N-128: user_meta.db を開けない環境では上の案内が誤案内（付けても保存
    # されない）。理由の詳細は起動時の警告トーストに任せ、ここは定型文だけ。
    "viewer.nav_rail.empty_curation_unavailable": "スター・あとで見る・ユーザータグを保存できません（user_meta.db を開けませんでした）",
    # (UIレビュー 09-11 N-25) 空状態が「無い」としか言わず、作り方がどこにも
    # 出ていなかった — 条件バーの入口を名指しする。
    "viewer.nav_rail.empty_saved_searches": "保存した検索はまだありません（検索すると条件バーに「この検索を保存…」が出ます）",
    "viewer.nav_rail.manage_bookmarks_tooltip": "ブックマークの管理ダイアログを開きます",
    "viewer.nav_rail.manage_libraries_tooltip": "ライブラリの管理ダイアログを開きます",
    "viewer.nav_rail.manage_saved_searches_tooltip": "保存した検索の管理ダイアログを開きます",
    "viewer.nav_rail.section_bookmarks": "ブックマーク",
    # (UIレビュー 2026-08-28 N-156) 他の 3 見出しは「その中に何が並ぶか」を
    # 名指ししているのに、ここだけ抽象カタカナだった。コード側の識別子
    # (curation / set_curation / CURATION_VIEWS) は据え置き（07-25 #21 と同じ流儀）。
    "viewer.nav_rail.section_curation": "印を付けたもの",
    # -- viewer.perf_dialog.* --------------------------------------------
    "viewer.perf_dialog.category_summary": "カテゴリ別サマリ",
    # UIレビュー 07-25 #82: 「クリア」ボタンは viewer.main_window.perf_clear
    # を再利用（perf_dialog.py 参照）— 専用キーを持たず表記の一致を構造的に
    # 保証する。
    "viewer.perf_dialog.col_avg": "平均",
    "viewer.perf_dialog.col_category": "カテゴリ",
    "viewer.perf_dialog.col_count": "回数",
    "viewer.perf_dialog.col_max": "最大",
    "viewer.perf_dialog.col_median": "中央値",
    "viewer.perf_dialog.col_min": "最小",
    "viewer.perf_dialog.col_time": "時間",
    "viewer.perf_dialog.col_total": "合計",
    "viewer.perf_dialog.copy_stats": "統計をコピー",
    # UIレビュー 07-25 #38: 「統計をコピー」に成功フィードバックが無かった。
    "viewer.perf_dialog.copy_stats_done_toast": "統計をクリップボードにコピーしました。",
    "viewer.perf_dialog.enable_measurement": "計測を有効化",
    "viewer.perf_dialog.hint": "計測を有効化してからグリッドでフォルダ操作を行うと、どこに時間を消費しているかが下の表に集計されます。\nNAS (SMB) の遅延は scan_children / folder_preview_scandir / thumb_decode に現れます。",
    "viewer.perf_dialog.recent_events": "最近のイベント (新しい順、最大200件)",
    "viewer.perf_dialog.status_recording": "記録中 · 経過 {elapsed:.1f} s · 累計 {total} 件",
    "viewer.perf_dialog.status_stopped": "停止中 · 累計 {total} 件",
    "viewer.perf_dialog.window_title": "ビューア パフォーマンス統計",
    # -- viewer.plugins.* -------------------------------------------------
    "viewer.plugins.activate_failed_body": "プラグイン「{name}」を読み込めなかったため、自動的に無効化しました。\n\n{error}\n\n原因を取り除いたあと、「ファイル ▸ プラグイン…」から再度有効化できます。",
    "viewer.plugins.activate_failed_title": "プラグインの読み込みに失敗しました",
    "viewer.plugins.broken_header": "読み込めないプラグイン",
    "viewer.plugins.changed_prompt_body": "以前に確認したプラグイン「{name}」が、別のフォルダから読み込まれようとしています。\n\n同じ名前を名乗る別のフォルダに入れ替わっている可能性があります（なりすましの恐れ）。プラグインはプログラムとして実行され、このPC上のあらゆる操作が可能です。信頼できる配布元から入手したものだと確認できる場合のみ有効化してください。\n\nこのフォルダのプラグインを有効化しますか？\n（あとで「ファイル ▸ プラグイン…」からいつでも変更できます）\n\n--- プラグインの申告 ---\n{name}  v{version}\n作者: {author}\nフォルダ: {folder}\n{description}",
    "viewer.plugins.changed_prompt_title": "プラグインのフォルダが変わりました",
    "viewer.plugins.col_name": "プラグイン",
    "viewer.plugins.col_status": "状態",
    "viewer.plugins.col_version": "バージョン",
    "viewer.plugins.crash_disabled_body": "前回の起動でプラグイン「{pid}」の読み込み中に異常終了したため、このプラグインを自動的に無効化しました。「ファイル ▸ プラグイン…」から再度有効化できます。",
    "viewer.plugins.declined_prompt_body": "このプラグインは、起動時の確認で「有効化しない」と選ばれたフォルダです。\n\n断ったときの理由（別のフォルダに入れ替わっている・同じ識別子のフォルダが複数ある）が解消されたかどうかを、本体は判別できません。プラグインはプログラムとして実行され、このPC上のあらゆる操作が可能です。信頼できる配布元から入手したものだと確認できる場合のみ有効化してください。\n\n「{folder}」のプラグインを有効化しますか？\n\n--- プラグインの申告 ---\n{name}  v{version}\n作者: {author}\nフォルダ: {folder}\n{description}",
    "viewer.plugins.declined_prompt_title": "一度「有効化しない」と答えたプラグインです",
    "viewer.plugins.detail_author": "作者: {author}",
    "viewer.plugins.detail_folder": "フォルダ: {folder}",
    "viewer.plugins.dialog_title": "プラグイン管理",
    "viewer.plugins.disabled_toast": "プラグイン「{name}」を無効化しました。",
    # 同一 id を名乗るフォルダが複数あるときの再確認。「別フォルダに入れ替わった」
    # （changed_prompt_*）とは状況が違う — 正規と詐称のどちらが勝者かを利用者が
    # 判断できるよう、勝者と敗者のフォルダ名を両方本文に出す。
    "viewer.plugins.duplicate_prompt_body": "同じ識別子を名乗るプラグインのフォルダが複数見つかりました。\n\n読み込もうとしているのは「{folder}」で、同じ識別子を名乗る次のフォルダは読み込まれません。\n{others}\n\nどちらが正規のものかを本体は判別できません（なりすましの恐れ）。プラグインはプログラムとして実行され、このPC上のあらゆる操作が可能です。信頼できる配布元から入手したものだと確認できる場合のみ有効化してください。\n\n「{folder}」のプラグインを有効化しますか？\n（あとで「ファイル ▸ プラグイン…」からいつでも変更できます）\n\n--- プラグインの申告 ---\n{name}  v{version}\n作者: {author}\nフォルダ: {folder}\n{description}",
    "viewer.plugins.duplicate_prompt_title": "同じ識別子のプラグインが複数あります",
    "viewer.plugins.enable_hint": "左端のチェックで有効 / 無効を切り替え、[OK] で確定します。",
    "viewer.plugins.enabled_toast": "プラグイン「{name}」を有効化しました。",
    "viewer.plugins.err_api_mismatch": "プラグインAPI v{plugin_api} 用のプラグインです（本体は v{host_api} に対応）。プラグイン側の更新が必要です。",
    "viewer.plugins.err_crash_last_run": "前回起動時の読み込み中に異常終了しました",
    "viewer.plugins.err_load_unresolved": "有効化されたフォルダを読み込めませんでした",
    "viewer.plugins.err_no_activate": "エントリーモジュールに activate(ctx) がありません",
    "viewer.plugins.load_failed_body": "有効になっていたプラグイン「{folder}」を読み込めませんでした。このプラグインの機能は今回の起動では使えません。\n\n{error}\n\n「ファイル ▸ プラグイン…」で状態を確認できます。",
    "viewer.plugins.load_failed_title": "プラグインを読み込めませんでした",
    # 2.5 節の告知の旧レコード版（folder 記録が無く、どのフォルダが
    # そのプラグインだったのかを突き合わせられないケース）。
    "viewer.plugins.load_failed_unresolved_body": "有効になっていたプラグイン「{pid}」を読み込めませんでした。このプラグインの機能は今回の起動では使えません。\n\n読み込めなかったフォルダ:\n{folders}\n\n「ファイル ▸ プラグイン…」で状態を確認できます。",
    "viewer.plugins.menu": "プラグイン…",
    "viewer.plugins.new_prompt_body": "plugins フォルダに新しいプラグインが見つかりました。\n\nプラグインはプログラムとして実行され、このPC上のあらゆる操作が可能です。信頼できる配布元から入手した場合のみ有効化してください。\n\nこのプラグインを有効化しますか？\n（あとで「ファイル ▸ プラグイン…」からいつでも変更できます）\n\n--- プラグインの申告 ---\n{name}  v{version}\n作者: {author}\nフォルダ: {folder}\n{description}",
    "viewer.plugins.new_prompt_title": "新しいプラグインが見つかりました",
    "viewer.plugins.no_plugins_hint": "プラグインはまだありません。plugins フォルダにプラグインを置くと、ここに表示されます。",
    "viewer.plugins.open_folder_btn": "プラグインフォルダを開く",
    "viewer.plugins.restart_note": "変更の一部は次回起動時に反映されます。",
    "viewer.plugins.safe_mode_note": "セーフモード (--no-plugins) で起動中のため、プラグインは読み込まれていません。設定の変更は次回の通常起動から反映されます。",
    "viewer.plugins.security_note": "プラグインはプログラムとして実行され、このPC上のあらゆる操作が可能です。信頼できる配布元から入手したプラグインだけを有効化してください。",
    "viewer.plugins.status_active": "有効",
    "viewer.plugins.status_disabled": "無効",
    "viewer.plugins.status_enabled_inactive": "有効 (再起動後に反映)",
    "viewer.plugins.status_new": "未有効化 (新規)",
    # チェックを切り替えた直後の「まだ確定していない」状態 (UIレビュー 08-28
    # N-36)。有効/無効フラグが store に届くのは OK 押下時なので、それまでは
    # 状態列にもそう書く。
    "viewer.plugins.status_pending_disable": "無効化予定 (OK で確定)",
    "viewer.plugins.status_pending_enable": "有効化予定 (OK で確定)",
    # (N-01) 初回信頼確認の動詞ラベル。ここで同意しているのは「このフォルダの
    # 任意のコードをビューアのプロセス内で実行してよい」ことなので、
    # 「はい」/「いいえ」では何に同意したのかがボタンに残らない。
    "viewer.plugins.trust_decline_btn": "有効化しない",
    "viewer.plugins.trust_enable_btn": "有効化する",
    # -- viewer.post_grid.* ----------------------------------------------
    "viewer.post_grid.back_tooltip": "1 つ前のフォルダに戻る (Alt+← / マウス戻るボタン)\n長押しで履歴一覧を表示",
    "viewer.post_grid.banner_count": "{n:,}件",
    "viewer.post_grid.chip_remove_tooltip": "「{label}」を解除",
    # (UIレビュー 07-25 #64) 本体クリックで編集できない条件チップの補足。
    "viewer.post_grid.chip_static_hint": "この条件は「×」で解除できます",
    "viewer.post_grid.clear_all": "すべて解除",
    "viewer.post_grid.clear_all_tooltip": "絞り込み・AIタグ検索・関連度順などの検索状態をすべて解除します (Esc)",
    "viewer.post_grid.clear_all_tooltip_free": "絞り込みなどの検索状態をすべて解除します (Esc)",
    # クリック規約のセッション初回ヒント (UIレビュー 2026-08-28 N-146)。
    "viewer.post_grid.click_hint": "クリック = 選択してプレビュー ・ ダブルクリック = 開く",
    # (UIレビュー 2026-08-28 N-158) 検索欄が同じ語を見せている間は search
    # チップを落とす（07-25 #15 — × の二重化を避ける）。その結果チップが
    # 0 本になると「件数 + すべて解除」だけの空帯に見えていたので、何で
    # 絞り込み中かを非クリックの薄いラベルで示す。
    "viewer.post_grid.condition_bar_name_filter_only": "検索欄の語で絞り込み中",
    # チップ行がバー幅を超えると全チップが省略記号だけに潰れ、どの × が何を
    # 落とすか判別不能になる（押せるので誤クリックの実害がある）。入り切らない
    # 分はまとめチップへ送り、中身はツールチップで名乗る。
    "viewer.post_grid.condition_overflow": "他 {n} 件",
    "viewer.post_grid.condition_overflow_tooltip": "表示しきれない条件:\n{items}\n個別の解除はフィルターから、まとめて外すなら「すべて解除」を使います。",
    "viewer.post_grid.count_filtered": "({total} 件中 {shown} 件)",
    "viewer.post_grid.count_filtered_shallow": "({total} 件中 {shown} 件・このフォルダのみ)",
    # (UIレビュー 07-25 #130) 「年齢制限を隠す」は永続設定で画面に痕跡が無く、
    # 部分適用中は「数が合わない」としか見えなかった — 件数表示に併記する。
    "viewer.post_grid.count_nsfw_hidden": " ほか年齢制限で {n} 件非表示",
    "viewer.post_grid.count_suffix": "({n} 件)",
    "viewer.post_grid.count_suffix_recursive": "({n} 件・サブフォルダも)",
    "viewer.post_grid.count_suffix_shallow": "({n} 件・このフォルダのみ)",
    "viewer.post_grid.curation_close": "一覧を閉じる",
    "viewer.post_grid.curation_empty_later": "「あとで見る」に登録された項目はまだありません\nグリッドで L キー、または右クリック ▸ あとで見る で、ここに集まります",
    # N-122: キーの範囲表記は `0〜5`（全角チルダ）に統一する。ここは 0 を含めた
    # 範囲を打てる説明なので `1〜5` ではなく `0〜5`（`desc_badge_star` の `1〜5`
    # はキーではなく★の値域の凡例なので別）。
    "viewer.post_grid.curation_empty_starred": "スターを付けた項目はまだありません\nグリッドで 0〜5 キー（0 で解除）、または右クリック ▸ スター で、ここに集まります",
    # (レビュー 2026-09-03 項目 #102) ユーザータグ一覧の母集合が 0 件のとき、
    # ★の付け方を案内すると開いている一覧とは別の軸を教えることになる。
    "viewer.post_grid.curation_empty_tag": "ユーザータグ「{tag}」を付けた項目はまだありません\nグリッドで右クリック ▸ ユーザータグを編集… で、ここに集まります",
    # (UIレビュー 2026-08-28 N-09) 1 件も読めなかったときは「まだありません」と
    # 言わない — 共有の切断を「印を付けていない」と読ませてしまう。最近追加
    # 一覧の recent_error と対の文言。
    "viewer.post_grid.curation_error": "一覧の項目を読み取れませんでした（移動・削除、またはネットワークの切断）",
    "viewer.post_grid.curation_failed": "横断一覧 — 読み取りに失敗しました",
    # (#133 項目 3) 到達不能な行のプレースホルダタイルのキャプション。missing /
    # unreadable の言い分けはステータス行 (curation_missing / curation_unreadable)
    # と同じ基準 — 「消えた」と断定してよいのは確実に不在と分かった分だけ。
    "viewer.post_grid.curation_ghost_missing": "{name}（見つかりません）",
    # (#133 R6) 実体が確実に消えた行を一覧から片付ける導線。現在の一覧種別の
    # 印（★ / あとで見る / タグ）だけを外す — 他の軸の表明には触れない。
    "viewer.post_grid.curation_ghost_remove": "この一覧から外す",
    # プレースホルダタイルのツールチップ先頭行 — 張り替えが右クリックの奥に
    # しか無く画面上の手掛かりがゼロだった（UIレビュー 2026-09-11 N-56）。
    # 項目名は curation_rebind_action と一字一句揃えること。
    "viewer.post_grid.curation_ghost_tooltip_hint":
        "右クリック ▸ 現在の場所を指定… で今の場所に付け替えられます",
    "viewer.post_grid.curation_ghost_unreadable": "{name}（読み取れません）",
    "viewer.post_grid.curation_loading": "一覧を読み込み中…",
    "viewer.post_grid.curation_missing": "{n} 件は見つかりませんでした（移動または削除済み）",
    # (#133 項目 3) プレースホルダタイル右クリックの張り替え導線。
    "viewer.post_grid.curation_rebind_action": "現在の場所を指定…",
    # (#133 R6) 張り替え先に既存キュレーションがあるときの併合確認モーダル。
    # 併合は不可逆（行き先の独自の★等が上書きされ得る）なので、両側の内容を
    # 見せてから動詞ボタンで確定させる（confirm_action 規約）。
    "viewer.post_grid.curation_rebind_merge_accept": "併合する",
    "viewer.post_grid.curation_rebind_merge_body": "指定した場所には既に別のキュレーションが付いています。\n\n元の行「{old_name}」: {old}\n指定先「{new_name}」: {new}\n\n併合すると、スターは大きい方・「あとで見る」はどちらかにあれば残り・タグは合算になります。この操作は元に戻せません。",
    "viewer.post_grid.curation_rebind_merge_none": "（キュレーションなし）",
    "viewer.post_grid.curation_rebind_merge_title": "キュレーションを併合しますか?",
    "viewer.post_grid.curation_rebind_pick_file": "「{name}」の現在の場所（ファイル）を選択",
    "viewer.post_grid.curation_rebind_pick_folder": "「{name}」の現在の場所（フォルダ）を選択",
    # 失敗の単位はボリューム（ドライブレターの付け替え・ライブラリごとの移動）
    # なので、行単位の張り替えだけだと数百回のモーダル往復になる。対象は
    # 「解決が到達不能と確定させた行」だけ — 同じ根の下で生きている行は動かない。
    "viewer.post_grid.curation_rebind_prefix_accept": "まとめて張り替える",
    "viewer.post_grid.curation_rebind_prefix_action": "同じ場所にあった {n} 件をまとめて指定…",
    # 併合は取り消せないので、巻き込む件数は 0 件のときも必ず書く。
    "viewer.post_grid.curation_rebind_prefix_body": "到達できなくなった {n} 件を、まとめて新しい場所へ付け替えます。\n\n元の場所: {old}\n新しい場所: {new}\n\nこのうち {merges} 件は、新しい場所に既に付いているキュレーションと併合されます（スターは大きい方・「あとで見る」はどちらかにあれば残り・タグは合算）。この操作は元に戻せません。",
    "viewer.post_grid.curation_rebind_prefix_pick": "「{name}」以下がまとめて移った先のフォルダを選択",
    "viewer.post_grid.curation_rebind_prefix_title": "まとめて張り替えますか?",
    # (UIレビュー 2026-08-28 N-09 / 旧 N-68) 「読めなかった」を「消えた」と
    # 断定しない — 消えたと確認できた分だけが curation_missing。
    "viewer.post_grid.curation_unreadable": "{n} 件は読み取れませんでした（ネットワークの切断など）",
    # (#53) 書き込みが永続化できなかったのに「★★★」の成功トーストが出ていた。
    # (UIレビュー 2026-09-11 N-129) 「保存できませんでした」だけでは理由も
    # 次の一手も分からない — 利用者が自分で確かめられる 1 行を添える。
    "viewer.post_grid.curation_write_failed_toast":
        "変更を保存できませんでした: {name}\n"
        "保存先（data フォルダ）の空き容量とアクセス権を確認してください",
    "viewer.post_grid.date_1y": "直近1年",
    "viewer.post_grid.date_30d": "直近30日",
    "viewer.post_grid.date_7d": "直近7日",
    "viewer.post_grid.date_range": "期間指定…",
    "viewer.post_grid.date_today": "今日",
    "viewer.post_grid.dim_ai_similar": "AIタグ類似検索: {terms}",
    "viewer.post_grid.dim_ai_tags": "AIタグ: {terms}",
    # (UIレビュー 07-25 #64) 横断一覧チップは「あとで見る」フィルタチップと
    # 2 文字差で並びうるうえ挙動も違う — 専用のプレフィクスで見分けられるように。
    "viewer.post_grid.dim_curation_view": "横断一覧: {name}",
    "viewer.post_grid.dim_date": "投稿日: {value}",
    "viewer.post_grid.dim_filter": "絞り込み: \"{text}\"",
    "viewer.post_grid.dim_media": "種別: {value}",
    "viewer.post_grid.dim_media_recursive": "種別(再帰): {value}",
    # (UIレビュー 2026-08-28 N-99) ラベルを用語表の正式名「年齢区分」へ統一
    # （AI ポップオーバーの age_band_label と同語）。
    "viewer.post_grid.dim_rating": "年齢区分: {value}",
    # 「最近追加されたファイル」一覧（フォルダ配下の全ファイルをファイル更新日時の
    # 新しい順にフラット表示）の条件チップ。
    "viewer.post_grid.dim_recent_view": "最近追加: {name}",
    "viewer.post_grid.dim_similar_image": "類似画像: {name}",
    "viewer.post_grid.dim_unit": "表示単位: {value}",
    # (UIレビュー 07-25 #13②) ユーザータグ絞り込みの条件チップ。
    "viewer.post_grid.dim_usertag": "ユーザータグ: {value}",
    "viewer.post_grid.edit_user_tags": "ユーザータグを編集…",
    "viewer.post_grid.edit_user_tags_label": "カンマまたは空白区切りでタグを入力\n（例: 風景, 構図参考, 後で印刷）",
    "viewer.post_grid.edit_user_tags_title": "ユーザータグを編集",
    # (UIレビュー 07-25 #31) 実際には検索欄・フィルタ・AI 検索まで全次元を解除
    # するボタン — ラベルが実効果より狭かったので実態に合わせる。
    "viewer.post_grid.empty_clear_filter": "検索条件をすべて解除",
    "viewer.post_grid.empty_filtered": "絞り込みに一致する項目がありません",
    # (UIレビュー 09-11 N-27) 分野限定条件（本文・投稿タグ等）は post.md 由来
    # なので、直下に post.md を持つフォルダが無ければ必ず 0 件になる。post.md
    # が無いのは異常ではないので、事実だけを中立に述べる。
    "viewer.post_grid.empty_filtered_no_post_md": "このフォルダには post.md がないため、「本文」などの分野限定条件には一致しません。",
    # (UIレビュー 07-25 #26) 名前フィルタ 0 件の真因は多くの場合「検索範囲＝
    # 表示中のフォルダだけ」— 見出しでそれを名指しし、回復導線を添える。
    "viewer.post_grid.empty_filtered_shallow": "このフォルダの中には一致する項目がありません\n（検索範囲は表示中のフォルダのみです）",
    "viewer.post_grid.empty_folder": "このフォルダは空です",
    "viewer.post_grid.empty_nsfw_action": "隠さずに表示",
    "viewer.post_grid.empty_nsfw_hidden": "「グリッドで年齢制限を隠す」の設定により、すべての項目が非表示になっています",
    # (UIレビュー 07-25 #26) 0 件カードの回復ボタン。
    "viewer.post_grid.empty_recursive_retry": "サブフォルダも検索して再試行",
    "viewer.post_grid.empty_searching": "検索中…\n結果が見つかるとここに表示されます",
    "viewer.post_grid.exclude_thumb": "クリエイターアイコンを隠す",
    "viewer.post_grid.exclude_thumb_tooltip": "クリエイターアイコン（`#thumb#` プレフィックスのファイル）を非表示にします。\nフォルダのサムネイルはアイコンを除いた次の画像を表示します。\n代替画像が無い場合は #thumb# 画像を赤枠で表示します。",
    "viewer.post_grid.field_scope_note": "「tags:」などの分野限定条件は直下のフォルダにのみ適用されます。配下はファイル名・フォルダ名だけで照合するため、配下のヒットはその条件を満たしていない場合があります。",
    # (UIレビュー 07-25 #40) ロックありのみを含む全絞り込み軸の入口。
    # #103: 実際の絞り込み軸（★ / あとで見る / ユーザータグを含む）を列挙する。
    "viewer.post_grid.filter_bar_tooltip": "種別・投稿日・★・あとで見る・ユーザータグ・ロックで現在の表示を絞り込みます（検索とは別。起動時は全解除）。",
    # 絞り込み構文ヘルプ — 「分野を限定」「コントロール」の**軸の一覧**は
    # 条件次元レジストリ（search_dimensions.filter_help_html）が接頭辞台帳
    # TOKEN_FIELDS + prefix_* 説明キーから生成する。ここに残るのは構文表・
    # 見出し・脚注の静的断片のみ（接頭辞補完と同じ行・同じ説明が並ぶ）。
    # (UIレビュー 2026-08-28 N-116) 絞り込み欄の初回フォーカスで自動表示する
    # 短縮版の 2 行。全文（分野限定表・コントロール表・脚注）は「?」側に
    # 残し、ここでは誘導と**閉じ方**だけを添える（閉じ方が画面のどこにも
    # 書かれていなかったのが指摘の後半）。
    "viewer.post_grid.filter_help_brief_dismiss": "<div style='margin-top:4px'><i>Esc または入力で閉じます。</i></div>",
    "viewer.post_grid.filter_help_brief_more": "<div style='margin-top:4px'>分野を限定する <code>分野:語</code> や <code>type:</code> などは検索欄右端のヘルプから。</div>",
    "viewer.post_grid.filter_help_controls_heading": "<b>コントロール</b>（フィルター / AI 検索ポップオーバーの操作と等価）",
    "viewer.post_grid.filter_help_controls_heading_free": "<b>コントロール</b>（フィルターポップオーバーの操作と等価）",
    "viewer.post_grid.filter_help_fields_heading": "<b>分野を限定</b>（<code>分野:語</code> / 除外は <code>-分野:語</code>）",
    "viewer.post_grid.filter_help_footer": "「サブフォルダも検索」をオンにすると、直下だけでなく配下のフォルダ・ファイル名も再帰的に検索します（分野限定は直下のみ）。<br><i>例: <code>body:風景 -plan_price:0</code> / <code>~猫 ~犬 type:画像</code></i>",
    "viewer.post_grid.filter_help_footer_free": "「サブフォルダも検索」をオンにすると、直下だけでなく配下のフォルダ・ファイル名も再帰的に検索します。",
    "viewer.post_grid.filter_help_syntax_html": "<b>絞り込み構文</b><br><table cellspacing='3'><tr><td><code>語1 語2</code></td><td>空白区切り＝すべて含む (AND)</td></tr><tr><td><code>-語</code></td><td>その語を含むものを除外</td></tr><tr><td><code>~語1 ~語2</code></td><td>いずれかを含む (OR)。~なしの語とは AND</td></tr></table>",
    "viewer.post_grid.filter_help_tooltip": "絞り込み構文のヘルプを表示",
    # (UIレビュー 07-25 #3) 欄幅拡大（案A）に合わせた短縮版（案B）。
    # 裸の「タグ」は使わない（search.md の用語不変 — ここで照合されるのは
    # post.md 由来の投稿タグで、tags.db 由来の AIタグではない。N-83）。
    "viewer.post_grid.filter_placeholder": "名前・タイトル・投稿タグで検索 (Ctrl+F)",
    # (UIレビュー 07-25 #14) AI 検索が結果を持っている間、この欄は AI ヒットへの
    # 名前オーバーレイでしかない — 常時見えるプレースホルダでそう名乗る。
    "viewer.post_grid.filter_placeholder_ai": "AI結果を名前で絞り込み",
    # (UIレビュー 2026-08-28 N-137) このポップオーバーの値は保存されない。
    # 機序は 2 つ（フォルダ移動での解除は設定で無効化でき、再起動リセットは
    # 常に効く）ので、「終了時に解除されます」のような一語にまとめない。
    # (UIレビュー 2026-09-11) 「設定で変更可」が参照先を示していなかった。
    # 実パスは 設定 ▸ 表示 ▸ ナビゲーション で一意に確定する。
    "viewer.post_grid.filter_popover_volatile_hint": "※ ここの条件は保存されません。フォルダを移動すると解除され（設定 ▸ 表示 ▸ ナビゲーション で変更できます）、アプリを閉じるとリセットされます。",
    "viewer.post_grid.filter_results_hint": "{key} で結果（グリッド）へ移動します。",
    # (UIレビュー 07-25 #110) 20 行超のホバーツールチップが画面を覆っていた —
    # 1〜2 行に短縮し、詳細は既存の構文ヘルプ（ポップオーバー）へ誘導する。
    "viewer.post_grid.filter_tooltip": "フォルダ名・タイトル・投稿タグで絞り込みます（空白区切りで AND、「-語」で除外）。\n分野限定（body: / tags: / star: など）や AIタグ検索の詳細は、検索欄右端のヘルプへ。",
    "viewer.post_grid.filter_tooltip_free": "フォルダ名・タイトル・投稿タグで絞り込みます（空白区切りで AND、「-語」で除外）。\n分野限定（body: / tags: / star: など）の詳細は、検索欄右端のヘルプへ。",
    "viewer.post_grid.filterbar_date_tooltip": "投稿日（post.md の投稿日時）で絞り込みます。\n投稿日が不明なファイル/フォルダは除外されません。",
    # N-115: 絞り込みの「あとで見る」は**読み取り条件**なのに、右クリックの
    # 書き込み命令（``watch_later_menu``）と同一文言だった。「ロックありのみ」
    # の先例に倣い、条件の側だけ専用の語にする（トークン ``later:`` や
    # 永続キーは不変 — 変えるのは表示文言だけ）。
    "viewer.post_grid.filterbar_later_label": "あとで見るのみ",
    "viewer.post_grid.filterbar_later_tooltip": "「あとで見る」を付けた項目だけに絞り込みます（その場フィルター）。",
    "viewer.post_grid.filterbar_media_tooltip": "現在表示中の項目を、選んだ種別のファイルだけに絞り込みます（その場フィルター）。\n配下を再帰的に探すには、AI 検索（AIタグ チップ）の種別を使います。",
    # (UIレビュー 2026-08-28 N-51) 素の配布に AI 検索は存在しないので、
    # 再帰の代替導線も違う。二本立ては条件次元レジストリの
    # tooltip_free_key 列が担う（片側だけ書き忘れることが起きない）。
    "viewer.post_grid.filterbar_media_tooltip_free": "現在表示中の項目を、選んだ種別のファイルだけに絞り込みます（その場フィルター）。\n配下も探すには「サブフォルダも検索」をオンにしてください。",
    # (UIレビュー 2026-08-28 N-99) 記号 1 文字ラベル「★」→ 他行と同じ
    # 「ラベル + コロン」形へ。中立値の語も「指定なし」独自語をやめ
    # common.filter.all（すべて）へ統一（コンボ構築側でキー参照）。
    "viewer.post_grid.filterbar_star_label": "スター:",
    "viewer.post_grid.filterbar_star_min": "★{n}以上",
    "viewer.post_grid.filterbar_star_tooltip": "自分で付けたスターが指定数以上の項目だけに絞り込みます（その場フィルター）。",
    # (UIレビュー 07-25 #13②) ★/あとで見ると同格のユーザータグ絞り込み。
    # 候補は user_meta.db にある全ユーザータグ（構文を覚えなくても選べる）。
    # 中立値「(すべて)」独自語は N-99 で common.filter.all へ統一済み。
    "viewer.post_grid.filterbar_usertag_label": "ユーザータグ:",
    "viewer.post_grid.filterbar_usertag_tooltip": "自分で付けたユーザータグで絞り込みます（その場フィルター）。候補は保存済みのタグから選べます。",
    "viewer.post_grid.forward_tooltip": "戻る操作を取り消して 1 つ先のフォルダへ進む (Alt+→ / マウス進むボタン)\n長押しで履歴一覧を表示",
    # (UIレビュー 2026-08-28 N-74) 「あとで見る」もキー (L) で撃てるように
    # なったので、★ / ユーザータグと同じ確定トーストを持たせる。
    "viewer.post_grid.later_cleared_toast": "「あとで見る」を解除しました: {name}",
    "viewer.post_grid.later_set_toast": "「あとで見る」に追加しました: {name}",
    "viewer.post_grid.loading_root": "読み込み中: {root}",
    "viewer.post_grid.locked_only": "ロックありのみ",
    # (UIレビュー 07-25 #40) ⋯ から フィルタ ポップオーバーへ移設＋揮発化。
    "viewer.post_grid.locked_only_tooltip": "ロックされたコンテンツを含む投稿だけに絞り込みます（その場フィルター。起動時は解除）。",
    "viewer.post_grid.nsfw_explicit": "R-18 を隠す",
    "viewer.post_grid.nsfw_menu": "グリッドで年齢制限を隠す",
    # (UIレビュー 2026-09-11 N-36) 設定は永続するのに席は常に素の文言で、
    # いま何が効いているかがメニューを開くまで分からなかった。
    "viewer.post_grid.nsfw_menu_active": "年齢制限: {value}",
    "viewer.post_grid.nsfw_needs_tagsdb": "年齢制限フィルターには tags.db が必要です。AIタグの索引を作るには Snappix Tagger でスキャンしてください（ヘルプ ▸ AIタグ検索のセットアップ…）。",
    "viewer.post_grid.nsfw_off": "隠さない（すべて表示）",
    "viewer.post_grid.nsfw_questionable": "R-15 以上を隠す",
    # prefix_* — ``field:`` トークンの説明（接頭辞補完のドロップダウンと
    # 構文ヘルプの軸一覧が**同じキーを共有**する — 条件次元レジスタリ
    # search_dimensions.TOKEN_FIELDS 参照。旧ヘルプ HTML 内の別文言と統合）。
    # 旧 post_grid.options_tooltip は ⋯ の一意化 (N-24) で廃止（バッジ語彙側）。
    "viewer.post_grid.open_folder_tooltip": "フォルダを開く (Ctrl+O)",
    "viewer.post_grid.prefix_body": "post.md 本文の全文検索",
    "viewer.post_grid.prefix_favorites": "お気に入り数",
    "viewer.post_grid.prefix_later": "「あとで見る」 (yes / no。yes はフィルターの「あとで見る」と連動)",
    "viewer.post_grid.prefix_mytags": "自分のユーザータグ",
    "viewer.post_grid.prefix_name": "フォルダ名",
    "viewer.post_grid.prefix_plan": "プラン名",
    "viewer.post_grid.prefix_plan_price": "プラン価格 (¥/$ 記号可)",
    "viewer.post_grid.prefix_rating": "年齢区分 (safe/r15/r18。要 tags.db)",
    "viewer.post_grid.prefix_score": "AIタグ精度の下限 (>0.5 など。AIタグ検索中のみ有効)",
    "viewer.post_grid.prefix_star": "自分のスター (>=3 / =5 / 0。>=N はフィルターの★と連動)",
    "viewer.post_grid.prefix_tags": "投稿タグ (post.md 由来)",
    # AI パック有効時の tags: 説明 — AIタグとの取り違え誘導つき。
    "viewer.post_grid.prefix_tags_ai": "投稿タグ (post.md 由来。AIタグは「AI 検索」へ)",
    "viewer.post_grid.prefix_title": "タイトル",
    "viewer.post_grid.prefix_type": "種別 (画像/動画/音声/文書/アーカイブ)",
    "viewer.post_grid.recent_crumb": "最近追加されたファイル: {name}",
    "viewer.post_grid.recent_done": "最近追加されたファイル — {n:,} 件",
    "viewer.post_grid.recent_empty": "このフォルダにファイルはありません",
    # 走査そのものの失敗を「ファイルはありません」と言わない — 共有の切断や
    # フォルダの削除を「増えていない」と読ませないための分岐（レビュー #66-6）。
    "viewer.post_grid.recent_error": "フォルダを読み取れませんでした（移動・削除、またはネットワークの切断）",
    "viewer.post_grid.recent_failed": "最近追加されたファイル — 読み取りに失敗しました",
    "viewer.post_grid.recent_files_action": "最近追加されたファイルを表示",
    "viewer.post_grid.recent_loading": "最近追加されたファイルを探しています…",
    # 上限で切ったことを黙らない（設計原則: 打ち切りを「全件」に見せない）。
    "viewer.post_grid.recent_truncated": "{total:,} 件中 新しい {shown:,} 件を表示",
    "viewer.post_grid.recursive_cache_hits": "キャッシュ {n} 件ヒット",
    "viewer.post_grid.recursive_check": "サブフォルダも検索",
    # (UIレビュー 07-25 #115) 絵文字の直書きをやめ、0 件は「該当なし」へ分岐。
    "viewer.post_grid.recursive_done": "サブフォルダ検索完了 — {hits:,} 件ヒット",
    "viewer.post_grid.recursive_done_none": "サブフォルダ検索完了 — 該当なし",
    "viewer.post_grid.recursive_done_scanned": "（{n:,} 件走査）",
    # 上限に達して走査を打ち切った（件数を全件の顔にしない）。
    "viewer.post_grid.recursive_done_truncated": "上限 {n:,} 件で打ち切りました",
    # 走査に穴が空いたら 0 件を「該当なし」と断定しない（最近追加一覧の
    # scan_partial と同じ原則 — レビュー 2026-09-03 項目 #64）。
    "viewer.post_grid.recursive_done_unreadable": "サブフォルダ検索完了 — 一部のフォルダを読み取れず、該当の有無を判断できません",
    "viewer.post_grid.recursive_scanned": "{n:,} 件走査",
    "viewer.post_grid.recursive_searching": "サブフォルダ検索中…",
    "viewer.post_grid.recursive_tooltip": "チェック時、サブフォルダ以下も再帰的にファイル名・フォルダ名で検索します。\nタグ・タイトル検索は性能負荷のため直下の子フォルダのみが対象です。",
    # (UIレビュー 2026-08-28 N-30) AI 検索中はこの範囲設定が効かない（AI の
    # ワーカーは常に再帰）— 無効化中のチェックに理由を名乗らせる。
    "viewer.post_grid.recursive_tooltip_ai": "AI 検索は常にサブフォルダも含めて（再帰的に）検索します。\nこの設定は通常検索でのみ使えます。",
    # 「ロックありのみ」中は子孫を採れない（子孫はロック数を読まない）ので、
    # 範囲を広げても結果は変わらない — 走査を蹴らないことと対で無効化する。
    "viewer.post_grid.recursive_tooltip_locked": "「ロックありのみ」中は直下の投稿だけが対象です。\nサブフォルダの中身はロック数を読まないため、範囲を広げても結果は変わりません。",
    "viewer.post_grid.reload_tooltip": "現在のフォルダを再読み込み (F5)",
    # (UIレビュー 2026-08-28 N-145) 並び順が「ランダム」のときだけ F5 は
    # 並びをシャッフルし直す。前例＝最大化中の戻るボタンの動的差し替え。
    "viewer.post_grid.reload_tooltip_random": "現在のフォルダを再読み込み。ランダム並びをシャッフルし直します (F5)",
    # (UIレビュー 09-11 N-25) 条件バーの保存導線。ラベルはメニュー項目と同じ
    # ``viewer.main_window.save_search`` を再利用する（同じ操作の 2 入口なので
    # 文言を 2 キーに分けない）。AI 軸（モード・参照画像）は保存対象外なので、
    # ツールチップでそれを約束しない。
    "viewer.post_grid.save_search_tooltip": "いま効いている条件に名前を付けて保存します（左のレールから呼び出せます）",
    # 走査に穴が空いたことも黙らない — 読めなかったフォルダの分を「無かった」と
    # 読ませない（打ち切りと同じ原則。レビュー #66 追修）。最近追加一覧と
    # サブフォルダ検索が同じ 1 文言を共有する（レビュー 2026-09-03 項目 #64）。
    "viewer.post_grid.scan_partial": "一部のフォルダを読み取れませんでした",
    "viewer.post_grid.scope_label": "検索範囲",
    # ♡ の呼称は post.md 仕様の favorites: と一致する「お気に入り数」へ片寄せ
    # （「いいね数」は廃止 — UIレビュー 07-25 #102）。
    "viewer.post_grid.sort_favorites_asc": "お気に入り数 (少ない順)",
    "viewer.post_grid.sort_favorites_desc": "お気に入り数 (多い順)",
    "viewer.post_grid.sort_mtime_asc": "更新日時 (古い順)",
    "viewer.post_grid.sort_name_asc": "名前 (昇順)",
    "viewer.post_grid.sort_name_desc": "名前 (降順)",
    "viewer.post_grid.sort_posted_asc": "投稿日 (古い順)",
    "viewer.post_grid.sort_posted_desc": "投稿日 (新しい順)",
    # (UIレビュー 2026-09-11 N-93) 投稿日は post.md だけが持つ情報なので、
    # 持つ項目が 1 つも無い一覧では投稿日系の行を無効化し、理由を添える。
    "viewer.post_grid.sort_posted_unavailable_tooltip": "この一覧には投稿情報（post.md）を持つ項目がありません",
    "viewer.post_grid.sort_random": "ランダム",
    "viewer.post_grid.sort_relevance": "関連度順",
    "viewer.post_grid.sort_relevance_tooltip": "意味検索・類似画像検索の結果は関連度順で表示されます",
    "viewer.post_grid.sort_size_asc": "サイズ (小さい順)",
    "viewer.post_grid.sort_size_desc": "サイズ (大きい順)",
    "viewer.post_grid.sort_star_desc": "スター (高い順)",
    "viewer.post_grid.star_cleared_toast": "スターを解除しました: {name}",
    "viewer.post_grid.star_menu": "スター",
    # (UIレビュー 2026-09-11 N-116) ★ピッカーの各行。★グリフだけだと 4 と 5
    # を目で数えることになるので数値を併記する。
    "viewer.post_grid.star_menu_item": "{stars} ({n})",
    # 右クリックの★サブメニュー**見出し**だけの別キー (UIレビュー 2026-08-28
    # N-124)。同じ右クリックメニューの画像操作系は「実寸表示 (Ctrl+0)」など
    # キーを併記しているのに、★だけ 0〜5 が実在するのに無記載だった。
    # ``star_menu`` 本体はバッジ語彙レジストリの正式名 (``_indicator``) と
    # 情報パネル / 詳細情報ウィンドウの行ラベルが引くので、そちらへ
    # 「(0〜5)」を混ぜないよう別キーにしてある。grid / file_list の 2 つの
    # ピッカーはこのキーを共有するので片側欠落は構造的に起きない。
    "viewer.post_grid.star_menu_keys": "スター (0〜5)",
    # (UIレビュー 2026-09-11 N-116) 見出しに現在値を出す版。0 のときは
    # star_menu_keys のまま（「現在 ★0」は読みにくい）。
    "viewer.post_grid.star_menu_keys_value": "スター (0〜5) — 現在 ★{n}",
    "viewer.post_grid.star_none": "スターを外す",
    "viewer.post_grid.star_set_toast": "{stars} {name}",
    "viewer.post_grid.up_tooltip": "1 つ上の階層へ移動 (Alt+Up)",
    # (UIレビュー 07-25 #13①) 編集ダイアログで OK しても画面は無変化だった —
    # ★と同じ funnel の作法で対象名つきの確認を出す。
    "viewer.post_grid.user_tags_cleared_toast": "ユーザータグを解除しました: {name}",
    "viewer.post_grid.user_tags_set_toast": "ユーザータグ: {tags} — {name}",
    "viewer.post_grid.watch_later": "あとで見る",
    # 右クリック / 編集メニューの行 — ★の「スター (0〜5)」と同じくキーを併記する
    # （メニューの常設行を外して二重表示を解いた分の予告 — N-01）。
    "viewer.post_grid.watch_later_menu": "あとで見る (L)",
    # -- viewer.post_link_index.* ----------------------------------------
    "viewer.post_link_index.open_local_tooltip": "ローカルの投稿を開く",
    # -- viewer.saved_search_dialog.* ------------------------------------
    # (UIレビュー 09-11 N-25) 作成導線は条件バーのボタンが第一入口になった。
    # 撤去していないメニュー経路も併記するが、名指しの主役は入れ替える。
    "viewer.saved_search_dialog.add_hint": "保存は検索を実行してから条件バーの「この検索を保存…」で行えます（メニューの「検索 ▸ この検索を保存…」も同じです）。名前はダブルクリックで変更できます。",
    # (UIレビュー 07-25 #34) 名前だけでは中身も適用範囲も分からなかった。
    "viewer.saved_search_dialog.col_query": "条件",
    "viewer.saved_search_dialog.empty_hint": "保存した検索はまだありません。検索を実行すると、条件バーの右端に「この検索を保存…」が出ます。",
    "viewer.saved_search_dialog.scope_hint": "現在のフォルダを起点に適用します",
    "viewer.saved_search_dialog.title": "保存した検索を管理",
    # -- viewer.settings_dialog.* ----------------------------------------
    "viewer.settings_dialog.aspect_probe_parallelism": "アスペクト比 読み込み並列度:",
    "viewer.settings_dialog.build_cache_done_body": "{ok:,} 件をキャッシュしました（失敗 {failed:,} 件 / 対象 {total:,} 件）。",
    "viewer.settings_dialog.build_cache_done_title": "キャッシュ作成完了",
    "viewer.settings_dialog.build_cache_tooltip": "指定フォルダ以下を走査して事前キャッシュします。作成時に\n・アスペクト比のみ（高速・ヘッダ読みのみ／配置が即確定）\n・サムネイルも作成（完全・再訪時の表示が最速だが低速）\nのいずれかを選べます。検索索引（ファイル名・post.md の投稿リンク）も同じ走査で同時に作成されます。",
    "viewer.settings_dialog.cache_build_bg_check": "バックグラウンドで実行（閲覧を続けながら作成）",
    "viewer.settings_dialog.cache_build_bg_commit_accept": "適用して開始する",
    "viewer.settings_dialog.cache_build_bg_commit_body": "設定を適用してこのダイアログを閉じ、バックグラウンドでキャッシュ作成を開始します。",
    "viewer.settings_dialog.cache_build_bg_commit_informative": "バックグラウンド作成の進捗はステータスバーに表示されます（一時停止・中止できます）。この操作では、まだ「OK」を押していない他のタブの変更も同時に適用されます。",
    "viewer.settings_dialog.cache_build_bg_tooltip": "オンにすると、キャッシュ作成をバックグラウンドで実行し、進捗はステータスバーに表示されます（一時停止・キャンセル可能）。\nオフにするとモーダルダイアログで実行し、完了までウィンドウを専有します。",
    "viewer.settings_dialog.cache_building_bg": "キャッシュ作成中…（バックグラウンド）",
    "viewer.settings_dialog.cache_cleared": "キャッシュを削除しました。",
    "viewer.settings_dialog.cache_cleared_kinds": "{names} キャッシュを削除しました",
    "viewer.settings_dialog.cache_entries_limit": "画像数上限:",
    "viewer.settings_dialog.cache_hint": "※ 「1枚あたり上限」を超える画像はキャッシュされず毎回再デコードされます。先読み枚数を 0 にすると先読みを無効化します。",
    "viewer.settings_dialog.cache_intro": "メモリ / ディスクのキャッシュ設定と、キャッシュの作成・削除の管理を行います。メモリ上限を超えた分は古い画像から自動的に解放されます。",
    "viewer.settings_dialog.cache_manage_viewer_only": "（ビューア起動時のみ利用できます）",
    "viewer.settings_dialog.cache_mem_limit": "メモリ上限:",
    "viewer.settings_dialog.cache_single_limit": "1枚あたり上限:",
    "viewer.settings_dialog.cache_stats_prefix": "現在のキャッシュ — ",
    "viewer.settings_dialog.cache_stats_unavailable": "(取得できませんでした)",
    "viewer.settings_dialog.caption_locked_label": "ロック数:",
    "viewer.settings_dialog.caption_plan_label": "プラン:",
    "viewer.settings_dialog.caption_show_locked_check": "ロック数を表示する",
    "viewer.settings_dialog.caption_show_plan_check": "プラン名・価格を表示する",
    "viewer.settings_dialog.caption_show_posted_check": "投稿日を表示する",
    "viewer.settings_dialog.caption_show_size_check": "ファイルサイズを表示する",
    "viewer.settings_dialog.caption_size_label": "ファイルサイズ:",
    "viewer.settings_dialog.chrome_hide_label": "操作バーを隠すまでの時間（全画面表示）:",
    "viewer.settings_dialog.chrome_hide_tooltip": "全画面表示 (F11) で、マウスやキーの操作が止まってから\n上部バー・画面下端の操作ボタン・画像一覧・マウスカーソルを\n自動的に隠すまでの時間です。",
    "viewer.settings_dialog.clear_cache": "すべて削除",
    "viewer.settings_dialog.clear_cache_confirm": "以下のキャッシュを削除します。よろしいですか？\n\n{detail}",
    "viewer.settings_dialog.clear_cache_title": "キャッシュを削除",
    "viewer.settings_dialog.clear_cache_tooltip": "ディスク上のサムネイル・アスペクト比・フォルダ代表画像・検索索引の各キャッシュと、メモリ上のサムネイルをすべて削除します。",
    "viewer.settings_dialog.clear_item": "・{name}: {size} / {count:,} 件",
    "viewer.settings_dialog.clear_item_disabled": "・{name}: 無効",
    # キャッシュ種別名は stat_*_name キーを {name} に埋め込む (項目#183 —
    # 統計行・削除確認・設定行・ボタンで呼称が揺れないように)。
    "viewer.settings_dialog.clear_mem_note": "・メモリ上のサムネイルも破棄されます",
    # (UIレビュー 2026-08-28 N-135) 種別別の削除は thumb / search の 2 本の
    # ボタンしか無く、``_CLEAR_KIND_META`` の 4 種を網羅していなかった。
    # コンボ + 1 ボタンへ寄せ、台帳を回すだけで全種別が出るようにする。
    "viewer.settings_dialog.clear_selected_kind": "選択した種別を削除",
    "viewer.settings_dialog.clear_selected_kind_tooltip": "左のコンボで選んだ種別のキャッシュだけを削除します（他の種別は残ります）。",
    "viewer.settings_dialog.data_location": "保存先: {path}",
    "viewer.settings_dialog.disk_cache_enable_check": "有効にする",
    # (UIレビュー 2026-09-11) この ☑ が制御するのは 4 種のうちサムネイル
    # 1 種だけ（アスペクト比・検索索引・フォルダ代表画像は常時 or 別設定）。
    "viewer.settings_dialog.disk_cache_label": "サムネイルの保存:",
    # (UIレビュー 08-28 N-79) 末尾は「容量・長辺・有効/無効の変更は再起動後に
    # 完全反映されます。」だったが、容量は即時（set_max_bytes + prune）・長辺は
    # 以後に作るサムネから効く（既存分は再起動しても古いまま）で、行注記と
    # 正面から矛盾していた。3 者の実態どおりに書き分ける。
    "viewer.settings_dialog.disk_hint": "※ デコード済みサムネを data/thumb_cache に保存し、再訪・再起動でも即表示します。サムネイルとアスペクト比は別々の容量枠で管理され、サムネ肥大でアスペクト比が消されることはありません。長辺を超える大きな表示は原本から再デコードします。容量上限の変更は即座に反映され、長辺の変更は以後に作成されるサムネから効きます（保存済みのサムネは作られた時点の長辺のままです）。サムネイルの保存の切り替えだけは次回起動時に反映されます。",
    "viewer.settings_dialog.disk_kind_max": "{name} 容量上限:",
    "viewer.settings_dialog.disk_thumb_edge": "サムネ長辺:",
    "viewer.settings_dialog.display_intro": "テーマや、サムネイルサイズの上限・ホイールのスクロール量など、見た目と表示動作を調整します。",
    # (UIレビュー 2026-09-11) 旧文の「1列で収まる最大サイズ」は既定の
    # justified レイアウトでは誤り（_current_size_max は約 2 列ぶん）。
    # あわせて実サイズの変更場所（左右 2 つのポップオーバー）を案内する —
    # 上限だけ説明して「ではどこで大きくするのか」が書かれていなかった。
    "viewer.settings_dialog.display_tab_hint": "※ ここで決めるのは「リスト」表示のときの上限です。サムネイル表示のときは、ペイン幅から自動で上限が決まります（「ぴったり配置」は 2 列ぶん、「グリッド」は 1 列ぶん）。サムネイルの大きさ自体は、ツールバーの「並び・表示」（情報パネルは「表示オプション」）のスライダで変更します。",
    "viewer.settings_dialog.favorites_label": "お気に入り数:",
    "viewer.settings_dialog.group_ai_tags": "AI タグ (tags.db)",
    "viewer.settings_dialog.group_cache_manage": "キャッシュ管理",
    # (UIレビュー 2026-09-11) グループには 4 種の容量上限が同居するので、
    # 種別名をグループ名から外す（効能文は disk_hint の先頭に残る）。
    "viewer.settings_dialog.group_disk_cache": "ディスクキャッシュ（容量上限）",
    "viewer.settings_dialog.group_image_preview": "画像プレビュー",
    "viewer.settings_dialog.group_io_parallelism": "I/O 並列度",
    "viewer.settings_dialog.group_iv_cache": "画像ビュー(単体画像)キャッシュ",
    "viewer.settings_dialog.group_md_cache": "post.md 埋め込み画像キャッシュ",
    "viewer.settings_dialog.group_media_preview": "メディアプレビュー (音声・動画)",
    "viewer.settings_dialog.group_pdf_preview": "PDF プレビュー",
    "viewer.settings_dialog.group_preset": "プリセット",
    "viewer.settings_dialog.group_slider_scroll": "スライダ・スクロール",
    "viewer.settings_dialog.group_text_preview": "テキストプレビュー",
    # UIレビュー 07-25 #133: 「サムネイル表示」(♡ のみ) と「サムネイルキャプ
    # ション」を統合した単一グループのタイトル。
    "viewer.settings_dialog.group_tile_display_items": "タイルの表示項目",
    "viewer.settings_dialog.group_zip_preview": "ZIP プレビュー",
    "viewer.settings_dialog.image_fit_no_upscale_check": "小さい画像を等倍以上に拡大しない",
    "viewer.settings_dialog.image_fit_no_upscale_label": "フィット表示:",
    "viewer.settings_dialog.image_minimap_check": "拡大時にミニマップを表示する",
    "viewer.settings_dialog.image_minimap_label": "ミニマップ:",
    "viewer.settings_dialog.image_wheel_zoom_check": "ホイールでズーム（送りは Ctrl+ホイール）",
    "viewer.settings_dialog.image_wheel_zoom_label": "ホイール操作:",
    "viewer.settings_dialog.image_zoom_persist_check": "ファイル間の移動でズーム倍率を維持する",
    "viewer.settings_dialog.image_zoom_persist_label": "ズーム維持:",
    "viewer.settings_dialog.iv_prefetch_label": "先読み枚数(前後):",
    # UIレビュー 07-25 #33: 「グリッドのサムネイル表示上限」では何の上限かが
    # 画面から分からなかった — 実体（サイズスライダの上限・リスト表示時のみ
    # 実効）を言い切る表記へ。
    # (UIレビュー 2026-09-11) 同じ物が「サイズスライダの上限」「情報パネルの
    # サイズスライダの上限」「アイコン上限」の 3 通りで呼ばれていた。用語表の
    # 正式名「サムネイルサイズ」＋ペイン名（グリッド / 情報パネル）へ寄せる。
    "viewer.settings_dialog.left_pane_list_max": "グリッドのサムネイルサイズの上限（リスト表示時）:",
    # (UIレビュー 2026-08-28 N-138) Ctrl+ホイールで変えられて永続する表示
    # 設定なのに、設定ダイアログにも「既定値に戻す」にも無かった。
    # (UIレビュー 2026-09-11) 同じ「テキストプレビュー」群に text_body_max
    # =「本文読み込み上限」が並ぶため「本文」が 2 つの別対象を指していた。
    # 対象は post.md と .md の両方（content_view の振り分け条件が両方）。
    "viewer.settings_dialog.markdown_font_pt": "Markdown 本文（post.md / .md）のフォントサイズ:",
    "viewer.settings_dialog.markdown_font_pt_auto": "アプリの既定に従う",
    "viewer.settings_dialog.markdown_font_pt_hint": "※ post.md や .md ファイルのプレビューに使う文字サイズです。「アプリの既定に従う」のままなら、アプリの標準サイズに合わせます。プレビュー上で Ctrl+ホイールでも変えられます。",
    "viewer.settings_dialog.media_autoplay_check": "選択時に自動再生する",
    "viewer.settings_dialog.media_autoplay_label": "自動再生:",
    "viewer.settings_dialog.media_loop_check": "最後まで再生したら繰り返す",
    "viewer.settings_dialog.media_loop_label": "ループ再生:",
    # UIレビュー 07-25 #131: 実挙動は「再生中の変更が即座に永続する現在音量」
    # であり「初期」ではない。
    "viewer.settings_dialog.media_volume_label": "音量:",
    "viewer.settings_dialog.open_data_folder": "データフォルダを開く",
    "viewer.settings_dialog.pdf_hint": "※ PDF プレビューが読み込むファイルの上限です。ページは表示中ずっとメモリに載るため、超えた PDF はサイズのみ表示します（既定アプリでは開けます）。",
    "viewer.settings_dialog.pdf_read_max": "PDF 読み込み上限:",
    "viewer.settings_dialog.perf_hint": "※ post.md 同時読み込み数の変更は次回フォルダ切り替えから反映されます。サムネキャッシュ/並列度・アスペクト比読み込み並列度は即時反映されます。値を上げると並列度が増しますが、SMB のクレジット枯渇で逆に遅くなる場合があります。",
    "viewer.settings_dialog.perf_intro": "保存先ストレージに合わせた読み込み設定です。通常はプリセットを選ぶだけで調整できます。",
    "viewer.settings_dialog.postmd_concurrency": "post.md 同時読み込み数:",
    "viewer.settings_dialog.preset_custom": "カスタム",
    "viewer.settings_dialog.preset_custom_tooltip": "個別項目を手動で調整します（項目を変更すると自動でカスタムになります）。",
    "viewer.settings_dialog.preset_hint": "※ NAS など遅いストレージでは並列アクセスが多すぎると逆に遅くなるため、「NAS向け」は同時読み込み数を抑えます。",
    "viewer.settings_dialog.preset_nas": "NAS向け",
    "viewer.settings_dialog.preset_nas_tooltip": "遅い NAS（SMB 共有）向けに並列度を控えめにし、サムネイルのメモリキャッシュを増やします。",
    "viewer.settings_dialog.preset_standard": "標準",
    "viewer.settings_dialog.preset_standard_tooltip": "ローカルディスク・高速な NAS 向けの既定値です。",
    # (UIレビュー 2026-09-11) 実体は GalleryView のホイールハンドラが読む値で、
    # プレビュー列だけでなくグリッド・情報パネルの一覧にも効く。
    "viewer.settings_dialog.preview_scroll_amount": "ホイールのスクロール量（プレビュー・一覧共通）:",
    "viewer.settings_dialog.preview_scroll_hint": "※ ホイール 1 ノッチで動く量です。プレビュー列と、グリッド・情報パネルの一覧の両方に効きます。",
    # UIレビュー 07-25 #35: 「既定値に戻す」の対象範囲（表示・キャッシュ・
    # パフォーマンスの 3 タブ全部）が不可視だったため、確認モーダル + ツール
    # チップ + 完了トーストを追加。
    "viewer.settings_dialog.restore_defaults_confirm_body": "設定ダイアログの 3 タブすべて（表示・キャッシュ・パフォーマンス、テーマを含む）を既定値に戻します。よろしいですか？",
    # 確認モーダルのタイトルは common.action.restore_defaults を再利用
    # （settings_dialog.py 参照 — 表記揺れガード対応）。
    # 書き戻しは OK 押下（accept）まで起きないので、この時点では「まだ適用
    # されていない」ことを言い切る（success ではなく info）。
    "viewer.settings_dialog.restore_defaults_pending_toast": "各項目を既定値に戻しました。「OK」を押すと適用されます。",
    "viewer.settings_dialog.restore_defaults_tooltip": "3 タブすべて（表示・キャッシュ・パフォーマンス、テーマを含む）が対象です。",
    "viewer.settings_dialog.restore_selection_check": "起動時に前回の選択・プレビューを復元する",
    "viewer.settings_dialog.restore_selection_label": "前回の続きから:",
    "viewer.settings_dialog.restore_selection_tooltip": "前回終了時に選択していたフォルダ/ファイルとプレビュー中の画像、\nグリッドのスクロール位置を次回起動時に復元します。",
    # (UIレビュー 08-28 N-35 / N-76) 「ファイル一覧表示上限」は件数の上限と
    # 読めるが実体は px（アイコン寸法）— 隣に並ぶ左ペイン行 (left_pane_list_max)
    # だけが 07-25 #33 で改訂され、この行が旧表記のまま取り残されていた。
    # 語彙を左と揃え、（リスト表示時）の限定注記も同じ形で付ける。
    "viewer.settings_dialog.right_pane_list_max": "情報パネルのサムネイルサイズの上限（リスト表示時）:",
    "viewer.settings_dialog.search_clear_on_nav_check": "フォルダ移動時に検索を解除し、戻るで復元する",
    # (UIレビュー 2026-09-11) clear_search_state は検索欄だけでなく検索範囲・
    # フィルターの各軸・AI 検索の条件まで中立化するので、名乗りを実態に合わせる。
    "viewer.settings_dialog.search_clear_on_nav_label": "検索・フィルターの解除/復元:",
    "viewer.settings_dialog.search_clear_on_nav_tooltip": "対象は検索語・検索範囲・フィルターの各軸・AI 検索の条件です。\n検索中に画像・フォルダを選んでそのフォルダへ移動すると検索状態を解除し、\n「戻る」で元の検索状態を復元します。\nオフにすると検索状態はフォルダ移動後も保持されます。\n（フォルダの選択位置は、この設定に関わらず「戻る」で復元されます）",
    "viewer.settings_dialog.show_advanced_check": "詳細設定を表示",
    # 位置非依存の文言にすること (レビュー #113): 四隅座席（既定）では
    # ♡ は右下のバッジ列、レガシー座席（「画像の下に表示」）では左下に出る
    # ため、「左下」と書くと既定構成の実挙動と食い違う。
    "viewer.settings_dialog.show_favorites_check": "お気に入り数をバッジで表示する",
    # (UIレビュー 2026-09-11) 揮発（[ / ] キー）と永続（この設定）の関係が
    # ショートカット一覧側にしか書かれていなかった対の片側欠落を閉じる。
    "viewer.settings_dialog.slideshow_interval_label": "スライドショー間隔（全画面表示）:",
    "viewer.settings_dialog.slideshow_interval_tooltip": "全画面表示 (F11) でスライドショーを開始したときの\n自動送り間隔です。\n全画面表示中の [ / ] キーでの変更はその回だけで、ここには保存されません。",
    # キャッシュ種別の表示名は *_name キーが唯一の情報源 (項目#182):
    # 統計行 (stat_item / stat_item_disabled)・削除確認 (clear_item*)・完了
    # トースト・設定行ラベル・ボタンのすべてが *_name を埋め込んで組み立てる。
    # 種別ごとの複合テンプレートを増やして表示名を焼き込まないこと。
    "viewer.settings_dialog.stat_aspect_name": "アスペクト比",
    "viewer.settings_dialog.stat_folder_name": "フォルダ代表画像",
    "viewer.settings_dialog.stat_item": "{name}: {size} / {count:,} 件",
    "viewer.settings_dialog.stat_item_disabled": "{name}: 無効",
    # (UIレビュー 2026-09-11 N-61) 「無効」が「設定でオフ」と「開けなかった」の
    # 2 義だったのを言い分ける（理由の材料はコントローラが既に持っている）。
    "viewer.settings_dialog.stat_item_failed": "{name}: 開けませんでした（ログを参照）",
    "viewer.settings_dialog.stat_item_off": "{name}: 設定でオフ",
    "viewer.settings_dialog.stat_search_index_name": "検索索引",
    "viewer.settings_dialog.stat_thumb_images_name": "サムネイル",
    "viewer.settings_dialog.suffix_seconds": " 秒",
    "viewer.settings_dialog.suffix_sheets": " 枚",
    "viewer.settings_dialog.tab_performance": "パフォーマンス",
    "viewer.settings_dialog.tag_db_not_loaded": "tags.db は読み込まれていません。AIタグの索引を作るには Snappix Tagger でスキャンしてください（ヘルプ ▸ AIタグ検索のセットアップ…）。",
    "viewer.settings_dialog.tag_info_hint": "※ tags.db は Snappix Tagger が生成します。ビューアは読み取り専用で、「AI 検索」でタグ・しきい値・投稿日による検索に使います。",
    # 「フロア」→「記録しきい値」(UIレビュー 07-25 #44 の統一を統計行にも波及)。
    "viewer.settings_dialog.tag_stats": "モデル: {model} ・ 記録しきい値: {floor:.2f} ・ 画像 {images:,} 件 ・ タグ種別 {tags:,} ・ {mib:.1f} MiB",
    "viewer.settings_dialog.text_body_max": "本文読み込み上限:",
    "viewer.settings_dialog.text_hint": "※ ログ・CSV・JSON などのプレビューで読み込む本文の上限です。超えた分は切り捨て、本文末尾に注記します。",
    # UIレビュー 07-25 #84: テーマコンボの「その他」6 択に明暗の付記を足す
    # （保存値は内部キーのまま — 表示名のみ）。
    "viewer.settings_dialog.theme_extra_suffix_dark": "{name}（ダーク）",
    "viewer.settings_dialog.theme_extra_suffix_light": "{name}（ライト）",
    "viewer.settings_dialog.theme_hint": "※ 表示メニューの「テーマ」と同じ設定です。OK で即時反映されます。",
    "viewer.settings_dialog.thumb_cache_max": "サムネキャッシュ上限:",
    "viewer.settings_dialog.thumb_decode_parallelism": "サムネデコード並列度:",
    "viewer.settings_dialog.tile_name_below": "画像の下に表示",
    "viewer.settings_dialog.tile_name_overlay": "画像に重ねる",
    "viewer.settings_dialog.tile_name_placement_label": "タイル名の表示位置:",
    "viewer.settings_dialog.tile_name_placement_tooltip": "中央グリッドのタイルに表示するフォルダ・ファイル名の位置です。\n「画像に重ねる」（既定）はサムネイル下端に文字を重ねて表示します。\n「画像の下に表示」はサムネイルの外側（下）に帯を設けて文字を置くため、絵柄に文字が重ならず読みやすくなります（タイルはその分だけ縦に伸びます）。",
    "viewer.settings_dialog.timing_new_thumbs": " （以後に作成されるサムネから反映）",
    "viewer.settings_dialog.timing_next_folder": " （次回フォルダ切替から反映）",
    "viewer.settings_dialog.timing_restart": " （再起動後に反映）",
    "viewer.settings_dialog.wheel_nav_grace_hint": "※ 端でホイールを回し続けてから次のファイルに切り替わるまでの待ち時間です。0 にすると即座に切り替わります。",
    "viewer.settings_dialog.wheel_nav_grace_label": "ホイールで次/前へ移行する猶予:",
    "viewer.settings_dialog.window_title": "設定",
    "viewer.settings_dialog.zip_hint": "※ ダブルクリックでフォルダとして開く / 一覧プレビュー両方の上限です。超えた ZIP はサイズのみ表示します。",
    "viewer.settings_dialog.zip_read_max": "ZIP 読み込み上限:",
    # -- viewer.shell_integration.* --------------------------------------
    "viewer.shell_integration.confirm_register_body": "フォルダの右クリックメニューに「Snappix Viewer で開く」を追加します。レジストリ（HKCU・現在のユーザーのみ）に書き込みます。",
    "viewer.shell_integration.confirm_register_btn": "登録する",
    "viewer.shell_integration.confirm_unregister_body": "右クリックメニューの「Snappix Viewer で開く」を削除し、レジストリの登録を解除します。",
    "viewer.shell_integration.confirm_unregister_btn": "解除する",
    "viewer.shell_integration.description": "フォルダの右クリックメニューに「Snappix Viewer で開く」を追加します。レジストリ（HKCU・現在のユーザーのみ）に書き込みます。ポータブル運用の方針上、アンインストールや別の場所への移動の前に必ず解除してください。",
    "viewer.shell_integration.dev_note": "開発実行（未ビルド）では登録できません。ビルド済みの SnappixViewer.exe から起動した場合のみ登録できます。既存の登録の解除はここから行えます。",
    "viewer.shell_integration.error": "レジストリ操作に失敗しました: {error}",
    "viewer.shell_integration.menu": "エクスプローラ統合…",
    "viewer.shell_integration.register": "登録",
    # 登録済みの exe が現在の場所と食い違うときのボタン・注記
    # (UIレビュー 08-28 N-37)。押すと現在の場所で上書き登録される。
    "viewer.shell_integration.register_update": "登録（この場所に更新）",
    "viewer.shell_integration.status_registered": "登録済み: {exe}",
    "viewer.shell_integration.status_stale": "※ 登録されている実行ファイルは、いま動いている実行ファイル（{exe}）と違います。フォルダを移動・コピーした場合、右クリックからは古い場所が起動される（または何も起きない）状態です。［登録（この場所に更新）］でこの場所に貼り直せます。",
    "viewer.shell_integration.status_unregistered": "未登録",
    "viewer.shell_integration.title": "エクスプローラ統合",
    "viewer.shell_integration.unavailable": "この機能は Windows でのみ利用できます。",
    "viewer.shell_integration.unregister": "解除",
    # -- viewer.shortcuts_dialog.* ---------------------------------------
    "viewer.shortcuts_dialog.cat_badges": "凡例: アイコン・バッジの意味（キー操作ではありません）",
    # UIレビュー 07-25 #50: split out of the old shared "common.label.display"
    # bucket — window-chrome visibility toggles vs. image-only operations were
    # scope-mixed under one category, and that label is reused verbatim by
    # unrelated UI (post_grid の「表示」ポップオーバー・settings タブ名).
    "viewer.shortcuts_dialog.cat_image_ops": "画像の操作（プレビュー・全画面）",
    "viewer.shortcuts_dialog.cat_media": "メディア",
    "viewer.shortcuts_dialog.cat_mode": "プレビュー（分割 ⇄ 最大化）",
    "viewer.shortcuts_dialog.cat_other": "その他",
    "viewer.shortcuts_dialog.cat_pdf": "PDF",
    "viewer.shortcuts_dialog.cat_window_display": "ウィンドウ表示",
    "viewer.shortcuts_dialog.col_entry": "入口",
    "viewer.shortcuts_dialog.col_key": "キー",
    "viewer.shortcuts_dialog.col_operation": "操作",
    "viewer.shortcuts_dialog.col_seat": "効く席",
    "viewer.shortcuts_dialog.desc_actual_size": "画像を実寸表示",
    "viewer.shortcuts_dialog.desc_ai_tag_search": "AIタグ検索（AI 検索ポップオーバーを開いて入力へ）",
    "viewer.shortcuts_dialog.desc_back": "戻る",
    "viewer.shortcuts_dialog.desc_back_forward_side": "戻る / 進む（サイドボタン）",
    # (UIレビュー 07-25 #57) 図像をしおり → 時計へ変更。
    "viewer.shortcuts_dialog.desc_badge_ghost": "元の場所に見つからない / 読み取れないエントリ",
    "viewer.shortcuts_dialog.desc_badge_later": "青い時計 =「あとで見る」を付けたエントリ",
    "viewer.shortcuts_dialog.desc_badge_likes": "投稿のお気に入り数（post.md の favorites）",
    "viewer.shortcuts_dialog.desc_badge_locked": "ロックされたコンテンツ数",
    "viewer.shortcuts_dialog.desc_badge_relevance": "検索の関連度（意味検索・類似画像検索）",
    # UIレビュー 08-28 N-63: ホバーで出る類似検索ボタンは凡例に載っていなかった。
    "viewer.shortcuts_dialog.desc_badge_similar": "画像タイルにホバーすると出る「この画像で類似検索」ボタン",
    "viewer.shortcuts_dialog.desc_badge_star": "自分で付けたスター（1〜5）",
    "viewer.shortcuts_dialog.desc_badge_thumb_fail": "サムネイルの読み込みに失敗",
    "viewer.shortcuts_dialog.desc_browse_mode": "分割ビューに戻す（プレビュー最大化中）",
    # UIレビュー 07-25 #107: Esc also exits the maximised preview
    # (desc_stage_exit below) — spell out which one wins when both could apply.
    # UIレビュー 2026-08-28 N-88: Esc の 3 つ目の意味（横断一覧 / 最近追加
    # 一覧からの退場 — ``post_grid._on_escape_clear`` の分岐）が表に無かった。
    # 画面上いちばん大きな状態変化なのに書かれていない。併せて N-92 の
    # 短縮（既定サイズで末尾が「…」で切れていた 4 行の 1 つ）。
    # 文脈語も実体へ（N-26）: Esc はウィンドウレベルの単一ハンドラなので
    # フォーカス位置を問わない — 「グリッド」は誤り。
    "viewer.shortcuts_dialog.desc_clear_filters": "絞り込み・検索を解除。一覧の表示中はその一覧も閉じる（最大化中は最大化の解除が優先）",
    "viewer.shortcuts_dialog.desc_copy_image": "画像をコピー",
    "viewer.shortcuts_dialog.desc_dblclick_maximize": "その画像をプレビューで最大化（グリッド / 一覧共通）",
    # UIレビュー 07-25 #49: 分岐列挙をキー=挙動の統一書式に揃え、その他ファイル
    # =既定アプリで開く／検索結果のファイル=親フォルダへドリルの2分岐を追記
    # （main_window._on_grid_file_activated の has_dedicated_view() 分岐と
    # parent != root 分岐。Enter 側の desc_open_tile と同一の列挙にする）。
    # UIレビュー 2026-08-28 N-92: 分岐の全列挙を Enter 側 (desc_open_tile) に
    # 1 箇所だけ置き、こちらは参照にする（両行が完全に同一の長文を持つ必要は
    # ない — 既定サイズで末尾が切れていた 4 行のうち 2 行がこれだった）。
    # (UIレビュー 2026-09-11 N-130) ドラッグ書き出しは画面上に手掛かりが
    # この 1 行しか無かった。等価の手段（右クリックの項目名は
    # viewer.context_menus.copy_file と一字一句同じであること）を添える。
    "viewer.shortcuts_dialog.desc_drag_export": "選択した項目をエクスプローラや他アプリへドラッグして書き出す（右クリック ▸ ファイルをコピー も同じ）",
    "viewer.shortcuts_dialog.desc_drill_down": "タイルを開く（Enter と同じ）",
    "viewer.shortcuts_dialog.desc_drop_folder": "フォルダをウィンドウに落とすとそのフォルダを開く（戻るで元の位置へ）",
    "viewer.shortcuts_dialog.desc_exit_lightbox": "全画面表示を終了",
    "viewer.shortcuts_dialog.desc_first_last_image": "先頭 / 末尾の画像へ",
    "viewer.shortcuts_dialog.desc_fit_window": "画像を表示領域に合わせる",
    "viewer.shortcuts_dialog.desc_flip_horizontal": "画像を左右反転（表示のみ）",
    "viewer.shortcuts_dialog.desc_focus_filter": "検索ボックスにフォーカス（ツールバー）",
    # UIレビュー 2026-08-28 N-28: ペイン間のフォーカス移動キー（畳んでいる席は
    # 移動前に開く）。フォーカスの現在位置は席の細枠で見える（N-26 案B）。
    # N-143: 副作用（畳んだ席を開く / 最大化を解く）は Python コメントではなく
    # 値そのものに書く — 利用者が読むのは操作ガイドの表だけ。
    "viewer.shortcuts_dialog.desc_focus_grid": "中央のグリッドにフォーカス（最大化中は分割ビューへ戻ります）",
    # 4 席目（情報パネル）だけ直行キーが無かった（UIレビュー 2026-09-11 N-15）。
    "viewer.shortcuts_dialog.desc_focus_info_panel": "情報パネル（右）にフォーカス（畳んでいれば開きます）",
    "viewer.shortcuts_dialog.desc_focus_nav_rail": "ナビレール（左）にフォーカス（隠れていれば開きます）",
    "viewer.shortcuts_dialog.desc_focus_preview": "プレビュー（中央右）にフォーカス（畳んでいれば開きます）",
    "viewer.shortcuts_dialog.desc_forward": "進む",
    "viewer.shortcuts_dialog.desc_frame_back": "1フレーム戻す",
    "viewer.shortcuts_dialog.desc_frame_forward": "1フレーム送る",
    # UIレビュー 2026-08-28 N-26: 「（グリッド）」はモード名のように読めるが
    # 実体はフォーカス位置で、しかも同じキーが情報パネル一覧でも効く
    # （``FileListView`` も ``ChildrenGrid``）。文脈語を実体へ書き直す。
    "viewer.shortcuts_dialog.desc_go_up": "上の階層へ移動（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_history_list": "戻る / 進む 履歴一覧を表示",
    "viewer.shortcuts_dialog.desc_jump_ancestor": "上位フォルダへジャンプ",
    "viewer.shortcuts_dialog.desc_jump_to_results": "検索欄から結果（グリッド）へフォーカスを移す",
    # UIレビュー 07-25 #89: the 1-5/0 star row was worded 3 ways across the
    # navigation / mode / lightbox sections — unify to "{対象}にスターを設定 /
    # 0 で解除（{モード}）" so the three rows read as one template with only
    # the target/context varying (見出し語彙は #11 のフォーカス対象問題とも整合)。
    "viewer.shortcuts_dialog.desc_lightbox_set_star": "現在の画像にスターを設定 / 0 で解除（全画面表示中）",
    "viewer.shortcuts_dialog.desc_lightbox_toggle_later": "表示中の画像の「あとで見る」を切替",
    "viewer.shortcuts_dialog.desc_lightbox_toggle_zoom": "実寸表示 ⇄ 表示領域に合わせる",
    # UIレビュー 07-25 #109: 中クリックはこれまで無割り当てだった。
    "viewer.shortcuts_dialog.desc_middle_click_fit": "画像の上で実寸表示 ⇄ 表示領域に合わせるを切替",
    "viewer.shortcuts_dialog.desc_next_image": "次の画像へ（末尾で隣の投稿へ）",
    "viewer.shortcuts_dialog.desc_open_detail": "詳細情報ウィンドウを開く",
    # UIレビュー 07-25 #49: 「ドリルイン」表記だった (desc_drill_down は
    # 「ドリルダウン」— main_window._on_folder_activated のコメント語彙に合わせて
    # 統一) + その他ファイル / 検索結果ファイルの2分岐を desc_drill_down と
    # 同一書式で追記。
    "viewer.shortcuts_dialog.desc_open_tile": "選択中のタイルを開く（フォルダ=ドリルダウン、画像=最大化、ZIP=展開、他=既定アプリ / 検索結果は親フォルダへ）",
    "viewer.shortcuts_dialog.desc_perf_stats": "パフォーマンス統計を表示",
    "viewer.shortcuts_dialog.desc_play_pause": "再生 / 一時停止",
    "viewer.shortcuts_dialog.desc_prev_next_image": "前 / 次の画像",
    "viewer.shortcuts_dialog.desc_prev_next_image_wrap": "前 / 次の画像（末尾でもう一度押すと隣の投稿へ）",
    "viewer.shortcuts_dialog.desc_preview_dblclick": "プレビューを最大化（分割時。最大化中の画像上はズーム切替）",
    "viewer.shortcuts_dialog.desc_reload": "現在のフォルダを再読み込み（並び順が「ランダム」のときは並びをシャッフルし直す）",
    "viewer.shortcuts_dialog.desc_rotate_left": "画像を左に回転（表示のみ）",
    "viewer.shortcuts_dialog.desc_rotate_right": "画像を右に回転（表示のみ）",
    # UIレビュー 07-25 #48: 3 new rows for grid keys handled in
    # GalleryView.keyPressEvent that had no table entry at all (↑/↓・Home/End・
    # PageUp/PageDown — mechanically closed by
    # tests/test_viewer_shortcuts_keypress_sync.py).
    "viewer.shortcuts_dialog.desc_select_first_last": "選択を先頭 / 末尾の項目へ移動（グリッド / 一覧にフォーカス時）",
    # UIレビュー 07-25 #67: plain ←/→ mean something different once the
    # preview is maximised (項目内の画像送り — desc_stage_step_image below);
    # call that out here so the two rows read as one pair, not a contradiction.
    # N-144: グリッド / 右一覧 / ナビレールはどれも ShortcutOverride を受理して
    # 自席の選択を動かすので、動くのは「フォーカスのある席」であってグリッドとは
    # 限らない。他 3 行（first_last / page / up_down）と同じ条件表記へ揃える。
    "viewer.shortcuts_dialog.desc_select_next": "選択を次の項目へ移動（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_select_page": "選択を 1 ページ分移動（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_select_prev": "選択を前の項目へ移動（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_select_up_down": "選択を上下の行へ移動（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_set_star": "選択中の項目にスターを設定 / 0 で解除（グリッド / 一覧にフォーカス時）",
    "viewer.shortcuts_dialog.desc_show_help": "このヘルプを表示",
    "viewer.shortcuts_dialog.desc_slideshow": "スライドショー開始 / 停止",
    "viewer.shortcuts_dialog.desc_slideshow_interval": "スライドショーの間隔を 1 秒ずつ短く / 長くする（この回だけ・保存されません）",
    # UIレビュー 07-25 #107: spell out the internal priority
    # (main_window._on_escape — 編集中の入力欄があればそちらを優先し
    # モードは維持) so this row and desc_clear_filters read as one ordered pair.
    "viewer.shortcuts_dialog.desc_stage_exit": "プレビューの最大化を解除（入力欄の編集中はその取り消しが優先）",
    # N-26: これらは ``content_view.keyPressEvent`` が「画像ページか」だけを
    # 見ており ``_ui_mode`` を参照しない = モードではなくフォーカス位置で決まる
    # （分割ビューでもプレビューにフォーカスがあれば効く）。
    "viewer.shortcuts_dialog.desc_stage_first_last_image": "項目内の先頭 / 末尾の画像へ（プレビューの画像にフォーカス時）",
    "viewer.shortcuts_dialog.desc_stage_mode": "プレビューの最大化 ⇄ 分割を切替",
    # UIレビュー 07-25 #22: Space / Home / End は全画面表示にしか
    # 無く、最大化プレビューとキー集合が非対称だった。両面で同じ操作になる。
    "viewer.shortcuts_dialog.desc_stage_next_image": "次の画像へ（プレビューの画像にフォーカス時）",
    "viewer.shortcuts_dialog.desc_stage_next_post": "グリッドの次の項目へ切替（プレビュー最大化中）",
    "viewer.shortcuts_dialog.desc_stage_prev_post": "グリッドの前の項目へ切替（プレビュー最大化中）",
    "viewer.shortcuts_dialog.desc_stage_set_star": "プレビュー中の画像にスターを設定 / 0 で解除（プレビューの画像にフォーカス時）",
    # UIレビュー 07-25 #67: 最大化中の主操作なのに一覧に無かった
    # （main_window._step_or_navigate — _ui_mode=="stage" では常に画像送り）。
    "viewer.shortcuts_dialog.desc_stage_step_image": "項目内の前後の画像へ切替（プレビューにフォーカス時 / 最大化中）",
    # グリッド / 右一覧の Ctrl+ホイール（N-66）。画像側の desc_zoom_image と
    # キー文字列は同じで、席（一覧 / 画像）が弁別する。
    "viewer.shortcuts_dialog.desc_thumb_zoom": "サムネイルサイズを拡大 / 縮小（グリッド / 右一覧の上で）",
    "viewer.shortcuts_dialog.desc_toggle_info_panel": "情報パネル（右）の表示 / 非表示",
    # (UIレビュー 2026-08-28 N-74)
    "viewer.shortcuts_dialog.desc_toggle_later": "操作中の面の項目の「あとで見る」を切替（グリッド / 右一覧 / プレビュー / 全画面）",
    "viewer.shortcuts_dialog.desc_toggle_lightbox": "全画面表示を開始 / 終了",
    "viewer.shortcuts_dialog.desc_toggle_nav_rail": "ナビレール（左）の表示 / 非表示",
    "viewer.shortcuts_dialog.desc_toggle_preview": "プレビュー列（中央右）の表示 / 非表示",
    # UIレビュー 07-25 #50 → 2026-09-11 N-71: 席条件は「効く席」列
    # （seat_image = プレビューの画像にフォーカス時）が名乗るので、desc 側の
    # 「〜表示中のみ」は落とす。desc に残すのは席では言えない追加条件だけ
    # （最大化中に限る / Markdown でも効く）。「表示のみ＝表示専用で保存
    # されない」という別種の注記 [desc_rotate_right 等] は意味が違うので据え置き。
    "viewer.shortcuts_dialog.desc_toggle_zoom": "実寸表示 ⇄ 表示領域に合わせるを切替（最大化中 / 全画面）",
    "viewer.shortcuts_dialog.desc_zoom_image": "画像を拡大 / 縮小・本文のフォントサイズを変更（Markdown表示中のみ）",
    "viewer.shortcuts_dialog.desc_zoom_in": "拡大",
    "viewer.shortcuts_dialog.desc_zoom_out": "縮小",
    "viewer.shortcuts_dialog.entry_capsule": "画像上の操作カプセル（マウスを動かすと出る）",
    "viewer.shortcuts_dialog.entry_click": "クリック / ダブルクリック",
    "viewer.shortcuts_dialog.entry_condition_bar": "条件バーの解除ボタン",
    "viewer.shortcuts_dialog.entry_context_menu": "右クリック",
    "viewer.shortcuts_dialog.entry_curation_strip": "印ストリップ（情報パネル / ヘッダー / 全画面バー）",
    "viewer.shortcuts_dialog.entry_filter_box": "検索欄",
    "viewer.shortcuts_dialog.entry_gesture": "マウス操作（ドラッグ / ドロップ / ホイール）",
    "viewer.shortcuts_dialog.entry_key_only": "キーのみ",
    "viewer.shortcuts_dialog.entry_legend": "—",
    "viewer.shortcuts_dialog.entry_lightbox_bar": "全画面の上部バー（マウスを動かすと出る）",
    "viewer.shortcuts_dialog.entry_media_controls": "動画の操作バー",
    "viewer.shortcuts_dialog.entry_menu_diag": "「診断」メニュー",
    "viewer.shortcuts_dialog.entry_menu_edit": "「編集」メニュー",
    "viewer.shortcuts_dialog.entry_menu_file": "「ファイル」メニュー",
    "viewer.shortcuts_dialog.entry_menu_help": "「ヘルプ」メニュー",
    "viewer.shortcuts_dialog.entry_menu_search": "「検索」メニュー",
    "viewer.shortcuts_dialog.entry_menu_view": "「表示」メニュー",
    "viewer.shortcuts_dialog.entry_pane_toggles": "ツールバー右端のペイン切替",
    "viewer.shortcuts_dialog.entry_settings": "「設定」ダイアログ",
    "viewer.shortcuts_dialog.entry_stage_header": "プレビューのヘッダー",
    "viewer.shortcuts_dialog.entry_toolbar": "ツールバー",
    "viewer.shortcuts_dialog.filter_no_hits": "絞り込みに一致する項目がありません。別の語や、キー名（Ctrl / F11 など）でも探せます。",
    "viewer.shortcuts_dialog.filter_placeholder": "ショートカットを絞り込み…",
    "viewer.shortcuts_dialog.intro_modes": "表示は 3 態: 分割ビュー（既定）→ プレビュー最大化（ヘッダーの [最大化] か画像をダブルクリック、[分割ビューに戻す] で戻る）→ 全画面（ヘッダーの [全画面]、Esc で戻る）。",
    "viewer.shortcuts_dialog.intro_note": "主要な操作はすべてマウスで画面から届きます。キーは慣れてきた人の近道です（この一覧は困ったときの補助）。",
    "viewer.shortcuts_dialog.intro_sheets": "画面は 3 つの面: 左レール（ライブラリ・印を付けたもの・ブックマーク・保存した検索）/ 中央（左のグリッドと右のプレビュー）/ 右の情報パネル（印・投稿情報・ファイル一覧）。",
    "viewer.shortcuts_dialog.seat_any": "どこでも",
    "viewer.shortcuts_dialog.seat_grid_list": "グリッド・右一覧",
    "viewer.shortcuts_dialog.seat_image": "プレビューの画像にフォーカス時",
    "viewer.shortcuts_dialog.seat_media": "動画を表示中",
    "viewer.shortcuts_dialog.seat_pdf": "PDF を表示中",
    "viewer.shortcuts_dialog.seat_preview": "プレビュー",
    "viewer.shortcuts_dialog.seat_stage": "プレビュー最大化中",
    "viewer.shortcuts_dialog.task_arrange": "整える",
    "viewer.shortcuts_dialog.task_find": "探す",
    "viewer.shortcuts_dialog.task_legend": "凡例",
    "viewer.shortcuts_dialog.task_mark": "印を付ける",
    "viewer.shortcuts_dialog.task_start": "はじめに",
    "viewer.shortcuts_dialog.task_view": "見る",
    # UIレビュー 07-25 #108: バッジ凡例（cat_badges）がこの窓に同居している
    # ことをタイトルにも反映。
    "viewer.shortcuts_dialog.window_title": "操作ガイド — ショートカットと画面の凡例",
    # -- viewer.stage_view.* ---------------------------------------------
    # 3 態の呼称を 1 軸に揃える (UIレビュー 07-25 #21 / #103):
    # 分割ビューに戻す (G) / 最大化 (E) / 全画面表示 (F11)。
    # post.md 本文表示中の戻り導線（UIレビュー 2026-08-28 N-87）。
    "viewer.stage_view.back_to_media": "画像に戻る",
    "viewer.stage_view.back_to_media_tooltip": "この項目の先頭の画像・動画へ戻ります",
    "viewer.stage_view.back_to_split": "分割ビューに戻す (G)",
    "viewer.stage_view.back_to_split_tooltip": "分割ビューに戻します (G / Esc)",
    "viewer.stage_view.fullscreen_btn_tooltip": "全画面表示で開きます (F11)",
    # 非メディア（.part / ZIP / PDF / テキスト）表示中は、全画面が「いま見て
    # いるもの」ではなくフォルダの先頭メディアへ移る（G05 の意図した機能）。
    # ボタン自身がそれを予告する（UIレビュー 2026-08-28 N-21）。
    "viewer.stage_view.fullscreen_folder": "フォルダを流し見 (F11)",
    "viewer.stage_view.fullscreen_folder_tooltip": "表示中のファイルは画像・動画ではないため、このフォルダを先頭の画像・動画から全画面で流し見します (F11)",
    "viewer.stage_view.maximize": "最大化 (E)",
    "viewer.stage_view.maximize_tooltip": "プレビューを最大化します (E)",
    # 「画像・動画」= 位置カウンタの母集合（UIレビュー 2026-08-28 N-22①）。
    # 旧文言は「画像の位置」と説明していたが、母集合は画像限定ではない。
    "viewer.stage_view.next_item_tooltip": "グリッドの次の項目（フォルダ・ファイル）へ移動します。隣の n/m は開いている項目の中の画像・動画の位置です (Ctrl+→ は最大化中)",
    "viewer.stage_view.position": "{n}/{m}",
    # 右一覧のサブフォルダ行を選ぶと表示中はフォルダになる。拡張子由来の
    # 種別ラベルだと「作品集 vol.2」が種別「2」に見えていた（N-137）。
    "viewer.stage_view.position_folder_tooltip": "フォルダを表示中です。位置カウンタは項目の中の画像・動画だけを数えます",
    # 表示中が画像・動画でないときは n/m の代わりに種別を出す（N-22②）。
    # 数えられない対象に嘘の位置を出さないための切替。拡張子だけだと
    # 「PART」「ZIP」が何を指すか読めないので「〜ファイル」まで名乗る（N-114）。
    "viewer.stage_view.position_kind": "{kind} ファイル",
    "viewer.stage_view.position_kind_tooltip": "表示中は画像・動画ではないファイル（{kind}）です。位置カウンタは画像・動画だけを数えます",
    # .part は拡張子を見せても伝わらないので健全性チェックと同じ語彙で言う。
    "viewer.stage_view.position_part": "ダウンロード途中",
    "viewer.stage_view.position_part_tooltip": "ダウンロードが中断して残った書きかけファイルです。位置カウンタは画像・動画だけを数えます",
    # post.md 本文を表示中の現在地表示（N-87）。post.md は母集合の外なので
    # n/m もトラックのハイライトも付かない — 「無所属」に見えないよう名指す。
    "viewer.stage_view.position_post_body": "投稿本文",
    "viewer.stage_view.position_post_body_tooltip": "投稿の本文（post.md）を表示中です。画像・動画の位置カウンタの対象外です",
    "viewer.stage_view.position_tooltip": "開いている項目の中の画像・動画の位置です（‹ › はグリッドの項目単位の移動）",
    "viewer.stage_view.prev_item_tooltip": "グリッドの前の項目（フォルダ・ファイル）へ移動します。隣の n/m は開いている項目の中の画像・動画の位置です (Ctrl+← は最大化中)",
    # -- viewer.tag_browser.* --------------------------------------------
    "viewer.tag_browser.add_to_exclude": "除外に追加",
    "viewer.tag_browser.add_to_search": "検索に追加",
    "viewer.tag_browser.col_image_count": "画像数",
    # 追加先のポップオーバーが閉じていて反映が見えなかった (UIレビュー 07-25
    # #28) — 積算状態をダイアログ内で完結させる読み取り専用のチップ列。
    "viewer.tag_browser.current_terms_empty": "（まだ追加されていません）",
    "viewer.tag_browser.current_terms_label": "現在の検索条件:",
    # (UIレビュー 09-11 N-102) 絞り込みで 0 件になっただけのときに「タグ統計が
    # ありません。」と出すのは誤案内 — 統計は正常でも日常的に起こる。
    "viewer.tag_browser.no_filter_match": "「{query}」に一致するAIタグがありません",
    "viewer.tag_browser.no_tag_stats": "タグ統計がありません。",
    "viewer.tag_browser.select_hint": "行を選択して追加、またはダブルクリックで追加",
    "viewer.tag_browser.showing_n": "{n} 件を表示",
    "viewer.tag_browser.showing_n_top": "{n} 件を表示（上位のみ）",
    "viewer.tag_browser.window_title": "AIタグ一覧",
    # -- viewer.tag_chips.* ----------------------------------------------
    # (UIレビュー 09-11 N-70) チップの隠れ機能を 1 行で予告する。
    "viewer.tag_chips.chip_tooltip": "ドラッグで並べ替え / 右クリックで除外に切替 / 入力欄が空のとき Backspace で左のAIタグを外します",
    # (UIレビュー 09-11 N-103) 検索条件から 1 語外すだけの操作に、実体削除と
    # 同じ「削除」(common.action.delete) を出さない。
    "viewer.tag_chips.remove": "このAIタグを外す",
    "viewer.tag_chips.toggle_to_exclude": "除外に切替",
    "viewer.tag_chips.toggle_to_include": "含めるに切替",
    # -- viewer.toolbar.* ------------------------------------------------
    "viewer.toolbar.layout_label": "表示形式",
    "viewer.toolbar.mode_ai": "AIタグ",
    "viewer.toolbar.mode_ai_tooltip": "AIタグで検索します（AI 検索ポップオーバーを開いてタグ入力へ）。",
    "viewer.toolbar.mode_body": "本文",
    # 単独の全文検索ダイアログは廃止（issue #81）— 本文検索の入口はこのチップ
    # （= body: 構文）だけなので、対象範囲の制約をここで言い切る。
    "viewer.toolbar.mode_body_tooltip": "表示中の一覧の post.md 本文を検索します（検索語を body: 構文に変換）。\n対象は表示中のフォルダ直下の投稿です（「サブフォルダも検索」でも本文は直下のみ）。",
    # AI 検索中は 名前 / 本文 モードへ入れない（不変条件）— 無効化した見た目に
    # 合わせて理由も答える (UIレビュー 2026-08-28 N-05)。
    "viewer.toolbar.mode_body_tooltip_ai": "AI 検索の結果を表示中は本文モードへ切り替えられません（AIタグ条件を解除すると戻ります）。",
    # 一覧（横断キュレーション / 最近追加）の母集合は post.md を持たないので
    # 本文は 1 件も照合できない — 押せると絞り込みが黙って全解除される。
    "viewer.toolbar.mode_body_tooltip_overlay": "一覧を表示中は本文モードへ切り替えられません（一覧を閉じると戻ります）。\n一覧の絞り込みは名前・タイトル・相対パスで照合します。",
    "viewer.toolbar.mode_name_tooltip": "名前・タイトル・投稿タグで検索します（既定）。",
    "viewer.toolbar.mode_name_tooltip_ai": "AI 検索の結果を表示中は名前モードへ切り替えられません（AIタグ条件を解除すると戻ります）。\n検索欄は AI 検索結果の絞り込みとして使え、名前・タイトルのみで照合します（投稿タグ・本文は対象外）。",
    # (UIレビュー 07-25 #24 / #100) メニューバーの「表示(V)」と同名で中身が別
    # だったため改名し、アイコンのみ化に伴い中身をツールチップで列挙する。
    "viewer.toolbar.view_label": "並び・表示",
    "viewer.toolbar.view_tooltip": "並び・表示（並び順・表示形式・サムネイルサイズ・表示オプション）",
    # -- viewer.zip_drill.* ----------------------------------------------
    "viewer.zip_drill.cannot_create_temp": "一時フォルダを作成できません",
    "viewer.zip_drill.cannot_open_zip": "ZIPを開けません",
    "viewer.zip_drill.extract_dialog_title": "ZIP展開",
    "viewer.zip_drill.extract_error_body": "{name}: {message}",
    "viewer.zip_drill.extract_error_title": "ZIP展開エラー",
    "viewer.zip_drill.extract_partial_toast": "一部のエントリを展開できませんでした（{count:,}件）",
    "viewer.zip_drill.extracted_status": "ZIPを展開: {name}",
    "viewer.zip_drill.extracting_progress": "ZIPを展開中: {name}",
    # UIレビュー 2026-08-28 N-96: サイズ probe 中の受理表示（cold NAS では
    # 展開ダイアログが出るまで数秒〜数十秒の無反応があった）。
    "viewer.zip_drill.probing_progress": "ZIPを確認中: {name}",
    "viewer.zip_drill.zip_size_limit_body": "このZIPは {size_mb:.1f} MB あり、上限の {limit_mb} MB を超えるためフォルダとして開けません。中身の一覧のみ表示します。",
    "viewer.zip_drill.zip_size_limit_title": "ZIPサイズ上限",
}
