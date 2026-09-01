"""Tests for the Firecrawl SearchProvider adapter (prism.research.firecrawl).

Everything runs against an injected fake async JSON client: no test in this
module ever touches the network.  The fakes pin the exact wire contract
(POST /v2/search and POST /v2/map, Bearer authorization header, explicit
request-body fields) and the security invariants (API-key isolation,
whitelist/SSRF re-validation of every returned URL, deterministic output).
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.config import PrismConfig, SourceConfig
from prism.research import SearchProvider, SearchQuery, ResearchWindow
from prism.research.firecrawl import (
    DEFAULT_BASE_URL,
    FIRECRAWL_API_KEY_ENV,
    MAP_CANDIDATE_TYPE,
    FirecrawlBlockedError,
    FirecrawlError,
    FirecrawlHttpError,
    FirecrawlJsonError,
    FirecrawlSchemaError,
    FirecrawlSearchProvider,
    FirecrawlTimeoutError,
    FirecrawlTransportError,
    JsonHttpResponse,
)
from prism.sources import FailureKind, SourceItem

UTC = timezone.utc
FETCHED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
WHITELIST = ("gov.example", "news.example")
KEY = "fc-test-key-123"
SEARCH_URL = DEFAULT_BASE_URL + "/v2/search"
MAP_URL = DEFAULT_BASE_URL + "/v2/map"


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch):
    """Keep a developer's real FIRECRAWL_API_KEY out of these tests."""
    monkeypatch.delenv(FIRECRAWL_API_KEY_ENV, raising=False)


class FakeJsonClient:
    """Records every POST and replays one canned JsonHttpResponse."""

    def __init__(self, response=None, *, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def post(self, url, *, headers, json_body, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": dict(json_body),
                "timeout": timeout,
            }
        )
        if self._error is not None:
            raise self._error
        return self._response


def ok(payload, *, url=None, status=200):
    return JsonHttpResponse(
        url=url or SEARCH_URL, status=status, body=json.dumps(payload)
    )


def entry(url, *, title=None, description=None, markdown=None, published=None):
    item = {"url": url}
    if title is not None:
        item["title"] = title
    if description is not None:
        item["description"] = description
    if markdown is not None:
        item["markdown"] = markdown
    if published is not None:
        item["publishedDate"] = published
    return item


def make_provider(client, *, whitelist=WHITELIST, api_key=KEY, **overrides):
    config = PrismConfig(sources=SourceConfig(whitelist=whitelist))
    values = {"client": client, "api_key": api_key, "clock": lambda: FETCHED}
    values.update(overrides)
    return FirecrawlSearchProvider(config, **values)


def window():
    return ResearchWindow(
        phase="publication",
        start_at=datetime(2025, 6, 1, tzinfo=UTC),
        end_at=datetime(2025, 9, 1, tzinfo=UTC),
        focus="Find the original publication.",
    )


def make_query(
    *,
    text="data retention policy",
    domains=("gov.example", "news.example"),
    types=("policy_document", "news"),
):
    return SearchQuery(
        query=text,
        window=window(),
        source_types=types,
        source_domains=domains,
        reason="Locate the original text.",
    )


# --------------------------------------------------------------------------
# Construction, protocol seam, and API-key isolation
# --------------------------------------------------------------------------


def test_provider_satisfies_search_provider_protocol():
    provider = make_provider(FakeJsonClient(ok({"success": True, "data": []})))
    assert provider.name == "firecrawl"
    assert isinstance(provider, SearchProvider)


def test_api_key_lives_only_in_the_authorization_header():
    client = FakeJsonClient(ok({"success": True, "data": []}))
    provider = make_provider(client)
    asyncio.run(provider.search(make_query()))
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["headers"] == {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
    }
    assert KEY not in json.dumps(call["body"])
    assert KEY not in repr(provider)


def test_api_key_may_come_from_environment(monkeypatch):
    monkeypatch.setenv(FIRECRAWL_API_KEY_ENV, "env-sourced-key")
    client = FakeJsonClient(ok({"success": True, "data": []}))
    provider = make_provider(client, api_key=None)
    asyncio.run(provider.search(make_query()))
    assert client.calls[0]["headers"]["Authorization"] == "Bearer env-sourced-key"


def test_constructor_api_key_wins_over_environment(monkeypatch):
    monkeypatch.setenv(FIRECRAWL_API_KEY_ENV, "env-sourced-key")
    client = FakeJsonClient(ok({"success": True, "data": []}))
    provider = make_provider(client, api_key="ctor-key")
    asyncio.run(provider.search(make_query()))
    assert client.calls[0]["headers"]["Authorization"] == "Bearer ctor-key"


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ValueError, match=FIRECRAWL_API_KEY_ENV):
        make_provider(FakeJsonClient(), api_key=None)
    with pytest.raises(ValueError, match=FIRECRAWL_API_KEY_ENV):
        make_provider(FakeJsonClient(), api_key="   ")


def test_constructor_rejects_bad_arguments():
    client = FakeJsonClient()
    config = PrismConfig(sources=SourceConfig(whitelist=WHITELIST))
    with pytest.raises(TypeError, match="config"):
        FirecrawlSearchProvider({"sources": WHITELIST}, client=client, api_key=KEY)
    with pytest.raises(TypeError, match="client"):
        FirecrawlSearchProvider(config, client=object(), api_key=KEY)
    with pytest.raises(TypeError, match="api_key"):
        FirecrawlSearchProvider(config, client=client, api_key=123)
    with pytest.raises(TypeError, match="clock"):
        FirecrawlSearchProvider(config, client=client, api_key=KEY, clock="now")


@pytest.mark.parametrize("limit", [0, -1, 101, True, "5", 3.5])
def test_constructor_rejects_bad_limits(limit):
    with pytest.raises((ValueError, TypeError)):
        make_provider(FakeJsonClient(), limit=limit)


@pytest.mark.parametrize("base_url", ["", "   ", "ftp://firecrawl.example", True])
def test_constructor_rejects_bad_base_urls(base_url):
    with pytest.raises((ValueError, TypeError)):
        make_provider(FakeJsonClient(), base_url=base_url)


def test_base_url_trailing_slashes_are_normalized():
    provider = make_provider(FakeJsonClient(), base_url="https://firecrawl.example/")
    assert provider.search_endpoint == "https://firecrawl.example/v2/search"
    assert provider.map_endpoint == "https://firecrawl.example/v2/map"
    assert make_provider(FakeJsonClient()).search_endpoint == SEARCH_URL


# --------------------------------------------------------------------------
# Search: wire contract and result mapping
# --------------------------------------------------------------------------


def test_search_posts_the_explicit_v2_search_contract():
    client = FakeJsonClient(ok({"success": True, "data": []}))
    provider = make_provider(client)
    asyncio.run(provider.search(make_query(), timeout=4.5))
    call = client.calls[0]
    assert call["url"] == SEARCH_URL
    assert call["timeout"] == 4.5
    assert call["body"] == {
        "query": "data retention policy",
        "limit": 10,
        "tbs": "cdr:1,cd_min:06/01/2025,cd_max:09/01/2025",
        "includeDomains": ["gov.example", "news.example"],
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
    }


def test_search_maps_results_onto_source_items():
    payload = {
        "success": True,
        "data": [
            entry(
                "https://gov.example/policy/a",
                title="Policy A",
                description="Agency published policy A.",
                markdown="# Policy A\nFull text.",
                published="2025-06-01T08:30:00Z",
            )
        ],
    }
    client = FakeJsonClient(ok(payload))
    items = asyncio.run(make_provider(client).search(make_query(types=("policy_document",))))
    assert items == (
        SourceItem(
            title="Policy A",
            source="gov.example",
            fetched_at=FETCHED,
            link="https://gov.example/policy/a",
            published_at=datetime(2025, 6, 1, 8, 30, tzinfo=UTC),
            summary="Agency published policy A.",
            content="# Policy A\nFull text.",
            type="policy_document",
        ),
    )


def test_search_falls_back_to_url_title_and_keeps_fields_optional():
    payload = {"success": True, "data": [entry("https://gov.example/bare")]}
    client = FakeJsonClient(ok(payload))
    (item,) = asyncio.run(make_provider(client).search(make_query()))
    assert item.title == "https://gov.example/bare"
    assert item.summary is None
    assert item.content is None
    assert item.published_at is None


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("2025-06-01T08:30:00+02:00", datetime(2025, 6, 1, 6, 30, tzinfo=UTC)),
        ("not-a-date", None),
        ("2025-06-01T00:00:00", None),  # naive timestamps are never trusted
        ("2030-01-01T00:00:00Z", None),  # future dates are dropped, not invented
    ],
)
def test_search_published_date_parsing(published, expected):
    payload = {
        "success": True,
        "data": [entry("https://gov.example/a", published=published)],
    }
    client = FakeJsonClient(ok(payload))
    (item,) = asyncio.run(make_provider(client).search(make_query()))
    if expected is None:
        assert item.published_at is None
    else:
        assert item.published_at == expected


# --------------------------------------------------------------------------
# Search: whitelist scope, SSRF policy, and early scope exit
# --------------------------------------------------------------------------


def test_search_filters_every_result_through_scope_and_ssrf_policy():
    payload = {
        "success": True,
        "data": [
            entry("https://gov.example/keep"),
            entry("https://news.example/out-of-query-scope"),
            entry("https://papers.example/out-of-whitelist"),
            entry("http://localhost:8080/admin"),
            entry("http://10.0.0.5/internal"),
            entry("https://gov.example.evil.io/lookalike"),
            entry("javascript:alert(1)"),
            entry("https://docs.gov.example/subdomain"),
        ],
    }
    client = FakeJsonClient(ok(payload))
    items = asyncio.run(make_provider(client).search(make_query(domains=("gov.example",))))
    assert [item.link for item in items] == ["https://gov.example/keep"]
    assert all(item.source == "gov.example" for item in items)


def test_search_returns_empty_without_calling_when_scope_misses_whitelist():
    client = FakeJsonClient(ok({"success": True, "data": []}))
    items = asyncio.run(make_provider(client).search(make_query(domains=("papers.example",))))
    assert items == ()
    assert client.calls == []


def test_provider_json_error_does_not_retain_raw_response_body_in_cause():
    client = FakeJsonClient(
        JsonHttpResponse(url=SEARCH_URL, status=200, body='{not-valid-json}')
    )
    provider = make_provider(client)

    with pytest.raises(FirecrawlJsonError) as info:
        asyncio.run(provider.search(make_query()))

    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_search_accepts_official_v2_web_response_array():
    payload = {
        "success": True,
        "web": [entry("https://gov.example/official", markdown="official result")],
    }
    client = FakeJsonClient(ok(payload))

    items = asyncio.run(make_provider(client).search(make_query()))

    assert [item.link for item in items] == ["https://gov.example/official"]
    assert items[0].content == "official result"


# --------------------------------------------------------------------------
# Search: dedup, limit, and deterministic ordering
# --------------------------------------------------------------------------


def test_search_dedups_normalized_links_keeping_first_payload():
    payload = {
        "success": True,
        "data": [
            entry("https://gov.example/a", markdown="first"),
            entry("https://gov.example/a/", markdown="second"),
            entry("HTTPS://GOV.EXAMPLE/a#frag", markdown="third"),
            entry("https://gov.example/b", markdown="other"),
        ],
    }
    client = FakeJsonClient(ok(payload))
    items = asyncio.run(make_provider(client).search(make_query()))
    assert len(items) == 2
    kept = items[0] if items[0].link == "https://gov.example/a" else items[1]
    assert kept.content == "first"
    assert kept.link == "https://gov.example/a"


def test_search_truncates_to_limit_by_rank_then_sorts_deterministically():
    payload = {
        "success": True,
        "data": [
            entry("https://gov.example/c"),
            entry("https://gov.example/a"),
            entry("https://gov.example/e"),
            entry("https://gov.example/b"),
            entry("https://gov.example/d"),
        ],
    }
    client = FakeJsonClient(ok(payload))
    items = asyncio.run(make_provider(client, limit=3).search(make_query()))
    assert [item.link for item in items] == [
        "https://gov.example/a",
        "https://gov.example/c",
        "https://gov.example/e",
    ]


def test_search_output_is_deterministic_across_calls_and_input_orders():
    forward = {"success": True, "data": [entry("https://gov.example/a"), entry("https://gov.example/b")]}
    reversed_payload = {
        "success": True,
        "data": [entry("https://gov.example/b"), entry("https://gov.example/a")],
    }
    client = FakeJsonClient(ok(forward))
    provider = make_provider(client)
    assert asyncio.run(provider.search(make_query())) == asyncio.run(
        provider.search(make_query())
    )
    other = asyncio.run(make_provider(FakeJsonClient(ok(reversed_payload))).search(make_query()))
    assert other == asyncio.run(make_provider(FakeJsonClient(ok(forward))).search(make_query()))


# --------------------------------------------------------------------------
# Search: failure classification
# --------------------------------------------------------------------------


def test_search_classifies_http_status_errors():
    client = FakeJsonClient(ok({"error": "payment required"}, status=402))
    with pytest.raises(FirecrawlHttpError, match="402") as exc_info:
        asyncio.run(make_provider(client).search(make_query()))
    assert exc_info.value.status == 402
    assert exc_info.value.kind is FailureKind.HTTP_STATUS
    assert isinstance(exc_info.value, FirecrawlError)
    assert KEY not in str(exc_info.value)


def test_search_classifies_invalid_json_errors():
    client = FakeJsonClient(JsonHttpResponse(url=SEARCH_URL, status=200, body="{oops"))
    with pytest.raises(FirecrawlJsonError) as exc_info:
        asyncio.run(make_provider(client).search(make_query()))
    assert exc_info.value.kind is FailureKind.PARSE


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"success": True},
        {"success": True, "data": {"oops": 1}},
        {"success": False, "data": []},
        {"success": True, "data": ["not-an-object"]},
        {"success": True, "data": [{"title": "no url"}]},
        {"success": True, "data": [{"url": 5}]},
        {"success": True, "data": [{"url": "   "}]},
        {"success": True, "data": [{"url": "https://gov.example/a", "description": 7}]},
    ],
)
def test_search_classifies_schema_errors(payload):
    client = FakeJsonClient(ok(payload))
    with pytest.raises(FirecrawlSchemaError) as exc_info:
        asyncio.run(make_provider(client).search(make_query()))
    assert exc_info.value.kind is FailureKind.PARSE


def test_search_blocks_off_host_response_without_reflecting_api_key_in_final_url():
    client = FakeJsonClient(
        ok({"success": True, "data": []}, url=f"https://evil.example/v2/search?token={KEY}")
    )
    with pytest.raises(FirecrawlBlockedError) as exc_info:
        asyncio.run(make_provider(client).search(make_query()))
    assert exc_info.value.kind is FailureKind.BLOCKED
    assert KEY not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_search_redacts_api_key_from_wrapped_client_errors():
    client = FakeJsonClient(error=ValueError(f"socket exploded with {KEY}"))
    with pytest.raises(FirecrawlTransportError) as exc_info:
        asyncio.run(make_provider(client).search(make_query()))
    assert exc_info.value.kind is FailureKind.TRANSPORT
    assert KEY not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_search_classifies_timeouts():
    client = FakeJsonClient(error=TimeoutError("too slow"))
    with pytest.raises(FirecrawlTimeoutError) as exc_info:
        asyncio.run(make_provider(client).search(make_query(), timeout=2.0))
    assert exc_info.value.kind is FailureKind.TIMEOUT


def test_search_rejects_clients_that_break_the_response_contract():
    client = FakeJsonClient(response=object())
    with pytest.raises(FirecrawlTransportError, match="JsonHttpResponse"):
        asyncio.run(make_provider(client).search(make_query()))


def test_search_rejects_bad_query_and_timeout_arguments():
    provider = make_provider(FakeJsonClient(ok({"success": True, "data": []})))
    with pytest.raises(TypeError, match="SearchQuery"):
        asyncio.run(provider.search("data retention policy"))
    for timeout in (0, -1, "5", True):
        with pytest.raises((ValueError, TypeError)):
            asyncio.run(provider.search(make_query(), timeout=timeout))


def test_search_requires_an_aware_clock():
    provider = make_provider(
        FakeJsonClient(ok({"success": True, "data": [entry("https://gov.example/a")]})),
        clock=lambda: datetime(2026, 9, 1),
    )
    with pytest.raises(RuntimeError, match="timezone-aware"):
        asyncio.run(provider.search(make_query()))


def test_client_transport_error_does_not_retain_secret_as_exception_cause():
    secret = "fc-test-key-123"
    client = FakeJsonClient(error=ValueError(f"socket exploded with {secret}"))
    provider = make_provider(client, api_key=secret)

    with pytest.raises(FirecrawlTransportError) as info:
        asyncio.run(provider.search(make_query()))

    assert secret not in str(info.value)
    assert info.value.__cause__ is None


# --------------------------------------------------------------------------
# Map: candidate URLs only, never fetched content
# --------------------------------------------------------------------------


def test_map_posts_the_explicit_v2_map_contract_and_filters_links():
    payload = {
        "success": True,
        "links": [
            "https://gov.example/docs/a",
            "https://gov.example/docs/a/",
            "https://news.example/foreign",
            "https://evil.example/hostile",
            "https://gov.example:443/docs/b",
        ],
    }
    client = FakeJsonClient(ok(payload, url=MAP_URL))
    items = asyncio.run(make_provider(client).map_site("https://gov.example/"))
    call = client.calls[0]
    assert call["url"] == MAP_URL
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["body"] == {
        "url": "https://gov.example/",
        "limit": 10,
        "includeSubdomains": False,
    }
    assert [item.link for item in items] == [
        "https://gov.example/docs/a",
        "https://gov.example:443/docs/b",
    ]
    for item in items:
        assert item.source == "gov.example"
        assert item.type == MAP_CANDIDATE_TYPE
        assert item.title == item.link
        assert item.content is None
        assert item.summary is None
        assert item.published_at is None
        assert item.fetched_at == FETCHED


def test_map_honors_limit_and_dedups_before_truncating():
    payload = {
        "success": True,
        "links": [
            "https://gov.example/c",
            "https://gov.example/a",
            "https://gov.example/a/",
            "https://gov.example/b",
        ],
    }
    client = FakeJsonClient(ok(payload, url=MAP_URL))
    items = asyncio.run(make_provider(client).map_site("https://gov.example/", limit=2))
    assert client.calls[0]["body"]["limit"] == 2
    assert [item.link for item in items] == [
        "https://gov.example/a",
        "https://gov.example/c",
    ]


def test_map_requires_a_whitelisted_target_url():
    client = FakeJsonClient(ok({"success": True, "links": []}, url=MAP_URL))
    with pytest.raises(FirecrawlBlockedError) as exc_info:
        asyncio.run(make_provider(client).map_site("https://evil.example/"))
    assert exc_info.value.kind is FailureKind.BLOCKED
    with pytest.raises(FirecrawlBlockedError):
        asyncio.run(make_provider(client).map_site("http://169.254.169.254/latest/meta-data"))
    with pytest.raises(TypeError, match="url"):
        asyncio.run(make_provider(client).map_site(123))
    assert client.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"success": True, "links": "https://gov.example/"},
        {"success": False, "links": []},
        {"success": True, "links": [5]},
    ],
)
def test_map_classifies_schema_errors(payload):
    client = FakeJsonClient(ok(payload, url=MAP_URL))
    with pytest.raises(FirecrawlSchemaError):
        asyncio.run(make_provider(client).map_site("https://gov.example/"))


def test_map_shares_the_search_failure_taxonomy():
    with pytest.raises(FirecrawlHttpError):
        asyncio.run(make_provider(FakeJsonClient(ok({}, url=MAP_URL, status=500))).map_site("https://gov.example/"))
    with pytest.raises(FirecrawlJsonError):
        asyncio.run(
            make_provider(
                FakeJsonClient(JsonHttpResponse(url=MAP_URL, status=200, body="nope"))
            ).map_site("https://gov.example/")
        )


# --------------------------------------------------------------------------
# Package-level exports
# --------------------------------------------------------------------------


def test_firecrawl_names_are_exported_from_prism_research():
    import prism.research

    assert prism.research.FirecrawlSearchProvider is FirecrawlSearchProvider
    assert prism.research.FirecrawlError is FirecrawlError
    assert prism.research.JsonHttpResponse is JsonHttpResponse
