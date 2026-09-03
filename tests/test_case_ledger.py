"""Focused tests for the persistent case-extraction ledger (module: cases.ledger)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.cases.ledger import (
    CaseExtractionLedger,
    CaseLedgerEntry,
    MaterialCaseConflict,
    extraction_from_json,
    extraction_to_json,
    material_from_json,
    material_to_json,
)
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


PUBLISHED = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

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
    happened_at=PUBLISHED,
    summary="The revised policy was published.",
    source_ids=("mat-1",),
    claim_ids=("claim-1",),
    valid_at=PUBLISHED,
    observed_at=PUBLISHED,
    evidence=(
        EvidenceLocator(
            source_id="mat-1",
            corpus_path="corpus/2026-08/example/doc-mat-1.md",
            paragraph=1,
            quote="The revised policy was published.",
        ),
    ),
    provenance_type="explicit",
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
    evidence=NODE.evidence,
)
CLAIM = Claim(
    claim_id="claim-1",
    actor="Agency",
    proposition="The revision improves clarity.",
    stance="support",
    stated_at=PUBLISHED,
    based_on=("mat-1",),
    evidence=NODE.evidence,
    observed_at=PUBLISHED,
    provenance_type="explicit",
    confidence=0.9,
    claim_type="interpretation",
)
CONFLICT = ExtractionConflict(
    conflict_id="conflict-1",
    subject="Agency",
    predicate="published",
    alternatives=("Revised policy", "Draft policy"),
    source_ids=("mat-1",),
    evidence=NODE.evidence,
)
GAP = ExtractionEvidenceGap(
    "evidence_location_failed",
    "quote was not found verbatim",
    "node",
    "node-9",
    ("mat-1",),
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
        "raw_path": "raw/mat-1.md",
        "access_level": "fulltext",
    }
    values.update(overrides)
    return Material(**values)


def make_extraction(case: EvolutionCase | None = CASE) -> ExtractionResult:
    return ExtractionResult(
        case=case,
        nodes=(NODE,) if case is not None else (),
        temporal_facts=(FACT,) if case is not None else (),
        claims=(CLAIM,) if case is not None else (),
        warnings=("model flagged low confidence",),
        conflicts=(CONFLICT,),
        evidence_gaps=(GAP,),
    )


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


# ------------------------------------------------------------------ codecs


def test_extraction_json_roundtrip_preserves_every_field():
    extraction = replace(
        make_extraction(),
        nodes=(replace(NODE, evidence_role="publication_event"),),
        temporal_facts=(
            replace(
                FACT,
                evidence_role="cited_prior_research",
                cited_source_ref="Smith et al. (2020)",
            ),
        ),
        claims=(replace(CLAIM, evidence_role="current_synthesis"),),
        conflicts=(
            replace(
                CONFLICT,
                evidence_role="cited_prior_research",
                cited_source_ref="Smith et al. (2020)",
            ),
        ),
        material_role="review",
    )
    decoded = extraction_from_json(extraction_to_json(extraction))
    assert decoded == extraction
    assert decoded.nodes[0].evidence[0].corpus_path == NODE.evidence[0].corpus_path
    assert decoded.conflicts == extraction.conflicts
    assert decoded.conflicts[0].cited_source_ref == "Smith et al. (2020)"
    assert decoded.evidence_gaps == (GAP,)
    assert decoded.temporal_facts[0].evidence_role == "cited_prior_research"
    assert decoded.temporal_facts[0].cited_source_ref == "Smith et al. (2020)"
    assert decoded.claims[0].evidence_role == "current_synthesis"


def test_old_extraction_json_without_evidence_roles_stays_compatible():
    encoded = json.loads(extraction_to_json(make_extraction()))
    encoded.pop("accumulation_status")
    for collection in ("nodes", "temporal_facts", "claims", "conflicts", "relations"):
        for candidate in encoded[collection]:
            candidate.pop("evidence_role")
            candidate.pop("cited_source_ref", None)

    decoded = extraction_from_json(json.dumps(encoded))

    assert decoded == make_extraction()
    assert decoded.nodes[0].evidence_role is None


def test_extraction_json_roundtrip_preserves_a_caseless_result():
    extraction = ExtractionResult(
        nodes=(replace(NODE, evidence_role="cited_prior_research"),),
        temporal_facts=(
            replace(
                FACT,
                evidence_role="cited_prior_research",
                cited_source_ref="Smith et al. (2020)",
            ),
        ),
        warnings=("no LLM router configured; structured extraction skipped",),
        evidence_gaps=(
            ExtractionEvidenceGap(
                "missing_case_context",
                "validated candidates were retained but have no case context",
                source_ids=("mat-1",),
            ),
        ),
        material_role="review",
    )
    decoded = extraction_from_json(extraction_to_json(extraction))
    assert decoded == extraction
    assert decoded.case is None
    assert decoded.nodes[0].evidence_role == "cited_prior_research"
    assert decoded.temporal_facts[0].cited_source_ref == "Smith et al. (2020)"


def test_material_scoped_result_and_ledger_state_are_explicit_and_durable(tmp_path):
    caseless = make_extraction()
    caseless = ExtractionResult(
        case=None,
        temporal_facts=caseless.temporal_facts,
        evidence_gaps=(
            ExtractionEvidenceGap(
                "missing_case_context",
                "validated candidates were retained but have no case context",
                source_ids=("mat-1",),
            ),
        ),
    )
    assert caseless.accumulation_status == "awaiting_case_binding"

    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        entry = ledger.record_material(make_material(), caseless)
        assert entry.status == "awaiting_case_binding"
        assert entry.material_id == "mat-1"
        assert entry.extraction == caseless
        assert ledger.material_entries() == (entry,)
    finally:
        ledger.close()

    reopened = CaseExtractionLedger(make_paths(tmp_path))
    try:
        assert reopened.material_entry("mat-1") == entry
    finally:
        reopened.close()


def test_material_json_roundtrip_preserves_every_field():
    material = make_material(
        authors=("A. Author",),
        doi="10.1000/example",
        container_title="Journal of Tests",
        url="https://example.test/doc",
    )
    assert material_from_json(material_to_json(material)) == material


def test_codecs_reject_invalid_payloads():
    with pytest.raises(ValueError):
        extraction_from_json("not json")
    with pytest.raises(ValueError):
        extraction_from_json("{}")
    with pytest.raises(TypeError):
        material_from_json('{"id": []}')


# ------------------------------------------------------------------ ledger


def test_record_new_entry_returns_none_and_entries_decode_in_order(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        first = ledger.record("case-1", make_material("mat-1"), make_extraction())
        second = ledger.record("case-1", make_material("mat-2"), make_extraction())

        assert first is None
        assert second is None
        entries = ledger.entries("case-1")
        assert [entry.material_id for entry in entries] == ["mat-1", "mat-2"]
        assert all(isinstance(entry, CaseLedgerEntry) for entry in entries)
        assert entries[0].material == make_material("mat-1")
        assert entries[0].extraction == make_extraction()
        assert entries[0].case_id == "case-1"
        assert entries[0].recorded_at.tzinfo is not None
    finally:
        ledger.close()


def test_record_replace_returns_previous_payload_for_rollback(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        material = make_material("mat-1")
        previous = ledger.record("case-1", material, make_extraction())
        assert previous is None

        updated = make_extraction()
        previous = ledger.record("case-1", material, updated)
        assert previous is not None
        previous_material_json, previous_extraction_json = previous
        assert material_from_json(previous_material_json) == material
        assert extraction_from_json(previous_extraction_json) == make_extraction()

        assert [e.material_id for e in ledger.entries("case-1")] == ["mat-1"]
        assert ledger.entries("case-1")[0].extraction == updated
    finally:
        ledger.close()


def test_remove_and_record_raw_restore_previous_state(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        material = make_material("mat-1")
        previous = ledger.record("case-1", material, make_extraction())
        assert previous is None

        assert ledger.remove("case-1", "mat-1") is True
        assert ledger.remove("case-1", "mat-1") is False
        assert ledger.entries("case-1") == ()

        ledger.record_raw(
            "case-1",
            "mat-1",
            material_to_json(material),
            extraction_to_json(make_extraction()),
        )
        assert [e.material_id for e in ledger.entries("case-1")] == ["mat-1"]
    finally:
        ledger.close()


def test_case_for_material_reverse_lookup(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        case_2 = EvolutionCase(
            case_id="case-2",
            case_type=CASE.case_type,
            canonical_name=CASE.canonical_name,
            start_at=CASE.start_at,
            status=CASE.status,
        )
        ledger.record("case-1", make_material("mat-1"), make_extraction())
        ledger.record("case-2", make_material("mat-2"), make_extraction(case_2))

        assert ledger.case_for_material("mat-1") == "case-1"
        assert ledger.case_for_material("mat-2") == "case-2"
        assert ledger.case_for_material("mat-unknown") is None
    finally:
        ledger.close()


def test_multi_case_rows_stay_readable_and_conflicts_are_typed(tmp_path):
    """Legacy ledgers may bind one material under several cases.

    Every row must stay readable and reportable; the ambiguous reverse
    lookup must raise an explicit, typed conflict instead of a bare,
    unexpected ValueError.
    """
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        case_2 = EvolutionCase(
            case_id="case-2",
            case_type=CASE.case_type,
            canonical_name=CASE.canonical_name,
            start_at=CASE.start_at,
            status=CASE.status,
        )
        ledger.record("case-1", make_material("mat-1"), make_extraction())
        ledger.record("case-2", make_material("mat-1"), make_extraction(case_2))

        assert ledger.case_ids_for_material("mat-1") == ("case-1", "case-2")
        assert ledger.case_ids_for_material("mat-unknown") == ()
        with pytest.raises(MaterialCaseConflict) as info:
            ledger.case_for_material("mat-1")
        assert info.value.material_id == "mat-1"
        assert info.value.case_ids == ("case-1", "case-2")
        assert "'case-1'" in str(info.value) and "'case-2'" in str(info.value)
    finally:
        ledger.close()


def test_entries_survive_close_and_reopen(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    ledger.record("case-1", make_material("mat-1"), make_extraction())
    ledger.record("case-1", make_material("mat-2"), make_extraction())
    ledger.close()

    reopened = CaseExtractionLedger(make_paths(tmp_path))
    try:
        entries = reopened.entries("case-1")
        assert [entry.material_id for entry in entries] == ["mat-1", "mat-2"]
        assert reopened.case_for_material("mat-2") == "case-1"
        assert reopened.entries("case-unknown") == ()
    finally:
        reopened.close()


def test_record_validates_row_integrity(tmp_path):
    ledger = CaseExtractionLedger(make_paths(tmp_path))
    try:
        with pytest.raises(ValueError):
            ledger.record("  ", make_material(), make_extraction())
        with pytest.raises(TypeError):
            ledger.record("case-1", "not-a-material", make_extraction())
        with pytest.raises(TypeError):
            ledger.record("case-1", make_material(), "not-an-extraction")
        # A ledger row must always bind an extraction that declares this case.
        with pytest.raises(ValueError):
            ledger.record("case-9", make_material(), make_extraction())
        assert ledger.entries("case-1") == ()
    finally:
        ledger.close()
