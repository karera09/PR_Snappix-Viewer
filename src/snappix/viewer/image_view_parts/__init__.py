"""中央プレビュー（``image_view.ImageView``）の部品サブパッケージ.

``image_view.py`` にはウィジェットの状態機械（3 段表示の進行・ストリームへの
投入と着地・部品への配線）だけを残し、それ以外をここへ分けている:

* :mod:`~snappix.viewer.image_view_parts.geometry` — ズーム率・フィット枠・
  パン範囲・パッチ矩形の純関数（入力は値、出力も値。Qt ウィジェットに触らない）
* :mod:`~snappix.viewer.image_view_parts.input` — キー / ホイール / ドラッグを
  **意図**（:data:`~snappix.viewer.image_view_parts.input.Intent`）へ翻訳する層
  （``gallery_view_parts.input`` と同じ作法）
* :mod:`~snappix.viewer.image_view_parts.prefetch_ledger` — 先読みの台帳
  :class:`~snappix.viewer.image_view_parts.prefetch_ledger.PrefetchLedger`
  （挿入ガード / 引き継ぎラッチ / デコード窓を 1 値型で持ち、ワーカー側の
  早期降車と着地側の振るいが同じメソッドを引く）
* :mod:`~snappix.viewer.image_view_parts.workers` — ワーカースレッドで走る
  純関数（デコード 2 段 / LANCZOS 全体レンダ / パッチリサンプル / 近傍先読み）
* :mod:`~snappix.viewer.image_view_parts.canvas_label` — 可視領域パッチ描画に
  対応した画像ラベル
  :class:`~snappix.viewer.image_view_parts.canvas_label._ImageCanvasLabel`
* :mod:`~snappix.viewer.image_view_parts.error_card` — デコード失敗の面
  :class:`~snappix.viewer.image_view_parts.error_card._DecodeErrorCard`
* :mod:`~snappix.viewer.image_view_parts.minimap` — 右下のミニマップ
  :class:`~snappix.viewer.image_view_parts.minimap._MinimapOverlay`
* :mod:`~snappix.viewer.image_view_parts.control_bar` — ズーム読み値
  （:func:`~snappix.viewer.image_view_parts.control_bar.format_zoom_readout` /
  :class:`~snappix.viewer.image_view_parts.control_bar._ZoomOverlayLabel`）と
  下端の操作カプセル
  :class:`~snappix.viewer.image_view_parts.control_bar._ControlBar`

パッケージ名が ``image_view`` ではなく ``image_view_parts`` なのは、同じ階層
のモジュール ``image_view.py`` と名前が衝突するため（``lightbox_parts`` と
同じ理由・同じ形）。

依存の向きは **部品 → ビューは無し**（部品は ``image_view.py`` を import
しない）。``image_view.py`` は分割前の名前を全て re-export するので、外から
見た import 面は分割前と変わらない。
"""

from __future__ import annotations
