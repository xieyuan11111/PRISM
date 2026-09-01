"""Real stdlib JSON HTTP client for the Firecrawl research seam.

``FirecrawlJsonHttpClient`` is the production implementation of the
:class:`~prism.research.firecrawl.JsonClient` protocol behind
:class:`~prism.research.firecrawl.FirecrawlSearchProvider`: it performs the
actual ``POST`` with the Python 3.11 standard library only —
``urllib.request`` offloaded through :func:`asyncio.to_thread` so the event
loop never blocks — and introduces no third-party dependency.

Security posture (mirrors the sources and LLM layers, REQUIREMENTS NFR-6 /
FR-1.15):

* only ``http``/``https`` endpoints with a host are accepted;
* the request body is UTF-8 JSON built from the caller's mapping, and headers
  are forwarded exactly as given — this client never attaches, rewrites, or
  stores an ``Authorization`` header of its own;
* redirects are never followed: the default handler chain installs
  :class:`FirecrawlNoRedirectHandler`, which closes the redirect body and
  raises :class:`FirecrawlHttpRedirectError` instead of re-sending, so a
  hostile 3xx can never forward the Bearer key to another host (an
  ``HTTPError`` carrying a 3xx status is refused the same way, whatever
  opener was injected); non-2xx responses are *returned as data* so the
  provider layer classifies them;
* the response body is read under a configurable byte ceiling and always
  closed, on every success and error path;
* decoding follows the charset advertised by ``Content-Type`` (UTF-8 by
  default), and failures are reported without echoing the body;
* every wrapped failure is scrubbed against the caller's ``Authorization``
  value and raised outside the handling ``except`` block, so neither the
  message nor the ``__cause__``/``__context__`` chain can carry the key.

The client stays ignorant of Firecrawl semantics — it moves bytes and
strings, while the adapter owns keys, endpoints, and the failure taxonomy.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .firecrawl import JsonHttpResponse

DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_REDACTED = "[redacted]"
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class FirecrawlHttpClientError(Exception):
    """Base class for Firecrawl JSON client transport failures."""


class FirecrawlHttpUrlError(FirecrawlHttpClientError):
    """The endpoint was not an http(s) URL with a host."""


class FirecrawlHttpRedirectError(FirecrawlHttpClientError):
    """A redirect response was refused instead of followed."""


class FirecrawlHttpResponseTooLargeError(FirecrawlHttpClientError):
    """The response body exceeded the configured byte ceiling."""


class FirecrawlHttpUnicodeError(FirecrawlHttpClientError):
    """The response body was not decodable with its advertised charset."""


class FirecrawlHttpTransportError(FirecrawlHttpClientError):
    """The request failed below the HTTP layer (DNS, connection, socket)."""


class FirecrawlHttpTimeoutError(FirecrawlHttpClientError, TimeoutError):
    """The request exceeded its timeout budget.

    Also a builtin :class:`TimeoutError` so
    :class:`~prism.research.firecrawl.FirecrawlSearchProvider` classifies it
    as a timeout rather than a generic transport failure.
    """


class FirecrawlNoRedirectHandler(HTTPRedirectHandler):
    """Redirect policy for Firecrawl requests: never follow, always fail.

    Any 3xx triggers :class:`FirecrawlHttpRedirectError` instead of a second
    request, so the caller's ``Authorization`` header (and its Bearer key)
    can never be replayed against the redirect target — same-host redirects
    are refused too, so the policy needs no per-host audit.  The redirect
    body is closed before raising so no socket leaks.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            close = getattr(fp, "close", None)
            if callable(close):
                close()
        raise FirecrawlHttpRedirectError(
            f"refusing to follow HTTP {code} redirect"
        )


def _check_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    return float(timeout)


def _final_url(response: Any, request: Request) -> str:
    geturl = getattr(response, "geturl", None)
    url = geturl() if callable(geturl) else None
    if not isinstance(url, str) or not url.strip():
        return request.full_url
    return url


def _status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise FirecrawlHttpTransportError("response reported no integer HTTP status")
    return status


def _headers_of(response: Any) -> Any:
    headers = getattr(response, "headers", None)
    if headers is not None:
        return headers
    info = getattr(response, "info", None)
    return info() if callable(info) else None


def _charset_of(headers: Any) -> str:
    """Read the charset advertised by Content-Type; UTF-8 when absent."""
    get = getattr(headers, "get", None)
    value = get("Content-Type") if callable(get) else None
    if not isinstance(value, str):
        return "utf-8"
    for parameter in value.split(";")[1:]:
        name, _, raw = parameter.strip().partition("=")
        if name.strip().lower() == "charset":
            charset = raw.strip().strip('"').strip("'")
            if charset:
                return charset
    return "utf-8"


class FirecrawlJsonHttpClient:
    """Stdlib-urllib transport satisfying the ``JsonClient`` protocol.

    ``opener`` (anything with an ``open(request, timeout=...)`` method, e.g.
    an ``OpenerDirector``) and ``redirect_handler`` (a urllib handler that
    replaces the default redirect policy — passing one is how a deployment
    customizes the no-follow posture) are the injection seams that keep tests
    offline.  The client never stores headers, so it holds no secrets and its
    ``repr`` cannot leak any.
    """

    def __init__(
        self,
        *,
        opener: Any = None,
        redirect_handler: Any = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if opener is not None and redirect_handler is not None:
            raise ValueError("pass opener or redirect_handler, not both")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise TypeError("max_response_bytes must be an integer")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise TypeError("opener must provide open()")
        if redirect_handler is not None and not hasattr(redirect_handler, "add_parent"):
            raise TypeError("redirect_handler must be a urllib request handler")
        if opener is not None:
            self._opener = opener
        else:
            self._opener = build_opener(
                redirect_handler if redirect_handler is not None
                else FirecrawlNoRedirectHandler()
            )
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}"
            f"(max_response_bytes={self._max_response_bytes})"
        )

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> JsonHttpResponse:
        """POST one UTF-8 JSON document without blocking the event loop."""
        request, seconds = self._build_request(
            url, headers=headers, json_body=json_body, timeout=timeout
        )
        return await asyncio.to_thread(self._post_sync, request, seconds)

    def _build_request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> tuple[Request, float]:
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        if not isinstance(headers, Mapping):
            raise TypeError("headers must be a mapping")
        if not isinstance(json_body, Mapping):
            raise TypeError("json_body must be a mapping")
        seconds = _check_timeout(timeout)
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            raise FirecrawlHttpUrlError(
                "url must be an http(s) URL with a host"
            ) from None
        if parts.scheme.lower() not in _ALLOWED_SCHEMES or not host:
            raise FirecrawlHttpUrlError("url must be an http(s) URL with a host")
        try:
            data = json.dumps(dict(json_body), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise FirecrawlHttpTransportError(
                f"json_body must be JSON serializable: {error}"
            ) from None
        return Request(url, data=data, headers=dict(headers), method="POST"), seconds

    def _post_sync(self, request: Request, timeout: float) -> JsonHttpResponse:
        """Run one POST in the calling (worker) thread without leaking the key."""
        failure: FirecrawlHttpClientError | None = None
        result: JsonHttpResponse | None = None
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            try:
                result = self._from_http_error(request, error)
            except FirecrawlHttpClientError as inner:
                # This handler runs inside HTTPError's except scope. Recreate
                # the safe message, then raise it after the scope so headers
                # carried by the original HTTPError cannot survive in context.
                failure = type(inner)(str(inner))
        except FirecrawlHttpClientError:
            raise
        except (TimeoutError, socket.timeout):
            failure = self._timeout_error(request, timeout)
        except URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                failure = self._timeout_error(request, timeout)
            else:
                failure = self._transport_error(request, error)
        except OSError as error:
            failure = self._transport_error(request, error)
        else:
            result = self._from_response(request, response)
        if failure is not None:
            # Raised outside every except clause so __context__ and __cause__
            # never retain the original exception or its response headers.
            raise failure
        if result is None:
            raise FirecrawlHttpTransportError("request completed without a response")
        return result

    def _from_http_error(
        self, request: Request, error: HTTPError
    ) -> JsonHttpResponse:
        """Turn a non-2xx HTTPError into response data for the caller to classify."""
        fp = error.fp
        redirect = 300 <= error.code < 400
        try:
            if redirect:
                # Even an injected opener must not smuggle a 3xx through as data.
                raise FirecrawlHttpRedirectError(
                    self._scrub(
                        request,
                        f"refusing to surface HTTP {error.code} redirect "
                        f"from {request.full_url} as a response",
                    )
                )
            raw = b"" if fp is None else self._read_limited(request, fp)
        finally:
            if fp is not None:
                close = getattr(fp, "close", None)
                if callable(close):
                    close()
        body = self._decode(request, raw, _charset_of(error.hdrs))
        return JsonHttpResponse(
            url=error.filename or request.full_url, status=error.code, body=body
        )

    def _from_response(self, request: Request, response: Any) -> JsonHttpResponse:
        url = _final_url(response, request)
        try:
            raw = self._read_limited(request, response)
            status = _status(response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        body = self._decode(request, raw, _charset_of(_headers_of(response)))
        return JsonHttpResponse(url=url, status=status, body=body)

    def _read_limited(self, request: Request, fp: Any) -> bytes:
        raw = fp.read(self._max_response_bytes + 1)
        if not isinstance(raw, bytes):
            raise FirecrawlHttpTransportError(
                f"response from {request.full_url} did not return bytes"
            )
        if len(raw) > self._max_response_bytes:
            raise FirecrawlHttpResponseTooLargeError(
                f"response from {request.full_url} exceeded the "
                f"{self._max_response_bytes}-byte limit"
            )
        return raw

    def _decode(self, request: Request, raw: bytes, charset: str) -> str:
        decode_failure: FirecrawlHttpUnicodeError | None = None
        try:
            return raw.decode(charset)
        except (UnicodeDecodeError, LookupError) as error:
            detail = "" if isinstance(error, LookupError) else f": {error}"
            decode_failure = FirecrawlHttpUnicodeError(
                self._scrub(
                    request,
                    f"response from {request.full_url} is not decodable "
                    f"as {charset}{detail}",
                )
            )
        raise decode_failure

    def _timeout_error(
        self, request: Request, timeout: float
    ) -> FirecrawlHttpTimeoutError:
        return FirecrawlHttpTimeoutError(
            self._scrub(
                request,
                f"POST to {request.full_url} timed out after {timeout:g}s",
            )
        )

    def _transport_error(
        self, request: Request, error: BaseException
    ) -> FirecrawlHttpTransportError:
        return FirecrawlHttpTransportError(
            self._scrub(request, f"POST to {request.full_url} failed: {error}")
        )

    def _scrub(self, request: Request, message: str) -> str:
        """Redact the caller's Authorization value from any error detail."""
        secret = request.get_header("Authorization")
        if not secret:
            return message
        redacted = message.replace(secret, _REDACTED)
        # Error text may echo the bare token without its auth-scheme prefix.
        scheme, separator, token = secret.strip().partition(" ")
        if separator and token:
            redacted = redacted.replace(token, _REDACTED)
        return redacted


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "FirecrawlHttpClientError",
    "FirecrawlHttpRedirectError",
    "FirecrawlHttpResponseTooLargeError",
    "FirecrawlHttpTimeoutError",
    "FirecrawlHttpTransportError",
    "FirecrawlHttpUnicodeError",
    "FirecrawlHttpUrlError",
    "FirecrawlJsonHttpClient",
    "FirecrawlNoRedirectHandler",
]
