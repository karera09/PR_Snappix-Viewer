"""Read embedded generation / capture metadata out of an image file (M05).

Two independent sources, both read through Pillow (already a dependency — no new
third-party requirement):

* **PNG text chunks** (``tEXt`` / ``iTXt``) — where Stable Diffusion front-ends
  stash their generation parameters: Automatic1111's ``parameters``, ComfyUI's
  ``prompt`` / ``workflow``, NovelAI's ``Comment``, plus any plain ``Software`` /
  ``Description`` / ``Comment`` string.  These can be very long (a full SD prompt
  + settings), so the detail window shows each in a wrapped, copyable box.
* **JPEG/TIFF EXIF** — the handful of fields a viewer actually wants: capture
  time, camera make/model, lens, ISO, aperture, shutter speed, focal length.

Everything here is **pure logic** (no Qt): the detail window calls
:func:`extract_image_metadata` on a worker thread and renders the returned
:class:`ImageMetadata`.  A file with neither PNG text nor EXIF yields an empty
result (``is_empty``) and the window then hides the whole section.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# EXIF fields we surface, in display order.  Each entry is
# ``(i18n-key, tag-id, formatter)``.  The tag ids are the standard EXIF tag
# numbers (stable — they are the on-disk data, never renamed).  ``formatter``
# turns the raw Pillow value (which may be an ``IFDRational``) into a display
# string, or returns ``""`` to omit the row.
_EXIF_DATETIME = 0x9003        # DateTimeOriginal (Exif sub-IFD)
_EXIF_DATETIME_FALLBACK = 0x0132  # DateTime (IFD0)
_EXIF_MAKE = 0x010F            # Make (IFD0)
_EXIF_MODEL = 0x0110           # Model (IFD0)
_EXIF_LENS = 0xA434            # LensModel (Exif sub-IFD)
_EXIF_ISO = 0x8827             # ISOSpeedRatings (Exif sub-IFD)
_EXIF_ISO_ALT = 0x8832         # PhotographicSensitivity (newer tag)
_EXIF_FNUMBER = 0x829D         # FNumber (Exif sub-IFD)
_EXIF_EXPOSURE = 0x829A        # ExposureTime (Exif sub-IFD)
_EXIF_FOCAL = 0x920A           # FocalLength (Exif sub-IFD)
_EXIF_SUB_IFD = 0x8769         # pointer to the Exif sub-IFD

# PNG ``info`` keys that are NOT human-readable text chunks — filtered out when
# falling back to ``img.info`` (``img.text`` already excludes them, but the
# fallback path scans ``info`` directly).
_PNG_NON_TEXT_KEYS = frozenset({
    "dpi", "gamma", "transparency", "icc_profile", "exif", "srgb", "aspect",
    "chromaticity", "background", "interlace", "compression", "loop",
    "duration", "date:create", "date:modify",
})


#: 1 チャンクの**表示用**の長さ上限（文字）。読み取り自体はワーカーだが、
#: 受け手は値をそのまま ``QPlainTextEdit`` に入れて GUI スレッドで組版する。
#: Pillow のガードは合計 64MB（``MAX_TEXT_MEMORY``）だけなので 1 チャンク
#: 数百 KB は素通りし、折り返し位置の無い長大な 1 行（base64 の XMP サムネ
#: イル・埋め込みプレビュー）ではワードラップの計算が破綻して GUI が秒〜分
#: 単位で止まる。全文は :attr:`ImageMetadata.full_text_chunks` に残すので、
#: コピーは切り詰めない。
TEXT_CHUNK_DISPLAY_LIMIT = 32 * 1024


@dataclass
class ImageMetadata:
    """Extracted metadata: PNG text chunks + selected EXIF fields.

    ``text_chunks`` is ``(name, value)`` for each PNG ``tEXt`` / ``iTXt`` entry
    (values may be long — the SD prompt use-case).  ``exif`` is
    ``(label, value)`` for the short capture fields.  Either may be empty; when
    both are, :meth:`is_empty` is ``True`` and the caller hides the section.

    ``text_chunks`` holds the **display** value (truncated at
    :data:`TEXT_CHUNK_DISPLAY_LIMIT` with a note); ``full_text_chunks`` holds
    the untruncated one for [コピー].  ``truncated_chunks`` names the chunks
    that were cut.
    """

    text_chunks: list[tuple[str, str]] = field(default_factory=list)
    exif: list[tuple[str, str]] = field(default_factory=list)
    full_text_chunks: dict[str, str] = field(default_factory=dict)
    truncated_chunks: frozenset[str] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not self.text_chunks and not self.exif

    def full_text(self, name: str) -> str:
        """*name* の全文（切り詰めていない値）。無ければ表示値を返す。"""
        if name in self.full_text_chunks:
            return self.full_text_chunks[name]
        for key, value in self.text_chunks:
            if key == name:
                return value
        return ""


def _to_float(value) -> float | None:
    """Coerce an EXIF value (``IFDRational`` / int / float) to ``float``."""
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _fmt_datetime(value) -> str:
    return str(value).strip() if value else ""


def _fmt_text(value) -> str:
    return str(value).strip() if value else ""


# 値側の単位もカタログから取る（ラベルは i18n キーなのに単位だけコードに
# 直書きで、しかも「秒」は日本語だった — 同じテーブルの中で i18n の境界が
# 割れている状態）。数値の整形はここ、語はカタログ、の分担にする。
def _unit(key: str, **kwargs) -> str:
    from ..common.i18n import t

    return t(key, **kwargs)


def _fmt_iso(value) -> str:
    # ISO can be a scalar or a tuple/list (ISOSpeedRatings historically a list).
    if isinstance(value, (tuple, list)):
        value = value[0] if value else None
    n = _to_float(value)
    if n is None or n <= 0:
        return ""
    return _unit("viewer.detail_window.exif_unit_iso", n=int(round(n)))


def _fmt_fnumber(value) -> str:
    n = _to_float(value)
    if n is None or n <= 0:
        return ""
    # Trim a trailing ".0" so "F8.0" reads as "F8".
    return _unit(
        "viewer.detail_window.exif_unit_fnumber",
        n=f"{n:.1f}".rstrip("0").rstrip("."),
    )


def _fmt_exposure(value) -> str:
    n = _to_float(value)
    if n is None or n <= 0:
        return ""
    if n >= 1:
        return _unit("viewer.detail_window.exif_unit_seconds", n=f"{n:g}")
    return _unit(
        "viewer.detail_window.exif_unit_seconds",
        n=f"1/{int(round(1.0 / n))}",
    )


def _fmt_focal(value) -> str:
    n = _to_float(value)
    if n is None or n <= 0:
        return ""
    return _unit("viewer.detail_window.exif_unit_mm", n=f"{n:g}")


# ``(i18n-key, [tag-ids in priority order], formatter)`` — the first tag id that
# yields a non-empty formatted value wins.
_EXIF_FIELDS: tuple[tuple[str, tuple[int, ...], object], ...] = (
    ("viewer.detail_window.exif_datetime", (_EXIF_DATETIME, _EXIF_DATETIME_FALLBACK), _fmt_datetime),
    ("viewer.detail_window.exif_make", (_EXIF_MAKE,), _fmt_text),
    ("viewer.detail_window.exif_model", (_EXIF_MODEL,), _fmt_text),
    ("viewer.detail_window.exif_lens", (_EXIF_LENS,), _fmt_text),
    ("viewer.detail_window.exif_iso", (_EXIF_ISO, _EXIF_ISO_ALT), _fmt_iso),
    ("viewer.detail_window.exif_fnumber", (_EXIF_FNUMBER,), _fmt_fnumber),
    ("viewer.detail_window.exif_exposure", (_EXIF_EXPOSURE,), _fmt_exposure),
    ("viewer.detail_window.exif_focal", (_EXIF_FOCAL,), _fmt_focal),
)


def _read_png_text(img) -> tuple[list[tuple[str, str]], dict[str, str], frozenset[str]]:
    """PNG ``tEXt`` / ``iTXt`` chunks as ``(name, value)`` (SD parameters etc.).

    Deliberately reads ``img.info``, NOT the ``PngImageFile.text`` property:
    ``.text`` unconditionally calls ``load()`` (full pixel decode + full file
    read) to pick up chunks placed *after* IDAT, which breaks
    :func:`extract_image_metadata`'s header-only contract — on a cold NAS a
    detail-window selection would re-read and decode the whole PNG (#11).
    ``info`` holds every pre-IDAT ``tEXt`` / ``zTXt`` / ``iTXt`` chunk, which
    is where SD front-ends (A1111 / ComfyUI / NovelAI) write their metadata;
    the rare post-IDAT text chunk is knowingly not surfaced.

    Returns ``(display chunks, full values by name, truncated names)`` — the
    display value is capped at :data:`TEXT_CHUNK_DISPLAY_LIMIT` because the
    receiver lays it out on the GUI thread.
    """
    out: list[tuple[str, str]] = []
    full: dict[str, str] = {}
    truncated: set[str] = set()
    source = getattr(img, "info", {}) or {}
    for key, value in source.items():
        if not isinstance(value, str):
            continue
        if key.lower() in _PNG_NON_TEXT_KEYS:
            continue
        stripped = value.strip()
        if not stripped:
            continue
        name = str(key)
        if len(stripped) > TEXT_CHUNK_DISPLAY_LIMIT:
            full[name] = stripped
            truncated.add(name)
            stripped = stripped[:TEXT_CHUNK_DISPLAY_LIMIT]
        out.append((name, stripped))
    return out, full, frozenset(truncated)


def _read_exif(img) -> list[tuple[str, str]]:
    """Selected EXIF capture fields as ``(i18n-key, value)`` — empty when none."""
    try:
        exif = img.getexif()
    except Exception:  # pragma: no cover (defensive — odd/corrupt EXIF)
        return []
    if not exif:
        return []
    # Merge IFD0 with the Exif sub-IFD so the sub-IFD fields (aperture, shutter,
    # ISO, lens, DateTimeOriginal) are reachable by tag id.
    merged: dict[int, object] = dict(exif)
    try:
        sub = exif.get_ifd(_EXIF_SUB_IFD)
        if sub:
            merged.update(sub)
    except Exception:  # pragma: no cover (defensive)
        pass

    out: list[tuple[str, str]] = []
    for label_key, tag_ids, formatter in _EXIF_FIELDS:
        for tag_id in tag_ids:
            if tag_id not in merged:
                continue
            text = formatter(merged[tag_id])
            if text:
                out.append((label_key, text))
                break
    return out


def extract_image_metadata(path: Path) -> ImageMetadata:
    """Read PNG text chunks + EXIF fields from *path* (best effort, off-thread).

    Never raises: any decode error (unsupported / corrupt / not an image)
    degrades to an empty :class:`ImageMetadata`.  Opens the file header-only
    (no full-image decode / ``load()``), so it is cheap enough for the detail
    window's per-selection worker even on a cold NAS.

    Pillow's decompression-bomb guard fires from ``Image.open`` on the
    *declared* size alone, so a 20000×20000 scan used to lose all of its
    metadata even though not a single pixel is decoded here.  Such a file is
    retried through :func:`_open_without_bomb_check`, which is safe because of
    the header-only contract — and which leaves ``Image.MAX_IMAGE_PIXELS``
    alone.  Suspending that module attribute would lift the ceiling for
    **every** thread: the thumbnail decoders read the same attribute and rely
    on it to refuse a ~716 MB allocation (``qimage_decode.open_pil_oriented``),
    so a concurrent decode landing inside the suspension window would go on to
    really decode the bomb.
    """
    from PIL import Image  # local import: keeps module import Qt/PIL-lazy

    try:
        return _read_metadata(path)
    except FileNotFoundError:
        return ImageMetadata()
    except Image.DecompressionBombError:
        try:
            return _read_metadata(path, bomb_check=False)
        except Exception as exc:  # noqa: BLE001 — any Pillow error → no metadata
            logger.debug("metadata read failed for {}: {}", path, exc)
            return ImageMetadata()
    except Exception as exc:  # noqa: BLE001 — any Pillow error → no metadata
        logger.debug("metadata read failed for {}: {}", path, exc)
        return ImageMetadata()


def _open_without_bomb_check(path: Path):
    """Open *path* through its format plugin, skipping the bomb size check.

    ``Image.open`` runs ``_decompression_bomb_check`` on the declared size
    right after the plugin has parsed the header, and the only knob it reads
    is the **module-global** ``MAX_IMAGE_PIXELS``.  Rewriting that global for
    the duration of a retry is a process-wide hole (see
    :func:`extract_image_metadata`), so this reproduces ``Image.open``'s own
    plugin dispatch — prefix sniff, then ``factory(path)`` — and simply never
    performs the check.  It stays header-only: no ``load()``, no pixels.

    Raises the same :class:`PIL.UnidentifiedImageError` as ``Image.open`` when
    no plugin accepts the file, and propagates whatever the plugin raises.
    """
    from PIL import Image, UnidentifiedImageError

    Image.init()  # register every plugin's OPEN entry (no-op once done)
    with path.open("rb") as fp:
        prefix = fp.read(16)
    for name in list(Image.ID):
        entry = Image.OPEN.get(name)
        if entry is None:  # pragma: no cover (defensive)
            continue
        factory, accept = entry
        try:
            # A ``str`` verdict is a plugin's "not this one, and here is why"
            # (``Image.open`` collects those as warnings), so it is a skip.
            verdict = True if accept is None else accept(prefix)
        except Exception:  # noqa: BLE001 (a sniffer must not decide the answer)
            continue
        if not verdict or isinstance(verdict, str):
            continue
        try:
            # Handing the *path* (not an open fp) lets Pillow own the handle,
            # so ``_exclusive_fp`` is set and ``with`` closes it — including
            # on a header parse that raises inside the constructor.
            return factory(path, None)
        except (SyntaxError, IndexError, TypeError, struct.error):
            continue
    raise UnidentifiedImageError(f"cannot identify image file {str(path)!r}")


def _read_metadata(path: Path, *, bomb_check: bool = True) -> ImageMetadata:
    """:func:`extract_image_metadata` の本体（例外はそのまま呼び出し元へ）。"""
    from PIL import Image

    opener = Image.open if bomb_check else _open_without_bomb_check
    with opener(path) as img:
        fmt = (img.format or "").upper()
        text_chunks: list[tuple[str, str]] = []
        full_chunks: dict[str, str] = {}
        truncated: frozenset[str] = frozenset()
        if fmt == "PNG":
            text_chunks, full_chunks, truncated = _read_png_text(img)
            # ``PngImageFile.getexif`` falls back to ``load()`` (full
            # decode) when no ``eXIf`` chunk was seen before IDAT —
            # skip EXIF for such PNGs to keep this header-only (#11).
            exif = _read_exif(img) if "exif" in img.info else []
        else:
            exif = _read_exif(img)
        return ImageMetadata(
            text_chunks=text_chunks,
            exif=exif,
            full_text_chunks=full_chunks,
            truncated_chunks=truncated,
        )
