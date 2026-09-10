from crawler.urlutils import hostname, is_crawlable, is_same_domain, normalize_url, resolve_any


class TestNormalizeUrl:
    def test_strips_fragment(self) -> None:
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_lowercases_scheme_and_host_only(self) -> None:
        assert (
            normalize_url("HTTPS://Example.COM/Path?Query=Value")
            == "https://example.com/Path?Query=Value"
        )

    def test_preserves_trailing_slash_distinction(self) -> None:
        # Deliberate: "/about" and "/about/" are NOT folded together, since
        # this value also becomes the literal URL that gets fetched.
        assert normalize_url("https://example.com/about/") == "https://example.com/about/"
        assert normalize_url("https://example.com/about") == "https://example.com/about"

    def test_preserves_query_string(self) -> None:
        assert (
            normalize_url("https://example.com/search?q=widgets")
            == "https://example.com/search?q=widgets"
        )

    def test_idempotent(self) -> None:
        url = "https://example.com/a/b?x=1"
        assert normalize_url(normalize_url(url)) == normalize_url(url)


class TestResolveAny:
    def test_resolves_relative_path(self) -> None:
        assert resolve_any("https://example.com/a/", "../b") == "https://example.com/b"

    def test_resolves_root_relative_path(self) -> None:
        assert resolve_any("https://example.com/a/b/", "/c") == "https://example.com/c"

    def test_passes_through_absolute_url(self) -> None:
        assert resolve_any("https://example.com/", "https://other.com/x") == "https://other.com/x"

    def test_keeps_non_http_schemes(self) -> None:
        # mailto:/tel: are real URLs the page contains and should be
        # reported, even though the crawler can't fetch them.
        assert resolve_any("https://example.com/", "mailto:hi@example.com") == (
            "mailto:hi@example.com"
        )
        assert resolve_any("https://example.com/", "tel:+441234567890") == "tel:+441234567890"

    def test_rejects_empty_href(self) -> None:
        assert resolve_any("https://example.com/", "") is None
        assert resolve_any("https://example.com/", "   ") is None

    def test_rejects_pure_fragment(self) -> None:
        assert resolve_any("https://example.com/page", "#top") is None

    def test_resolves_relative_with_fragment(self) -> None:
        # A relative link that also has a fragment IS a distinct URL to a
        # different page (fragment doesn't get stripped here -- that's
        # normalize_url's job, applied later by the crawler).
        assert resolve_any("https://example.com/a", "/b#section") == "https://example.com/b#section"


class TestIsCrawlable:
    def test_http_and_https_are_crawlable(self) -> None:
        assert is_crawlable("http://example.com/") is True
        assert is_crawlable("https://example.com/") is True

    def test_other_schemes_are_not(self) -> None:
        assert is_crawlable("mailto:hi@example.com") is False
        assert is_crawlable("tel:+441234567890") is False
        assert is_crawlable("javascript:void(0)") is False
        assert is_crawlable("ftp://files.example.com/report.pdf") is False


class TestHostname:
    def test_extracts_lowercased_host(self) -> None:
        assert hostname("https://Example.COM/path") == "example.com"

    def test_no_port_in_result(self) -> None:
        assert hostname("https://example.com:8080/path") == "example.com"

    def test_empty_for_unparseable(self) -> None:
        assert hostname("mailto:hi@example.com") == ""


class TestIsSameDomain:
    def test_exact_match(self) -> None:
        assert is_same_domain("https://example.com/x", "example.com") is True

    def test_subdomain_is_not_same_domain(self) -> None:
        assert is_same_domain("https://blog.example.com/x", "example.com") is False

    def test_www_is_not_same_domain(self) -> None:
        # A real trade-off, not an oversight -- see the README.
        assert is_same_domain("https://www.example.com/x", "example.com") is False

    def test_different_domain(self) -> None:
        assert is_same_domain("https://other.com/x", "example.com") is False
