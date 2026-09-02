"""Focused composition-root tests for PRISM's offline runtime."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import StringIO

import pytest

from prism.cli import main as cli_main
from prism.config import FirecrawlConfig, PathConfig, PrismConfig, SourceConfig
from prism.research import FirecrawlSearchProvider, ResearchExecutor
from prism.sources import HttpResponse, ScholarlyMetadataClient
from prism.events import Event
from prism.graph import GraphEpisode
from prism.runtime import OfflineGraphBackend, PrismRuntime, create_runtime


class FakeFirecrawlClient:
    async def post(self, url, *, headers, json_body, timeout):
        raise AssertionError("network client must not be called during composition")

class FakeSearchProvider:
    name = "fake"

    async def search(self, query, *, timeout=10.0):
        return ()

class FakeGraphBackend:
    """Small injected backend that cannot access a network or an LLM."""

    def __init__(self) -> None:
        self.episodes: dict[str, GraphEpisode] = {}
        self.search_calls: list[str] = []

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        self.search_calls.append(query)
        return tuple(self.episodes.values())


def run(coro):
    return asyncio.run(coro)


def test_enabled_firecrawl_is_composed_only_with_explicit_client(tmp_path, monkeypatch):
    home = tmp_path / "firecrawl-home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    monkeypatch.setenv("LOCAL_FIRECRAWL_KEY", "test-only-key")
    config = PrismConfig(
        sources=SourceConfig(("example.gov",)),
        firecrawl=FirecrawlConfig(
            enabled=True,
            api_key_env="LOCAL_FIRECRAWL_KEY",
            base_url="https://firecrawl.example.test",
        ),
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)

    async def exercise():
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            firecrawl_client=FakeFirecrawlClient(),
        )
        try:
            assert isinstance(runtime.search_provider, FirecrawlSearchProvider)
            assert isinstance(runtime.research_executor, ResearchExecutor)
            assert runtime.source_service is not None
            assert runtime.scholarly_metadata_client is not None
            assert isinstance(runtime.scholarly_metadata_client, ScholarlyMetadataClient)
            assert runtime.api._search_provider is runtime.search_provider
        finally:
            await runtime.close()

    run(exercise())


def test_runtime_optional_fields_keep_legacy_positional_order(tmp_path, monkeypatch):
    home = tmp_path / "positional-home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    monkeypatch.setenv("LOCAL_FIRECRAWL_KEY", "test-only-key")
    config = PrismConfig(
        sources=SourceConfig(("example.gov",)),
        firecrawl=FirecrawlConfig(
            enabled=True,
            api_key_env="LOCAL_FIRECRAWL_KEY",
            base_url="https://firecrawl.example.test",
        ),
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)

    async def exercise():
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            firecrawl_client=FakeFirecrawlClient(),
        )
        try:
            # PrismRuntime historically ended with ..., research_executor,
            # search_provider, source_service, llm_router as positional
            # defaults; scholarly_metadata_client must be appended after them
            # so legacy positional callers keep their original slots.
            rebuilt = PrismRuntime(
                runtime.config,
                runtime.paths,
                runtime.ingestion_service,
                runtime.evidence_store,
                runtime.event_bus,
                runtime.graph_backend,
                runtime.graph_service,
                runtime.analyzer_service,
                runtime.report_service,
                runtime.api,
                runtime.extraction_service,
                runtime.pipeline_service,
                runtime.research_planner,
                runtime.research_executor,
                runtime.search_provider,
                runtime.source_service,
                runtime.llm_router,
            )
            assert rebuilt.research_executor is runtime.research_executor
            assert rebuilt.search_provider is runtime.search_provider
            assert rebuilt.source_service is runtime.source_service
            assert rebuilt.llm_router is runtime.llm_router
            assert rebuilt.scholarly_metadata_client is None
            assert rebuilt.scholarly_metadata_client is not runtime.scholarly_metadata_client

            # The new dependency is reachable as the appended optional field.
            appended = PrismRuntime(
                runtime.config,
                runtime.paths,
                runtime.ingestion_service,
                runtime.evidence_store,
                runtime.event_bus,
                runtime.graph_backend,
                runtime.graph_service,
                runtime.analyzer_service,
                runtime.report_service,
                runtime.api,
                runtime.extraction_service,
                runtime.pipeline_service,
                runtime.research_planner,
                runtime.research_executor,
                runtime.search_provider,
                runtime.source_service,
                runtime.llm_router,
                runtime.scholarly_metadata_client,
            )
            assert appended.scholarly_metadata_client is runtime.scholarly_metadata_client
        finally:
            await runtime.close()

    run(exercise())


def test_disabled_firecrawl_does_not_create_provider_or_source_network_client(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "offline-home"))

    async def exercise():
        runtime = await create_runtime(graph_backend=FakeGraphBackend())
        try:
            assert runtime.search_provider is None
            assert runtime.source_service is None
            assert runtime.scholarly_metadata_client is None
        finally:
            await runtime.close()

    run(exercise())


class FakeScholarlyGetter:
    """Public transport fake: blocked publisher page, valid Crossref answer."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float):
        self.calls.append(url)
        if url.startswith("https://api.crossref.org/"):
            body = json.dumps(
                {
                    "message": {
                        "title": ["Composed fallback work"],
                        "DOI": "10.1007/s11783-021-1503-6",
                    }
                }
            )
            return HttpResponse(url, 200, body, "application/json")
        return HttpResponse(url, 403, "")


def test_composed_scholarly_fallback_resolves_doi_metadata(tmp_path, monkeypatch):
    """Regression: the composed client's clock must work when fallback fires.

    ``create_runtime`` wires ``ScholarlyMetadataClient`` with a real clock;
    exercising the composed client (blocked page -> Crossref) must produce an
    honestly labeled metadata item, not crash inside the clock callable.
    """

    home = tmp_path / "scholarly-home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    config = PrismConfig(sources=SourceConfig(("link.springer.com",)))
    config_path = tmp_path / "config.json"
    config.save(config_path)
    getter = FakeScholarlyGetter()

    async def exercise():
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            http_getter=getter,
        )
        try:
            report = await runtime.api.fetch_source(
                "https://link.springer.com/article/10.1007/s11783-021-1503-6",
                process=False,
            )
            # The scholarly path reports the resolved record link (Crossref
            # falls back to the canonical percent-encoded doi.org URL), not
            # the blocked page.
            assert report.url == "https://doi.org/10.1007%2Fs11783-021-1503-6"
            assert len(report.items) == 1
            item = report.items[0]
            assert item.access_level == "metadata_only"
            assert "api.crossref.org/works/" in " ".join(getter.calls)
        finally:
            await runtime.close()

    run(exercise())

def test_firecrawl_client_and_custom_provider_conflict_is_rejected(
    tmp_path, monkeypatch
):
    home = tmp_path / "conflict-home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    monkeypatch.setenv("LOCAL_FIRECRAWL_KEY", "test-only-key")
    config = PrismConfig(
        firecrawl=FirecrawlConfig(
            enabled=True,
            api_key_env="LOCAL_FIRECRAWL_KEY",
        )
    )
    config_path = tmp_path / "config.json"
    config.save(config_path)

    with pytest.raises(ValueError, match="cannot be combined"):
        run(
            create_runtime(
                config_path,
                graph_backend=FakeGraphBackend(),
                search_provider=FakeSearchProvider(),
                firecrawl_client=FakeFirecrawlClient(),
            )
        )
def test_explicit_config_is_loaded_and_paths_resolve_under_prism_home(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    explicit = tmp_path / "chosen-config.json"
    PrismConfig(
        paths=PathConfig(
            data_dir="state/index",
            cache_dir="state/cache",
            output_dir="results",
            raw_dir="sources",
            corpus_dir="documents",
        )
    ).save(explicit)
    PrismConfig(paths=PathConfig(data_dir="wrong")).save(home / "config.json")

    async def exercise():
        runtime = await create_runtime(explicit, graph_backend=FakeGraphBackend())
        try:
            assert isinstance(runtime, PrismRuntime)
            assert runtime.config == PrismConfig.load(explicit)
            assert runtime.paths.data_dir == (home / "state/index").resolve()
            assert runtime.paths.cache_dir == (home / "state/cache").resolve()
            assert runtime.paths.output_dir == (home / "results").resolve()
            assert runtime.paths.raw_dir == (home / "state/sources").resolve()
            assert runtime.paths.corpus_dir == (home / "state/documents").resolve()
            assert runtime.graph_service.backend is runtime.graph_backend
        finally:
            await runtime.close()

    run(exercise())


def test_prism_home_config_is_used_when_no_explicit_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    expected = PrismConfig(paths=PathConfig(output_dir="published"))
    expected.save(home / "config.json")

    async def exercise():
        runtime = await create_runtime(graph_backend=FakeGraphBackend())
        try:
            assert runtime.config == expected
            assert runtime.paths.output_dir == (home / "published").resolve()
        finally:
            await runtime.close()

    run(exercise())


def test_absent_config_uses_secret_free_offline_defaults_and_creates_directories(
    tmp_path, monkeypatch
):
    home = tmp_path / "empty-home"
    monkeypatch.setenv("PRISM_HOME", str(home))

    async def exercise():
        async with await create_runtime() as runtime:
            assert runtime.config == PrismConfig()
            assert runtime.config.llm.providers == {}
            assert runtime.config.sources.whitelist == ()
            assert isinstance(runtime.graph_backend, OfflineGraphBackend)
            assert runtime.evidence_store.db_path.is_file()
            for directory in (
                runtime.paths.data_dir,
                runtime.paths.cache_dir,
                runtime.paths.output_dir,
                runtime.paths.raw_dir,
                runtime.paths.corpus_dir,
            ):
                assert directory.is_dir()

    run(exercise())


def test_runtime_owns_started_bus_and_closes_bus_and_store_without_leaked_worker(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "home"))

    async def exercise():
        runtime = await create_runtime(graph_backend=FakeGraphBackend())
        received: list[str] = []

        async def handler(event: Event) -> None:
            received.append(event.event_id)

        runtime.event_bus.subscribe("test.event", handler)
        event = Event(
            event_id="event-1",
            event_type="test.event",
            occurred_at=datetime.now(timezone.utc),
            payload={},
            correlation_id=None,
        )
        await runtime.event_bus.publish(event)
        await runtime.close()
        await runtime.close()

        assert received == ["event-1"]
        assert runtime.evidence_store._connection is None
        assert not any(
            task.get_name().startswith("prism.events:") and not task.done()
            for task in asyncio.all_tasks()
        )
        with pytest.raises(RuntimeError, match="not running"):
            await runtime.event_bus.publish(event)

    run(exercise())


def test_cli_builds_and_closes_default_runtime_when_api_is_not_injected(
    tmp_path, monkeypatch
):
    home = tmp_path / "cli-home"
    monkeypatch.setenv("PRISM_HOME", str(home))
    stdout = StringIO()
    stderr = StringIO()

    status = run(
        cli_main(
            ["search", "nothing-indexed"],
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert status == 0
    assert stdout.getvalue() == "[]\n"
    assert stderr.getvalue() == ""
    database = home / "data" / "index.db"
    database.unlink()
    assert not database.exists()
