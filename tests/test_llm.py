import asyncio
from dataclasses import FrozenInstanceError

import pytest

from prism.llm import (
    Completion,
    LLMRouter,
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
)


class FakeTransport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self.connection_calls = []
        self.active = 0
        self.max_active = 0

    async def complete(self, *, provider, api_key, payload, timeout):
        self.calls.append(
            {
                "provider": provider.name,
                "api_key": api_key,
                "payload": payload,
                "timeout": timeout,
            }
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            outcome = self.outcomes.pop(0) if self.outcomes else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            return TransportResponse(text=outcome)
        finally:
            self.active -= 1

    async def test_connection(self, *, provider, api_key, timeout):
        self.connection_calls.append(
            (provider.name, api_key, provider.default_model, timeout)
        )
        return True


def make_provider(name, *, concurrency_limit=2):
    return Provider(
        name=name,
        base_url=f"https://{name}.example.test/v1",
        api_key_env=f"{name.upper()}_API_KEY",
        default_model=f"{name}-model",
        timeout=4.5,
        concurrency_limit=concurrency_limit,
    )


def make_router(monkeypatch, transport=None, *, retry_policy=None, concurrency_limit=2):
    primary = make_provider("primary", concurrency_limit=concurrency_limit)
    backup = make_provider("backup")
    monkeypatch.setenv(primary.api_key_env, "primary-secret")
    monkeypatch.setenv(backup.api_key_env, "backup-secret")
    return LLMRouter(
        providers=(primary, backup),
        routes=(
            TaskRoute(TaskRole.EXTRACT, ("primary", "backup")),
            TaskRoute(TaskRole.SUMMARIZE, ("primary",)),
            TaskRoute(TaskRole.DEBATE, ("backup",)),
            TaskRoute(TaskRole.SUMMARIZE_REPORT, ("primary",)),
            TaskRoute(TaskRole.SOURCE_SELECTOR, ("backup",)),
        ),
        transport=transport or FakeTransport(),
        retry_policy=retry_policy or RetryPolicy(),
    )


def test_runtime_models_are_immutable_and_validated():
    provider = make_provider("primary")

    with pytest.raises(FrozenInstanceError):
        provider.timeout = 10
    with pytest.raises(ValueError, match="timeout"):
        Provider("bad", "https://bad.test", "BAD_KEY", "model", timeout=0)
    with pytest.raises(ValueError, match="concurrency"):
        Provider(
            "bad", "https://bad.test", "BAD_KEY", "model", concurrency_limit=0
        )
    with pytest.raises(ValueError, match="task role"):
        TaskRoute("translate", ("primary",))


def test_route_selection_is_deterministic_and_manual_override_is_explicit(monkeypatch):
    transport = FakeTransport(["primary", "backup"])
    router = make_router(monkeypatch, transport)

    routed = asyncio.run(router.complete(TaskRole.EXTRACT, "prompt"))
    overridden = asyncio.run(
        router.complete(TaskRole.EXTRACT, "prompt", provider="backup")
    )

    assert routed == Completion("primary", "primary", "primary-model")
    assert overridden == Completion("backup", "backup", "backup-model")
    assert [call["provider"] for call in transport.calls] == ["primary", "backup"]


def test_retryable_failures_are_bounded_then_fall_back(monkeypatch):
    transport = FakeTransport(
        [
            RetryableLLMError("temporary one"),
            RetryableLLMError("temporary two"),
            "from backup",
        ]
    )
    router = make_router(
        monkeypatch,
        transport,
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    )

    result = asyncio.run(router.complete("extract", "prompt"))

    assert result.provider == "backup"
    assert result.text == "from backup"
    assert [call["provider"] for call in transport.calls] == [
        "primary",
        "primary",
        "backup",
    ]


def test_retry_exhaustion_is_explicit_and_does_not_loop(monkeypatch):
    transport = FakeTransport(
        [RetryableLLMError("still down") for _ in range(4)]
    )
    router = make_router(
        monkeypatch,
        transport,
        retry_policy=RetryPolicy(max_attempts_per_provider=2),
    )

    with pytest.raises(RetriesExhaustedError, match="primary.*backup"):
        asyncio.run(router.complete("extract", "prompt"))

    assert len(transport.calls) == 4


def test_non_retryable_transport_error_does_not_fall_back(monkeypatch):
    transport = FakeTransport([ValueError("bad request"), "unused"])
    router = make_router(monkeypatch, transport)

    with pytest.raises(ValueError, match="bad request"):
        asyncio.run(router.complete("extract", "prompt"))

    assert len(transport.calls) == 1


def test_api_key_is_resolved_at_call_time_and_kept_out_of_payload_and_repr(monkeypatch):
    transport = FakeTransport(["first", "second"])
    router = make_router(monkeypatch, transport)

    asyncio.run(router.complete("extract", "prompt"))
    monkeypatch.setenv("PRIMARY_API_KEY", "rotated-secret")
    asyncio.run(router.complete("extract", "prompt"))

    assert [call["api_key"] for call in transport.calls] == [
        "primary-secret",
        "rotated-secret",
    ]
    assert all("api_key" not in call["payload"] for call in transport.calls)
    assert "primary-secret" not in repr(router)
    assert "rotated-secret" not in repr(router)


def test_missing_provider_role_and_key_have_explicit_errors(monkeypatch):
    router = make_router(monkeypatch)

    with pytest.raises(MissingRoleError, match="unknown"):
        asyncio.run(router.complete("unknown", "prompt"))
    with pytest.raises(MissingProviderError, match="missing"):
        asyncio.run(router.complete("extract", "prompt", provider="missing"))

    monkeypatch.delenv("PRIMARY_API_KEY")
    with pytest.raises(MissingAPIKeyError, match="PRIMARY_API_KEY"):
        asyncio.run(router.complete("summarize", "prompt"))


def test_model_override_and_usage_counters_are_deterministic(monkeypatch):
    transport = FakeTransport(["eight chars"])
    router = make_router(monkeypatch, transport)

    result = asyncio.run(
        router.complete("summarize", "four chars", model="manual-model")
    )

    assert result.model == "manual-model"
    assert transport.calls[0]["payload"] == {
        "model": "manual-model",
        "prompt": "four chars",
    }
    assert router.usage.calls == 1
    assert router.usage.estimated_tokens == 6
    assert router.usage.by_provider == {"primary": 1}


def test_provider_concurrency_limit_is_enforced(monkeypatch):
    transport = FakeTransport([str(index) for index in range(6)])
    router = make_router(monkeypatch, transport, concurrency_limit=2)

    async def run_calls():
        return await asyncio.gather(
            *(router.complete("summarize", str(index)) for index in range(6))
        )

    asyncio.run(run_calls())

    assert transport.max_active == 2


def test_connection_test_uses_injected_transport_and_current_environment(monkeypatch):
    transport = FakeTransport()
    router = make_router(monkeypatch, transport)
    monkeypatch.setenv("BACKUP_API_KEY", "fresh-backup-secret")

    connected = asyncio.run(router.test_connection("backup"))

    assert connected is True
    assert transport.connection_calls == [
        ("backup", "fresh-backup-secret", "backup-model", 4.5)
    ]


def test_router_rejects_routes_that_reference_missing_providers():
    with pytest.raises(MissingProviderError, match="missing"):
        LLMRouter(
            providers=(make_provider("primary"),),
            routes=(TaskRoute("extract", ("missing",)),),
            transport=FakeTransport(),
        )
