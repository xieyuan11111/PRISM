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
