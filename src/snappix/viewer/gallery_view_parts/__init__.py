"""``GalleryView`` の部品置き場（ウィジェットを持たない層）。

殻の :mod:`..gallery_view` は「イベントを受けて意図を得て、自分の状態を変え、
1 フレームぶんの環境を組んで描画を頼む」だけを持ち、中身はここに分かれる:

* :mod:`.tiles` — 値型（``Tile`` / ``IconSeats``）と種別バケット。
* :mod:`.captions` — キャプションの行分割・省略・描画。
* :mod:`.painter` — タイル 1 枚の描画（バッジ・座席・スクリム・プレース
  ホルダ）。``QPainter`` を受け取り、環境は ``TileStyle`` で渡す。
* :mod:`.empty_card` — 空状態カードの計測・描画・ボタン配置。
* :mod:`.input` — イベント → 意図（``Intent``）の変換。
* :mod:`.selection` — 単一選択モデル。

いずれも ``gallery_view`` が従来の公開名で re-export するので、ホスト
（``children_grid`` / ``folder_preview_view`` / ``main_window``）とテストの
import 元は変わらない。
"""

from __future__ import annotations

from .tiles import IconSeats, Tile, file_icon_bucket

__all__ = ["IconSeats", "Tile", "file_icon_bucket"]
