"""Japanese (default) message catalog, assembled from domain fragments.

The catalog is split by area (``_common`` / ``_viewer``)
purely for navigability — they are merged here into a single ``MESSAGES``
dict.  The merge is **collision-checked**: a key defined in two fragments
raises at import time, catching the classic fan-out mistake of two agents
minting the same key.

Inside a fragment the keys are kept in **alphabetical order** (sections =
module names therefore sort too), which is what makes a several-hundred-key
dict navigable by eye and keeps a duplicate visible — the merge above cannot
see a key repeated *within* one fragment, since a dict literal silently
overwrites it.  ``tests/test_i18n.py::test_catalog_keys_are_sorted`` enforces
the order.
"""

from __future__ import annotations

from . import _common, _viewer


def _merge(*fragments: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for frag in fragments:
        for key, value in frag.items():
            if key in out:
                raise ValueError(
                    f"i18n(ja): duplicate key {key!r} defined in more than one "
                    f"catalog fragment"
                )
            out[key] = value
    return out


MESSAGES: dict[str, str] = _merge(
    _common.MESSAGES,
    _viewer.MESSAGES,
)
