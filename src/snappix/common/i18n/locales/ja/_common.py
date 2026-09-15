"""Japanese catalog fragment: ``common.*`` — strings shared across tools.

Canonical, reused wordings (dialog buttons, labels, media types, status).
Every call site that shows one of these uses the ``common.*`` key, so the
wording is defined once — the machine-enforced 表記揺れ guard
(``tests/test_i18n.py::test_no_duplicate_values``) keeps it that way.

Auto-organised by sub-namespace; edit a value here to change every screen
that shows it.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # -- common.action.* -------------------------------------------------
    "common.action.abort": "中止",
    "common.action.cancel": "キャンセル",
    "common.action.choose": "選択",
    "common.action.choose_folder": "フォルダを選択",
    "common.action.close": "閉じる",
    "common.action.copy": "コピー",
    "common.action.delete": "削除",
    # 破壊的確認モーダルの動詞ラベル（`common/ui/buttons.py::confirm_action`）。
    # 「はい」では何に同意したのか残らないので、押すボタン自体が行為を名乗る
    # （UIレビュー 2026-08-28 N-01 / N-40）。
    "common.action.delete_confirm": "削除する",
    "common.action.delete_permanently": "完全に削除する",
    "common.action.move_down": "下へ",
    "common.action.move_up": "上へ",
    "common.action.next_page": "次のページ",
    "common.action.ok": "OK",
    "common.action.open": "開く",
    "common.action.open_in_explorer": "エクスプローラで開く",
    "common.action.open_with_default": "既定アプリで開く",
    "common.action.prev_page": "前のページ",
    "common.action.quit": "終了",
    "common.action.rename": "名前を変更",
    "common.action.restore_defaults": "既定値に戻す",
    "common.action.retry": "再試行",
    "common.action.settings": "設定…",
    # -- common.category.* -----------------------------------------------
    "common.category.navigation": "ナビゲーション",
    # -- common.file_dialog.* ---------------------------------------------
    # Qt のファイルダイアログのうち、``qtbase_ja.qm`` が届かない .ui 由来の
    # ラベル（viewer/dialogs.py が setLabelText で埋める — N-01）。
    "common.file_dialog.file_name": "ファイル名:",
    "common.file_dialog.file_type": "ファイルの種別:",
    "common.file_dialog.folder": "フォルダ:",
    "common.file_dialog.look_in": "場所:",
    # -- common.filter.* -------------------------------------------------
    "common.filter.all": "すべて",
    # -- common.label.* --------------------------------------------------
    "common.label.details": "詳細",
    "common.label.display": "表示",
    "common.label.file": "ファイル",
    "common.label.folder": "フォルダ",
    "common.label.name": "名前",
    "common.label.path": "パス",
    "common.label.posted_colon": "投稿日:",
    "common.label.search": "検索",
    "common.label.size": "サイズ",
    "common.label.tag": "タグ",
    "common.label.thumb": "サムネイルサイズ",
    # ファイル種別の呼称は「種別」1 本（「種類」は廃止 — UIレビュー 07-25 #103）。
    # 右ファイル一覧の並び順と詳細情報 / 情報パネルの行ラベルが同じ語を共有する。
    "common.label.type": "種別",
    "common.label.type_colon": "種別:",
    # -- common.legal.* ----------------------------------------------------
    "common.legal.file_missing_body": "{name} が見つかりませんでした。\n配布フォルダ（実行ファイルと同じ場所）にあるファイルをご確認ください。",
    "common.legal.file_missing_title": "ファイルが見つかりません",
    "common.legal.open_failed_body": "{name} を開けませんでした。\nお使いの環境でテキストファイルを開くアプリが設定されていない可能性があります。\n配布フォルダ（実行ファイルと同じ場所）から直接開いてください。",
    "common.legal.open_failed_title": "ファイルを開けません",
    "common.legal.terms_menu": "利用規約・免責事項…",
    "common.legal.third_party_menu": "サードパーティライセンス…",
    # -- common.media_type.* ---------------------------------------------
    "common.media_type.archive": "アーカイブ",
    "common.media_type.audio": "音声",
    "common.media_type.document": "文書",
    "common.media_type.image": "画像",
    "common.media_type.video": "動画",
    # -- common.menu.* -----------------------------------------------------
    "common.menu.help": "ヘルプ(&H)",
    # -- common.punct.* --------------------------------------------------
    # 「…」= 押すとさらに入力 / 別の窓が要る、のメニュー規約記号。付け方を
    # 1 箇所に閉じる（viewer/_menu_text.py::menu_label — UIレビュー N-105）。
    "common.punct.ellipsis": "…",
    # -- common.sep.* ----------------------------------------------------
    "common.sep.comma": "、",
    "common.sep.middot": " ・ ",
    # -- common.status.* -------------------------------------------------
    "common.status.loading": "読み込み中…",
    "common.status.loading_name": "読み込み中… ({name})",
    "common.status.waiting": "待機中",
    # -- common.terms_dialog.* --------------------------------------------
    # UIレビュー 07-25 #123: terms_dialog.py の直書き文言を i18n カタログへ
    # 移設。#42: 規約改定による再同意時は intro_revised + version_label へ
    # 切り替える（terms_text.py の本文・TERMS_VERSION は不変）。
    "common.terms_dialog.agree_btn": "同意する",
    "common.terms_dialog.decline_btn": "同意しない",
    "common.terms_dialog.declined_notice": "利用規約に同意されなかったため、Snappix Viewer を終了します。\nご利用には利用規約への同意が必要です。次回起動時に改めてご確認いただけます。",
    "common.terms_dialog.hint_after": "ご確認ありがとうございます。内容にご同意のうえ、選択してください。",
    "common.terms_dialog.hint_before": "※ 本文を最後までスクロールすると「同意する」が押せるようになります。",
    "common.terms_dialog.hint_progress": "※ 本文を最後までスクロールすると「同意する」が押せるようになります（現在 {pct}%）。",
    "common.terms_dialog.intro": "本ソフトウェアを利用する前に、以下の利用規約・免責事項を最後までお読みください。内容に同意される場合は「同意する」を、同意されない場合は「同意しない」を押してください。「同意しない」を選ぶと本ソフトウェアは終了します。",
    "common.terms_dialog.intro_revised": "利用規約が改定されました（改定日: {date}）。改定後の内容を最後までお読みください。内容に同意される場合は「同意する」を、同意されない場合は「同意しない」を押してください。「同意しない」を選ぶと本ソフトウェアは終了します。",
    "common.terms_dialog.version_label": "現在の版: {version}",
    "common.terms_dialog.window_title": "Snappix Viewer 利用規約・免責事項",
    # -- common.theme.* --------------------------------------------------
    "common.theme.dark": "ダーク",
    "common.theme.extra_astro": "天文台の赤色灯",
    "common.theme.extra_brass": "真鍮の計器盤",
    "common.theme.extra_dusk": "薄暮の刻",
    "common.theme.extra_linen": "亜麻色のアトリエ",
    "common.theme.extra_obsidian": "黒曜と熔岩",
    "common.theme.extra_washi": "書院の和紙",
    "common.theme.light": "ライト",
    "common.theme.standard": "標準（ダーク）",
    # -- common.view.* ---------------------------------------------------
    "common.view.grid": "グリッド",
    "common.view.list": "リスト",
}
