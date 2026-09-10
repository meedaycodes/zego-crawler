"""URL normalization, resolution, and same-domain matching.

Kept dependency-free (stdlib `urllib.parse` only) since these are small,
hot-path, easily-unit-tested pure functions -- exactly the kind of code
that doesn't benefit from a third-party library.
"""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin, urlparse

_CRAWLABLE_SCHEMES = {"http", "https"}


def normalize_url(url: str) -> str:
    """Canonicalize a URL so equivalent pages hash/compare equal.

    - Strips the fragment ("#section"): fragments are resolved client-side
      and never change what the server returns, so keeping them would treat
      "/page#a" and "/page#b" as two different pages.
    - Lowercases the scheme and host (host/scheme are case-insensitive per
      RFC 3986); the path and query are left byte-for-byte untouched.

    Deliberately *not* done here: collapsing "/about" and "/about/" to the
    same page. They usually are the same page, but "usually" is doing a lot
    of work -- some servers route them differently, and this value doubles
    as the literal URL that gets fetched (see crawler.py), so silently
    rewriting it risks requesting a different resource than the one that
    was actually linked. Treating them as distinct is the conservative,
    always-correct choice at the cost of occasionally crawling both. See
    the README's "URL normalisation" section for the trade-off.
    """
    defragmented, _fragment = urldefrag(url)
    parsed = urlparse(defragmented)
    normalized = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower())
    return normalized.geturl()


def resolve_any(base_url: str, href: str) -> str | None:
    """Resolve a (possibly relative) href found on `base_url` to an absolute URL.

    Used for *reporting* every URL a page links to, per the brief -- so this
    keeps non-http(s) URIs like "mailto:" and "tel:" rather than discarding
    them, since they're still genuine URLs the page contains. The only
    hrefs treated as "not a URL" are empty strings and pure in-page anchors
    ("#top"), which resolve to the current page rather than a separate one.
    """
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    try:
        return urljoin(base_url, href)
    except ValueError:
        # Malformed href (e.g. control characters); skip rather than crash
        # the crawl over one bad link on one page.
        return None


def is_crawlable(url: str) -> bool:
    """True for URLs the crawler can actually fetch over HTTP(S).

    Used to narrow the broader `resolve_any()` output down to what's worth
    enqueueing: "mailto:", "tel:", and "javascript:" hrefs are real URLs
    worth reporting (see `resolve_any`) but there is nothing to GET.
    """
    return urlparse(url).scheme.lower() in _CRAWLABLE_SCHEMES


def hostname(url: str) -> str:
    """The exact hostname a URL points at, lowercased, with no port."""
    return (urlparse(url).hostname or "").lower()


def is_same_domain(url: str, base_host: str) -> bool:
    """True only if `url`'s host is *exactly* `base_host`.

    Deliberately not a suffix/eTLD+1 match: the brief asks to exclude
    subdomains, and "blog.example.com" is a subdomain of "example.com" even
    though both share a registrable domain. This does mean "example.com"
    and "www.example.com" are treated as different domains too, which is a
    real trade-off -- see the README.
    """
    return hostname(url) == base_host
