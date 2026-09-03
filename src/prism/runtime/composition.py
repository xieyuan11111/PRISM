"""Application composition root for PRISM's local runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
from typing import Any

from prism.api import PrismAPI
from prism.analyzer import AnalyzerService
from prism.cases import CaseBundleMerger, CaseService
from prism.cases.ledger import CaseExtractionLedger
from prism.config import GraphitiConfig, PathConfig, PrismConfig
from prism.domain import EvolutionCase, Material
from prism.events import EventBus
from prism.extraction import ExtractionEvidenceGap, ExtractionResult, ExtractionService
from prism.graph import (
    GraphBackend,
    GraphEpisode,
    GraphitiBackend,
    GraphService,
    SQLiteEpisodeRegistry,
)
from prism.graph.graphiti_client import build_graphiti_client, resolve_episode_type_json
from prism.ingestion import IngestionService
from prism.llm import (
    LLMRouter,
    LLMTransport,
    OpenAICompatibleTransport,
    Provider,
    TaskRoute,
)
from prism.pipeline import (
    MATERIAL_INGESTED,
    PipelineOutcomeLedger,
    PipelineService,
    StoreMaterialResolver,
)
from prism.report import ReportService
from prism.research import (
    FirecrawlJsonHttpClient,
    FirecrawlSearchProvider,
    ResearchExecutor,
    ResearchPlanner,
    SearchProvider,
)
from prism.sources import (
    CrossrefClient,
    EuropePmcClient,
    HttpGetter,
    OpenAlexClient,
    ScholarlyMetadataClient,
    SourceService,
    UrllibHttpGetter,
)
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

    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Path | None = None,
        target_case: EvolutionCase | None = None,
    ) -> ExtractionResult:
        return ExtractionResult(
            warnings=("no LLM router configured; structured extraction skipped",),
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "extraction_unavailable",
                    "no LLM router configured; no evolution candidates were produced",
                    source_ids=(material.id,),
                ),
            ),
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


def _graphiti_dependency_available() -> bool:
    """Whether the optional graphiti extra's packages are importable.

    Probing with ``find_spec`` never imports the packages, so the offline
    default stays dependency-free even when this probe runs (it only ever
    runs on the explicitly enabled path).
    """
    return (
        importlib.util.find_spec("graphiti_core") is not None
        and importlib.util.find_spec("neo4j") is not None
    )


def _compose_graphiti_backend(
    config: PrismConfig,
    graph_backend: GraphBackend | None,
    graphiti_client_factory: Callable[[GraphitiConfig], Any] | None,
    *,
    paths: PathConfig,
) -> tuple[
    GraphBackend, GraphitiBackend | None, SQLiteEpisodeRegistry | None
]:
    """Compose the graph backend for the configured Graphiti opt-in.

    Returns ``(backend, owned_graphiti_backend, owned_registry)``.
    ``owned_graphiti_backend``/``owned_registry`` are not None only when THIS
    call created a real :class:`GraphitiBackend` (and its project-owned
    persistent registry) from a client factory; the runtime must then close
    both on shutdown.  Injected ``graph_backend`` instances belong to the
    caller and are never closed by the runtime, and no registry is created
    next to them.

    The default (``graphiti.enabled=false``) path never imports
    graphiti-core/neo4j, never builds a client, never probes dependencies,
    never reads credential env vars and never creates a registry.  The
    enabled path only attempts the real client when a factory is injected or
    the optional dependencies are installed; anything missing fails with an
    explicit error before any service is touched.  When the real backend IS
    created, PRISM's own SQLite-backed episode registry
    (:class:`SQLiteEpisodeRegistry`) is created and injected too: it shares
    the EvidenceStore SQLite file under the data dir and persists the
    episode_key -> real Graphiti uuid mapping, so writes stay idempotent and
    body-less search results stay attributable across process restarts.
    """
    if not config.graphiti.enabled:
        if graphiti_client_factory is not None:
            raise ValueError("graphiti_client_factory requires graphiti.enabled=true")
        return (
            graph_backend if graph_backend is not None else OfflineGraphBackend(),
            None,
            None,
        )
    if graph_backend is not None and graphiti_client_factory is not None:
        raise ValueError(
            "graphiti_client_factory cannot be combined with graph_backend"
        )
    if graph_backend is not None:
        # Explicit backend injection is a full override: no client is built,
        # no dependency probe, no credential lookup and no registry.
        return graph_backend, None, None
    if graphiti_client_factory is None and not _graphiti_dependency_available():
        raise RuntimeError(
            "graphiti.enabled=true but the optional graphiti dependencies are "
            "not installed; install them with 'pip install -e \".[graphiti]\"' "
            "or inject graphiti_client_factory/graph_backend"
        )
    if graphiti_client_factory is not None:
        client = graphiti_client_factory(config.graphiti)
        if client is None:
            raise ValueError("graphiti_client_factory returned None")
    else:
        client = build_graphiti_client(config.graphiti)
    # The real path always gets PRISM's own persistent registry (never a
    # caller-supplied one): it records the PRISM key, the real Graphiti
    # uuid captured from each add result, the group/database and the
    # canonical body in the shared SQLite file under the data dir.  The
    # table is created additively, so pre-existing store databases migrate
    # without any change to their rows.
    registry = SQLiteEpisodeRegistry(
        paths, database=config.graphiti.database
    )
    backend = GraphitiBackend(
        client,
        group_id=config.graphiti.group_id,
        episode_type_json=resolve_episode_type_json(),
        registry=registry,
    )
    return backend, backend, registry


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
    research_executor: ResearchExecutor | None = None
    search_provider: SearchProvider | None = None
    source_service: SourceService | None = None
    llm_router: LLMRouter | None = None
    # New dependencies are appended after the original optional fields so
    # existing positional callers keep their argument order unchanged.
    scholarly_metadata_client: ScholarlyMetadataClient | None = None
    # Owned Graphiti backend created by the composition root (never the
    # caller-injected ``graph_backend``); closed by :meth:`close`.
    graphiti_backend: GraphitiBackend | None = None
    # Owned persistent episode registry (PRISM key -> real Graphiti uuid)
    # created by the composition root next to ``graphiti_backend``; closed
    # by :meth:`close`.  None on the offline default and when a caller
    # injected ``graph_backend`` (full override).
    graph_episode_registry: SQLiteEpisodeRegistry | None = None
    # Automatic evolution pipeline: the accumulated-case service, its durable
    # ledger, and the event subscription that feeds material.ingested events
    # into PipelineService.handle_event (subscribed before the bus starts and
    # unsubscribed by :meth:`close`).
    case_service: CaseService | None = None
    case_ledger: CaseExtractionLedger | None = None
    pipeline_subscription_id: str | None = None
    # Durable pipeline-outcome ledger (per-material failed/committed states in
    # the shared local SQLite file, hydrated into the pipeline at startup);
    # closed by :meth:`close`.  None on runtimes that injected no pipeline.
    pipeline_outcome_ledger: PipelineOutcomeLedger | None = None
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

    @property
    def dispatch_errors(self) -> tuple:
        """Every isolated event-subscriber failure recorded so far.

        Each entry is a :class:`~prism.events.DispatchError` carrying the
        subscription, the event (hence the material id and event time), the
        exception and the failure time.  For the automatic pipeline the
        material-level audit is also queryable through
        ``pipeline.failure_for(material_id)`` (stage, error type, message,
        time), the lifecycle state through ``pipeline.outcome_for``
        (``pending``/``failed``/``committed``, terminal states durable across
        restarts) and the completed outcome through ``pipeline.run_for`` —
        a failure is never reported as a completed run."""

        return self.event_bus.errors

    async def close(self) -> None:
        """Stop asynchronous workers, release the SQLite connection and close
        every real resource this runtime created (an owned Graphiti client
        and its persistent episode registry included).  The automatic
        pipeline's event subscription is removed first — draining any
        in-flight ``material.ingested`` event before the bus stops — so one
        ingestion can never lose its index→extract→graph processing to a
        shutdown race.  Idempotent; caller-injected services stay open."""

        if self._closed:
            return
        self._closed = True
        try:
            if self.pipeline_subscription_id is not None:
                await self.event_bus.unsubscribe(self.pipeline_subscription_id)
        finally:
            try:
                await self.event_bus.stop()
            finally:
                try:
                    self.evidence_store.close()
                finally:
                    try:
                        if self.graphiti_backend is not None:
                            await self.graphiti_backend.close()
                    finally:
                        try:
                            if self.graph_episode_registry is not None:
                                self.graph_episode_registry.close()
                        finally:
                            if self.case_ledger is not None:
                                self.case_ledger.close()
                            if self.pipeline_outcome_ledger is not None:
                                self.pipeline_outcome_ledger.close()

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
    firecrawl_client: object | None = None,
    graphiti_client_factory: Callable[[GraphitiConfig], Any] | None = None,
    extraction_service: ExtractionService | OfflineExtractor | None = None,
) -> PrismRuntime:
    """Construct and start a local runtime without implicit external clients.

    The default configuration is fully offline.  A caller may inject a
    ``http_getter`` and/or ``search_provider`` for controlled integrations;
    setting ``firecrawl.enabled`` is the explicit opt-in that creates the
    standard-library Firecrawl client and public GET transport.  Credentials
    are resolved from the configured environment variable and never stored in
    :class:`~prism.config.PrismConfig`.

    ``graphiti.enabled=true`` is the opt-in for the real Graphiti/Neo4j spike
    path: it only attempts a client when ``graphiti_client_factory`` is
    injected or the optional ``[graphiti]`` dependencies are installed, and
    composition itself never connects to any service.  The enabled path also
    creates PRISM's own SQLite-backed episode registry (episode_key -> real
    Graphiti uuid) under the data dir and injects it into the backend, so
    duplicate writes and body-less search attribution stay correct across
    process restarts; :meth:`PrismRuntime.close` closes both.  A
    ``graph_backend`` injection is a full override that stays fully offline
    and creates no registry.

    The automatic evolution pipeline is wired unconditionally from local
    resources only: the store-backed material resolver, the durable
    :class:`~prism.cases.ledger.CaseExtractionLedger` and the accumulating
    :class:`~prism.cases.CaseService`.  ``PipelineService.handle_event`` is
    subscribed to ``material.ingested`` BEFORE the event bus starts, so one
    ingestion automatically runs index → extract → merged-case graph write
    without any manual pipeline call.  ``PrismAPI.ingest_material`` is the
    asynchronous path — it returns once the material is ingested and
    indexed, with automatic processing queued (query its outcome through
    ``PrismAPI.process_material(material_id)``, which waits and reports the
    authoritative pipeline/case result, or through
    ``pipeline.run_for``/``pipeline.failure_for``/``pipeline.outcome_for``);
    ``process_material`` is the synchronous path and never reports success
    before the pipeline and case outcome exist.  Every material's lifecycle
    is queryable as ``pending``/``failed``/``committed`` through
    ``pipeline.outcome_for``, and terminal states are persisted in the local
    ``pipeline_outcomes`` table of the shared SQLite file (hydrated on the
    next start) — a local, single-process-file ledger, not a cross-process
    outbox.  Subscriber failures are isolated, recorded with
    time/stage/error type and visible as :attr:`PrismRuntime.dispatch_errors`
    plus per-material ``pipeline.failure_for``/``pipeline.outcome_for``
    records — they never corrupt the publisher or fake a completed run, and
    :meth:`PrismRuntime.close` unsubscribes the pipeline subscriber after
    draining in-flight events.
    An injected ``extraction_service`` is a full override of the default
    router-less/LLM-backed extraction choice, for controlled offline tests
    and embedders; composition itself never calls it.
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

    backend, owned_graphiti_backend, owned_registry = _compose_graphiti_backend(
        config, graph_backend, graphiti_client_factory, paths=paths
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
        extraction_service
        if extraction_service is not None
        else (
            ExtractionService(llm_router, evidence_locator=store.locate)
            if llm_router is not None
            else OfflineExtractor()
        )
    )
    if not callable(getattr(extraction, "extract", None)):
        raise TypeError(
            "extraction_service must provide extract()/extract_material()"
        )
    # Automatic evolution pipeline: resolve events from the authoritative
    # store, accumulate successful extractions durably per case, and write
    # merged case bundles (never one full case per material).  Terminal
    # per-material pipeline outcomes (failed/committed) are persisted in the
    # shared local SQLite file so failures stay auditable after a restart.
    resolver = StoreMaterialResolver(store, paths)
    case_ledger = CaseExtractionLedger(paths)
    case_service = CaseService(
        ledger=case_ledger, merger=CaseBundleMerger(), graph_service=graph
    )
    pipeline_outcome_ledger = PipelineOutcomeLedger(paths)
    pipeline = PipelineService(
        indexer=store,
        extraction_service=extraction,
        graph_service=graph,
        material_resolver=resolver,
        case_service=case_service,
        outcome_store=pipeline_outcome_ledger,
    )
    research_planner = ResearchPlanner(config, router=llm_router)

    effective_provider = search_provider
    effective_http_getter = http_getter
    research_timeout = 10.0
    if firecrawl_client is not None and search_provider is not None:
        raise ValueError(
            "firecrawl_client cannot be combined with search_provider"
        )
    if config.firecrawl.enabled:
        effective_http_getter = effective_http_getter or UrllibHttpGetter()
        research_timeout = config.firecrawl.timeout
        if effective_provider is None:
            client = firecrawl_client if firecrawl_client is not None else FirecrawlJsonHttpClient()
            api_key = os.environ.get(config.firecrawl.api_key_env, "")
            effective_provider = FirecrawlSearchProvider(
                config,
                client=client,
                api_key=api_key,
                base_url=config.firecrawl.base_url,
                limit=config.firecrawl.limit,
            )
    elif firecrawl_client is not None:
        raise ValueError("firecrawl_client requires firecrawl.enabled=true")

    source_service = (
        SourceService(config, getter=effective_http_getter)
        if effective_http_getter is not None
        else None
    )
    research_executor = None
    scholarly_metadata_client = None
    if effective_http_getter is not None:
        scholarly_metadata_client = ScholarlyMetadataClient(
            CrossrefClient(effective_http_getter),
            OpenAlexClient(effective_http_getter),
            EuropePmcClient(effective_http_getter),
            clock=lambda: datetime.now(timezone.utc),
        )
    if effective_provider is not None:
        if source_service is None:
            raise ValueError(
                "search_provider requires http_getter or firecrawl.enabled=true"
            )

    try:
        # The pipeline subscriber must exist before the bus starts so no
        # material.ingested event published after start() can be missed.
        pipeline_subscription_id = events.subscribe(
            MATERIAL_INGESTED, pipeline.handle_event
        )
        store.initialize()
        await events.start()
    except BaseException:
        if owned_graphiti_backend is not None:
            await owned_graphiti_backend.close()
        if owned_registry is not None:
            owned_registry.close()
        case_ledger.close()
        pipeline_outcome_ledger.close()
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
        search_provider=effective_provider,
        scholarly_metadata_client=scholarly_metadata_client,
        extraction_service=extraction,
        case_service=case_service,
        material_resolver=resolver,
    )
    if effective_provider is not None:
        research_executor = ResearchExecutor(
            effective_provider, api, search_timeout=research_timeout
        )
        api._research_executor = research_executor
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
        research_executor=research_executor,
        scholarly_metadata_client=scholarly_metadata_client,
        search_provider=effective_provider,
        source_service=source_service,
        llm_router=llm_router,
        graphiti_backend=owned_graphiti_backend,
        graph_episode_registry=owned_registry,
        case_service=case_service,
        case_ledger=case_ledger,
        pipeline_subscription_id=pipeline_subscription_id,
        pipeline_outcome_ledger=pipeline_outcome_ledger,
    )


__all__ = [
    "OfflineExtractor",
    "OfflineGraphBackend",
    "PrismRuntime",
    "create_runtime",
    "load_config",
]
