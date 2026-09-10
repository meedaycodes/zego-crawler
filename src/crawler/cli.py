"""Command-line entry point: `domain-crawler <url>`.

See the README's "extending beyond a CLI" section for why a CLI is the
right shape for this exercise specifically, and what shape a version
crawling many domains, or running continuously, would need instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import TextIO

from crawler.crawler import CrawlResult, crawl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-crawler",
        description=(
            "Crawl a single domain starting from BASE_URL, printing each page "
            "found and every link discovered on it. Only links on the same "
            "host as BASE_URL are followed; links to other domains or "
            "subdomains are reported but not crawled."
        ),
    )
    parser.add_argument("base_url", help="The starting URL, e.g. https://example.com")
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=50,
        help="Maximum number of pages fetched at once (default: 50)",
    )
    parser.add_argument(
        "-n",
        "--max-pages",
        type=int,
        default=None,
        help="Stop after this many pages have been crawled (default: unlimited)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-request timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: human-readable text, or newline-delimited JSON (default: text)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity for diagnostics printed to stderr (default: WARNING)",
    )
    return parser


def _print_text(result: CrawlResult, stream: TextIO) -> None:
    if result.error is not None:
        print(f"FAILED {result.page_url} ({result.error})", file=stream)
        return
    print(result.page_url, file=stream)
    for link in sorted(set(result.links)):
        print(f"  {link}", file=stream)


def _print_json(result: CrawlResult, stream: TextIO) -> None:
    record = {
        "page_url": result.page_url,
        "status": result.status,
        "error": result.error,
        "links": sorted(set(result.links)),
    }
    print(json.dumps(record), file=stream)


async def _run(args: argparse.Namespace) -> int:
    printer = _print_json if args.format == "json" else _print_text
    start = time.monotonic()

    stats = await crawl(
        args.base_url,
        concurrency=args.concurrency,
        max_pages=args.max_pages,
        timeout_seconds=args.timeout,
        on_result=lambda result: printer(result, sys.stdout),
    )

    elapsed = time.monotonic() - start
    pages_per_second = stats.pages_crawled / elapsed if elapsed > 0 else 0.0
    print(
        f"--- {stats.pages_crawled} pages crawled, {stats.pages_failed} failed, "
        f"{stats.links_found} links found in {elapsed:.2f}s "
        f"({pages_per_second:.1f} pages/s) ---",
        file=sys.stderr,
    )
    return 1 if stats.pages_crawled == 0 and stats.pages_failed > 0 else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(_run(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
