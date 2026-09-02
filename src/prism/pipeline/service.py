"""Incremental post-ingestion orchestration for PRISM materials.

The pipeline continues where ingestion ends: one already-ingested
:class:`~prism.ingestion.IngestionResult` is pushed through three stages in a
fixed order — index the corpus file, extract structured domain objects, then
write the resulting case bundle to the graph.  Every collaborator (indexer,
extractor, graph writer, clock, and the event-to-material resolver) is
injected, so the service is fully testable offline.  Failures raise
:class:`PipelineError` with the audit trail of already-completed stages; no
partial or fabricated results are ever returned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.events import Event
from prism.extraction import ExtractionResult
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.sources import redact_audit_text
from prism.store import IndexOutcome


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


class _GraphWriter(Protocol):
    async def add_case(
        self,
        case: EvolutionCase,
        *,
        nodes: Iterable[EvolutionNode] = (),
        facts: Iterable[TemporalFact] = (),
        claims: Iterable[Claim] = (),
        materials: Iterable[Material] = (),
    ) -> GraphWriteResult: ...


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


def _required_dependency(name: str, dependency: object, method: str):
    if dependency is None:
        raise ValueError(f"{name} is required")
    if not callable(getattr(dependency, method, None)):
        raise TypeError(f"{name} must provide {method}()")
    return dependency


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class PipelineService:
    """Orchestrate index → extract → graph-write for ingested materials.

    The service is deliberately serialized: one material runs at a time, and
    successfully processed ``material_id``/``correlation_id`` keys are never
    reprocessed.  It creates no background tasks and owns no resources.
    """

    def __init__(
        self,
        *,
        indexer: _Indexer,
        extraction_service: _Extractor,
        graph_service: _GraphWriter,
        clock: _Clock | None = None,
        material_resolver: _MaterialResolver | None = None,
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
        self._clock: _Clock = clock or (lambda: datetime.now(timezone.utc))
        self._resolver = material_resolver
        self._lock = asyncio.Lock()
        self._completed: dict[str, PipelineRun] = {}
        self._by_correlation: dict[str, str] = {}

    async def run_material(
        self, result: IngestionResult, *, correlation_id: str | None = None
    ) -> PipelineRun:
        """Run the full pipeline for one ingested material, exactly once.

        Materials whose ``access_level`` is not ``fulltext`` — an abstract,
        a bibliographic placeholder, or a blocked record — carry no full
        text of the work: they are still indexed (so the metadata stays
        searchable) but the extract and graph stages are skipped and
        recorded as such, so the placeholder is never treated as an article
        body.
        """

        if not isinstance(result, IngestionResult):
            raise TypeError("result must be an IngestionResult")
        if correlation_id is not None:
            _require_text("correlation_id", correlation_id)
        async with self._lock:
            return await self._run_locked(result, correlation_id)

    async def handle_event(self, event: Event) -> bool:
        """Handle ``material.ingested`` events; ignore everything else."""

        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if event.event_type != MATERIAL_INGESTED:
            return False
        if self._resolver is None:
            raise RuntimeError(
                "material_resolver is required to handle material.ingested events"
            )
        result = self._resolver(event)
        if not isinstance(result, IngestionResult):
            raise TypeError("material_resolver must return an IngestionResult")
        await self.run_material(result, correlation_id=event.correlation_id)
        return True

    async def _run_locked(
        self, result: IngestionResult, correlation_id: str | None
    ) -> PipelineRun:
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
                if callable(extract_material):
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
                stages.append(
                    PipelineStage(
                        _STAGE_GRAPH, _STATUS_SKIPPED, None, detail=_DETAIL_NO_CASE
                    )
                )
            else:
                try:
                    write = await self._graph.add_case(
                        extraction.case,
                        nodes=extraction.nodes,
                        facts=extraction.temporal_facts,
                        claims=extraction.claims,
                        materials=(result.material,),
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

    def _now(self) -> datetime:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise RuntimeError("clock must return timezone-aware datetimes")
        return value
