"""Shared test fixtures.

Rather than mocking aiohttp internals, tests spin up a tiny real
aiohttp.web application on a loopback socket via aiohttp's own
`test_utils.TestServer`. This means fetcher.py and crawler.py are tested
against the exact same HTTP client/server machinery they use in
production, with no separate mocking library (and no risk of that library
lagging behind aiohttp's own release cadence -- which is exactly what
ruled out `aioresponses` here, see the README).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiohttp import web
from aiohttp.test_utils import TestServer


@dataclass
class RouteSpec:
    """What to return from a given path, across successive requests.

    `responses` is a list of (status, body, content_type) tuples: request
    N gets `responses[N]`, and once the list is exhausted the last entry
    repeats. A single-entry list means "always respond this way"; a
    two-entry list lets a test simulate "fails once, then succeeds" for
    exercising retry logic.
    """

    responses: list[tuple[int, str, str]]

    @classmethod
    def html(cls, body: str = "", status: int = 200) -> RouteSpec:
        return cls([(status, body, "text/html")])

    @classmethod
    def status(cls, status: int) -> RouteSpec:
        return cls([(status, "", "text/plain")])


@dataclass
class CallLog:
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, path: str) -> int:
        """Record one call to `path`; returns this call's zero-based index."""
        index = self.counts.get(path, 0)
        self.counts[path] = index + 1
        return index


@asynccontextmanager
async def running_site(routes: dict[str, RouteSpec]) -> AsyncIterator[tuple[str, CallLog]]:
    """Serve `routes` (keyed by path, e.g. "/about") on a loopback port.

    Yields (base_url, call_log). Any path not present in `routes` returns a
    plain 404, matching real-world "page doesn't exist" behaviour.
    """
    calls = CallLog()

    async def handler(request: web.Request) -> web.Response:
        path = request.path
        call_index = calls.record(path)
        spec = routes.get(path)
        if spec is None:
            return web.Response(status=404, text="not found")
        status, body, content_type = spec.responses[min(call_index, len(spec.responses) - 1)]
        return web.Response(status=status, text=body, content_type=content_type)

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/")), calls
    finally:
        await server.close()
