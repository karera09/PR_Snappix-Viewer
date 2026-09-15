"""閲覧モードの端ラッチとオートハイドの純ロジック（Qt 非依存）.

「いつ投稿を跨いでよいか」「いつクロームを隠してよいか」の判定だけを持つ
状態機械 2 つ。タイマー駆動と実際の show / hide はウィジェット側
（``lightbox.LightboxWindow``）が行うので、ここは時刻（``now``）を引数で
受け取るだけで時計にも Qt にも触らない — 実時間を待たずにテストできる。
"""

from __future__ import annotations

# 投稿横断ラッチ: エッジで 1 回目の←/→が予告、この秒数以内の同方向 2 回目で確定。
_CROSS_ARM_TIMEOUT_SEC = 2.5
# オートハイド: 操作が止まってからこの時間で上部バー・操作カプセル・カーソルを
# 隠す**既定値**。実効値は ``ViewerState.lightbox_chrome_hide_ms``（設定
# ダイアログ）で、ホストが ``LightboxWindow.set_chrome_hide_ms`` で流し込む。
# フィルムストリップ（下端の画像リスト）も同じ時間で自動消灯する（制御系統は
# 別 — 「画像を移動したときだけ表示」。中央での拡大・パン等のマウス操作では
# 出さず、下端リビール帯へのホバー時のみ手動で呼び出せる）。
_AUTO_HIDE_MS = 1500
# 設定で選べる下限（これ未満だと表示が一瞬で消えて操作できない）。
_AUTO_HIDE_MIN_MS = 100
# 下端の何 px を「ストリップ召喚帯」とするか（ストリップ高さへの上乗せマージン）。
_STRIP_REVEAL_MARGIN = 20


class EdgeLatch:
    """投稿横断の誤爆防止ラッチ（Qt 非依存・テスト可能）.

    プレイリスト末尾/先頭で←/→を押しても即座には隣の投稿へ移らず、1 回目は
    アーム（予告表示）だけ行い、``timeout_sec`` 以内の**同方向** 2 回目で確定
    する。``edge_nav`` の grace 機構（スクロール端→ナビの猶予）と同じ
    「一度止まってもう一度で確定」思想の離散キー版。方向が変わるか放置で
    再アームに戻る。
    """

    def __init__(self, timeout_sec: float = _CROSS_ARM_TIMEOUT_SEC) -> None:
        self._timeout = timeout_sec
        self._direction = 0
        self._armed_at = 0.0

    def reset(self) -> None:
        self._direction = 0
        self._armed_at = 0.0

    def is_armed(self, direction: int, now: float) -> bool:
        return (
            self._direction == direction
            and now - self._armed_at <= self._timeout
        )

    def try_cross(self, direction: int, now: float) -> bool:
        """``True`` = 確定（遷移してよい）。``False`` = 今回はアームのみ。"""
        if not self.is_armed(direction, now):
            self._direction = direction
            self._armed_at = now
            return False
        self.reset()
        return True


class AutoHideEngine:
    """オートハイド UI の純ロジック状態機械（Qt 非依存・テスト可能）.

    「アクティビティで表示、``timeout_sec`` 静止で非表示」の判定だけを持ち、
    タイマー駆動・実際の show/hide はウィジェット側（:class:`LightboxWindow`）
    が行う。
    """

    def __init__(self, timeout_sec: float = _AUTO_HIDE_MS / 1000.0) -> None:
        self.timeout_sec = timeout_sec
        self.visible = True
        self._last_activity = 0.0

    def activity(self, now: float) -> bool:
        """アクティビティを記録し、非表示→表示に遷移したら ``True``。"""
        self._last_activity = now
        changed = not self.visible
        self.visible = True
        return changed

    def should_hide(self, now: float) -> bool:
        return self.visible and (now - self._last_activity) >= self.timeout_sec

    def remaining_sec(self, now: float) -> float:
        """非表示にしてよくなるまでの残り秒（0 以上）。

        タイマーが予定より**早く**着火したとき（Qt の CoarseTimer は最大 5%
        前後にずれる）に、捨てずにこの残りで張り直すための値。
        """
        return max(0.0, self.timeout_sec - (now - self._last_activity))

    def mark_hidden(self) -> None:
        self.visible = False


__all__ = ["AutoHideEngine", "EdgeLatch"]
