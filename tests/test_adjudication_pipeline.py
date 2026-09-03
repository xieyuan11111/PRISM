"""Pipeline-level wiring tests for the LLM adjudication layer.

The adjudicator runs inside the extract stage between the deterministic
extractor and the case/graph recording; these tests lock the pipeline
contract: no candidates -> no LLM round trip, adjudicated results replace
the extract-stage result, case-less candidates still persist with status
``awaiting_case_binding``, the declared target case is forwarded, and
adjudication-layer failures (format/role/transport) fail OPEN on the
already-verified first-layer extraction while non-adjudication-layer
failures stay fail-closed extract-stage failures.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.adjudication import (
    AdjudicationBatchFailure,
    AdjudicationDecision,
    AdjudicationLedger,
    AdjudicationService,
)
from prism.cases import CaseBundleMerger, CaseExtractionLedger, CaseService
from prism.config import PathConfig
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
    ExtractionService,
)
from prism.graph import GraphWriteResult
from prism.ingestion import IngestionResult
from prism.llm import MissingRoleError, TaskRole
from prism.pipeline import PipelineError, PipelineService
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
    node_ids=("node-1", "node-2"),
)
LOCATOR = EvidenceLocator("mat-1", "corpus/doc-mat-1.md", paragraph=1, quote="The agency published the revised policy.")


def _node(node_id: str) -> EvolutionNode:
    return EvolutionNode(
        id=node_id,
        case_id="case-1",
        node_type="publication",
        happened_at=PUBLISHED,
        summary="The revised policy was published.",
        source_ids=("mat-1",),
        evidence=(LOCATOR,),
        valid_at=PUBLISHED,
        observed_at=PUBLISHED,
        provenance_type="source_explicit",
        evidence_role="primary_observation",
    )


def make_material(**overrides) -> Material:
    values = {
        "id": "mat-1",
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


def make_index_outcome() -> IndexOutcome:
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
    return IndexOutcome(entry, "indexed")


class FakeClock:
    def __init__(self) -> None:
        self._next = T0

    def __call__(self) -> datetime:
        value = self._next
        self._next = value + CLOCK_STEP
        return value


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def index_file(self, path) -> IndexOutcome:
        self.calls.append(Path(path))
        return make_index_outcome()


class FakeExtractor:
    def __init__(self, result: ExtractionResult) -> None:
        self.calls: list[tuple[Material, object]] = []
        self.result = result

    async def extract(self, material: Material) -> ExtractionResult:
        self.calls.append((material, None))
        return self.result

    async def extract_material(self, material: Material, *, corpus_path=None, target_case=None) -> ExtractionResult:
        self.calls.append((material, target_case))
        return self.result


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def add_case(self, case, *, nodes=(), facts=(), claims=(), materials=(), **kwargs):
        self.calls.append((case, tuple(nodes), tuple(facts), tuple(claims), tuple(materials)))
        return GraphWriteResult((), ("episode-1",), ())


class FakeCaseRecorder:
    def __init__(self) -> None:
        self.record_calls: list[tuple[Material, ExtractionResult]] = []
        self.material_calls: list[tuple[Material, ExtractionResult]] = []

    async def record_extraction(self, material: Material, extraction: ExtractionResult) -> object:
        self.record_calls.append((material, extraction))
        return SimpleNamespace(
            write=GraphWriteResult((), ("episode-1",), ()),
            material_ids=("mat-1",),
        )

    async def record_material_extraction(self, material: Material, extraction: ExtractionResult) -> object:
        self.material_calls.append((material, extraction))
        return SimpleNamespace(
            material_id=material.id,
            status="awaiting_case_binding",
            extraction=extraction,
        )


class FakeAdjudicator:
    """Canned adjudicator: returns ``result`` or forwards the extraction."""

    def __init__(self, result: ExtractionResult | None = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.result = result
        self.exc = exc

    async def adjudicate(self, material: Material, extraction: ExtractionResult, *, target_case=None, corpus_path=None) -> object:
        self.calls.append((material, extraction, target_case, corpus_path))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(extraction=self.result or extraction)


def make_service(*, extractor, adjudicator=None, case_recorder=None, graph=None):
    clock = FakeClock()
    indexer = FakeIndexer()
    graph = graph or FakeGraph()
    service = PipelineService(
        indexer=indexer,
        extraction_service=extractor,
        graph_service=graph,
        clock=clock,
        case_service=case_recorder,
        adjudicator=adjudicator,
    )
    return service, indexer, extractor, graph


async def _run(service, result, **kwargs):
    return await service.run_material(result, **kwargs)


# ---------------------------------------------------------------------------

def test_adjudicator_is_not_invoked_for_empty_or_gap_only_extractions():
    async def main():
        empty = ExtractionResult(case=None)
        service, _, extractor, _ = make_service(extractor=FakeExtractor(empty),
                                                adjudicator=FakeAdjudicator())
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert extractor.calls
        assert service._adjudicator.calls == []
        gap_only = ExtractionResult(
            case=None,
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "evidence_location_failed", "not found", "node", "n9",
                    source_ids=("mat-1",),
                ),
            ),
        )
        service, _, _, _ = make_service(extractor=FakeExtractor(gap_only),
                                        adjudicator=FakeAdjudicator())
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert service._adjudicator.calls == []

    asyncio.run(main())


def test_adjudicated_extraction_replaces_the_extract_stage_result():
    async def main():
        original = ExtractionResult(case=CASE, nodes=(_node("node-1"), _node("node-2")))
        adjudicated = ExtractionResult(case=CASE, nodes=(_node("node-1"),))
        extractor = FakeExtractor(original)
        adjudicator = FakeAdjudicator(result=adjudicated)
        service, _, _, graph = make_service(extractor=extractor, adjudicator=adjudicator)
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert len(adjudicator.calls) == 1
        extract_stage = run.stages[1]
        assert extract_stage.status == "extracted"
        assert extract_stage.result is adjudicated
        assert [node.id for node in graph.calls[0][1]] == ["node-1"]

    asyncio.run(main())


def test_adjudicator_receives_the_declared_target_case():
    async def main():
        target = EvolutionCase(
            case_id="case-1", case_type="policy", canonical_name="Revised policy",
            start_at=CASE.start_at, status="active", node_ids=("node-1",),
        )
        extraction = ExtractionResult(case=target, nodes=(_node("node-1"),))
        extractor = FakeExtractor(extraction)
        adjudicator = FakeAdjudicator()
        service, _, _, _ = make_service(extractor=extractor, adjudicator=adjudicator)
        run = await _run(service, make_result(), target_case=target)
        assert run.status == "completed"
        assert extractor.calls and extractor.calls[0][1] is target
        assert adjudicator.calls[0][2] is target

    asyncio.run(main())


def test_non_adjudication_layer_failure_stays_fail_closed():
    async def main():
        original = ExtractionResult(case=CASE, nodes=(_node("node-1"),))
        extractor = FakeExtractor(original)
        # A failure that is NOT a structured adjudication-layer error (a bug
        # or a storage failure) still fails the extract stage closed.
        adjudicator = FakeAdjudicator(exc=RuntimeError("adjudicator bug"))
        service, _, _, graph = make_service(extractor=extractor, adjudicator=adjudicator)
        with pytest.raises(PipelineError) as caught:
            await _run(service, make_result())
        assert caught.value.stage == "extract"
        failure = service.failure_for("mat-1")
        assert failure is not None and failure.stage == "extract"
        assert graph.calls == []
        assert service.outcome_for("mat-1").status == "failed"

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Second-layer fail-open (real-smoke regression): the FIRST-layer extraction
# is already strictly verified; when the adjudication layer itself fails
# (malformed decisions JSON, missing role, transport timeout), the pipeline
# keeps the original verified extraction, attaches a canonical warning, and
# continues case merge + graph write.  Only adjudication-layer failures fail
# open; the failure reason is audit text, never a graph fact.
# ---------------------------------------------------------------------------


class _RaisingDecisionRouter:
    """Canned ADJUDICATE router raising the given transport/role error."""

    def __init__(self, exc):
        self.exc = exc
        self.calls: list[str] = []

    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        self.calls.append(prompt)
        raise self.exc


def test_real_adjudicator_batch_format_failure_fails_open_with_audit(tmp_path):
    async def main():
        node_1 = _node("node-1")
        node_2 = _node("node-2")
        original = ExtractionResult(case=CASE, nodes=(node_1, node_2))
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        secret = "MODEL-SECRET-552211"
        # Real-like second-layer output: a decision object with an extra
        # field the strict schema forbids (the smoke failure shape).
        router = _DecisionRouter([
            {
                "candidate_kind": "node",
                "candidate_id": "node-1",
                "decision": "accepted",
                "reason": "ok",
                "unexpected_field": secret,
            },
        ])
        paths = PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)
        ledger = AdjudicationLedger(paths)
        adjudicator = _RealAdjudicator(
            router,
            extraction_service=_dummy_extraction_service(),
            ledger=ledger,
        )
        recorder, _ = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        # Fail open: the original verified extraction replaced nothing and
        # carries exactly one canonical warning.
        extract_stage = run.stages[1]
        assert extract_stage.status == "extracted"
        assert [node.id for node in extract_stage.result.nodes] == [
            "node-1", "node-2",
        ]
        assert extract_stage.result.nodes[0] is node_1
        assert extract_stage.result.nodes[1] is node_2
        warnings = extract_stage.result.warnings
        assert len(warnings) == 1
        assert "adjudication" in warnings[0].lower()
        assert "unknown_decision_fields" in warnings[0]
        assert secret not in warnings[0]
        # The case merge and graph write proceeded with the original
        # candidates (the case merger re-scopes ids as material::local);
        # the raw model garbage never reaches the graph body.
        assert run.stages[2].status == "written"
        written_local_ids = {
            node.id.rsplit("::", 1)[-1] for node in graph.calls[0][1]
        }
        assert written_local_ids == {"node-1", "node-2"}
        assert secret not in repr(graph.calls)
        # The batch failure is persisted as one durable candidate_kind=batch
        # audit record and stays readable after a restart.
        reopened = AdjudicationLedger(paths)
        entries = reopened.entries("mat-1")
        assert len(entries) == 1
        batch = entries[0]
        assert batch.candidate_kind == "batch"
        assert batch.decision == "adjudication_failed"
        # The restart read-back carries the formal enum, never a bare string:
        # a consumer using decision.value on the durable row must not crash.
        assert isinstance(batch.decision, AdjudicationDecision)
        assert batch.decision is AdjudicationDecision.ADJUDICATION_FAILED
        assert batch.decision.value == "adjudication_failed"
        assert batch.revalidation_outcome == "adjudication_failed"
        assert "unknown_decision_fields" in batch.reason
        raw = (paths.data_dir / "index.db").read_bytes()
        assert secret.encode("utf-8") not in raw
        ledger.close()
        reopened.close()

    asyncio.run(main())


def test_real_adjudicator_role_and_transport_failures_fail_open_with_audit(
    tmp_path,
):
    async def main():
        cases = [
            (MissingRoleError("missing task role: 'adjudicate'"), "missing_role"),
            (TimeoutError("slow transport"), "transport_failure"),
        ]
        for exc, code in cases:
            original = ExtractionResult(
                case=CASE,
                nodes=(_node("node-1"), _node("node-2")),
            )
            graph = FakeGraph()
            extractor = FakeExtractor(original)
            router = _RaisingDecisionRouter(exc)
            paths = PathConfig(
                data_dir=tmp_path / code
            ).resolve(tmp_path)
            ledger = AdjudicationLedger(paths)
            adjudicator = _RealAdjudicator(router, ledger=ledger)
            recorder, _ = make_real_recorder(tmp_path / code, graph)
            service, _, _, _ = make_service(
                extractor=extractor,
                adjudicator=adjudicator,
                case_recorder=recorder,
                graph=graph,
            )
            run = await _run(service, make_result())
            assert run.status == "completed"
            assert run.stages[1].result is not original
            assert {n.id for n in run.stages[1].result.nodes} == {
                "node-1", "node-2",
            }
            assert run.stages[2].status == "written"
            written_local_ids = {
                node.id.rsplit("::", 1)[-1] for node in graph.calls[0][1]
            }
            assert written_local_ids == {"node-1", "node-2"}
            entries = AdjudicationLedger(paths).entries("mat-1")
            assert len(entries) == 1
            assert entries[0].candidate_kind == "batch"
            assert code in entries[0].reason
            ledger.close()

    asyncio.run(main())


def test_fake_adjudicator_role_and_transport_errors_still_fail_open():
    async def main():
        for exc in (MissingRoleError("no route"), TimeoutError("slow")):
            original = ExtractionResult(case=CASE, nodes=(_node("node-1"),))
            extractor = FakeExtractor(original)
            adjudicator = FakeAdjudicator(exc=exc)
            service, _, _, graph = make_service(
                extractor=extractor, adjudicator=adjudicator
            )
            run = await _run(service, make_result())
            assert run.status == "completed"
            assert run.stages[2].status == "written"
            assert [node.id for node in graph.calls[0][1]] == ["node-1"]
            extract_stage = run.stages[1]
            assert [node.id for node in extract_stage.result.nodes] == ["node-1"]
            assert any("adjudication" in w.lower() for w in extract_stage.result.warnings)

    asyncio.run(main())


def test_fake_adjudicator_batch_failure_fails_open_with_warning():
    async def main():
        original = ExtractionResult(case=CASE, nodes=(_node("node-1"),))
        extractor = FakeExtractor(original)
        adjudicator = FakeAdjudicator(
            exc=AdjudicationBatchFailure(
                "unknown_decision_fields", field="decisions[0]"
            )
        )
        service, _, _, graph = make_service(
            extractor=extractor, adjudicator=adjudicator
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result is not original
        assert [node.id for node in run.stages[1].result.nodes] == ["node-1"]
        assert "unknown_decision_fields" in run.stages[1].result.warnings[0]
        assert run.stages[2].status == "written"
        assert [node.id for node in graph.calls[0][1]] == ["node-1"]

    asyncio.run(main())


def test_extract_failure_stays_fail_closed_even_with_an_adjudicator():
    async def main():
        class _RaisingExtractor(FakeExtractor):
            async def extract_material(
                self, material, *, corpus_path=None, target_case=None
            ):
                raise ValueError("first-layer extraction failed")

        extractor = _RaisingExtractor(ExtractionResult(case=None))
        adjudicator = FakeAdjudicator()
        service, _, _, graph = make_service(
            extractor=extractor, adjudicator=adjudicator
        )
        with pytest.raises(PipelineError) as caught:
            await _run(service, make_result())
        assert caught.value.stage == "extract"
        assert adjudicator.calls == []
        assert graph.calls == []
        assert service.outcome_for("mat-1").status == "failed"
        assert service.failure_for("mat-1").stage == "extract"

    asyncio.run(main())


def test_unsafe_revision_fails_revalidation_and_never_enters_the_graph(
    tmp_path,
):
    async def main():
        content = "The agency published the revised policy."
        material = make_material(content=content)
        node = _node("node-1")
        original = ExtractionResult(case=CASE, nodes=(node, _node("node-2")))
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        fabricated = "This sentence does not exist in the material body."
        router = _DecisionRouter([
            _decision(
                "node", "node-1", "revised", reason="rewrite",
                revised_payload={
                    "summary": "FABRICATED-NODE-SUMMARY-778899",
                    "evidence": [
                        {
                            "source_id": "mat-1",
                            "quote": fabricated,
                            "paragraph": 1,
                            "page": None,
                        }
                    ],
                },
            ),
        ])
        paths = PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)
        ledger = AdjudicationLedger(paths)
        adjudicator = _RealAdjudicator(
            router,
            extraction_service=_dummy_extraction_service(),
            ledger=ledger,
        )
        recorder, _ = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result(material))
        assert run.status == "completed"
        assert run.stages[2].status == "written"
        written = graph.calls[0][1]
        written_by_local = {
            node.id.rsplit("::", 1)[-1]: node for node in written
        }
        # The unvalidated revision never entered the graph: the original,
        # strictly verified node was written instead.
        assert written_by_local["node-1"].summary == node.summary
        assert fabricated not in repr(graph.calls)
        assert "FABRICATED-NODE-SUMMARY-778899" not in repr(graph.calls)
        entries = AdjudicationLedger(paths).entries("mat-1")
        assert len(entries) == 1
        assert entries[0].revalidation_outcome.startswith(
            "rejected_revalidation"
        )
        ledger.close()

    asyncio.run(main())


def test_adjudicated_caseless_candidates_still_persist_awaiting_binding():
    async def main():
        node = _node("node-1")
        caseless = ExtractionResult(
            case=None,
            nodes=(node,),
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "missing_case_context", "no case supplied",
                    source_ids=("mat-1",),
                ),
            ),
        )
        adjudicated = ExtractionResult(
            case=None, nodes=(node,),
            evidence_gaps=caseless.evidence_gaps,
            warnings=("LLM automatic adjudication accepted for node:node-1: ok",),
        )
        extractor = FakeExtractor(caseless)
        recorder = FakeCaseRecorder()
        adjudicator = FakeAdjudicator(result=adjudicated)
        service, _, _, _ = make_service(extractor=extractor,
                                        adjudicator=adjudicator,
                                        case_recorder=recorder)
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert recorder.material_calls == [(make_result().material, adjudicated)]
        assert recorder.record_calls == []
        assert run.stages[2].status == "skipped"
        assert "awaiting_case_binding" in run.stages[2].detail
        assert adjudicator.calls[0][1] is caseless

    asyncio.run(main())


def test_no_adjudicator_keeps_the_legacy_behaviour():
    async def main():
        original = ExtractionResult(case=CASE, nodes=(_node("node-1"),))
        extractor = FakeExtractor(original)
        service, _, _, graph = make_service(extractor=extractor)
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result is original
        assert [node.id for node in graph.calls[0][1]] == ["node-1"]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Real-adjudicator contract: case-less candidates that survive LLM automatic
# adjudication (accepted / revised / preserve_conflict) must keep the
# material-scoped staging semantics — pipeline completes, the graph stage is
# skipped, and the durable material_evidence_ledger row carries status
# ``awaiting_case_binding``.  The real CaseService gate raises unless the
# adjudicated extraction still reports that status, so these tests lock the
# whole real path (no fakes for the recorder).
# ---------------------------------------------------------------------------


class _DecisionRouter:
    """Canned ADJUDICATE completion carrying the given decisions array."""

    def __init__(self, decisions):
        self.decisions = decisions
        self.calls: list[str] = []

    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        self.calls.append(prompt)
        return SimpleNamespace(text=json.dumps({"decisions": self.decisions}))


class _RealAdjudicator:
    """The real AdjudicationService plus a call log for pipeline assertions."""

    def __init__(self, router, *, extraction_service=None, ledger=None):
        self.calls: list[tuple] = []
        self._service = AdjudicationService(
            router,
            ledger=ledger,
            extraction_service=extraction_service,
        )

    async def adjudicate(self, material, extraction, *, target_case=None, corpus_path=None):
        self.calls.append((material, extraction, target_case, corpus_path))
        return await self._service.adjudicate(
            material, extraction, target_case=target_case, corpus_path=corpus_path
        )

    def history(self, material_id=None):
        return self._service.history(material_id)


def _dummy_extraction_service() -> ExtractionService:
    class _NeverCalled:
        async def complete(self, role, prompt):
            raise AssertionError("extraction router must not be called")

    return ExtractionService(_NeverCalled())


def _fact(fact_id: str = "fact-1") -> TemporalFact:
    return TemporalFact(
        "Agency", "published", "Revised policy", PUBLISHED, None, PUBLISHED,
        ("mat-1",), 0.9, "source_explicit",
        evidence=(LOCATOR,), fact_id=fact_id,
        evidence_role="primary_observation",
    )


def _claim(claim_id: str = "claim-1") -> Claim:
    return Claim(
        claim_id, "Agency", "The revision improves clarity.", "support",
        PUBLISHED,
        based_on=("mat-1",), evidence=(LOCATOR,),
        observed_at=PUBLISHED, provenance_type="source_explicit",
        evidence_role="primary_observation",
    )


def _conflict(conflict_id: str = "conflict-1") -> ExtractionConflict:
    return ExtractionConflict(
        conflict_id, "agency", "status", ("active", "paused"), ("mat-1",),
        evidence=(LOCATOR,),
        valid_at=PUBLISHED, observed_at=PUBLISHED,
        evidence_role="primary_observation",
    )


def _caseless(**collections) -> ExtractionResult:
    """A strict-extractor-shaped case-less result with substantive
    candidates, a ``missing_case_context`` gap and a material role."""
    return ExtractionResult(
        case=None,
        evidence_gaps=(
            ExtractionEvidenceGap(
                "missing_case_context", "no case supplied",
                source_ids=("mat-1",),
            ),
        ),
        material_role="news_report",
        **collections,
    )


def make_real_recorder(tmp_path: Path, graph: FakeGraph):
    """The real CaseService + durable ledger + merger, sharing ``graph``."""
    paths = PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)
    ledger = CaseExtractionLedger(paths)
    service = CaseService(
        ledger=ledger, merger=CaseBundleMerger(), graph_service=graph
    )
    return service, ledger


def _decision(kind: str, ident: str, decision: str, **extra) -> dict:
    item = {
        "candidate_kind": kind,
        "candidate_id": ident,
        "decision": decision,
        "reason": "reviewed",
    }
    item.update(extra)
    return item


def test_real_adjudicator_accepted_caseless_candidates_complete_and_stage(
    tmp_path,
):
    async def main():
        node = _node("node-1")
        fact = _fact()
        claim = _claim()
        original = _caseless(nodes=(node,), temporal_facts=(fact,), claims=(claim,))
        assert original.accumulation_status == "awaiting_case_binding"
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        router = _DecisionRouter([
            _decision("node", "node-1", "accepted"),
            _decision("temporal_fact", "fact-1", "accepted"),
            _decision("claim", "claim-1", "accepted"),
        ])
        adjudicator = _RealAdjudicator(
            router, extraction_service=_dummy_extraction_service()
        )
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        extract_stage = run.stages[1]
        assert extract_stage.status == "extracted"
        # The adjudicated extraction still declares the material-scoped
        # accumulation state the recorder gate requires.
        assert extract_stage.result.accumulation_status == "awaiting_case_binding"
        # Adjudication is audit-only for accepted candidates: the original
        # candidate objects survive untouched.
        assert extract_stage.result.nodes[0] is node
        assert extract_stage.result.temporal_facts[0] is fact
        assert extract_stage.result.claims[0] is claim
        assert original.nodes == (node,)
        assert original.accumulation_status == "awaiting_case_binding"
        # The graph stage is skipped and the durable material ledger row
        # carries status awaiting_case_binding with the candidates intact.
        graph_stage = run.stages[2]
        assert graph_stage.status == "skipped"
        assert "awaiting_case_binding" in graph_stage.detail
        assert graph.calls == []
        entry = ledger.material_entry("mat-1")
        assert entry is not None
        assert entry.status == "awaiting_case_binding"
        assert entry.extraction.accumulation_status == "awaiting_case_binding"
        assert [c.id for c in entry.extraction.nodes] == ["node-1"]
        assert [c.fact_id for c in entry.extraction.temporal_facts] == ["fact-1"]
        assert [c.claim_id for c in entry.extraction.claims] == ["claim-1"]
        ledger.close()

    asyncio.run(main())


def test_real_adjudicator_revised_caseless_candidate_completes_and_stages(
    tmp_path,
):
    async def main():
        fact = _fact()
        original = _caseless(temporal_facts=(fact,))
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        router = _DecisionRouter([
            _decision(
                "temporal_fact", "fact-1", "revised",
                revised_payload={"subject": "Agency revised"},
            ),
        ])
        adjudicator = _RealAdjudicator(
            router, extraction_service=_dummy_extraction_service()
        )
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result.accumulation_status == "awaiting_case_binding"
        assert run.stages[2].status == "skipped"
        assert graph.calls == []
        entry = ledger.material_entry("mat-1")
        assert entry is not None
        assert entry.status == "awaiting_case_binding"
        # The strict revalidation merged the revision over the original
        # candidate and the staged extraction keeps every verified field.
        assert entry.extraction.temporal_facts[0].subject == "Agency revised"
        assert entry.extraction.temporal_facts[0].predicate == "published"
        ledger.close()

    asyncio.run(main())


def test_real_adjudicator_preserve_conflict_caseless_completes_and_stages(
    tmp_path,
):
    async def main():
        conflict = _conflict()
        original = _caseless(conflicts=(conflict,))
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        router = _DecisionRouter([
            _decision("conflict", "conflict-1", "preserve_conflict"),
        ])
        adjudicator = _RealAdjudicator(router)
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result.accumulation_status == "awaiting_case_binding"
        assert run.stages[2].status == "skipped"
        assert "awaiting_case_binding" in run.stages[2].detail
        assert graph.calls == []
        entry = ledger.material_entry("mat-1")
        assert entry is not None
        assert entry.status == "awaiting_case_binding"
        assert [c.conflict_id for c in entry.extraction.conflicts] == ["conflict-1"]
        ledger.close()

    asyncio.run(main())


def test_real_adjudicator_rejecting_every_caseless_candidate_enters_no_ledger(
    tmp_path,
):
    async def main():
        original = _caseless(
            nodes=(_node("node-1"),), claims=(_claim("claim-1"),)
        )
        graph = FakeGraph()
        extractor = FakeExtractor(original)
        router = _DecisionRouter([
            _decision("node", "node-1", "rejected"),
            _decision("claim", "claim-1", "rejected"),
        ])
        adjudicator = _RealAdjudicator(router)
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result.nodes == ()
        assert run.stages[1].result.claims == ()
        assert run.stages[2].status == "skipped"
        assert ledger.material_entry("mat-1") is None
        assert ledger.material_entries() == ()
        assert graph.calls == []
        ledger.close()

    asyncio.run(main())


def test_real_adjudicator_accepted_case_bound_candidates_stay_unchanged(
    tmp_path,
):
    async def main():
        extraction = ExtractionResult(
            case=CASE,
            nodes=(_node("node-1"), _node("node-2")),
        )
        graph = FakeGraph()
        extractor = FakeExtractor(extraction)
        router = _DecisionRouter([
            _decision("node", "node-1", "accepted"),
            _decision("node", "node-2", "accepted"),
        ])
        adjudicator = _RealAdjudicator(router)
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        assert run.stages[1].result.accumulation_status == "case_bound"
        assert run.stages[2].status == "written"
        assert graph.calls  # the accumulated case was really written
        entries = ledger.entries("case-1")
        assert len(entries) == 1
        assert entries[0].extraction.accumulation_status == "case_bound"
        assert {node.id for node in entries[0].extraction.nodes} == {
            "node-1", "node-2",
        }
        ledger.close()

    asyncio.run(main())


def test_real_adjudicator_target_case_path_does_not_regress(tmp_path):
    async def main():
        target = EvolutionCase(
            case_id="case-1", case_type="policy",
            canonical_name="Revised policy",
            start_at=CASE.start_at, status="active",
            node_ids=("node-1",),
        )
        extraction = ExtractionResult(case=target, nodes=(_node("node-1"),))
        graph = FakeGraph()
        extractor = FakeExtractor(extraction)
        router = _DecisionRouter([
            _decision("node", "node-1", "accepted"),
        ])
        adjudicator = _RealAdjudicator(router)
        recorder, ledger = make_real_recorder(tmp_path, graph)
        service, _, _, _ = make_service(
            extractor=extractor,
            adjudicator=adjudicator,
            case_recorder=recorder,
            graph=graph,
        )
        run = await _run(service, make_result(), target_case=target)
        assert run.status == "completed"
        # The declared target case reaches the real adjudicator.
        assert adjudicator.calls[0][2] is target
        assert run.stages[2].status == "written"
        entries = ledger.entries("case-1")
        assert len(entries) == 1
        assert entries[0].extraction.case.case_id == "case-1"
        ledger.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Gap-input closure: the adjudicator must also run when the deterministic
# layer demoted every candidate to evidence gaps that carry candidate
# payloads, so gapped candidates can be repaired and revived into the graph.
# ---------------------------------------------------------------------------


def _gap_node_payload(node_id: str, quote: str, paragraph: int) -> dict:
    return {
        "id": node_id,
        "case_id": "case-1",
        "node_type": "publication",
        "assertion_type": "fact",
        "happened_at": PUBLISHED.isoformat(),
        "valid_at": PUBLISHED.isoformat(),
        "observed_at": PUBLISHED.isoformat(),
        "summary": "The revised policy was published.",
        "source_ids": ["mat-1"],
        "claim_ids": [],
        "provenance_type": "source_explicit",
        "evidence_role": "primary_observation",
        "evidence": [
            {"source_id": "mat-1", "quote": quote, "paragraph": paragraph, "page": None}
        ],
    }


def test_adjudicator_is_invoked_for_gap_only_extractions_with_payloads():
    async def main():
        gap_only = ExtractionResult(
            case=None,
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "candidate_validation_failed",
                    "node-1 not graph-ready: bad time",
                    "node",
                    "node-1",
                    ("mat-1",),
                    _gap_node_payload("node-1", "published", 1),
                ),
            ),
        )
        extractor = FakeExtractor(gap_only)
        adjudicator = FakeAdjudicator()
        service, _, _, graph = make_service(
            extractor=extractor, adjudicator=adjudicator
        )
        run = await _run(service, make_result())
        assert run.status == "completed"
        # A payload-bearing gap is an adjudication input even when no
        # candidate is graph-ready.
        assert len(adjudicator.calls) == 1
        assert adjudicator.calls[0][1] is gap_only
        # The corpus path needed to re-bind quotes is forwarded.
        assert adjudicator.calls[0][3] == Path("corpus") / "doc-mat-1.md"
        assert run.stages[1].result is gap_only
        assert run.stages[2].status == "skipped"
        assert graph.calls == []

    asyncio.run(main())


def test_real_adjudicator_revives_a_gapped_node_and_writes_the_case():
    async def main():
        content = (
            "The agency published the revised policy.\n\n"
            "Analysts welcomed the clarity."
        )
        material = make_material(content=content)
        case = replace(CASE, node_ids=())
        gap_only = ExtractionResult(
            case=case,
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "evidence_location_failed",
                    "node-2 quote was not found verbatim",
                    "node",
                    "node-2",
                    ("mat-1",),
                    _gap_node_payload(
                        "node-2", "words absent from the material", 2
                    ),
                ),
            ),
        )
        graph = FakeGraph()
        extractor = FakeExtractor(gap_only)
        router = _DecisionRouter([
            _decision(
                "node", "node-2", "revised", reason="verbatim quote",
                revised_payload={
                    "evidence": [
                        {
                            "source_id": "mat-1",
                            "quote": "Analysts welcomed the clarity.",
                            "paragraph": 2,
                            "page": None,
                        }
                    ]
                },
            ),
        ])
        adjudicator = _RealAdjudicator(
            router, extraction_service=_dummy_extraction_service()
        )
        service, _, _, _ = make_service(
            extractor=extractor, adjudicator=adjudicator, graph=graph
        )
        run = await _run(service, make_result(material))
        assert run.status == "completed"
        assert len(adjudicator.calls) == 1
        assert adjudicator.calls[0][3] == Path("corpus") / "doc-mat-1.md"
        # The repaired node passed strict revalidation and replaced the
        # extract-stage result; the graph write carries it under the case.
        assert run.stages[1].result is not gap_only
        assert [node.id for node in run.stages[1].result.nodes] == ["node-2"]
        assert run.stages[2].status == "written"
        assert graph.calls
        assert [node.id for node in graph.calls[0][1]] == ["node-2"]
        assert graph.calls[0][0].node_ids == ("node-2",)
        assert (
            graph.calls[0][1][0].evidence[0].quote
            == "Analysts welcomed the clarity."
        )

    asyncio.run(main())
