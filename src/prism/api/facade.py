"""Unified application API shared by PRISM's CLI and WebUI."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from prism.analyzer import EvolutionAnalysis
from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.events import Event
from prism.graph import GraphTimeline, GraphWriteResult
from prism.ingestion import IngestionResult
from prism.report import ReportDocument, ReportService
from prism.store import IndexOutcome, SearchFilter


_Dependency = TypeVar("_Dependency")


class _IngestionService(Protocol):
    def ingest(
        self, path: str | Path, metadata: dict[str, Any] | None = None
    ) -> IngestionResult: ...


class _EvidenceStore(Protocol):
    def index_file(self, path: str | Path) -> IndexOutcome: ...

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


class _ReportService(Protocol):
    async def report(self, analysis: EvolutionAnalysis) -> ReportDocument: ...


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
        self._offline_report: _ReportService | None = None

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

    async def build_timeline(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Build the graph timeline valid at ``as_of``."""
        return await self._graph.timeline(case_id, as_of)

    async def query_history(self, case_id: str, as_of: datetime) -> GraphTimeline:
        """Explicit historical-query entry point, equivalent to a timeline build."""
        return await self._graph.timeline(case_id, as_of)

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
