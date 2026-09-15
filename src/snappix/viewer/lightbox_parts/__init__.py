"""閲覧モード（ライトボックス）の部品サブパッケージ.

``lightbox.py`` から独立部品を切り出した置き場。窓本体
（``lightbox.LightboxWindow``）だけが ``lightbox.py`` に残り、そこから
独立して読める部分をモジュールごとに分ける:

* :mod:`~snappix.viewer.lightbox_parts.scan` — プレイリスト列挙と投稿横断の
  深さ優先探索（Qt 非依存・ワーカースレッドから呼ぶ純関数群）
* :mod:`~snappix.viewer.lightbox_parts.auto_hide` — 端ラッチとオートハイドの
  純ロジック状態機械（Qt 非依存）
* :mod:`~snappix.viewer.lightbox_parts.overlays` — 固定オーバーレイ色と、
  その色で描くクローム部品（カウンタ / ヒント / 中央メッセージ / 上部バー /
  操作カプセル / 空プレイリストのページ）
* :mod:`~snappix.viewer.lightbox_parts.filmstrip` — 下端のサムネイル帯

パッケージ名が ``lightbox`` ではなく ``lightbox_parts`` なのは、同じ階層の
モジュール ``lightbox.py`` と名前が衝突するため。

依存の向きは **部品 → 窓は無し**（部品は ``lightbox.py`` を import しない）。
``lightbox.py`` は従来の公開名を全てここから re-export するので、外から見た
import 面は分割前と変わらない。
"""

from __future__ import annotations
