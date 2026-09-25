"""``ImageView`` のワーカースレッド側の仕事（純関数）。

Qt ウィジェットに触らない — 引数の値（パス / バイト列 / PIL 画像 / 目標寸）
だけを見て結果を返す。投入（``GuardedStream.submit`` / ``submit_batch``）と
着地（スロット）は :mod:`~snappix.viewer.image_view` が持つ。``QPixmap`` 化は
GUI スレッドの仕事なので、ここから返すのは ``QImage`` か PIL のまま。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple

from loguru import logger
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QRect, QSize, Qt
from PySide6.QtGui import QImageReader

from .._runnable import StreamJob, StreamOutcome
from ...common.i18n import t
from ..image_scale import lanczos_downscale_pil
from ..qimage_decode import (
    _QT_READER_LOCK,
    decode_pil,
    decode_pil_bytes,
    decode_qimage_bytes,
    image_size_from_bytes,
    read_file_bytes,
)


def _is_animated_bytes(data: bytes) -> bool:
    """True when *data* decodes to more than one frame (animated image).

    Reads the frame count from an in-memory ``QBuffer`` rather than
    ``QImageReader(str(path))`` so the SMB/CJK-path failure that motivates
    :mod:`.qimage_decode` can't make an animated WebP look static.
    """
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    if not buffer.open(QBuffer.ReadOnly):
        return False
    try:
        # Serialise the QImageReader use under the shared lock like every other
        # worker-thread reader (see qimage_decode): PySide6's QImageReader holds
        # the GIL across the native call and the Qt image-plugin factory lock is
        # not reentrant, so concurrent readers can deadlock the whole process.
        with _QT_READER_LOCK:
            reader = QImageReader(buffer)
            return reader.imageCount() > 1
    finally:
        buffer.close()


#: :func:`_load_image` の結末。``loaded`` の値は **原寸の**
#: ``PIL.Image.Image``（フィット / ズーム / リサイズのたびに原寸から
#: LANCZOS をかけ直せるよう、中間の劣化物は持たない）、``failed`` はメッセージ、
#: ``animated`` はファイルのバイト列。途中経過（プレビュー）は ``done`` では
#: なく ``progress`` の ``QImage``。
_LoadKind = Literal["loaded", "failed", "animated"]

#: プレビュー段を走らせる価値のある縮小率の下限。これを下回るなら
#: プレビューは全体デコードの仕事をなぞるだけで見た目の得が無い。
_PREVIEW_MIN_REDUCTION = 1.5


def _report_preview(
    job: StreamJob, path: Path, data: bytes, preview_box: QSize,
) -> None:
    """段 1 のプレビューを ``progress`` へ流す（出せないときは黙って抜ける）。

    ``QImageReader.setScaledSize`` 相当の縮小デコード。JPEG なら libjpeg の
    scaled DCT（全体の 5〜10 倍速）、他形式でも小さいバッファへ落ちるので
    GUI へ渡すのが安い。4K 級は全体デコードだけで数十〜数百 ms かかるので、
    この段があるかどうかが「即表示」と「送りのたびに固まる」の差になる。
    """
    if preview_box.isEmpty():
        return
    wh = image_size_from_bytes(data, path)
    if wh is None:
        return  # format doesn't expose size cheaply — skip preview
    src_size = QSize(wh[0], wh[1])
    target = src_size.scaled(preview_box, Qt.KeepAspectRatio)
    if target.isEmpty():
        return
    if src_size.width() < target.width() * _PREVIEW_MIN_REDUCTION:
        return  # source isn't much bigger than viewport — full pass is fast enough
    preview = decode_qimage_bytes(data, path, target_size=target)
    if preview is None or preview.isNull():
        return
    job.report(preview)


def _load_image(
    job: StreamJob, path: Path, preview_box: QSize,
    animation_probe: bool = False,
) -> StreamOutcome | None:
    """Two-stage decode of an image off the main thread (純関数).

    Stage 1 — *preview*: :func:`_report_preview` ships a viewport-sized
    ``QImage`` through ``progress`` so the user sees *something* long
    before the full-resolution pass is done.

    Stage 2 — *full*: decodes at native resolution so the LANCZOS pipeline
    has a lossless source to resample from; lands on ``done``.

    ``job.cancel`` は投入の追い越し（``submit``）で降りるので、送りを続けた
    ユーザーの分だけ無駄なデコードを途中で捨てられる。

    With *animation_probe* this also owns the animated-vs-static routing
    for ``.gif`` / ``.webp``: the byte read and the frame-count probe both
    used to run synchronously in ``show_image`` — a cold NAS read of a
    multi-MB file froze the GUI, and a static WebP then paid a *second*
    read here.  Animated files short-circuit via the ``animated`` outcome
    (bytes included, so the GUI-side QMovie needs no re-read); static ones
    fall through to the normal preview/full pipeline reusing the same bytes.
    """
    try:
        if job.cancel.is_cancelled():
            return None
        # One bytes read serves both the preview and the full decode
        # (previously each stage opened the file separately).  The
        # Python-open + Pillow-first pipeline in qimage_decode also
        # fixes SMB/CJK paths and GIL stalls — see that module.
        data = read_file_bytes(path)
        if data is None:
            return StreamOutcome(
                "failed",
                t("viewer.image_view.image_load_failed_name", name=path.name),
            )
        if animation_probe:
            # ``.gif`` always routes to QMovie (matching the old sync
            # path); ``.webp`` only when the byte probe finds multiple
            # frames — a static WebP takes the Lanczos pipeline below.
            if path.suffix.lower() == ".gif" or _is_animated_bytes(data):
                return StreamOutcome("animated", data)
        _report_preview(job, path, data, preview_box)
        # Re-check after preview: the preview decode itself can take
        # 10-50 ms, during which the user may have scrolled on.
        if job.cancel.is_cancelled():
            return None
        pil = decode_pil_bytes(data, path)
        if pil is None:
            return StreamOutcome(
                "failed",
                t("viewer.image_view.image_load_failed_name", name=path.name),
            )
    except Exception as exc:  # pragma: no cover (decode path is robust)
        logger.warning("Image decode failed for {}: {}", path, exc)
        return StreamOutcome(
            "failed",
            t("viewer.image_view.image_load_failed_error", error=exc),
        )
    return StreamOutcome("loaded", pil)


#: :func:`_lanczos_full` / :func:`_patch_resample` の結末。**同じ 1 本の
#: ストリーム**に載せるので、モード切替を跨いだ古い結果は必ず追い越される
#: （全体レンダとパッチレンダは互いに無効化し合う）。
#: ``full`` の値は ``(dpr, PIL.Image.Image)``、``patch`` は
#: ``(QRect, PIL.Image.Image)``。``QPixmap`` 化は GUI スレッドの仕事なので
#: どちらも PIL のまま返す。
_ScaleKind = Literal["full", "patch"]


def _lanczos_full(
    pil: Image.Image, box: QSize, dpr: float,
) -> StreamOutcome | None:
    """Resample the source image to a target box on a worker thread (純関数).

    LANCZOS on a 4K image costs ~40-80 ms, enough to visibly stutter the
    GUI when zooming.  *dpr* is the device-pixel-ratio captured at schedule
    time; carrying it through avoids a re-read in the slot which would be
    wrong if the window moved monitors while the work ran.
    """
    try:
        resized = lanczos_downscale_pil(pil, box)
    except Exception as exc:  # pragma: no cover
        logger.warning("LANCZOS resample failed: {}", exc)
        return None
    return StreamOutcome("full", (dpr, resized))


def _patch_resample(
    pil: Image.Image, crop_box: tuple[int, int, int, int],
    out_size: QSize, target_rect: QRect,
) -> StreamOutcome | None:
    """原寸ソースから可視領域を切り出して拡大する（純関数）.

    ``crop`` は原寸のごく一部（ビューポート/ズーム 相当）なので、全体レンダ
    の :func:`_lanczos_full` よりはるかに軽い。

    リサンプルフィルタは :func:`lanczos_downscale_pil` と同じ規約
    （縮小 = LANCZOS / 拡大 = BICUBIC）。パッチモードは通常 1x 超の拡大なので
    実質 BICUBIC だが、高 DPR × 低ズームの境界（論理拡大・物理縮小）も正しく
    扱うため出力/入力比で選ぶ。*target_rect* はラベル論理座標のパッチ矩形を
    そのまま返し、スロット側の再計算ズレを防ぐ。
    """
    try:
        region = pil.crop(crop_box)
        out_w = max(1, out_size.width())
        out_h = max(1, out_size.height())
        upscale = out_w * out_h >= region.width * region.height
        resample = (
            Image.Resampling.BICUBIC if upscale else Image.Resampling.LANCZOS
        )
        resized = region.resize((out_w, out_h), resample)
    except Exception as exc:  # pragma: no cover
        logger.warning("patch resample failed: {}", exc)
        return None
    return StreamOutcome("patch", (QRect(target_rect), resized))


def _pil_sizeof(img: Image.Image) -> int:
    """Approximate in-memory footprint of a decoded PIL image, in bytes.

    PIL stores ``mode`` pixels as 1 byte per channel for 8-bit modes (RGB,
    RGBA, L).  This ignores metadata overhead but is close enough for the
    cache's byte budget (the image data dominates).
    """
    w, h = img.size
    return max(0, w * h * max(1, len(img.getbands())))


class PrefetchMiss(NamedTuple):
    """先読みが画像を持ち帰らなかった着地（パスを載せて返す）。

    素の ``None`` を返すと着地側はどのパスの話か分からず、デコード窓
    （右ペインのスピナー述語）から外せない。壊れた / 消えた近傍が窓に
    残ったままスピナーと 80ms 再描画が止まらなくなるので、パスを必ず載せる。

    ``declined`` は「要らなくなって降りた」（キャンセル / 台帳の ``wanted`` が
    偽）で、``False`` は「読めなかった」（デコード失敗）。前者はデコードが
    まだ終わっていないので窓から外さない。
    """

    path: Path
    declined: bool


def _prefetch_decode(
    job: StreamJob, path: Path, wanted: Callable[[Path], bool],
) -> tuple[Path, Image.Image] | PrefetchMiss:
    """Decode a neighbor image off the GUI thread for the ImageView cache.

    結末は 1 つ（``(path, PIL.Image.Image)``）なので ``StreamOutcome`` には
    畳まない。Best-effort only: errors are logged at debug level (the user
    hasn't asked for this file yet, so a silent skip is correct — if they
    navigate to it we'll retry via :meth:`ImageView.show_image`).

    降りる条件は 2 つある。``job.cancel`` はフォルダ切替 / クリアで降り、
    *wanted* は「この先読みがまだ要るか」を GUI 側の台帳へ聞く（``str`` の
    集合メンバシップ = GIL アトミックな読み）。後者があるので、表示要求や
    近傍集合の入れ替え（:meth:`ImageView._schedule_prefetch` はストリームを
    畳まない）で追い越されたままキューに残っていた先読みは走り出しても即座に
    降り、現在のデコードと CPU を奪い合わない。
    """
    if job.cancel.is_cancelled() or not wanted(path):
        return PrefetchMiss(path, declined=True)
    try:
        pil = decode_pil(path)
    except Exception as exc:  # noqa: BLE001 — 近傍の失敗は黙って飛ばす
        logger.debug("Prefetch decode failed for {}: {}", path, exc)
        return PrefetchMiss(path, declined=False)
    if pil is None:
        return PrefetchMiss(path, declined=False)
    if job.cancel.is_cancelled() or not wanted(path):
        return PrefetchMiss(path, declined=True)
    return path, pil


__all__ = [
    "_LoadKind",
    "_ScaleKind",
    "_is_animated_bytes",
    "_lanczos_full",
    "_load_image",
    "_patch_resample",
    "_pil_sizeof",
    "_prefetch_decode",
    "PrefetchMiss",
    "_report_preview",
]
