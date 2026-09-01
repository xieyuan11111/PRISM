"""Tests for PRISM's dependency-free public GET transport."""

from __future__ import annotations

import asyncio
from email.message import Message
from urllib.error import HTTPError

import pytest

from prism.sources import (
    HttpGetterNoRedirectHandler,
    HttpGetterResponseTooLargeError,
    UrllibHttpGetter,
)


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, body=b"<html>ok</html>", *, url="https://example.gov/a", status=200):
        self.body = body
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        self.closed = False
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


def test_no_redirect_handler_never_builds_a_followup_request():
    handler = HttpGetterNoRedirectHandler()

    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.test") is None
def test_urllib_getter_fetches_without_credentials_and_closes_response():
    response = FakeResponse()
    opener = FakeOpener(response)
    getter = UrllibHttpGetter(opener=opener)

    result = run(getter.get("https://example.gov/a", timeout=2.5))

    assert result.url == "https://example.gov/a"
    assert result.status == 200
    assert result.body == "<html>ok</html>"
    assert response.closed is True
    request, timeout = opener.requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") is None
    assert timeout == 2.5


def test_urllib_getter_enforces_response_byte_limit_and_closes_response():
    response = FakeResponse(b"12345")
    getter = UrllibHttpGetter(opener=FakeOpener(response), max_response_bytes=4)

    with pytest.raises(HttpGetterResponseTooLargeError):
        run(getter.get("https://example.gov/a", timeout=1))

    assert response.closed is True
    assert response.read_sizes == [5]


def test_urllib_getter_returns_http_error_status_without_reading_error_body():
    error_body = FakeResponse(b"secret body")
    error = HTTPError(
        "https://example.gov/a", 404, "Not Found", Message(), error_body
    )
    class ErrorOpener:
        def open(self, request, timeout):
            raise error

    result = run(UrllibHttpGetter(opener=ErrorOpener()).get("https://example.gov/a", timeout=1))

    assert result.status == 404
    assert result.body == ""
    assert error_body.closed is True
