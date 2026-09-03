"""Core models and router for provider-neutral LLM text completion.

``LLMRouter.complete`` accepts a task role and plain-text prompt.  Route order
is deterministic, retryable failures use a bounded per-provider retry policy,
and provider fallbacks are tried in declared order.  API keys are read from the
environment immediately before transport calls and are passed separately from
the secret-free request payload.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


class TaskRole(str, Enum):
    """Supported PRISM LLM task roles."""

    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    DEBATE = "debate"
    SUMMARIZE_REPORT = "summarize_report"
    SOURCE_SELECTOR = "source_selector"
    ADJUDICATE = "adjudicate"


@dataclass(frozen=True, slots=True)
class Provider:
    """Secret-free connection and execution settings for one provider."""

    name: str
    base_url: str
    api_key_env: str
    default_model: str
    timeout: float = 30.0
    concurrency_limit: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text("provider name", self.name))
        base_url = _text("base_url", self.base_url)
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        object.__setattr__(self, "base_url", base_url)

        api_key_env = _text("api_key_env", self.api_key_env)
        if not _ENV_NAME.fullmatch(api_key_env):
            raise ValueError("api_key_env must be a valid environment variable name")
        object.__setattr__(self, "api_key_env", api_key_env)
        object.__setattr__(
            self, "default_model", _text("default_model", self.default_model)
        )

        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        object.__setattr__(self, "timeout", float(self.timeout))

        if isinstance(self.concurrency_limit, bool) or not isinstance(
            self.concurrency_limit, int
        ):
            raise TypeError("concurrency_limit must be an integer")
        if self.concurrency_limit <= 0:
            raise ValueError("concurrency_limit must be greater than zero")


@dataclass(frozen=True, slots=True)
class TaskRoute:
    """An ordered provider fallback chain for one supported task role."""

    role: TaskRole | str
    providers: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            role = TaskRole(self.role)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unsupported task role: {self.role!r}") from error
        object.__setattr__(self, "role", role)

        if isinstance(self.providers, str):
            raise TypeError("route providers must be an iterable of provider names")
        providers = tuple(
            _text("provider name", provider_name)
            for provider_name in self.providers
        )
        if not providers:
            raise ValueError("route providers must not be empty")
        if len(set(providers)) != len(providers):
            raise ValueError("route providers must not contain duplicates")
        object.__setattr__(self, "providers", providers)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retries applied independently to each selected provider."""

    max_attempts_per_provider: int = 1
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts_per_provider, bool) or not isinstance(
            self.max_attempts_per_provider, int
        ):
            raise TypeError("max_attempts_per_provider must be an integer")
        if self.max_attempts_per_provider <= 0:
            raise ValueError("max_attempts_per_provider must be greater than zero")
        if isinstance(self.backoff_seconds, bool) or not isinstance(
            self.backoff_seconds, (int, float)
        ):
            raise TypeError("backoff_seconds must be a number")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")
        object.__setattr__(self, "backoff_seconds", float(self.backoff_seconds))


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """Provider-neutral text returned by an injected transport."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("transport response text must be a string")


@dataclass(frozen=True, slots=True)
class Completion:
    """Successful completion text plus the provider and model that produced it."""

    text: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Snapshot of completion attempts and deterministic token estimates."""

    calls: int
    estimated_tokens: int
    by_provider: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "by_provider", MappingProxyType(dict(self.by_provider))
        )


class LLMTransport(Protocol):
    """Async transport boundary implemented by an application or test fake."""

    async def complete(
        self,
        *,
        provider: Provider,
        api_key: str,
        payload: Mapping[str, str],
        timeout: float,
    ) -> TransportResponse:
        """Send one completion attempt."""

    async def test_connection(
        self, *, provider: Provider, api_key: str, timeout: float
    ) -> bool:
        """Verify that the provider is reachable and accepts the credentials."""


class LLMRouterError(Exception):
    """Base class for router-owned failures."""


class MissingProviderError(LLMRouterError):
    """A provider name was not declared."""


class MissingRoleError(LLMRouterError):
    """A task role has no declared route."""


class MissingAPIKeyError(LLMRouterError):
    """A provider's configured environment variable is unset or empty."""


class RetryableLLMError(LLMRouterError):
    """Transport failure that may be retried or routed to a fallback."""


class RetriesExhaustedError(LLMRouterError):
    """Every attempt in the selected provider chain failed retryably."""

    def __init__(self, providers: Sequence[str], attempts: int) -> None:
        chain = ", ".join(providers)
        super().__init__(
            f"retry attempts exhausted after {attempts} calls across providers: {chain}"
        )
        self.providers = tuple(providers)
        self.attempts = attempts


class LLMRouter:
    """Route async text completions through injected, provider-neutral transport."""

    def __init__(
        self,
        *,
        providers: Sequence[Provider],
        routes: Sequence[TaskRoute],
        transport: LLMTransport,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        provider_map: dict[str, Provider] = {}
        for provider in providers:
            if not isinstance(provider, Provider):
                raise TypeError("providers must contain Provider instances")
            if provider.name in provider_map:
                raise ValueError(f"duplicate provider: {provider.name!r}")
            provider_map[provider.name] = provider

        route_map: dict[TaskRole, TaskRoute] = {}
        for route in routes:
            if not isinstance(route, TaskRoute):
                raise TypeError("routes must contain TaskRoute instances")
            if route.role in route_map:
                raise ValueError(f"duplicate task role: {route.role.value!r}")
            for provider_name in route.providers:
                if provider_name not in provider_map:
                    raise MissingProviderError(
                        f"task role {route.role.value!r} references missing provider "
                        f"{provider_name!r}"
                    )
            route_map[route.role] = route

        if transport is None:
            raise TypeError("transport is required")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be a RetryPolicy")

        self._providers = MappingProxyType(provider_map)
        self._routes = MappingProxyType(route_map)
        self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy()
        self._semaphores = {
            name: asyncio.Semaphore(provider.concurrency_limit)
            for name, provider in provider_map.items()
        }
        self._calls = 0
        self._estimated_tokens = 0
        self._calls_by_provider = {name: 0 for name in provider_map}

    @property
    def usage(self) -> Usage:
        """Return a read-only snapshot; calls count completion attempts."""

        return Usage(
            calls=self._calls,
            estimated_tokens=self._estimated_tokens,
            by_provider={
                name: calls
                for name, calls in self._calls_by_provider.items()
                if calls
            },
        )

    async def complete(
        self,
        role: TaskRole | str,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> Completion:
        """Complete a prompt using a role route or one explicit provider override."""

        task_role = self._require_role(role)
        prompt = _text("prompt", prompt)
        provider_names = (
            (self._require_provider(provider).name,)
            if provider is not None
            else self._routes[task_role].providers
        )
        selected_model = _text("model", model) if model is not None else None
        attempts = 0
        last_error: RetryableLLMError | None = None

        for provider_name in provider_names:
            selected = self._providers[provider_name]
            request_model = selected_model or selected.default_model
            for attempt in range(self._retry_policy.max_attempts_per_provider):
                attempts += 1
                try:
                    response = await self._attempt(selected, request_model, prompt)
                except RetryableLLMError as error:
                    last_error = error
                    if (
                        attempt + 1 < self._retry_policy.max_attempts_per_provider
                        and self._retry_policy.backoff_seconds
                    ):
                        await asyncio.sleep(self._retry_policy.backoff_seconds)
                    continue

                self._estimated_tokens += self._estimate_tokens(response.text)
                return Completion(response.text, selected.name, request_model)

        exhausted = RetriesExhaustedError(provider_names, attempts)
        raise exhausted from last_error

    async def test_connection(self, provider: str) -> bool:
        """Run the injected transport's connection check for one provider."""

        selected = self._require_provider(provider)
        api_key = self._resolve_api_key(selected)
        async with self._semaphores[selected.name]:
            return bool(
                await self._transport.test_connection(
                    provider=selected,
                    api_key=api_key,
                    timeout=selected.timeout,
                )
            )

    async def _attempt(
        self, provider: Provider, model: str, prompt: str
    ) -> TransportResponse:
        api_key = self._resolve_api_key(provider)
        payload = {"model": model, "prompt": prompt}
        async with self._semaphores[provider.name]:
            self._calls += 1
            self._calls_by_provider[provider.name] += 1
            self._estimated_tokens += self._estimate_tokens(prompt)
            response = await self._transport.complete(
                provider=provider,
                api_key=api_key,
                payload=payload,
                timeout=provider.timeout,
            )
        if not isinstance(response, TransportResponse):
            raise TypeError("transport.complete must return TransportResponse")
        return response

    def _require_role(self, role: TaskRole | str) -> TaskRole:
        try:
            task_role = TaskRole(role)
        except (TypeError, ValueError) as error:
            raise MissingRoleError(f"missing task role: {role!r}") from error
        if task_role not in self._routes:
            raise MissingRoleError(f"missing task role: {task_role.value!r}")
        return task_role

    def _require_provider(self, name: str) -> Provider:
        if not isinstance(name, str) or name not in self._providers:
            raise MissingProviderError(f"missing provider: {name!r}")
        return self._providers[name]

    @staticmethod
    def _resolve_api_key(provider: Provider) -> str:
        api_key = os.environ.get(provider.api_key_env)
        if api_key is None or not api_key.strip():
            raise MissingAPIKeyError(
                f"missing API key environment variable: {provider.api_key_env}"
            )
        return api_key

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)
