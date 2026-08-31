"""Dependency-free, async LLM routing with injectable offline transports.

The public API contains immutable provider/route models, :class:`LLMRouter`,
small result and usage value objects, and explicit router exceptions.  This
package deliberately implements no HTTP client or provider SDK; applications
inject a transport matching :class:`LLMTransport`.
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

__all__ = [
    "Completion",
    "LLMRouter",
    "LLMRouterError",
    "LLMTransport",
    "MissingAPIKeyError",
    "MissingProviderError",
    "MissingRoleError",
    "Provider",
    "RetriesExhaustedError",
    "RetryPolicy",
    "RetryableLLMError",
    "TaskRole",
    "TaskRoute",
    "TransportResponse",
    "Usage",
]
