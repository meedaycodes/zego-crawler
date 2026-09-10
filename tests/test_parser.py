from crawler.parser import extract_links


def test_extracts_and_resolves_relative_links() -> None:
    html = '<html><body><a href="/about">About</a><a href="contact">Contact</a></body></html>'
    links = extract_links("https://example.com/", html)
    assert links == ["https://example.com/about", "https://example.com/contact"]


def test_extracts_absolute_links() -> None:
    html = '<a href="https://other.com/page">Other</a>'
    assert extract_links("https://example.com/", html) == ["https://other.com/page"]


def test_ignores_anchors_with_no_href() -> None:
    html = '<a name="top">No href</a><a href="/real">Real</a>'
    assert extract_links("https://example.com/", html) == ["https://example.com/real"]


def test_ignores_empty_and_fragment_only_hrefs() -> None:
    html = '<a href="">Empty</a><a href="#top">Anchor</a><a href="/ok">OK</a>'
    assert extract_links("https://example.com/", html) == ["https://example.com/ok"]


def test_includes_non_http_uris() -> None:
    html = '<a href="mailto:hi@example.com">Email</a>'
    assert extract_links("https://example.com/", html) == ["mailto:hi@example.com"]


def test_preserves_duplicates_and_order() -> None:
    html = '<a href="/a">1</a><a href="/b">2</a><a href="/a">3</a>'
    assert extract_links("https://example.com/", html) == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/a",
    ]


def test_ignores_non_anchor_link_bearing_tags() -> None:
    # <img>/<script>/<link> are asset references, not page links -- see the
    # parser module's docstring for the reasoning.
    html = (
        '<img src="/logo.png">'
        '<script src="/app.js"></script>'
        '<link rel="stylesheet" href="/style.css">'
        '<a href="/page">Page</a>'
    )
    assert extract_links("https://example.com/", html) == ["https://example.com/page"]


def test_handles_malformed_html_gracefully() -> None:
    # selectolax's underlying parser (Modest/Lexbor) is designed to recover
    # from malformed markup the way browsers do, rather than raising.
    html = '<html><body><a href="/a">Unclosed<div><a href="/b">Nested'
    links = extract_links("https://example.com/", html)
    assert "https://example.com/a" in links
    assert "https://example.com/b" in links


def test_no_links_on_page_without_anchors() -> None:
    assert extract_links("https://example.com/", "<html><body>No links here.</body></html>") == []
