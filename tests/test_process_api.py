"""Focused tests for the unified processing entry points (module: api.facade)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.api.facade import PrismAPI, ProcessMaterialResult
from prism.cases.ledger import MaterialCaseConflict
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
from prism.events import Event
from prism.extraction import (
    ExtractionConflict,
    ExtractionEvidenceGap,
    ExtractionResult,
)
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.pipeline import PipelineRun, PipelineStage
from prism.store import IndexEntry, IndexOutcome


PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

CASE = EvolutionCase(
    case_id="case-1",
    case_type="policy",
    canonical_name="Revised policy",
    start_at=datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc),
    status="active",
)
LOCATOR = EvidenceLocator(
    source_id="mat-1",
    corpus_path="corpus/2026-08/example/doc-mat-1.md",
    paragraph=1,
    quote="The agency published the revised policy.",
)
EXTRACTION = ExtractionResult(
    case=CASE,
    nodes=(
        EvolutionNode(
            id="node-1",
            case_id="case-1",
            node_type="publication",
            happened_at=PUBLISHED,
            summary="The revised policy was published.",
            source_ids=("mat-1",),
            valid_at=PUBLISHED,
            observed_at=PUBLISHED,
            evidence=(LOCATOR,),
            provenance_type="explicit",
        ),
    ),
    temporal_facts=(
        TemporalFact(
            subject="Agency",
            predicate="published",
            object="Revised policy",
            valid_at=PUBLISHED,
            invalid_at=None,
            observed_at=PUBLISHED,
            source_ids=("mat-1",),
            confidence=0.8,
            provenance_type="explicit",
            evidence=(LOCATOR,),
        ),
    ),
    claims=(
        Claim(
            claim_id="claim-1",
            actor="Agency",
            proposition="The revision improves clarity.",
            stance="support",
            stated_at=PUBLISHED,
            based_on=("mat-1",),
            evidence=(LOCATOR,),
            observed_at=PUBLISHED,
        ),
    ),
    warnings=("model flagged low confidence",),
    evidence_gaps=(
        ExtractionEvidenceGap(
            "evidence_location_failed",
            "quote was not found verbatim in material mat-1",
            "node",
            "node-9",
            ("mat-1",),
        ),
    ),
    conflicts=(
        ExtractionConflict(
            conflict_id="conflict-1",
            subject="Agency",
            predicate="published",
            alternatives=("Revised policy", "Draft policy"),
            source_ids=("mat-1",),
            evidence=(LOCATOR,),
        ),
    ),
)


def make_material(material_id: str = "mat-1") -> Material:
    return Material(
        id=material_id,
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        case_tags=("case-1",),
    )


def make_result(material: Material | None = None) -> IngestionResult:
    material = material or make_material()
    return IngestionResult(
        material=material,
        raw_path=Path("raw") / f"{material.id}.md",
        corpus_path=Path("corpus") / f"doc-{material.id}.md",
        used_ocr=False,
        extracted_via="direct",
    )


def make_entry() -> IndexEntry:
    return IndexEntry(
        source_id="mat-1",
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        path="corpus/doc-mat-1.md",
        content_hash="0" * 64,
        case_tags=("case-1",),
    )


class FakeIngestion:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ingest(self, path, metadata=None):
        self.calls.append((Path(path), metadata))
        return make_result()


class FakeStore:
    def __init__(self) -> None:
        self.entries = {"mat-1": make_entry()}

    def index_file(self, path):
        return IndexOutcome(make_entry(), "indexed")

    def get(self, source_id):
        return self.entries.get(source_id)

    def search(self, criteria, *, limit, offset):
        return SimpleNamespace(hits=())


class FakeGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("not used in these tests")

    async def add_case(self, case, **kwargs):
        return GraphWriteResult((), ("episode-1",), ())


class FakeBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event):
        self.events.append(event)


class FakePipeline:
    """Stateful stand-in: the first run executes, later runs are skipped.

    ``_outcome_provider`` mirrors the real recorder wiring: when a completed
    run's graph stage was a merged case write, the case service outcome it
    produced becomes queryable through :meth:`case_outcome_for` — exactly like
    the composed pipeline, where the recorder returns the outcome during the
    run itself.
    """

    def __init__(self, outcome_provider=None) -> None:
        self.calls: list[IngestionResult] = []
        self.executions = 0
        self._outcome_provider = outcome_provider
        self._runs: dict[str, PipelineRun] = {}
        self._outcomes: dict[str, object] = {}

    async def run_material(self, result, *, correlation_id=None):
        self.calls.append(result)
        material_id = result.material.id
        if material_id in self._runs:
            previous = self._runs[material_id]
            return PipelineRun(
                material_id=material_id,
                status="skipped",
                detail="duplicate material_id",
                correlation_id=previous.correlation_id,
            )
        run = self._completed_run(material_id)
        self._runs[material_id] = run
        self.executions += 1
        if self._outcome_provider is not None and run.stages[2].status == "written":
            outcome = self._outcome_provider(material_id)
            if outcome is not None:
                self._outcomes[material_id] = outcome
        return run

    @staticmethod
    def _completed_run(material_id: str) -> PipelineRun:
        index_stage = PipelineStage("index", "indexed", IndexOutcome(make_entry(), "indexed"))
        extract_stage = PipelineStage("extract", "extracted", EXTRACTION)
        write = GraphWriteResult((), ("merged-episode",), ())
        graph_stage = PipelineStage(
            "graph",
            "written",
            write,
            detail="merged case write across 1 accumulated material(s)",
        )
        return PipelineRun(
            material_id=material_id,
            status="completed",
            stages=(index_stage, extract_stage, graph_stage),
            started_at=PUBLISHED,
            finished_at=FETCHED,
        )

    def record_outcome(self, material_id: str, outcome: object) -> None:
        """Simulate an earlier (e.g. event-driven) completed run + outcome."""
        if material_id not in self._runs:
            self._runs[material_id] = self._completed_run(material_id)
        self._outcomes[material_id] = outcome

    def run_for(self, material_id):
        return self._runs.get(material_id)

    def case_outcome_for(self, material_id):
        return self._outcomes.get(material_id)


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, material_id):
        self.calls.append(material_id)
        return make_result()

    def __call__(self, event):
        return self.resolve(event.payload["material_id"])


class FakeCaseService:
    def __init__(self) -> None:
        self.recorded: list[tuple] = []
        self.merge_case_calls: list[str] = []
        self.merge_explicit_calls: list[tuple] = []
        self.known = {"mat-1": "case-1"}
        self.outcome = SimpleNamespace(
            case_id="case-1",
            write=GraphWriteResult((), ("merged-episode",), ()),
            material_ids=("mat-1",),
            warnings=("merger warning A",),
        )

    async def record_extraction(self, material, extraction):
        self.recorded.append((material.id, extraction))
        return self.outcome

    async def merge_case(self, case_id):
        self.merge_case_calls.append(case_id)
        return self.outcome if case_id == "case-1" else None

    async def merge_explicit(self, case_id, material_ids):
        self.merge_explicit_calls.append((case_id, tuple(material_ids)))
        return self.outcome

    def case_for_material(self, material_id):
        return self.known.get(material_id)


def make_api(**overrides) -> tuple[PrismAPI, dict]:
    cases = FakeCaseService()
    parts = {
        "ingestion": FakeIngestion(),
        "store": FakeStore(),
        "graph": FakeGraph(),
        "bus": FakeBus(),
        "pipeline": FakePipeline(outcome_provider=lambda material_id: cases.outcome),
        "resolver": FakeResolver(),
        "cases": cases,
    }
    api = PrismAPI(
        parts["ingestion"],
        parts["store"],
        parts["graph"],
        parts["bus"],
        pipeline_service=parts["pipeline"],
        case_service=parts["cases"],
        material_resolver=parts["resolver"],
    )
    return api, parts


def run(coro):
    return asyncio.run(coro)


def test_process_material_by_id_returns_pipeline_case_and_audit_warnings():
    async def main():
        api, parts = make_api()
        result = await api.process_material("mat-1")

        assert isinstance(result, ProcessMaterialResult)
        assert result.material_id == "mat-1"
        assert result.pipeline.status == "completed"
        assert result.replayed is False
        assert result.case_id == "case-1"
        # The case outcome is the outcome the pipeline run itself produced —
        # the API never runs a second, no-op merge after the pipeline.
        assert result.case_outcome is parts["cases"].outcome
        assert parts["resolver"].calls == ["mat-1"]
        assert parts["pipeline"].calls[0].material.id == "mat-1"
        assert parts["pipeline"].executions == 1
        assert parts["cases"].merge_case_calls == []

        warnings = "\n".join(result.warnings)
        assert "evidence_location_failed" in warnings
        assert "conflict-1" in warnings
        assert "model flagged low confidence" in warnings
        assert "merger warning A" in warnings

    run(main())


def test_process_material_by_path_ingests_first_and_processes_once():
    async def main():
        api, parts = make_api()
        document = Path("materials") / "policy-update.md"
        result = await api.process_material(document, metadata={"case_tags": ["case-1"]})

        assert parts["ingestion"].calls == [(document, {"case_tags": ["case-1"]})]
        assert result.material_id == "mat-1"
        assert parts["pipeline"].calls[0].material.id == "mat-1"
        assert parts["bus"].events and parts["bus"].events[0].event_type == "material.ingested"
        assert result.case_id == "case-1"
        assert result.replayed is False
        # One ingestion event, one pipeline execution, no duplicate merge.
        assert len(parts["bus"].events) == 1
        assert parts["pipeline"].executions == 1
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_reports_the_completed_run_when_deduplicated():
    async def main():
        api, parts = make_api()
        # An earlier (event-driven) run already completed this material.
        parts["pipeline"].record_outcome("mat-1", parts["cases"].outcome)
        result = await api.process_material("mat-1")

        # The replay is explicit: no new execution, no duplicate merge, and
        # the authoritative completed run and its case outcome are reported.
        assert result.replayed is True
        assert result.pipeline.status == "completed"
        assert result.pipeline.material_id == "mat-1"
        assert result.case_outcome is parts["cases"].outcome
        assert parts["pipeline"].executions == 0
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_repeat_calls_are_clearly_idempotent():
    async def main():
        api, parts = make_api()
        first = await api.process_material("mat-1")
        second = await api.process_material("mat-1")

        assert first.replayed is False
        assert second.replayed is True
        # Identical authoritative outcome, no re-execution, no re-merge.
        assert second.pipeline is first.pipeline
        assert second.case_outcome is first.case_outcome
        assert second.case_id == first.case_id == "case-1"
        assert parts["pipeline"].executions == 1
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_without_a_case_reports_it_auditably():
    async def main():
        api, parts = make_api()
        parts["cases"].known.clear()

        class CaselessPipeline(FakePipeline):
            @staticmethod
            def _completed_run(material_id):
                run = FakePipeline._completed_run(material_id)
                stages = list(run.stages)
                stages[2] = PipelineStage(
                    "graph", "skipped", None, detail="extraction produced no case"
                )
                return PipelineRun(
                    material_id=material_id,
                    status="completed",
                    stages=tuple(stages),
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )

        parts["pipeline"].__class__ = CaselessPipeline
        result = await api.process_material("mat-1")

        assert result.replayed is False
        assert result.case_id is None
        assert result.case_outcome is None
        assert parts["cases"].merge_case_calls == []
        assert "extraction produced no case" in "\n".join(result.warnings)

    run(main())


def test_process_material_reports_a_legacy_multi_case_binding_as_typed_conflict():
    """An ambiguous legacy binding never surfaces as an unexpected ValueError:
    the typed conflict carries the material id and every bound case."""

    async def main():
        api, parts = make_api()
        parts["cases"].known.clear()
        parts["cases"].outcome = None

        class AmbiguousCaseService(FakeCaseService):
            def case_for_material(self, material_id):
                raise MaterialCaseConflict(
                    material_id, ("case-1", "case-2")
                )

        parts["cases"].__class__ = AmbiguousCaseService
        parts["pipeline"].__class__ = FakePipeline  # no recorder outcome
        with pytest.raises(MaterialCaseConflict) as info:
            await api.process_material("mat-1")
        assert info.value.material_id == "mat-1"
        assert info.value.case_ids == ("case-1", "case-2")
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_reports_a_durable_binding_without_a_fake_outcome():
    """A run that produced no case outcome never fabricates one: the durable
    ledger binding is reported with an explicit rebuild hint instead."""

    async def main():
        api, parts = make_api()
        parts["cases"].outcome = None  # pipeline recorder produced nothing

        class CaselessPipeline(FakePipeline):
            @staticmethod
            def _completed_run(material_id):
                run = FakePipeline._completed_run(material_id)
                stages = list(run.stages)
                stages[2] = PipelineStage(
                    "graph", "skipped", None, detail="extraction produced no case"
                )
                return PipelineRun(
                    material_id=material_id,
                    status="completed",
                    stages=tuple(stages),
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )

        parts["pipeline"].__class__ = CaselessPipeline
        result = await api.process_material("mat-1")

        assert result.case_id == "case-1"  # durable binding, reported truthfully
        assert result.case_outcome is None  # ...but never a fake outcome
        warnings = "\n".join(result.warnings)
        assert "bound to case 'case-1'" in warnings
        assert "merge-case" in warnings
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_validates_inputs_and_dependencies():
    async def main():
        api, parts = make_api()
        with pytest.raises(LookupError, match="mat-unknown"):
            await api.process_material("mat-unknown")

        api_no_pipeline, _ = make_api()
        api_no_pipeline._pipeline = None
        with pytest.raises(ValueError, match="pipeline_service"):
            await api_no_pipeline.process_material("mat-1")

        api_no_resolver, _ = make_api()
        api_no_resolver._material_resolver = None
        with pytest.raises(ValueError, match="material_resolver"):
            await api_no_resolver.process_material("mat-1")

    run(main())


def test_merge_case_supports_full_accumulation_and_explicit_subsets():
    async def main():
        api, parts = make_api()
        outcome = await api.merge_case("case-1")
        assert outcome is parts["cases"].outcome
        assert parts["cases"].merge_case_calls == ["case-1"]
        assert parts["cases"].merge_explicit_calls == []

        subset = await api.merge_case("case-1", materials=("mat-1",))
        assert subset is parts["cases"].outcome
        assert parts["cases"].merge_explicit_calls == [("case-1", ("mat-1",))]

        with pytest.raises(LookupError, match="case-unknown"):
            await api.merge_case("case-unknown")

        api_no_cases, _ = make_api()
        api_no_cases._case_service = None
        with pytest.raises(ValueError, match="case_service"):
            await api_no_cases.merge_case("case-1")

    run(main())


def test_process_material_surfaces_a_typed_conflict_from_the_pipeline():
    """A case-binding conflict raised by the automatic accumulator inside the
    pipeline must reach process_material callers as the typed, structured
    MaterialCaseConflict — never as a generic wrapped pipeline error."""

    async def main():
        api, parts = make_api()
        from prism.cases.ledger import MaterialCaseConflict
        from prism.pipeline import PipelineError

        class ConflictPipeline(FakePipeline):
            async def run_material(self, result, *, correlation_id=None):
                conflict = MaterialCaseConflict(
                    result.material.id,
                    ("case-1",),
                    attempted_case="case-2",
                )
                raise PipelineError(
                    "pipeline stage 'graph' failed: material 'mat-1' is "
                    "already bound to case 'case-1'",
                    stage="graph",
                    material_id=result.material.id,
                ) from conflict

        parts["pipeline"].__class__ = ConflictPipeline
        with pytest.raises(MaterialCaseConflict) as info:
            await api.process_material("mat-1")
        assert info.value.material_id == "mat-1"
        assert info.value.case_ids == ("case-1",)
        assert info.value.attempted_case == "case-2"
        assert parts["cases"].merge_case_calls == []

    run(main())


def test_process_material_raises_pipeline_error_never_a_fake_success():
    """A failed attempt is raised as the structured PipelineError (stage and
    material id), and no result object is ever returned for the failure."""

    async def main():
        api, parts = make_api()
        from prism.pipeline import PipelineError

        class FailingPipeline(FakePipeline):
            async def run_material(self, result, *, correlation_id=None):
                raise PipelineError(
                    "pipeline stage 'extract' failed for material 'mat-1'",
                    stage="extract",
                    material_id=result.material.id,
                )

        parts["pipeline"].__class__ = FailingPipeline
        with pytest.raises(PipelineError) as info:
            await api.process_material("mat-1")
        assert info.value.stage == "extract"
        assert info.value.material_id == "mat-1"
        assert parts["cases"].merge_case_calls == []

    run(main())
