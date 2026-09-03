"""Incremental post-ingestion orchestration for PRISM materials.

The pipeline continues where ingestion ends: one already-ingested
:class:`~prism.ingestion.IngestionResult` is pushed through three stages in a
fixed order — index the corpus file, extract structured domain objects, then
write the resulting case bundle to the graph. Case-less substantive candidates
are first accumulated in the local material evidence ledger with status
``awaiting_case_binding``; their graph stage remains skipped until an explicit
case binding. Every collaborator (indexer,
extractor, graph writer, clock, and the event-to-material resolver) is
injected, so the service is fully testable offline.  Failures raise
:class:`PipelineError` with the audit trail of already-completed stages; no
partial or fabricated results are ever returned.  Each material's lifecycle
is queryable as a structured :class:`~prism.pipeline.outcomes.PipelineOutcome`
(``pending``/``failed``/``committed``); terminal outcomes may additionally be
persisted through an injected local outcome ledger.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

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
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.sources import redact_audit_text
from prism.store import IndexOutcome

from .outcomes import (
    COMMITTED,
    FAILED,
    PENDING,
    PipelineOutcome,
)


MATERIAL_INGESTED = "material.ingested"

_STAGE_INDEX = "index"
_STAGE_EXTRACT = "extract"
_STAGE_GRAPH = "graph"
_STAGE_NAMES = frozenset({_STAGE_INDEX, _STAGE_EXTRACT, _STAGE_GRAPH})

_STATUS_COMPLETED = "completed"
_STATUS_SKIPPED = "skipped"
_RUN_STATUSES = frozenset({_STATUS_COMPLETED, _STATUS_SKIPPED})

_DETAIL_DUPLICATE_MATERIAL = "duplicate material_id"
_DETAIL_DUPLICATE_CORRELATION = "duplicate correlation_id"
_DETAIL_NO_CASE = "extraction produced no case"
_DETAIL_CASELESS_CANDIDATES = (
    "extraction retained material-scoped candidates without a case; "
    "case-specific graph writing was skipped"
)

# Levels that carry evidence *about* a work — an abstract, bibliographic
# metadata only, or an access-blocked placeholder — never the work's full
# text.  Structured extraction and graph writing over such bodies would
# treat the placeholder as the article, so the pipeline indexes them but
# skips those stages with an audit record.  Only ``fulltext`` materials
# (and unlabeled materials, which are not scholarly placeholders) run the
# extract and graph stages.
_NON_FULLTEXT_ACCESS_LEVELS = frozenset({"abstract_only", "metadata_only", "blocked"})

_STAGE_RESULT_TYPES = (IndexOutcome, ExtractionResult, GraphWriteResult)


class _Indexer(Protocol):
    def index_file(self, path: str | Path) -> IndexOutcome: ...


class _Extractor(Protocol):
    async def extract(self, material: Material) -> ExtractionResult: ...

    async def extract_material(
        self,
        material: Material,
        *,
        corpus_path: str | Path | None = ...,
        target_case: EvolutionCase | None = ...,
    ) -> ExtractionResult: ...


class _GraphWriter(Protocol):
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


class _CaseRecorder(Protocol):
    """Accumulates successful extractions and writes the merged case.

    Implemented by :class:`prism.cases.CaseService`; the returned outcome
    must expose the merged ``write`` (:class:`GraphWriteResult`) and the
    accumulated ``material_ids``.
    """

    async def record_extraction(
        self, material: Material, extraction: ExtractionResult
    ) -> object: ...

    async def record_material_extraction(
        self, material: Material, extraction: ExtractionResult
    ) -> object: ...


_MaterialResolver = Callable[[Event], IngestionResult]
_Clock = Callable[[], datetime]


class PipelineError(RuntimeError):
    """A pipeline stage failed; completed stages remain auditable."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        material_id: str,
        stages: Iterable["PipelineStage"] = (),
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.material_id = material_id
        self.stages = tuple(stages)


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """Audit record for one executed pipeline stage."""

    name: str
    status: str
    result: IndexOutcome | ExtractionResult | GraphWriteResult | None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.name not in _STAGE_NAMES:
            raise ValueError(
                "name must be one of: " + ", ".join(sorted(_STAGE_NAMES))
            )
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be a non-empty string")
        if self.detail is not None and (
            not isinstance(self.detail, str) or not self.detail.strip()
        ):
            raise ValueError("detail must be a non-empty string")
        if self.result is not None and not isinstance(self.result, _STAGE_RESULT_TYPES):
            raise TypeError("result must be an IndexOutcome, ExtractionResult, or GraphWriteResult")
        if self.result is None and self.status != _STATUS_SKIPPED:
            raise ValueError("result is required unless the stage was skipped")


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """The auditable outcome of one pipeline attempt for one material."""

    material_id: str
    status: str
    detail: str | None = None
    correlation_id: str | None = None
    stages: tuple[PipelineStage, ...] = ()
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, str) or not self.material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        if self.status not in _RUN_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(sorted(_RUN_STATUSES))
            )
        if self.detail is not None and (
            not isinstance(self.detail, str) or not self.detail.strip()
        ):
            raise ValueError("detail must be a non-empty string")
        if self.correlation_id is not None and (
            not isinstance(self.correlation_id, str) or not self.correlation_id.strip()
        ):
            raise ValueError("correlation_id must be a non-empty string")
        object.__setattr__(self, "stages", tuple(self.stages))
        for stage in self.stages:
            if not isinstance(stage, PipelineStage):
                raise TypeError("stages must contain only PipelineStage objects")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at must not be earlier than started_at")
        if self.status == _STATUS_COMPLETED:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("completed runs require started_at and finished_at")
        elif self.stages:
            raise ValueError("skipped runs must not carry stage records")


@dataclass(frozen=True, slots=True)
class PipelineFailure:
    """The auditable record of one failed pipeline attempt for one material.

    ``material_id`` names the material, ``stage`` the pipeline stage that
    failed (``None`` when the failure preceded any stage, e.g. a resolver
    error), ``error_type``/``message`` describe the underlying error and
    ``failed_at`` when it happened.  A failed attempt never produces a
    completed run; retrying is safe and clears the stale audit record.
    """

    material_id: str
    stage: str | None
    error_type: str
    message: str
    failed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, str) or not self.material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        if self.stage is not None and (
            not isinstance(self.stage, str) or not self.stage.strip()
        ):
            raise ValueError("stage must be a non-empty string or None")
        if not isinstance(self.error_type, str) or not self.error_type.strip():
            raise ValueError("error_type must be a non-empty string")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if (
            not isinstance(self.failed_at, datetime)
            or self.failed_at.tzinfo is None
            or self.failed_at.utcoffset() is None
        ):
            raise ValueError("failed_at must be timezone-aware")


def _required_dependency(name: str, dependency: object, method: str):
    if dependency is None:
        raise ValueError(f"{name} is required")
    if not callable(getattr(dependency, method, None)):
        raise TypeError(f"{name} must provide {method}()")
    return dependency


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class _OutcomeStore(Protocol):
    """Persists terminal pipeline outcomes (``failed``/``committed``)."""

    def record(self, outcome: PipelineOutcome) -> None: ...

    def entries(self) -> tuple[PipelineOutcome, ...]: ...


class PipelineService:
    """Orchestrate index → extract → graph-write for ingested materials.

    The service is deliberately serialized: one material runs at a time, and
    successfully processed ``material_id``/``correlation_id`` keys are never
    reprocessed.  It creates no background tasks and owns no resources.

    Every material's lifecycle is queryable as a structured
    :class:`PipelineOutcome` (:meth:`outcome_for`, :meth:`outcomes`):
    ``pending`` while an attempt is in flight, ``failed`` when an attempt
    raised (with stage, error type and time), ``committed`` only after an
    attempt completed — index, extraction and, when a case was produced, the
    accumulated-case merge and graph write all succeeded.  Terminal outcomes
    are persisted through the optional injected ``outcome_store`` (a local
    SQLite ledger), so failures stay auditable after a restart; ``pending``
    is transient and lives only in this process.  A failed attempt never
    produces a completed run or a committed outcome; retrying is safe and a
    later success clears the stale failure audit.
    """

    def __init__(
        self,
        *,
        indexer: _Indexer,
        extraction_service: _Extractor,
        graph_service: _GraphWriter,
        clock: _Clock | None = None,
        material_resolver: _MaterialResolver | None = None,
        case_service: _CaseRecorder | None = None,
        outcome_store: _OutcomeStore | None = None,
    ) -> None:
        self._indexer = _required_dependency("indexer", indexer, "index_file")
        self._extraction = _required_dependency(
            "extraction_service", extraction_service, "extract"
        )
        self._graph = _required_dependency("graph_service", graph_service, "add_case")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if material_resolver is not None and not callable(material_resolver):
            raise TypeError("material_resolver must be callable")
        if case_service is not None and not callable(
            getattr(case_service, "record_extraction", None)
        ):
            raise TypeError("case_service must provide record_extraction()")
        if outcome_store is not None and not callable(
            getattr(outcome_store, "record", None)
        ):
            raise TypeError("outcome_store must provide record()")
        self._clock: _Clock = clock or (lambda: datetime.now(timezone.utc))
        self._resolver = material_resolver
        self._case_recorder = case_service
        self._outcome_store = outcome_store
        self._lock = asyncio.Lock()
        self._completed: dict[str, PipelineRun] = {}
        self._by_correlation: dict[str, str] = {}
        self._failures: dict[str, PipelineFailure] = {}
        self._case_outcomes: dict[str, object] = {}
        self._outcomes: dict[str, PipelineOutcome] = {}
        if outcome_store is not None:
            # Terminal outcomes survive process restarts: hydrate the query
            # view from the durable local ledger.  The completed-run registry
            # stays per-process, so a fresh process still re-runs a material
            # whose durable state is committed — never a blind replay.
            entries = getattr(outcome_store, "entries", None)
            if not callable(entries):
                raise TypeError("outcome_store must provide entries()")
            for outcome in entries():
                if not isinstance(outcome, PipelineOutcome):
                    raise TypeError(
                        "outcome_store entries must be PipelineOutcome objects"
                    )
                self._outcomes[outcome.material_id] = outcome

    async def run_material(
        self,
        result: IngestionResult,
        *,
        correlation_id: str | None = None,
        target_case: EvolutionCase | None = None,
    ) -> PipelineRun:
        """Run the full pipeline for one ingested material, exactly once.

        ``target_case`` is the caller-declared evolution case this material
        belongs to; when supplied it is forwarded verbatim to the extractor's
        ``extract_material(..., target_case=...)`` so the completion is
        anchored to that real case.  The extractor must support the keyword
        (the auditable failure isolates the material); an extractor with only
        the legacy ``extract()`` entry point cannot process a target case.

        Materials whose ``access_level`` is not ``fulltext`` — an abstract,
        a bibliographic placeholder, or a blocked record — carry no full
        text of the work: they are still indexed (so the metadata stays
        searchable) but the extract and graph stages are skipped and
        recorded as such, so the placeholder is never treated as an article
        body.

        A failed attempt raises :class:`PipelineError` and is recorded as a
        queryable :class:`PipelineFailure` (:meth:`failure_for`) and a
        ``failed`` :class:`PipelineOutcome` (:meth:`outcome_for`) — never as
        a completed run or a committed outcome.  A later successful retry
        clears the stale audit and moves the outcome to ``committed``.
        """

        if not isinstance(result, IngestionResult):
            raise TypeError("result must be an IngestionResult")
        if correlation_id is not None:
            _require_text("correlation_id", correlation_id)
        if target_case is not None and not isinstance(target_case, EvolutionCase):
            raise TypeError("target_case must be an EvolutionCase or None")
        async with self._lock:
            material_id = result.material.id
            previous = self._completed.get(material_id)
            if previous is not None:
                return PipelineRun(
                    material_id=material_id,
                    status=_STATUS_SKIPPED,
                    detail=_DETAIL_DUPLICATE_MATERIAL,
                    correlation_id=previous.correlation_id,
                )
            if correlation_id is not None and correlation_id in self._by_correlation:
                return PipelineRun(
                    material_id=material_id,
                    status=_STATUS_SKIPPED,
                    detail=_DETAIL_DUPLICATE_CORRELATION,
                    correlation_id=correlation_id,
                )
            started_at = self._now()
            # An attempt is in flight: querying the material's outcome during
            # the run reports pending — never a fake success or an absence.
            self._outcomes[material_id] = PipelineOutcome(
                material_id, PENDING, started_at
            )
            try:
                run = await self._run_locked(
                    result, correlation_id, started_at, target_case
                )
            except Exception as exc:
                self._record_failure(material_id, exc)
                raise
            self._failures.pop(material_id, None)
            self._persist_outcome(
                PipelineOutcome(
                    material_id,
                    COMMITTED,
                    run.finished_at,
                    correlation_id=run.correlation_id,
                )
            )
            return run

    def run_for(self, material_id: str) -> PipelineRun | None:
        """The last completed run for ``material_id``, or ``None``.

        Skipped duplicate attempts never replace the completed run, so this
        is the authoritative audit record even when an event subscriber and
        an explicit caller race to process the same material.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        return self._completed.get(material_id)

    def failure_for(self, material_id: str) -> PipelineFailure | None:
        """The auditable record of the last failed attempt, or ``None``.

        A material with no completed run and no failure record is still
        pending (queued or never announced).  A successful retry clears the
        record, so a stale failure is never reported as current state.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        return self._failures.get(material_id)

    def outcome_for(self, material_id: str) -> PipelineOutcome | None:
        """The current lifecycle outcome of one material, or ``None``.

        ``pending`` while an attempt is in flight, ``failed`` after a failed
        attempt (with stage, error type and time), ``committed`` only after a
        successful attempt.  ``None`` means the material was never announced
        or processed in this process.  Terminal outcomes hydrate from the
        durable local ledger after a restart, so a material that failed in an
        earlier process is still queryable as ``failed`` here.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        return self._outcomes.get(material_id)

    def outcomes(self) -> tuple[PipelineOutcome, ...]:
        """Every material's current lifecycle outcome, in first-recorded
        order (durable terminal outcomes first, then this process's runs)."""
        return tuple(self._outcomes.values())

    def case_outcome_for(self, material_id: str) -> object | None:
        """The accumulated-case outcome recorded when this material ran.

        The outcome is the :class:`~prism.cases.service.CaseWriteOutcome`
        produced by the case recorder during the material's completed run
        (``None`` when the run produced no case or no recorder is wired).
        Querying it never re-merges or re-writes the case.
        """
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError("material_id must be a non-empty string")
        return self._case_outcomes.get(material_id)

    async def handle_event(self, event: Event) -> bool:
        """Handle ``material.ingested`` events; ignore everything else.

        Failures before any stage (e.g. the store-backed resolver cannot
        find the announced material) are recorded as a per-material
        :class:`PipelineFailure` with ``stage=None``, so an event-driven
        failure is always auditable with the material id, error type and
        time — never only a generic dispatch error.
        """

        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if event.event_type != MATERIAL_INGESTED:
            return False
        if self._resolver is None:
            raise RuntimeError(
                "material_resolver is required to handle material.ingested events"
            )
        payload = event.payload
        material_id = (
            payload.get("material_id") if isinstance(payload, Mapping) else None
        )
        try:
            result = self._resolver(event)
        except Exception as exc:
            if isinstance(material_id, str) and material_id.strip():
                self._record_failure(material_id, exc)
            raise
        if not isinstance(result, IngestionResult):
            error = TypeError(
                "material_resolver must return an IngestionResult"
            )
            if isinstance(material_id, str) and material_id.strip():
                self._record_failure(material_id, error)
            raise error
        await self.run_material(result, correlation_id=event.correlation_id)
        return True

    async def _run_locked(
        self,
        result: IngestionResult,
        correlation_id: str | None,
        started_at: datetime,
        target_case: EvolutionCase | None = None,
    ) -> PipelineRun:
        material_id = result.material.id
        stages: list[PipelineStage] = []

        try:
            outcome = self._indexer.index_file(result.corpus_path)
            if not isinstance(outcome, IndexOutcome):
                raise TypeError(
                    "indexer must return an IndexOutcome, got "
                    f"{type(outcome).__name__}"
                )
        except Exception as exc:
            raise self._stage_failure(
                _STAGE_INDEX, material_id, stages, exc
            ) from exc
        stages.append(PipelineStage(_STAGE_INDEX, outcome.status, outcome))

        if result.material.access_level in _NON_FULLTEXT_ACCESS_LEVELS:
            # The material is an abstract or a bibliographic placeholder, not
            # full text: ordinary structured extraction (and therefore graph
            # writing) is skipped, never run over the placeholder.  The skip
            # is recorded per stage with the access level preserved so the
            # audit trail shows exactly why no domain objects were produced.
            skip_detail = (
                f"material access_level {result.material.access_level!r} does "
                "not provide full text; structured extraction and graph "
                "writing are skipped"
            )
            stages.append(
                PipelineStage(_STAGE_EXTRACT, _STATUS_SKIPPED, None, detail=skip_detail)
            )
            stages.append(
                PipelineStage(_STAGE_GRAPH, _STATUS_SKIPPED, None, detail=skip_detail)
            )
        else:
            try:
                extract_material = getattr(self._extraction, "extract_material", None)
                if target_case is not None:
                    if not callable(extract_material):
                        raise TypeError(
                            "extraction_service must provide "
                            "extract_material(target_case=...) when a target "
                            "case is declared"
                        )
                    extraction = await extract_material(
                        result.material,
                        corpus_path=result.corpus_path,
                        target_case=target_case,
                    )
                elif callable(extract_material):
                    extraction = await extract_material(
                        result.material, corpus_path=result.corpus_path
                    )
                else:
                    # Compatibility for injected pre-v0 extractors.
                    extraction = await self._extraction.extract(result.material)
                if not isinstance(extraction, ExtractionResult):
                    raise TypeError(
                        "extraction_service must return an ExtractionResult, got "
                        f"{type(extraction).__name__}"
                    )
            except Exception as exc:
                raise self._stage_failure(
                    _STAGE_EXTRACT, material_id, stages, exc
                ) from exc
            stages.append(PipelineStage(_STAGE_EXTRACT, "extracted", extraction))

            if extraction.case is None:
                has_candidates = bool(
                    extraction.nodes
                    or extraction.temporal_facts
                    or extraction.claims
                    or extraction.conflicts
                    or extraction.relations
                )
                material_status = None
                if has_candidates and self._case_recorder is not None:
                    record_material = getattr(
                        self._case_recorder, "record_material_extraction", None
                    )
                    if not callable(record_material):
                        error = TypeError(
                            "case_service must provide "
                            "record_material_extraction() for case-less candidates"
                        )
                        raise self._stage_failure(
                            _STAGE_GRAPH, material_id, stages, error
                        ) from error
                    try:
                        material_outcome = await record_material(
                            result.material, extraction
                        )
                        material_status = getattr(
                            material_outcome, "status", None
                        )
                        if material_status != "awaiting_case_binding":
                            raise TypeError(
                                "case_service material outcome must expose "
                                "status 'awaiting_case_binding'"
                            )
                    except Exception as exc:
                        raise self._stage_failure(
                            _STAGE_GRAPH, material_id, stages, exc
                        ) from exc
                detail = (
                    _DETAIL_CASELESS_CANDIDATES
                    + (
                        "; persisted with status awaiting_case_binding"
                        if material_status == "awaiting_case_binding"
                        else "; no material evidence ledger is configured"
                    )
                    if has_candidates
                    else _DETAIL_NO_CASE
                )
                stages.append(
                    PipelineStage(
                        _STAGE_GRAPH,
                        _STATUS_SKIPPED,
                        None,
                        detail=detail,
                    )
                )
            elif self._case_recorder is not None:
                # Automatic case accumulation: the merged bundle for the
                # whole accumulated case is written by the case service, so
                # one material never overwrites or duplicates the complete
                # case with its own single-material extraction.
                try:
                    outcome = await self._case_recorder.record_extraction(
                        result.material, extraction
                    )
                    write = getattr(outcome, "write", None)
                    if not isinstance(write, GraphWriteResult):
                        raise TypeError(
                            "case_service record_extraction must expose a "
                            f"GraphWriteResult 'write', got {type(write).__name__}"
                        )
                    accumulated = len(
                        tuple(getattr(outcome, "material_ids", ()) or ())
                    )
                    self._case_outcomes[material_id] = outcome
                except Exception as exc:
                    raise self._stage_failure(
                        _STAGE_GRAPH, material_id, stages, exc
                    ) from exc
                stages.append(
                    PipelineStage(
                        _STAGE_GRAPH,
                        "written",
                        write,
                        detail=(
                            "merged case write across "
                            f"{accumulated} accumulated material(s)"
                        ),
                    )
                )
            else:
                try:
                    graph_arguments = {
                        "nodes": extraction.nodes,
                        "facts": extraction.temporal_facts,
                        "claims": extraction.claims,
                        "materials": (result.material,),
                    }
                    if extraction.relations:
                        graph_arguments["relations"] = extraction.relations
                    if extraction.conflicts:
                        graph_arguments["conflicts"] = extraction.conflicts
                    write = await self._graph.add_case(
                        extraction.case, **graph_arguments
                    )
                    if not isinstance(write, GraphWriteResult):
                        raise TypeError(
                            "graph_service must return a GraphWriteResult, got "
                            f"{type(write).__name__}"
                        )
                except Exception as exc:
                    raise self._stage_failure(
                        _STAGE_GRAPH, material_id, stages, exc
                    ) from exc
                stages.append(PipelineStage(_STAGE_GRAPH, "written", write))

        run = PipelineRun(
            material_id=material_id,
            status=_STATUS_COMPLETED,
            correlation_id=correlation_id,
            stages=tuple(stages),
            started_at=started_at,
            finished_at=self._now(),
        )
        self._completed[material_id] = run
        if correlation_id is not None:
            self._by_correlation[correlation_id] = material_id
        return run

    @staticmethod
    def _stage_failure(
        stage: str,
        material_id: str,
        stages: list[PipelineStage],
        exc: Exception,
    ) -> PipelineError:
        return PipelineError(
            "pipeline stage "
            f"{stage!r} failed for material {redact_audit_text(material_id)!r}: "
            f"{redact_audit_text(str(exc))}",
            stage=stage,
            material_id=material_id,
            stages=stages,
        )

    def _record_failure(self, material_id: str, exc: Exception) -> None:
        """Record one failed attempt as an audit record AND a failed outcome.

        The failure audit keeps the structured stage/error/time fields, and
        the lifecycle outcome moves to ``failed`` — a failed attempt never
        leaves the material looking pending, committed, or unannounced.
        """
        audit = self._failure_audit(material_id, exc)
        self._failures[material_id] = audit
        self._persist_outcome(
            PipelineOutcome(
                material_id,
                FAILED,
                audit.failed_at,
                stage=audit.stage,
                error_type=audit.error_type,
                message=audit.message,
            )
        )

    def _persist_outcome(self, outcome: PipelineOutcome) -> None:
        """Record one lifecycle transition in memory and, when terminal, in
        the durable local outcome ledger.  ``pending`` is transient and never
        persisted, so a crash mid-run cannot leave a stale pending row."""
        self._outcomes[outcome.material_id] = outcome
        if self._outcome_store is not None and outcome.status != PENDING:
            self._outcome_store.record(outcome)

    def _failure_audit(self, material_id: str, exc: Exception) -> PipelineFailure:
        """Build the material-level audit of one failed pipeline attempt.

        ``stage`` comes from a :class:`PipelineError` when the failure
        happened inside a stage; ``error_type``/``message`` describe the
        underlying error (the cause when one is chained), never a fabricated
        success.
        """
        if isinstance(exc, PipelineError):
            stage = exc.stage
            cause = exc.__cause__
            root = cause if isinstance(cause, Exception) else exc
        else:
            stage = None
            root = exc
        message = redact_audit_text(str(root)) or type(root).__name__
        return PipelineFailure(
            material_id=material_id,
            stage=stage,
            error_type=type(root).__name__,
            message=message,
            failed_at=self._now(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value
