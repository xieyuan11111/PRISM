"""Application composition root for PRISM's local runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from prism.api import PrismAPI
from prism.analyzer import AnalyzerService
from prism.config import PathConfig, PrismConfig
from prism.events import EventBus
from prism.graph import GraphBackend, GraphEpisode, GraphService
from prism.ingestion import IngestionService
from prism.llm import (
    LLMRouter,
    LLMTransport,
    OpenAICompatibleTransport,
    Provider,
    TaskRoute,
)
from prism.report import ReportService
from prism.store import EvidenceStore


class OfflineGraphBackend:
    """Process-local graph storage used when no external backend is injected."""

    def __init__(self) -> None:
        self._episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self._episodes:
            return False
        self._episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        # GraphService owns filtering and temporal evaluation.  Returning the
        # local episodes keeps this backend deterministic and fully offline.
        return tuple(self._episodes.values())


def load_config(
    config_path: str | os.PathLike[str] | None = None,
) -> PrismConfig:
    """Load an explicit config or ``PRISM_HOME/config.json`` when it exists."""

    source = (
        Path(config_path).expanduser()
        if config_path is not None
        else PathConfig.prism_home() / "config.json"
    )
    if not source.exists():
        return PrismConfig()
    return PrismConfig.load(source)


def _compose_llm_router(
    config: PrismConfig,
    transport: LLMTransport | None,
) -> LLMRouter | None:
    """Build configured LLM routing without resolving credentials or doing I/O."""

    provider_configs = config.llm.providers
    task_roles = config.llm.task_roles
    if not provider_configs and not task_roles:
        return None
    if not provider_configs:
        raise ValueError("configured LLM task routes require at least one provider")
    if not task_roles:
        raise ValueError("configured LLM providers require task routes")

    providers: list[Provider] = []
    for name, provider_config in provider_configs.items():
        if provider_config.base_url is None:
            raise ValueError(f"LLM provider {name!r} requires base_url")
        if provider_config.api_key_env is None:
            raise ValueError(f"LLM provider {name!r} requires api_key_env")
        providers.append(
            Provider(
                name=name,
                base_url=provider_config.base_url,
                api_key_env=provider_config.api_key_env,
                default_model=provider_config.model,
                timeout=provider_config.timeout,
                concurrency_limit=provider_config.concurrency_limit,
            )
        )

    routes = [
        TaskRoute(role=role, providers=(provider_name,))
        for role, provider_name in task_roles.items()
    ]
    selected_transport = (
        transport if transport is not None else OpenAICompatibleTransport()
    )
    return LLMRouter(
        providers=providers,
        routes=routes,
        transport=selected_transport,
    )


@dataclass(slots=True)
class PrismRuntime:
    """Owned, initialized services behind one :class:`PrismAPI` facade."""

    config: PrismConfig
    paths: PathConfig
    ingestion_service: IngestionService
    evidence_store: EvidenceStore
    event_bus: EventBus
    graph_backend: GraphBackend
    graph_service: GraphService
    analyzer_service: AnalyzerService
    report_service: ReportService
    api: PrismAPI
    llm_router: LLMRouter | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def ingestion(self) -> IngestionService:
        return self.ingestion_service

    @property
    def store(self) -> EvidenceStore:
        return self.evidence_store

    @property
    def events(self) -> EventBus:
        return self.event_bus

    @property
    def graph(self) -> GraphService:
        return self.graph_service

    async def close(self) -> None:
        """Stop asynchronous workers and release the SQLite connection."""

        if self._closed:
            return
        self._closed = True
        try:
            await self.event_bus.stop()
        finally:
            self.evidence_store.close()

    async def __aenter__(self) -> PrismRuntime:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def create_runtime(
    config_path: str | os.PathLike[str] | None = None,
    *,
    graph_backend: GraphBackend | None = None,
    llm_transport: LLMTransport | None = None,
) -> PrismRuntime:
    """Construct and start a local runtime without implicit external clients."""

    config = load_config(config_path)
    llm_router = _compose_llm_router(config, llm_transport)
    paths = config.resolved_paths()
    for directory in (
        paths.data_dir,
        paths.cache_dir,
        paths.output_dir,
        paths.raw_dir,
        paths.corpus_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    backend: GraphBackend = (
        graph_backend if graph_backend is not None else OfflineGraphBackend()
    )
    if not isinstance(backend, GraphBackend):
        raise TypeError("graph_backend must provide add_episode() and search()")

    ingestion = IngestionService(paths)
    store = EvidenceStore(paths)
    events = EventBus()
    graph = GraphService(backend)
    analyzer = AnalyzerService(graph)
    report = ReportService(llm_router)
    try:
        store.initialize()
        await events.start()
    except BaseException:
        await events.stop()
        store.close()
        raise

    api = PrismAPI(
        ingestion,
        store,
        graph,
        events,
        analyzer_service=analyzer,
        report_service=report,
    )
    return PrismRuntime(
        config=config,
        paths=paths,
        ingestion_service=ingestion,
        evidence_store=store,
        event_bus=events,
        graph_backend=backend,
        graph_service=graph,
        analyzer_service=analyzer,
        report_service=report,
        llm_router=llm_router,
        api=api,
    )


__all__ = [
    "OfflineGraphBackend",
    "PrismRuntime",
    "create_runtime",
    "load_config",
]
