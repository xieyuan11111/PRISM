"""Composition-root tests for the opt-in Graphiti/Neo4j runtime path.

The default runtime must stay fully offline: no graphiti-core/neo4j import,
no client, no registry, no environment lookup.  Only ``graphiti.enabled=true``
with an explicit factory/backend injection (or the optional dependencies
installed) may attempt the real path, and missing credentials/dependencies
must fail with clear errors before any service is touched.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
from datetime import datetime, timezone

import pytest

from prism.config import GraphitiConfig, PrismConfig
from prism.graph import GraphEpisode, GraphitiBackend
from prism.runtime import OfflineGraphBackend, create_runtime

from graphiti_fakes import FakeGraphitiClient

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def enabled_config(tmp_path, *, uri="bolt://prism-graphiti-spike:7688") -> PrismConfig:
    return PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri=uri,
            group_id="prism-spike",
            password_env="PRISM_GRAPHITI_PASSWORD",
        )
    )


class FakeGraphBackend:
    """Injected backend that cannot reach a network or an LLM."""

    def __init__(self) -> None:
        self.episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str):
        return tuple(self.episodes.values())


class RecordingFactory:
    def __init__(self) -> None:
        self.calls: list[GraphitiConfig] = []
        self.clients: list[FakeGraphitiClient] = []

    def __call__(self, config: GraphitiConfig) -> FakeGraphitiClient:
        self.calls.append(config)
        client = FakeGraphitiClient()
        self.clients.append(client)
        return client


def episode() -> GraphEpisode:
    return GraphEpisode(
        episode_key="4d8fe701-5578-5ca3-a436-1f24d29c6300",
        name="prism:case:claim:claim-a",
        case_id="case",
        kind="claim",
        episode_body='{"kind":"claim"}',
        reference_time=NOW,
        valid_at=NOW,
        invalid_at=None,
        source_ids=("material-a",),
    )


def test_default_offline_runtime_never_imports_graphiti_or_neo4j(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "graphiti_core" or name.startswith("graphiti_core."):
            raise AssertionError("graphiti-core was imported by the offline default")
        if name == "neo4j" or name.startswith("neo4j."):
            raise AssertionError("neo4j was imported by the offline default")
        return real_import(name, *args, **kwargs)

    def guarded_find_spec(name, *args, **kwargs):
        if name in ("graphiti_core", "neo4j"):
            raise AssertionError("dependency probe ran while graphiti is disabled")
        return importlib.util.find_spec(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(importlib.util, "find_spec", guarded_find_spec)

    async def exercise():
        runtime = await create_runtime()
        try:
            assert isinstance(runtime.graph_backend, OfflineGraphBackend)
            assert runtime.graphiti_backend is None
        finally:
            await runtime.close()

    run(exercise())


def test_enabled_without_dependencies_or_factory_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PRISM_GRAPHITI_PASSWORD", "test-only-password")
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)

    with pytest.raises(RuntimeError) as excinfo:
        run(create_runtime(config_path))

    message = str(excinfo.value)
    assert "graphiti" in message
    assert "install" in message
    assert "graphiti_client_factory" in message


def test_enabled_missing_password_env_fails_before_any_import(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_GRAPHITI_PASSWORD", raising=False)
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    # Pretend the optional dependencies are installed: the error must still
    # be the missing credential, raised before any graphiti import attempt.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("graphiti_core"):
            raise AssertionError("graphiti-core was imported before env checks")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError) as excinfo:
        run(create_runtime(config_path))

    assert "PRISM_GRAPHITI_PASSWORD" in str(excinfo.value)


def test_enabled_missing_username_env_fails_when_username_env_configured(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_GRAPHITI_USERNAME", raising=False)
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri="bolt://prism-graphiti-spike:7688",
            group_id="prism-spike",
            username_env="PRISM_GRAPHITI_USERNAME",
        )
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())

    with pytest.raises(RuntimeError) as excinfo:
        run(create_runtime(config_path))

    assert "PRISM_GRAPHITI_USERNAME" in str(excinfo.value)


def test_enabled_with_injected_factory_composes_owned_backend_and_closes_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_GRAPHITI_PASSWORD", raising=False)
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    factory = RecordingFactory()

    async def exercise():
        runtime = await create_runtime(config_path, graphiti_client_factory=factory)
        try:
            assert isinstance(runtime.graph_backend, GraphitiBackend)
            assert runtime.graphiti_backend is runtime.graph_backend
            assert runtime.graphiti_backend.group_id == "prism-spike"
            assert factory.calls == [config.graphiti]
            client = factory.clients[-1]

            # No credential env is needed on the injected-factory path: the
            # factory owns credential handling for its own client.
            assert await runtime.graph_backend.add_episode(episode()) is True
            assert client.add_calls[-1]["group_id"] == "prism-spike"
            assert await runtime.graph_backend.add_episode(episode()) is False
            assert len(client.add_calls) == 1
            assert client.closed is False
        finally:
            await runtime.close()
            await runtime.close()  # close is idempotent

        # The runtime owns the backend it created, so close() reached the client.
        assert factory.clients[-1].closed is True

    run(exercise())


def test_enabled_with_injected_backend_skips_factory_dependency_and_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_GRAPHITI_PASSWORD", raising=False)
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    # No dependencies installed and no factory: the injected backend alone
    # must be enough for an offline/controlled integration.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    injected = FakeGraphBackend()

    async def exercise():
        runtime = await create_runtime(config_path, graph_backend=injected)
        try:
            assert runtime.graph_backend is injected
            assert runtime.graphiti_backend is None
        finally:
            await runtime.close()

    run(exercise())


def test_factory_and_backend_conflict_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)

    with pytest.raises(ValueError, match="cannot be combined"):
        run(
            create_runtime(
                config_path,
                graph_backend=FakeGraphBackend(),
                graphiti_client_factory=RecordingFactory(),
            )
        )


def test_factory_requires_enabled_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    PrismConfig().save(config_path)

    with pytest.raises(ValueError, match="enabled"):
        run(
            create_runtime(
                config_path,
                graphiti_client_factory=RecordingFactory(),
            )
        )


def test_enabled_config_without_uri_fails_at_load_before_any_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"graphiti": {"enabled": True, "group_id": "prism-spike"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="uri"):
        run(create_runtime(config_path))


def test_disabled_config_never_probes_dependencies_or_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config_path = tmp_path / "config.json"
    config = PrismConfig(
        graphiti=GraphitiConfig(
            uri="bolt://prism-graphiti-spike:7688",
            group_id="prism-spike",
        )
    )
    config.save(config_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())

    async def exercise():
        runtime = await create_runtime(config_path)
        try:
            assert isinstance(runtime.graph_backend, OfflineGraphBackend)
            assert runtime.graphiti_backend is None
        finally:
            await runtime.close()

    run(exercise())
