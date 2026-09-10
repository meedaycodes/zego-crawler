"""HTML link extraction.

Uses selectolax (a Python binding over the Modest/Lexbor C HTML engine)
rather than BeautifulSoup or raw lxml. On link-extraction workloads it is
routinely 5-20x faster than BeautifulSoup with either parser backend, and
crawler parsing is squarely CPU-bound once fetches are concurrent -- see the
README's "HTML parsing" section for the benchmark reasoning and the
fallback story if selectolax isn't installable on a given machine.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser

from crawler.urlutils import resolve_any


def extract_links(page_url: str, html: str) -> list[str]:
    """Return every absolute URL linked from `html` via <a href>.

    Only anchor tags are treated as "page" links, matching the brief's
    "URLs it finds on that page" framing. Asset references (<img src>,
    <link href> stylesheets, <script src>) are deliberately excluded, since
    they're resources the page uses, not pages the crawler should print or
    follow -- see the README for the trade-off and how to widen this.

    This includes non-http(s) URLs such as "mailto:" and "tel:" links: the
    brief asks for every URL found on the page, and narrowing that down to
    "what the crawler can also fetch" is the crawler's concern (it applies
    that filter separately when deciding what to enqueue), not the
    extractor's -- see `crawler.urlutils.is_crawlable`.

    Duplicate hrefs on the same page are preserved in the returned list (in
    document order) rather than de-duplicated here: de-duplication is the
    crawler's job, against its global "seen" set, not the parser's.
    """
    tree = HTMLParser(html)
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = node.attributes.get("href")
        if not href:
            continue
        absolute = resolve_any(page_url, href)
        if absolute is not None:
            links.append(absolute)
    return links
