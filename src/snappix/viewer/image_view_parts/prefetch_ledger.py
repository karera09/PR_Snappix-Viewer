"""``ImageView`` の先読み台帳（protect / adopt / decode window を 1 値型で）。

先読みの是非は 3 か所から問われる:

* ワーカースレッド — :func:`~snappix.viewer.image_view._prefetch_decode` が
  走り出す前と原寸デコードの直後に「この答えはまだ要るか」を聞いて早期降車する
* 着地スロット — :meth:`~snappix.viewer.image_view.ImageView._on_prefetch_landed`
  が同じ問いで前の近傍集合の残骸をふるう
* 右ペインのスピナー — 「いまデコードが進行中と言えるか」

3 つが別々の属性を手で読み書きしていると、「ワーカーは降りたのに着地は通る」
ような片側のずれが起こせる。ここに 1 つの値型として置き、**同じメソッド**
(:meth:`PrefetchLedger.wanted`) を両側が引くことでそれを構造的に禁じる。

この層は Qt にもファイルシステムにも触らない — 入力は ``Path`` / ``str``、
状態は ``str`` の集合と辞書だけ。ワーカースレッドから読まれるのは
:meth:`wanted` の ``str`` メンバシップ（GIL アトミック）のみ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PrefetchLedger:
    """先読み 1 セットの台帳。

    :attr:`protect` — ターゲットごとの**距離認識の挿入ガード**。値は「表示中の
    画像 + それより近いターゲット」のキー列で、着地した先読みはこれらを追い
    出してまで入ってはいけない。バイト予算が先読みのワーキングセットより小さい
    とき、素の LRU は**ちょうど逆**の順に追い出す（±2 の結果が届く頃には表示中
    の画像が最も古い挿入になっている）。

    :attr:`adopt_key` — 引き継ぎラッチ。表示要求は先読みストリームを畳まない
    代わりにここへ「いま表示しようとしているファイル」を記録する。単一の先読み
    ワーカーがちょうどそのファイルをデコード中なら、それが全解像度表示への
    最短経路なので殺さず、着地した結果をそのまま表示へ採用する。

    :attr:`decode_window` — 「いまデコードが進行中」と言えるパスの集合。右ペインの
    pending スピナーの唯一の判断材料で、「LRU に載っているか」とは別物: LRU は
    選択中 ± 先読み半径しか保持しないので、それより外の画像は何も起きていない
    のに恒久的に「未キャッシュ」になる。

    :attr:`inflight` — 投入済みで未着地の先読み。近傍集合の入れ替えは
    ストリームを畳まずにこれを見て「まだ投げていないもの」だけを積む: 走行中の
    デコードが新しい集合でも要るなら捨てずにそのまま着地させ（巨大 CG では
    1 枚 0.5〜1 秒の損失になる）、要らなくなった前の集合の残りは
    :meth:`wanted` 越しに走り出した直後に降りる。GUI スレッドだけが読み書きする。
    """

    protect: dict[str, tuple[str, ...]] = field(default_factory=dict)
    adopt_key: str | None = None
    decode_window: set[str] = field(default_factory=set)
    inflight: set[str] = field(default_factory=set)

    # ------------------------------------------------------------ 問い合わせ

    def wanted(self, path: Path) -> bool:
        """この先読みの答えがまだ要るか（ワーカーと着地の**共通**述語）。

        母集合は「現在の近傍集合」+「採用ラッチ」。ワーカースレッドからも
        呼ばれるので、読むのは ``str`` のメンバシップだけに保つこと。
        """
        key = str(path)
        return key in self.protect or key == self.adopt_key

    def protect_for(self, key: str) -> tuple[str, ...]:
        """*key* の着地が追い出してはいけないキー列（無ければ空）。

        採用された結果は現在画像そのもので、近傍集合の一員ではないので自然に
        空が返る（= 素の LRU として入る）。
        """
        return self.protect.get(key, ())

    def adopts(self, key: str, current: Path | None) -> bool:
        """*key* の着地を「いま表示すべきもの」として採用してよいか。"""
        return (
            key == self.adopt_key
            and current is not None
            and str(current) == key
        )

    def pending(self, key: str) -> bool:
        """*key* のデコードが進行中と言える窓の中にあるか。"""
        return key in self.decode_window

    def needs_submit(self, key: str) -> bool:
        """*key* の先読みを投げるべきか（まだ投げておらず、近傍集合の一員）。

        キャッシュ済みかどうかは呼び出し側（LRU の持ち主）が見る。
        """
        return key in self.protect and key not in self.inflight

    # ---------------------------------------------------------------- 更新

    def begin_show(self, path: Path) -> None:
        """表示要求で台帳を張り替える（先読みストリームは畳まない）。

        「まだ要る」と言えるのはこの 1 枚だけ — 前の近傍集合は用済みなので
        :attr:`protect` を空にし、走行中のワーカーは :meth:`wanted` 越しに
        それを読んで降りる。唯一残すのがこのパス（採用ラッチ）。
        """
        key = str(path)
        self.protect = {}
        self.adopt_key = key
        self.decode_window = {key}

    def plan(self, current: Path, targets: list[Path]) -> None:
        """近傍集合 *targets* の挿入ガードとデコード窓を組み直す。

        ガードは**全ターゲット**に対して作る（既にキャッシュ済みのものも
        含む）— 既に載っている近い隣接を、遠い着地から守るため。
        """
        protect_acc: list[str] = [str(current)]
        protect_map: dict[str, tuple[str, ...]] = {}
        for path in targets:
            key = str(path)
            protect_map[key] = tuple(protect_acc)
            protect_acc.append(key)
        self.protect = protect_map
        self.decode_window = {str(current), *(str(p) for p in targets)}

    def mark_inflight(self, key: str) -> None:
        """*key* の先読みを投げた（着地まで二重に投げない）。"""
        self.inflight.add(key)

    def landed(self, key: str) -> None:
        """*key* の先読みが着地した（成功・失敗・降車のどれでも呼ぶ）。"""
        self.inflight.discard(key)

    def cacheable_target_count(self, max_entries: int) -> int:
        """挿入ガードの下で構造上キャッシュに載り得るターゲット数。

        k 番目（0 始まり）のターゲットは「現在画像 + より近い k 件」のガード
        付きで put されるため、近傍が揃った時点で ``k + 2 > max_entries`` の
        挿入は恒久的に拒否される。拒否されるファイルは残らないので、発行すると
        ナビゲーションのたびにフルデコード → 破棄を繰り返し、単一の先読み
        ワーカーを無為に占有する。
        """
        return max(0, max_entries - 1)

    def settle(self, path: Path | None) -> None:
        """決着したパスをデコード窓から外す。

        述語の意味は「載っているか」ではなく「終わったか」。載らない経路は 2 つ
        とも正常動作（有界キャッシュが単品上限超の値を拒否する場合と、QMovie
        経路がそもそも PIL の LRU を使わない場合）なので、決着した時点で外す。
        """
        if path is not None:
            self.decode_window.discard(str(path))

    def reset(self) -> None:
        """採用ラッチと挿入ガードを捨てる（フォルダ切替・クリア）。

        跨フォルダの採用は古いデコードを蘇らせるので、ラッチもガードも
        キャッシュと一緒に落とす。デコード窓は**触らない** — 進行中デコードの
        決着は :meth:`settle` が受け持ち、ここで先に消すと決着前のスピナーが
        窓の外に出て、着地の取りこぼしが見えなくなる。

        :attr:`inflight` は空にする — 呼び出し元はストリームの ``cancel`` と
        対で呼ぶので、畳まれた世代の着地はもう届かない（残すと、同じパスを
        二度と投げなくなる）。
        """
        self.protect = {}
        self.adopt_key = None
        self.inflight.clear()


__all__ = ["PrefetchLedger"]
