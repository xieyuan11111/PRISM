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
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prism.config import GraphitiConfig, PrismConfig
from prism.graph import GraphEpisode, GraphitiBackend, SQLiteEpisodeRegistry
from prism.runtime import OfflineGraphBackend, create_runtime

from graphiti_fakes import FakeGraphStore, FakeGraphitiClient, NestedResultClient

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def enabled_config(tmp_path, *, uri="bolt://prism-graphiti-spike:7688") -> PrismConfig:
    # Phase B Community shape: group_id == database == the container's single
    # built-in database "neo4j" (graphiti-core 0.29.3 realises a Neo4j group
    # as a database, so the config validation requires the two to be equal).
    return PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri=uri,
            database="neo4j",
            group_id="neo4j",
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
            database="neo4j",
            group_id="neo4j",
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
            assert runtime.graphiti_backend.group_id == "neo4j"
            assert factory.calls == [config.graphiti]
            client = factory.clients[-1]

            # No credential env is needed on the injected-factory path: the
            # factory owns credential handling for its own client.
            assert await runtime.graph_backend.add_episode(episode()) is True
            assert client.add_calls[-1]["group_id"] == "neo4j"
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
        json.dumps({"graphiti": {"enabled": True, "group_id": "neo4j"}}),
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
            database="neo4j",
            group_id="neo4j",
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


# ---------------------------------------------------------------------------
# Persistent SQLite registry lifecycle in the composition root (Phase B).
# ---------------------------------------------------------------------------


def test_enabled_factory_runtime_creates_and_closes_the_sqlite_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    graph_store = FakeGraphStore()

    def factory(config: GraphitiConfig) -> FakeGraphitiClient:
        # Real 0.29.3 search shape: body-less EntityEdge results, so restart
        # attribution depends on the persisted registry mapping.
        return NestedResultClient(graph_store, with_body=False)

    async def exercise():
        first = await create_runtime(config_path, graphiti_client_factory=factory)
        try:
            assert isinstance(first.graph_backend, GraphitiBackend)
            assert first.graphiti_backend is first.graph_backend
            registry = first.graph_episode_registry
            assert isinstance(registry, SQLiteEpisodeRegistry)
            assert registry.closed is False
            # The registry shares the EvidenceStore SQLite file under the
            # configured data dir.
            assert registry.db_path == (
                tmp_path / "home" / "data" / "index.db"
            ).resolve()

            ep = episode()
            assert await first.graph_backend.add_episode(ep) is True
            assert await first.graph_backend.add_episode(ep) is False
            assert len(graph_store.episodes) == 1
        finally:
            await first.close()

        # close() closed the owned backend AND the registry it created.
        assert registry.closed is True

        # Restart: a fresh runtime, fresh client and fresh registry object on
        # the same database file must short-circuit the duplicate write and
        # map body-less EntityEdge results through the persisted mapping.
        second = await create_runtime(config_path, graphiti_client_factory=factory)
        try:
            assert await second.graph_backend.add_episode(episode()) is False
            assert len(graph_store.episodes) == 1
            results = await second.graph_backend.search("prism query")
            assert results == (episode(),)
        finally:
            await second.close()

    run(exercise())


def test_enabled_runtime_restart_timeline_stays_stable_with_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    graph_store = FakeGraphStore()

    def factory(config: GraphitiConfig) -> FakeGraphitiClient:
        return NestedResultClient(graph_store, with_body=False)

    async def exercise():
        first = await create_runtime(config_path, graphiti_client_factory=factory)
        ep = episode()
        try:
            await first.graph_backend.add_episode(ep)
            before = await first.graph.timeline(
                "case", NOW + timedelta(days=1)
            )
        finally:
            await first.close()

        second = await create_runtime(config_path, graphiti_client_factory=factory)
        try:
            # The duplicate write across the restart is a no-op...
            await second.graph_backend.add_episode(ep)
            after = await second.graph.timeline("case", NOW + timedelta(days=1))
        finally:
            await second.close()

        assert [entry.episode_key for entry in after.entries] == [
            entry.episode_key for entry in before.entries
        ]
        assert after.entries and after.entries[0].kind == "claim"

    run(exercise())


def test_offline_default_creates_no_registry_and_no_registry_table(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        runtime = await create_runtime()
        try:
            assert runtime.graph_episode_registry is None
        finally:
            await runtime.close()

    run(exercise())

    # The EvidenceStore database exists offline, but the registry table must
    # not: the default path never creates a registry or touches its schema.
    db = tmp_path / "home" / "data" / "index.db"
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "documents" in tables
    assert "graphiti_episode_registry" not in tables


def test_enabled_with_injected_backend_override_creates_no_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PRISM_GRAPHITI_PASSWORD", raising=False)
    config = enabled_config(tmp_path)
    config_path = tmp_path / "config.json"
    config.save(config_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    injected = FakeGraphBackend()

    async def exercise():
        runtime = await create_runtime(config_path, graph_backend=injected)
        try:
            assert runtime.graph_backend is injected
            assert runtime.graphiti_backend is None
            # A caller-injected backend is a full override: the runtime does
            # not create its own registry beside it.
            assert runtime.graph_episode_registry is None
        finally:
            await runtime.close()

    run(exercise())
