import pytest
from conftest import RouteSpec, running_site

from crawler import fetcher as fetcher_module
from crawler.fetcher import Fetcher


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real exponential-backoff sleep so retry tests run instantly."""

    async def _no_sleep(_attempt: int) -> None:
        return None

    monkeypatch.setattr(fetcher_module, "_backoff", _no_sleep)


async def test_fetches_html_successfully() -> None:
    async with (
        running_site({"/": RouteSpec.html("<html>hi</html>")}) as (base_url, _calls),
        Fetcher() as fetcher,
    ):
        result = await fetcher.fetch(base_url)

    assert result.status == 200
    assert result.html == "<html>hi</html>"
    assert result.error is None


async def test_non_html_response_has_no_html_and_no_error() -> None:
    routes = {"/file.pdf": RouteSpec([(200, "%PDF-1.4", "application/pdf")])}
    async with running_site(routes) as (base_url, _calls), Fetcher() as fetcher:
        result = await fetcher.fetch(base_url + "file.pdf")

    assert result.status == 200
    assert result.html is None
    assert result.error is None


async def test_404_is_reported_as_error_not_parsed() -> None:
    routes = {"/missing": RouteSpec.html("<html>Not found</html>", status=404)}
    async with running_site(routes) as (base_url, _calls), Fetcher() as fetcher:
        result = await fetcher.fetch(base_url + "missing")

    assert result.status == 404
    assert result.html is None
    assert result.error == "HTTP 404"


async def test_retries_transient_5xx_then_succeeds() -> None:
    routes = {
        "/": RouteSpec(
            [
                (503, "", "text/plain"),
                (200, "<html>ok</html>", "text/html"),
            ]
        )
    }
    async with running_site(routes) as (base_url, calls), Fetcher(max_retries=2) as fetcher:
        result = await fetcher.fetch(base_url)

    assert result.status == 200
    assert result.html == "<html>ok</html>"
    assert calls.counts["/"] == 2  # one failed attempt, one retry


async def test_exhausts_retries_and_surfaces_final_status() -> None:
    async with (
        running_site({"/": RouteSpec.status(503)}) as (base_url, calls),
        Fetcher(max_retries=2) as fetcher,
    ):
        result = await fetcher.fetch(base_url)

    assert result.status == 503
    assert result.error == "HTTP 503"
    assert calls.counts["/"] == 3  # initial attempt + 2 retries, all 503


async def test_non_retryable_status_is_not_retried() -> None:
    async with (
        running_site({"/private": RouteSpec.status(403)}) as (base_url, calls),
        Fetcher(max_retries=2) as fetcher,
    ):
        result = await fetcher.fetch(base_url + "private")

    assert result.status == 403
    assert result.error == "HTTP 403"
    assert calls.counts["/private"] == 1  # no retry wasted on a real auth failure


async def test_connection_error_is_reported() -> None:
    # Bind a server, note its port, then tear it down: fetching that port
    # afterwards gets a real, immediate "connection refused" from the OS --
    # no mocking, no network/DNS dependency.
    async with running_site({}) as (base_url, _calls):
        dead_url = base_url

    async with Fetcher(max_retries=0) as fetcher:
        result = await fetcher.fetch(dead_url)

    assert result.status is None
    assert result.html is None
    assert result.error is not None
