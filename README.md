# domain-crawler

A command-line, single-domain web crawler. Point it at a URL; it crawls every
page reachable from that URL on the *same host*, printing each page's URL
and every link found on it. Links to other domains, subdomains, and non-http
schemes (`mailto:`, `tel:`) are reported but never followed.

```
$ domain-crawler https://example.com
https://example.com/
  https://example.com/about
  https://example.com/contact
  https://cdn.example.org/logo.png
https://example.com/about
  https://example.com/
  https://example.com/team
--- 2 pages crawled, 0 failed, 4 links found in 0.31s (6.5 pages/s) ---
```

No Scrapy, no Playwright, no browser automation, per the brief. The two
libraries this project does depend on are narrow and single-purpose:
[`aiohttp`](https://docs.aiohttp.org/) for HTTP, and
[`selectolax`](https://github.com/rushter/selectolax) for HTML parsing.


## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                          # installs runtime + dev dependencies
uv run domain-crawler https://example.com
```

Or install it as a proper CLI tool:

```bash
uv tool install .
domain-crawler https://example.com
```

### Trying it without the live internet

A small demo site ships in `examples/demo_site.py` — ~40 pages with the
kind of criss-crossing internal links, a broken link, and an external link
a real site has, so a single crawl against it exercises every code path.
This is genuinely how the crawler was verified during development (see
[Verification](#verification-what-i-actually-ran) below), not just a toy:

```bash
uv run python examples/demo_site.py &     # starts on http://127.0.0.1:8000/
uv run domain-crawler http://127.0.0.1:8000/
```

### Trying it against a real site

`sample_public_sites.txt` lists three sites that explicitly permit
scraping, ordered smallest-first. Actual runs on this machine
(`-c 50`, the default):

| Site | Pages | Failed | Links | Time | Rate |
|---|---|---|---|---|---|
| `http://quotes.toscrape.com/` | 214 | 0 | 4,406 | 1.96s | 109 pages/s |
| `https://www.scrapethissite.com/pages/` | 34 | 3 | 889 | 1.16s | 29 pages/s |
| `http://books.toscrape.com/` | 1,195 | 0 | 33,256 | 17.7s | 68 pages/s |

Notes from the runs:

- **quotes.toscrape.com** is larger than it first looks — the ~10 listing
  pages fan out into per-author and per-tag pages, which is what gets the
  count to 214.
- **scrapethissite.com** reports 3 failures, all `HTTP 400` on
  `/pages/advanced/?gotcha=...` URLs. Those endpoints deliberately reject
  clients that don't send the headers/cookies a browser would — the
  crawler correctly reports them as failed and keeps going.
- **books.toscrape.com** is the one big enough for `--concurrency` to
  matter. Same 1,195 pages and 33,256 links every run; only the wall time
  moves: `-c 5` → 31.7s, `-c 50` → 17.7s, `-c 100` → 17.1s (the site's
  own latency floor is the limit past ~50).

## CLI reference

```
domain-crawler BASE_URL [-c N] [-n N] [--timeout S] [--format text|json] [--log-level LEVEL]
```

| Flag | Default | Meaning |
|---|---|---|
| `-c`, `--concurrency` | `50` | Max pages fetched at once |
| `-n`, `--max-pages` | unlimited | Stop after this many pages |
| `--timeout` | `10` | Per-request timeout, seconds |
| `--format` | `text` | `text` (human-readable) or `json` (newline-delimited, one page per line) |
| `--log-level` | `WARNING` | Diagnostic verbosity on stderr |

Page data goes to **stdout**; the final run summary and any diagnostic
logging go to **stderr** — so `domain-crawler https://example.com > pages.txt`
gives a clean data file with progress still visible on the terminal, and the
output composes normally with `grep`, `jq`, `head`, and friends.

## Design decisions

### Concurrency model: a worker pool over a shared frontier

The core loop (`crawler.py`) is a fixed pool of `concurrency` async worker
coroutines pulling from a shared `asyncio.Queue` (the "frontier"), rather
than the more obvious-looking recursive approach: fetch the start page,
`asyncio.gather()` all its links, then gather *their* links, and so on
layer by layer.

The layer-by-layer approach has a real cost: concurrency is only as high as
the current layer's link count, and every worker in a layer has to wait for
the *slowest* page in that layer before the next layer can start. On a real
site with uneven page sizes, that idle time adds up fast. A worker pool
instead keeps every worker continuously busy — the instant one page
finishes, that worker immediately starts the next queued URL, regardless of
which "layer" it came from.

**Alternatives considered:**

- **`ThreadPoolExecutor` + `requests`** — works, and is simpler for someone
  unfamiliar with `asyncio`. Rejected because crawling is almost entirely
  I/O wait, which is precisely what async I/O is built for: one thread can
  hold hundreds of sockets open concurrently with far less overhead than
  hundreds of OS threads, and there's no GIL contention to reason about.
- **`multiprocessing`** — would help if parsing were the bottleneck (it
  isn't, particularly with `selectolax` — see below), and adds real
  complexity (shared state across processes, serialization) for a workload
  that's I/O-bound, not CPU-bound.
- **Naive recursive `asyncio.gather`** — simplest async version, but has
  the layer-idling problem above, and depth-first recursion risks Python's
  recursion limit on a deep or cyclic site without extra bookkeeping the
  queue-based version doesn't need.

**Termination** is the one genuinely subtle part of a worker-pool crawler:
workers can't just decide "the queue looks empty, I'll stop," because
another worker mid-fetch might be about to add more URLs to it — a
classic producer/consumer race. The fix is `asyncio.Queue`'s built-in
task-tracking: every `put()` increments an internal counter, every
`task_done()` decrements it, and `queue.join()` blocks until that counter
is genuinely zero — which can only happen once no in-flight page can add
more work. This is the standard library's own documented pattern for
exactly this shutdown problem, not something bespoke.

### HTTP client: aiohttp

`aiohttp` over `httpx` mainly for one concrete lever: `TCPConnector` gives
direct control over the connection pool size (`limit`, `limit_per_host`),
sized to match the crawler's concurrency so requests reuse keep-alive
connections instead of paying a fresh TCP+TLS handshake per request — the
single biggest lever available for crawling one host quickly. `httpx` is a
perfectly reasonable alternative (and its HTTP/2 support is a genuine
edge for some workloads); `aiohttp` was chosen for being the more
battle-tested option for exactly this shape of high-concurrency,
single-host workload.

Retries are narrow and deliberate (`fetcher.py`): only `429` and `5xx`
responses get retried, with exponential backoff, up to `max_retries`. A
`404` or `403` is a real answer from the server — retrying it wastes time
without changing the outcome, so those return immediately.

### HTML parsing: selectolax

`selectolax` wraps the Modest/Lexbor C HTML engines and is routinely
5-20x faster than BeautifulSoup (with either the `html.parser` or `lxml`
backend) on link-extraction workloads, while still being tolerant of the
malformed markup real websites produce (see
`tests/test_parser.py::test_handles_malformed_html_gracefully`). Given the
brief's explicit ask to run "as quickly as possible," and that HTML
parsing is the one genuinely CPU-bound step in an otherwise I/O-bound
pipeline, this was worth a dependency BeautifulSoup wouldn't have needed
justifying as hard.

### What gets *printed* vs. what gets *crawled*

These are deliberately different filters (`urlutils.py`: `resolve_any()`
vs. `is_crawlable()`), because the brief asks for two different things:
"print... all the URLs it finds on that page" (every URL, full stop) and
"only process that single domain" (a much narrower *crawling* rule).
Collapsing them into one filter — which an earlier version of this code
did — silently drops real data: a `mailto:` link on a contact page is a
genuine URL the page contains, even though there's obviously nothing to
`GET` there. So `extract_links()` returns everything (excluding only
empty hrefs and pure in-page anchors like `#top`, which aren't separate
resources at all), and the crawler applies `is_crawlable()` + same-domain
filtering only when deciding what to *enqueue*.

`<a href>` is the only tag inspected. `<img src>`, `<script src>`, and
`<link href>` (stylesheets) are asset references the page *uses*, not
pages the crawler should print or follow — see
[Limitations](#limitations--what-id-refine-with-more-time) for how this
would generalize.

### URL normalization and domain matching

`normalize_url()` strips the fragment (`#section` never changes what the
server returns) and lowercases the scheme/host (case-insensitive per
RFC 3986) — but deliberately does **not** collapse `/about` and `/about/`
into the same page. That wasn't the original design: an earlier version
did fold trailing slashes together, on the assumption they're "obviously"
the same page. Running the crawler against a real multi-page site during
development caught this immediately — the normalized string doubles as
the *literal URL that gets fetched*, so silently rewriting `/contact/` to
`/contact` requests a different resource than the one actually linked,
and on a server that treats them differently (mine, in testing, and
plenty of real ones) that's a wrong answer wearing a confident face. The
conservative fix — treat them as distinct — costs an occasional duplicate
crawl in exchange for never guessing wrong about what a URL points to.

`is_same_domain()` compares hostnames exactly: `blog.example.com` is *not*
the same domain as `example.com`, matching the brief's explicit "not
crawl URLs pointing to other domains or **subdomains**." A real
consequence of that literal reading: `www.example.com` and `example.com`
are technically also in a subdomain relationship, so they're treated as
different domains too, even though many sites redirect one to the other.
This is documented in the code (`urlutils.py`) and tested explicitly
(`test_urlutils.py::test_www_is_not_same_domain`) rather than left as a
silent surprise — the alternative (special-casing `www.`) is a reasonable
choice too, just a different reading of "subdomain" than the brief's
wording supports.

### Error handling philosophy

Every layer has one job and fails predictably: `fetcher.py` turns network
errors *and* HTTP error statuses into a `FetchResult` with `.error` set —
it never raises for anything the network legitimately did. `crawler.py`'s
worker loop wraps per-page processing in a broad exception handler so one
malformed page (an HTML parsing edge case, say) can't take down the whole
crawl; the failure is logged and counted, and the crawl moves on.

## A real bug caught by actually running the thing

Partway through development I ran the CLI's own output through `head`
(`domain-crawler ... | head -3`) — an extremely normal thing to do with a
command-line tool — and it hung. Worse, before it hung, it was spamming a
full Python traceback for every remaining page.

The cause: `head` reads its 3 lines and exits, which closes the pipe.
The *next* page whose result tried to `print()` raised `BrokenPipeError`
— and the crawler's "don't let one bad page kill the whole crawl"
exception handler (entirely correct for a page that failed to *fetch*)
was treating a broken *output sink* exactly the same way: log it, count
it as a failed page, keep going. Since the pipe stays closed, every
subsequent page hit the identical error. In the worst case — many
concurrent workers all hitting a closed pipe near-simultaneously — every
worker could die on its next print attempt without ever calling
`task_done()` on its current item, which is precisely what
`asyncio.Queue.join()` waits on: nothing left alive to drain the queue,
`join()` never returns, the process hangs forever.

The fix (`crawler.py`, see `emit()`): a closed output sink is a
fundamentally different failure than "this page didn't fetch" — it's not
a per-page condition, so it shouldn't be handled by the per-page handler.
`emit()` catches `BrokenPipeError` once, logs it once, and silently
no-ops on every call after that, so remaining pages still get *fetched and
counted* (the crawl finishes correctly and the final stats are accurate)
but nothing tries to print into a pipe that's gone. `tests/test_crawler.py
::test_broken_output_pipe_does_not_hang_or_spam_failures` reproduces this
exact scenario as a regression test, with an explicit `asyncio.wait_for`
timeout so a reintroduced version of the bug fails the test suite instead
of hanging CI.

The trade-off I kept, consciously: the crawl still runs to completion in
the background even though nothing more gets printed, rather than
aborting immediately like a C program receiving `SIGPIPE`. A fully
"correct" version would cancel the frontier the moment output closes,
instead of continuing to fetch pages nobody will see — see
[Limitations](#limitations--what-id-refine-with-more-time).

## Testing

44 tests, `uv run pytest` (~0.1s):

- **`test_urlutils.py`** — pure-function unit tests for normalization,
  resolution, and domain matching, including the trailing-slash and
  `www.` edge cases discussed above.
- **`test_parser.py`** — link extraction: relative/absolute resolution,
  non-http URIs, malformed HTML, asset tags correctly ignored.
- **`test_fetcher.py`** — retry/backoff behaviour, the 4xx/5xx-vs-network-
  error distinction, non-HTML content-type handling.
- **`test_crawler.py`** — full orchestration: same-domain-only crawling,
  dedup across multiple inbound links, `max_pages`, failures that don't
  stop the crawl, and the broken-pipe regression above.

**On mocking:** the original plan was `aioresponses` for HTTP mocking.
It turned out to be incompatible with the `aiohttp` version `uv` resolved
(`aioresponses` hasn't kept up with a signature change in `aiohttp`'s
`ClientResponse.__init__`) — a `TypeError` on every mocked call. Rather
than pin to an older `aiohttp` just to keep an unmaintained mocking layer
working, the tests use `aiohttp.test_utils.TestServer` instead: a tiny
real `aiohttp.web` app on a loopback socket (`tests/conftest.py`). This
means the fetcher and crawler are tested against the exact HTTP
client/server machinery they use in production, with one fewer dependency
and no risk of a mocking library lagging behind `aiohttp`'s release
cadence again.

### Verification: what I actually ran

Beyond `pytest`, `ruff check`, `ruff format --check`, and `mypy --strict`
(all wired into CI — see `.github/workflows/ci.yml`), I ran the actual
`domain-crawler` console-script entry point — not just the internal
`crawl()` function — against `examples/demo_site.py` (~40 pages) from the
command line, at both `-c 1` and `-c 20`:

```
-c 1:  37 pages crawled, 1 failed, 113 links found in 0.94s  ( 39.3 pages/s)
-c 20: 37 pages crawled, 1 failed, 113 links found in 0.11s  (351.6 pages/s)
```

Same page set both runs (diffed to confirm), same one deliberately-broken
link correctly reported as a failure both times — concurrency changed
throughput by roughly 9x without changing the result. That's the actual
number behind this README's "as quickly as possible" claims, not an
assumption.

It has since also been run against live public sites — see
[Trying it against a real site](#trying-it-against-a-real-site) above for
the numbers. `books.toscrape.com` (1,195 pages, 33,256 links) is the
useful one: the page/link counts are identical across `-c 5`, `-c 50`,
and `-c 100`, and only the wall-clock time changes (31.7s → 17.7s →
17.1s), which is the same result-stable / throughput-variable behaviour
the demo-site runs showed, now confirmed over the network rather than
just against `127.0.0.1`. `scrapethissite.com` also exercised the
error path for real: 3 endpoints there return `HTTP 400` to non-browser
clients, and the crawler reported them as failed and carried on.

The demo site still exists so a reviewer in a locked-down environment (or
anyone who'd rather not crawl a stranger's server while reviewing this)
has something real to run it against too.

## Extending beyond a CLI

The brief asks specifically about this, so concretely:

**Multiple domains at once** changes more than it sounds like. The
in-memory `seen: set[str]` and single `asyncio.Queue` frontier work fine
for one host; for many hosts crawled together you'd want per-host
politeness (a shared queue lets one slow or rate-limiting host starve
every other host's fair share of concurrency — worth a per-host token
bucket or a queue-of-queues rather than one flat frontier), per-host
`robots.txt` compliance (skipped entirely in this exercise — genuinely
important at multi-domain scale, much less so pointed at a single site
you already have permission to crawl), and a dedup store that isn't a
Python `set` in one process's memory — a Redis set or a bloom filter,
once "how many pages have I seen" no longer fits in one process's RAM.

**A CLI stops being the right interface** past roughly this scale, for a
simple reason: a CLI invocation blocks for the crawl's entire duration
and its result lives only in that terminal's stdout. That's fine for "run
this once against one site." It falls apart for "crawl 500 domains
continuously, revisit each on a schedule, and let three other services
query what's been found so far." What that actually wants is a small
service: an API to submit a crawl job and get an ID back, workers
(plural, likely on separate machines) pulling URLs from a shared queue —
genuinely the same architectural shape as a Kafka-topic-plus-consumer-
groups setup, not a coincidence — persisting results to a database or
object store instead of stdout, and a status/results endpoint instead of
watching a terminal. The core `crawl()` function in this repo is already
factored to make that transition less painful than it could be: it's a
plain library function decoupled from the CLI (`on_result` is just a
callback — swapping "print to stdout" for "write to Postgres" or "publish
to a queue" doesn't touch the crawl logic at all), so the CLI in `cli.py`
is genuinely one of several possible front ends, not baked into the core.

Other things a production, multi-domain version would need that a
single-domain command-line exercise doesn't: a persistent frontier (so a
crash doesn't lose all progress — the in-memory queue here does), metrics
(pages/sec per domain, error rates, queue depth — this version's
stderr summary is a start, not an observability story), and revisit
scheduling (a news site's homepage wants re-crawling far more often than
a static About page).

## Limitations & what I'd refine with more time

- **No `robots.txt` or rate-limiting.** Reasonable for a single-domain
  exercise against a site you have permission to crawl; not reasonable
  at any larger scale, and the first thing I'd add given more time.
- **Broken-pipe handling keeps crawling in the background** rather than
  cancelling the frontier immediately (see the bug writeup above) — a
  small amount of wasted work in the one scenario where output closes
  early, traded for a fix I could implement and test correctly under
  time pressure rather than a more elaborate cancellation-race version.
- **Only `<a href>` is followed/reported**, not `<img src>`,
  `<link href>`, or `<script src>`. Easy to extend — `parser.py`'s
  `extract_links()` would just take an additional CSS selector list —
  but asset discovery is a different feature from page discovery, and
  the brief asks specifically about pages and the URLs on them.
- **No JavaScript rendering**, which the brief itself rules out via "no
  Playwright." Worth stating plainly: a heavily client-side-rendered
  site will show a real crawler few or no links, since there's nothing
  in the initial HTML to find. A production version aimed at the modern
  web would need to reckon with this trade-off explicitly.
- **No depth limit**, only a total page-count cap (`--max-pages`). A
  `--max-depth` flag would need the frontier to track depth per URL
  (a small change — `(url, depth)` tuples instead of bare URLs) but
  didn't seem essential for a single-domain exercise.
- **The in-memory `seen` set and queue are the whole state.** Fine at
  the scale of one site's crawl; wouldn't scale past what fits in one
  process's memory, and a crash loses all progress with nothing to
  resume from. See "Extending beyond a CLI" above.
- **No duplicate-*content*  detection** — two different URLs serving
  identical content (a common real-world case: session IDs or tracking
  params in the query string) are currently crawled as separate pages.
  Content hashing to skip near-duplicates is a reasonable addition,
  scoped out here for time.

## Tooling and AI-assistant workflow
**Editor:** VS Code, day to day.

**AI assistant:** This implementation, its tests, and this README were
produced through Claude (in Cowork mode — Anthropic's AI coding
assistant), working in its own sandboxed development environment (a
Linux shell, Python, `uv`, `pytest`, `ruff`, `mypy`) under my direction,
rather than by hand-typing every line myself. Concretely, what that meant
in practice:

- I set the constraints and shape up front — concurrency-focused
  single-domain crawler, no Scrapy/Playwright, `uv` for tooling, a
  production-style repo layout — and Claude proposed and implemented the
  architecture (the worker-pool/queue concurrency model, the
  `aiohttp`/`selectolax` library choices, the URL-handling and testing
  approach) within those constraints.
- The implementation was verified by actually running it, not just
  generated and assumed correct: the full test suite, linter, and type
  checker all ran and had to pass; the CLI was exercised for real against
  a local demo site with a genuine before/after concurrency benchmark
  (the 39.3 → 351.6 pages/s numbers above); and running it that way
  surfaced two real bugs during development — the trailing-slash
  URL-normalization bug and the broken-pipe cascading-failure bug, both
  described in detail above — that were found, diagnosed, and fixed with
  regression tests added, not just patched over.
- I reviewed the resulting design decisions and trade-offs as they were
  made rather than treating the output as a black box — the
  documentation throughout this README (including the parts describing
  *why* something was chosen over an alternative) reflects reasoning I
  went through and agreed with, not an unexamined AI-generated narrative.

I'm including this level of detail deliberately, given the brief's own
"you work AI-first ... arrive curious, experiment fast" framing — this
*is* what that looks like in practice for me: directing the tool
precisely, then verifying its output with real tests and real runs rather
than trusting it blind.

## Project structure

```
zego-crawler/
├── src/crawler/
│   ├── cli.py          # argparse CLI, text/JSON output
│   ├── crawler.py       # worker-pool orchestration, CrawlResult/CrawlStats
│   ├── fetcher.py       # aiohttp wrapper: retries, content-type handling
│   ├── parser.py        # selectolax-based link extraction
│   └── urlutils.py      # normalization, resolution, domain matching
├── tests/
│   ├── conftest.py       # aiohttp.test_utils-based test server fixture
│   ├── test_urlutils.py
│   ├── test_parser.py
│   ├── test_fetcher.py
│   └── test_crawler.py
├── examples/
│   └── demo_site.py      # local ~40-page site for trying the crawler out
├── .github/workflows/ci.yml
├── pyproject.toml
└── README.md
```

## License

MIT — see [`LICENSE`](LICENSE).
