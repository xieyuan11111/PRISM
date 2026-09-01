"""Focused tests for the public sources collection layer (module: sources).

All HTTP traffic is faked: every test injects a ``FakeGetter`` that returns
canned ``HttpResponse`` objects or raises canned exceptions, so the suite
never touches the real network.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig, PrismConfig, SourceConfig
from prism.ingestion import IngestionService
from prism.sources import (
    FailureKind,
    FetchBatch,
    FetchFailure,
    FetchResult,
    FeedFetcher,
    HttpGetter,
    HttpResponse,
    PageFetcher,
    SourceFetchError,
    SourceFetcher,
    SourceItem,
    SourceService,
    normalize_url,
)


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

FEED_URL = "https://example.gov/feeds/news.xml"
ATOM_URL = "https://example.org/feeds/analysis.xml"
PAGE_URL = "https://example.gov/announcements/policy.html"
WHITELIST = ("example.gov", "example.org")

RSS_BODY = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Gov News</title>
    <link>https://example.gov/news</link>
    <description>Official news feed</description>
    <item>
      <title>Housing policy updated</title>
      <link>https://example.gov/news/housing-policy-updated</link>
      <pubDate>Mon, 24 Aug 2026 08:00:00 +0000</pubDate>
      <description>The housing policy was revised.</description>
    </item>
    <item>
      <title>Ministry answers questions</title>
      <link>https://example.gov/news/ministry-answers</link>
      <guid>https://example.gov/news/ministry-answers</guid>
      <pubDate>Tue, 25 Aug 2026 09:30:00 +0000</pubDate>
      <description>Answers to press questions.</description>
      <source url="https://example.gov/ministry">Ministry Desk</source>
    </item>
  </channel>
</rss>
"""

ATOM_BODY = """\
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Org Analysis</title>
  <updated>2026-08-30T10:00:00Z</updated>
  <entry>
    <title>Debate on consumption theory</title>
    <link rel="alternate" href="https://example.org/papers/consumption-debate"/>
    <published>2026-08-26T07:15:30Z</published>
    <summary>Two camps re-read the evidence.</summary>
    <content type="html">&lt;p&gt;Full debate text.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>Relative link entry</title>
    <link href="/papers/relative-entry"/>
    <published>2026-08-27T07:15:30Z</published>
    <summary>Relative link.</summary>
  </entry>
</feed>
"""

PAGE_BODY = """\
<!DOCTYPE html>
<html>
<head>
  <title>Policy announcement page</title>
  <meta name="description" content="Official announcement details">
</head>
<body>
  <nav>menu home contact</nav>
  <article><p>The ministry announced the new measures today.</p><p>Details follow.</p></article>
  <script>trackingCode();</script>
</body>
</html>
"""


def ok(url: str, body: str) -> HttpResponse:
    return HttpResponse(url=url, status=200, body=body)


class FakeGetter:
    """Injectable async HTTP transport with per-URL canned outcomes."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.calls: list[tuple[str, float]] = []

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.calls.append((url, timeout))
        outcome = self.routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def make_service(
    getter: FakeGetter,
    *,
    whitelist: tuple[str, ...] = WHITELIST,
    clock: object = lambda: NOW,
    timeout: float = 5.0,
    **kwargs: object,
) -> SourceService:
    config = PrismConfig(sources=SourceConfig(whitelist=whitelist))
    return SourceService(
        config, getter=getter, clock=clock, timeout=timeout, **kwargs  # type: ignore[arg-type]
    )


# --- RSS / Atom parsing -----------------------------------------------------


def test_fetch_feed_parses_rss_items() -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(FEED_URL, kind="feed"))

    assert isinstance(result, FetchResult)
    assert result.url == FEED_URL
    assert result.fetched_at == NOW
    assert result.duplicate_keys == ()
    assert getter.calls == [(FEED_URL, 5.0)]
    assert [item.title for item in result.items] == [
        "Housing policy updated",
        "Ministry answers questions",
    ]
    first, second = result.items
    assert first.link == "https://example.gov/news/housing-policy-updated"
    assert first.published_at == PUBLISHED
    assert first.published_at.tzinfo is not None
    assert first.summary == "The housing policy was revised."
    assert first.source == "example.gov"
    assert first.fetched_at == NOW
    assert first.fetched_at.tzinfo is not None
    assert second.source == "Ministry Desk"  # RSS <source> element honored


def test_fetch_feed_parses_atom_entries() -> None:
    getter = FakeGetter({ATOM_URL: ok(ATOM_URL, ATOM_BODY)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(ATOM_URL, kind="feed"))

    first, second = result.items
    assert first.title == "Debate on consumption theory"
    assert first.link == "https://example.org/papers/consumption-debate"
    assert first.published_at == datetime(2026, 8, 26, 7, 15, 30, tzinfo=timezone.utc)
    assert first.summary == "Two camps re-read the evidence."
    assert first.content == "<p>Full debate text.</p>"
    assert first.source == "example.org"
    assert second.link == "https://example.org/papers/relative-entry"


def test_auto_detection_routes_xml_to_feed_and_html_to_page() -> None:
    getter = FakeGetter(
        {
            FEED_URL: ok(FEED_URL, RSS_BODY),
            PAGE_URL: ok(PAGE_URL, PAGE_BODY),
        }
    )
    service = make_service(getter)

    feed_result = asyncio.run(service.fetch(FEED_URL))
    page_result = asyncio.run(service.fetch(PAGE_URL))

    assert len(feed_result.items) == 2
    assert feed_result.items[0].link.startswith("https://example.gov/news/")
    assert len(page_result.items) == 1
    assert page_result.items[0].title == "Policy announcement page"


def test_entries_without_dates_or_with_future_dates_are_not_fabricated() -> None:
    body = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Gov News</title>
  <item>
    <title>Undated entry</title>
    <link>https://example.gov/news/undated</link>
    <description>No pubDate here.</description>
  </item>
  <item>
    <title>Future entry</title>
    <link>https://example.gov/news/future</link>
    <pubDate>Tue, 01 Sep 2026 23:00:00 +0000</pubDate>
    <description>Dated after the fetch clock.</description>
  </item>
</channel></rss>
"""
    getter = FakeGetter({FEED_URL: ok(FEED_URL, body)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(FEED_URL, kind="feed"))

    undated, future = result.items
    assert undated.published_at is None
    assert future.published_at is None
    metadata = future.to_ingestion_metadata()
    assert metadata["published_at"] == NOW  # falls back to fetch time, never invented


def test_feed_fetcher_parses_without_any_http() -> None:
    items = FeedFetcher().parse(RSS_BODY, url=FEED_URL, fetched_at=NOW)

    assert len(items) == 2
    assert items[0].title == "Housing policy updated"


# --- public web pages -------------------------------------------------------


def test_fetch_page_extracts_title_meta_and_visible_text() -> None:
    getter = FakeGetter({PAGE_URL: ok(PAGE_URL, PAGE_BODY)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(PAGE_URL, kind="page"))

    (item,) = result.items
    assert item.title == "Policy announcement page"
    assert item.link == PAGE_URL
    assert item.source == "example.gov"
    assert item.published_at is None
    assert item.summary == "Official announcement details"
    assert "The ministry announced the new measures today." in item.content
    assert "Details follow." in item.content
    assert "trackingCode" not in item.content
    assert item.fetched_at == NOW


def test_empty_page_body_is_rejected_not_stored_as_empty_content() -> None:
    getter = FakeGetter({PAGE_URL: ok(PAGE_URL, "   ")})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(PAGE_URL, kind="page"))

    assert excinfo.value.kind is FailureKind.PARSE


# --- whitelist & SSRF protection --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.gov/feeds/news.xml",          # non-http(s) scheme
        "file:///etc/passwd",                        # non-http(s) scheme
        "https://user:pass@example.gov/feeds/news.xml",  # embedded credentials
        "https://user@example.gov/feeds/news.xml",   # embedded user
        "http://localhost/feeds/news.xml",           # localhost name
        "http://127.0.0.1/feeds/news.xml",           # IPv4 loopback
        "http://[::1]/feeds/news.xml",               # IPv6 loopback
        "http://10.0.0.5/feeds/news.xml",            # private IPv4
        "http://172.16.31.5/feeds/news.xml",         # private IPv4
        "http://192.168.1.5/feeds/news.xml",         # private IPv4
        "http://169.254.169.254/latest/meta-data",   # link-local cloud metadata
        "http://[fe80::1]/feeds/news.xml",           # link-local IPv6
        "http://[fd12::1]/feeds/news.xml",           # unique-local IPv6
        "https://other.example.net/feeds/news.xml",  # not whitelisted
        "https://example.gov.evil.net/feeds/news.xml",  # suffix look-alike
        "https:///feeds/news.xml",                   # missing host
        "https://example.gov\\@attacker.net/feed",   # backslash userinfo trick
        "https://example.gov/feed\n",                # control character
    ],
)
def test_blocked_urls_never_reach_the_getter(url: str) -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(url))

    assert excinfo.value.kind is FailureKind.BLOCKED
    assert getter.calls == []


def test_private_ip_is_blocked_even_when_whitelisted() -> None:
    getter = FakeGetter()
    service = make_service(getter, whitelist=("example.gov", "10.0.0.5"))

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch("http://10.0.0.5/feeds/news.xml"))

    assert excinfo.value.kind is FailureKind.BLOCKED


def test_whitelist_matches_the_host_exactly_regardless_of_case() -> None:
    url = "https://EXAMPLE.gov/Feeds/news.xml"
    getter = FakeGetter({url: ok(url, RSS_BODY)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(url))

    assert len(result.items) == 2


def test_final_url_after_redirect_is_revalidated() -> None:
    getter = FakeGetter(
        {FEED_URL: HttpResponse(url="http://127.0.0.1/redirected", status=200, body=RSS_BODY)}
    )
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL))

    assert excinfo.value.kind is FailureKind.BLOCKED
    assert "127.0.0.1" in excinfo.value.detail


def test_validate_url_is_available_for_pre_checks() -> None:
    service = make_service(FakeGetter())

    assert service.validate_url("https://example.gov/x") == "https://example.gov/x"

    with pytest.raises(TypeError):
        service.validate_url(123)  # type: ignore[arg-type]
    with pytest.raises(SourceFetchError):
        service.validate_url("")


# --- failure classification ---------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_error_statuses_are_classified(status: int) -> None:
    getter = FakeGetter({FEED_URL: HttpResponse(url=FEED_URL, status=status, body="")})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL, kind="feed"))

    assert excinfo.value.kind is FailureKind.HTTP_STATUS
    assert excinfo.value.url == FEED_URL
    assert str(status) in excinfo.value.detail


@pytest.mark.parametrize("status", [401, 403, 407])
def test_access_control_responses_are_never_bypassed(status: int) -> None:
    getter = FakeGetter({PAGE_URL: HttpResponse(url=PAGE_URL, status=status, body="")})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(PAGE_URL))

    assert excinfo.value.kind is FailureKind.HTTP_STATUS
    assert "never bypass" in excinfo.value.detail


def test_timeout_errors_are_classified() -> None:
    getter = FakeGetter({FEED_URL: TimeoutError("too slow")})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL))

    assert excinfo.value.kind is FailureKind.TIMEOUT


def test_transport_errors_are_classified() -> None:
    getter = FakeGetter({FEED_URL: OSError("connection reset")})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL))

    assert excinfo.value.kind is FailureKind.TRANSPORT


def test_getter_contract_violations_are_transport_errors() -> None:
    getter = FakeGetter({FEED_URL: "not-a-response"})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL))

    assert excinfo.value.kind is FailureKind.TRANSPORT
    assert "HttpResponse" in excinfo.value.detail


@pytest.mark.parametrize(
    "body",
    [
        "this is not xml at all <",
        '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>',
        "<unexpected><root/></unexpected>",
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<rss version=\"2.0\"><channel><title>x</title><item><title>&xxe;</title>"
        "<link>https://example.gov/a</link></item></channel></rss>",
    ],
)
def test_malformed_or_dangerous_feeds_are_parse_errors(body: str) -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, body)})
    service = make_service(getter)

    with pytest.raises(SourceFetchError) as excinfo:
        asyncio.run(service.fetch(FEED_URL, kind="feed"))

    assert excinfo.value.kind is FailureKind.PARSE


# --- timezone-aware fetch time -----------------------------------------------


def test_default_clock_produces_timezone_aware_fetch_times() -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    config = PrismConfig(sources=SourceConfig(whitelist=WHITELIST))
    service = SourceService(config, getter=getter)

    result = asyncio.run(service.fetch(FEED_URL, kind="feed"))

    assert result.fetched_at.tzinfo is not None
    assert result.fetched_at.utcoffset() is not None
    assert all(item.fetched_at.tzinfo is not None for item in result.items)


def test_naive_clock_is_rejected() -> None:
    getter = FakeGetter({FEED_URL: ok(FEED_URL, RSS_BODY)})
    service = make_service(getter, clock=lambda: datetime(2026, 9, 1, 12, 0))

    with pytest.raises(RuntimeError, match="timezone-aware"):
        asyncio.run(service.fetch(FEED_URL, kind="feed"))


# --- deduplication -----------------------------------------------------------


def test_normalize_url_is_stable() -> None:
    assert (
        normalize_url(" https://Example.gov:443/news/a/#frag ")
        == "https://example.gov/news/a"
    )
    assert normalize_url("http://example.gov:80/") == "http://example.gov/"
    assert normalize_url("https://example.gov/a?x=1") == "https://example.gov/a?x=1"


def test_dedup_key_prefers_link_then_content_hash() -> None:
    by_link = SourceItem(
        title="A", source="example.gov", fetched_at=NOW, link="https://example.gov/x"
    )
    same_link_other_title = SourceItem(
        title="B", source="example.gov", fetched_at=NOW, link="https://example.gov/x"
    )
    same_target_variant = SourceItem(
        title="C", source="example.gov", fetched_at=NOW, link="https://EXAMPLE.gov/x#frag"
    )
    by_content = SourceItem(
        title="D", source="example.org", fetched_at=NOW, content="Alpha body"
    )
    same_content_other_spacing = SourceItem(
        title="E", source="example.org", fetched_at=NOW, content="Alpha\n body"
    )
    other_content = SourceItem(
        title="F", source="example.org", fetched_at=NOW, content="Beta body"
    )

    assert by_link.dedup_key == same_link_other_title.dedup_key
    assert by_link.dedup_key == same_target_variant.dedup_key
    assert by_link.dedup_key.startswith("link:")
    assert by_content.dedup_key == same_content_other_spacing.dedup_key
    assert by_content.dedup_key != other_content.dedup_key
    assert by_content.dedup_key.startswith("content:")


def test_fetch_dedupes_within_and_across_fetches() -> None:
    body = RSS_BODY.replace(
        "</channel>",
        "    <item>\n"
        "      <title>Duplicate link entry</title>\n"
        "      <link>https://example.gov/news/housing-policy-updated</link>\n"
        "      <pubDate>Wed, 26 Aug 2026 08:00:00 +0000</pubDate>\n"
        "      <description>Same link as the first entry.</description>\n"
        "    </item>\n"
        "</channel>",
    )
    getter = FakeGetter({FEED_URL: ok(FEED_URL, body)})
    service = make_service(getter)

    first = asyncio.run(service.fetch(FEED_URL, kind="feed"))
    assert len(first.items) == 2  # in-feed duplicate link collapsed
    assert len(first.duplicate_keys) == 1
    assert first.duplicate_keys[0].startswith("link:")

    second = asyncio.run(service.fetch(FEED_URL, kind="feed"))
    assert second.items == ()
    assert len(second.duplicate_keys) == 3  # every parsed entry was already seen

    service.reset_dedup()
    third = asyncio.run(service.fetch(FEED_URL, kind="feed"))
    assert len(third.items) == 2
    assert service.seen_keys == frozenset(third.duplicate_keys) | {
        item.dedup_key for item in third.items
    }


def test_linkless_items_with_identical_content_are_deduped() -> None:
    body = """\
<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Gov News</title>
  <item>
    <title>First</title>
    <description>Identical advisory text.</description>
  </item>
  <item>
    <title>Second</title>
    <description>Identical advisory text.</description>
  </item>
</channel></rss>
"""
    getter = FakeGetter({FEED_URL: ok(FEED_URL, body)})
    service = make_service(getter)

    result = asyncio.run(service.fetch(FEED_URL, kind="feed"))

    (item,) = result.items
    assert item.link is None
    assert len(result.duplicate_keys) == 1
    assert result.duplicate_keys[0].startswith("content:")


# --- batches, plugins, and constructor validation -----------------------------


def test_fetch_all_collects_results_and_classified_failures() -> None:
    missing = "https://example.gov/feeds/missing.xml"
    blocked = "https://blocked.example.net/feed"
    getter = FakeGetter(
        {
            FEED_URL: ok(FEED_URL, RSS_BODY),
            missing: HttpResponse(url=missing, status=404, body=""),
        }
    )
    service = make_service(getter)

    batch = asyncio.run(service.fetch_all([FEED_URL, missing, blocked]))

    assert isinstance(batch, FetchBatch)
    assert [result.url for result in batch.results] == [FEED_URL]
    assert len(batch.results[0].items) == 2
    assert [(failure.url, failure.kind) for failure in batch.failures] == [
        (missing, FailureKind.HTTP_STATUS),
        (blocked, FailureKind.BLOCKED),
    ]
    assert isinstance(batch.failures[0], FetchFailure)
    assert batch.failures[0].detail


def test_custom_fetcher_kind_is_registrable() -> None:
    class ShoutFetcher:
        kind = "shout"

        def parse(self, body: str, *, url: str, fetched_at: datetime):
            return (
                SourceItem(title=body.upper(), source="example.gov", fetched_at=fetched_at, link=url),
            )

    getter = FakeGetter({FEED_URL: ok(FEED_URL, "quiet headline")})
    service = make_service(getter, fetchers=[ShoutFetcher()])

    result = asyncio.run(service.fetch(FEED_URL, kind="shout"))

    assert result.items[0].title == "QUIET HEADLINE"


def test_constructor_and_argument_validation() -> None:
    config = PrismConfig(sources=SourceConfig(whitelist=WHITELIST))
    getter = FakeGetter()

    with pytest.raises(TypeError):
        SourceService(config)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SourceService(config, getter=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceService(config, getter=getter, timeout=0)
    with pytest.raises(TypeError):
        SourceService(config, getter=getter, timeout="fast")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourceService(config, getter=getter, clock="not-callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SourceService({"sources": {}}, getter=getter)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceService(
            config, getter=getter, fetchers=[FeedFetcher(), FeedFetcher()]
        )

    service = SourceService(config, getter=getter)
    with pytest.raises(ValueError, match="unknown kind"):
        asyncio.run(service.fetch(FEED_URL, kind="bogus"))
    with pytest.raises(TypeError):
        asyncio.run(service.fetch(123))  # type: ignore[arg-type]


def test_public_contracts_are_importable() -> None:
    assert callable(SourceService)
    assert callable(FeedFetcher)
    assert callable(PageFetcher)
    assert FeedFetcher().kind == "feed"
    assert PageFetcher().kind == "page"
    assert HttpGetter is not None
    assert SourceFetcher is not None
    assert {kind.value for kind in FailureKind} == {
        "blocked",
        "http_status",
        "timeout",
        "parse",
        "transport",
    }


# --- SourceItem contract -------------------------------------------------------


def test_source_item_validation_and_immutability() -> None:
    with pytest.raises(ValueError):
        SourceItem(title="  ", source="example.gov", fetched_at=NOW)
    with pytest.raises(TypeError):
        SourceItem(title="t", source="example.gov", fetched_at=NOW, link=42)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceItem(
            title="t",
            source="example.gov",
            fetched_at=datetime(2026, 9, 1, 12, 0),  # naive
        )
    with pytest.raises(ValueError):
        SourceItem(
            title="t",
            source="example.gov",
            fetched_at=NOW,
            published_at=NOW.replace(year=2027),
        )

    blanked = SourceItem(title="t", source="example.gov", fetched_at=NOW, link="   ")
    assert blanked.link is None
    assert blanked.dedup_key.startswith("content:")

    item = SourceItem(
        title="t",
        source="example.gov",
        fetched_at=NOW,
        case_tags=["housing"],
    )
    assert item.case_tags == ("housing",)
    assert item.type == "news"
    with pytest.raises(FrozenInstanceError):
        item.title = "changed"  # type: ignore[misc]


def test_source_item_hands_off_to_ingestion_service(tmp_path: Path) -> None:
    published = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    item = SourceItem(
        title="Housing policy updated",
        source="example.gov",
        fetched_at=NOW,
        link="https://example.gov/news/housing-policy-updated",
        published_at=published,
        summary="The housing policy was revised.",
        content="The full body of the policy update.",
        type="policy",
        case_tags=["housing"],
    )

    metadata = item.to_ingestion_metadata()
    assert metadata == {
        "title": "Housing policy updated",
        "source": "example.gov",
        "published_at": published,
        "fetched_at": NOW,
        "type": "policy",
        "case_tags": ["housing"],
        "url": "https://example.gov/news/housing-policy-updated",
    }

    source_file = tmp_path / "item.md"
    source_file.write_text(item.content or "", encoding="utf-8")
    paths = PathConfig(
        data_dir=tmp_path / "data",
        raw_dir=Path("raw"),
        corpus_dir=Path("corpus"),
    )
    result = IngestionService(paths).ingest(source_file, metadata)

    assert result.material.title == item.title
    assert result.material.source == item.source
    assert result.material.url == item.link
    assert result.material.published_at == published
    assert result.material.fetched_at == NOW
    assert result.material.content == item.content
