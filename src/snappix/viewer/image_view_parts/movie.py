"""``ImageView`` のアニメーション（``QMovie``）の組み立てと再生状態。

* :func:`open_movie` — バイト列から ``QMovie`` を組む（``QBuffer`` 経由・
  キャッシュモード・フレーム 0 の読み出し）
* :func:`render_frame` — 原寸でデコードされた現在フレームを、表示用の向き
  補正と（必要なら）表示寸への縮小を掛けた 1 枚にする（ビューはそれを
  canvas ラベルへ渡し、拡縮は描画時に行う）
* :class:`MoviePlayback` — 再生 / 一時停止を「誰が決めたか」の印ごと持つ
  （利用者のトグル・ページ離脱・別ウィンドウへの退避・ダブルクリックの
  打ち消し）

依存の向きは **部品 → ビューは無し**（``image_view`` を import しない）。
"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QObject, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QMovie, QPixmap, QTransform

#: ``CacheAll`` で全フレームを保持してよいデコード済みバイト数の上限。
#: 超えるアニメーションは ``CacheNone``（フレームを読むたびにデコードする。
#: 元データは ``QBuffer`` 上なので I/O は無い）。
MOVIE_CACHE_MAX_BYTES = 256 * 1024 * 1024


def movie_cache_mode(frame_count: int, natural: QSize) -> QMovie.CacheMode:
    """原寸 *natural* のフレームが *frame_count* 枚あるアニメーションのキャッシュモード。

    フレームは原寸でキャッシュされるので、必要量はここで確定する。フレーム
    数か原寸が読めないときは見積もれないので ``CacheNone``。
    """
    if frame_count <= 0 or natural.width() <= 0 or natural.height() <= 0:
        return QMovie.CacheMode.CacheNone
    cost = frame_count * natural.width() * natural.height() * 4
    if cost <= MOVIE_CACHE_MAX_BYTES:
        return QMovie.CacheMode.CacheAll
    return QMovie.CacheMode.CacheNone


def _probe_frames(qbytes: QByteArray) -> tuple[int, QSize]:
    """*qbytes* のフレーム数と原寸（``QMovie`` のフレームを読まずに）。"""
    probe = QBuffer()
    probe.setData(qbytes)
    if not probe.open(QBuffer.OpenModeFlag.ReadOnly):
        return 0, QSize()
    reader = QImageReader(probe)
    result = reader.imageCount(), reader.size()
    probe.close()
    return result


def oriented_size(size: QSize, rotation: int) -> QSize:
    """表示用の回転（90° 単位）を掛けた後の寸法。"""
    return size.transposed() if rotation % 180 else QSize(size)


def orient_frame(frame: QImage, rotation: int, flip_h: bool) -> QImage:
    """フレームへ表示用の回転（時計回り）→ 左右反転を掛ける（静止画と同じ順）。"""
    rot = rotation % 360
    if rot:
        frame = frame.transformed(QTransform().rotate(rot))
    if flip_h:
        frame = frame.flipped(Qt.Orientation.Horizontal)
    return frame


def render_frame(
    movie: QMovie, *, target: QSize, rotation: int, flip_h: bool,
) -> QPixmap | None:
    """現在フレームを向き補正し、*target*（物理画素）より大きければ縮めた 1 枚。

    拡大はしない — canvas の描画が露出領域だけを伸縮するので、原寸を渡せば
    ズーム後の全面を確保せずに済む。縮小だけは描画時の双線形だと大きな
    縮小率でエイリアスするので、ここで Smooth に 1 回縮める。
    """
    if rotation % 360 or flip_h:
        image = orient_frame(movie.currentImage(), rotation, flip_h)
        if image.isNull():
            return None
        pix = QPixmap.fromImage(image)
    else:
        pix = movie.currentPixmap()
        if pix.isNull():
            return None
    if (
        target.width() > 0 and target.height() > 0
        and target.width() * target.height() < pix.width() * pix.height()
    ):
        pix = pix.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pix


def open_movie(
    parent: QObject, data: bytes,
) -> tuple[QMovie, QBuffer, QByteArray] | None:
    """*data* を再生する ``QMovie`` と、それが読む ``QBuffer`` / バイト列。

    ``QMovie(str(path))`` は QFile を使い、Qt6 は CJK + 全角記号を含む一部の
    SMB/NAS パスを開けない（静止画の ``qimage_decode`` と同じ不具合）ので
    メモリ上の ``QBuffer`` から読む。バッファとバイト列は ``QMovie`` より
    長生きする必要があるので、呼び出し側が保持すること。

    ムービーは *parent* へ、バッファはムービーへ親付けする — 窓が明示の
    後始末なしに壊されても Qt が破棄順を守る（生きた ``QMovie`` の下で
    ``QBuffer`` が先に消えるとプロセス終了時にアクセス違反になる）。
    読めなければ ``None``（呼び出し側は静止画の経路へ落とす）。
    """
    qbytes = QByteArray(data)
    movie = QMovie(parent)
    buffer = QBuffer(movie)
    buffer.setData(qbytes)
    if not buffer.open(QBuffer.OpenModeFlag.ReadOnly):
        buffer.deleteLater()
        movie.deleteLater()
        return None
    movie.setDevice(buffer)
    if not movie.isValid():
        movie.deleteLater()
        return None
    # フレームは常に原寸でデコードさせ（``setScaledSize`` は使わない —
    # 拡縮は ``ImageView`` が描画時に行う）、キャッシュの寸法が表示寸に
    # 依存しないようにする。だから CacheAll のメモリは「フレーム数 × 原寸
    # 面積 × 4 バイト」の確定値で、フレームを読む前に見積もれる。キャッシュ
    # モードは最初のフレームを読む前に決める必要があるので、同じバイト列を
    # 別の ``QBuffer`` で覗いて原寸とフレーム数を取る。
    movie.setCacheMode(movie_cache_mode(*_probe_frames(qbytes)))
    # ``frameRect`` is only populated once a frame is actually loaded;
    # jumping to frame 0 makes the natural size queryable immediately
    # so the initial fit-to-window scale matches the first paint.
    if not movie.jumpToFrame(0):
        movie.deleteLater()
        return None
    return movie, buffer, qbytes


class MoviePlayback:
    """再生 / 一時停止の状態と、それを「誰が決めたか」の印。

    :attr:`suspended` — 別ウィンドウ（全画面）へ退避するときに**こちらが**
    止めた再生か。全画面を同じ画像のまま閉じた復路は選択が変わらず
    ``show_image`` を通らない（同じパスの再選択は emit されない）ので、
    止めた側が :meth:`resume` で戻す。利用者が自分で止めていた GIF や、
    ページ離脱で止まった（隠れた）GIF は戻りで動かさない。

    :attr:`release_toggled` — 直前のクリックの Release が再生を切り替えたか。
    Qt のダブルクリックは Press → Release → DblClick → Release で届き（2 回目
    の Press は DblClick に置き換わる）、トグルは 1 回目の Release で 1 回
    だけ走る。最大化のダブルクリックは :meth:`undo_release_toggle` でそれを
    打ち消して再生状態を保つ。次の押下（:meth:`arm`）で落とす。
    """

    def __init__(self) -> None:
        self.suspended = False
        self.release_toggled = False

    def forget(self) -> None:
        """ムービーを畳んだ / 作り直した — 印は前のムービーのもの。"""
        self.suspended = False
        self.release_toggled = False

    def pause(self, movie: QMovie | None) -> None:
        """ページ離脱で止める（戻りは ``show_image`` が作り直すので印は無し）。"""
        self.suspended = False
        if movie is not None and movie.state() == QMovie.MovieState.Running:
            movie.setPaused(True)

    def suspend(self, movie: QMovie | None) -> None:
        """別ウィンドウへ退避する間だけ止める（再生中だったときだけ印を立てる）。"""
        if movie is not None and movie.state() == QMovie.MovieState.Running:
            movie.setPaused(True)
            self.suspended = True

    def resume(self, movie: QMovie | None) -> None:
        """:meth:`suspend` が止めた再生を再開する（それ以外は no-op）。"""
        if not self.suspended:
            return
        self.suspended = False
        if movie is not None and movie.state() == QMovie.MovieState.Paused:
            movie.setPaused(False)

    def toggle(self, movie: QMovie | None) -> None:
        """利用者の再生 / 一時停止（退避の復路で上書きしないよう印を落とす）。"""
        self.suspended = False
        if movie is None:
            return
        state = movie.state()
        if state == QMovie.MovieState.Running:
            movie.setPaused(True)
        elif state == QMovie.MovieState.Paused:
            movie.setPaused(False)
        else:
            movie.start()

    def arm(self) -> None:
        """新しいクリックの押下 — 前のクリックの Release の印を落とす。"""
        self.release_toggled = False

    def toggle_on_release(self, movie: QMovie | None) -> None:
        """ドラッグに発展しなかった Release のトグル（印を立てる）。"""
        self.toggle(movie)
        self.release_toggled = True

    def undo_release_toggle(self, movie: QMovie | None) -> None:
        """ダブルクリックの 1 回目の Release が切り替えた再生状態を戻す。"""
        if self.release_toggled:
            self.release_toggled = False
            self.toggle(movie)


__all__ = [
    "MOVIE_CACHE_MAX_BYTES",
    "MoviePlayback",
    "movie_cache_mode",
    "open_movie",
    "orient_frame",
    "oriented_size",
    "render_frame",
]
