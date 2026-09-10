"""Async HTTP fetching with a shared connection pool and light retry logic."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import TracebackType

import aiohttp

logger = logging.getLogger(__name__)

# Transient failures worth one or two retries: rate limiting and the classic
# "server is briefly unhappy" 5xx codes. Anything else (404, 403, ...) is a
# real answer from the server and retrying it wastes time without changing
# the outcome.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DEFAULT_USER_AGENT = "domain-crawler/0.1 (+https://github.com/meedaycodes/domain-crawler)"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of fetching one URL.

    Exactly one of `html` / `error` is meaningful for a given result:
    - success, HTML page: status is 2xx/3xx, html is set, error is None.
    - success, non-HTML response (PDF, image, ...): status is 2xx/3xx, html
      is None, error is None -- the crawler treats this as "nothing to
      parse", not a failure.
    - failure, HTTP error status (4xx/5xx, including a 5xx whose retries
      were exhausted): status is set, html is None, error names the status.
    - failure, transport-level (timeout, connection error, DNS failure):
      status is None, html is None, error describes the exception.
    """

    url: str
    status: int | None
    html: str | None
    error: str | None


class Fetcher:
    """A shared aiohttp session sized for a known level of concurrency.

    This class does *not* itself limit how many fetches run at once --
    that's the crawler's worker pool's job (see crawler.py), since it's the
    one component that actually knows the target concurrency. Duplicating a
    semaphore here as well would just be a second, harder-to-reason-about
    knob controlling the same thing. What this class *does* own is sizing
    the underlying TCP connection pool to match that concurrency, so
    connections are reused (keep-alive) instead of a full TCP+TLS handshake
    on every single request -- the single biggest lever available for
    crawling one host quickly.
    """

    def __init__(
        self,
        *,
        concurrency: int = 50,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._concurrency = concurrency
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_retries = max_retries
        self._headers = {"User-Agent": user_agent}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Fetcher:
        connector = aiohttp.TCPConnector(
            limit=self._concurrency,
            limit_per_host=self._concurrency,
            ttl_dns_cache=300,
        )
        self._session = aiohttp.ClientSession(
            timeout=self._timeout, headers=self._headers, connector=connector
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()

    async def fetch(self, url: str) -> FetchResult:
        """GET `url`, retrying transient failures with exponential backoff.

        Only HTML responses have their body read: everything else is
        identified by its Content-Type and its body is left unread, so a
        page that links to a 200MB video doesn't get buffered into memory
        for no reason.
        """
        if self._session is None:
            raise RuntimeError("Fetcher must be used as an async context manager")

        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._session.get(url, allow_redirects=True) as response:
                    if response.status in _RETRYABLE_STATUS and attempt < self._max_retries:
                        await _backoff(attempt)
                        continue
                    # Either a non-retryable status, or a retryable one whose
                    # retries are exhausted: this is the real terminal
                    # answer. Any 4xx/5xx is treated as a failed fetch --
                    # even a nicely-styled HTML 404 page -- so error pages
                    # never get parsed and crawled as if they were content.
                    if response.status >= 400:
                        return FetchResult(url, response.status, None, f"HTTP {response.status}")
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" not in content_type:
                        return FetchResult(url, response.status, None, None)
                    html = await response.text(errors="replace")
                    return FetchResult(url, response.status, html, None)
            except (aiohttp.ClientError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._max_retries:
                    await _backoff(attempt)
                    continue

        return FetchResult(url, None, None, last_error or "request failed")


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(0.5 * (2**attempt))
