"""Backwards-compatible façade for the viewer's off-thread scanners.

The scanners were originally all defined here; #141 split them into two
focused modules:

* :mod:`scan_children` — :class:`ChildrenScanner` (the shallow + progressive
  ``post.md`` two-stage children scan shared by both panes).
* :mod:`scan_search` — :class:`RecursiveSearchScanner` and the shared
  posted_at / existence-check helpers.

The AI-tag / vector scanners (``TagSearchScanner`` / ``VectorSearchScanner``)
moved out of the viewer entirely — they now live in the official AI plugin
(``plugins/snappix_ai/engine/scanners.py``) and reach the viewer only through
the :mod:`~.ai_pack` provider registry.

This module re-exports every *remaining* name the old monolith exposed so
existing imports (``from .scan_worker import ChildrenScanner`` /
``_ScanChildrenTask`` / ``_CancelToken`` / …) keep working unchanged.  New
code should import from the focused modules directly.
"""

from __future__ import annotations

# Shared cooperative cancel flag — historically lived here as ``_CancelToken``;
# now sourced from its own tiny module (aspect_probe and the split scanners all
# import it).  Re-exported for callers/tests that still reach for
# ``scan_worker._CancelToken``.
from .cancel_token import CancelToken as _CancelToken
from .folder_scan import FolderEntry
from .scan_children import (
    ChildrenScanner,
    _ScanChildrenTask,
    _ScanSignals,
)
from .scan_search import (
    RecursiveSearchScanner,
    _node_row_to_entry,
    _RecursiveSearchSignals,
    _RecursiveSearchTask,
    emit_with_existence_check,
)

__all__ = [
    "ChildrenScanner",
    "FolderEntry",
    "RecursiveSearchScanner",
]
