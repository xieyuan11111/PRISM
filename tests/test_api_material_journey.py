"""Focused tests for the facade-level material journey queries (WB-2.6).

``PrismAPI.material_journey`` / ``material_journeys`` compose the evidence
store, the pipeline's public query methods (run, failure, outcome, audit,
case outcome), the durable case binding and the run audit's report-version
annotation into one read-only :class:`MaterialJourneyView`.  The WebUI must
consume exactly this projection — never assemble its own fact view — and the
composition must never invent success for pending, unknown or failed
materials.  Everything here is offline with duck-typed fakes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from prism.api.facade import PrismAPI
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
from prism.pipeline.outcomes import (
    PipelineOutcome,
    PipelineRunAudit,
    StageAuditRecord,
)
from prism.pipeline.service import PipelineFailure, PipelineRun, PipelineStage
from prism.store import IndexEntry, IndexOutcome


UTC = timezone.utc
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
STARTED = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 1, 8, 0, 5, tzinfo=UTC)
FAILED_AT = datetime(2026, 9, 1, 8, 0, 3, tzinfo=UTC)

CASE = EvolutionCase(
    case_id="case-1",
    case_type="policy",
    canonical_name="Revised policy",
    start_at=datetime(2026, 8, 29, 8, 0, tzinfo=UTC),
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


def run(coro):
    return asyncio.run(coro)


def make_material(material_id: str = "mat-1") -> Material:
    return Material(
        id=material_id,
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
    )


def make_result(material_id: str = "mat-1") -> IngestionResult:
    return IngestionResult(
        material=make_material(material_id),
        raw_path=Path("raw") / f"{material_id}.md",
        corpus_path=Path("corpus") / f"doc-{material_id}.md",
        used_ocr=False,
        extracted_via="direct",
    )


def make_entry(material_id: str = "mat-1") -> IndexEntry:
    return IndexEntry(
        source_id=material_id,
        title="Policy update",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content="The agency published the revised policy.",
        path=f"corpus/doc-{material_id}.md",
        content_hash="0" * 64,
        original_format="md",
        raw_path=f"raw/{material_id}.md",
    )


def completed_run(material_id: str = "mat-1") -> PipelineRun:
    return PipelineRun(
        material_id=material_id,
        status="completed",
        stages=(
            PipelineStage(
                "index", "indexed", IndexOutcome(make_entry(material_id), "indexed")
            ),
            PipelineStage("extract", "extracted", EXTRACTION),
            PipelineStage(
                "graph",
                "written",
                GraphWriteResult((), ("merged-episode",), ()),
                detail="merged case write across 1 accumulated material(s)",
            ),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
        correlation_id="corr-1",
    )


def committed_audit(material_id: str = "mat-1") -> PipelineRunAudit:
    return PipelineRunAudit(
        material_id=material_id,
        status="completed",
        stages=(
            StageAuditRecord("index", "indexed"),
            StageAuditRecord("extract", "extracted"),
            StageAuditRecord(
                "graph", "written", detail="merged case write across 1 material(s)"
            ),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
        correlation_id="corr-1",
        report_version_id="rv-1",
    )


class FakeIngestion:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def ingest(self, path, metadata=None):
        self.calls.append((Path(path), metadata))
        return make_result()


class FakeStore:
    def __init__(self, *material_ids: str) -> None:
        self.entries = {
            material_id: make_entry(material_id)
            for material_id in (material_ids or ("mat-1",))
        }

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


class FakeJourneyPipeline:
    """Duck-typed pipeline query surface for journey composition tests."""

    def __init__(self, *, material_ids=("mat-1",), runs=None, outcomes=None,
                 failures=None, audits=None, case_outcomes=None) -> None:
        self.material_ids = tuple(material_ids)
        self.runs = runs or {}
        self._outcome_rows = outcomes or {}
        self.failures = failures or {}
        self.audits = audits or {}
        self.case_outcomes = case_outcomes or {}
        self.notes: list[tuple[str, str]] = []
        self.run_calls: list[object] = []

    async def run_material(self, result, *, correlation_id=None,
                           target_case=None):
        self.run_calls.append(result)
        material_id = result.material.id
        run = self.runs.get(material_id) or completed_run(material_id)
        self.runs[material_id] = run
        return run

    def run_for(self, material_id):
        return self.runs.get(material_id)

    def failure_for(self, material_id):
        return self.failures.get(material_id)

    def outcome_for(self, material_id):
        return self._outcome_rows.get(material_id)

    def outcomes(self):
        return tuple(
            self._outcome_rows[material_id]
            for material_id in self.material_ids
            if material_id in self._outcome_rows
        )

    def audit_for(self, material_id):
        return self.audits.get(material_id)

    def case_outcome_for(self, material_id):
        return self.case_outcomes.get(material_id)

    def note_report_version(self, material_id, version_id):
        self.notes.append((material_id, version_id))


class FakeCaseService:
    def __init__(self, known=None) -> None:
        self.known = known if known is not None else {"mat-1": "case-1"}
        self.loaded: list[str] = []

    async def record_extraction(self, material, extraction):
        raise AssertionError("not used in these tests")

    async def merge_case(self, case_id):
        raise AssertionError("not used in these tests")

    async def merge_explicit(self, case_id, material_ids):
        raise AssertionError("not used in these tests")

    async def bind_material_to_case(self, material_id, case_id):
        raise AssertionError("not used in these tests")

    def case_for_material(self, material_id):
        return self.known.get(material_id)

    def load_case(self, case_id):
        self.loaded.append(case_id)
        if case_id != "case-1":
            return None
        return CASE


class FakeResolver:
    def resolve(self, material_id):
        return make_result(material_id)

    def __call__(self, event):
        return self.resolve(event.payload["material_id"])


def make_api(store=None, pipeline=None, cases=None) -> PrismAPI:
    return PrismAPI(
        FakeIngestion(),
        store if store is not None else FakeStore(),
        FakeGraph(),
        FakeBus(),
        pipeline_service=pipeline,
        case_service=cases if cases is not None else FakeCaseService(),
        material_resolver=FakeResolver(),
    )


# ------------------------------------------------------------ journey assembly


def test_journey_requires_a_pipeline_service():
    api = make_api(pipeline=None)

    with pytest.raises(ValueError, match="pipeline_service"):
        run(api.material_journey("mat-1"))


def test_journey_of_an_unknown_material_raises_lookup_error():
    api = make_api(pipeline=FakeJourneyPipeline())

    with pytest.raises(LookupError, match="mat-x"):
        run(api.material_journey("mat-x"))


def test_journey_validates_the_material_id():
    api = make_api(pipeline=FakeJourneyPipeline())

    with pytest.raises(ValueError):
        run(api.material_journey("  "))


def test_journey_composes_every_projection_source():
    pipeline = FakeJourneyPipeline(
        runs={"mat-1": completed_run()},
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED,
                                           correlation_id="corr-1")},
        audits={"mat-1": committed_audit()},
        case_outcomes={"mat-1": SimpleNamespace(case_id="case-1")},
    )
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.material_id == "mat-1"
    assert view.display_name == "Policy update"
    assert view.source_format == "md"
    assert view.case_id == "case-1"
    assert view.raw_path == "raw/mat-1.md"
    assert view.corpus_path == "corpus/doc-mat-1.md"
    assert view.content_hash == "0" * 64
    assert view.fetched_at == FETCHED
    assert view.occurred_at == FINISHED
    assert view.lifecycle_status == "committed"
    assert view.run is not None
    assert view.run_audit is not None
    assert view.failure is None
    assert view.mechanism_status == "pass"
    assert view.semantic_status == "unknown"
    assert view.evidence_gap_count == 1
    assert view.evidence_gaps == (
        "evidence_location_failed on node: quote was not found verbatim in "
        "material mat-1",
    )
    assert view.unresolved_conflicts == (
        "unresolved conflict conflict-1: Agency published -> Revised policy | "
        "Draft policy",
    )
    assert view.report_version_id == "rv-1"


def test_journey_projects_case_binding_from_the_durable_ledger():
    pipeline = FakeJourneyPipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED)},
        audits={"mat-1": committed_audit()},
    )
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.case_id == "case-1"
    assert view.lifecycle_status == "committed"


def test_journey_without_any_outcome_reports_unknown_honestly():
    pipeline = FakeJourneyPipeline()
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.lifecycle_status == "unknown"
    assert view.mechanism_status == "unknown"
    assert view.semantic_status == "unknown"
    assert view.evidence_gap_count is None
    assert view.report_version_id is None


def test_journey_of_a_failed_attempt_carries_the_audit_trail():
    failure = PipelineFailure(
        material_id="mat-1",
        stage="extract",
        error_type="RuntimeError",
        message="extractor exploded",
        failed_at=FAILED_AT,
    )
    pipeline = FakeJourneyPipeline(
        runs={},
        outcomes={
            "mat-1": PipelineOutcome(
                "mat-1", "failed", FAILED_AT, stage="extract",
                error_type="RuntimeError", message="extractor exploded",
            )
        },
        failures={"mat-1": failure},
        audits={
            "mat-1": PipelineRunAudit(
                "mat-1", "failed",
                stages=(StageAuditRecord("index", "indexed"),),
                finished_at=FAILED_AT,
            )
        },
    )
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.lifecycle_status == "failed"
    assert view.mechanism_status == "fail"
    assert view.failure is failure
    assert view.run is None
    assert view.run_audit is not None
    assert view.run_audit.status == "failed"


def test_journey_of_a_pending_material_is_never_a_success():
    pipeline = FakeJourneyPipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "pending", STARTED)},
    )
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.lifecycle_status == "pending"
    assert view.mechanism_status == "unknown"


def test_journey_gaps_come_only_from_the_in_process_run():
    # Post-restart (no run, audit only) the per-material gap detail is not
    # retained: the count must report unknown, never a fabricated zero.
    pipeline = FakeJourneyPipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED)},
        audits={"mat-1": committed_audit()},
    )
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.evidence_gap_count is None
    assert view.evidence_gaps == ()


def test_journey_projects_stored_absolute_raw_paths_as_relative():
    entry = make_entry()
    object.__setattr__(
        entry, "raw_path",
        "E:/Users/private/.prism/raw/mat-1.md".replace("\\", "/"),
    )
    store = FakeStore()
    store.entries = {"mat-1": entry}
    pipeline = FakeJourneyPipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED)},
    )
    api = make_api(store=store, pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    assert view.raw_path == "raw/mat-1.md"
    assert view.corpus_path == "corpus/doc-mat-1.md"

    object.__setattr__(entry, "raw_path", "E:/elsewhere/unknown/binary.bin")
    view = run(api.material_journey("mat-1"))
    assert view.raw_path is None


# ----------------------------------------------------------------- journeys()


def test_journeys_list_and_filter_by_status_and_case():
    pipeline = FakeJourneyPipeline(
        material_ids=("mat-1", "mat-2", "mat-3"),
        outcomes={
            "mat-1": PipelineOutcome("mat-1", "committed", FINISHED),
            "mat-2": PipelineOutcome(
                "mat-2", "failed", FAILED_AT, stage="extract",
                error_type="RuntimeError", message="extractor exploded",
            ),
            "mat-3": PipelineOutcome("mat-3", "pending", STARTED),
        },
    )
    api = make_api(
        store=FakeStore("mat-1", "mat-2", "mat-3"),
        pipeline=pipeline,
        cases=FakeCaseService(known={"mat-1": "case-1", "mat-2": "case-2"}),
    )

    views = run(api.material_journeys())

    assert [view.material_id for view in views] == [
        "mat-1", "mat-2", "mat-3",
    ]  # most recent outcome first

    failed = run(api.material_journeys(status="failed"))
    assert [view.material_id for view in failed] == ["mat-2"]

    in_case = run(api.material_journeys(case_id="case-1"))
    assert [view.material_id for view in in_case] == ["mat-1"]


def test_journeys_validate_their_filters():
    api = make_api(pipeline=FakeJourneyPipeline())

    with pytest.raises(ValueError, match="status"):
        run(api.material_journeys(status="green"))
    with pytest.raises(ValueError, match="case_id"):
        run(api.material_journeys(case_id="  "))


def test_journeys_keep_ledger_rows_whose_index_entry_is_gone():
    pipeline = FakeJourneyPipeline(
        material_ids=("mat-gone",),
        outcomes={"mat-gone": PipelineOutcome("mat-gone", "failed", FAILED_AT,
                                              stage="graph",
                                              error_type="RuntimeError",
                                              message="write failed")},
    )
    api = make_api(store=FakeStore(), pipeline=pipeline)

    views = run(api.material_journeys())

    (view,) = views
    assert view.material_id == "mat-gone"
    assert view.display_name is None
    assert view.lifecycle_status == "failed"


# ----------------------------------------------- add_material -> audit linkage


def test_add_material_annotates_the_run_audit_with_the_report_version():
    pipeline = FakeJourneyPipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED)},
        case_outcomes={"mat-1": SimpleNamespace(case_id="case-1")},
    )
    api = make_api(pipeline=pipeline)
    version = SimpleNamespace(version_id=f"rv-{uuid4().hex[:8]}")

    async def fake_save(case_id, as_of=None, *, use_llm=True,
                        debate_result=None, trigger="initial"):
        return version

    api.save_report_version = fake_save  # type: ignore[method-assign]

    result = run(api.add_material(
        str(Path("materials") / "note.md"), "case-1", None, use_llm=False
    ))

    assert result.report_version is version
    assert pipeline.notes == [("mat-1", version.version_id)]


def test_add_material_survives_pipelines_without_note_support():
    class NoNotePipeline(FakeJourneyPipeline):
        note_report_version = None  # older duck-typed pipeline: no seam

    pipeline = NoNotePipeline(
        outcomes={"mat-1": PipelineOutcome("mat-1", "committed", FINISHED)},
        case_outcomes={"mat-1": SimpleNamespace(case_id="case-1")},
    )
    api = make_api(pipeline=pipeline)
    version = SimpleNamespace(version_id="rv-2")

    async def fake_save(case_id, as_of=None, *, use_llm=True,
                        debate_result=None, trigger="initial"):
        return version

    api.save_report_version = fake_save  # type: ignore[method-assign]

    result = run(api.add_material(
        str(Path("materials") / "note.md"), "case-1", None, use_llm=False
    ))

    assert result.report_version is version


# ------------------------------------------- legacy pipeline compatibility (LOW)

# The workbench-era journey/audit capabilities must stay OPTIONAL: a
# pipeline implementing only the pre-workbench contract (run_material and
# nothing else) keeps constructing and serving through the facade — the
# typing (see _PipelineService in facade.py) must not force the new
# methods, and the runtime must degrade honestly instead of crashing.


class LegacyPipeline:
    """The pre-workbench pipeline surface: run_material and nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_material(self, result, *, correlation_id=None,
                           target_case=None):
        self.calls.append(result.material.id)
        return completed_run(result.material.id)


def test_a_legacy_pipeline_keeps_processing_through_the_facade():
    pipeline = LegacyPipeline()
    api = make_api(pipeline=pipeline)

    result = run(api.process_material("mat-1"))

    assert pipeline.calls == ["mat-1"]
    assert result.material_id == "mat-1"
    assert result.pipeline.status == "completed"


def test_a_legacy_pipeline_serves_journeys_as_purely_unknown():
    pipeline = LegacyPipeline()
    api = make_api(pipeline=pipeline)

    view = run(api.material_journey("mat-1"))

    # No fabricated state: without the journey query surface every
    # projection stays unknown instead of crashing or being invented.
    assert view.lifecycle_status == "unknown"
    assert view.mechanism_status == "unknown"
    assert view.run is None
    assert view.run_audit is None
    assert view.failure is None
    assert view.report_version_id is None


def test_a_legacy_pipeline_lists_no_journeys_instead_of_crashing():
    api = make_api(pipeline=LegacyPipeline())

    assert run(api.material_journeys()) == ()
