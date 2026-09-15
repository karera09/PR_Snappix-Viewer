"""ユーザーキュレーション層の部品サブパッケージ.

``user_meta.py`` から、ストア本体の外側で独立して読める部分を切り出した
置き場。ストア :class:`~snappix.viewer.user_meta.UserMetaStore` と、行の同一性
を決めるキー計算（``absolute_spelling`` / ``normalize_entry_key``）とタグ・★の
正規化は ``user_meta.py`` に残る:

* :mod:`~snappix.viewer.user_meta_parts.resolve` — 到達性の判定（消えた /
  読めなかった）・改名追従の postref 台帳 :class:`MovedResolver` と
  ``build_moved_resolver``・横断一覧のパス解決 :class:`CurationResolve` と
  ``resolve_curation_paths``（全て Qt 非依存、ワーカースレッドから呼ぶ）

パッケージ名が ``user_meta`` ではなく ``user_meta_parts`` なのは、同じ階層の
モジュール ``user_meta.py`` と名前が衝突するため。

依存の向きは **部品 → ストアは無し**（部品は ``user_meta.py`` を import
しない）。``user_meta.py`` は従来の公開名を全てここから re-export するので、
外から見た import 面は分割前と変わらない。

**キー計算をここへ動かしてはならない**: ``tests/test_viewer_user_meta_path_keys.py``
はキー経路の呼び出し閉包が ``user_meta.py`` の中で閉じることを AST で要求する
（呼び先が別モジュールにあると閉包走査が本体を一度も検査しないまま素通りし、
#133 の差し戻し理由がそのまま戻る）。``clamp_star`` / ``split_user_tags`` /
``join_user_tags`` もその閉包の中（``load_all`` → ``_row_to_meta``）にある。
"""

from __future__ import annotations
