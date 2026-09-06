"""Focused tests for the durable per-material run audit (pipeline.outcomes).

Phase A of the WebUI workbench (WB-2): stage-level audits must survive a
restart so the material journey stays a read-only projection of recorded
audit data — never a new fact store and never new temporal semantics (H-6).
These tests pin the SQLite round-trip of :class:`PipelineRunAudit`, the
``PipelineService`` recording/hydration seams, the report-version annotation
written by the append flow, and compatibility with older outcome stores
that know nothing about run audits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import shutil
import tempfile

import pytest

from prism.config import PathConfig
from prism.domain import Material
from prism.extraction import ExtractionResult
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.pipeline.outcomes import (
    PipelineOutcome,
    PipelineOutcomeLedger,
    PipelineRunAudit,
    StageAuditRecord,
)
from prism.pipeline.service import PipelineError, PipelineService
from prism.store import IndexEntry, IndexOutcome


UTC = timezone.utc
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 8, 0, 5, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sandbox():
    # This sandboxed environment denies access to pytest's tmp_path root, so
    # the SQLite-backed tests use their own tempfile directory instead.
    directory = Path(tempfile.mkdtemp(prefix="prism-run-audits-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def make_paths(sandbox: Path) -> PathConfig:
    return PathConfig(data_dir=sandbox / "data").resolve(sandbox)


def completed_audit(material_id: str = "mat-1") -> PipelineRunAudit:
    return PipelineRunAudit(
        material_id=material_id,
        status="completed",
        stages=(
            StageAuditRecord("index", "indexed"),
            StageAuditRecord("extract", "extracted"),
            StageAuditRecord(
                "graph", "skipped", detail="extraction produced no case"
            ),
        ),
        started_at=T0,
        finished_at=T1,
        correlation_id="corr-1",
        report_version_id="rv-1",
    )


def failed_audit(material_id: str = "mat-2") -> PipelineRunAudit:
    return PipelineRunAudit(
        material_id=material_id,
        status="failed",
        stages=(StageAuditRecord("index", "indexed"),),
        finished_at=T1,
    )


# ------------------------------------------------------------- dataclass gates


def test_audit_records_reject_unknown_stage_names_and_naive_times():
    with pytest.raises(ValueError, match="name"):
        StageAuditRecord("ingest", "completed")
    with pytest.raises(ValueError, match="status"):
        StageAuditRecord("index", "")
    with pytest.raises(ValueError, match="detail"):
        StageAuditRecord("index", "indexed", detail="  ")
    with pytest.raises(ValueError, match="status"):
        PipelineRunAudit("mat-1", "pending")
    with pytest.raises(ValueError, match="timezone-aware"):
        PipelineRunAudit("mat-1", "completed", finished_at=T0.replace(tzinfo=None))


# ------------------------------------------------------------ ledger round-trip


def test_ledger_roundtrips_run_audits(sandbox):
    ledger = PipelineOutcomeLedger(make_paths(sandbox))
    try:
        assert ledger.run_audit_entries() == ()
        ledger.record_run_audit(completed_audit())
        ledger.record_run_audit(failed_audit())

        entries = ledger.run_audit_entries()
        assert [audit.material_id for audit in entries] == ["mat-1", "mat-2"]
        assert entries[0] == completed_audit()
        assert entries[0].stages[2].detail == "extraction produced no case"
        assert entries[0].report_version_id == "rv-1"
        assert entries[0].started_at == T0
        assert entries[0].correlation_id == "corr-1"
        assert entries[1] == failed_audit()
    finally:
        ledger.close()


def test_ledger_upserts_one_current_audit_per_material(sandbox):
    ledger = PipelineOutcomeLedger(make_paths(sandbox))
    try:
        ledger.record_run_audit(failed_audit("mat-1"))
        ledger.record_run_audit(completed_audit("mat-1"))

        entries = ledger.run_audit_entries()
        assert len(entries) == 1
        assert entries[0].status == "completed"
        assert [stage.name for stage in entries[0].stages] == [
            "index", "extract", "graph",
        ]
    finally:
        ledger.close()


def test_ledger_refuses_non_audit_records(sandbox):
    ledger = PipelineOutcomeLedger(make_paths(sandbox))
    try:
        with pytest.raises(TypeError):
            ledger.record_run_audit(
                PipelineOutcome("mat-1", "committed", T1)
            )
    finally:
        ledger.close()


# ------------------------------------------------- service recording / querying


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
    )


class FakeIndexer:
    def index_file(self, path):
        return IndexOutcome(make_entry(), "indexed")


class CaselessExtractor:
    async def extract(self, material):
        return ExtractionResult(case=None)

    async def extract_material(self, material, *, corpus_path=None,
                               target_case=None):
        return ExtractionResult(case=None)


class FailingExtractor:
    async def extract(self, material):
        raise RuntimeError("extractor exploded")

    async def extract_material(self, material, *, corpus_path=None,
                               target_case=None):
        raise RuntimeError("extractor exploded")


class FakeGraph:
    async def add_case(self, case, **kwargs):
        return GraphWriteResult((), ("episode-1",), ())


class MemoryOutcomeStore:
    """In-memory outcome store that also persists run audits."""

    def __init__(self) -> None:
        self.outcomes: dict[str, PipelineOutcome] = {}
        self.audits: dict[str, PipelineRunAudit] = {}

    def record(self, outcome: PipelineOutcome) -> None:
        self.outcomes[outcome.material_id] = outcome

    def entries(self):
        return tuple(self.outcomes.values())

    def record_run_audit(self, audit: PipelineRunAudit) -> None:
        self.audits[audit.material_id] = audit

    def run_audit_entries(self):
        return tuple(self.audits.values())


class LegacyOutcomeStore:
    """Older store contract: outcomes only, no run-audit methods."""

    def __init__(self) -> None:
        self.recorded: list[PipelineOutcome] = []

    def record(self, outcome: PipelineOutcome) -> None:
        self.recorded.append(outcome)

    def entries(self):
        return ()


def make_service(store, extractor=None) -> PipelineService:
    return PipelineService(
        indexer=FakeIndexer(),
        extraction_service=extractor if extractor is not None
        else CaselessExtractor(),
        graph_service=FakeGraph(),
        outcome_store=store,
    )


def test_committed_run_records_a_rebuildable_stage_audit():
    store = MemoryOutcomeStore()
    service = make_service(store)

    completed = run(service.run_material(make_result()))

    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "completed"
    assert [stage.name for stage in audit.stages] == [
        "index", "extract", "graph",
    ]
    assert audit.stages[0].status == "indexed"
    assert audit.stages[2].status == "skipped"
    assert audit.started_at == completed.started_at
    assert audit.finished_at == completed.finished_at
    assert store.audits["mat-1"] == audit


def test_failed_run_records_the_stages_that_already_completed():
    store = MemoryOutcomeStore()
    service = make_service(store, extractor=FailingExtractor())

    with pytest.raises(PipelineError):
        run(service.run_material(make_result()))

    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "failed"
    assert [stage.name for stage in audit.stages] == ["index"]
    outcome = store.outcomes["mat-1"]
    assert outcome.status == "failed"
    assert outcome.stage == "extract"
    assert outcome.error_type == "RuntimeError"


def test_retry_success_replaces_the_failed_audit():
    class FlakyExtractor:
        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, material):
            return ExtractionResult(case=None)

        async def extract_material(self, material, *, corpus_path=None,
                                   target_case=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("extractor exploded")
            return ExtractionResult(case=None)

    store = MemoryOutcomeStore()
    extractor = FlakyExtractor()
    service = make_service(store, extractor=extractor)

    with pytest.raises(PipelineError):
        run(service.run_material(make_result()))
    run(service.run_material(make_result()))

    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "completed"
    assert audit.stages[-1].name == "graph"
    assert store.outcomes["mat-1"].status == "committed"


def test_audits_hydrate_from_the_store_after_a_restart():
    store = MemoryOutcomeStore()
    first = make_service(store)
    run(first.run_material(make_result()))

    second = make_service(store)

    assert second.audit_for("mat-1") == first.audit_for("mat-1")


def test_a_service_without_a_store_records_no_durable_audit():
    service = make_service(None)

    run(service.run_material(make_result()))

    assert service.audit_for("mat-1") is not None


def test_legacy_outcome_stores_keep_working_without_audit_methods():
    store = LegacyOutcomeStore()
    service = make_service(store)

    completed = run(service.run_material(make_result()))

    assert completed.status == "completed"
    # The in-memory audit still serves this process's queries; the legacy
    # durable store simply never receives audit rows (duck-typed optional).
    assert service.audit_for("mat-1") is not None
    assert store.recorded  # outcomes keep flowing to the old contract


def test_note_report_version_annotates_the_current_audit():
    store = MemoryOutcomeStore()
    service = make_service(store)
    run(service.run_material(make_result()))

    service.note_report_version("mat-1", "rv-9")

    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.report_version_id == "rv-9"
    assert store.audits["mat-1"].report_version_id == "rv-9"


def test_note_report_version_validates_its_arguments():
    service = make_service(MemoryOutcomeStore())

    with pytest.raises(ValueError):
        service.note_report_version("  ", "rv-9")
    with pytest.raises(ValueError):
        service.note_report_version("mat-1", "")


def test_note_report_version_refuses_uncommitted_materials():
    store = MemoryOutcomeStore()
    service = make_service(store, extractor=FailingExtractor())

    with pytest.raises(PipelineError):
        run(service.run_material(make_result()))

    with pytest.raises(ValueError, match="committed"):
        service.note_report_version("mat-1", "rv-9")


def test_sqlite_ledger_persists_audits_across_service_instances(sandbox):
    paths = make_paths(sandbox)
    first = PipelineService(
        indexer=FakeIndexer(),
        extraction_service=CaselessExtractor(),
        graph_service=FakeGraph(),
        outcome_store=PipelineOutcomeLedger(paths),
    )
    run(first.run_material(make_result()))
    first.note_report_version("mat-1", "rv-1")

    second = PipelineService(
        indexer=FakeIndexer(),
        extraction_service=CaselessExtractor(),
        graph_service=FakeGraph(),
        outcome_store=PipelineOutcomeLedger(paths),
    )

    audit = second.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "completed"
    assert audit.report_version_id == "rv-1"
    assert [stage.name for stage in audit.stages] == [
        "index", "extract", "graph",
    ]


# ------------------------------------------ atomic terminal persistence (review)

# HIGH: a terminal outcome and its run audit must be persisted atomically
# (or at minimum audit-first and recoverably): a durable committed outcome
# without its audit is unreachable for remediation, and a persistence
# failure must fail conservatively while a retry — including one in the
# SAME process — can still remediate.


class OrderRecordingStore(MemoryOutcomeStore):
    """Legacy two-step store recording the order of its durable writes."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[tuple[str, str, str]] = []

    def record(self, outcome: PipelineOutcome) -> None:
        self.order.append(("outcome", outcome.material_id, outcome.status))
        super().record(outcome)

    def record_run_audit(self, audit: PipelineRunAudit) -> None:
        self.order.append(("audit", audit.material_id, audit.status))
        super().record_run_audit(audit)


class FlakyAuditStore(OrderRecordingStore):
    """Legacy two-step store whose audit write fails the first N times."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self._remaining = failures

    def record_run_audit(self, audit: PipelineRunAudit) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("simulated ledger failure")
        super().record_run_audit(audit)


class AtomicOutcomeStore(MemoryOutcomeStore):
    """Modern store contract: one atomic pair-write for terminal states."""

    def __init__(self, failures: int = 0) -> None:
        super().__init__()
        self._remaining = failures
        self.terminal: list[tuple[PipelineOutcome, PipelineRunAudit]] = []

    def record_terminal(
        self, outcome: PipelineOutcome, audit: PipelineRunAudit
    ) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("simulated ledger failure")
        self.terminal.append((outcome, audit))
        self.outcomes[outcome.material_id] = outcome
        self.audits[audit.material_id] = audit


class BrokenLedgerStore:
    """A store whose durable writes always fail."""

    def record(self, outcome: PipelineOutcome) -> None:
        raise RuntimeError("ledger broken")

    def entries(self):
        return ()


def test_the_audit_is_persisted_before_the_terminal_outcome():
    # Legacy two-step fallback ordering: the audit lands first, so a crash
    # between the writes can never leave a committed outcome whose audit
    # is missing (the forbidden state).
    store = OrderRecordingStore()
    service = make_service(store)

    run(service.run_material(make_result()))

    assert store.order == [
        ("audit", "mat-1", "completed"),
        ("outcome", "mat-1", "committed"),
    ]


def test_terminal_persistence_failure_fails_conservatively():
    store = FlakyAuditStore(failures=1)
    service = make_service(store)

    with pytest.raises(PipelineError, match="persistence"):
        run(service.run_material(make_result()))

    # Nothing durable claims committed, and the in-process view reports the
    # conservative failed state instead of a success or a pending hang.
    assert store.outcomes["mat-1"].status == "failed"
    assert service.outcome_for("mat-1").status == "failed"
    # The completed-run registration is rolled back, so the same-process
    # duplicate skip can never block remediation.
    assert service.run_for("mat-1") is None
    # The compensating failure audit keeps the stage records that did
    # complete, plus the ledger failure as the error.
    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "failed"
    assert [stage.name for stage in audit.stages] == [
        "index", "extract", "graph",
    ]
    failure = service.failure_for("mat-1")
    assert failure is not None
    assert failure.error_type == "RuntimeError"


def test_same_process_retry_after_a_persistence_failure_remediates():
    class CountingExtractor(CaselessExtractor):
        def __init__(self) -> None:
            self.calls = 0

        async def extract_material(self, material, *, corpus_path=None,
                                   target_case=None):
            self.calls += 1
            return await super().extract_material(
                material, corpus_path=corpus_path, target_case=target_case
            )

    store = FlakyAuditStore(failures=1)
    extractor = CountingExtractor()
    service = make_service(store, extractor=extractor)

    with pytest.raises(PipelineError):
        run(service.run_material(make_result()))
    completed = run(service.run_material(make_result()))

    assert completed.status == "completed"
    # The retry genuinely re-executed instead of returning a duplicate skip.
    assert extractor.calls == 2
    assert store.outcomes["mat-1"].status == "committed"
    audit = service.audit_for("mat-1")
    assert audit is not None
    assert audit.status == "completed"


def test_atomic_pair_failure_also_fails_conservatively_and_retries():
    store = AtomicOutcomeStore(failures=1)
    service = make_service(store)

    with pytest.raises(PipelineError, match="persistence"):
        run(service.run_material(make_result()))
    # The committed pair write is all-or-nothing: only the compensating
    # failed pair ever landed.
    assert [
        (outcome.status, audit.status)
        for outcome, audit in store.terminal
    ] == [("failed", "failed")]
    assert service.outcome_for("mat-1").status == "failed"

    completed = run(service.run_material(make_result()))

    assert completed.status == "completed"
    outcome, audit = store.terminal[-1]
    assert outcome.status == "committed"
    assert audit.status == "completed"


def test_a_stage_failure_is_never_masked_by_a_broken_ledger():
    service = make_service(BrokenLedgerStore(), extractor=FailingExtractor())

    # The structured stage failure stays the raised error; the secondary
    # ledger failure is never allowed to replace it.
    with pytest.raises(PipelineError):
        run(service.run_material(make_result()))


# ------------------------------------------------------ ledger pair-write gate


def test_ledger_record_terminal_writes_both_rows_or_neither(sandbox):
    ledger = PipelineOutcomeLedger(make_paths(sandbox))
    try:
        ledger.record_terminal(
            PipelineOutcome("mat-1", "committed", T1), completed_audit("mat-1")
        )

        assert [
            (outcome.material_id, outcome.status)
            for outcome in ledger.entries()
        ] == [("mat-1", "committed")]
        assert [audit.material_id for audit in ledger.run_audit_entries()] == [
            "mat-1"
        ]

        with pytest.raises(ValueError, match="same material"):
            ledger.record_terminal(
                PipelineOutcome(
                    "mat-2", "failed", T1, stage="extract",
                    error_type="RuntimeError", message="boom",
                ),
                completed_audit("mat-1"),
            )
        with pytest.raises(ValueError, match="pending"):
            ledger.record_terminal(
                PipelineOutcome("mat-3", "pending", T0),
                PipelineRunAudit("mat-3", "failed"),
            )
        with pytest.raises(TypeError):
            ledger.record_terminal(
                PipelineOutcome("mat-4", "committed", T1), "not an audit"
            )
        # None of the refused pairs wrote anything.
        assert [outcome.material_id for outcome in ledger.entries()] == [
            "mat-1"
        ]
    finally:
        ledger.close()


# ----------------------------------------- restart failure recovery (WB review)


def test_failed_outcomes_rehydrate_the_failure_record_after_a_restart():
    store = MemoryOutcomeStore()
    first = make_service(store, extractor=FailingExtractor())
    with pytest.raises(PipelineError):
        run(first.run_material(make_result()))

    second = make_service(store)

    # The structured PipelineFailure itself is in-process only, so the
    # durable failed outcome must rebuild it for post-restart journeys.
    failure = second.failure_for("mat-1")
    assert failure is not None
    assert failure.stage == "extract"
    assert failure.error_type == "RuntimeError"
    assert "extractor exploded" in failure.message
    assert failure.failed_at is not None
    assert second.outcome_for("mat-1").status == "failed"
    # A committed outcome rehydrates no failure record.
    committed_store = MemoryOutcomeStore()
    ok = make_service(committed_store)
    run(ok.run_material(make_result()))
    restarted = make_service(committed_store)
    assert restarted.failure_for("mat-1") is None
