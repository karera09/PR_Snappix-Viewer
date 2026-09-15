"""保存済みウィンドウジオメトリの復元ガード（本体 / プラグイン共有）。

サブモニタで終了 → そのモニタを外す（または解像度を変更）→ 起動、という
経路で ``QWidget.restoreGeometry`` はどの画面にも無い座標を復元し得る。
ウィンドウは見えない場所に開き、**アプリ内に戻す導線が無い**（表示されて
いないのでタスクバーからのドラッグにも辿り着けない）。

その復帰導線を 2 つの関数に閉じ込める:

* :func:`frame_intersects_any_screen` — フレームがどれか 1 枚の画面（作業
  領域）と交差しているか。
* :func:`center_on_primary` — 既定サイズでプライマリ画面の中央へ**置き直す**。

**この 2 つを対で使うこと**が要点。判定だけ持って復帰側が ``resize`` の
みだと「画面外のウィンドウを画面外のまま大きくする」だけで症状は 1 ミリも
改善しない（issue #128 — ビューア本体とプラグインの窓が同じガードを別々に
手書きしていて、片側だけが no-op のまま残っていた）。共有ヘルパにしてある
のは、同型の片側欠落が構造的に起きないようにするため。
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QWidget


def frame_intersects_any_screen(window: QWidget) -> bool:
    """True when *window*'s frame intersects at least one screen."""
    frame = window.frameGeometry()
    for screen in QApplication.screens():
        if screen.availableGeometry().intersects(frame):
            return True
    return False


def center_on_primary(window: QWidget, size: tuple[int, int]) -> None:
    """*window* を既定 *size* でプライマリ画面の中央へ置き直す（復帰導線）。

    **サイズだけ戻しても救えない**のが要点: 画面外に居るウィンドウを
    ``resize`` しても座標はそのままで、見えないままになる。

    既定サイズは作業領域に**収まるようクランプ**する（レビュー
    2026-08-30）。例えば 1280x800 をそのまま中央寄せすると、作業領域が
    それより小さい画面（1366x768 のノート、150% スケーリングの 1080p など）
    で上端が負になり、**タイトルバーが画面外に出てドラッグで戻せない** —
    このガードが救おうとしている症状そのものになる。ウィンドウ構築時の
    素の ``resize`` は自分で ``move`` しないので WM が正気な位置に置いて
    くれるが、こちらは自前で動かすぶん自分でクランプする必要がある。

    クランプは**枠（タイトルバー + 境界）の厚みを差し引いて**行う（レビュー
    2026-08-30 M-3）。``resize`` が決めるのはクライアント領域なので、
    ``min(size, available.size)`` だけだと枠のぶん（Windows の既定テーマで
    タイトルバー 39px + 境界 16px 前後）が作業領域からはみ出す — 1366x768 の
    ノートで下端がタスクバーの裏に 39px 潜る。**この関数が保証するのは
    2 段構え**:

    1. 枠が作業領域に収まるなら、``frameGeometry`` は作業領域に**完全に**
       収まる（``QRect.contains``）。
    2. 収まらないとき（ウィンドウの最小サイズが作業領域より大きい、枠が
       極端に厚い）でも、**左上だけは**作業領域内に押し戻す＝タイトルバーは
       必ず掴める。下端 / 右端のはみ出しはここでは救えない。

    枠の厚みは**ネイティブウィンドウが出来てから**しか判らない（``show``
    前は ``frameGeometry() == geometry()`` で余白 0）。起動時の復元は
    ``show`` 前に走るので、そのときの実効は「(2) + サイズのクランプ」まで。
    (1) は表示後に呼ばれた場合（および枠を報告するプラットフォーム）で効く。
    """
    screen = QApplication.primaryScreen()
    if screen is None:  # pragma: no cover (画面ゼロのヘッドレス保険)
        window.resize(*size)
        return
    available = screen.availableGeometry()
    # 枠の厚み。``resize`` はクライアント領域を決めるので、作業領域との
    # 比較の前にこれを引いておかないと枠のぶんだけはみ出す。
    outer = window.frameGeometry()
    margin_w = max(0, outer.width() - window.width())
    margin_h = max(0, outer.height() - window.height())
    window.resize(
        max(1, min(size[0], available.width() - margin_w)),
        max(1, min(size[1], available.height() - margin_h)),
    )
    frame = window.frameGeometry()
    frame.moveCenter(available.center())
    # 中央寄せ後に左上を作業領域内へ押し戻す。上のクランプでも収まらない
    # ケース（``minimumSize`` が作業領域より大きい等）で、**タイトルバーが
    # 掴める**ことだけは守る最後の砦。
    frame.moveLeft(max(frame.left(), available.left()))
    frame.moveTop(max(frame.top(), available.top()))
    window.move(frame.topLeft())
