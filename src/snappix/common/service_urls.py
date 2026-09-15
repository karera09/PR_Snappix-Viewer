"""Service post-URL reverse lookup: post-page URL → ``(service_id, post_id)``.

Single home for the "which service does this post URL belong to, and what is
its post id" knowledge.  This is a *compatibility* registry for libraries
written by external tools: the
viewer resolves author-written links in a rendered ``post.md`` body
(``https://fantia.jp/posts/12345``) back to ``(service, post_id)`` so it can
offer a jump to the local copy — it only ever *parses* these URLs, never
builds them or contacts the services.

Centralising the regexes here as a registry keeps one source of truth:
re-declaring them inside ``viewer/post_link_index.py`` would create a second
copy that a newly supported service silently breaks.  Supporting a new
service's URL shape is one :func:`register_service_urls` call here.

The registry is a module-global list of :class:`ServiceUrlPattern`.  The three
built-in services (fantia / fanbox / patreon) are registered at import time;
an extension can add its own with :func:`register_service_urls` before its
posts are browsed.

    >>> parse_post_url("https://fantia.jp/posts/12345")
    ('fantia', '12345')

``post_id`` is unique within a service, so matching on ``(service, post_id)``
is robust against the author writing a different URL form than the one the
external tool stored (notably Fanbox's ``@creator/posts/ID`` vs the legacy
``creator.fanbox.cc/posts/ID``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "ServiceUrlPattern",
    "register_service_urls",
    "parse_post_url",
    "registered_services",
]


@dataclass(frozen=True)
class ServiceUrlPattern:
    """One service's post-URL → ``(service_id, post_id)`` mapping.

    * ``service_id`` — the canonical service key (``fantia`` / ``fanbox`` /
      ``patreon`` / …), the same value external tools write to the
      ``post.md`` ``- service:`` meta line.
    * ``patterns`` — compiled regexes; the **last** capture group of the first
      one that matches yields the post id.  Several patterns per service cover
      alternate URL forms (Fanbox's ``@creator`` vs subdomain form).
    """

    service_id: str
    patterns: tuple[re.Pattern[str], ...]

    def match(self, url: str) -> str | None:
        """Return the post id if *url* is one of this service's post pages.

        Anchored at the start of *url* (``re.Pattern.match``) so a service URL
        embedded mid-string — e.g. an open-redirect
        ``https://evil.example.com/?u=https://fantia.jp/posts/12345`` — is not
        misresolved to a local post.  Query/fragment suffixes still match
        because the patterns don't require an end anchor.
        """
        for pat in self.patterns:
            m = pat.match(url)
            if m:
                # The post id is always the last capture group so alternate
                # forms (which may capture a creator subdomain first) still land
                # on the id — the canonical post-page form of each service.
                return m.group(m.lastindex or 1)
        return None


#: Ordered registry of every known service's URL patterns.  Insertion order is
#: the match order; the built-ins are registered below and plugins append.
_REGISTRY: list[ServiceUrlPattern] = []


def register_service_urls(
    service_id: str, patterns: list[str] | list[re.Pattern[str]]
) -> None:
    """Register (or replace) a service's post-URL patterns.

    Idempotent per ``service_id``: registering the same id again replaces the
    previous entry in place (so a plugin re-import or a test re-registration
    doesn't stack duplicates).  ``patterns`` are compiled case-insensitively if
    given as strings; pre-compiled patterns are used verbatim.
    """
    compiled = tuple(
        p if isinstance(p, re.Pattern) else re.compile(p, re.IGNORECASE)
        for p in patterns
    )
    entry = ServiceUrlPattern(service_id, compiled)
    for i, existing in enumerate(_REGISTRY):
        if existing.service_id == service_id:
            _REGISTRY[i] = entry
            return
    _REGISTRY.append(entry)


def parse_post_url(url: str) -> tuple[str, str] | None:
    """Return ``(service_id, post_id)`` for a recognised post-page *url*.

    Iterates the registry in registration order and returns the first match.
    Returns ``None`` for any URL that isn't a known service's post page (or an
    empty string).
    """
    if not url:
        return None
    for entry in _REGISTRY:
        post_id = entry.match(url)
        if post_id:
            return (entry.service_id, post_id)
    return None


def registered_services() -> list[str]:
    """The service ids currently registered, in match order (for diagnostics)."""
    return [e.service_id for e in _REGISTRY]


# --------------------------------------------------------------------------
# Built-in services.  Each pattern's LAST capture group is the post id; the
# forms are each service's canonical post-page URL.
# --------------------------------------------------------------------------

register_service_urls("fantia", [r"https?://fantia\.jp/posts/(\d+)"])
register_service_urls(
    "fanbox",
    [
        # ``@creator`` form (current ``endpoints.post_page`` output).
        r"https?://(?:www\.)?fanbox\.cc/@[^/]+/posts/(\d+)",
        # Legacy ``creator.fanbox.cc/posts/ID`` subdomain form.  The creator
        # subdomain is captured first but ``match`` uses the LAST group (the id).
        r"https?://([^./]+)\.fanbox\.cc/posts/(\d+)",
    ],
)
# Patreon canonical form is ``/posts/<slug>-<id>``; the slug (with its own
# hyphens) is consumed greedily, then backtracking leaves ``(\d+)`` on the
# trailing id.  Bare ``/posts/<id>`` (no slug) also matches.
register_service_urls(
    "patreon",
    [r"https?://(?:www\.)?patreon\.com/posts/(?:[\w-]*-)?(\d+)"],
)
