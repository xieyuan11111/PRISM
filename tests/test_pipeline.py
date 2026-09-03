"""Focused tests for the post-ingestion incremental pipeline (module: pipeline)."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.events import Event
from prism.extraction import ExtractionError, ExtractionResult
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.pipeline import (
    MATERIAL_INGESTED,
    PipelineError,
    PipelineOutcome,
    PipelineRun,
    PipelineService,
    PipelineStage,
)
from prism.store import IndexEntry, IndexOutcome


PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
CLOCK_STEP = timedelta(minutes=1)

CASE = EvolutionCase(
    case_id="case-1",
    case_type="policy",
    canonical_name="Revised policy",
    start_at=datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc),
    status="active",
    node_ids=("node-1",),
)
NODE = EvolutionNode(
    id="node-1",
    case_id="case-1",
    node_type="publication",
    happened_at=datetime(2026, 8, 30, 9, 30, tzinfo=timezone.utc),
    summary="The revised policy was published.",
    source_ids=("mat-1",),
    claim_ids=("claim-1",),
)
FACT = TemporalFact(
    subject="Agency",
    predicate="published",
    object="Revised policy",
    valid_at=PUBLISHED,
    invalid_at=None,
    observed_at=PUBLISHED,
    source_ids=("mat-1",),
    confidence=0.82,
    provenance_type="explicit",
)
CLAIM = Claim(
    claim_id="claim-1",
    actor="Agency",
    proposition="The revision improves clarity.",
    stance="support",
    stated_at=PUBLISHED,
    based_on=("mat-1",),
)
EXTRACTION = ExtractionResult(
    case=CASE, nodes=(NODE,), temporal_facts=(FACT,), claims=(CLAIM,)
)


def make_material(material_id: str = "mat-1", **overrides) -> Material:
    values = {
        "id": material_id,
        "title": "Policy update",
        "source": "example.test",
        "published_at": PUBLISHED,
        "fetched_at": FETCHED,
        "type": "policy",
        "content": "The agency published the revised policy.",
        "case_tags": ("case-1",),
    }
    values.update(overrides)
    return Material(**values)


def make_result(material: Material | None = None) -> IngestionResult:
    material = material or make_material()
    return IngestionResult(
        material=material,
        raw_path=Path("raw") / f"{material.id}.md",
        corpus_path=Path("corpus") / f"doc-{material.id}.md",
        used_ocr=False,
        extracted_via="direct",
    )


def make_index_outcome(status: str = "indexed") -> IndexOutcome:
    entry = IndexEntry(
        source_id="mat-1",
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        path="documents/doc-mat-1.md",
        content_hash="0" * 64,
    )
    return IndexOutcome(entry, status)


def make_event(
    event_type: str = MATERIAL_INGESTED,
    material_id: str = "mat-1",
    correlation_id: str | None = "mat-1",
    event_id: str = "evt-1",
) -> Event:
    return Event(
        event_id=event_id,
        event_type=event_type,
        occurred_at=T0,
        payload={"material_id": material_id, "corpus_path": f"corpus/doc-{material_id}.md"},
        correlation_id=correlation_id,
    )


class FakeClock:
    def __init__(self, start: datetime = T0, step: timedelta = CLOCK_STEP) -> None:
        self._next = start
        self._step = step
        self.ticks = 0

    def __call__(self) -> datetime:
        value = self._next
        self._next = value + self._step
        self.ticks += 1
        return value


class FakeIndexer:
    def __init__(self, outcome: object | None = None, exc: Exception | None = None) -> None:
        self.calls: list[Path] = []
        self.outcome = outcome
        self.exc = exc

    def index_file(self, path) -> IndexOutcome:
        self.calls.append(Path(path))
        if self.exc is not None:
            raise self.exc
        return self.outcome if self.outcome is not None else make_index_outcome()


class FakeExtractor:
    def __init__(self, result: object | None = None, exc: Exception | None = None) -> None:
        self.calls: list[Material] = []
        self.result = result
        self.exc = exc

    async def extract(self, material: Material) -> ExtractionResult:
        self.calls.append(material)
        if self.exc is not None:
            raise self.exc
        return self.result if self.result is not None else EXTRACTION


class FakeGraph:
    def __init__(self, result: object | None = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.result = result
        self.exc = exc

    async def add_case(self, case, *, nodes=(), facts=(), claims=(), materials=()):
        self.calls.append((case, tuple(nodes), tuple(facts), tuple(claims), tuple(materials)))
        if self.exc is not None:
            raise self.exc
        return (
            self.result
            if self.result is not None
            else GraphWriteResult((), ("episode-1",), ())
        )


def make_service(
    *,
    index_outcome: object | None = None,
    index_exc: Exception | None = None,
    extraction_result: object | None = None,
    extraction_exc: Exception | None = None,
    graph_result: object | None = None,
    graph_exc: Exception | None = None,
    clock=None,
    resolver=None,
):
    order: list[str] = []

    class OrderedIndexer(FakeIndexer):
        def index_file(self, path):
            order.append("index")
            return super().index_file(path)

    class OrderedExtractor(FakeExtractor):
        async def extract(self, material):
            order.append("extract")
            return await super().extract(material)

    class OrderedGraph(FakeGraph):
        async def add_case(self, case, **kwargs):
            order.append("graph")
            return await super().add_case(case, **kwargs)

    indexer = OrderedIndexer(outcome=index_outcome, exc=index_exc)
    extractor = OrderedExtractor(result=extraction_result, exc=extraction_exc)
    graph = OrderedGraph(result=graph_result, exc=graph_exc)
    service = PipelineService(
        indexer=indexer,
        extraction_service=extractor,
        graph_service=graph,
        clock=clock if clock is not None else FakeClock(),
        material_resolver=resolver,
    )
    return service, indexer, extractor, graph, order


def test_run_material_executes_index_extract_graph_in_order():
    async def main():
        result = make_result()
        service, indexer, extractor, graph, order = make_service()
        run = await service.run_material(result)

        assert order == ["index", "extract", "graph"]
        assert indexer.calls == [result.corpus_path]
        assert extractor.calls == [result.material]
        assert graph.calls == [
            (CASE, (NODE,), (FACT,), (CLAIM,), (result.material,))
        ]
        assert run.status == "completed"
        assert run.material_id == "mat-1"
        assert [stage.name for stage in run.stages] == ["index", "extract", "graph"]
        assert [stage.status for stage in run.stages] == [
            "indexed",
            "extracted",
            "written",
        ]

    asyncio.run(main())


def test_run_material_returns_auditable_immutable_records_with_injected_clock():
    async def main():
        service, _, _, _, _ = make_service()
        run = await service.run_material(make_result())

        assert isinstance(run, PipelineRun)
        assert run.started_at == T0
        assert run.finished_at == T0 + CLOCK_STEP
        assert run.started_at.tzinfo is not None
        assert run.finished_at.tzinfo is not None

        index_stage, extract_stage, graph_stage = run.stages
        assert isinstance(index_stage, PipelineStage)
        assert isinstance(index_stage.result, IndexOutcome)
        assert index_stage.result.status == "indexed"
        assert isinstance(extract_stage.result, ExtractionResult)
        assert extract_stage.result.case is CASE
        assert isinstance(graph_stage.result, GraphWriteResult)
        assert graph_stage.result.added_keys == ("episode-1",)

        with pytest.raises(FrozenInstanceError):
            run.status = "skipped"
        with pytest.raises(FrozenInstanceError):
            index_stage.status = "boom"
        assert not hasattr(run, "__dict__")
        assert not hasattr(index_stage, "__dict__")

    asyncio.run(main())


def test_run_material_is_idempotent_for_a_repeated_material_id():
    async def main():
        service, indexer, extractor, graph, order = make_service()
        first = await service.run_material(make_result())
        assert first.status == "completed"

        second = await service.run_material(make_result())
        assert second.status == "skipped"
        assert "duplicate" in second.detail
        assert "material_id" in second.detail
        assert second.stages == ()
        assert order == ["index", "extract", "graph"]
        assert len(indexer.calls) == 1
        assert len(extractor.calls) == 1
        assert len(graph.calls) == 1

        third = await service.run_material(make_result())
        assert third == second

    asyncio.run(main())


def test_run_material_is_idempotent_for_a_repeated_correlation_id():
    async def main():
        service, indexer, extractor, graph, _ = make_service()
        result_a = make_result(make_material("mat-a"))
        result_b = make_result(make_material("mat-b"))

        first = await service.run_material(result_a, correlation_id="corr-42")
        assert first.status == "completed"
        assert first.correlation_id == "corr-42"

        second = await service.run_material(result_b, correlation_id="corr-42")
        assert second.status == "skipped"
        assert "duplicate" in second.detail
        assert "correlation_id" in second.detail
        assert len(extractor.calls) == 1
        assert len(graph.calls) == 1

    asyncio.run(main())


def test_index_failure_raises_pipeline_error_and_stays_retryable():
    async def main():
        boom = OSError("corpus volume offline")
        service, indexer, extractor, graph, _ = make_service(index_exc=boom)

        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        error = info.value
        assert error.stage == "index"
        assert error.material_id == "mat-1"
        assert error.stages == ()
        assert error.__cause__ is boom
        assert extractor.calls == []
        assert graph.calls == []

        indexer.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"

    asyncio.run(main())


def test_extraction_failure_reports_the_completed_index_stage():
    async def main():
        boom = ExtractionError("completion is not valid JSON")
        service, indexer, extractor, graph, _ = make_service(extraction_exc=boom)

        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        error = info.value
        assert error.stage == "extract"
        assert error.__cause__ is boom
        assert [stage.name for stage in error.stages] == ["index"]
        assert error.stages[0].result.status == "indexed"
        assert graph.calls == []

        extractor.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"

    asyncio.run(main())


def test_graph_failure_reports_index_and_extract_stages():
    async def main():
        boom = RuntimeError("graph backend unavailable")
        service, indexer, extractor, graph, _ = make_service(graph_exc=boom)

        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        error = info.value
        assert error.stage == "graph"
        assert error.__cause__ is boom
        assert [stage.name for stage in error.stages] == ["index", "extract"]
        assert len(graph.calls) == 1

        graph.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"

    asyncio.run(main())


def test_pipeline_error_audit_message_redacts_credentials_but_keeps_cause():
    async def main():
        secret = "pipeline-test-secret"
        boom = RuntimeError(f"api_key={secret} Bearer {secret}")
        service, *_ = make_service(extraction_exc=boom)

        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        assert secret not in str(info.value)
        assert "api_key=<redacted>" in str(info.value)
        assert "Bearer <redacted>" in str(info.value)
        assert info.value.__cause__ is boom

    asyncio.run(main())


def test_non_fulltext_materials_are_indexed_but_skip_extraction_and_graph():
    async def main():
        for level in ("abstract_only", "metadata_only"):
            service, indexer, extractor, graph, order = make_service()
            material = make_material(
                access_level=level,
                retrieval_level=level,
                type="academic",
                source="academic",
            )
            run = await service.run_material(make_result(material))

            # The placeholder is never handed to extraction or the graph, but
            # the material is still indexed so its metadata stays searchable.
            assert order == ["index"]
            assert indexer.calls == [Path("corpus") / f"doc-{material.id}.md"]
            assert extractor.calls == []
            assert graph.calls == []
            assert run.status == "completed"
            assert [stage.name for stage in run.stages] == [
                "index",
                "extract",
                "graph",
            ]
            assert [stage.status for stage in run.stages] == [
                "indexed",
                "skipped",
                "skipped",
            ]
            assert run.stages[1].result is None
            assert run.stages[2].result is None
            assert level in run.stages[1].detail
            assert level in run.stages[2].detail

    asyncio.run(main())


def test_blocked_materials_skip_extraction_and_graph():
    """A ``blocked`` material never reaches extraction or the graph.

    Regression for the review finding: the non-fulltext skip set covered
    only ``abstract_only``/``metadata_only``, so a material whose access
    level is ``blocked`` fell through to the extract and graph stages.
    """
    async def main():
        service, indexer, extractor, graph, order = make_service()
        material = make_material(
            access_level="blocked",
            retrieval_level="blocked",
            type="academic",
            source="academic",
        )
        run = await service.run_material(make_result(material))

        assert order == ["index"]
        assert indexer.calls == [Path("corpus") / f"doc-{material.id}.md"]
        assert extractor.calls == []
        assert graph.calls == []
        assert run.status == "completed"
        assert [stage.name for stage in run.stages] == ["index", "extract", "graph"]
        assert [stage.status for stage in run.stages] == [
            "indexed",
            "skipped",
            "skipped",
        ]
        assert "blocked" in run.stages[1].detail
        assert "blocked" in run.stages[2].detail

    asyncio.run(main())


def test_fulltext_materials_still_run_extraction_and_graph():
    async def main():
        service, indexer, extractor, graph, order = make_service()
        material = make_material(access_level="fulltext", retrieval_level="fulltext")
        run = await service.run_material(make_result(material))

        assert order == ["index", "extract", "graph"]
        assert extractor.calls == [material]
        assert graph.calls == [
            (CASE, (NODE,), (FACT,), (CLAIM,), (material,))
        ]
        assert run.status == "completed"
        assert [stage.status for stage in run.stages] == [
            "indexed",
            "extracted",
            "written",
        ]

    asyncio.run(main())


def test_caseless_extraction_skips_the_graph_stage_without_fabricating_a_case():
    async def main():
        service, indexer, extractor, graph, order = make_service(
            extraction_result=ExtractionResult()
        )
        run = await service.run_material(make_result())

        assert run.status == "completed"
        assert order == ["index", "extract"]
        assert [stage.name for stage in run.stages] == ["index", "extract", "graph"]
        graph_stage = run.stages[2]
        assert graph_stage.status == "skipped"
        assert graph_stage.result is None
        assert "no case" in graph_stage.detail
        assert graph.calls == []

        duplicate = await service.run_material(make_result())
        assert duplicate.status == "skipped"

    asyncio.run(main())


def test_unexpected_stage_result_types_fail_explicitly():
    async def main():
        service, indexer, *_ = make_service()
        indexer.outcome = "ok"

        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        assert info.value.stage == "index"

        indexer.outcome = None
        service2, _, extractor2, _graph2, _ = make_service()
        extractor2.result = {"case": None}
        with pytest.raises(PipelineError) as info2:
            await service2.run_material(make_result())
        assert info2.value.stage == "extract"

    asyncio.run(main())


def test_handle_event_ignores_events_other_than_material_ingested():
    async def main():
        service, indexer, extractor, graph, _ = make_service()
        handled = await service.handle_event(
            make_event(event_type="material.reindexed")
        )

        assert handled is False
        assert indexer.calls == []
        assert extractor.calls == []
        assert graph.calls == []

    asyncio.run(main())


def test_handle_event_processes_material_ingested_events_idempotently():
    async def main():
        result = make_result()

        def resolver(event: Event) -> IngestionResult:
            assert event.event_type == MATERIAL_INGESTED
            assert event.payload["material_id"] == result.material.id
            return result

        service, indexer, extractor, graph, _ = make_service(resolver=resolver)
        assert await service.handle_event(make_event()) is True
        assert len(indexer.calls) == 1
        assert len(extractor.calls) == 1
        assert len(graph.calls) == 1

        assert await service.handle_event(make_event(event_id="evt-2")) is True
        assert len(indexer.calls) == 1
        assert len(extractor.calls) == 1
        assert len(graph.calls) == 1

    asyncio.run(main())


def test_handle_event_requires_a_resolver_for_material_ingested():
    async def main():
        service, *_ = make_service()
        with pytest.raises(RuntimeError, match="material_resolver"):
            await service.handle_event(make_event())

    asyncio.run(main())


def test_constructor_and_input_validation():
    async def main():
        indexer = FakeIndexer()
        extractor = FakeExtractor()
        graph = FakeGraph()

        with pytest.raises(ValueError):
            PipelineService(indexer=None, extraction_service=extractor, graph_service=graph)
        with pytest.raises(ValueError):
            PipelineService(indexer=indexer, extraction_service=None, graph_service=graph)
        with pytest.raises(ValueError):
            PipelineService(indexer=indexer, extraction_service=extractor, graph_service=None)
        with pytest.raises(TypeError):
            PipelineService(
                indexer=object(),
                extraction_service=extractor,
                graph_service=graph,
            )
        with pytest.raises(TypeError):
            PipelineService(
                indexer=indexer,
                extraction_service=extractor,
                graph_service=graph,
                clock="not-callable",
            )
        with pytest.raises(TypeError):
            PipelineService(
                indexer=indexer,
                extraction_service=extractor,
                graph_service=graph,
                material_resolver="not-callable",
            )

        service = PipelineService(
            indexer=indexer, extraction_service=extractor, graph_service=graph
        )
        with pytest.raises(TypeError):
            await service.run_material("not-an-ingestion-result")
        with pytest.raises(ValueError):
            await service.run_material(make_result(), correlation_id="   ")

    asyncio.run(main())


def test_clock_must_return_timezone_aware_datetimes():
    async def main():
        service, *_ = make_service(clock=lambda: datetime(2026, 9, 1, 8, 0))
        with pytest.raises(RuntimeError, match="timezone-aware"):
            await service.run_material(make_result())

    asyncio.run(main())


def test_no_background_tasks_are_created():
    async def main():
        result = make_result()
        service, *_ = make_service(resolver=lambda event: result)
        await service.run_material(result)
        await service.handle_event(make_event(event_id="evt-2"))
        current = asyncio.current_task()
        assert {task for task in asyncio.all_tasks() if task is not current} == set()

    asyncio.run(main())


# ------------------------------------------------- accumulated case writing


from types import SimpleNamespace  # noqa: E402


class FakeCaseRecorder:
    """Stand-in for prism.cases.CaseService.record_extraction."""

    def __init__(self, outcome: object | None = None, exc: Exception | None = None):
        self.calls: list[tuple[Material, ExtractionResult]] = []
        self.outcome = outcome
        self.exc = exc

    async def record_extraction(self, material, extraction):
        self.calls.append((material, extraction))
        if self.exc is not None:
            raise self.exc
        return (
            self.outcome
            if self.outcome is not None
            else SimpleNamespace(
                write=GraphWriteResult((), ("merged-episode",), ()),
                material_ids=("mat-1",),
            )
        )


def make_recorded_service(recorder: FakeCaseRecorder, **kwargs):
    service, indexer, extractor, graph, order = make_service(**kwargs)
    service._case_recorder = recorder
    return service, indexer, extractor, graph, order


def test_case_service_writes_the_merged_case_instead_of_a_per_material_case():
    async def main():
        recorder = FakeCaseRecorder()
        service, indexer, extractor, graph, order = make_recorded_service(recorder)
        result = make_result()
        run = await service.run_material(result)

        assert order == ["index", "extract"]
        assert recorder.calls == [(result.material, EXTRACTION)]
        # The pipeline itself must not write one full case per material; the
        # accumulated merged write belongs to the case service.
        assert graph.calls == []
        graph_stage = run.stages[2]
        assert graph_stage.status == "written"
        assert graph_stage.result == GraphWriteResult((), ("merged-episode",), ())
        assert "1 accumulated material(s)" in graph_stage.detail

    asyncio.run(main())


def test_case_service_failures_raise_auditable_retryable_pipeline_errors():
    async def main():
        recorder = FakeCaseRecorder(exc=ValueError("foreign case"))
        service, *_ = make_recorded_service(recorder)
        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        assert info.value.stage == "graph"
        assert [stage.name for stage in info.value.stages] == ["index", "extract"]

        recorder.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"

    asyncio.run(main())


def test_case_service_outcome_must_expose_a_graph_write_result():
    async def main():
        recorder = FakeCaseRecorder(outcome=SimpleNamespace(material_ids=()))
        service, *_ = make_recorded_service(recorder)
        with pytest.raises(PipelineError) as info:
            await service.run_material(make_result())
        assert info.value.stage == "graph"

    asyncio.run(main())


def test_case_service_receives_no_caseless_or_non_fulltext_extractions():
    async def main():
        recorder = FakeCaseRecorder()
        service, *_ = make_recorded_service(recorder)
        run = await service.run_material(make_result())
        assert len(recorder.calls) == 1

        caseless, *_ = make_recorded_service(
            FakeCaseRecorder(), extraction_result=ExtractionResult()
        )
        run = await caseless.run_material(make_result())
        assert run.stages[2].status == "skipped"
        assert "no case" in run.stages[2].detail

        blocked_material = make_material(
            access_level="blocked", retrieval_level="blocked"
        )
        blocked, *_ = make_recorded_service(FakeCaseRecorder())
        run = await blocked.run_material(make_result(blocked_material))
        assert run.stages[1].status == "skipped"
        assert run.stages[2].status == "skipped"

    asyncio.run(main())


def test_run_for_returns_the_last_completed_run_only():
    async def main():
        service, *_ = make_service()
        assert service.run_for("mat-1") is None
        completed = await service.run_material(make_result())
        assert service.run_for("mat-1") is completed
        skipped = await service.run_material(make_result())
        assert skipped.status == "skipped"
        assert service.run_for("mat-1") is completed
        assert service.run_for("mat-other") is None

    asyncio.run(main())


# ------------------------------------- failure audit and recorder outcomes


def test_failed_runs_are_auditable_per_material_and_cleared_by_retry():
    async def main():
        service, *_ = make_service(extraction_exc=RuntimeError("extractor exploded"))
        with pytest.raises(PipelineError):
            await service.run_material(make_result())

        # No completed run may exist for the failed material...
        assert service.run_for("mat-1") is None
        # ...and the failure itself is a queryable audit record: material id,
        # stage, error type, message and time are all present.
        failure = service.failure_for("mat-1")
        assert failure is not None
        assert failure.material_id == "mat-1"
        assert failure.stage == "extract"
        assert failure.error_type == "RuntimeError"
        assert "extractor exploded" in failure.message
        assert failure.failed_at is not None
        assert failure.failed_at.tzinfo is not None
        assert failure.failed_at >= T0
        assert service.failure_for("mat-other") is None

        # A successful retry clears the stale failure audit.
        service._extraction.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"
        assert service.failure_for("mat-1") is None

    asyncio.run(main())


def test_case_write_failures_keep_the_stage_in_the_failure_audit():
    async def main():
        recorder = FakeCaseRecorder(exc=ValueError("foreign case"))
        service, *_ = make_recorded_service(recorder)
        with pytest.raises(PipelineError):
            await service.run_material(make_result())
        failure = service.failure_for("mat-1")
        assert failure is not None
        assert failure.stage == "graph"
        assert failure.error_type == "ValueError"
        assert "foreign case" in failure.message

    asyncio.run(main())


def test_failure_audit_validates_material_ids():
    service, *_ = make_service()
    with pytest.raises(ValueError):
        service.failure_for("   ")


def test_event_resolution_failures_are_auditable_with_the_material_id():
    async def main():
        def resolver(event):
            raise LookupError("material not found: mat-1")

        service, *_ = make_service(resolver=resolver)
        with pytest.raises(LookupError, match="mat-1"):
            await service.handle_event(make_event())

        # Pre-stage failures keep the audit record too: material id, no
        # stage, error type and time are all queryable.
        failure = service.failure_for("mat-1")
        assert failure is not None
        assert failure.material_id == "mat-1"
        assert failure.stage is None
        assert failure.error_type == "LookupError"
        assert failure.failed_at is not None
        assert failure.failed_at.tzinfo is not None

        # A later successful event run clears the stale audit.
        healthy, *_ = make_service()
        await healthy.run_material(make_result())
        assert healthy.failure_for("mat-1") is None

    asyncio.run(main())


def test_case_outcome_for_exposes_the_recorded_outcome_of_a_material():
    async def main():
        outcome = SimpleNamespace(
            write=GraphWriteResult((), ("merged-episode",), ()),
            material_ids=("mat-1",),
        )
        recorder = FakeCaseRecorder(outcome=outcome)
        service, *_ = make_recorded_service(recorder)
        run = await service.run_material(make_result())
        assert run.status == "completed"
        # The pipeline run's merged case write produced an outcome for THIS
        # material; it is queryable without re-merging the case.
        assert service.case_outcome_for("mat-1") is outcome
        assert service.case_outcome_for("mat-other") is None

        # A second (skipped) attempt still reports the recorded outcome.
        skipped = await service.run_material(make_result())
        assert skipped.status == "skipped"
        assert service.case_outcome_for("mat-1") is outcome

    asyncio.run(main())


def test_case_outcome_for_is_none_without_a_recorder_or_case():
    async def main():
        service, *_ = make_service()
        run = await service.run_material(make_result())
        assert run.status == "completed"
        assert service.case_outcome_for("mat-1") is None

        caseless, *_ = make_recorded_service(
            FakeCaseRecorder(), extraction_result=ExtractionResult()
        )
        run = await caseless.run_material(make_result())
        assert run.stages[2].status == "skipped"
        assert caseless.case_outcome_for("mat-1") is None

    asyncio.run(main())


# ------------------------------------------------------ outcome lifecycle audit


class GatedExtractor(FakeExtractor):
    """An extractor whose first extraction blocks until released."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, material: Material) -> ExtractionResult:
        self.started.set()
        await self.release.wait()
        return await super().extract(material)


def test_outcome_tracks_pending_then_committed():
    """A run in flight is queryable as pending; a successful run is committed.

    Pending is the honest state of an announced material whose pipeline has
    started but not finished — never an absence that could be confused with
    "never announced" or with success.
    """

    async def main():
        extractor = GatedExtractor()
        service = PipelineService(
            indexer=FakeIndexer(),
            extraction_service=extractor,
            graph_service=FakeGraph(),
            clock=FakeClock(),
        )
        result = make_result()
        assert service.outcome_for("mat-1") is None

        attempt = asyncio.create_task(service.run_material(result))
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        pending = service.outcome_for("mat-1")
        assert pending is not None
        assert isinstance(pending, PipelineOutcome)
        assert pending.status == "pending"
        assert pending.material_id == "mat-1"
        assert pending.occurred_at == T0
        assert pending.occurred_at.tzinfo is not None
        # Pending is neither a fake success nor a failure.
        assert pending.stage is None
        assert pending.error_type is None
        assert service.run_for("mat-1") is None
        assert service.failure_for("mat-1") is None

        extractor.release.set()
        run = await asyncio.wait_for(attempt, timeout=1)
        assert run.status == "completed"

        committed = service.outcome_for("mat-1")
        assert committed is not None
        assert committed.status == "committed"
        assert committed.occurred_at == T0 + CLOCK_STEP
        assert committed.error_type is None
        assert service.failure_for("mat-1") is None

    asyncio.run(main())


def test_outcome_records_failure_structured_fields_and_retry_commits():
    async def main():
        boom = RuntimeError("extractor exploded")
        service, *_ = make_service(extraction_exc=boom)
        with pytest.raises(PipelineError):
            await service.run_material(make_result())

        # No fake success: no completed run and no committed outcome.
        assert service.run_for("mat-1") is None
        outcome = service.outcome_for("mat-1")
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.material_id == "mat-1"
        assert outcome.stage == "extract"
        assert outcome.error_type == "RuntimeError"
        assert "extractor exploded" in outcome.message
        assert outcome.occurred_at is not None
        assert outcome.occurred_at.tzinfo is not None
        assert outcome.occurred_at >= T0
        assert service.outcome_for("mat-other") is None

        # A safe retry moves the material from failed to committed and clears
        # the stale failure audit; the failed state is never terminal fiction.
        service._extraction.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"
        assert service.outcome_for("mat-1").status == "committed"
        assert service.failure_for("mat-1") is None

    asyncio.run(main())


def test_outcome_is_committed_only_after_recorder_and_graph_write_succeed():
    async def main():
        recorder = FakeCaseRecorder(exc=ValueError("graph backend unavailable"))
        service, *_ = make_recorded_service(recorder)
        with pytest.raises(PipelineError):
            await service.run_material(make_result())
        assert service.outcome_for("mat-1").status == "failed"
        assert service.outcome_for("mat-1").stage == "graph"

        recorder.exc = None
        retried = await service.run_material(make_result())
        assert retried.status == "completed"
        assert service.outcome_for("mat-1").status == "committed"
        assert service.outcome_for("mat-1").error_type is None

    asyncio.run(main())


def test_outcomes_enumeration_and_skipped_duplicates_never_clobber():
    async def main():
        service, *_ = make_service()
        assert service.outcomes() == ()
        await service.run_material(make_result(make_material("mat-a")))
        await service.run_material(make_result(make_material("mat-b")))

        outcomes = service.outcomes()
        assert [item.material_id for item in outcomes] == ["mat-a", "mat-b"]
        assert [item.status for item in outcomes] == ["committed", "committed"]

        # A skipped duplicate attempt is not a new lifecycle transition: the
        # authoritative committed outcome stays put and no second record is
        # invented for the same material.
        skipped = await service.run_material(make_result(make_material("mat-a")))
        assert skipped.status == "skipped"
        assert service.outcome_for("mat-a").status == "committed"
        assert service.outcome_for("mat-a").occurred_at == outcomes[0].occurred_at
        assert len(service.outcomes()) == 2

    asyncio.run(main())


def test_handle_event_resolution_failures_record_a_failed_outcome():
    async def main():
        def resolver(event):
            raise LookupError("material not found: mat-1")

        service, *_ = make_service(resolver=resolver)
        with pytest.raises(LookupError, match="mat-1"):
            await service.handle_event(make_event())

        outcome = service.outcome_for("mat-1")
        assert outcome is not None
        assert outcome.status == "failed"
        assert outcome.stage is None  # the failure preceded any stage
        assert outcome.error_type == "LookupError"
        assert outcome.occurred_at is not None
        assert outcome.occurred_at.tzinfo is not None

    asyncio.run(main())


def test_pipeline_outcome_validates_inputs_and_is_immutable():
    from dataclasses import FrozenInstanceError

    with pytest.raises(ValueError):
        PipelineOutcome("  ", "pending", T0)
    with pytest.raises(ValueError):
        PipelineOutcome("mat-1", "bogus", T0)
    with pytest.raises(ValueError):
        PipelineOutcome("mat-1", "pending", datetime(2026, 9, 1, 8, 0))
    # A failed record must say what failed; a success must not carry failure
    # fields that could be mistaken for one.
    with pytest.raises(ValueError):
        PipelineOutcome("mat-1", "failed", T0)
    with pytest.raises(ValueError):
        PipelineOutcome("mat-1", "failed", T0, error_type="RuntimeError")
    with pytest.raises(ValueError):
        PipelineOutcome("mat-1", "committed", T0, error_type="RuntimeError")

    failed = PipelineOutcome(
        "mat-1",
        "failed",
        T0,
        stage="extract",
        error_type="RuntimeError",
        message="extractor exploded",
    )
    assert failed.status == "failed"
    assert failed.stage == "extract"
    with pytest.raises(FrozenInstanceError):
        failed.status = "committed"

    committed = PipelineOutcome(
        "mat-1", "committed", T0 + CLOCK_STEP, correlation_id="corr-1"
    )
    assert committed.correlation_id == "corr-1"
    assert committed.error_type is None
