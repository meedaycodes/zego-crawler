"""Core crawl orchestration: a bounded worker pool draining a shared frontier."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from crawler.fetcher import Fetcher
from crawler.parser import extract_links
from crawler.urlutils import hostname, is_crawlable, is_same_domain, normalize_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """One crawled page: its URL and every link found on it."""

    page_url: str
    links: list[str]
    status: int | None
    error: str | None


@dataclass(slots=True)
class CrawlStats:
    pages_crawled: int = 0
    pages_failed: int = 0
    links_found: int = 0


async def crawl(
    base_url: str,
    *,
    concurrency: int = 50,
    max_pages: int | None = None,
    timeout_seconds: float = 10.0,
    on_result: Callable[[CrawlResult], None] | None = None,
) -> CrawlStats:
    """Breadth-first crawl of `base_url`'s exact host.

    Concurrency model: a fixed pool of `concurrency` worker coroutines
    continuously pull URLs from a shared `asyncio.Queue` (the "frontier")
    rather than fetching recursively layer-by-layer (all of depth 0, *then*
    all of depth 1, ...). A worker pool keeps concurrency saturated the
    whole time -- as soon as one page finishes, that worker immediately
    starts the next queued URL -- instead of bursting to `concurrency`
    in-flight requests at the start of each layer and idling while the
    slowest page in that layer finishes. On a real site with uneven page
    sizes and response times, that idle time is the difference between a
    crawl that's fast and one that's fast on paper. See the README's
    "concurrency model" section for the alternatives considered.

    Termination: workers never decide "the frontier looks empty, I'll stop"
    themselves -- that's racy, since another worker mid-fetch might be about
    to enqueue more URLs. Instead this uses `asyncio.Queue`'s built-in
    task-tracking: every `put()` increments an internal counter, every
    `task_done()` decrements it, and `queue.join()` blocks until that
    counter is genuinely zero -- which can only happen once no page being
    processed can add more work. This is the standard library's own
    documented pattern for exactly this producer/consumer shutdown problem.
    """
    base_url = normalize_url(base_url)
    base_host = hostname(base_url)
    if not base_host:
        raise ValueError(f"Could not determine a hostname from {base_url!r}")

    frontier: asyncio.Queue[str] = asyncio.Queue()
    seen: set[str] = {base_url}
    stats = CrawlStats()
    await frontier.put(base_url)

    # A closed output sink (classically: stdout piped into `head`, which
    # exits and closes the pipe once it has its N lines) is a different
    # kind of failure from "this one page didn't fetch": it's not
    # per-page, and calling on_result again just raises BrokenPipeError
    # again, on every remaining page, forever. `emit` catches that once,
    # logs it once, and silently no-ops afterwards -- the crawl still runs
    # to completion in the background rather than printing into the void.
    # (A fuller version would cancel the frontier immediately instead of
    # continuing to fetch pages nobody will see -- see the README.)
    output_closed = False

    def emit(result: CrawlResult) -> None:
        nonlocal output_closed
        if output_closed or on_result is None:
            return
        try:
            on_result(result)
        except BrokenPipeError:
            output_closed = True
            logger.info(
                "Output closed by downstream consumer (e.g. piped into `head`); "
                "continuing the crawl without printing further results."
            )

    async def worker(fetcher: Fetcher) -> None:
        while True:
            url = await frontier.get()
            try:
                if max_pages is not None and stats.pages_crawled >= max_pages:
                    # Cap reached: drain the rest of the frontier without
                    # fetching, so join() can still resolve. This is a soft
                    # cap -- fetches already in flight when the cap is hit
                    # still complete -- which is the right trade-off for a
                    # crawl-size *limit*, as opposed to a hard kill-switch.
                    continue
                await _process_one(url, fetcher, base_host, frontier, seen, stats, emit)
            except Exception:  # noqa: BLE001 - one bad page must never kill the crawl
                logger.exception("Unhandled error processing %s", url)
                stats.pages_failed += 1
            finally:
                frontier.task_done()

    async with Fetcher(concurrency=concurrency, timeout_seconds=timeout_seconds) as fetcher:
        workers = [asyncio.create_task(worker(fetcher)) for _ in range(concurrency)]
        await frontier.join()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    return stats


async def _process_one(
    url: str,
    fetcher: Fetcher,
    base_host: str,
    frontier: asyncio.Queue[str],
    seen: set[str],
    stats: CrawlStats,
    on_result: Callable[[CrawlResult], None] | None,
) -> None:
    result = await fetcher.fetch(url)

    if result.error is not None:
        stats.pages_failed += 1
        if on_result:
            on_result(CrawlResult(url, [], None, result.error))
        return

    if result.html is None:
        # A non-HTML response (a PDF, an image, ...) that a page happened to
        # link to. Not a failure, just nothing to parse or follow further.
        return

    links = extract_links(url, result.html)
    stats.pages_crawled += 1
    stats.links_found += len(links)
    if on_result:
        # Every link found on the page is reported, per the brief -- external
        # domains, subdomains, and non-http(s) URIs (mailto:, tel:, ...)
        # included. Only same-domain, http(s) links are enqueued to crawl.
        on_result(CrawlResult(url, links, result.status, None))

    for link in links:
        if not is_crawlable(link):
            continue
        normalized = normalize_url(link)
        if normalized in seen or not is_same_domain(normalized, base_host):
            continue
        seen.add(normalized)
        await frontier.put(normalized)
