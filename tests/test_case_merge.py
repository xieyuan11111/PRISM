"""TDD contracts for explicit multi-material case assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.cases import CaseBundleMerger, CaseEvidence, MergedCaseBundle
from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.extraction import ExtractionResult

UTC = timezone.utc
T0 = datetime(2024, 12, 31, tzinfo=UTC)
T1 = datetime(2026, 8, 18, tzinfo=UTC)
T2 = datetime(2026, 9, 2, tzinfo=UTC)
CASE_ID = "case-hpf"


def material(source_id: str, *, tags=(CASE_ID,)) -> Material:
    return Material(
        id=source_id,
        title=source_id,
        source="example.gov",
        published_at=T1,
        fetched_at=T2,
        type="policy",
        content="evidence",
        original_format="md",
        case_tags=tags,
    )


def case() -> EvolutionCase:
    return EvolutionCase(CASE_ID, "policy", "Housing provident fund", T1, "active")


def node(node_id: str, source_id: str, *, case_id=CASE_ID) -> EvolutionNode:
    return EvolutionNode(node_id, case_id, "publication", T1, node_id, (source_id,))


def fact(source_id: str, predicate: str) -> TemporalFact:
    return TemporalFact(
        "housing fund", predicate, "value", T0, None, T1, (source_id,), 0.9, "official"
    )


def claim(claim_id: str, source_id: str, proposition: str) -> Claim:
    return Claim(claim_id, "Agency", proposition, "support", T1, (source_id,))


def test_merger_returns_graph_ready_bundle_and_deduplicates_exact_records():
    source_a = material("mat-a")
    source_b = material("mat-b")
    extraction_a = ExtractionResult(
        case=case(),
        nodes=(node("node-a", "mat-a"),),
        temporal_facts=(fact("mat-a", "balance"),),
        claims=(claim("claim-a", "mat-a", "The policy exists."),),
    )
    extraction_b = ExtractionResult(
        case=None,
        temporal_facts=(fact("mat-b", "balance"),),
    )

    merged = CaseBundleMerger().merge(
        case(),
        [CaseEvidence(source_a, extraction_a), CaseEvidence(source_b, extraction_b)],
    )

    assert isinstance(merged, MergedCaseBundle)
    assert merged.case.case_id == CASE_ID
    assert [item.id for item in merged.materials] == ["mat-a", "mat-b"]
    assert [item.id for item in merged.nodes] == ["mat-a::node-a"]
    assert len(merged.temporal_facts) == 2
    assert [item.claim_id for item in merged.claims] == ["mat-a::claim-a"]
    assert merged.case.node_ids == ("mat-a::node-a",)
    assert merged.source_ids == ("mat-a", "mat-b")


def test_merger_remaps_explicitly_adopted_foreign_case_nodes():
    source = material("mat-a", tags=())
    foreign_case = EvolutionCase("local-case", "policy", "Local", T1, "active")
    extraction = ExtractionResult(
        case=foreign_case,
        nodes=(node("node-a", "mat-a", case_id="local-case"),),
    )

    merged = CaseBundleMerger().merge(
        case(), [CaseEvidence(source, extraction, adopt_case=True)]
    )

    assert merged.nodes[0].id == "mat-a::node-a"
    assert merged.nodes[0].case_id == CASE_ID
    assert merged.case.node_ids == ("mat-a::node-a",)


def test_merger_rejects_node_case_without_explicit_adoption():
    source = material("mat-a", tags=())
    extraction = ExtractionResult(
        case=None,
        nodes=(node("node-a", "mat-a", case_id="local-case"),),
    )

    with pytest.raises(ValueError, match="adopt_case"):
        CaseBundleMerger().merge(case(), [CaseEvidence(source, extraction)])
def test_merger_warns_when_existing_case_nodes_are_rebuilt():
    source = material("mat-a")
    existing = EvolutionCase(
        CASE_ID, "policy", "Housing provident fund", T1, "active", ("old-node",)
    )
    merged = CaseBundleMerger().merge(
        existing,
        [CaseEvidence(source, ExtractionResult(nodes=(node("new-node", "mat-a"),)))],
    )

    assert merged.case.node_ids == ("mat-a::new-node",)
    assert any("old-node" in warning for warning in merged.warnings)
def test_merger_rejects_foreign_case_without_explicit_adoption():
    source = material("mat-a", tags=())
    foreign_case = EvolutionCase("local-case", "policy", "Local", T1, "active")
    extraction = ExtractionResult(case=foreign_case, nodes=(node("node-a", "mat-a", case_id="local-case"),))

    with pytest.raises(ValueError, match="adopt_case"):
        CaseBundleMerger().merge(case(), [CaseEvidence(source, extraction)])


def test_node_claim_references_are_scoped_and_resolved_across_materials():
    source_a = material("mat-a")
    source_b = material("mat-b")
    extraction_a = ExtractionResult(
        nodes=(
            EvolutionNode("node-a", CASE_ID, "publication", T1, "node", ("mat-a",), ("claim-x",)),
        )
    )
    extraction_b = ExtractionResult(
        claims=(claim("claim-x", "mat-b", "A claim."),)
    )

    merged = CaseBundleMerger().merge(
        case(),
        [CaseEvidence(source_a, extraction_a), CaseEvidence(source_b, extraction_b)],
    )

    assert merged.nodes[0].claim_ids == ("mat-b::claim-x",)
def test_merger_rejects_conflicting_duplicate_ids_and_unbound_sources():
    source = material("mat-a")
    first = ExtractionResult(nodes=(node("same", "mat-a"),))
    second = ExtractionResult(nodes=(node("same", "mat-a"),))
    # Exact duplicate is safe.
    assert len(CaseBundleMerger().merge(case(), [CaseEvidence(source, first), CaseEvidence(source, second)]).nodes) == 1

    conflicting = ExtractionResult(
        nodes=(EvolutionNode("same", CASE_ID, "revision", T1, "different", ("mat-a",)),)
    )
    with pytest.raises(ValueError, match="node id collision"):
        CaseBundleMerger().merge(case(), [CaseEvidence(source, first), CaseEvidence(source, conflicting)])

    unbound = ExtractionResult(nodes=(node("unbound", "mat-missing"),))
    with pytest.raises(ValueError, match="unknown source"):
        CaseBundleMerger().merge(case(), [CaseEvidence(source, unbound)])
