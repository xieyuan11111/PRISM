"""Dependency-free HTTP transport for OpenAI-compatible LLM providers."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .router import (
    LLMRouterError,
    Provider,
    RetryableLLMError,
    TransportResponse,
)


_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429})


class LLMTransportError(LLMRouterError):
    """A non-retryable HTTP transport or response failure."""


class OpenAICompatibleTransport:
    """Send router requests to an OpenAI-compatible HTTP API.

    ``opener`` is injectable for offline use and tests.  It may be a callable
    with the same interface as :func:`urllib.request.urlopen`, or an object
    exposing an ``open`` method such as ``urllib.request.OpenerDirector``.
    """

    def __init__(self, opener: Callable[..., Any] | Any | None = None) -> None:
        candidate = urlopen if opener is None else opener
        open_method = getattr(candidate, "open", None)
        if callable(open_method):
            self._open = open_method
        elif callable(candidate):
            self._open = candidate
        else:
            raise TypeError("opener must be callable or expose an open method")

    async def complete(
        self,
        *,
        provider: Provider,
        api_key: str,
        payload: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        """Send one chat completion without blocking the event loop."""

        request = self._completion_request(provider, api_key, payload)
        return await asyncio.to_thread(
            self._complete_sync, request, provider, api_key, timeout
        )

    async def test_connection(
        self, *, provider: Provider, api_key: str, timeout: float
    ) -> bool:
        """Check credentials with a minimal, body-free ``GET /models``."""

        request = Request(
            self._endpoint(provider.base_url, "models"),
            headers=self._headers(api_key, include_content_type=False),
            method="GET",
        )
        await asyncio.to_thread(
            self._request_bytes, request, provider, api_key, timeout
        )
        return True

    def _completion_request(
        self,
        provider: Provider,
        api_key: str,
        payload: Mapping[str, str],
    ) -> Request:
        if not isinstance(payload, Mapping):
            raise LLMTransportError("completion payload must be a mapping")

        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise LLMTransportError("completion payload requires a model")

        if "messages" in payload:
            messages: object = payload["messages"]
            if not isinstance(messages, list):
                raise LLMTransportError("completion payload messages must be a list")
        else:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise LLMTransportError("completion payload requires a prompt or messages")
            messages = [{"role": "user", "content": prompt}]

        try:
            body = json.dumps(
                {"model": model, "messages": messages},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LLMTransportError(
                "completion payload messages must be JSON serializable"
            ) from error

        return Request(
            self._endpoint(provider.base_url, "chat/completions"),
            data=body,
            headers=self._headers(api_key, include_content_type=True),
            method="POST",
        )

    def _complete_sync(
        self,
        request: Request,
        provider: Provider,
        api_key: str,
        timeout: float,
    ) -> TransportResponse:
        body = self._request_bytes(request, provider, api_key, timeout)
        return self._parse_completion(body, provider)

    def _request_bytes(
        self,
        request: Request,
        provider: Provider,
        api_key: str,
        timeout: float,
    ) -> bytes:
        response: Any | None = None
        try:
            response = self._open(request, timeout=timeout)
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", 200)
            body = response.read()
            if not isinstance(body, bytes):
                raise LLMTransportError(
                    f"provider {provider.name!r} returned a non-bytes HTTP response"
                )
            if not isinstance(status, int) or not 200 <= status < 300:
                raise self._http_error(provider, status)
            return body
        except HTTPError as error:
            raise self._http_error(provider, error.code) from None
        except (TimeoutError, socket.timeout):
            raise RetryableLLMError(
                f"LLM request to provider {provider.name!r} timed out"
            ) from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                detail = "timed out"
            else:
                detail = "failed due to a temporary network error"
            raise RetryableLLMError(
                f"LLM request to provider {provider.name!r} {detail}"
            ) from None
        except (LLMTransportError, RetryableLLMError):
            raise
        except OSError:
            raise RetryableLLMError(
                f"LLM request to provider {provider.name!r} failed due to a network error"
            ) from None
        except Exception as error:
            detail = self._redact(str(error), api_key)
            suffix = f": {detail}" if detail else ""
            raise LLMTransportError(
                f"LLM transport failed for provider {provider.name!r}{suffix}"
            ) from None
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    @staticmethod
    def _parse_completion(body: bytes, provider: Provider) -> TransportResponse:
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LLMTransportError(
                f"provider {provider.name!r} returned a malformed JSON response"
            ) from None

        try:
            choices = document["choices"]
            message = choices[0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMTransportError(
                f"provider {provider.name!r} returned an invalid completion response schema"
            ) from None

        if not isinstance(content, str):
            raise LLMTransportError(
                f"provider {provider.name!r} returned non-text completion response content"
            )

        usage = document.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise LLMTransportError(
                f"provider {provider.name!r} returned invalid usage in its response"
            )
        return TransportResponse(text=content)

    @staticmethod
    def _headers(api_key: str, *, include_content_type: bool) -> dict[str, str]:
        if not isinstance(api_key, str) or not api_key.strip():
            raise LLMTransportError("API key must be a non-empty string")
        if any(character in api_key for character in "\r\n"):
            raise LLMTransportError("API key contains invalid header characters")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        if include_content_type:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _endpoint(base_url: str, resource: str) -> str:
        parsed = urlsplit(base_url)
        path = parsed.path.rstrip("/")
        if path.rsplit("/", 1)[-1] != "v1":
            path = f"{path}/v1"
        path = f"{path}/{resource}"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    @staticmethod
    def _http_error(provider: Provider, status: object) -> Exception:
        message = f"provider {provider.name!r} returned HTTP status {status}"
        if isinstance(status, int) and (
            status in _RETRYABLE_HTTP_STATUSES or 500 <= status <= 599
        ):
            return RetryableLLMError(message)
        return LLMTransportError(message)

    @staticmethod
    def _redact(message: str, api_key: str) -> str:
        if api_key:
            return message.replace(api_key, "[REDACTED]")
        return message


__all__ = ["LLMTransportError", "OpenAICompatibleTransport"]
