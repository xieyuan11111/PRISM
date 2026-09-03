"""Focused tests for wiring configured LLM services into the runtime."""

from __future__ import annotations

import asyncio

import pytest

from prism.config import LLMConfig, LLMProviderConfig, PrismConfig
from prism.llm import (
    LLMRouter,
    MissingAPIKeyError,
    OpenAICompatibleTransport,
    TransportResponse,
)
from prism.runtime import create_runtime


class OfflineTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.connection_calls: list[dict[str, object]] = []

    async def complete(self, *, provider, api_key, payload, timeout):
        self.calls.append(
            {
                "provider": provider,
                "api_key": api_key,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return TransportResponse("offline answer")

    async def test_connection(self, *, provider, api_key, timeout):
        self.connection_calls.append(
            {"provider": provider, "api_key": api_key, "timeout": timeout}
        )
        return True


def run(coro):
    return asyncio.run(coro)


def configured_runtime_file(tmp_path):
    config_path = tmp_path / "config.json"
    PrismConfig(
        llm=LLMConfig(
            providers={
                "primary": LLMProviderConfig(
                    model="provider/model-v1",
                    base_url="https://llm.example.test/v1",
                    api_key_env="PRISM_TEST_API_KEY",
                    timeout=12.5,
                    concurrency_limit=3,
                )
            },
            task_roles={"extract": "primary", "summarize": "primary"},
        )
    ).save(config_path)
    return config_path


def test_empty_llm_config_preserves_safe_offline_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        async with await create_runtime() as runtime:
            assert runtime.llm_router is None

    run(exercise())


def test_configured_providers_and_routes_build_router_without_reading_key(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_TEST_API_KEY", raising=False)
    transport = OfflineTransport()

    async def exercise():
        runtime = await create_runtime(
            configured_runtime_file(tmp_path), llm_transport=transport
        )
        try:
            assert isinstance(runtime.llm_router, LLMRouter)
            assert transport.calls == []
            assert transport.connection_calls == []

            provider = runtime.llm_router._providers["primary"]
            assert provider.name == "primary"
            assert provider.default_model == "provider/model-v1"
            assert provider.base_url == "https://llm.example.test/v1"
            assert provider.api_key_env == "PRISM_TEST_API_KEY"
            assert provider.timeout == 12.5
            assert provider.concurrency_limit == 3
            assert {
                route.role.value: route.providers
                for route in runtime.llm_router._routes.values()
            } == {
                "extract": ("primary",),
                "summarize": ("primary",),
            }

            with pytest.raises(MissingAPIKeyError, match="PRISM_TEST_API_KEY"):
                await runtime.llm_router.complete("extract", "prompt")
            assert transport.calls == []

            monkeypatch.setenv("PRISM_TEST_API_KEY", "only-read-on-call")
            completion = await runtime.llm_router.complete("extract", "prompt")
            assert completion.text == "offline answer"
            assert transport.calls[0]["api_key"] == "only-read-on-call"
            assert transport.calls[0]["payload"] == {
                "model": "provider/model-v1",
                "prompt": "prompt",
            }
        finally:
            await runtime.close()

        assert len(transport.calls) == 1
        assert transport.connection_calls == []

    run(exercise())


def test_default_transport_is_constructed_but_never_called_implicitly(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_TEST_API_KEY", raising=False)

    async def exercise():
        runtime = await create_runtime(configured_runtime_file(tmp_path))
        try:
            assert isinstance(runtime.llm_router, LLMRouter)
            assert isinstance(
                runtime.llm_router._transport, OpenAICompatibleTransport
            )
            assert runtime.llm_router.usage.calls == 0
        finally:
            await runtime.close()
        assert runtime.llm_router.usage.calls == 0

    run(exercise())


@pytest.mark.parametrize(
    ("llm", "message"),
    [
        (
            LLMConfig(
                providers={
                    "primary": LLMProviderConfig(
                        model="model",
                        base_url="https://llm.example.test/v1",
                        api_key_env="PRISM_TEST_API_KEY",
                    )
                }
            ),
            "task routes",
        ),
        (
            LLMConfig(
                providers={
                    "primary": LLMProviderConfig(
                        model="model",
                        base_url="https://llm.example.test/v1",
                        api_key_env="PRISM_TEST_API_KEY",
                    )
                },
                task_roles={"translate": "primary"},
            ),
            "unsupported task role",
        ),
        (
            LLMConfig(
                providers={"primary": LLMProviderConfig(model="model")},
                task_roles={"extract": "primary"},
            ),
            "base_url",
        ),
    ],
)
def test_incomplete_llm_configuration_is_rejected_clearly(
    tmp_path, monkeypatch, llm, message
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "invalid.json"
    PrismConfig(llm=llm).save(config_path)

    with pytest.raises(ValueError, match=message):
        run(create_runtime(config_path, llm_transport=OfflineTransport()))


def test_adjudicate_role_wires_the_adjudicator_into_pipeline_and_api(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRISM_TEST_API_KEY", "key")
    transport = OfflineTransport()

    async def exercise():
        runtime = await create_runtime(
            configured_runtime_file(tmp_path),
            llm_transport=transport,
        )
        try:
            # Default task_roles (extract/summarize) never create an
            # adjudicator, so the pipeline keeps its pre-adjudication
            # behaviour without any LLM role.
            assert runtime.adjudicator is None
            assert runtime.pipeline._adjudicator is None
            assert runtime.api.adjudication_history() == ()
        finally:
            await runtime.close()

    run(exercise())


def test_adjudicate_role_creates_a_durable_ledger_backed_adjudicator(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRISM_TEST_API_KEY", "key")
    config_path = tmp_path / "config.json"
    PrismConfig(
        llm=LLMConfig(
            providers={
                "primary": LLMProviderConfig(
                    model="provider/model-v1",
                    base_url="https://llm.example.test/v1",
                    api_key_env="PRISM_TEST_API_KEY",
                )
            },
            task_roles={
                "extract": "primary",
                "summarize": "primary",
                "adjudicate": "primary",
            },
        )
    ).save(config_path)
    transport = OfflineTransport()

    async def exercise():
        runtime = await create_runtime(config_path, llm_transport=transport)
        try:
            from prism.adjudication import AdjudicationService

            assert isinstance(runtime.adjudicator, AdjudicationService)
            assert runtime.pipeline._adjudicator is runtime.adjudicator
            assert runtime.api._adjudicator is runtime.adjudicator
            # The durable ledger lives in the shared data-dir SQLite file.
            ledger = runtime.adjudicator._ledger
            assert ledger is not None
            assert ledger._db_path == runtime.paths.data_dir / "index.db"
            # Nothing was called and nothing was recorded at wiring time.
            assert transport.calls == []
            assert runtime.api.adjudication_history() == ()
        finally:
            await runtime.close()

    run(exercise())
