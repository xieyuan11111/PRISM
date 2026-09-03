"""TDD contracts for explicit multi-material case assembly."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.cases import CaseBundleMerger, CaseEvidence, MergedCaseBundle
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
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


def test_merger_preserves_temporal_provenance_and_evidence_fields():
    """The merger must not erase extraction times or evidence locators.

    Dropping ``observed_at`` here would let a node that was only reported by a
    later material leak into historical states that predate that material.
    """
    source = material("mat-a")
    locator = EvidenceLocator("mat-a", "corpus/mat-a.md", paragraph=2, quote="text")
    extracted_node = EvolutionNode(
        "node-a",
        CASE_ID,
        "publication",
        T0,
        "Published.",
        ("mat-a",),
        (),
        T0,
        T1,
        (locator,),
        "The ministry revised the plan.",
        "reliable_transcription",
        evidence_role="cited_prior_research",
    )
    extracted_claim = Claim(
        "claim-a",
        "Agency",
        "The mechanism may stabilize the market.",
        "conditional",
        T1,
        ("mat-a",),
        None,
        (locator,),
    )
    authored_case = EvolutionCase(
        CASE_ID,
        "policy",
        "Housing provident fund",
        T1,
        "implemented",
        status_at=T2,
        status_observed_at=T2,
    )

    merged = CaseBundleMerger().merge(
        authored_case,
        [
            CaseEvidence(
                source,
                ExtractionResult(
                    case=case(),
                    nodes=(extracted_node,),
                    claims=(extracted_claim,),
                ),
            )
        ],
    )

    (merged_node,) = merged.nodes
    assert merged_node.valid_at == T0
    assert merged_node.observed_at == T1
    assert merged_node.evidence == (locator,)
    assert merged_node.change_reason == "The ministry revised the plan."
    assert merged_node.provenance_type == "reliable_transcription"
    assert merged_node.evidence_role == "cited_prior_research"
    (merged_claim,) = merged.claims
    assert merged_claim.evidence == (locator,)
    assert merged.case.status_at == T2
    assert merged.case.status_observed_at == T2


def test_merger_preserves_claim_layering_fields():
    """The merger must not erase claim_type/provenance_type/confidence.

    v0 extraction records whether a claim is an interpretation, a value
    judgment, or a prediction, plus its provenance and confidence.  The
    merger re-scopes claim ids but every other recorded field must survive
    so the fact/interpretation/prediction layering stays intact.
    """
    source = material("mat-a")
    locator = EvidenceLocator("mat-a", "corpus/mat-a.md", paragraph=1, quote="text")
    extracted_claim = Claim(
        "claim-a",
        "Analyst",
        "The mechanism may expand next year.",
        "uncertain",
        T1,
        ("mat-a",),
        None,
        (locator,),
        None,
        "reliable_transcription",
        0.7,
        "prediction",
        evidence_role="current_synthesis",
    )

    merged = CaseBundleMerger().merge(
        case(),
        [
            CaseEvidence(
                source,
                ExtractionResult(claims=(extracted_claim,)),
            )
        ],
    )

    (merged_claim,) = merged.claims
    assert merged_claim.claim_id == "mat-a::claim-a"
    assert merged_claim.claim_type == "prediction"
    assert merged_claim.provenance_type == "reliable_transcription"
    assert merged_claim.confidence == 0.7
    assert merged_claim.evidence == (locator,)
    assert merged_claim.observed_at is None
    assert merged_claim.evidence_role == "current_synthesis"
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
