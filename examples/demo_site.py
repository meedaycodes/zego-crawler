"""A small, self-contained website you can crawl locally to try the tool out.

Not part of the package or its test suite -- this is a hands-on fixture for
a reviewer (or Habeeb) to point the crawler at without needing the live
internet, which is exactly how the crawler itself was verified end-to-end
during development. See the README's "trying it out" section.

Usage:
    uv run python examples/demo_site.py [--port 8000] [--latency-ms 40]
    # in another terminal:
    uv run domain-crawler http://127.0.0.1:8000/

The site has ~40 pages: a home page, a handful of category pages, several
product pages per category (each linking back to its category and to a
couple of "related" products, creating the kind of criss-crossing link
graph a real site has), one link to a subdomain, one to an external
domain, and one deliberately broken link -- so a single crawl exercises
dedup, domain restriction, and error handling all at once.
"""

from __future__ import annotations

import argparse
import asyncio
import random

from aiohttp import web

CATEGORIES = ["books", "electronics", "garden", "toys"]
PRODUCTS_PER_CATEGORY = 8


def _nav(*links: tuple[str, str]) -> str:
    return "".join(f'<a href="{href}">{text}</a>' for href, text in links)


def build_app(latency_ms: int) -> web.Application:
    async def with_latency() -> None:
        if latency_ms:
            await asyncio.sleep(random.uniform(0, latency_ms) / 1000)

    async def home(request: web.Request) -> web.Response:
        await with_latency()
        links = _nav(*[(f"/category/{c}/", c.title()) for c in CATEGORIES])
        links += '<a href="https://status.example-cdn.invalid/">Status page (external)</a>'
        return web.Response(
            text=f"<html><body><h1>Demo Shop</h1>{links}</body></html>",
            content_type="text/html",
        )

    async def category(request: web.Request) -> web.Response:
        await with_latency()
        name = request.match_info["name"]
        if name not in CATEGORIES:
            return web.Response(status=404, text="no such category")
        product_links = _nav(
            *[
                (f"/category/{name}/product/{i}", f"{name.title()} item {i}")
                for i in range(PRODUCTS_PER_CATEGORY)
            ]
        )
        return web.Response(
            text=(
                f"<html><body><h1>{name.title()}</h1>"
                f'<a href="/">Home</a>{product_links}</body></html>'
            ),
            content_type="text/html",
        )

    async def product(request: web.Request) -> web.Response:
        await with_latency()
        name = request.match_info["name"]
        index = int(request.match_info["index"])
        if name not in CATEGORIES or not (0 <= index < PRODUCTS_PER_CATEGORY):
            return web.Response(status=404, text="no such product")
        related = (index + 1) % PRODUCTS_PER_CATEGORY
        links = _nav(
            (f"/category/{name}/", "Back to category"),
            (f"/category/{name}/product/{related}", "Related item"),
        )
        if index == 0:
            # One deliberately broken link, and one out-of-domain link, so
            # a real crawl surfaces both an HTTP error and a skipped domain.
            links += '<a href="/category/does-not-exist">Clearance (broken)</a>'
            links += '<a href="https://reviews.example-cdn.invalid/">Reviews (external)</a>'
        return web.Response(
            text=(f"<html><body><h1>{name.title()} item {index}</h1>{links}</body></html>"),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/category/{name}/", category)
    app.router.add_get("/category/{name}/product/{index}", product)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=40,
        help="Simulated random per-request latency, so concurrency has a visible effect",
    )
    args = parser.parse_args()

    app = build_app(args.latency_ms)
    print(f"Demo site running at http://127.0.0.1:{args.port}/  (Ctrl+C to stop)")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
