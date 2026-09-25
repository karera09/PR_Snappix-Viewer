"""テキスト系プレビューの符号推定（Qt 非依存・ワーカースレッドから呼ぶ）。

プレーンテキスト（``content.text_view``）と Markdown 本文
（``markdown_view``）が同じ判定を使うための 1 本口。どちらも「上限で先頭
だけ読む」ので、切り詰めた末尾に半端な多バイト文字が残る前提で扱う。
"""

from __future__ import annotations

import codecs

#: BOM の無い入力で試す符号の順（strict）。成功が信頼できる順に並べる:
#: UTF-8 は非 UTF-8 の入力でほぼ必ず失敗するので先頭。cp932（Shift_JIS の
#: 上位集合）と EUC-JP は重なるが、Windows 由来の日本語の方がはるかに多い
#: ので cp932 を先にする。
_FALLBACK_CODECS: tuple[str, ...] = ("utf-8", "cp932", "euc_jp")


def _strict_decode(raw: bytes, encoding: str, *, truncated: bool) -> str:
    """*raw* を *encoding* で strict にデコードする（失敗は例外で返す）。

    *truncated* のときは末尾の**不完全な**多バイト列だけを許す — 増分デコーダ
    を ``final=False`` で 1 回呼ぶと、末尾の半端な列はバッファに残って捨て
    られ、途中の不正なバイトは従来どおり ``UnicodeDecodeError`` になる。上限
    での切断はほぼ必ず多バイト文字の途中に当たる（日本語 UTF-8 で約 2/3、
    cp932 / EUC-JP でも同程度）ので、これが無いと 1 文字の半端で strict
    デコード全体が失敗し、最後の Latin-1 に落ちて**本文全体**が文字化けする。
    符号ごとの手書きトリム（UTF-8 の継続バイト遡り・UTF-16 の 2 バイト境界と
    サロゲート上位）を置き換える一般解で、UTF-16 の取り残されたサロゲート
    上位も同じ仕組みで落ちる。
    """
    if not truncated:
        return raw.decode(encoding)
    return codecs.getincrementaldecoder(encoding)().decode(raw, final=False)


def decode_text(
    raw: bytes, *, truncated: bool = False, last_resort: str = "latin-1",
) -> str:
    """テキストプレビュー用のベストエフォートなデコード。

    先に成功したものが信頼できる順に試す:

    1. **BOM**（曖昧さが無い）: UTF-16 LE/BE か UTF-8 の BOM があれば符号を
       確定する。``utf-16`` は BOM を消費してエンディアンを読み、
       ``utf-8-sig`` は UTF-8 の BOM を落とす。
    2. BOM の無い **strict UTF-8** → **cp932** → **euc_jp**
       （:data:`_FALLBACK_CODECS`）。
    3. **最後の手段** *last_resort*（``errors="replace"``）。既定の Latin-1 は
       決して失敗しない可逆な受け皿。書式上 UTF-8 のはずの入力（post.md）は
       ``"utf-8"`` を渡し、壊れたバイトだけを U+FFFD にして全文の化けを避ける。

    *truncated* は「*raw* が上限で切った先頭部分である」ことを表し、末尾の
    半端な多バイト文字を捨てる（:func:`_strict_decode`）。
    """
    candidates: list[str] = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates.append("utf-16")
    elif raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    candidates.extend(_FALLBACK_CODECS)
    for enc in candidates:
        try:
            return _strict_decode(raw, enc, truncated=truncated)
        except (UnicodeDecodeError, LookupError):
            pass
    if last_resort == "utf-8" and raw.startswith(b"\xef\xbb\xbf"):
        last_resort = "utf-8-sig"
    return raw.decode(last_resort, errors="replace")
