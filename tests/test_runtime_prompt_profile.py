"""TDD tests for the ``prompt_profile`` seam on ``create_runtime``.

The default production composition must stay byte-identical baseline: the
optional ``prompt_profile`` argument may only flow into the default LLM
``ExtractionService`` the composition itself creates.  An explicitly
injected ``extraction_service`` remains a full override, and combining it
with an experimental profile is a hard, fail-closed error instead of a
silently ignored selection.  The profile is never written into config files
or the SQLite schema — it is a runtime constructor argument only.
"""

from __future__ import annotations

import asyncio

import pytest

from prism.config import LLMConfig, LLMProviderConfig, PrismConfig
from prism.extraction import ExtractionService
from prism.runtime import OfflineExtractor, create_runtime


class OfflineTransport:
    async def complete(self, *, provider, api_key, payload, timeout):
        raise AssertionError("composition must never call the transport")


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
                )
            },
            task_roles={"extract": "primary"},
        )
    ).save(config_path)
    return config_path


class StubExtractor:
    name = "stub"

    async def extract(self, material):
        raise AssertionError("composition never calls the extraction service")

    async def extract_material(self, material, *, corpus_path=None, target_case=None):
        raise AssertionError("composition never calls the extraction service")


def _profile_of(tmp_path, monkeypatch, **kwargs) -> object:
    """Create a runtime, capture its extraction service, close in-loop."""

    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = configured_runtime_file(tmp_path)

    async def exercise():
        runtime = await create_runtime(config_path, **kwargs)
        try:
            service = runtime.extraction_service
            profile = getattr(service, "_prompt_profile", "no-attribute")
            return service, profile
        finally:
            await runtime.close()

    return run(exercise())


def test_default_composition_keeps_the_baseline_prompt_profile(tmp_path, monkeypatch):
    service, profile = _profile_of(
        tmp_path, monkeypatch, llm_transport=OfflineTransport()
    )
    assert isinstance(service, ExtractionService)
    # No profile argument: the baseline prompt stays byte-identical.
    assert profile is None


def test_prompt_profile_reaches_the_default_extraction_service(tmp_path, monkeypatch):
    service, profile = _profile_of(
        tmp_path,
        monkeypatch,
        llm_transport=OfflineTransport(),
        prompt_profile="protocol-v1",
    )
    assert isinstance(service, ExtractionService)
    assert profile == "protocol-v1"


def test_explicit_baseline_profile_is_normalized_to_the_untouched_default(
    tmp_path, monkeypatch
):
    # "baseline" spells the untouched default; it must not behave like a
    # non-baseline selection (which would break the legacy extract path).
    _, profile = _profile_of(
        tmp_path,
        monkeypatch,
        llm_transport=OfflineTransport(),
        prompt_profile="baseline",
    )
    assert profile is None


def test_unknown_prompt_profile_fails_closed_at_composition(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = configured_runtime_file(tmp_path)

    async def exercise():
        await create_runtime(
            config_path,
            llm_transport=OfflineTransport(),
            prompt_profile="protocol-v3",
        )

    with pytest.raises(ValueError, match="unknown prompt_profile"):
        run(exercise())


def test_injected_extraction_service_keeps_full_override_semantics(
    tmp_path, monkeypatch
):
    stub = StubExtractor()
    service, _ = _profile_of(
        tmp_path,
        monkeypatch,
        llm_transport=OfflineTransport(),
        extraction_service=stub,
    )
    assert service is stub


def test_injected_extraction_service_rejects_experimental_profile(
    tmp_path, monkeypatch
):
    # A profile selection that cannot be honored must never be silently
    # ignored: an injected service plus an experimental profile is a hard
    # error so a caller can never believe protocol-v1 ran while baseline did.
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = configured_runtime_file(tmp_path)

    async def exercise():
        await create_runtime(
            config_path,
            llm_transport=OfflineTransport(),
            extraction_service=StubExtractor(),
            prompt_profile="protocol-v1",
        )

    with pytest.raises(ValueError, match="extraction_service"):
        run(exercise())


def test_profile_without_an_llm_router_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        await create_runtime(prompt_profile="protocol-v1")

    with pytest.raises(ValueError, match="LLM router"):
        run(exercise())


def test_offline_default_runtime_is_unchanged_without_a_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        runtime = await create_runtime()
        try:
            return runtime.extraction_service
        finally:
            await runtime.close()

    assert isinstance(run(exercise()), OfflineExtractor)
