"""Transport contract for the PRISM sources layer.

The collection layer never performs I/O itself: it depends on an injectable,
async ``HttpGetter`` so tests run entirely against fakes and production code
can plug in any transport (stdlib ``urllib`` in a thread executor, aiohttp,
...) without touching this module.  The contract deliberately exposes no way
to attach credentials, cookies, or custom headers — PRISM only collects
publicly accessible material and never bypasses access controls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP GET outcome as seen by the sources layer.

    ``url`` is the *final* URL after any redirects; the service re-validates
    it against the source whitelist so a redirect cannot smuggle the request
    to an unapproved host.
    """

    url: str
    status: int
    body: str
    content_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")
        if isinstance(self.status, bool) or not isinstance(self.status, int):
            raise TypeError("status must be an integer")
        if not isinstance(self.body, str):
            raise TypeError("body must be a string")
        if self.content_type is not None and (
            not isinstance(self.content_type, str) or not self.content_type.strip()
        ):
            raise ValueError("content_type must be a non-empty string or None")


class HttpGetterError(RuntimeError):
    """Base class for the standard-library public HTTP getter."""


class HttpGetterResponseTooLargeError(HttpGetterError):
    """A public response exceeded the configured byte ceiling."""


class HttpGetterNoRedirectHandler(HTTPRedirectHandler):
    """Never follow a redirect; SourceService owns URL policy."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibHttpGetter:
    """Async, dependency-free GET transport for explicitly enabled research.

    Redirects are refused rather than followed.  The source service can then
    classify the response without allowing a public URL to make an unchecked
    request to another host.
    """

    def __init__(
        self,
        *,
        opener: Any | None = None,
        max_response_bytes: int = 4 * 1024 * 1024,
        user_agent: str = "PRISM/0.1 (+public-evidence-collector)",
    ) -> None:
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise TypeError("opener must provide open()")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise TypeError("max_response_bytes must be an integer")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least 1")
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise ValueError("user_agent must be a non-empty string")
        self._opener = opener or build_opener(HttpGetterNoRedirectHandler())
        self._max_response_bytes = max_response_bytes
        self._user_agent = user_agent.strip()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(max_response_bytes={self._max_response_bytes}, "
            "credentials=none)"
        )

    async def get(self, url: str, *, timeout: float) -> HttpResponse:
        """Fetch one public URL without credentials or automatic redirects."""
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        request = Request(
            url.strip(),
            method="GET",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self._user_agent,
            },
        )
        return await asyncio.to_thread(self._get_sync, request, float(timeout))

    def _get_sync(self, request: Request, timeout: float) -> HttpResponse:
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            try:
                status = int(error.code)
                error_url = str(error.geturl() or request.full_url)
                return HttpResponse(error_url, status, "")
            finally:
                if getattr(error, "fp", None) is not None:
                    error.fp.close()

        try:
            body = self._read_body(response)
            charset = None
            headers = getattr(response, "headers", None)
            if headers is not None:
                getter = getattr(headers, "get_content_charset", None)
                if callable(getter):
                    charset = getter()
                content_type = headers.get("Content-Type")
            else:
                content_type = None
            encoding = charset or "utf-8"
            try:
                text = body.decode(encoding)
            except (LookupError, UnicodeDecodeError) as error:
                raise HttpGetterError(
                    f"response from {request.full_url} is not decodable as {encoding}"
                ) from error
            return HttpResponse(
                str(getattr(response, "url", None) or request.full_url),
                int(getattr(response, "status", 200)),
                text,
                content_type,
            )
        finally:
            response.close()

    def _read_body(self, response: Any) -> bytes:
        raw = response.read(self._max_response_bytes + 1)
        if not isinstance(raw, bytes):
            raise HttpGetterError("HTTP response body must be bytes")
        if len(raw) > self._max_response_bytes:
            raise HttpGetterResponseTooLargeError(
                f"response exceeded {self._max_response_bytes}-byte limit"
            )
        return raw


class HttpGetter(Protocol):
    """Async HTTP GET transport injected into ``SourceService``.

    Implementations must not follow redirects to hosts the caller has not
    approved (the service re-checks ``HttpResponse.url``), must honor the
    ``timeout`` in seconds, and must never attach authentication material.
    """

    async def get(self, url: str, *, timeout: float) -> HttpResponse: ...
