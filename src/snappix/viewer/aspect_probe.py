"""Off-thread image aspect-ratio probing for the justified gallery layout.

Reads image *headers* (cheap — no pixel decode) to learn each image's
pixel ``(width, height)``, which the justified layout needs before it can
pack rows.  Mirrors :class:`scan_worker.ChildrenScanner`'s pattern: a
``QThreadPool``-dispatched task carrying a
:class:`~.cancel_token.ScanSession` (generation + cooperative cancel, #35)
so navigating away abandons in-flight probes instead of hammering a NAS
share.

The probe is **viewport-bounded** (the view only requests aspects for the
buffered visible range, like thumbnails) and runs at low concurrency
(pool ≤ 2) so it doesn't blow the SMB credit budget shared with the 4
thumbnail + 6 metadata workers.

Workers never touch sqlite — they hand back plain integers and the main
thread writes them to :class:`thumb_meta_cache.ThumbMetaCache`.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Union

# A probe spec is either ``(key_path, mtime, size)`` — read the header from
# ``key_path`` and key the result by it — or ``(key_path, mtime, size,
# image_path)`` when a folder's representative thumbnail is a *child* file: the
# header is read from ``image_path`` but the emitted row is keyed by
# ``key_path``.
ProbeSpec = Union[
    tuple[Path, float, int],
    tuple[Path, float, int, Path],
]

from loguru import logger
from PySide6.QtCore import QObject, QThreadPool, Signal

from .cancel_token import ScanSession, SessionOwner, SessionRunnable
from .gil_pacing import GilPacer
from .perf import measure
from .qimage_decode import pil_header_size

# Low default — header reads are tiny but still cost SMB round-trips that
# compete with thumbnail (4) + metadata (6) workers.  Tunable at runtime.
_PROBE_PARALLELISM = 2
# Emit results in chunks so the view can relayout progressively rather than
# waiting for the whole batch.
_PROBE_BATCH_SIZE = 16

# Header parsing is a pure-Python burst; probe workers running back-to-back
# starve the GUI thread of the GIL (see gil_pacing.py).  Shared across the
# executor's threads — counters are per-thread.
_PROBE_PACER = GilPacer()


def _read_image_aspect_paced(path: Path) -> tuple[int, int] | None:
    wh = read_image_aspect(path)
    _PROBE_PACER.tick()
    return wh


def read_image_aspect(path: Path) -> tuple[int, int] | None:
    """Return the displayed ``(width, height)`` of *path*, or ``None``.

    Header-only and **Pillow-only** — delegates to the shared
    :func:`~snappix.viewer.qimage_decode.pil_header_size` core (one home for
    the EXIF-orientation swap), deliberately without that module's
    ``QImageReader`` fallback: probe workers must never enter Qt's decoder,
    whose PySide6 binding holds the GIL for the whole call.  Returns
    ``None`` for unreadable / unidentified files so the caller falls back
    to a placeholder aspect.
    """
    return pil_header_size(path)


class _ProbeSignals(QObject):
    # (generation, list[(path_str, mtime, size, width, height)])
    probed = Signal(int, list)


class _AspectProbeTask(SessionRunnable):
    """Probe a batch of image specs on a worker thread.

    ``specs`` is ``[(Path, mtime, size), ...]``.  Reads headers via an
    internal ``ThreadPoolExecutor`` (≤ ``parallelism`` concurrent) and
    emits ``probed`` in chunks.  Polls the cancel token between results.

    **Cancellation never discards a header that was already read** (#47).
    A cancel stops *issuing* new reads (queued futures are cancelled, the
    dispatch loop breaks), but results the executor already produced — the
    completed-but-unconsumed futures plus the accumulated fractional batch —
    are still emitted.  This is sound because every row is self-validating
    and every receiver is idempotent:

    * a row is ``(path_str, mtime, size, w, h)`` — keyed by the file, not by
      which request asked; its correctness is independent of generation;
    * ``ThumbMetaCache.put_many`` upserts by ``(path, mtime, size)`` and
      skips ``w<=0`` failure rows, so replaying a row is a no-op;
    * ``GalleryView.set_aspect`` ignores keys absent from the current tiles.

    Dropping them (the old behaviour) threw away NAS round-trips that had
    already been paid: with a simulated 20 ms SMB latency and the grid's
    40 ms re-request cadence, ~75% of issued header reads never reached the
    GUI and were re-read on the next scroll pass.  Salvaged emissions are
    stamped with the scanner's *latest* generation (``latest_generation``)
    so the consumer's stale-chunk guard doesn't undo the salvage.
    """

    def __init__(
        self,
        session: ScanSession,
        specs: list[ProbeSpec],
        signals: _ProbeSignals,
        *,
        parallelism: int = _PROBE_PARALLELISM,
        latest_generation=None,
    ) -> None:
        super().__init__(session)
        self.generation = session.generation
        self.specs = specs
        self.signals = signals
        # 歴史的な別名: 本体は ``session`` そのもの（旧 CancelToken 互換）。
        self.cancel = session
        self.parallelism = max(1, int(parallelism))
        # Optional zero-arg callable answering the scanner's newest
        # generation; salvaged (post-cancel) emissions carry it — see the
        # class docstring.  ``None`` falls back to this task's generation.
        self.latest_generation = latest_generation

    def _emit_generation(self) -> int:
        """Generation to stamp on an emission (latest one once cancelled)."""
        if self.cancel.is_cancelled() and self.latest_generation is not None:
            return self.latest_generation()
        return self.generation

    def _rows_for(
        self,
        fut: "concurrent.futures.Future",
        key: tuple[Path, float, int],
    ) -> list[tuple[str, float, int, int, int]]:
        """Turn one finished future into emit rows (empty when cancelled)."""
        key_path, mtime, size = key
        try:
            wh = fut.result()
        except concurrent.futures.CancelledError:
            return []
        except Exception as exc:  # pragma: no cover (defensive)
            logger.debug("probe future failed {}: {}", key_path, exc)
            wh = None
        # Emit a (0, 0) row for unreadable / unidentified files too (parity
        # with cache_builder._probe_aspects) so the consumer can remember
        # "probed, failed" and stop re-issuing the header read on every
        # scroll pass (NAS round-trip per visit otherwise).  The meta cache
        # skips w<=0 rows, so no bad aspect is stored.
        if wh is None:
            return [(str(key_path), mtime, size, 0, 0)]
        return [(str(key_path), mtime, size, wh[0], wh[1])]

    def _run(self) -> None:
        # (``SessionRunnable.run`` already returned for a cancelled session.)
        if not self.specs:
            return
        batch: list[tuple[str, float, int, int, int]] = []
        try:
            with measure("aspect_probe", f"{len(self.specs)} specs"):
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.parallelism, len(self.specs)),
                    thread_name_prefix="viewer-aspect",
                ) as ex:
                    # A spec is ``(key_path, mtime, size)`` or, when the image
                    # to read differs from the cache key (a folder whose
                    # representative thumbnail is a child file),
                    # ``(key_path, mtime, size, image_path)``.  The emitted
                    # row is keyed by ``key_path`` but the header is read from
                    # ``image_path``.
                    futures: dict = {}
                    for spec in self.specs:
                        key_path, m, s = spec[0], spec[1], spec[2]
                        image_path = spec[3] if len(spec) > 3 else spec[0]
                        futures[ex.submit(_read_image_aspect_paced, image_path)] = (
                            key_path, m, s
                        )
                    consumed: set = set()
                    for fut in concurrent.futures.as_completed(futures):
                        if self.cancel.is_cancelled():
                            # Stop issuing: queued reads are cancelled.  The
                            # ``with`` exit below still waits for the ≤
                            # ``parallelism`` in-flight reads to finish —
                            # their results are harvested after it.
                            for f in futures:
                                f.cancel()
                            break
                        consumed.add(fut)
                        batch.extend(self._rows_for(fut, futures[fut]))
                        if len(batch) >= _PROBE_BATCH_SIZE:
                            self.signals.probed.emit(self._emit_generation(), batch)
                            batch = []
                # ``ex.__exit__`` (wait=True) has drained the workers: every
                # future is now finished or cancelled.  Harvest the completed
                # ones the loop never consumed — their headers were already
                # read off the share, and the rows are valid regardless of
                # cancellation (see the class docstring, #47).
                for fut, key in futures.items():
                    if fut in consumed or fut.cancelled():
                        continue
                    batch.extend(self._rows_for(fut, key))
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning("aspect probe batch failed: {}", exc)
        # The fractional tail batch is emitted even when cancelled — dropping
        # it would waste up to ``_PROBE_BATCH_SIZE - 1`` completed reads.
        if batch:
            self.signals.probed.emit(self._emit_generation(), batch)


class AspectProbeScanner(QObject):
    """Off-thread aspect-ratio prober (mirrors :class:`ChildrenScanner`).

    Emits :sig:`probed(int generation, list[(path_str, mtime, size, w, h)])`
    in chunks.  Consumers compare the generation against
    :meth:`latest_generation` and drop stale payloads.
    """

    probed = Signal(int, list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _ProbeSignals()
        # 転送先は**メソッド**にする（``self.probed.emit`` を直接つながない）。
        # bound method の受け手は QObject の ``self`` なので、self が破棄されると
        # Qt が接続ごと外し、着地し損ねたチャンクは黙って捨てられる。
        # ``Signal.emit`` は QObject ではないただの callable で、PySide は
        # グローバル受け手経由でつなぐため self の破棄に追随せず、破棄後に
        # 届いた queued 結果が "Signal source has been deleted" を送出する
        # （閉じた窓を破棄するテストハーネスで実測 — 2026-09-03）。
        self._signals.probed.connect(self._forward_probed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)  # one dispatch task at a time
        # 世代とキャンセルのペアは SessionOwner が 1 箇所で持つ（項目#35）。
        self._sessions = SessionOwner()
        self._parallelism = _PROBE_PARALLELISM

    def _forward_probed(self, generation: int, rows: list) -> None:
        """ワーカーのチャンクを公開シグナルへ転送する（受け手 = self の slot）。"""
        self.probed.emit(generation, rows)

    def set_parallelism(self, value: int) -> None:
        """Header-read concurrency for the next request (default 2)."""
        self._parallelism = max(1, int(value))

    def request(self, specs: list[ProbeSpec]) -> int:
        """Probe ``specs`` (each ``(path, mtime, size[, image_path])``); cancel
        any prior request first (``SessionOwner.start``)."""
        # Queued-but-unstarted dispatch tasks are stale by definition — drop
        # them rather than letting them run just to early-return.
        self._pool.clear()
        session = self._sessions.start()
        task = _AspectProbeTask(
            session, list(specs), self._signals,
            parallelism=self._parallelism,
            latest_generation=self._sessions.latest_generation,
        )
        self._pool.start(task)
        return session.generation

    def cancel(self) -> None:
        """Cancel the in-flight probe; bumps the generation too (#35) so a
        chunk already queued for delivery goes stale — except the salvaged
        completed-read rows, which are re-stamped with the newest generation
        by the task (#47)."""
        self._pool.clear()
        self._sessions.cancel()

    def latest_generation(self) -> int:
        return self._sessions.latest_generation()


__all__ = ["AspectProbeScanner", "read_image_aspect"]
