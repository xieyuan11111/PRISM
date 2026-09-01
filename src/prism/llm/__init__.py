"""Dependency-free, async LLM routing and OpenAI-compatible HTTP transport.

The public API contains immutable provider/route models, :class:`LLMRouter`,
small result and usage value objects, and explicit router exceptions.
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
from .transport import LLMTransportError, OpenAICompatibleTransport

__all__ = [
    "Completion",
    "LLMRouter",
    "LLMRouterError",
    "LLMTransport",
    "LLMTransportError",
    "MissingAPIKeyError",
    "MissingProviderError",
    "MissingRoleError",
    "OpenAICompatibleTransport",
    "Provider",
    "RetriesExhaustedError",
    "RetryPolicy",
    "RetryableLLMError",
    "TaskRole",
    "TaskRoute",
    "TransportResponse",
    "Usage",
]
