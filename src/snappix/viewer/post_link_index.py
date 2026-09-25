"""Pure helpers for the MarkdownView "downloaded-post link" feature.

A ``post.md`` body often links to the creator's *other* posts
(``https://fantia.jp/posts/12345``).  When such a post is already
downloaded we want to:

* show the link distinctly (it is a *local* post, not just any URL), and
* offer a one-click jump to the downloaded copy *in addition to* the web link.

This module is Qt-free so it can be unit-tested without a display:

* :func:`parse_post_url` extracts ``(service, post_id)`` from a service URL.
  The URL patterns themselves live in :mod:`snappix.common.service_urls` —
  a *common* registry is what lets a new service register its URL shape in
  one place instead of editing this module.  :func:`parse_post_url` is kept
  here as a thin re-export so existing callers / tests need no change.
* :func:`classify_post_links` rewrites rendered HTML, tagging anchors whose
  target resolves to a downloaded folder and appending a 📁 jump anchor.

Resolution itself (``(service, post_id) → folder``) lives in
:class:`~snappix.viewer.search_index.SearchIndex` (the ``postref`` table); the
caller passes a ``resolve`` callable so this module stays storage-agnostic.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from ..common.i18n import t
from ..common.service_urls import parse_post_url


# Rendered anchors from markdown-it (commonmark + linkify) are simple
# ``<a href="...">text</a>``.  DOTALL so inner text spanning the (rare) line
# break is captured; non-greedy ``.*?`` so adjacent anchors don't merge.
_ANCHOR_RE = re.compile(
    r'<a\s+href="([^"]+)"([^>]*)>(.*?)</a>', re.IGNORECASE | re.DOTALL
)

# Custom scheme used for the appended local-jump anchor.  ``MarkdownView``
# intercepts this scheme in ``anchorClicked`` and maps the key back to the
# folder via the per-post ``targets`` dict returned here.
LOCAL_SCHEME = "snappixpost"


def _is_self_folder(folder: Path, self_folder: Path | None) -> bool:
    """Cheap textual equality — never a filesystem stat.

    ``classify_post_links`` runs on the GUI thread for every anchor on every
    ``setHtml`` (see its docstring), so this mirrors that NAS-free contract:
    plain ``normcase``/``normpath`` string comparison, no ``resolve()``/
    ``samefile()``.
    """
    if self_folder is None:
        return False
    return os.path.normcase(os.path.normpath(str(folder))) == os.path.normcase(
        os.path.normpath(str(self_folder))
    )


def classify_post_links(
    html: str,
    resolve: Callable[[str], Path | None],
    *,
    self_folder: Path | None = None,
) -> tuple[str, dict[str, Path]]:
    """Tag downloaded-post links in *html* and append a 📁 local-jump anchor.

    For every ``<a href=URL>`` whose ``resolve(URL)`` returns a folder:

    * the original anchor keeps its web ``href`` (clicking opens the browser)
      and gains ``class="localpost"`` so CSS can mark it as a downloaded post;
    * a sibling ``<a href="snappixpost:<key>" class="localpost-jump">📁</a>`` is
      inserted right after it, where ``<key>`` indexes the returned dict.

    Unresolved anchors are left untouched.  Returns ``(new_html, targets)``
    where ``targets`` maps each generated key to its folder ``Path``.

    *self_folder*, when given, is the folder of the post currently being
    rendered.  ``html`` is not just the post body — the caller prepends the
    post-card header, whose 「投稿ページ」line links to the post's *own*
    source URL (see ``markdown_pipeline._post_header_html``).  Without exclusion
    that self-link always resolves (the post is, by definition, downloaded —
    it's the one on screen), so every post's card would sprout a local-copy pill
    + 📁 that just re-opens the folder already showing, drowning out the
    signal for genuine links to *other* downloaded posts. A link resolving to ``self_folder`` is therefore
    treated the same as an unresolved one — left untouched, no key emitted.

    *resolve* is called once per anchor and results are **not** cached here
    (this module is stateless): the caller re-runs classification on every
    ``setHtml``, so it owns the memo whose lifetime matches the shown post —
    see ``MarkdownView._post_link_cache``.
    """
    targets: dict[str, Path] = {}
    n = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal n
        href, attrs, inner = m.group(1), m.group(2), m.group(3)
        folder = resolve(href)
        if folder is None or _is_self_folder(folder, self_folder):
            return m.group(0)
        key = f"p{n}"
        n += 1
        targets[key] = folder
        tooltip = t("viewer.post_link_index.open_local_tooltip")
        return (
            f'<a href="{href}" class="localpost"{attrs}>{inner}</a>'
            f'<a href="{LOCAL_SCHEME}:{key}" class="localpost-jump"'
            f' title="{tooltip}">\U0001F4C1</a>'
        )

    return _ANCHOR_RE.sub(_sub, html), targets


__all__ = ["parse_post_url", "classify_post_links", "LOCAL_SCHEME"]
