"""Async LLM routing and the official-openai-SDK transport.

The public API contains immutable provider/route models, :class:`LLMRouter`,
small result and usage value objects, explicit router exceptions, and
:class:`OpenAISDKTransport` — the transport that drives the official
``openai`` package's ``AsyncOpenAI`` client.  The SDK is an opt-in extra
and is imported lazily; importing this package stays dependency-free.
"""

from .router import (
    Completion,
    LLMRouter,
    LLMRouterError,
    LLMTransport,
    MissingAPIKeyError,
    MissingProviderError,
    MissingRoleError,
    Provider,
    RetryPolicy,
    RetryableLLMError,
    RetriesExhaustedError,
    TaskRole,
    TaskRoute,
    TransportResponse,
    Usage,
)
from .transport import LLMTransportError, OpenAISDKTransport

__all__ = [
    "Completion",
    "LLMRouter",
    "LLMRouterError",
    "LLMTransport",
    "LLMTransportError",
    "MissingAPIKeyError",
    "MissingProviderError",
    "MissingRoleError",
    "OpenAISDKTransport",
    "Provider",
    "RetriesExhaustedError",
    "RetryPolicy",
    "RetryableLLMError",
    "TaskRole",
    "TaskRoute",
    "TransportResponse",
    "Usage",
]
