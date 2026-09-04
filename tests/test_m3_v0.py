"""M3 v0: case overview, report versions, and material-triggered recalculation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3

import pytest

from prism.analyzer import EvolutionAnalysis, TimelineStage
from prism.api import PrismAPI
from prism.api.facade import ProcessMaterialResult
from prism.cases import MaterialCaseConflict
from prism.cases.ledger import CaseExtractionLedger
from prism.cases.overview import CaseOverview, CaseOverviewService
from prism.cli import (
    build_parser,
    handle_add_material,
    handle_cases,
    handle_report,
    handle_rebuild_report,
    handle_report_version,
    handle_report_versions,
    main,
)
from prism.config import PathConfig, PrismConfig
from prism.debate import DebateResult
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
from prism.extraction import (
    ExtractionConflict,
    ExtractionEvidenceGap,
    ExtractionResult,
)
from prism.graph import GraphEpisode
from prism.ingestion import IngestionResult
from prism.pipeline import PipelineError
from prism.report import ReportDocument, ReportService
from prism.report.ledger import ReportVersion, ReportVersionLedger
from prism.runtime import create_runtime


UTC = timezone.utc
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
AS_OF_LATER = datetime(2026, 9, 2, tzinfo=UTC)
SECRET = "sk-test-secret-value"


def run(coro):
    return asyncio.run(coro)


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


def make_case(case_id: str = "case-b", **overrides) -> EvolutionCase:
    values = {
        "case_id": case_id,
        "case_type": "policy",
        "canonical_name": f"Case {case_id}",
        "start_at": datetime(2026, 1, 1, tzinfo=UTC),
        "status": "active",
    }
    values.update(overrides)
    return EvolutionCase(**values)


def make_material(material_id: str = "mat-1", **overrides) -> Material:
    values = {
        "id": material_id,
        "title": f"Material {material_id}",
        "source": "example.test",
        "published_at": PUBLISHED,
        "fetched_at": FETCHED,
        "type": "policy",
        "content": "The agency published the revised policy.",
        "case_tags": ("case-b",),
    }
    values.update(overrides)
    return Material(**values)


def make_extraction(
    material_id: str = "mat-1",
    case: EvolutionCase | None = None,
    *,
    gaps: tuple[ExtractionEvidenceGap, ...] = (),
    conflicts: tuple[ExtractionConflict, ...] = (),
) -> ExtractionResult:
    bound_case = case if case is not None else make_case()
    locator = EvidenceLocator(
        source_id=material_id,
        corpus_path=f"corpus/2026-08/example.test/{material_id}.md",
        paragraph=1,
        quote="The agency published the revised policy.",
    )
    node = EvolutionNode(
        id="node-1",
        case_id=bound_case.case_id,
        node_type="publication",
        happened_at=PUBLISHED,
        summary="The revised policy was published.",
        source_ids=(material_id,),
        valid_at=PUBLISHED,
        observed_at=PUBLISHED,
        evidence=(locator,),
    )
    fact = TemporalFact(
        subject="Agency",
        predicate="published",
        object="Revised policy",
        valid_at=PUBLISHED,
        invalid_at=None,
        observed_at=PUBLISHED,
        source_ids=(material_id,),
        confidence=0.9,
        provenance_type="explicit",
        evidence=(locator,),
    )
    claim = Claim(
        claim_id="claim-1",
        actor="Agency",
        proposition="The policy improves clarity.",
        stance="support",
        stated_at=PUBLISHED,
        based_on=(material_id,),
        evidence=(locator,),
        observed_at=PUBLISHED,
    )
    return ExtractionResult(
        case=bound_case,
        nodes=(node,),
        temporal_facts=(fact,),
        claims=(claim,),
        evidence_gaps=gaps,
        conflicts=conflicts,
    )


def make_analysis(
    case_id: str = "case-b",
    as_of: datetime = AS_OF,
    *,
    summary: str = "The policy was published.",
) -> EvolutionAnalysis:
    stage = TimelineStage(
        episode_key=f"case-{case_id}",
        kind="evolution_case",
        layer="fact",
        summary=summary,
        valid_at=PUBLISHED,
        invalid_at=None,
        reference_time=PUBLISHED,
        source_ids=("mat-1",),
    )
    return EvolutionAnalysis(
        case_id=case_id,
        as_of=as_of,
        case_type="policy",
        stages=(stage,),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
    )


def make_debate(case_id: str = "case-b") -> DebateResult:
    return DebateResult(
        case_id=case_id,
        question="What changed?",
        as_of=AS_OF,
        profiles=("observer",),
        results=(),
        synthesis=None,
        status="no_conclusion",
        fallback_reason="offline test",
        evidence_bundle_hash="debate-input-hash",
    )


def make_document(
    analysis: EvolutionAnalysis | None = None, debate: DebateResult | None = None
) -> ReportDocument:
    return run(ReportService().report(analysis or make_analysis(), debate_result=debate))


def ledger_with_cases(tmp_path: Path) -> CaseExtractionLedger:
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    gap = ExtractionEvidenceGap(
        "evidence_location_failed",
        "quote was not found verbatim",
        "node",
        "node-9",
        ("mat-b1",),
    )
    conflict = ExtractionConflict(
        conflict_id="conflict-b",
        subject="Agency",
        predicate="published",
        alternatives=("Revised policy", "Draft policy"),
        source_ids=("mat-b1",),
        evidence=(
            EvidenceLocator(
                source_id="mat-b1",
                corpus_path="corpus/2026-08/example.test/mat-b1.md",
                paragraph=1,
                quote="The agency published the revised policy.",
            ),
        ),
    )
    ledger.record(
        "case-b",
        make_material("mat-b1"),
        make_extraction("mat-b1", gaps=(gap,), conflicts=(conflict,)),
    )
    ledger.record(
        "case-b",
        make_material(
            "mat-b2",
            fetched_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            published_at=datetime(2026, 8, 31, 9, 0, tzinfo=UTC),
        ),
        make_extraction("mat-b2"),
    )
    ledger.record(
        "case-a",
        make_material("mat-a1"),
        make_extraction("mat-a1", case=make_case("case-a")),
    )
    return ledger


def test_case_overview_is_empty_filters_sorts_and_survives_restart(tmp_path):
    empty = CaseExtractionLedger(make_paths(tmp_path))
    try:
        assert CaseOverviewService(empty).list() == ()
    finally:
        empty.close()

    ledger = ledger_with_cases(tmp_path)
    service = CaseOverviewService(ledger)
    try:
        overviews = service.list()
        assert [item.case_id for item in overviews] == ["case-a", "case-b"]
        case_b = overviews[1]
        assert isinstance(case_b, CaseOverview)
        assert case_b.case_type == "policy"
        assert case_b.name == "Case case-b"
        assert case_b.status == "active"
        assert case_b.material_count == 2
        assert case_b.earliest_observed_at == PUBLISHED
        assert case_b.latest_observed_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        assert case_b.latest_node_at == PUBLISHED
        assert case_b.last_updated_at == ledger.case_updated_at("case-b")
        assert case_b.last_updated_at >= case_b.latest_observed_at
        assert case_b.has_unresolved_gaps is True
        assert case_b.has_unresolved_conflicts is True
        case_a = service.list(case_id="case-a")[0]
        assert case_a.has_unresolved_gaps is False
        assert case_a.has_unresolved_conflicts is False

        assert [
            item.case_id
            for item in service.list(order="last_updated", reverse=True)
        ] == ["case-a", "case-b"]
        assert [item.case_id for item in service.list(case_type="policy")] == [
            "case-a",
            "case-b",
        ]
        assert service.list(status="paused") == ()
        assert [item.case_id for item in service.list(unresolved_only=True)] == ["case-b"]
    finally:
        ledger.close()

    restarted = CaseExtractionLedger(make_paths(tmp_path))
    try:
        assert [
            item.case_id for item in CaseOverviewService(restarted).list()
        ] == ["case-a", "case-b"]
    finally:
        restarted.close()


def test_report_version_ledger_is_immutable_idempotent_and_restart_durable(tmp_path):
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    analysis = make_analysis()
    debate = make_debate()
    document = make_document(analysis, debate)
    try:
        initial = ledger.save(
            document, analysis, trigger="initial", debate_result=debate
        )
        assert isinstance(initial, ReportVersion)
        assert initial.version_id.startswith("rv_")
        assert initial.case_id == "case-b"
        assert initial.as_of == AS_OF
        assert initial.trigger == "initial"
        assert initial.parent_version_id is None
        assert initial.summary_origin == "fallback"
        assert initial.debate_input_hash is not None
        assert initial.markdown == document.markdown
        assert initial.input_hash != initial.markdown_hash
        assert len(initial.input_hash) == 64
        assert len(initial.markdown_hash) == 64

        repeated = ledger.save(
            document, analysis, trigger="rebuild", debate_result=debate
        )
        assert repeated == initial
        assert ledger.versions() == (initial,)

        changed_analysis = make_analysis(
            as_of=AS_OF_LATER,
            summary="A second material changed the evidence.",
        )
        changed_document = make_document(changed_analysis)
        material_added = ledger.save(
            changed_document, changed_analysis, trigger="material_added"
        )
        assert material_added.parent_version_id == initial.version_id
        assert material_added.trigger == "material_added"
        assert material_added.version_id != initial.version_id
        assert [version.trigger for version in ledger.versions()] == [
            "initial",
            "material_added",
        ]
        assert ledger.versions(case_id="case-b", as_of=AS_OF) == (initial,)
        assert ledger.get(initial.version_id) == initial
        assert ledger.latest("case-b") == material_added
    finally:
        ledger.close()

    reopened = ReportVersionLedger(paths)
    try:
        versions = reopened.versions()
        assert [version.trigger for version in versions] == [
            "initial",
            "material_added",
        ]
        assert reopened.get(versions[0].version_id) == versions[0]
    finally:
        reopened.close()

    connection = sqlite3.connect(paths.data_dir / "index.db")
    try:
        rows = connection.execute("SELECT * FROM report_versions").fetchall()
        rendered = json.dumps([tuple(row) for row in rows], default=str)
    finally:
        connection.close()
    assert SECRET not in rendered
    assert str(tmp_path) not in rendered
    assert "D:/" not in rendered and "E:/" not in rendered
    assert initial.input_hash in rendered
    assert initial.markdown_hash in rendered


def test_report_version_input_hash_is_instant_stable_across_offsets(tmp_path):
    """One cutoff instant must hash and filter identically in any offset."""
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        utc_analysis = make_analysis()  # as_of == AS_OF (UTC)
        offset_instant = datetime(2026, 9, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        assert offset_instant == AS_OF
        offset_analysis = make_analysis(as_of=offset_instant)

        assert ledger.input_hash(utc_analysis) == ledger.input_hash(offset_analysis)
        assert ledger.input_hash(utc_analysis, make_debate()) == ledger.input_hash(
            offset_analysis, make_debate()
        )

        initial = ledger.save(
            make_document(utc_analysis), utc_analysis, trigger="initial"
        )
        # Identical instant rendered through a differently-offset analysis is the
        # same version, never a second row.
        repeated = ledger.save(
            make_document(offset_analysis), offset_analysis, trigger="rebuild"
        )
        assert repeated == initial
        assert ledger.versions() == (initial,)

        # History filters by the instant, whatever offset the query uses.
        assert ledger.versions(case_id="case-b", as_of=offset_instant) == (initial,)
        assert ledger.versions(case_id="case-b", as_of=AS_OF) == (initial,)
        assert ledger.get(initial.version_id).as_of == AS_OF
    finally:
        ledger.close()

    reopened = ReportVersionLedger(paths)
    try:
        assert reopened.versions(case_id="case-b", as_of=AS_OF)[0] == initial
    finally:
        reopened.close()


def test_report_version_creation_order_is_stable_when_clock_repeats(tmp_path):
    """Equal ``created_at`` values must never reorder or mis-parent versions."""
    fixed = datetime(2026, 9, 3, tzinfo=UTC)
    ledger = ReportVersionLedger(make_paths(tmp_path), clock=lambda: fixed)
    first_analysis = make_analysis(summary="first material")
    second_analysis = make_analysis(
        as_of=AS_OF_LATER, summary="second material changed the evidence."
    )
    try:
        first = ledger.save(
            make_document(first_analysis), first_analysis, trigger="initial"
        )
        second = ledger.save(
            make_document(second_analysis),
            second_analysis,
            trigger="material_added",
        )
        assert second.parent_version_id == first.version_id
        assert ledger.latest("case-b") == second
        assert [item.version_id for item in ledger.versions("case-b")] == [
            first.version_id,
            second.version_id,
        ]
    finally:
        ledger.close()


def test_report_version_save_rejects_a_debate_the_document_does_not_carry(tmp_path):
    """A version must never record a debate hash for a document that lacks it."""
    ledger = ReportVersionLedger(make_paths(tmp_path))
    analysis = make_analysis()
    debate = make_debate()
    document = make_document(analysis, debate)
    try:
        with pytest.raises(ValueError):
            ledger.save(document, analysis, debate_result=make_debate())
        with pytest.raises(ValueError):
            ledger.save(make_document(analysis), analysis, debate_result=debate)
        assert ledger.versions() == ()
    finally:
        ledger.close()


def test_api_add_material_unchanged_input_adds_no_duplicate_version(tmp_path):
    """A pipeline success that changes no report input returns the existing
    version — the ledger never stores a second row for identical input."""
    analysis = make_analysis()
    document = make_document(analysis)
    pipeline = FakePipeline(pipeline_result())
    analyzer = FakeAnalyzer([analysis, analysis])
    reports = FakeReportService([document])
    api = make_api(tmp_path, analyzer, reports, pipeline=pipeline)

    initial = run(api.save_report_version("case-b", AS_OF, trigger="initial"))
    added = run(api.add_material("input.md", "case-b", as_of=AS_OF))

    assert added.report_version == initial
    assert initial.trigger == "initial"
    assert api._report_version_service.versions() == (initial,)
    # The idempotent add-materal save must not re-render the report.
    assert reports.calls == [(analysis, None)]
    assert len(analyzer.calls) == 2


def test_report_version_save_failure_writes_no_partial_row(tmp_path):
    """A failed render never leaves a report_versions row behind."""
    ledger = ReportVersionLedger(make_paths(tmp_path))
    analysis = make_analysis()
    try:
        with pytest.raises(ValueError):
            # as_of mismatch between document and analysis is rejected before
            # any SQL is executed.
            ledger.save(make_document(make_analysis(as_of=AS_OF_LATER)), analysis)
        assert ledger.versions() == ()
        connection = sqlite3.connect(make_paths(tmp_path).data_dir / "index.db")
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM report_versions"
            ).fetchone()[0]
        finally:
            connection.close()
        assert count == 0
    finally:
        ledger.close()


def test_report_version_preserves_debate_as_interpretation_not_fact(tmp_path):
    ledger = ReportVersionLedger(make_paths(tmp_path))
    analysis = make_analysis()
    debate = make_debate()
    document = make_document(analysis, debate)
    try:
        version = ledger.save(document, analysis, debate_result=debate)
        assert "## Debate Interpretation" in version.markdown
        assert "offline test" in version.markdown
        structured = version.markdown.split("## Timeline Stages", 1)[1]
        assert "What changed?" not in structured
        assert "The policy was published." in structured
    finally:
        ledger.close()


class StubIngestion:
    def ingest(self, path, metadata=None):
        return IngestionResult(
            material=make_material("mat-new"),
            raw_path=Path("raw/input.md"),
            corpus_path=Path("corpus/input.md"),
            used_ocr=False,
            extracted_via="test",
        )


class StubStore:
    def index_file(self, path):
        return object()

    def search(self, criteria, *, limit=50, offset=0):
        return []

    def get(self, source_id):
        return object()


class StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("case overview must not depend on the graph")

    async def add_case(self, case, **bundle):
        raise AssertionError("case overview must not depend on the graph")


class StubBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class FakeAnalyzer:
    def __init__(self, analyses):
        self.analyses = list(analyses)
        self.calls = []

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append((case_id, as_of))
        return self.analyses.pop(0)


class FakeReportService:
    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = []

    async def report(self, analysis, debate_result=None):
        self.calls.append((analysis, debate_result))
        return self.documents.pop(0)


class FakeOverview:
    def list(self, **filters):
        return (
            CaseOverview(
                case_id="case-b",
                case_type="policy",
                name="Case",
                status="active",
                material_count=2,
            ),
        )


class FakeCaseService:
    async def merge_case(self, case_id):
        return None
    async def record_extraction(self, material, extraction):
        return None
    def load_case(self, case_id):
        return make_case(case_id)

    def case_for_material(self, material_id):
        return "case-b"


class FakePipeline:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def run_material(self, result, *, target_case=None):
        self.calls.append((result, target_case))
        if self.error is not None:
            raise self.error
        if isinstance(self.result, ProcessMaterialResult):
            return self.result.pipeline
        return self.result

    def case_outcome_for(self, material_id):
        return type("Outcome", (), {"case_id": "case-b", "warnings": ()})()

    def run_for(self, material_id):
        return self.result.pipeline


def make_api(tmp_path: Path, analyzer, reports, pipeline=None) -> PrismAPI:
    return PrismAPI(
        StubIngestion(),
        StubStore(),
        StubGraph(),
        StubBus(),
        analyzer_service=analyzer,
        report_service=reports,
        pipeline_service=pipeline,
        case_service=FakeCaseService(),
        case_overview_service=FakeOverview(),
        report_version_service=ReportVersionLedger(make_paths(tmp_path)),
    )


def test_api_report_save_is_idempotent_before_the_report_llm_call(tmp_path):
    analysis = make_analysis()
    document = make_document(analysis)
    analyzer = FakeAnalyzer([analysis, analysis])
    reports = FakeReportService([document])
    api = make_api(tmp_path, analyzer, reports)

    first = run(api.save_report_version("case-b", AS_OF, trigger="initial"))
    second = run(api.save_report_version("case-b", AS_OF, trigger="initial"))

    assert first == second
    assert reports.calls == [(analysis, None)]
    assert analyzer.calls == [("case-b", AS_OF), ("case-b", AS_OF)]
    assert api._report_version_service.versions() == (first,)
    assert run(api.report_versions("case-b", as_of=AS_OF)) == (first,)


def pipeline_result() -> ProcessMaterialResult:
    @dataclass
    class Run:
        status: str = "completed"
        stages: tuple = ()
    return ProcessMaterialResult(
        material_id="mat-new",
        pipeline=Run(),
        case_id="case-b",
        case_outcome=None,
    )


def test_api_add_material_recalculates_report_only_after_pipeline_success(tmp_path):
    analysis = make_analysis()
    document = make_document(analysis)
    pipeline = FakePipeline(pipeline_result())
    analyzer = FakeAnalyzer([analysis])
    reports = FakeReportService([document])
    api = make_api(tmp_path, analyzer, reports, pipeline=pipeline)

    result = run(api.add_material("input.md", "case-b", as_of=AS_OF))

    assert result.material_id == "mat-new"
    assert pipeline.calls[0][1].case_id == "case-b"
    assert reports.calls == [(analysis, None)]
    version = api._report_version_service.latest("case-b")
    assert version is not None and version.trigger == "material_added"


def test_api_add_material_extraction_failure_creates_no_report_version(tmp_path):
    pipeline = FakePipeline(
        error=PipelineError(
            "extract failed",
            stage="extract",
            material_id="mat-new",
        )
    )
    analyzer = FakeAnalyzer([make_analysis()])
    reports = FakeReportService([make_document(make_analysis())])
    api = make_api(tmp_path, analyzer, reports, pipeline=pipeline)

    with pytest.raises(PipelineError):
        run(api.add_material("input.md", "case-b", as_of=AS_OF))

    assert reports.calls == []
    assert api._report_version_service.versions() == ()


def test_api_add_material_cross_case_conflict_creates_no_report_version(tmp_path):
    conflict = MaterialCaseConflict(
        "mat-new", ("case-other",), attempted_case="case-b"
    )
    error = PipelineError("graph failed", stage="graph", material_id="mat-new")
    error.__cause__ = conflict
    pipeline = FakePipeline(error=error)
    analyzer = FakeAnalyzer([make_analysis()])
    reports = FakeReportService([make_document(make_analysis())])
    api = make_api(tmp_path, analyzer, reports, pipeline=pipeline)

    with pytest.raises(MaterialCaseConflict):
        run(api.add_material("input.md", "case-b", as_of=AS_OF))

    assert reports.calls == []
    assert api._report_version_service.versions() == ()


class RecordingCLIAPI:
    def __init__(self):
        self.calls = []

    async def case_overviews(self, **filters):
        self.calls.append(("case_overviews", (), filters))
        return []

    async def report_case(self, case_id, as_of=None, use_llm=True):
        self.calls.append(("report_case", (case_id, as_of, use_llm), {}))
        return make_document(make_analysis(case_id))

    async def save_report_version(
        self,
        case_id,
        as_of=None,
        use_llm=True,
        debate_result=None,
        trigger="initial",
    ):
        self.calls.append(
            ("save_report_version", (case_id, as_of, use_llm, debate_result, trigger), {})
        )
        return ReportVersion(
            version_id="rv_test",
            case_id=case_id,
            as_of=as_of or AS_OF,
            created_at=AS_OF,
            input_hash="a" * 64,
            markdown_hash="b" * 64,
            summary_origin="fallback",
            debate_input_hash=None,
            markdown="# report",
            parent_version_id=None,
            trigger=trigger,
        )

    async def report_versions(self, case_id=None, *, as_of=None):
        self.calls.append(("report_versions", (case_id,), {"as_of": as_of}))
        return ()

    async def report_version(self, version_id):
        self.calls.append(("report_version", (version_id,), {}))
        return None

    async def add_material(
        self, source, target_case, metadata=None, as_of=None, use_llm=True
    ):
        self.calls.append(
            ("add_material", (source, target_case, metadata, as_of, use_llm), {})
        )
        return pipeline_result()

    async def rebuild_report(self, case_id, as_of=None, use_llm=True):
        self.calls.append(("rebuild_report", (case_id, as_of, use_llm), {}))
        return None


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


def test_cli_exposes_m3_v0_commands_and_delegates_to_the_same_api():
    api = RecordingCLIAPI()

    cases = build_parser().parse_args(
        [
            "cases",
            "--type",
            "policy",
            "--status",
            "active",
            "--unresolved-only",
            "--order",
            "last_updated",
            "--reverse",
        ]
    )
    assert cases.handler is handle_cases
    status, stdout, stderr = run_cli(
        [
            "cases",
            "--type",
            "policy",
            "--status",
            "active",
            "--unresolved-only",
            "--order",
            "last_updated",
            "--reverse",
        ],
        api,
    )
    assert status == 0 and stderr == ""
    assert api.calls[-1] == (
        "case_overviews",
        (),
        {
            "case_id": None,
            "case_type": "policy",
            "status": "active",
            "unresolved_only": True,
            "order": "last_updated",
            "reverse": True,
        },
    )
    assert json.loads(stdout) == []

    report = build_parser().parse_args(
        ["report", "case-b", "--save", "--as-of", "2026-09-01T00:00:00+00:00", "--no-llm"]
    )
    assert report.handler is handle_report
    assert report.save is True

    status, _, _ = run_cli(
        ["report", "case-b", "--save", "--as-of", "2026-09-01T00:00:00+00:00", "--no-llm"],
        api,
    )
    assert status == 0
    assert api.calls[-1] == (
        "save_report_version",
        ("case-b", datetime(2026, 9, 1, tzinfo=UTC), False, None, "initial"),
        {},
    )

    status, _, _ = run_cli(["report-versions", "case-b"], api)
    assert status == 0 and api.calls[-1] == (
        "report_versions",
        ("case-b",),
        {"as_of": None},
    )
    versions = build_parser().parse_args(
        ["report-versions", "case-b", "--as-of", "2026-09-01T00:00:00+00:00"]
    )
    assert versions.handler is handle_report_versions
    status, _, _ = run_cli(
        ["report-versions", "case-b", "--as-of", "2026-09-01T00:00:00+00:00"],
        api,
    )
    assert status == 0
    assert api.calls[-1] == (
        "report_versions",
        ("case-b",),
        {"as_of": datetime(2026, 9, 1, tzinfo=UTC)},
    )

    status, _, _ = run_cli(["report-version", "rv_test"], api)
    assert status == 0 and api.calls[-1] == ("report_version", ("rv_test",), {})

    add = build_parser().parse_args(
        [
            "add-material",
            "input.md",
            "--case-id",
            "case-b",
            "--as-of",
            "2026-09-01T00:00:00+00:00",
            "--no-llm",
        ]
    )
    assert add.handler is handle_add_material
    status, _, _ = run_cli(
        [
            "add-material",
            "input.md",
            "--case-id",
            "case-b",
            "--as-of",
            "2026-09-01T00:00:00+00:00",
            "--no-llm",
        ],
        api,
    )
    assert status == 0
    assert api.calls[-1] == (
        "add_material",
        ("input.md", "case-b", None, datetime(2026, 9, 1, tzinfo=UTC), False),
        {},
    )

    rebuild = build_parser().parse_args(
        ["rebuild-report", "case-b", "--as-of", "2026-09-01T00:00:00+00:00", "--no-llm"]
    )
    assert rebuild.handler is handle_rebuild_report
    status, _, _ = run_cli(
        ["rebuild-report", "case-b", "--as-of", "2026-09-01T00:00:00+00:00", "--no-llm"],
        api,
    )
    assert status == 0
    assert api.calls[-1] == (
        "rebuild_report",
        ("case-b", datetime(2026, 9, 1, tzinfo=UTC), False),
        {},
    )


class FakeGraphBackend:
    def __init__(self):
        self.episodes = {}

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode] = True
        return True

    async def search(self, query: str):
        return tuple(self.episodes)


class TargetBoundExtractor:
    name = "target-bound"

    def __init__(self, fail_next=False):
        self.fail_next = fail_next
        self.calls = []

    async def extract(self, material):
        return await self.extract_material(material)

    async def extract_material(self, material, *, corpus_path=None, target_case=None):
        self.calls.append((material.id, target_case))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("extractor exploded")
        if target_case is None:
            return ExtractionResult()
        locator = EvidenceLocator(
            source_id=material.id,
            corpus_path=f"corpus/2026-08/example.test/{material.id}.md",
            paragraph=1,
            quote=material.content,
        )
        node = EvolutionNode(
            id="node-1",
            case_id=target_case.case_id,
            node_type="publication",
            happened_at=material.published_at,
            summary=material.title,
            source_ids=(material.id,),
            valid_at=material.published_at,
            observed_at=material.published_at,
            evidence=(locator,),
        )
        fact = TemporalFact(
            subject="Agency",
            predicate="published",
            object=material.title,
            valid_at=material.published_at,
            invalid_at=None,
            observed_at=material.published_at,
            source_ids=(material.id,),
            confidence=0.9,
            provenance_type="explicit",
            evidence=(locator,),
        )
        return ExtractionResult(
            case=target_case,
            nodes=(node,),
            temporal_facts=(fact,),
        )


DOC_TEMPLATE = """---
source: example.test
title: {title}
published_at: 2026-08-30T09:00:00+00:00
fetched_at: 2026-08-31T12:00:00+00:00
type: policy
case_tags: ["case-b"]
access_level: fulltext
---

{body}
"""


def write_material(home: Path, name: str, body: str) -> Path:
    directory = home / "corpus" / "2026-08" / "example.test"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.md"
    target.write_text(DOC_TEMPLATE.format(title=name, body=body), encoding="utf-8")
    return target


def test_runtime_add_material_creates_version_and_survives_restart(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("PRISM_HOME", str(tmp_path))
        config_path = tmp_path / "config.json"
        PrismConfig().save(config_path)
        extractor = TargetBoundExtractor()
        runtime = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=extractor,
        )
        try:
            first_doc = write_material(
                tmp_path, "policy-update", "The agency published the revised policy."
            )
            first = await runtime.api.process_material(
                first_doc, target_case=make_case("case-b")
            )
            assert first.case_id == "case-b"
            initial = await runtime.api.save_report_version(
                "case-b", AS_OF, use_llm=False, trigger="initial"
            )
            assert initial.trigger == "initial"

            second_doc = write_material(
                tmp_path, "policy-reaction", "Analysts responded to the revised policy."
            )
            added = await runtime.api.add_material(
                second_doc, "case-b", as_of=AS_OF, use_llm=False
            )
            assert added.case_id == "case-b"
            assert [
                entry.material_id
                for entry in runtime.case_ledger.entries("case-b")
            ] == [first.material_id, added.material_id]
            versions = runtime.report_version_ledger.versions("case-b")
            assert [version.trigger for version in versions] == [
                "initial",
                "material_added",
            ]
            assert versions[1].parent_version_id == versions[0].version_id
            assert f"As of: {AS_OF.isoformat()}" in versions[1].markdown

            extractor.fail_next = True
            third_doc = write_material(
                tmp_path, "failed-material", "This extraction will fail."
            )
            with pytest.raises(PipelineError):
                await runtime.api.add_material(
                    third_doc, "case-b", as_of=AS_OF, use_llm=False
                )
            assert len(runtime.report_version_ledger.versions("case-b")) == 2
        finally:
            await runtime.close()

        restarted = await create_runtime(
            config_path,
            graph_backend=FakeGraphBackend(),
            extraction_service=TargetBoundExtractor(),
        )
        try:
            overviews = await restarted.api.case_overviews()
            assert [item.case_id for item in overviews] == ["case-b"]
            assert [item.material_count for item in overviews] == [2]
            assert [
                version.trigger
                for version in restarted.report_version_ledger.versions("case-b")
            ] == ["initial", "material_added"]
        finally:
            await restarted.close()

    run(scenario())
