"""Application composition root for PRISM's local runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from prism.api import PrismAPI
from prism.analyzer import AnalyzerService
from prism.config import PathConfig, PrismConfig
from prism.domain import Material
from prism.events import EventBus
from prism.extraction import ExtractionResult, ExtractionService
from prism.graph import GraphBackend, GraphEpisode, GraphService
from prism.ingestion import IngestionService
from prism.llm import (
    LLMRouter,
    LLMTransport,
    OpenAICompatibleTransport,
    Provider,
    TaskRoute,
)
from prism.pipeline import PipelineService
from prism.report import ReportService
from prism.research import ResearchPlanner, SearchProvider
from prism.sources import HttpGetter, SourceService
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


class OfflineExtractor:
    """Deterministic extraction stand-in used when no LLM router is configured.

    It performs no I/O and produces no domain objects: the returned
    :class:`~prism.extraction.ExtractionResult` carries an explicit warning
    so pipeline audit trails record why structured extraction was skipped
    instead of silently fabricating one.  Configuring any LLM provider
    replaces it with the real :class:`~prism.extraction.ExtractionService`.
    """

    name = "offline"

    async def extract(self, material: Material) -> ExtractionResult:
        return ExtractionResult(
            warnings=("no LLM router configured; structured extraction skipped",)
        )


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
    extraction_service: ExtractionService | OfflineExtractor
    pipeline_service: PipelineService
    research_planner: ResearchPlanner
    search_provider: SearchProvider | None = None
    source_service: SourceService | None = None
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

    @property
    def extraction(self) -> ExtractionService | OfflineExtractor:
        return self.extraction_service

    @property
    def pipeline(self) -> PipelineService:
        return self.pipeline_service

    @property
    def sources(self) -> SourceService | None:
        return self.source_service

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
    http_getter: HttpGetter | None = None,
    search_provider: SearchProvider | None = None,
) -> PrismRuntime:
    """Construct and start a local runtime without implicit external clients.

    ``http_getter`` is the only way to arm source collection: without an
    explicitly injected getter no ``SourceService`` is created at all, so the
    default runtime can never issue a real network request.
    """

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
    extraction: ExtractionService | OfflineExtractor = (
        ExtractionService(llm_router) if llm_router is not None else OfflineExtractor()
    )
    pipeline = PipelineService(
        indexer=store, extraction_service=extraction, graph_service=graph
    )
    research_planner = ResearchPlanner(config, router=llm_router)
    source_service = (
        SourceService(config, getter=http_getter)
        if http_getter is not None
        else None
    )
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
        source_service=source_service,
        pipeline_service=pipeline,
        source_raw_dir=paths.raw_dir,
        research_planner=research_planner,
        search_provider=search_provider,
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
        api=api,
        extraction_service=extraction,
        pipeline_service=pipeline,
        research_planner=research_planner,
        search_provider=search_provider,
        source_service=source_service,
        llm_router=llm_router,
    )


__all__ = [
    "OfflineExtractor",
    "OfflineGraphBackend",
    "PrismRuntime",
    "create_runtime",
    "load_config",
]
