"""``advanced_search.py`` の部品（クエリ値型 / ホスト境界 / 席 / 走査 / 往復）.

分割の軸は「Qt に触るか」「誰の状態を持つか」:

* :mod:`.query` — クエリの値型と純粋な導出（Qt 非依存・単体テスト可能）
* :mod:`.host` — コントローラが触ってよいホストの面（``Protocol``）
* :mod:`.popover` — ポップオーバーの席と配線（判断は持たない）
* :mod:`.scan` — スキャナの生成 / 投入 / 着地ガード
* :mod:`.persistence` — ``ViewerState`` との設定往復
* :mod:`.facade` — ``PostGrid`` 側のホスト読み替えと旧名の委譲（表 1 枚）
"""
