"""壊れた代表画像 → 同じフォルダの次候補、の帳簿（中央プレビューとフォルダプレビューで共有）.

画面が**自分で選んだ**代表画像（``shown``）のデコード失敗だけを受け、その候補を
skip に足して次候補の探索（:func:`~.folder_scan.next_representative`）を投げ
直させる。明示選択の失敗は差し替えないので、呼び出し側は明示選択のたびに
``shown`` を ``None`` にする。探索の投入と着地は各画面のストリームが持つ。
"""

from __future__ import annotations

from pathlib import Path

#: skip に積める失敗の上限。1 回の探索は最大 ~33 回の scandir なので、壊れた
#: ファイルが並ぶフォルダで NAS を掃き続けない。
FALLBACK_MAX = 3


class RepresentativeFallback:
    """表示中の代表（``shown``）と、デコードに失敗した候補（``skip``）."""

    __slots__ = ("shown", "_folder", "_skip")

    def __init__(self) -> None:
        self.shown: Path | None = None
        self._folder: Path | None = None
        self._skip: frozenset[Path] = frozenset()

    @property
    def skip(self) -> frozenset[Path]:
        return self._skip

    def restart(self, folder: Path | None) -> None:
        """*folder* の通常の探索 — 失敗の記録を忘れる（``shown`` は残す）."""
        self._folder = folder
        self._skip = frozenset()

    def on_failed(self, folder: Path | None, path: Path) -> frozenset[Path] | None:
        """*path* の失敗を受けて次の探索の skip を返す（``None`` = 探し直さない）."""
        if folder is None or path != self.shown:
            return None
        skip = (self._skip if folder == self._folder else frozenset()) | {path}
        if len(skip) > FALLBACK_MAX:
            return None
        self.shown, self._folder, self._skip = None, folder, skip
        return skip
