import asyncio

import pytest
from conftest import RouteSpec, running_site

from crawler import fetcher as fetcher_module
from crawler.crawler import CrawlResult, crawl


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_attempt: int) -> None:
        return None

    monkeypatch.setattr(fetcher_module, "_backoff", _no_sleep)


def _html(*hrefs: str) -> str:
    return "<html><body>" + "".join(f'<a href="{h}">link</a>' for h in hrefs) + "</body></html>"


async def test_crawls_only_the_same_domain() -> None:
    routes = {
        "/": RouteSpec.html(
            _html(
                "/about",
                "https://sub.example.invalid/",  # subdomain -- must not be crawled
                "https://other.invalid/",  # different domain -- must not be crawled
            )
        ),
        "/about": RouteSpec.html(_html()),
    }
    async with running_site(routes) as (base_url, calls):
        results: list[CrawlResult] = []
        stats = await crawl(base_url, concurrency=5, on_result=results.append)

    crawled_urls = {r.page_url for r in results}
    assert crawled_urls == {base_url, base_url + "about"}
    assert stats.pages_crawled == 2
    assert stats.pages_failed == 0
    # Only the two same-domain paths were ever requested from our server --
    # confirms the crawler never even attempted the off-domain links.
    assert set(calls.counts) == {"/", "/about"}

    home = next(r for r in results if r.page_url == base_url)
    assert "https://sub.example.invalid/" in home.links
    assert "https://other.invalid/" in home.links


async def test_deduplicates_pages_linked_from_multiple_places() -> None:
    routes = {
        "/": RouteSpec.html(_html("/a", "/b")),
        "/a": RouteSpec.html(_html("/shared")),
        "/b": RouteSpec.html(_html("/shared")),
        "/shared": RouteSpec.html(_html()),
    }
    async with running_site(routes) as (base_url, calls):
        stats = await crawl(base_url, concurrency=5)

    assert stats.pages_crawled == 4  # /, /a, /b, /shared
    assert calls.counts["/shared"] == 1  # fetched once despite two inbound links


async def test_records_fetch_failures_without_stopping_the_crawl() -> None:
    routes = {
        "/": RouteSpec.html(_html("/broken", "/ok")),
        "/broken": RouteSpec.status(500),
        "/ok": RouteSpec.html(_html()),
    }
    async with running_site(routes) as (base_url, _calls):
        results: list[CrawlResult] = []
        stats = await crawl(base_url, concurrency=5, on_result=results.append)

    assert stats.pages_crawled == 2  # / and /ok
    assert stats.pages_failed == 1  # /broken
    broken = next(r for r in results if r.page_url == base_url + "broken")
    assert broken.error == "HTTP 500"
    assert broken.links == []


async def test_respects_max_pages_cap() -> None:
    # A chain: / -> /1 -> /2 -> /3 -> /4, each page linking only to the
    # next, so the crawl order is deterministic at concurrency=1.
    routes = {"/": RouteSpec.html(_html("/1"))}
    for i in range(1, 5):
        nxt = f"/{i + 1}" if i < 4 else None
        routes[f"/{i}"] = RouteSpec.html(_html(nxt) if nxt else _html())

    async with running_site(routes) as (base_url, _calls):
        stats = await crawl(base_url, concurrency=1, max_pages=2)

    assert stats.pages_crawled == 2


async def test_non_http_links_are_reported_but_never_fetched() -> None:
    routes = {"/": RouteSpec.html(_html("mailto:hi@example.com", "tel:+441234567890"))}
    async with running_site(routes) as (base_url, calls):
        results: list[CrawlResult] = []
        stats = await crawl(base_url, concurrency=5, on_result=results.append)

    assert stats.pages_crawled == 1
    assert set(results[0].links) == {"mailto:hi@example.com", "tel:+441234567890"}
    assert set(calls.counts) == {"/"}  # never attempted to fetch the mailto:/tel: URIs


async def test_invalid_base_url_raises_value_error() -> None:
    with pytest.raises(ValueError):
        await crawl("not-a-url", concurrency=1)


async def test_broken_output_pipe_does_not_hang_or_spam_failures() -> None:
    """Regression test for a real bug found while dogfooding the CLI.

    Piping the CLI's output into `head` closes stdout early, so on_result
    raises BrokenPipeError on some later page. The original implementation
    treated that exactly like a failed fetch: it logged a full traceback
    and counted it as a failed *page*, then did the same for every single
    remaining page (since on_result keeps raising) -- and in the worst
    case, if every worker's next on_result call raises around the same
    time, every worker dies without dequeuing again, which can leave items
    stuck in the frontier forever with nothing left alive to drain them:
    `crawl()` would hang. This drives a 6-page chain through an on_result
    that raises BrokenPipeError from the very first call, and asserts the
    crawl still completes (no hang) with all pages counted as crawled, not
    failed.
    """
    routes = {"/": RouteSpec.html(_html("/1"))}
    for i in range(1, 6):
        nxt = f"/{i + 1}" if i < 5 else None
        routes[f"/{i}"] = RouteSpec.html(_html(nxt) if nxt else _html())

    def always_broken(_result: CrawlResult) -> None:
        raise BrokenPipeError("downstream closed the pipe")

    async with running_site(routes) as (base_url, _calls):
        stats = await asyncio.wait_for(
            crawl(base_url, concurrency=3, on_result=always_broken), timeout=5.0
        )

    assert stats.pages_crawled == 6
    assert stats.pages_failed == 0
