"""Focused tests for the accumulating case service (module: cases.service)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.cases.ledger import CaseExtractionLedger, MaterialCaseConflict
from prism.cases.service import CaseService, CaseWriteOutcome
from prism.cases import CaseBundleMerger, MergedCaseBundle
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
)
from prism.graph import GraphWriteResult


PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def make_case(case_id: str = "case-1", **overrides) -> EvolutionCase:
    values = {
        "case_id": case_id,
        "case_type": "policy",
        "canonical_name": "Revised policy",
        "start_at": datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc),
        "status": "active",
    }
    values.update(overrides)
    return EvolutionCase(**values)


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


def make_extraction(
    material_id: str = "mat-1",
    *,
    case: EvolutionCase | None = None,
    node_case_id: str | None = None,
    node_id: str = "node-1",
    gaps: tuple[ExtractionEvidenceGap, ...] = (),
    conflicts: tuple[ExtractionConflict, ...] = (),
    warnings: tuple[str, ...] = (),
) -> ExtractionResult:
    bound_case = case if case is not None else make_case()
    locator = EvidenceLocator(
        source_id=material_id,
        corpus_path=f"corpus/2026-08/example/doc-{material_id}.md",
        paragraph=1,
        quote="The agency published the revised policy.",
    )
    node = EvolutionNode(
        id=node_id,
        case_id=node_case_id if node_case_id is not None else bound_case.case_id,
        node_type="publication",
        happened_at=PUBLISHED,
        summary="The revised policy was published.",
        source_ids=(material_id,),
        claim_ids=(),
        valid_at=PUBLISHED,
        observed_at=PUBLISHED,
        evidence=(locator,),
        provenance_type="explicit",
    )
    fact = TemporalFact(
        subject="Agency",
        predicate="published",
        object="Revised policy",
        valid_at=PUBLISHED,
        invalid_at=None,
        observed_at=PUBLISHED,
        source_ids=(material_id,),
        confidence=0.82,
        provenance_type="explicit",
        evidence=(locator,),
    )
    claim = Claim(
        claim_id="claim-1",
        actor="Agency",
        proposition="The revision improves clarity.",
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
        warnings=warnings,
        conflicts=conflicts,
        evidence_gaps=gaps,
    )


class DedupeGraph:
    """Offline graph writer that dedupes episode keys like a real backend."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.keys: set[str] = set()
        self.exc = exc

    async def add_case(self, case, *, nodes=(), facts=(), claims=(), materials=()):
        self.calls.append((case, tuple(nodes), tuple(facts), tuple(claims), tuple(materials)))
        if self.exc is not None:
            raise self.exc
        added: list[str] = []
        skipped: list[str] = []
        for node in nodes:
            key = f"{case.case_id}:node:{node.id}"
            (added if key not in self.keys else skipped).append(key)
            self.keys.add(key)
        for fact in facts:
            key = f"{case.case_id}:fact:{fact.subject}:{fact.predicate}"
            (added if key not in self.keys else skipped).append(key)
            self.keys.add(key)
        for claim in claims:
            key = f"{case.case_id}:claim:{claim.claim_id}"
            (added if key not in self.keys else skipped).append(key)
            self.keys.add(key)
        return GraphWriteResult((), tuple(added), tuple(skipped))


def make_service(tmp_path: Path, graph: DedupeGraph | None = None):
    ledger = CaseExtractionLedger(PathConfig(data_dir=tmp_path / "data").resolve(tmp_path))
    graph = graph if graph is not None else DedupeGraph()
    service = CaseService(ledger=ledger, merger=CaseBundleMerger(), graph_service=graph)
    return service, ledger, graph


def run(coro):
    return asyncio.run(coro)


def test_record_extraction_merges_and_writes_the_accumulated_case(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        material_1 = make_material("mat-1")
        material_2 = make_material("mat-2")
        first = await service.record_extraction(
            material_1, make_extraction("mat-1")
        )
        assert isinstance(first, CaseWriteOutcome)
        assert first.case_id == "case-1"
        assert first.material_ids == ("mat-1",)
        assert first.bundle.materials == (material_1,)

        second = await service.record_extraction(
            material_2, make_extraction("mat-2", node_id="node-2")
        )
        assert second.material_ids == ("mat-1", "mat-2")
        assert second.bundle.materials == (material_1, material_2)
        # Scoped node ids keep per-material evidence separate; the case is
        # written once per merge with every accumulated material, never one
        # full case per material.
        assert {node.id for node in second.bundle.nodes} == {
            "mat-1::node-1",
            "mat-2::node-2",
        }
        assert len(graph.calls) == 2
        written_case, nodes, _facts, _claims, materials = graph.calls[-1]
        assert written_case.case_id == "case-1"
        assert len(nodes) == 2
        assert {m.id for m in materials} == {"mat-1", "mat-2"}

    run(main())


def test_record_extraction_is_idempotent_for_a_repeated_material(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        material = make_material("mat-1")
        extraction = make_extraction("mat-1")
        first = await service.record_extraction(material, extraction)
        assert first.write.added_keys != ()

        repeated = await service.record_extraction(material, extraction)
        assert repeated.material_ids == ("mat-1",)
        assert repeated.bundle == first.bundle
        assert repeated.write.added_keys == ()
        assert repeated.write.skipped_keys == first.write.added_keys
        assert len(ledger.entries("case-1")) == 1

    run(main())


def test_failed_merge_rolls_the_new_material_back(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        # A foreign node case without an explicit adopt must fail the merge.
        poisoned = make_extraction(
            "mat-2", node_id="node-2", node_case_id="case-9"
        )
        with pytest.raises(ValueError, match="foreign case"):
            await service.record_extraction(make_material("mat-2"), poisoned)

        assert [e.material_id for e in ledger.entries("case-1")] == ["mat-1"]
        outcome = await service.merge_case("case-1")
        assert outcome is not None
        assert outcome.material_ids == ("mat-1",)

    run(main())


def test_first_material_failure_leaves_the_ledger_empty(tmp_path):
    async def main():
        service, ledger, _ = make_service(tmp_path)
        poisoned = make_extraction("mat-1", node_case_id="case-9")
        with pytest.raises(ValueError, match="foreign case"):
            await service.record_extraction(make_material("mat-1"), poisoned)
        assert ledger.entries("case-1") == ()
        assert await service.merge_case("case-1") is None

    run(main())


def test_failed_graph_write_rolls_the_new_material_back(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        graph.exc = RuntimeError("graph backend unavailable")
        with pytest.raises(RuntimeError, match="graph backend unavailable"):
            await service.record_extraction(
                make_material("mat-2"), make_extraction("mat-2", node_id="node-2")
            )
        graph.exc = None
        assert [e.material_id for e in ledger.entries("case-1")] == ["mat-1"]

    run(main())


def test_replacing_a_material_rolls_back_to_the_previous_extraction(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        material = make_material("mat-1")
        good = make_extraction("mat-1")
        await service.record_extraction(material, good)

        graph.exc = RuntimeError("graph backend unavailable")
        with pytest.raises(RuntimeError):
            await service.record_extraction(
                material, make_extraction("mat-1", node_id="node-1b")
            )
        graph.exc = None

        entries = ledger.entries("case-1")
        assert [e.material_id for e in entries] == ["mat-1"]
        assert entries[0].extraction == good

    run(main())


def test_evidence_gaps_and_conflicts_never_silently_disappear(tmp_path):
    async def main():
        gap = ExtractionEvidenceGap(
            "evidence_location_failed",
            "quote was not found verbatim in material mat-1",
            "node",
            "node-9",
            ("mat-1",),
        )
        conflict = ExtractionConflict(
            conflict_id="conflict-1",
            subject="Agency",
            predicate="published",
            alternatives=("Revised policy", "Draft policy"),
            source_ids=("mat-1",),
            evidence=(
                EvidenceLocator(
                    source_id="mat-1",
                    corpus_path="corpus/2026-08/example/doc-mat-1.md",
                    paragraph=1,
                    quote="The agency published the revised policy.",
                ),
            ),
        )
        extraction = make_extraction(
            "mat-1",
            gaps=(gap,),
            conflicts=(conflict,),
            warnings=("model flagged low confidence",),
        )
        service, ledger, _ = make_service(tmp_path)
        outcome = await service.record_extraction(make_material("mat-1"), extraction)

        joined = "\n".join(outcome.warnings)
        assert "evidence gap" in joined and "evidence_location_failed" in joined
        assert "unresolved conflict" in joined and "conflict-1" in joined
        assert "model flagged low confidence" in joined

        # The persisted ledger keeps the gaps and conflicts verbatim, so a
        # rebuilt outcome after restart still reports them.
        rebuilt = await service.merge_case("case-1")
        assert rebuilt is not None
        assert "evidence_location_failed" in "\n".join(rebuilt.warnings)
        assert "conflict-1" in "\n".join(rebuilt.warnings)
        assert ledger.entries("case-1")[0].extraction.conflicts == (conflict,)

    run(main())


def test_case_metadata_divergence_warns_and_keeps_the_recorded_case(tmp_path):
    async def main():
        service, _, _graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        diverging = make_extraction(
            "mat-2",
            node_id="node-2",
            case=make_case(canonical_name="A different name", status="paused"),
        )
        outcome = await service.record_extraction(make_material("mat-2"), diverging)

        assert outcome.bundle.case.canonical_name == "Revised policy"
        assert outcome.bundle.case.status == "active"
        warning = "\n".join(outcome.warnings)
        assert "mat-2" in warning and "case record is kept" in warning
        assert "canonical_name" in warning and "status" in warning

    run(main())


def test_merge_case_returns_none_for_an_unknown_case(tmp_path):
    async def main():
        service, _, graph = make_service(tmp_path)
        assert await service.merge_case("case-unknown") is None
        assert graph.calls == []

    run(main())


def test_merge_case_rebuilds_the_same_bundle_after_restart(tmp_path):
    async def main():
        service, _, _ = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        await service.record_extraction(
            make_material("mat-2"), make_extraction("mat-2", node_id="node-2")
        )
        original = await service.merge_case("case-1")
        assert original is not None

        # A fresh service over the same PRISM data dir rebuilds the identical
        # accumulated bundle from the ledger alone, with no re-extraction.
        restarted_ledger = CaseExtractionLedger(
            PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)
        )
        restarted = CaseService(
            ledger=restarted_ledger,
            merger=CaseBundleMerger(),
            graph_service=DedupeGraph(),
        )
        outcome = await restarted.merge_case("case-1")
        assert outcome is not None
        assert isinstance(outcome.bundle, MergedCaseBundle)
        assert outcome.bundle == original.bundle
        assert outcome.material_ids == ("mat-1", "mat-2")
        assert restarted.case_for_material("mat-2") == "case-1"

    run(main())


def test_merge_explicit_selects_only_requested_materials(tmp_path):
    async def main():
        service, _, _ = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        await service.record_extraction(
            make_material("mat-2"), make_extraction("mat-2", node_id="node-2")
        )
        outcome = await service.merge_explicit("case-1", ("mat-2",))
        assert outcome.material_ids == ("mat-2",)
        assert outcome.bundle.materials == (make_material("mat-2"),)

        with pytest.raises(LookupError, match="mat-unknown"):
            await service.merge_explicit("case-1", ("mat-1", "mat-unknown"))

    run(main())


def test_foreign_case_materials_can_never_enter_the_accumulation(tmp_path):
    async def main():
        service, ledger, _ = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        # The merger's conservative rule: a foreign node case is rejected
        # without an explicit adopt decision, and the automatic accumulator
        # never adopts — adopting is reserved for the later arbitration
        # capability.  The failed material never reaches the ledger.
        with pytest.raises(ValueError, match="foreign case"):
            await service.record_extraction(
                make_material("mat-2"),
                make_extraction("mat-2", node_id="node-2", node_case_id="case-9"),
            )
        assert [e.material_id for e in ledger.entries("case-1")] == ["mat-1"]
        with pytest.raises(LookupError, match="mat-2"):
            await service.merge_explicit("case-1", ("mat-2",))
        outcome = await service.merge_case("case-1")
        assert outcome is not None
        assert outcome.material_ids == ("mat-1",)

    run(main())


def test_record_extraction_refuses_a_second_case_binding(tmp_path):
    """One material binds one case: re-binding under a different case must be
    rejected explicitly before any row or graph write, never silently added.
    """
    async def main():
        service, ledger, graph = make_service(tmp_path)
        material = make_material("mat-1")
        await service.record_extraction(material, make_extraction("mat-1"))

        other_case = make_case(case_id="case-2")
        with pytest.raises(MaterialCaseConflict) as info:
            await service.record_extraction(
                material, make_extraction("mat-1", case=other_case)
            )
        assert info.value.material_id == "mat-1"
        assert info.value.case_ids == ("case-1",)
        assert info.value.attempted_case == "case-2"

        # No ambiguous second row, no case-2 graph write, binding unchanged.
        assert ledger.entries("case-2") == ()
        assert service.case_ids_for_material("mat-1") == ("case-1",)
        assert service.case_for_material("mat-1") == "case-1"
        assert graph.calls[-1][0].case_id == "case-1"
        assert len([call for call in graph.calls if call[0].case_id == "case-2"]) == 0

        # Re-recording under the SAME case stays an allowed idempotent update.
        outcome = await service.record_extraction(material, make_extraction("mat-1"))
        assert outcome.material_ids == ("mat-1",)
        assert service.case_ids_for_material("mat-1") == ("case-1",)

    run(main())


def test_record_extraction_requires_a_case(tmp_path):
    async def main():
        service, ledger, _ = make_service(tmp_path)
        with pytest.raises(ValueError, match="case"):
            await service.record_extraction(
                make_material("mat-1"), ExtractionResult()
            )
        assert ledger.entries("case-1") == ()

    run(main())


def test_material_scoped_candidates_accumulate_without_graph_and_bind_explicitly(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        graph.calls.clear()

        candidate = make_extraction("mat-2", node_id="secondary-node")
        caseless = ExtractionResult(
            case=None,
            temporal_facts=(
                replace(
                    candidate.temporal_facts[0],
                    evidence_role="cited_prior_research",
                    cited_source_ref="Smith et al. (2020)",
                ),
            ),
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "missing_case_context",
                    "validated candidates await explicit case binding",
                    source_ids=("mat-2",),
                ),
            ),
            material_role="review",
        )
        pending = await service.record_material_extraction(
            make_material("mat-2"), caseless
        )

        assert pending.status == "awaiting_case_binding"
        assert pending.extraction.accumulation_status == "awaiting_case_binding"
        assert graph.calls == []
        assert ledger.material_entry("mat-2") == pending

        outcome = await service.bind_material_to_case("mat-2", "case-1")

        assert outcome.case_id == "case-1"
        assert outcome.material_ids == ("mat-1", "mat-2")
        assert ledger.material_entry("mat-2") is None
        bound = ledger.entries("case-1")[1].extraction
        assert bound.case is not None and bound.case.case_id == "case-1"
        assert bound.accumulation_status == "case_bound"
        assert bound.temporal_facts == caseless.temporal_facts
        assert all(
            gap.gap_type != "missing_case_context"
            for gap in bound.evidence_gaps
        )
        assert len(graph.calls) == 1

    run(main())


def test_bind_material_requires_an_existing_explicit_case_and_revalidates_evidence(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        candidate = make_extraction("mat-2")
        caseless = ExtractionResult(
            case=None,
            temporal_facts=(
                replace(
                    candidate.temporal_facts[0],
                    evidence_role="cited_prior_research",
                    cited_source_ref="Smith et al. (2020)",
                ),
            ),
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "missing_case_context", "awaiting case", source_ids=("mat-2",)
                ),
            ),
        )
        await service.record_material_extraction(make_material("mat-2"), caseless)

        with pytest.raises(LookupError, match="existing case"):
            await service.bind_material_to_case("mat-2", "case-unknown")
        assert ledger.material_entry("mat-2") is not None
        assert graph.calls == []

    run(main())


def test_bind_rejects_tampered_quote_and_keeps_pending_material(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        graph.calls.clear()

        candidate = make_extraction("mat-2").temporal_facts[0]
        bad_locator = replace(
            candidate.evidence[0], quote="This quote is absent from the material."
        )
        tampered = ExtractionResult(
            case=None,
            temporal_facts=(
                replace(
                    candidate,
                    evidence=(bad_locator,),
                    evidence_role="cited_prior_research",
                    cited_source_ref="Smith et al. (2020)",
                ),
            ),
            evidence_gaps=(
                ExtractionEvidenceGap(
                    "missing_case_context", "awaiting case", source_ids=("mat-2",)
                ),
            ),
        )
        ledger.record_material(make_material("mat-2"), tampered)

        with pytest.raises(ValueError, match="not present verbatim"):
            await service.bind_material_to_case("mat-2", "case-1")
        assert ledger.material_entry("mat-2") is not None
        assert graph.calls == []

    run(main())


def test_bind_graph_failure_rolls_back_case_row_and_keeps_pending_material(tmp_path):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        candidate = make_extraction("mat-2").temporal_facts[0]
        caseless = ExtractionResult(
            case=None,
            temporal_facts=(
                replace(
                    candidate,
                    evidence_role="cited_prior_research",
                    cited_source_ref="Smith et al. (2020)",
                ),
            ),
        )
        await service.record_material_extraction(make_material("mat-2"), caseless)
        graph.exc = RuntimeError("graph unavailable")

        with pytest.raises(RuntimeError, match="graph unavailable"):
            await service.bind_material_to_case("mat-2", "case-1")

        assert ledger.material_entry("mat-2") is not None
        assert [entry.material_id for entry in ledger.entries("case-1")] == ["mat-1"]

    run(main())


@pytest.mark.parametrize("role", [None, "context_only", "publication_event"])
def test_material_accumulation_rejects_non_substantive_candidates(tmp_path, role):
    async def main():
        service, ledger, graph = make_service(tmp_path)
        candidate = replace(
            make_extraction("mat-2").temporal_facts[0], evidence_role=role
        )
        caseless = ExtractionResult(case=None, temporal_facts=(candidate,))
        with pytest.raises(ValueError, match="evidence_role|publication-only"):
            await service.record_material_extraction(
                make_material("mat-2"), caseless
            )
        assert ledger.material_entry("mat-2") is None
        assert graph.calls == []

    run(main())


def test_case_for_material_and_constructor_validation(tmp_path):
    async def main():
        ledger = CaseExtractionLedger(
            PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)
        )
        graph = DedupeGraph()
        service = CaseService(
            ledger=ledger, merger=CaseBundleMerger(), graph_service=graph
        )
        await service.record_extraction(
            make_material("mat-1"), make_extraction("mat-1")
        )
        assert service.case_for_material("mat-1") == "case-1"
        assert service.case_for_material("mat-none") is None

        with pytest.raises(ValueError):
            CaseService(ledger=None, merger=CaseBundleMerger(), graph_service=graph)
        with pytest.raises(TypeError):
            CaseService(
                ledger=ledger, merger=CaseBundleMerger(), graph_service=object()
            )

    run(main())
