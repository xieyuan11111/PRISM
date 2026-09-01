"""Tests for the stdlib JSON HTTP client (prism.research.firecrawl_http).

``FirecrawlJsonHttpClient`` is the production transport behind
``FirecrawlSearchProvider``: real ``urllib.request`` POSTs, offloaded with
``asyncio.to_thread``.  Every test here runs offline against injected fakes
and a canned-response urllib handler — no test ever touches the network or a
real Firecrawl endpoint.  The suite pins the wire contract (stdlib ``Request``,
UTF-8 JSON body, exactly the caller's headers) and the security invariants:
no client-added Authorization, redirects refused instead of followed, a
configurable response byte ceiling, Content-Type-aware decoding, and API-key
redaction with no leakage through exception text or the cause/context chain.
"""

import asyncio
import email.message
import io
import json
import threading
import time
import urllib.error
import urllib.request
import urllib.response
from datetime import datetime, timezone

import pytest

from prism.config import PrismConfig, SourceConfig
from prism.research import ResearchWindow, SearchQuery
from prism.research.firecrawl import (
    FIRECRAWL_API_KEY_ENV,
    FirecrawlSearchProvider,
    FirecrawlTimeoutError,
    JsonHttpResponse,
)
from prism.research.firecrawl_http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    FirecrawlHttpClientError,
    FirecrawlHttpRedirectError,
    FirecrawlHttpResponseTooLargeError,
    FirecrawlHttpTimeoutError,
    FirecrawlHttpTransportError,
    FirecrawlHttpUnicodeError,
    FirecrawlHttpUrlError,
    FirecrawlJsonHttpClient,
    FirecrawlNoRedirectHandler,
)

UTC = timezone.utc
FETCHED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
KEY = "fc-secret-key-456"
ENDPOINT = "https://api.firecrawl.dev/v2/search"
HTTP_ENDPOINT = "http://api.firecrawl.example/v2/search"
AUTH_HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
BODY = {"query": "数据保留政策", "limit": 5}


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch):
    """Keep a developer's real FIRECRAWL_API_KEY out of these tests."""
    monkeypatch.delenv(FIRECRAWL_API_KEY_ENV, raising=False)


class FakeResponse:
    """Minimal urllib-response stand-in that records close()."""

    def __init__(self, *, url=ENDPOINT, status=200, body=b"", headers=None):
        self._body = body
        self.url = url
        self.status = status
        self.code = status
        self.msg = "OK"
        self.closed = False
        self.headers = dict(headers or {})

    def read(self, amount=-1):
        if amount is None or amount < 0:
            return self._body
        return self._body[:amount]

    def close(self):
        self.closed = True

    def geturl(self):
        return self.url

    def info(self):
        return self.headers


class FakeOpener:
    """Records the Request and replays one canned outcome."""

    def __init__(self, result=None, *, error=None, delay=0.0):
        self._result = result
        self._error = error
        self._delay = delay
        self.calls = []
        self.thread_ids = []

    def open(self, request, timeout=None):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(
            {"request": request, "timeout": timeout, "at": time.perf_counter()}
        )
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


class TrackedBytesIO(io.BytesIO):
    """BytesIO that records that close() ran."""

    def __init__(self, *args):
        super().__init__(*args)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class ServingHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Offline transport answering from one canned response.

    Subclassing both ``HTTPHandler`` and ``HTTPSHandler`` keeps
    ``build_opener`` from installing the real network transport for either
    scheme, while the response still flows through urllib's genuine
    processor and error-dispatch chain (``HTTPErrorProcessor``, redirect
    handling, ``HTTPError`` for non-2xx).
    """

    def __init__(self, *, status=200, body=b"", content_type="application/json",
                 location=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.location = location
        self.requests = []
        self.bodies = []

    def http_open(self, req):
        return self._serve(req)

    def https_open(self, req):
        return self._serve(req)

    def _serve(self, req):
        self.requests.append(req)
        fp = TrackedBytesIO(self.body)
        self.bodies.append(fp)
        headers = email.message.Message()
        if self.content_type is not None:
            headers["Content-Type"] = self.content_type
        if self.location is not None:
            headers["Location"] = self.location
        response = urllib.response.addinfourl(fp, headers, req.full_url, self.status)
        response.msg = "OK"
        return response


def serving_opener(handler):
    """Build a real urllib opener that serves canned data, never sockets."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), handler, FirecrawlNoRedirectHandler()
    )


def make_client(opener=None, **overrides):
    kwargs = {"max_response_bytes": 1024}
    if opener is not None:
        kwargs["opener"] = opener
    kwargs.update(overrides)
    return FirecrawlJsonHttpClient(**kwargs)


def run_post(client, *, url=ENDPOINT, headers=AUTH_HEADERS, body=BODY, timeout=6.0):
    return asyncio.run(client.post(url, headers=headers, json_body=body, timeout=timeout))


def assert_no_key_leak(error, key=KEY):
    """No exception in the cause/context chain exposes the key."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert key not in str(current)
        assert key not in repr(current)
        pending.append(current.__cause__)
        pending.append(current.__context__)


def window():
    return ResearchWindow(
        phase="publication",
        start_at=datetime(2025, 6, 1, tzinfo=UTC),
        end_at=datetime(2025, 9, 1, tzinfo=UTC),
        focus="Find the original publication.",
    )


def make_query():
    return SearchQuery(
        query="数据保留政策",
        window=window(),
        source_types=("policy_document",),
        source_domains=("gov.example",),
        reason="Locate the original text.",
    )


def make_provider(client):
    config = PrismConfig(sources=SourceConfig(whitelist=("gov.example",)))
    return FirecrawlSearchProvider(
        config, client=client, api_key=KEY, clock=lambda: FETCHED
    )


# --------------------------------------------------------------------------
# Construction and injection seams
# --------------------------------------------------------------------------


def test_constructor_validates_injection_arguments():
    for limit in (0, -1, True, "5", 1.5):
        with pytest.raises((TypeError, ValueError)):
            FirecrawlJsonHttpClient(max_response_bytes=limit)
    with pytest.raises(ValueError, match="not both"):
        FirecrawlJsonHttpClient(
            opener=FakeOpener(), redirect_handler=FirecrawlNoRedirectHandler()
        )
    with pytest.raises(TypeError, match="opener"):
        FirecrawlJsonHttpClient(opener=object())
    client = FirecrawlJsonHttpClient()
    assert (
        repr(client)
        == f"FirecrawlJsonHttpClient(max_response_bytes={DEFAULT_MAX_RESPONSE_BYTES})"
    )
    assert KEY not in repr(client)


def test_default_response_ceiling_is_multi_megabyte():
    assert DEFAULT_MAX_RESPONSE_BYTES == 4 * 1024 * 1024


# --------------------------------------------------------------------------
# Wire contract: stdlib Request, UTF-8 JSON, exactly the caller's headers
# --------------------------------------------------------------------------


def test_post_builds_a_stdlib_post_request_with_a_utf8_json_body():
    response = FakeResponse(
        body=b'{"success": true}', headers={"Content-Type": "application/json"}
    )
    opener = FakeOpener(result=response)
    result = run_post(make_client(opener), timeout=4.5)

    (call,) = opener.calls
    request = call["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == ENDPOINT
    assert request.get_method() == "POST"
    assert request.data == json.dumps(BODY, ensure_ascii=False).encode("utf-8")
    assert request.data.decode("utf-8") == '{"query": "数据保留政策", "limit": 5}'
    assert call["timeout"] == 4.5
    assert result == JsonHttpResponse(url=ENDPOINT, status=200, body='{"success": true}')
    assert response.closed


def test_post_forwards_exactly_the_caller_headers_and_adds_nothing():
    opener = FakeOpener(result=FakeResponse(body=b"{}"))
    run_post(
        make_client(opener),
        headers={"Content-Type": "application/json", "X-PRISM-Test": "1"},
    )
    request = opener.calls[0]["request"]
    # urllib capitalizes header keys ("Content-Type" -> "Content-type")
    assert set(request.headers) == {"Content-type", "X-prism-test"}
    assert not request.has_header("Authorization")
    assert request.get_header("Authorization") is None

    opener = FakeOpener(result=FakeResponse(body=b"{}"))
    run_post(make_client(opener), headers=AUTH_HEADERS)
    request = opener.calls[0]["request"]
    assert set(request.headers) == {"Authorization", "Content-type"}
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://firecrawl.example/v2/search",
        "file:///etc/passwd",
        "http:///missing-host",
        "gopher://firecrawl.example",
        "https://[::1",
    ],
)
def test_post_accepts_only_http_and_https_endpoints(url):
    opener = FakeOpener()
    with pytest.raises(FirecrawlHttpUrlError):
        run_post(make_client(opener), url=url)
    assert opener.calls == []


def test_post_rejects_bad_arguments():
    opener = FakeOpener(result=FakeResponse())
    client = make_client(opener)
    with pytest.raises(TypeError, match="url"):
        asyncio.run(client.post(123, headers=AUTH_HEADERS, json_body=BODY, timeout=5))
    with pytest.raises(TypeError, match="headers"):
        asyncio.run(client.post(ENDPOINT, headers=[("a", "b")], json_body=BODY, timeout=5))
    with pytest.raises(TypeError, match="json_body"):
        asyncio.run(client.post(ENDPOINT, headers=AUTH_HEADERS, json_body="query", timeout=5))
    for timeout in (0, -1, "5", True, None):
        with pytest.raises((TypeError, ValueError)):
            asyncio.run(client.post(ENDPOINT, headers=AUTH_HEADERS, json_body=BODY, timeout=timeout))
    assert opener.calls == []


# --------------------------------------------------------------------------
# Event-loop safety: the blocking request runs on a worker thread
# --------------------------------------------------------------------------


def test_post_offloads_the_request_so_the_event_loop_keeps_running():
    opener = FakeOpener(
        result=FakeResponse(body=b'{"success": true}'), delay=0.3
    )
    client = make_client(opener)

    async def scenario():
        loop_thread = threading.get_ident()
        ticks = []

        async def heartbeat():
            for _ in range(8):
                await asyncio.sleep(0.02)
                ticks.append(time.perf_counter())

        heartbeat_task = asyncio.create_task(heartbeat())
        started_at = time.perf_counter()
        await client.post(ENDPOINT, headers=AUTH_HEADERS, json_body=BODY, timeout=5.0)
        returned_at = time.perf_counter()
        await heartbeat_task
        return loop_thread, ticks, started_at, returned_at

    loop_thread, ticks, started_at, returned_at = asyncio.run(scenario())

    assert opener.thread_ids[0] != loop_thread  # ran off the loop thread
    assert returned_at - started_at >= 0.25  # the blocking sleep really happened
    assert any(started_at + 0.05 < tick < returned_at - 0.05 for tick in ticks)


# --------------------------------------------------------------------------
# Failure taxonomy: timeouts, transport errors, and key redaction
# --------------------------------------------------------------------------


def test_post_maps_timeout_failures_onto_timeouterror():
    assert issubclass(FirecrawlHttpTimeoutError, TimeoutError)
    assert issubclass(FirecrawlHttpTimeoutError, FirecrawlHttpClientError)
    opener = FakeOpener(error=TimeoutError(f"timed out near {KEY}"))
    with pytest.raises(FirecrawlHttpTimeoutError) as info:
        run_post(make_client(opener))
    assert info.value.__cause__ is None
    assert_no_key_leak(info.value)


def test_post_maps_urlerror_wrapped_timeouts_onto_timeouterror():
    opener = FakeOpener(error=urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(FirecrawlHttpTimeoutError):
        run_post(make_client(opener))


def test_post_redacts_the_authorization_key_from_transport_errors():
    opener = FakeOpener(
        error=urllib.error.URLError(RuntimeError(f"socket exploded near {KEY}"))
    )
    with pytest.raises(FirecrawlHttpTransportError) as info:
        run_post(make_client(opener))
    assert KEY not in str(info.value)
    assert "[redacted]" in str(info.value)
    assert info.value.__cause__ is None
    assert_no_key_leak(info.value)


# --------------------------------------------------------------------------
# HTTP status handling: non-2xx is data, redirects are refused
# --------------------------------------------------------------------------


def test_post_returns_non_2xx_responses_for_upper_layer_classification():
    serving = ServingHandler(status=402, body=b'{"error": "payment required"}')
    client = make_client(opener=serving_opener(serving))

    response = run_post(client, url=HTTP_ENDPOINT)

    assert response.status == 402
    assert response.url == HTTP_ENDPOINT
    assert response.body == '{"error": "payment required"}'
    assert serving.bodies[0].was_closed


def test_http_error_response_failures_do_not_retain_exception_context_with_headers():
    serving = ServingHandler(status=302, body=b"moved", location=f"https://evil.example/?token={KEY}")
    client = make_client(opener=serving_opener(serving))

    with pytest.raises(FirecrawlHttpRedirectError) as info:
        run_post(client, url=HTTP_ENDPOINT)

    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert_no_key_leak(info.value)


def test_post_refuses_redirects_instead_of_following_them():
    serving = ServingHandler(
        status=302, body=b"moved", location="https://evil.example/steal"
    )
    client = make_client(opener=serving_opener(serving))

    with pytest.raises(FirecrawlHttpRedirectError) as info:
        run_post(client, url=HTTP_ENDPOINT)

    assert "302" in str(info.value)
    assert "evil.example" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert_no_key_leak(info.value)
    # the Bearer key reached only the one legitimate request, never the target
    assert len(serving.requests) == 1
    assert serving.requests[0].get_header("Authorization") == f"Bearer {KEY}"
    assert serving.bodies[0].was_closed


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_no_redirect_handler_refuses_every_redirect_family(code):
    handler = FirecrawlNoRedirectHandler()
    request = urllib.request.Request(HTTP_ENDPOINT, method="POST")
    fp = TrackedBytesIO(b"redirect body")
    with pytest.raises(FirecrawlHttpRedirectError) as info:
        handler.redirect_request(
            request, fp, code, "Redirected", {"location": "https://evil.example/x"},
            "https://evil.example/x",
        )
    assert str(code) in str(info.value)
    assert "evil.example" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert_no_key_leak(info.value)
    assert fp.was_closed


def test_http_error_redirect_statuses_are_refused_too():
    fp = TrackedBytesIO(b"")
    error = urllib.error.HTTPError(
        HTTP_ENDPOINT, 302, "Found", {"location": "https://evil.example/x"}, fp
    )
    opener = FakeOpener(error=error)
    with pytest.raises(FirecrawlHttpRedirectError):
        run_post(make_client(opener), url=HTTP_ENDPOINT)
    assert fp.was_closed


def test_custom_redirect_handlers_can_be_injected(monkeypatch):
    served = ServingHandler(status=302, location="https://evil.example/next")
    monkeypatch.setattr(
        urllib.request.HTTPHandler,
        "http_open",
        lambda self, req: served.http_open(req),
    )
    seen = []

    class Spy(FirecrawlNoRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            seen.append((code, newurl))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    client = FirecrawlJsonHttpClient(redirect_handler=Spy(), max_response_bytes=1024)
    with pytest.raises(FirecrawlHttpRedirectError):
        run_post(client, url=HTTP_ENDPOINT)
    assert seen == [(302, "https://evil.example/next")]


# --------------------------------------------------------------------------
# Response handling: byte ceiling, charset, and decoding failures
# --------------------------------------------------------------------------


def test_post_enforces_the_configured_response_byte_limit():
    response = FakeResponse(body=b"x" * 64)
    opener = FakeOpener(result=response)
    client = make_client(opener, max_response_bytes=32)
    with pytest.raises(FirecrawlHttpResponseTooLargeError) as info:
        run_post(client)
    assert "32" in str(info.value)
    assert response.closed
    assert_no_key_leak(info.value)


@pytest.mark.parametrize(
    ("content_type", "raw", "expected"),
    [
        ("application/json; charset=utf-8", "标题数据".encode("utf-8"), "标题数据"),
        ("application/json", "标题数据".encode("utf-8"), "标题数据"),
        (None, "plain ascii".encode("utf-8"), "plain ascii"),
        ("application/json; charset=iso-8859-1", b"caf\xe9", "café"),
        ('application/json; charset="utf-8"', "引…用".encode("utf-8"), "引…用"),
    ],
)
def test_post_decodes_bodies_with_the_advertised_charset(content_type, raw, expected):
    headers = {} if content_type is None else {"Content-Type": content_type}
    opener = FakeOpener(result=FakeResponse(body=raw, headers=headers))
    assert run_post(make_client(opener)).body == expected


@pytest.mark.parametrize(
    ("content_type", "raw"),
    [
        ("application/json; charset=utf-8", b'{"a": "\xff\xfe\xfa"}'),
        ("application/json; charset=x-unknown-charset", b"anything"),
    ],
)
def test_post_reports_undecodable_bodies_without_leaking_them(content_type, raw):
    response = FakeResponse(body=raw, headers={"Content-Type": content_type})
    opener = FakeOpener(result=response)
    with pytest.raises(FirecrawlHttpUnicodeError) as info:
        run_post(make_client(opener))
    assert content_type.split("=", 1)[-1].strip('"') in str(info.value)
    assert response.closed
    assert_no_key_leak(info.value)


# --------------------------------------------------------------------------
# Provider integration: the client satisfies the JsonClient protocol offline
# --------------------------------------------------------------------------


def test_client_plugs_into_the_search_provider_end_to_end():
    payload = {
        "success": True,
        "data": [
            {
                "url": "https://gov.example/policy/a",
                "title": "Policy A",
                "markdown": "Full text.",
                "publishedDate": "2025-06-01T08:30:00Z",
            }
        ],
    }
    serving = ServingHandler(body=json.dumps(payload).encode("utf-8"))
    provider = make_provider(
        FirecrawlJsonHttpClient(opener=serving_opener(serving), max_response_bytes=65536)
    )

    items = asyncio.run(provider.search(make_query()))

    assert [item.link for item in items] == ["https://gov.example/policy/a"]
    assert items[0].content == "Full text."
    assert items[0].published_at == datetime(2025, 6, 1, 8, 30, tzinfo=UTC)
    assert serving.requests[0].get_header("Authorization") == f"Bearer {KEY}"


def test_provider_classifies_client_timeouts_as_timeout_failures():
    client = make_client(FakeOpener(error=TimeoutError("timed out")))
    with pytest.raises(FirecrawlTimeoutError):
        asyncio.run(make_provider(client).search(make_query()))


# --------------------------------------------------------------------------
# Package-level exports
# --------------------------------------------------------------------------


def test_firecrawl_http_names_are_exported_from_prism_research():
    import prism.research

    assert prism.research.FirecrawlJsonHttpClient is FirecrawlJsonHttpClient
    assert prism.research.FirecrawlNoRedirectHandler is FirecrawlNoRedirectHandler
    assert prism.research.FirecrawlHttpClientError is FirecrawlHttpClientError
    assert prism.research.FirecrawlHttpTimeoutError is FirecrawlHttpTimeoutError
