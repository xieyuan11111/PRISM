"""Unified application API shared by PRISM's CLI and WebUI."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from uuid import uuid4

from prism.analyzer import EvolutionAnalysis, HistoricalCaseState
from prism.domain import (
    Claim,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
    TemporalRelation,
)
from prism.events import Event
from prism.extraction import ExtractionResult
from prism.graph import GraphTimeline, GraphWriteResult
from prism.ingestion import IngestionResult
from prism.pipeline import PipelineError
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

_INPUT_SUFFIXES = (".md", ".markdown", ".pdf")


@dataclass(frozen=True, slots=True)
class ProcessMaterialResult:
    """The end-to-end outcome of processing one material automatically.

    ``pipeline`` is the authoritative completed
    :class:`~prism.pipeline.PipelineRun` (never a duplicate-skip record),
    ``case_id``/``case_outcome`` describe the accumulated case the material
    entered (both ``None`` when the extraction produced no case), and
    ``warnings`` collects every auditable skip reason, extraction warning,
    evidence gap and unresolved conflict surfaced along the way.

    ``replayed`` is ``False`` when this call executed the pipeline, ``True``
    when it was an idempotent replay of a run that had already completed
    (e.g. an event-driven run or an earlier call) — the fields above are the
    authoritative outcome either way, and nothing is merged a second time.
    """

    material_id: str
    pipeline: object
    case_id: str | None
    case_outcome: object | None
    warnings: tuple[str, ...] = ()
    replayed: bool = False


def _is_pathlike_input(source: object) -> bool:
    if isinstance(source, Path):
        return True
    if not isinstance(source, str):
        raise TypeError("source must be a material id or a path")
    text = source.strip()
    return (
        "/" in text
        or "\\" in text
        or text.lower().endswith(_INPUT_SUFFIXES)
    )


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
        relations: Iterable[TemporalRelation] = (),
        conflicts: Iterable[object] = (),
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
        self,
        result: IngestionResult,
        *,
        correlation_id: str | None = ...,
        target_case: EvolutionCase | None = ...,
    ) -> object: ...

    def run_for(self, material_id: str) -> object | None: ...

    def case_outcome_for(self, material_id: str) -> object | None: ...


class _CaseService(Protocol):
    async def record_extraction(
        self, material: Material, extraction: ExtractionResult
    ) -> object: ...

    async def merge_case(self, case_id: str) -> object | None: ...

    async def merge_explicit(
        self, case_id: str, material_ids: "Iterable[str]"
    ) -> object: ...

    async def bind_material_to_case(
        self, material_id: str, case_id: str
    ) -> object: ...

    def case_for_material(self, material_id: str) -> str | None: ...

    def load_case(self, case_id: str) -> EvolutionCase: ...


class _MaterialResolver(Protocol):
    def resolve(self, material_id: str) -> IngestionResult: ...

    def __call__(self, event: Event) -> IngestionResult: ...


class _ExtractionService(Protocol):
    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Path | None = ...,
        target_case: EvolutionCase | None = ...,
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
        case_service: _CaseService | None = None,
        material_resolver: _MaterialResolver | None = None,
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
        self._case_service = _optional_dependency(
            "case_service", case_service, "merge_case"
        )
        _optional_dependency("case_service", case_service, "record_extraction")
        self._material_resolver = _optional_dependency(
            "material_resolver", material_resolver, "resolve"
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
        """Normalize and index one material, then announce it on the bus.

        This is the asynchronous, event-driven entry point: it returns when
        ingestion and indexing are complete, and announces the material on
        the event bus.  In a runtime with the automatic pipeline subscribed,
        index → extract → accumulated-case merge → graph processing is
        QUEUED and finishes after this call returns — this result never
        claims pipeline completion.  The material's lifecycle is queryable
        at any moment: ``pipeline.outcome_for(material_id)`` reports
        ``pending``/``failed``/``committed``, ``pipeline.run_for`` the
        completed run and ``pipeline.failure_for`` the last failed attempt.
        To block until processing is done (or to force a retry of a failed
        material) call :meth:`process_material` with the material id: it
        waits for the in-flight or completed run and reports the
        authoritative outcome, replaying nothing and merging nothing twice;
        a persistent failure is raised there as the structured
        :class:`~prism.pipeline.PipelineError`.  Subscriber failures are
        recorded as auditable dispatch errors and per-material pipeline
        failures — never reported here as processing success.
        """
        result, index_result = self._ingest_and_index(path, metadata)
        await self._announce(
            result, index_status=getattr(index_result, "status", None)
        )
        return result

    def _ingest_and_index(
        self, path: str | Path, metadata: dict[str, Any] | None
    ) -> tuple[IngestionResult, object]:
        """Normalize one input and index its corpus file (no events)."""
        result = self._ingestion.ingest(path, metadata)
        index_result = self._store.index_file(result.corpus_path)
        return result, index_result

    async def _announce(
        self, result: IngestionResult, *, index_status: object | None
    ) -> None:
        """Publish the ``material.ingested`` announcement for one material."""
        event = Event(
            event_id=f"material-ingested-{uuid4()}",
            event_type="material.ingested",
            occurred_at=datetime.now(timezone.utc),
            payload={
                "material_id": result.material.id,
                "corpus_path": str(result.corpus_path),
                "index_status": (
                    index_status if isinstance(index_status, str) else None
                ),
            },
            correlation_id=result.material.id,
        )
        await self._events.publish(event)

    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Path | None = None,
        target_case: EvolutionCase | None = None,
    ) -> ExtractionResult:
        """Run the evidence-bound Evolution Extraction v0 public entry point.

        ``target_case`` is forwarded to the extraction service: when supplied,
        the completion must anchor every candidate to that declared case.
        """

        if self._extraction is None:
            raise ValueError("extraction_service is required for extract_material()")
        return await self._extraction.extract_material(
            material, corpus_path=corpus_path, target_case=target_case
        )

    async def process_material(
        self,
        source: str | os.PathLike[str],
        metadata: dict[str, Any] | None = None,
        *,
        target_case: EvolutionCase | str | None = None,
    ) -> ProcessMaterialResult:
        """Run one material through the automatic pipeline, end to end.

        This is the synchronous entry point: ``source`` is either a material
        id already indexed in the evidence store or a path to an input
        document (which is ingested and indexed first).  The API returns only
        after the material's pipeline run — and, when a case was produced,
        its accumulated case merge/write outcome — has completed.

        ``target_case`` is the explicit case context the caller declares for
        this material.  It may be a real :class:`~prism.domain.EvolutionCase`
        object or the id of an already recorded case; an id is loaded through
        the case service's durable ledger — never guessed from a title, tag,
        or vector — and an unknown id raises :class:`LookupError` before
        anything is ingested or run.  The resolved case is forwarded to the
        pipeline, whose extractor anchors the completion to it; a completion
        that refuses the anchor or drifts from it fails the material
        auditably instead of being silently rewritten.  Because the automatic
        path already refuses re-binding a material that the durable ledger
        binds to another case, processing a material under a second target
        case surfaces the typed
        :class:`~prism.cases.MaterialCaseConflict` whenever the pipeline
        actually re-executes (a fresh process); in-process repeats stay
        idempotent replays of the completed run.

        Semantics are explicit and idempotent within one process:
        * a material whose run has not yet started is executed now (a path
          input, or an id with no in-process run);
        * a material whose event-driven run is still in flight is waited on
          (the pipeline is serialized) and its authoritative outcome is
          reported with ``replayed=True``;
        * a repeated call for an already completed material is an explicit
          idempotent replay (``replayed=True``) of that run — the accumulated
          case is never merged a second time by this entry point;
        * a material whose last attempt failed is RETRIED safely: the stale
          failed audit/outcome is cleared by a later success, and a
          persistent failure raises the structured
          :class:`~prism.pipeline.PipelineError` (stage and material id) —
          a fake success is never returned.

        Cross-process behavior is honest and explicit: the completed-run
        registry is per-process, so a fresh process (e.g. another CLI
        invocation) re-executes the pipeline for an id whose durable state is
        already committed — ``replayed=False`` for that genuine run.  The
        writes stay idempotent (graph episodes are deduplicated by episode
        key and the durable ledger row is upserted under the same case), but
        extraction is re-run, so a changed extraction REPLACES the recorded
        evidence; if the re-extraction now declares a different case than the
        durable binding, the typed
        :class:`~prism.cases.MaterialCaseConflict` refuses the re-binding
        before any write — the automatic path never adds ambiguous bindings
        and a legacy multi-case binding is never silently resolved.

        The returned :class:`ProcessMaterialResult` carries the authoritative
        pipeline run, the case outcome the run produced, and every auditable
        warning.  A failed attempt raises the structured
        :class:`~prism.pipeline.PipelineError` (with stage and material id)
        and never returns a fake success; an ambiguous legacy multi-case
        binding raises the typed :class:`~prism.cases.MaterialCaseConflict`.
        """
        if self._pipeline is None:
            raise ValueError("pipeline_service is required for process_material()")
        resolved_target = self._resolve_target_case(target_case)
        if _is_pathlike_input(source):
            ingested, index_outcome = self._ingest_and_index(source, metadata)
            material_id = ingested.material.id
            ingestion_result: IngestionResult = ingested
        else:
            material_id = str(source).strip()
            getter = getattr(self._store, "get", None)
            if not callable(getter):
                raise TypeError(
                    "evidence_store must provide get() to process by material id"
                )
            if getter(material_id) is None:
                raise LookupError(f"material not found: {material_id}")
            if self._material_resolver is None:
                raise ValueError(
                    "material_resolver is required to process by material id"
                )
            ingestion_result = self._material_resolver.resolve(material_id)
            index_outcome = None

        try:
            if resolved_target is None:
                attempt = await self._pipeline.run_material(ingestion_result)
            else:
                run_material = self._pipeline.run_material
                if not self._accepts_kwarg(run_material, "target_case"):
                    raise TypeError(
                        "pipeline_service.run_material must accept target_case "
                        "to process with a declared target case"
                    )
                attempt = await run_material(
                    ingestion_result, target_case=resolved_target
                )
        except PipelineError as error:
            # A case-binding refusal raised by the automatic accumulator
            # inside the pipeline surfaces as the typed, structured conflict
            # (material id, bound cases, attempted case) — never as a generic
            # wrapped stage error.  The failure audit with the typed
            # error_type stays recorded on the pipeline either way.
            from prism.cases.ledger import MaterialCaseConflict

            cause = error.__cause__
            if isinstance(cause, MaterialCaseConflict):
                raise cause
            raise
        if attempt.status == "completed":
            run = attempt
            replayed = False
        else:
            # Skipped duplicate attempt: the authoritative completed run is
            # the one this call waited on and must report.
            run_for = getattr(self._pipeline, "run_for", None)
            run = run_for(material_id) if callable(run_for) else None
            if run is None:
                raise RuntimeError(
                    "pipeline reported no run for the processed material"
                )
            replayed = True
        if index_outcome is not None:
            # The synchronous path announced nothing before processing; the
            # completed material is announced now — event subscribers
            # deduplicate by material id, so nothing runs twice.
            await self._announce(
                ingestion_result,
                index_status=getattr(index_outcome, "status", None),
            )

        case_outcome: object | None = None
        case_id: str | None = None
        outcome_getter = getattr(self._pipeline, "case_outcome_for", None)
        if callable(outcome_getter):
            case_outcome = outcome_getter(material_id)
        warnings = self._audit_warnings(run)
        if case_outcome is not None:
            outcome_case_id = getattr(case_outcome, "case_id", None)
            if isinstance(outcome_case_id, str) and outcome_case_id.strip():
                case_id = outcome_case_id
            warnings.extend(tuple(getattr(case_outcome, "warnings", ()) or ()))
        elif self._case_service is not None:
            # No fresh case outcome: report any durable binding truthfully —
            # the ledger may bind the material from an earlier process even
            # when this run produced no case.
            case_id = self._case_service.case_for_material(material_id)
            if case_id is not None:
                warnings.append(
                    f"material {material_id!r} is bound to case {case_id!r} "
                    "in the durable ledger but this run produced no case "
                    "outcome; run merge-case to rebuild the accumulated case"
                )
        return ProcessMaterialResult(
            material_id=material_id,
            pipeline=run,
            case_id=case_id,
            case_outcome=case_outcome,
            warnings=tuple(dict.fromkeys(warnings)),
            replayed=replayed,
        )

    async def merge_case(
        self,
        case_id: str,
        materials: Iterable[str] | None = None,
    ) -> object:
        """Merge and write one case's accumulated evidence on demand.

        This is the explicit reconciliation entry point, independent of the
        automatic pipeline: without ``materials`` the full accumulated case
        is rebuilt from the durable ledger; with ``materials`` only the
        explicitly listed materials are merged (an id with no recorded
        extraction raises :class:`LookupError`).  Rewriting the same
        accumulated state is idempotent (graph episodes are deduplicated by
        episode key).  An unknown case raises :class:`LookupError` — it is
        never silently treated as empty.
        """
        if self._case_service is None:
            raise ValueError("case_service is required for merge_case()")
        if materials is None:
            outcome = await self._case_service.merge_case(case_id)
            if outcome is None:
                raise LookupError(
                    f"no accumulated extractions for case {case_id!r}"
                )
            return outcome
        return await self._case_service.merge_explicit(case_id, tuple(materials))

    async def bind_material_to_case(
        self, material_id: str, case_id: str
    ) -> object:
        """Explicitly attach pending material evidence to an existing case.

        Both identifiers come from the caller.  The case service revalidates
        the stored evidence and refuses unknown cases; this facade performs
        no title-, tag-, or vector-based case inference.
        """
        if self._case_service is None:
            raise ValueError(
                "case_service is required for bind_material_to_case()"
            )
        bind = getattr(self._case_service, "bind_material_to_case", None)
        if not callable(bind):
            raise TypeError("case_service must provide bind_material_to_case()")
        return await bind(material_id, case_id)

    @staticmethod
    def _accepts_kwarg(method: object, name: str) -> bool:
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == name
            for parameter in parameters
        )

    def _resolve_target_case(
        self, target_case: EvolutionCase | str | None
    ) -> EvolutionCase | None:
        """Normalize the declared target-case argument to a real case.

        An ``EvolutionCase`` is used as-is; an id string is resolved through
        the case service's durable ledger (:meth:`CaseService.load_case`) so
        only a real recorded case is ever used — an unknown id raises
        :class:`LookupError`, and nothing here guesses a case from titles,
        tags, or vectors.
        """
        if target_case is None:
            return None
        if isinstance(target_case, EvolutionCase):
            return target_case
        if isinstance(target_case, str):
            case_id = target_case.strip()
            if not case_id:
                raise ValueError("target_case case id must be a non-empty string")
            if self._case_service is None:
                raise ValueError(
                    "case_service is required to resolve a target_case case id"
                )
            load = getattr(self._case_service, "load_case", None)
            if not callable(load):
                raise TypeError(
                    "case_service must provide load_case() to resolve a "
                    "target_case case id"
                )
            loaded = load(case_id)
            if loaded is None or not isinstance(loaded, EvolutionCase):
                raise LookupError(
                    f"no recorded evolution case {case_id!r}; record a material "
                    "under the case first or declare the case explicitly"
                )
            return loaded
        raise TypeError(
            "target_case must be an EvolutionCase or a recorded case id string"
        )

    @staticmethod
    def _audit_warnings(run: object) -> list[str]:
        """Surface every skip reason, warning, gap and conflict of one run."""
        warnings: list[str] = []
        for stage in getattr(run, "stages", ()) or ():
            result = getattr(stage, "result", None)
            if isinstance(result, ExtractionResult):
                for warning in result.warnings:
                    warnings.append(f"extraction warning: {warning}")
                for gap in result.evidence_gaps:
                    warnings.append(
                        f"evidence gap ({gap.gap_type})"
                        + (f" on {gap.item_kind}" if gap.item_kind else "")
                        + f": {gap.detail}"
                    )
                for conflict in result.conflicts:
                    warnings.append(
                        f"unresolved conflict {conflict.conflict_id}: "
                        f"{conflict.subject} {conflict.predicate} -> "
                        + " | ".join(conflict.alternatives)
                    )
            if getattr(stage, "status", None) == "skipped" and stage.detail:
                warnings.append(f"stage {stage.name} skipped: {stage.detail}")
        return warnings

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
        relations: Iterable[TemporalRelation] = (),
        conflicts: Iterable[object] = (),
        materials: Iterable[Material] = (),
    ) -> GraphWriteResult:
        """Add one case and its related domain objects to the graph."""
        graph_arguments = {
            "nodes": nodes,
            "facts": facts,
            "claims": claims,
            "materials": materials,
        }
        relation_items = tuple(relations)
        conflict_items = tuple(conflicts)
        if relation_items:
            graph_arguments["relations"] = relation_items
        if conflict_items:
            graph_arguments["conflicts"] = conflict_items
        return await self._graph.add_case(case, **graph_arguments)

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
