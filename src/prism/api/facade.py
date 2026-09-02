"""Unified application API shared by PRISM's CLI and WebUI."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from uuid import uuid4

from prism.analyzer import EvolutionAnalysis, HistoricalCaseState
from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.events import Event
from prism.graph import GraphTimeline, GraphWriteResult
from prism.ingestion import IngestionResult
from prism.report import ReportDocument, ReportService
from prism.sources import (
    ScholarlyMetadataClient,
    SourceFetchError,
    SourceItem,
    extract_doi,
    extract_pmcid,
    extract_pmid,
)
from prism.store import IndexEntry, IndexOutcome, SearchFilter

if TYPE_CHECKING:
    from prism.extraction import ExtractionResult
    from prism.research import ResearchExecutionReport, ResearchPlan

from .fetching import (
    SPOOL_DIRNAME,
    SourceBatchReport,
    SourceFetchReport,
    SourceItemReport,
    SourceURLFailure,
    spool_source_item,
)


_Dependency = TypeVar("_Dependency")


class _IngestionService(Protocol):
    def ingest(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> IngestionResult: ...


class _EvidenceStore(Protocol):
    def index_file(self, path: str | Path) -> IndexOutcome: ...

    def get(self, source_id: str) -> IndexEntry | None: ...

    def search(self, criteria: SearchFilter, *, limit: int, offset: int) -> object: ...


class _GraphService(Protocol):
    async def timeline(self, case_id: str, as_of: datetime) -> GraphTimeline: ...

    async def add_case(
        self,
        case: EvolutionCase,
        *,
        nodes: Iterable[EvolutionNode] = (),
        facts: Iterable[TemporalFact] = (),
        claims: Iterable[Claim] = (),
        materials: Iterable[Material] = (),
    ) -> GraphWriteResult: ...


class _EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...


class _AnalyzerService(Protocol):
    async def analyze(
        self,
        case_id: str,
        as_of: datetime | None = None,
        *,
        kinds: Iterable[str] | None = None,
    ) -> EvolutionAnalysis: ...

    async def state(
        self, case_id: str, cutoff_at: datetime
    ) -> HistoricalCaseState: ...


class _ReportService(Protocol):
    async def report(self, analysis: EvolutionAnalysis) -> ReportDocument: ...


class _SourceService(Protocol):
    async def fetch(self, url: str, *, kind: str = ...) -> object: ...


class _ScholarlyMetadataClient(Protocol):
    async def fetch(self, value: str) -> SourceItem: ...

    async def fetch_by_title(
        self, title: str, *, link: str | None = ...
    ) -> SourceItem: ...


class _PipelineService(Protocol):
    async def run_material(
        self, result: IngestionResult, *, correlation_id: str | None = ...
    ) -> object: ...


class _ExtractionService(Protocol):
    async def extract_material(
        self, material: Material, *, corpus_path: str | Path | None = ...
    ) -> ExtractionResult: ...


class _ResearchPlanner(Protocol):
    async def plan(
        self,
        material: Material,
        extraction: ExtractionResult | None = None,
        *,
        core_claims: Iterable[str] = (),
        evidence_boundaries: Iterable[str] = (),
    ) -> ResearchPlan: ...


class _ResearchExecutor(Protocol):
    async def execute(
        self, plan: ResearchPlan, *, process: bool = True
    ) -> ResearchExecutionReport: ...


class _SearchProvider(Protocol):
    async def search(self, query: object, *, timeout: float = ...) -> object: ...


class _MaterialLookup(Protocol):
    def get(self, source_id: str) -> IndexEntry | None: ...


def _required_dependency(
    name: str, dependency: _Dependency | None, method: str
) -> _Dependency:
    if dependency is None:
        raise ValueError(f"{name} is required")
    if not callable(getattr(dependency, method, None)):
        raise TypeError(f"{name} must provide {method}()")
    return dependency


def _optional_dependency(
    name: str, dependency: _Dependency | None, method: str
) -> _Dependency | None:
    if dependency is None:
        return None
    if not callable(getattr(dependency, method, None)):
        raise TypeError(f"{name} must provide {method}()")
    return dependency


class PrismAPI:
    """Small dependency-injected facade over PRISM application services.

    All public operations are async so callers have one consistent boundary.
    The injected event bus remains caller-owned and must already be running.
    """

    def __init__(
        self,
        ingestion_service: _IngestionService,
        evidence_store: _EvidenceStore,
        graph_service: _GraphService,
        event_bus: _EventBus,
        *,
        analyzer_service: _AnalyzerService | None = None,
        report_service: _ReportService | None = None,
        source_service: _SourceService | None = None,
        pipeline_service: _PipelineService | None = None,
        source_raw_dir: str | os.PathLike[str] | None = None,
        research_planner: _ResearchPlanner | None = None,
        search_provider: _SearchProvider | None = None,
        research_executor: _ResearchExecutor | None = None,
        research_intake: object | None = None,
        scholarly_metadata_client: _ScholarlyMetadataClient | None = None,
        extraction_service: _ExtractionService | None = None,
    ) -> None:
        self._ingestion = _required_dependency(
            "ingestion_service", ingestion_service, "ingest"
        )
        self._store = _required_dependency(
            "evidence_store", evidence_store, "index_file"
        )
        _required_dependency("evidence_store", evidence_store, "search")
        self._graph = _required_dependency(
            "graph_service", graph_service, "timeline"
        )
        _required_dependency("graph_service", graph_service, "add_case")
        self._events = _required_dependency("event_bus", event_bus, "publish")
        self._analyzer = _optional_dependency(
            "analyzer_service", analyzer_service, "analyze"
        )
        self._report = _optional_dependency(
            "report_service", report_service, "report"
        )
        self._source = _optional_dependency(
            "source_service", source_service, "fetch"
        )
        self._pipeline = _optional_dependency(
            "pipeline_service", pipeline_service, "run_material"
        )
        if source_raw_dir is None:
            self._source_raw_dir: Path | None = None
        elif isinstance(source_raw_dir, (str, os.PathLike)):
            self._source_raw_dir = Path(source_raw_dir)
        else:
            raise TypeError("source_raw_dir must be path-like")
        self._offline_report: _ReportService | None = None
        self._research_planner = _optional_dependency(
            "research_planner", research_planner, "plan"
        )
        self._search_provider = _optional_dependency(
            "search_provider", search_provider, "search"
        )
        self._research_executor = _optional_dependency(
            "research_executor", research_executor, "execute"
        )
        if research_intake is not None and not callable(
            getattr(research_intake, "fetch_source", None)
        ):
            raise TypeError("research_intake must provide fetch_source()")
        self._research_intake = research_intake
        self._scholarly = _optional_dependency(
            "scholarly_metadata_client", scholarly_metadata_client, "fetch"
        )
        self._extraction = _optional_dependency(
            "extraction_service", extraction_service, "extract_material"
        )

    async def search(
        self,
        query: str | None = None,
        *,
        case_tag: str | None = None,
        source: str | None = None,
        type: str | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> object:
        """Search evidence through the configured SQLite/FTS store."""
        criteria = SearchFilter(
            query=query,
            case_tag=case_tag,
            source=source,
            type=type,
            published_after=published_after,
            published_before=published_before,
        )
        return self._store.search(criteria, limit=limit, offset=offset)

    async def ingest_material(
        self,
        path: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> IngestionResult:
        """Normalize and index one material, then publish its completion event."""
        result = self._ingestion.ingest(path, metadata)
        index_result = self._store.index_file(result.corpus_path)
        event = Event(
            event_id=f"material-ingested-{uuid4()}",
            event_type="material.ingested",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "material_id": result.material.id,
                "corpus_path": str(result.corpus_path),
                "index_status": index_result.status,
            },
            correlation_id=result.material.id,
        )
        await self._events.publish(event)
        return result

    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Path | None = None,
    ) -> ExtractionResult:
        """Run the evidence-bound Evolution Extraction v0 public entry point."""

        if self._extraction is None:
            raise ValueError("extraction_service is required for extract_material()")
        return await self._extraction.extract_material(
            material, corpus_path=corpus_path
        )

    async def fetch_source(
        self,
        url: str,
        *,
        kind: str = "auto",
        process: bool = True,
    ) -> SourceFetchReport:
        """Fetch one whitelisted public URL and route every new item inward.

        Each collected :class:`~prism.sources.SourceItem` is spooled to a
        safe file under ``source_raw_dir``, normalized into the corpus by the
        injected :class:`~prism.ingestion.IngestionService` (never bypassed),
        optionally pushed through the injected pipeline service, and announced
        on the event bus.  Fetch failures raise the classified
        :class:`~prism.sources.SourceFetchError` unchanged; nothing is ever
        reported as fetched when it was not.
        """
        source = self._fetch_dependencies(process)
        try:
            fetch_result = await source.fetch(url, kind=kind)
        except SourceFetchError:
            # The scholarly fallback only fires for inputs that carry a real
            # scholarly identifier (DOI, PMCID, or PMID) — never for pages
            # that merely look academic.
            if self._scholarly is None or (
                extract_doi(url) is None
                and extract_pmcid(url) is None
                and extract_pmid(url) is None
            ):
                raise
            item = await self._scholarly.fetch(url)
            item_report = await self._intake_source_item(item, process=process)
            return SourceFetchReport(
                url=item.link or url,
                fetched_at=item.fetched_at,
                items=(item_report,),
                duplicate_keys=(),
            )
        items: list[SourceItemReport] = []
        for item in getattr(fetch_result, "items", ()):
            if not isinstance(item, SourceItem):
                raise TypeError("source_service items must be SourceItem objects")
            items.append(await self._intake_source_item(item, process=process))
        return SourceFetchReport(
            url=fetch_result.url,
            fetched_at=fetch_result.fetched_at,
            items=tuple(items),
            duplicate_keys=tuple(fetch_result.duplicate_keys),
        )

    async def fetch_sources(
        self,
        urls: Iterable[str],
        *,
        kind: str = "auto",
        process: bool = True,
    ) -> SourceBatchReport:
        """Fetch many URLs, keeping a classified record of every failure.

        One URL failing never aborts the batch, but it is never counted as a
        success either: each failure is preserved with its classification
        (``FailureKind`` value for fetch failures, the exception class name
        for intake failures) alongside the reports of the URLs that did
        fetch.
        """
        self._fetch_dependencies(process)
        reports: list[SourceFetchReport] = []
        failures: list[SourceURLFailure] = []
        for url in urls:
            try:
                reports.append(
                    await self.fetch_source(url, kind=kind, process=process)
                )
            except SourceFetchError as exc:
                failures.append(
                    SourceURLFailure(
                        url=exc.url, kind=exc.kind.value, detail=exc.detail
                    )
                )
            except Exception as exc:
                failures.append(
                    SourceURLFailure(
                        url=str(url),
                        kind=type(exc).__name__,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return SourceBatchReport(tuple(reports), tuple(failures))

    async def fetch_scholarly_by_title(
        self,
        title: str,
        *,
        link: str | None = None,
        process: bool = True,
    ) -> SourceFetchReport:
        """Resolve one academic record by strictly matching a title.

        For candidates whose URL carries no DOI/PMID/PMCID (a publisher
        landing page that could not be fetched), the already-known title is
        matched against public Crossref/OpenAlex bibliographic search.  The
        scholarly client only accepts a verified title match — a DOI is never
        guessed from a fuzzy hit.  The resolved item flows through the same
        intake boundary as every other source: spool, ingest, optional
        pipeline, event.
        """
        if self._scholarly is None:
            raise ValueError(
                "scholarly_metadata_client is required for title-based scholarly resolution"
            )
        if self._source_raw_dir is None:
            raise ValueError(
                "source_raw_dir is required for title-based scholarly resolution"
            )
        if process and self._pipeline is None:
            raise ValueError(
                "pipeline_service is required for title-based scholarly resolution with process=True"
            )
        item = await self._scholarly.fetch_by_title(title, link=link)
        item_report = await self._intake_source_item(item, process=process)
        return SourceFetchReport(
            url=item.link or link or title,
            fetched_at=item.fetched_at,
            items=(item_report,),
            duplicate_keys=(),
        )

    def _fetch_dependencies(self, process: bool) -> _SourceService:
        if self._source is None:
            raise ValueError("source_service is required for source fetching")
        if self._source_raw_dir is None:
            raise ValueError("source_raw_dir is required for source fetching")
        if process and self._pipeline is None:
            raise ValueError(
                "pipeline_service is required for source fetching with process=True"
            )
        return self._source

    async def _intake_source_item(
        self, item: SourceItem, *, process: bool
    ) -> SourceItemReport:
        spool_path = spool_source_item(item, self._source_raw_dir / SPOOL_DIRNAME)
        result = self._ingestion.ingest(spool_path, item.to_ingestion_metadata())
        run = await self._pipeline.run_material(result) if process else None
        event = Event(
            event_id=f"source-material-ingested-{uuid4()}",
            event_type="material.ingested",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "material_id": result.material.id,
                "corpus_path": str(result.corpus_path),
                "spool_path": str(spool_path),
                "url": item.link,
                "pipeline_status": getattr(run, "status", None),
            },
            correlation_id=result.material.id,
        )
        await self._events.publish(event)
        return SourceItemReport(
            title=item.title,
            source=item.source,
            link=item.link,
            material_id=result.material.id,
            spool_path=spool_path,
            raw_path=result.raw_path,
            corpus_path=result.corpus_path,
            pipeline=run,
            access_level=item.access_level,
        )

    async def plan_research(
        self,
        material: Material,
        extraction: ExtractionResult | None = None,
        *,
        core_claims: Iterable[str] = (),
        evidence_boundaries: Iterable[str] = (),
    ) -> ResearchPlan:
        """Create an auditable temporal research plan for one material."""
        if self._research_planner is None:
            raise ValueError("research_planner is required for plan_research()")
        return await self._research_planner.plan(
            material,
            extraction,
            core_claims=core_claims,
            evidence_boundaries=evidence_boundaries,
        )

    async def plan_research_by_id(
        self,
        source_id: str,
        extraction: ExtractionResult | None = None,
        *,
        core_claims: Iterable[str] = (),
        evidence_boundaries: Iterable[str] = (),
    ) -> ResearchPlan:
        """Plan research for a material already indexed in the evidence store."""
        getter = getattr(self._store, "get", None)
        if not callable(getter):
            raise TypeError("evidence_store must provide get() for research planning")
        entry = getter(source_id)
        if entry is None:
            raise LookupError(f"material not found: {source_id}")
        if not isinstance(entry, IndexEntry):
            raise TypeError("evidence_store.get() must return an IndexEntry or None")
        material = Material(
            id=entry.source_id,
            title=entry.title,
            source=entry.source,
            published_at=entry.published_at,
            fetched_at=entry.fetched_at,
            type=entry.type,
            content=entry.content,
            original_format=entry.original_format,
            ocr=entry.ocr,
            extracted_via=entry.extracted_via,
            raw_path=entry.raw_path,
            case_tags=entry.case_tags,
            url=entry.url,
            retrieval_level=getattr(entry, "retrieval_level", None),
            access_level=getattr(entry, "access_level", None),
            doi=getattr(entry, "doi", None),
            authors=getattr(entry, "authors", ()),
            container_title=getattr(entry, "container_title", None),
        )
        return await self.plan_research(
            material,
            extraction,
            core_claims=core_claims,
            evidence_boundaries=evidence_boundaries,
        )

    async def execute_research(
        self, plan: ResearchPlan, *, process: bool = True
    ) -> ResearchExecutionReport:
        """Execute a plan through the injected provider and authoritative intake."""
        from prism.research import ResearchExecutor

        executor = self._research_executor
        if executor is None:
            if self._search_provider is None:
                raise ValueError(
                    "search_provider is required for execute_research()"
                )
            intake = self._research_intake or self
            if self._research_intake is None and self._source is None:
                raise ValueError(
                    "source_service or research_intake is required for execute_research()"
                )
            executor = ResearchExecutor(self._search_provider, intake)
        return await executor.execute(plan, process=process)

    async def build_timeline(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Build the graph timeline valid at ``as_of``."""
        return await self._graph.timeline(case_id, as_of)

    async def query_history(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Explicit historical-query entry point, equivalent to a timeline build."""
        return await self._graph.timeline(case_id, as_of)

    async def query_case_state(
        self, case_id: str, cutoff_at: datetime
    ) -> HistoricalCaseState:
        """Return status, nodes, facts, interpretations and gaps at a cutoff."""

        if self._analyzer is None:
            raise ValueError("analyzer_service is required for query_case_state()")
        state = getattr(self._analyzer, "state", None)
        if not callable(state):
            raise TypeError("analyzer_service must provide state()")
        return await state(case_id, cutoff_at)

    async def add_case_bundle(
        self,
        case: EvolutionCase,
        *,
        nodes: Iterable[EvolutionNode] = (),
        facts: Iterable[TemporalFact] = (),
        claims: Iterable[Claim] = (),
        materials: Iterable[Material] = (),
    ) -> GraphWriteResult:
        """Add one case and its related domain objects to the graph."""
        return await self._graph.add_case(
            case,
            nodes=nodes,
            facts=facts,
            claims=claims,
            materials=materials,
        )

    async def report_case(
        self,
        case_id: str,
        as_of: datetime | None = None,
        use_llm: bool = True,
    ) -> ReportDocument:
        """Analyze one case, then render the analysis as a report document.

        The injected ``AnalyzerService.analyze`` runs first and its finished
        ``EvolutionAnalysis`` is handed verbatim to ``ReportService.report``;
        neither stage is reimplemented here.  ``as_of`` is forwarded unchanged
        (``None`` lets the analyzer use its own clock) and must be
        timezone-aware when supplied.  ``use_llm=False`` renders through a
        router-less ``ReportService`` so an explicitly disabled LLM is never
        contacted regardless of how the injected report service was wired;
        ``use_llm=True`` uses the injected service, whose router (if any) is
        the only LLM path.
        """

        if self._analyzer is None:
            raise ValueError("analyzer_service is required for report_case()")
        analysis = await self._analyzer.analyze(case_id, as_of)
        return await self._report_service_for(use_llm).report(analysis)

    def _report_service_for(self, use_llm: bool) -> _ReportService:
        if use_llm:
            if self._report is None:
                raise ValueError("report_service is required for report_case()")
            return self._report
        if self._offline_report is None:
            self._offline_report = ReportService()
        return self._offline_report
