"""LLM transport backed by the official ``openai`` Python SDK.

PRISM owns no hand-rolled HTTP or SSE protocol here: request encoding,
header handling and server-sent-event parsing all belong to the official
SDK's ``AsyncOpenAI`` client.  The SDK is an explicit opt-in extra
(``pip install "news-prism[openai-sdk]"``) and is imported lazily —
importing this module, constructing the transport and composing an
offline runtime never import it and never touch the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .router import (
    LLMRouterError,
    Provider,
    RetryableLLMError,
    TransportResponse,
)


_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429})

# Class-name fallback used only when the SDK itself is not importable
# (unit tests drive the transport through injected fake clients).  With the
# SDK installed, classification uses isinstance checks instead.
_RETRYABLE_ERROR_NAMES = frozenset(
    {"APITimeoutError", "APIConnectTimeoutError", "APIConnectionError"}
)

_INSTALL_HINT = (
    "the optional openai SDK is not installed; "
    'install it with pip install "news-prism[openai-sdk]"'
)


class LLMTransportError(LLMRouterError):
    """A non-retryable transport or response failure."""


class OpenAISDKTransport:
    """Send router requests through the official ``AsyncOpenAI`` client.

    ``client_factory`` is injectable for offline unit tests; it is called
    as ``client_factory(api_key=..., base_url=..., timeout=...)`` and must
    return an ``AsyncOpenAI``-shaped client.  The default factory imports
    the SDK lazily on first use, so a default offline runtime (or a fake
    factory in tests) never needs the package installed.

    ``base_url`` is forwarded to the SDK verbatim: providers that already
    carry a version segment (for example the Volcano coding endpoint
    ``.../api/coding/v3``) must not have ``/v1`` appended by PRISM.

    ``stream`` selects between the SDK's non-streaming and streaming
    ``chat.completions.create`` calls.  Streaming uses the SDK's own SSE
    parsing, collects ``choices[0].delta.content`` fragments and requires
    the stream to end with an explicit ``finish_reason``.

    Raised error messages are deliberately built from the provider name,
    HTTP status and exception class name only: the SDK's own message text
    (which can echo the API key, prompt or response body) is never
    propagated.
    """

    def __init__(
        self,
        *,
        client_factory: Any | None = None,
        stream: bool = False,
        json_mode: bool = False,
    ) -> None:
        if not isinstance(stream, bool):
            raise TypeError("stream must be bool")
        if not isinstance(json_mode, bool):
            raise TypeError("json_mode must be bool")
        self._client_factory = client_factory
        self._stream = stream
        self._json_mode = json_mode
        self._clients: dict[tuple[str, str, float], Any] = {}

    async def complete(
        self,
        *,
        provider: Provider,
        api_key: str,
        payload: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        """Send one chat completion through the official SDK client."""

        model, messages = self._completion_request(payload)
        client = self._client_for(provider, api_key, timeout)
        request = {
            "model": model,
            "messages": messages,
            "stream": self._stream,
            "timeout": timeout,
        }
        if self._json_mode:
            request["response_format"] = {"type": "json_object"}
        try:
            if self._stream:
                return await self._complete_streaming(
                    client, request, provider
                )
            return await self._complete_non_streaming(client, request, provider)
        except (LLMTransportError, RetryableLLMError):
            raise
        except Exception as error:
            raise self._map_error(error, provider) from None

    async def test_connection(
        self, *, provider: Provider, api_key: str, timeout: float
    ) -> bool:
        """Check credentials with the SDK's body-free ``models.list``."""

        client = self._client_for(provider, api_key, timeout)
        try:
            await client.models.list(timeout=timeout)
        except (LLMTransportError, RetryableLLMError):
            raise
        except Exception as error:
            raise self._map_error(error, provider) from None
        return True

    def _client_for(
        self, provider: Provider, api_key: str, timeout: float
    ) -> Any:
        key = (provider.name, api_key, float(timeout))
        client = self._clients.get(key)
        if client is None:
            factory = self._client_factory or self._default_client_factory
            client = factory(api_key=api_key, base_url=provider.base_url, timeout=timeout)
            if client is None:
                raise LLMTransportError("openai client factory returned None")
            self._clients[key] = client
        return client

    @staticmethod
    def _default_client_factory(
        *, api_key: str, base_url: str, timeout: float
    ) -> Any:
        try:
            # Lazy by design: composition and offline tests never reach it.
            from openai import AsyncOpenAI
        except ImportError:
            raise LLMTransportError(_INSTALL_HINT) from None
        # Retries stay owned by the router's bounded policy, not the SDK.
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    @staticmethod
    def _completion_request(
        payload: Mapping[str, str],
    ) -> tuple[str, list[dict[str, str]]]:
        if not isinstance(payload, Mapping):
            raise LLMTransportError("completion payload must be a mapping")

        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise LLMTransportError("completion payload requires a model")

        if "messages" in payload:
            messages = payload["messages"]
            if not isinstance(messages, list):
                raise LLMTransportError(
                    "completion payload messages must be a list"
                )
        else:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise LLMTransportError(
                    "completion payload requires a prompt or messages"
                )
            messages = [{"role": "user", "content": prompt}]
        return model, messages

    async def _complete_non_streaming(
        self, client: Any, request: dict[str, Any], provider: Provider
    ) -> TransportResponse:
        response = await client.chat.completions.create(**request)
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise LLMTransportError(
                f"provider {provider.name!r} returned an invalid completion "
                "response schema"
            )
        choice = choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if not isinstance(content, str):
            raise LLMTransportError(
                f"provider {provider.name!r} returned non-text completion "
                "response content"
            )
        return TransportResponse(text=content)

    async def _complete_streaming(
        self, client: Any, request: dict[str, Any], provider: Provider
    ) -> TransportResponse:
        stream = await client.chat.completions.create(**request)
        parts: list[str] = []
        finish_reason: str | None = None
        async for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if isinstance(content, str) and content:
                parts.append(content)
            reason = getattr(choice, "finish_reason", None)
            if isinstance(reason, str) and reason:
                finish_reason = reason
        if finish_reason is None:
            raise LLMTransportError(
                f"provider {provider.name!r} stream ended without an explicit "
                "finish_reason"
            )
        return TransportResponse(text="".join(parts))

    @staticmethod
    def _map_error(error: Exception, provider: Provider) -> Exception:
        status = getattr(error, "status_code", None)
        if isinstance(status, bool) or not isinstance(status, int):
            status = None
        if status is not None:
            if status in _RETRYABLE_HTTP_STATUSES or 500 <= status <= 599:
                return RetryableLLMError(
                    f"provider {provider.name!r} returned HTTP status {status}"
                )
            return LLMTransportError(
                f"provider {provider.name!r} returned HTTP status {status}"
            )
        if isinstance(error, TimeoutError):
            return RetryableLLMError(
                f"LLM request to provider {provider.name!r} timed out"
            )
        try:
            # Lazy classification only; never required for offline use.
            import openai as openai_module
        except ImportError:
            openai_module = None
        if openai_module is not None:
            if isinstance(error, openai_module.APITimeoutError):
                return RetryableLLMError(
                    f"LLM request to provider {provider.name!r} timed out"
                )
            if isinstance(error, openai_module.APIConnectionError):
                return RetryableLLMError(
                    f"LLM request to provider {provider.name!r} failed due to "
                    "a temporary network error"
                )
        if type(error).__name__ in _RETRYABLE_ERROR_NAMES:
            detail = (
                "timed out"
                if type(error).__name__ == "APITimeoutError"
                else "failed due to a temporary network error"
            )
            return RetryableLLMError(
                f"LLM request to provider {provider.name!r} {detail}"
            )
        return LLMTransportError(
            f"LLM transport failed for provider {provider.name!r} "
            f"({type(error).__name__})"
        )


__all__ = ["LLMTransportError", "OpenAISDKTransport"]
