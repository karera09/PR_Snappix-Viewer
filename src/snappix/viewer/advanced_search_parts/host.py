"""``AdvancedSearchController`` ↔ ホスト（``PostGrid``）の明示インタフェース.

以前この境界は存在しなかった: AI 検索は ``PostGrid`` に混ぜ込まれた mixin で、
ホストの平置き属性を 20 個直読みし、ホストのメソッドを 14 本呼んでいた。
「どちらが何を持つか」はコードを全部読むまで分からず、片方を触ると他方の
どこが壊れるかも読めない。:class:`SearchHost` はその 34 本を**宣言**にして、
コントローラ側が触ってよいホストの面をこの 1 ファイルに閉じる。

規約: コントローラはホストの ``_`` 付き属性を触らない。増やしたくなったら
まずここへ 1 行足す（= 境界が広がったことがレビューに見える）。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from ..folder_scan import FolderEntry


class SearchHost(Protocol):
    """AI 検索コントローラが読む / 呼ぶホストの面。

    **読む**（状態）: :attr:`tag_index` / :attr:`vector_index` /
    :attr:`root_or_folder` / :attr:`filter_locked_only` /
    :attr:`filterbar_media` / :attr:`filterbar_star_min` /
    :attr:`filterbar_later` / :attr:`filterbar_user_tag` /
    :attr:`nsfw_hidden_count` / :attr:`overlay_active` /
    :attr:`requery_suspended` / :attr:`thumb_key_prefix` /
    :meth:`filter_text` / :meth:`general_filter_terms` /
    :meth:`pixmap_for_key` / :meth:`current_tile_path` /
    :meth:`shown_tile_count` / :meth:`breadcrumb_has_trail` /
    :meth:`breadcrumb_count_text` / :meth:`panel_reset_dimensions`。

    **読む**（ホストが席を持つウィジェット）: :attr:`rating_combo` /
    :attr:`date_combo` / :attr:`date_from` / :attr:`date_to` /
    :attr:`date_separator` / :meth:`ai_chip_anchor` / :meth:`widget`。

    **呼ぶ**: :meth:`rebuild_grid` / :meth:`preserve_selection_for_rebuild` /
    :meth:`set_search_status` / :meth:`sorted_dir_first` /
    :meth:`drop_thumb_markers` / :meth:`update_filter_bar` /
    :meth:`sync_search_mode_chips` / :meth:`set_breadcrumb_count_text` /
    :meth:`strip_filter_control_field` / :meth:`batched_condition_clear` /
    :meth:`set_hide_nsfw` / :meth:`clear_filter_text` /
    :meth:`set_locked_only_checked` / :meth:`exit_overlay` /
    :meth:`maybe_start_recursive_scan` / :meth:`on_date_filter_changed` /
    :meth:`set_search_indexes` / :meth:`request_tag_db_reload`。
    """

    # ------------------------------------------------------------ 読む（状態）

    @property
    def tag_index(self):
        """開いている ``tags.db`` リーダ（``None`` = 無い / 開けない）。"""

    @property
    def vector_index(self):
        """開いているベクトル索引（``None`` = 無い）。"""

    @property
    def root_or_folder(self) -> Path | None:
        """走査の起点（``None`` ならスキャンを投げない）。"""

    @property
    def filter_locked_only(self) -> bool:
        """🔒 のみ表示（AI 結果は post.md 未読なので一律抑止する軸）。"""

    @property
    def filterbar_media(self) -> str:
        """フィルターポップオーバー側の種別（AI 側の種別と AND する別軸）。"""

    @property
    def filterbar_star_min(self) -> int:
        """★ の下限（0 = 無制限）。"""

    @property
    def filterbar_later(self) -> bool:
        """「あとで見る」のみ。"""

    @property
    def filterbar_user_tag(self) -> str:
        """ユーザータグ（``""`` = すべて）。"""

    @property
    def nsfw_hidden_count(self) -> int:
        """直近の再構築で「年齢制限を隠す」が落としたタイル数。"""

    @property
    def overlay_active(self) -> bool:
        """横断キュレーション / 最近追加一覧がグリッドを占有しているか。"""

    @property
    def requery_suspended(self) -> bool:
        """次元を一括で中立化している最中か（途中の条件で蹴らない）。"""

    @property
    def thumb_key_prefix(self) -> str:
        """サムネキャッシュのキー接頭辞（類似シードのプレビュー解決用）。"""

    def filter_text(self) -> str:
        """絞り込み欄の現在値（前後の空白は落とす）。"""

    def general_filter_terms(
        self,
    ) -> tuple[list[str], list[str], list[str]]:
        """分野限定でない絞り込み語 ``(includes, excludes, or_pool)``。"""

    def pixmap_for_key(self, key: str):
        """サムネキャッシュから ``QPixmap`` を引く（無ければ ``None``）。"""

    def current_tile_path(self) -> Path | None:
        """選択タイルのパス（フォルダ / 未選択は ``None``）。"""

    def shown_tile_count(self) -> int | None:
        """画面に載っているタイル数（ビュー未構築なら ``None``）。"""

    def breadcrumb_has_trail(self) -> bool:
        """パンくずが経路表示中か（件数表記を持てる状態か）。"""

    def breadcrumb_count_text(self) -> str:
        """パンくずが今表示している件数テキスト。"""

    def panel_reset_dimensions(self) -> list:
        """［詳細条件のみリセット］の対象ビュー次元（台帳の ``panel_reset``）。"""

    # -------------------------------------------- 読む（ホストが席を持つ面）

    @property
    def rating_combo(self):
        """年齢区分コンボ（席はフィルターポップオーバー側）。"""

    @property
    def date_combo(self):
        """投稿日プリセットコンボ（席はフィルターポップオーバー側）。"""

    @property
    def date_from(self):
        """投稿日「範囲」の開始日エディタ。"""

    @property
    def date_to(self):
        """投稿日「範囲」の終了日エディタ。"""

    @property
    def date_separator(self):
        """投稿日「範囲」の区切りラベル。"""

    def ai_chip_anchor(self):
        """AI ポップオーバーの既定アンカー（ツールバーの AIタグチップ）。"""

    def widget(self):
        """ダイアログ / ポップオーバーの親に使う ``QWidget`` 実体。"""

    # ------------------------------------------------------------------ 呼ぶ

    def rebuild_grid(self) -> None:
        """グリッド再構築（表示の単一チョークポイント）。"""

    def preserve_selection_for_rebuild(
        self, *, ancestor_fallback: bool = False,
    ) -> None:
        """再構築を跨いで選択（または親フォルダ）を保つ。"""

    def set_search_status(self, text: str) -> None:
        """ステータス行の文言を差し替える（``""`` で消す）。"""

    def sorted_dir_first(self, entries: list[FolderEntry]) -> list[FolderEntry]:
        """フォルダ優先 + 現在の並び順で整列する。"""

    def drop_thumb_markers(
        self, entries: list[FolderEntry],
    ) -> list[FolderEntry]:
        """``#thumb#`` 除外が ON のときファイル行からマーカーを落とす。"""

    def update_filter_bar(self) -> None:
        """フィルターポップオーバーのアクセント同期。"""

    def sync_search_mode_chips(self) -> None:
        """ツールバーの 名前 / 本文 / AIタグ チップを現在の検索へ合わせる。"""

    def set_breadcrumb_count_text(self, text: str) -> None:
        """パンくずの件数テキストを差し替える。"""

    def strip_filter_control_field(self, field: str) -> None:
        """絞り込み欄からコントロールトークン 1 軸（``score:`` 等）を落とす。"""

    def batched_condition_clear(self) -> AbstractContextManager[None]:
        """複数次元の中立化を 1 回の引き直しへ畳むコンテキストマネージャ。

        ``with`` に入れる値を**返す**（``@contextmanager`` で装飾された
        ジェネレータ関数そのものではない）ので、注釈は ``Iterator`` ではなく
        コンテキストマネージャ。
        """

    def set_hide_nsfw(self, band: str) -> None:
        """「年齢制限を隠す」の帯を設定する（メニュー同期 + 再構築込み）。"""

    def clear_filter_text(self) -> None:
        """絞り込み欄を正規の signal 経路で空にする。"""

    def set_locked_only_checked(self, checked: bool) -> None:
        """🔒 のみ表示を正規の経路で切り替える。"""

    def exit_overlay(self) -> None:
        """占有一覧から退場する（AI 検索がグリッドの単一所有者になる）。"""

    def maybe_start_recursive_scan(self) -> None:
        """再帰ファイル名検索が再び適格になったかを評価して蹴る。"""

    def on_date_filter_changed(self) -> None:
        """投稿日コンボの正規ハンドラ（軸の中立化 + アクセント同期）。"""

    def set_search_indexes(self, tag_index, vector_index) -> None:
        """ホストが握る索引ハンドルを差し替える（再読み込みの反映）。"""

    def request_tag_db_reload(self) -> None:
        """``tags.db`` の再オープンをホストへ要求する。"""
