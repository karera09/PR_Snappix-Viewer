"""Menu-label composition — 「入口名 = 着地タイトル」 を機械的に保証する.

メニュー項目の名前（「ライブラリを管理…」）と開いた窓のタイトル
（「ライブラリの管理」）が食い違うと — 入口とその着地が別の名前を名乗ると —
「押したものが開いたか」を読み手が名前で確認できない。

「入口名」と「窓タイトル」を別々のカタログキーとして独立に書くと食い違う
ので、ここでは窓タイトルを**単一の情報源**とし、メニュー項目はそこから
:func:`menu_label` で機械的に組む — 「…」（押すと窓が開く、の記号）の付け方も
1 箇所に閉じる。
"""

from __future__ import annotations

from ..common.i18n import t


def menu_label(title_key: str) -> str:
    """*title_key* の窓タイトルから、メニュー項目の名前を組む。

    末尾の 「…」 は「押すとさらに入力 / 別の窓が要る」の意味で、この製品の
    メニュー全体が従っている規約。記号そのものはカタログの
    ``common.punct.ellipsis`` に置く（地の文に直書きしないため）。
    """
    return t(title_key) + t("common.punct.ellipsis")


__all__ = ["menu_label"]
