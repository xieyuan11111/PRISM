from prism.llm import MissingRoleError, RetriesExhaustedError, TaskRole
from dataclasses import replace
from datetime import datetime, timezone
import asyncio
import json
import pytest
from prism.adjudication import (
    AdjudicationBatchFailure,
    AdjudicationDecision,
    AdjudicationLedger,
    AdjudicationService,
)
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


class _Router:
    def __init__(self, text): self.text = text
    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        return type("C", (), {"text": self.text})()


def test_adjudicate_task_role_and_models_exist():
    assert TaskRole.ADJUDICATE.value == "adjudicate"
    from prism.adjudication import AdjudicationService, AuditRecord

    assert AdjudicationService is not None
    assert AuditRecord.__slots__


def _fixture():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    material = Material("m1", "t", "s", now, now, "news", "A quote")
    evidence = EvidenceLocator("m1", "corpus/a.md", paragraph=1, quote="A quote")
    node = EvolutionNode("n1", "case-x", "publication", now, "A quote", ("m1",), evidence=(evidence,), valid_at=now, observed_at=now, provenance_type="source_explicit", evidence_role="primary_observation")
    return material, ExtractionResult(nodes=(node,))


def test_rejected_is_audited_and_original_is_unchanged(tmp_path):
    material, extraction = _fixture()
    text = '{"decisions":[{"candidate_kind":"node","candidate_id":"n1","decision":"rejected","reason":"unsupported"}]}'
    service = AdjudicationService(_Router(text), ledger=AdjudicationLedger(tmp_path / "index.db"))
    result = asyncio.run(service.adjudicate(material, extraction))
    assert extraction.nodes
    assert result.extraction.nodes == ()
    assert result.audits[0].decision.value == "rejected"
    assert len(service.history("m1")) == 1


def test_adjudication_rejects_duplicate_or_unknown_json_fields():
    material, extraction = _fixture()
    service = AdjudicationService(_Router('{"decisions":[],"x":1}'))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        asyncio.run(service.adjudicate(material, extraction))
    assert caught.value.error_code == "invalid_payload"


def test_revised_candidate_is_revalidated_against_verbatim_evidence():
    material, extraction = _fixture()
    revised = {
        "id": "n1", "case_id": "case-x", "node_type": "revision", "assertion_type": "fact",
        "happened_at": "2026-01-01T00:00:00+00:00", "valid_at": "2026-01-01T00:00:00+00:00",
        "observed_at": "2026-01-01T00:00:00+00:00", "summary": "A quote revised",
        "source_ids": ["m1"], "claim_ids": [], "provenance_type": "source_explicit",
        "evidence_role": "primary_observation", "evidence": [{"source_id":"m1","quote":"A quote","paragraph":1,"page":None}],
    }
    text = json.dumps({"decisions":[{"candidate_kind":"node","candidate_id":"n1","decision":"revised","reason":"clarified","revised_payload":revised}]})
    service = AdjudicationService(_Router(text), extraction_service=ExtractionService(_Router("{}")))
    result = asyncio.run(service.adjudicate(material, extraction))
    assert result.extraction.nodes[0].node_type == "revision"
    service = AdjudicationService(_Router('{"decisions": [{"candidate_kind":"node","candidate_kind":"node","candidate_id":"n1","decision":"accepted","reason":"ok"}]}'))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        asyncio.run(service.adjudicate(material, extraction))
    assert caught.value.error_code == "duplicate_json_key"


# ---------------------------------------------------------------------------
# Review-driven coverage: multi-candidate revisions, dependent-reference
# pruning, awaiting_case_binding semantics, duplicate targets, ledger
# durability/idempotency, redaction, and role fallback.
# ---------------------------------------------------------------------------

CASE_X = EvolutionCase(
    case_id="case-x",
    case_type="policy",
    canonical_name="Case X",
    start_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
    status="active",
    node_ids=("n1", "n2"),
)


def _material(content: str = "A quote\n\nSecond quote") -> Material:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Material("m1", "t", "s", now, now, "news", content)


def _locator(paragraph: int, quote: str) -> EvidenceLocator:
    return EvidenceLocator("m1", "corpus/a.md", paragraph=paragraph, quote=quote)


def _node(node_id: str, paragraph: int, quote: str, *, claim_ids=(), case: str = "case-x") -> EvolutionNode:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return EvolutionNode(
        node_id, case, "publication", now, quote, ("m1",),
        claim_ids=claim_ids, evidence=(_locator(paragraph, quote),),
        valid_at=now, observed_at=now,
        provenance_type="source_explicit", evidence_role="primary_observation",
    )


def _claim(claim_id: str, quote: str = "A quote") -> Claim:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Claim(
        claim_id, "actor", "proposition", "support", now,
        based_on=("m1",), evidence=(_locator(1, quote),),
        observed_at=now, provenance_type="source_explicit",
        evidence_role="primary_observation",
    )


def _run(service, material, extraction, **kwargs):
    return asyncio.run(service.adjudicate(material, extraction, **kwargs))


def _decision(kind: str, ident: str, decision: str, reason: str = "reviewed", **extra) -> dict:
    item = {"candidate_kind": kind, "candidate_id": ident,
            "decision": decision, "reason": reason}
    item.update(extra)
    return item


# --- REVISED must work in realistic multi-candidate extractions -------------

def test_revised_applies_alongside_untouched_candidates():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X,
        nodes=(_node("n1", 1, "A quote"), _node("n2", 2, "Second quote")),
    )
    revised = {
        "id": "n1", "case_id": "case-x", "node_type": "revision",
        "assertion_type": "fact",
        "happened_at": "2026-01-01T00:00:00+00:00",
        "valid_at": "2026-01-01T00:00:00+00:00",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "summary": "A quote revised",
        "source_ids": ["m1"], "claim_ids": [],
        "provenance_type": "source_explicit",
        "evidence_role": "primary_observation",
        "evidence": [{"source_id": "m1", "quote": "A quote", "paragraph": 1, "page": None}],
    }
    text = json.dumps({"decisions": [
        _decision("node", "n1", "revised", "clarified", revised_payload=revised)]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)
    assert len(result.extraction.nodes) == 2
    assert result.extraction.nodes[0].node_type == "revision"
    assert result.extraction.nodes[1].node_type == "publication"
    assert result.extraction.case.node_ids == ("n1", "n2")
    assert result.audits[0].revalidation_outcome == "revalidated"


def test_revised_payload_may_be_a_partial_update_over_the_original():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X,
        nodes=(_node("n1", 1, "A quote"), _node("n2", 2, "Second quote")),
    )
    # Only the changed fields are supplied; identity/evidence/source stay
    # verified from the original candidate.
    text = json.dumps({"decisions": [
        _decision("node", "n1", "revised", "typo",
                  revised_payload={"node_type": "revision",
                                   "summary": "A quote revised"})]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)
    assert result.extraction.nodes[0].node_type == "revision"
    assert result.extraction.nodes[0].summary == "A quote revised"
    assert result.audits[0].revalidation_outcome == "revalidated"


def test_revision_with_fabricated_quote_is_rejected_and_audited():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X, nodes=(_node("n1", 1, "A quote"),),
    )
    revised = {
        "node_type": "revision",
        "evidence": [{"source_id": "m1", "quote": "Not in the material",
                      "paragraph": 1, "page": None}],
    }
    text = json.dumps({"decisions": [
        _decision("node", "n1", "revised", "fix quote", revised_payload=revised)]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)
    # The fabricated quote never enters the result; the failure is audited
    # and the original candidate is untouched.
    assert result.extraction.nodes[0].node_type == "publication"
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")


def test_revision_cannot_drift_from_the_declared_target_case():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X, nodes=(_node("n1", 1, "A quote"),),
    )
    target = EvolutionCase("case-y", "policy", "Case Y",
                           datetime(2025, 12, 1, tzinfo=timezone.utc),
                           "active", node_ids=("n1",))
    text = json.dumps({"decisions": [
        _decision("node", "n1", "revised", "edit",
                  revised_payload={"summary": "changed"})]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction, target_case=target)
    assert result.extraction.nodes[0].summary == "A quote"
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")


def test_revised_claim_or_fact_keeps_other_candidate_types():
    material = _material("A quote\n\nSecond quote")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fact = TemporalFact("Agency", "published", "thing", now, None, now,
                        ("m1",), 0.9, "source_explicit",
                        evidence=(_locator(1, "A quote"),), fact_id="f1",
                        evidence_role="primary_observation")
    extraction = ExtractionResult(case=CASE_X, nodes=(_node("n1", 1, "A quote"),),
                                  temporal_facts=(fact,))
    text = json.dumps({"decisions": [
        _decision("temporal_fact", "f1", "revised", "clarify",
                  revised_payload={"subject": "Agency revised", "object": "thing"})]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)
    assert result.extraction.nodes[0].id == "n1"
    assert result.extraction.temporal_facts[0].subject == "Agency revised"
    assert result.audits[0].revalidation_outcome == "revalidated"


# --- REJECTED must keep the whole result internally consistent --------------

def test_rejected_claim_prunes_dependent_node_references():
    material = _material()
    case = EvolutionCase(
        case_id="case-x", case_type="policy", canonical_name="Case X",
        start_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        status="active", node_ids=("n1",),
    )
    extraction = ExtractionResult(
        case=case,
        nodes=(_node("n1", 1, "A quote", claim_ids=("c1",)),),
        claims=(_claim("c1"),),
    )
    text = json.dumps({"decisions": [_decision("claim", "c1", "rejected", "unsupported")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert result.extraction.claims == ()
    assert result.extraction.nodes[0].claim_ids == ()
    assert result.extraction.case.node_ids == ("n1",)


def test_rejected_node_updates_case_node_ids():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X,
        nodes=(_node("n1", 1, "A quote"), _node("n2", 2, "Second quote")),
    )
    text = json.dumps({"decisions": [_decision("node", "n2", "rejected", "duplicate")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert [n.id for n in result.extraction.nodes] == ["n1"]
    assert result.extraction.case.node_ids == ("n1",)


def test_rejected_conflict_is_removed_and_audited():
    material = _material()
    conflict = ExtractionConflict(
        "cf1", "agency", "status", ("active", "paused"), ("m1",),
        evidence=(_locator(1, "A quote"),),
        valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    extraction = ExtractionResult(case=CASE_X, conflicts=(conflict,))
    text = json.dumps({"decisions": [_decision("conflict", "cf1", "rejected", "resolved")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert result.extraction.conflicts == ()
    assert result.audits[0].decision.value == "rejected"


# --- AWAITING_CASE_BINDING ------------------------------------------------

def test_awaiting_case_binding_on_caseless_material_is_audit_only():
    material = _material()
    extraction = ExtractionResult(case=None, nodes=(_node("n1", 1, "A quote", case="?"),))
    text = json.dumps({"decisions": [
        _decision("node", "n1", "awaiting_case_binding", "needs a case")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert len(result.extraction.nodes) == 1
    assert result.audits[0].revalidation_outcome == "awaiting_case_binding"


def test_awaiting_case_binding_excludes_case_bound_candidate_from_graph_write():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X,
        nodes=(_node("n1", 1, "A quote"), _node("n2", 2, "Second quote")),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n1", "awaiting_case_binding", "not this case")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert [n.id for n in result.extraction.nodes] == ["n2"]
    assert result.extraction.case.node_ids == ("n2",)
    gaps = [g for g in result.extraction.evidence_gaps
            if g.gap_type == "awaiting_case_binding"]
    assert len(gaps) == 1 and gaps[0].item_kind == "node" and gaps[0].item_id == "n1"
    assert result.audits[0].revalidation_outcome == "excluded_pending_binding"


def test_awaiting_case_binding_on_every_candidate_drops_the_case_anchor():
    material = _material()
    extraction = ExtractionResult(
        case=CASE_X, nodes=(_node("n1", 1, "A quote"),),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n1", "awaiting_case_binding", "not this case")]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert result.extraction.case is None
    assert result.extraction.nodes == ()


def test_accepted_and_preserve_conflict_are_audited_without_changes():
    material = _material()
    conflict = ExtractionConflict(
        "cf1", "agency", "status", ("active", "paused"), ("m1",),
        evidence=(_locator(1, "A quote"),),
        valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    extraction = ExtractionResult(case=CASE_X, nodes=(_node("n1", 1, "A quote"),),
                                  conflicts=(conflict,))
    text = json.dumps({"decisions": [
        _decision("node", "n1", "accepted", "solid"),
        _decision("conflict", "cf1", "preserve_conflict", "unresolved"),
    ]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)
    assert len(result.extraction.nodes) == 1
    assert len(result.extraction.conflicts) == 1
    assert [a.decision.value for a in result.audits] == ["accepted", "preserve_conflict"]
    assert result.applied is True


# --- Adversarial / strictness ---------------------------------------------

def test_duplicate_decision_targets_raise_with_only_a_batch_audit(tmp_path):
    material, extraction = _fixture()
    text = json.dumps({"decisions": [
        _decision("node", "n1", "rejected", "first"),
        _decision("node", "n1", "accepted", "second"),
    ]})
    db_path = tmp_path / "ledger.db"
    service = AdjudicationService(_Router(text), ledger=AdjudicationLedger(db_path))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "duplicate_decision_target"
    assert extraction.nodes  # original untouched
    # The failed batch never leaves a candidate-level partial trail: exactly
    # one durable batch-level failure record is written.
    records = service.history()
    assert len(records) == 1
    assert records[0].candidate_kind == "batch"
    assert records[0].revalidation_outcome == "adjudication_failed"


def test_decision_targeting_a_gapped_candidate_is_rejected():
    material, extraction = _fixture()
    gapped = ExtractionResult(
        case=None, nodes=(),
        evidence_gaps=(ExtractionEvidenceGap(
            "evidence_location_failed", "not found", "node", "n9",
            source_ids=("m1",)),),
    )
    text = json.dumps({"decisions": [_decision("node", "n9", "rejected", "no")]})
    service = AdjudicationService(_Router(text))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, gapped)
    assert caught.value.error_code == "unknown_candidate_identity"


def test_nan_and_infinity_constants_are_rejected():
    material, extraction = _fixture()
    service = AdjudicationService(_Router('{"decisions": [NaN]}'))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "invalid_json_constant"
    service = AdjudicationService(_Router('{"decisions": [{"candidate_kind":"node","candidate_id":"n1","decision":"accepted","reason":"x","revised_payload":{"confidence": Infinity}}]}'))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "invalid_json_constant"


def test_revision_with_invented_internal_node_field_is_rejected():
    material = _material()
    extraction = ExtractionResult(case=CASE_X, nodes=(_node("n1", 1, "A quote"),))
    # change_reason is not part of the strict extraction schema: an invented
    # value must fail, never be silently written into the node.
    text = json.dumps({"decisions": [
        _decision("node", "n1", "revised", "edit",
                  revised_payload={"change_reason": "made up by the model"})]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)
    assert result.extraction.nodes[0].change_reason is None
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")


class _MissingRoleRouter:
    async def complete(self, role, prompt):
        from prism.llm import MissingRoleError
        raise MissingRoleError("no route")


def test_missing_role_raises_a_structured_batch_failure():
    material, extraction = _fixture()
    service = AdjudicationService(_MissingRoleRouter())
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "missing_role"
    assert extraction.nodes  # original untouched


# --- Ledger durability, idempotency and redaction --------------------------

class _FixedClock:
    def __init__(self, value):
        self.value = value
    def __call__(self):
        return self.value


def _service_with_ledger(text: str, db_path, *, clock_value=None, extraction_service=None):
    now = clock_value or datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    service = AdjudicationService(
        _Router(text), ledger=AdjudicationLedger(db_path),
        extraction_service=extraction_service,
        clock=_FixedClock(now),
    )
    return service


def test_ledger_deduplicates_identical_decisions_across_restart(tmp_path):
    material, extraction = _fixture()
    db_path = tmp_path / "ledger.db"
    text = json.dumps({"decisions": [_decision("node", "n1", "rejected", "same")]})
    first = _service_with_ledger(text, db_path)
    _run(first, material, extraction)
    # A restart reuses the same durable file and re-adjudicates the same
    # material; identical content decisions collapse into the one row.
    second = _service_with_ledger(text, db_path)
    result = _run(second, material, extraction)
    assert len(result.audits) == 1
    assert len(second.history("m1")) == 1
    reopened = AdjudicationLedger(db_path)
    assert len(reopened.entries("m1")) == 1


def test_ledger_keeps_distinct_decisions_and_filters_by_material(tmp_path):
    material, extraction = _fixture()
    db_path = tmp_path / "ledger.db"
    reject = json.dumps({"decisions": [_decision("node", "n1", "rejected", "reason one")]})
    accept = json.dumps({"decisions": [_decision("node", "n1", "accepted", "reason two")]})
    _run(_service_with_ledger(reject, db_path), material, extraction)
    second = _service_with_ledger(accept, db_path)
    result = _run(second, material, extraction)
    assert len(result.audits) == 1
    entries = second.history("m1")
    assert len(entries) == 2
    assert {e.decision.value for e in entries} == {"rejected", "accepted"}
    assert second.history("other-material") == ()


def test_audit_record_is_frozen_timezone_aware_and_does_not_leak_material_body(tmp_path):
    secret = "SUPER-SECRET-93421"
    content = f"A quote\n\n{secret}"
    material = _material(content)
    extraction = ExtractionResult(
        case=None,
        nodes=(EvolutionNode(
            "n1", "?",
            "publication", datetime(2026, 1, 1, tzinfo=timezone.utc),
            "A quote", ("m1",),
            evidence=(_locator(1, "A quote"),),
            valid_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            provenance_type="source_explicit", evidence_role="primary_observation",
        ),),
    )
    db_path = tmp_path / "ledger.db"
    text = json.dumps({"decisions": [_decision("node", "n1", "rejected", "no")]})
    service = _service_with_ledger(text, db_path)
    result = _run(service, material, extraction)
    record = result.audits[0]
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        record.decision = "accepted"  # type: ignore[misc]
    assert record.decided_at.tzinfo is not None
    raw = db_path.read_bytes()
    assert secret.encode("utf-8") not in raw
    with pytest.raises(ValueError):
        from prism.adjudication.models import AuditRecord
        AuditRecord(
            "d1", "m1", "node", "n1", "h", {"summary": "x"}, (),
            AdjudicationDecision.ACCEPTED, "ok", None, "adjudicate",
            datetime(2026, 1, 1), "accepted",
        )


# ---------------------------------------------------------------------------
# Gap-candidate adjudication: the deterministic layer demotes candidates it
# cannot verify; payload-bearing evidence gaps carry the original candidate
# snapshot so the LLM adjudicator can review/repair them.  A repair is
# revived ONLY through strict revalidation of the saved payload; accepted
# alone never resurrects, context_only never revives, and rejected/preserved
# decisions never mutate the graph unless a legal payload revalidates.
# ---------------------------------------------------------------------------


def _node_payload(
    node_id: str,
    quote: str,
    paragraph: int,
    *,
    happened_at: str = "2026-01-01T00:00:00+00:00",
    case_id: str = "case-x",
    summary: str = "A node.",
) -> dict:
    return {
        "id": node_id,
        "case_id": case_id,
        "node_type": "publication",
        "assertion_type": "fact",
        "happened_at": happened_at,
        "valid_at": happened_at,
        "observed_at": happened_at,
        "summary": summary,
        "source_ids": ["m1"],
        "claim_ids": [],
        "provenance_type": "source_explicit",
        "evidence_role": "primary_observation",
        "evidence": [
            {"source_id": "m1", "quote": quote, "paragraph": paragraph, "page": None}
        ],
    }


def _payload_gap(
    kind: str,
    ident: str,
    payload: dict,
    *,
    gap_type: str = "candidate_validation_failed",
) -> ExtractionEvidenceGap:
    return ExtractionEvidenceGap(gap_type, f"{kind} {ident} not graph-ready", kind, ident, ("m1",), payload)


def _gap_extraction() -> tuple[Material, ExtractionResult]:
    """One verified node n1 plus payload gaps for n2 (bad time) and n3 (bad
    quote); the case declares only n1 so a revived n2 must re-enter node_ids."""
    material = _material("A quote\n\nSecond quote")
    case = replace(CASE_X, node_ids=("n1",))
    n1 = _node("n1", 1, "A quote")
    n2 = _node_payload(
        "n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00"
    )
    n3 = _node_payload(
        "n3", "Nothing like this appears", 2, summary="Never binds."
    )
    n3["evidence"][0]["paragraph"] = 1
    extraction = ExtractionResult(
        case=case,
        nodes=(n1,),
        evidence_gaps=(
            _payload_gap("node", "n2", n2),
            _payload_gap(
                "node", "n3", n3, gap_type="evidence_location_failed"
            ),
        ),
    )
    return material, extraction


_TIME_FIX = {
    "happened_at": "2025-12-15T00:00:00+00:00",
    "valid_at": "2025-12-15T00:00:00+00:00",
    "observed_at": "2025-12-15T00:00:00+00:00",
}


def test_revised_gap_candidate_is_strictly_revalidated_and_enters_the_graph():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "time fixed", revised_payload=_TIME_FIX),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1", "n2"]
    assert result.extraction.case.node_ids == ("n1", "n2")
    assert result.extraction.nodes[1].happened_at == datetime(
        2025, 12, 15, tzinfo=timezone.utc
    )
    assert result.extraction.nodes[1].evidence[0].quote == "Second quote"
    assert result.audits[0].revalidation_outcome == "revalidated"
    assert result.audits[0].original_payload["id"] == "n2"
    # The resolved gap is gone; the unrelated payload gap survives untouched.
    assert all(gap.item_id != "n2" for gap in result.extraction.evidence_gaps)
    assert any(
        gap.item_id == "n3" and gap.candidate_payload is not None
        for gap in result.extraction.evidence_gaps
    )
    # The original extraction is immutable and still carries both gaps.
    assert [node.id for node in extraction.nodes] == ["n1"]
    assert extraction.case.node_ids == ("n1",)
    assert {gap.item_id for gap in extraction.evidence_gaps} == {"n2", "n3"}


def test_accepted_without_revision_cannot_revive_a_gap_candidate():
    material = _material("A quote\n\nSecond quote")
    n2 = _node_payload("n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("node", "n2", n2),),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n2", "accepted", "looks right"),
    ]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)

    assert result.extraction.nodes == ()
    assert result.audits[0].decision.value == "accepted"
    assert result.audits[0].revalidation_outcome == "accepted_gap_retained"
    gap = result.extraction.evidence_gaps[0]
    assert gap.item_id == "n2" and gap.candidate_payload is not None
    assert extraction.evidence_gaps[0].candidate_payload["id"] == "n2"


def test_accepted_with_revised_payload_revives_a_gap_candidate():
    material = _material("A quote\n\nSecond quote")
    n2 = _node_payload("n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("node", "n2", n2),),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n2", "accepted", "and repaired",
                  revised_payload=_TIME_FIX),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction, corpus_path="corpus/a.md")

    assert [node.id for node in result.extraction.nodes] == ["n2"]
    assert result.extraction.accumulation_status == "awaiting_case_binding"
    assert result.audits[0].revalidation_outcome == "revalidated"
    # The revived material-scoped candidate legitimately re-enters with the
    # parser's missing-case-context audit gap (and nothing else).
    assert tuple(gap.gap_type for gap in result.extraction.evidence_gaps) == (
        "missing_case_context",
    )
    assert extraction.nodes == ()


def test_revision_with_fabricated_quote_keeps_the_gap_and_audits_failure():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "fix quote",
                  revised_payload={"evidence": [
                      {"source_id": "m1", "quote": "Not in the material at all",
                       "paragraph": 2, "page": None},
                  ]}),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert "quote" in result.audits[0].revalidation_outcome
    assert {gap.item_id for gap in result.extraction.evidence_gaps} == {"n2", "n3"}
    assert all(
        gap.candidate_payload is not None
        for gap in result.extraction.evidence_gaps
    )


def test_revision_with_foreign_source_keeps_the_gap():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "fix source",
                  revised_payload={"source_ids": ["m-other"]}),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert result.extraction.evidence_gaps[0].item_id == "n2"


def test_revision_with_invalid_time_keeps_the_gap():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "later",
                  revised_payload={"happened_at": "2027-01-01T00:00:00+00:00",
                                   "valid_at": "2027-01-01T00:00:00+00:00",
                                   "observed_at": "2027-01-01T00:00:00+00:00"}),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert "future" in result.audits[0].revalidation_outcome


def test_revision_cannot_drift_the_case_identity_of_a_gap_candidate():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "re-anchor",
                  revised_payload={"case_id": "case-other"}),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert result.extraction.evidence_gaps[0].item_id == "n2"


def test_gap_revival_cannot_drift_from_a_declared_target_case():
    material = _material("A quote\n\nSecond quote")
    n2 = _node_payload("n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("node", "n2", n2),),
    )
    target = EvolutionCase(
        "case-y", "policy", "Case Y",
        datetime(2025, 12, 1, tzinfo=timezone.utc),
        "active", node_ids=(),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "fix", revised_payload=_TIME_FIX),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction,
                  target_case=target, corpus_path="corpus/a.md")

    assert result.extraction.nodes == ()
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert "target case" in result.audits[0].revalidation_outcome
    assert result.extraction.evidence_gaps[0].candidate_payload is not None


def test_context_only_gap_candidates_are_never_revived():
    material = _material("A quote\n\nSecond quote")
    fact_payload = {
        "fact_id": "f1",
        "subject": "Researchers",
        "predicate": "discussed",
        "object": "the intervention",
        "assertion_type": "fact",
        "valid_at": "2026-01-01T00:00:00+00:00",
        "invalid_at": None,
        "observed_at": "2026-01-01T00:00:00+00:00",
        "source_ids": ["m1"],
        "confidence": 0.4,
        "provenance_type": "context_only",
        "evidence_role": "context_only",
        "evidence": [
            {"source_id": "m1", "quote": "A quote", "paragraph": 1, "page": None}
        ],
    }
    extraction = ExtractionResult(
        case=None,
        material_role="review",
        evidence_gaps=(
            ExtractionEvidenceGap(
                "review_context", "context-only fact excluded",
                "temporal_fact", "f1", ("m1",), fact_payload,
            ),
        ),
    )
    text = json.dumps({"decisions": [
        _decision("temporal_fact", "f1", "revised", "relabel",
                  revised_payload={"evidence_role": "primary_observation",
                                   "provenance_type": "source_explicit"}),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction, corpus_path="corpus/a.md")

    assert result.extraction.temporal_facts == ()
    outcome = result.audits[0].revalidation_outcome
    assert outcome.startswith("rejected_revalidation")
    assert "context_only" in outcome
    assert result.extraction.evidence_gaps[0].candidate_payload is not None


def test_rejected_gap_stays_out_of_the_graph_and_is_audited():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "rejected", "unsupported"),
    ]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].decision.value == "rejected"
    assert result.audits[0].revalidation_outcome == "rejected"
    assert any(
        gap.item_id == "n2" and gap.candidate_payload is not None
        for gap in result.extraction.evidence_gaps
    )


def test_awaiting_case_binding_on_a_gap_candidate_is_audit_only():
    material, extraction = _gap_extraction()
    text = json.dumps({"decisions": [
        _decision("node", "n2", "awaiting_case_binding", "another case"),
    ]})
    service = AdjudicationService(_Router(text))
    result = _run(service, material, extraction)

    assert [node.id for node in result.extraction.nodes] == ["n1"]
    assert result.audits[0].revalidation_outcome == "awaiting_case_binding"
    assert any(gap.item_id == "n2" for gap in result.extraction.evidence_gaps)


def _conflict_payload(alternatives=("active", "paused")) -> dict:
    return {
        "conflict_id": "cf1",
        "subject": "Agency",
        "predicate": "status",
        "alternatives": list(alternatives),
        "source_ids": ["m1"],
        "evidence": [
            {"source_id": "m1", "quote": "A quote", "paragraph": 1, "page": None}
        ],
        "valid_at": "2025-12-15T00:00:00+00:00",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "provenance_type": "reported_conflict",
        "confidence": 1.0,
    }


def test_preserve_conflict_revives_only_a_legal_complete_conflict_payload():
    material = _material("A quote\n\nSecond quote")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("conflict", "cf1", _conflict_payload()),),
    )
    text = json.dumps({"decisions": [
        _decision("conflict", "cf1", "preserve_conflict", "unresolved"),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction, corpus_path="corpus/a.md")

    assert [conflict.conflict_id for conflict in result.extraction.conflicts] == ["cf1"]
    assert result.audits[0].revalidation_outcome == "preserved"
    assert all(gap.item_id != "cf1" for gap in result.extraction.evidence_gaps)


def test_preserve_conflict_with_an_incomplete_payload_keeps_the_gap():
    material = _material("A quote\n\nSecond quote")
    incomplete = _conflict_payload(alternatives=("active",))
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("conflict", "cf1", incomplete),),
    )
    text = json.dumps({"decisions": [
        _decision("conflict", "cf1", "preserve_conflict", "unresolved"),
    ]})
    service = AdjudicationService(_Router(text),
                                  extraction_service=ExtractionService(_Router("{}")))
    result = _run(service, material, extraction, corpus_path="corpus/a.md")

    assert result.extraction.conflicts == ()
    assert result.audits[0].revalidation_outcome.startswith("rejected_revalidation")
    assert any(gap.item_id == "cf1" for gap in result.extraction.evidence_gaps)


def test_preserve_conflict_is_unavailable_for_non_conflict_targets():
    material = _material("A quote\n\nSecond quote")
    n2 = _node_payload("n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("node", "n2", n2),),
    )
    text = json.dumps({"decisions": [
        _decision("node", "n2", "preserve_conflict", "keep it"),
    ]})
    service = AdjudicationService(_Router(text))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "invalid_preserve_conflict"
    assert "preserve_conflict" in caught.value.message


def test_gap_revival_audits_are_durable_idempotent_and_redacted(tmp_path):
    material = _material("A quote\n\nSecond quote\n\nSECRET-LINE-93421")
    n2 = _node_payload("n2", "Second quote", 2, happened_at="2025-11-01T00:00:00+00:00")
    extraction = ExtractionResult(
        case=None,
        evidence_gaps=(_payload_gap("node", "n2", n2),),
    )
    db_path = tmp_path / "ledger.db"
    text = json.dumps({"decisions": [
        _decision("node", "n2", "revised", "fixed", revised_payload=_TIME_FIX),
    ]})
    first = _service_with_ledger(
        text, db_path, extraction_service=ExtractionService(_Router("{}"))
    )
    result = _run(first, material, extraction, corpus_path="corpus/a.md")
    assert [node.id for node in result.extraction.nodes] == ["n2"]
    assert len(first.history("m1")) == 1

    second = _service_with_ledger(
        text, db_path, extraction_service=ExtractionService(_Router("{}"))
    )
    result = _run(second, material, extraction, corpus_path="corpus/a.md")
    assert len(result.audits) == 1
    assert len(second.history("m1")) == 1  # identical decision collapsed

    record = second.history("m1")[0]
    assert record.original_payload == n2
    assert record.revalidation_outcome == "revalidated"
    # The audit snapshot is plain JSON without internal corpus paths, and the
    # material body (including its secret line) never reaches the ledger.
    raw = db_path.read_bytes()
    assert b"corpus_path" not in raw
    assert b"SECRET-LINE-93421" not in raw


# ---------------------------------------------------------------------------
# Batch-level fail-open (real-smoke regression): the FIRST-layer extraction is
# already strictly verified; when the SECOND layer (LLM adjudication) returns
# a malformed batch, the service must raise a structured, secret-free
# AdjudicationBatchFailure (error code + field path + canonical summary only)
# and persist exactly one durable candidate_kind='batch' audit record, so the
# pipeline can keep the verified extraction instead of dropping the material.
# ---------------------------------------------------------------------------


class _FailRouter:
    def __init__(self, text):
        self.text = text

    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        return type("C", (), {"text": self.text})()


class _RaisingRouter:
    def __init__(self, exc):
        self.exc = exc

    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        raise self.exc


class _CaptureRouter:
    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, role, prompt):
        assert role == TaskRole.ADJUDICATE
        self.prompts.append(prompt)
        return type("C", (), {"text": '{"decisions":[]}'})()


def _bad_output(secret: str, path: str) -> str:
    """A real-like model completion: a decision object carrying an extra
    field the strict schema forbids, whose name/value embed a secret and a
    Windows absolute path (the smoke failure had exactly this shape)."""
    return json.dumps({
        "decisions": [
            {
                "candidate_kind": "node",
                "candidate_id": "n1",
                "decision": "accepted",
                "reason": "ok",
                path: secret,
            }
        ]
    })


def test_unknown_decision_field_raises_structured_failure_without_raw_output():
    material, extraction = _fixture()
    secret = "SECRET-TOKEN-778877"
    path = r"C:\Users\victim\.secrets\creds.json"
    service = AdjudicationService(_FailRouter(_bad_output(secret, path)))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    failure = caught.value
    assert failure.error_code == "unknown_decision_fields"
    assert failure.field == "decisions[0]"
    # Only the error type, field path and a canonical summary survive: never
    # the raw model output, its secrets, or absolute paths.
    assert secret not in str(failure)
    assert path not in str(failure)
    assert secret not in failure.message
    assert path not in failure.message
    assert ".secrets" not in failure.message
    # The already-verified extraction is untouched by the failed batch.
    assert [node.id for node in extraction.nodes] == ["n1"]


def test_missing_decision_fields_raise_structured_batch_failure():
    material, extraction = _fixture()
    text = json.dumps({"decisions": [
        {"candidate_kind": "node", "candidate_id": "n1", "reason": "ok"},
    ]})
    service = AdjudicationService(_FailRouter(text))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "missing_decision_fields"
    assert caught.value.field == "decisions[0]"


def test_invalid_decision_value_and_missing_revision_payload_are_structured():
    material, extraction = _fixture()
    bad_value = json.dumps({"decisions": [
        {"candidate_kind": "node", "candidate_id": "n1",
         "decision": "maybe", "reason": "?"},
    ]})
    service = AdjudicationService(_FailRouter(bad_value))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "invalid_decision_value"
    assert caught.value.field == "decisions[0].decision"
    revised_without_payload = json.dumps({"decisions": [
        {"candidate_kind": "node", "candidate_id": "n1",
         "decision": "revised", "reason": "edit"},
    ]})
    service = AdjudicationService(_FailRouter(revised_without_payload))
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "missing_revised_payload"
    assert caught.value.field == "decisions[0]"


def test_other_batch_shape_problems_are_structured_and_safe():
    material, extraction = _fixture()
    cases = [
        ('{"decisions": {}}', "invalid_payload", None),
        ("not json at all", "invalid_json", None),
        ('{"decisions": [42]}', "invalid_decision", "decisions[0]"),
        ('{"decisions": [{"candidate_kind": "gadget", "candidate_id": "n1", "decision": "accepted", "reason": "ok"}]}',
         "invalid_decision_kind", "decisions[0].candidate_kind"),
        ('{"decisions": [{"candidate_kind": "node", "candidate_id": "", "decision": "accepted", "reason": "ok"}]}',
         "invalid_candidate_id", "decisions[0].candidate_id"),
        ('{"decisions": [{"candidate_kind": "node", "candidate_id": "n1", "decision": "accepted", "reason": ""}]}',
         "invalid_reason", "decisions[0].reason"),
    ]
    for text, code, field in cases:
        service = AdjudicationService(_FailRouter(text))
        with pytest.raises(AdjudicationBatchFailure) as caught:
            _run(service, material, extraction)
        assert caught.value.error_code == code, text
        assert caught.value.field == field, text
        assert caught.value.message
    # A non-string completion is also a structured, safe batch failure.
    class _NoTextRouter:
        async def complete(self, role, prompt):
            return type("C", (), {"text": 123})()

    service = AdjudicationService(_NoTextRouter())
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(service, material, extraction)
    assert caught.value.error_code == "invalid_completion"


def test_missing_role_and_transport_errors_raise_structured_batch_failures(
    tmp_path,
):
    material, extraction = _fixture()
    db_path = tmp_path / "ledger.db"
    ledger = AdjudicationLedger(db_path)
    cases = [
        (MissingRoleError("missing task role: 'adjudicate'"), "missing_role"),
        (TimeoutError("timed out"), "transport_failure"),
        (RetriesExhaustedError(("provider-1",), 2), "transport_failure"),
    ]
    for exc, code in cases:
        service = AdjudicationService(
            _RaisingRouter(exc), ledger=ledger
        )
        with pytest.raises(AdjudicationBatchFailure) as caught:
            _run(service, material, extraction)
        assert caught.value.error_code == code
        assert caught.value.message
    records = ledger.entries("m1")
    # The two transport failures share one content-addressed code/field
    # identity, so they collapse into a single durable row.
    assert len(records) == 2
    assert all(record.candidate_kind == "batch" for record in records)
    assert {record.reason.split(":")[0] for record in records} == {
        "missing_role",
        "transport_failure",
    }


def test_batch_failure_is_audited_once_and_survives_restart(tmp_path):
    material, extraction = _fixture()
    db_path = tmp_path / "ledger.db"
    text = json.dumps({"decisions": [
        {"candidate_kind": "node", "candidate_id": "n1", "decision": "accepted",
         "reason": "ok", "confidence": 0.9},
    ]})
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    first = AdjudicationService(
        _FailRouter(text),
        ledger=AdjudicationLedger(db_path),
        clock=_FixedClock(now),
    )
    with pytest.raises(AdjudicationBatchFailure) as caught:
        _run(first, material, extraction)
    assert caught.value.error_code == "unknown_decision_fields"
    records = first.history("m1")
    assert len(records) == 1
    batch = records[0]
    assert batch.candidate_kind == "batch"
    assert batch.candidate_id == "adjudication_batch"
    assert batch.decision == "adjudication_failed"
    assert batch.revalidation_outcome == "adjudication_failed"
    assert batch.original_payload == {}
    assert "unknown_decision_fields" in batch.reason
    # A restart re-adjudicates the same material; the identical batch failure
    # collapses into the same durable row (no duplicate audit pile-up).
    second = AdjudicationService(
        _FailRouter(text),
        ledger=AdjudicationLedger(db_path),
        clock=_FixedClock(now),
    )
    with pytest.raises(AdjudicationBatchFailure):
        _run(second, material, extraction)
    reopened = AdjudicationLedger(db_path)
    entries = reopened.entries("m1")
    assert len(entries) == 1
    assert entries[0].decision == "adjudication_failed"
    assert entries[0].candidate_kind == "batch"
    # The failed batch never applied any decision and never mutated the
    # verified extraction.
    assert [node.id for node in extraction.nodes] == ["n1"]


def test_batch_failure_decision_is_a_formal_enum_with_value_across_restart(
    tmp_path,
):
    """A batch-failure audit record's decision is the AdjudicationDecision
    enum member ADJUDICATION_FAILED — never a bare string — so consumers can
    rely on ``decision.value``; this must hold for the freshly built record
    and for a ledger restart read-back."""
    material, extraction = _fixture()
    db_path = tmp_path / "ledger.db"
    text = json.dumps({"decisions": [
        {"candidate_kind": "node", "candidate_id": "n1", "decision": "accepted",
         "reason": "ok", "confidence": 0.9},
    ]})
    now = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    service = AdjudicationService(
        _FailRouter(text),
        ledger=AdjudicationLedger(db_path),
        clock=_FixedClock(now),
    )
    with pytest.raises(AdjudicationBatchFailure):
        _run(service, material, extraction)
    # The freshly built batch record already carries the formal enum.
    batch = service.history("m1")[0]
    assert isinstance(batch.decision, AdjudicationDecision)
    assert batch.decision is AdjudicationDecision.ADJUDICATION_FAILED
    assert batch.decision.value == "adjudication_failed"
    # A restart read-back reconstructs the same enum, never a bare string:
    # history consumers that use decision.value must not crash.
    reopened = AdjudicationLedger(db_path)
    entry = reopened.entries("m1")[0]
    assert isinstance(entry.decision, AdjudicationDecision)
    assert entry.decision is AdjudicationDecision.ADJUDICATION_FAILED
    assert entry.decision.value == "adjudication_failed"


def test_audit_record_decision_is_coerced_to_the_formal_enum():
    """AuditRecord normalizes any decision value to AdjudicationDecision
    (the batch-level 'adjudication_failed' string included) and rejects
    unknown decision strings instead of silently carrying a bare str."""
    from prism.adjudication.models import AuditRecord

    def make_record(decision):
        return AuditRecord(
            decision_id="d1",
            material_id="m1",
            candidate_kind="batch",
            candidate_id="adjudication_batch",
            original_payload_hash="h",
            original_payload={},
            validation_failures=(),
            decision=decision,
            reason="transport_failure: the adjudication LLM request failed",
            revised_payload=None,
            model_role="adjudicate",
            decided_at=datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc),
            revalidation_outcome="adjudication_failed",
        )

    record = make_record("adjudication_failed")
    assert isinstance(record.decision, AdjudicationDecision)
    assert record.decision is AdjudicationDecision.ADJUDICATION_FAILED
    assert record.decision.value == "adjudication_failed"
    with pytest.raises(ValueError):
        make_record("not_a_real_decision")


def test_batch_failure_audit_never_stores_raw_output_secret_or_paths(
    tmp_path,
):
    material, extraction = _fixture()
    secret = "SECRET-LINE-778877"
    path = r"C:\Users\victim\.secrets\creds.json"
    db_path = tmp_path / "ledger.db"
    service = AdjudicationService(
        _FailRouter(_bad_output(secret, path)),
        ledger=AdjudicationLedger(db_path),
        clock=_FixedClock(datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)),
    )
    with pytest.raises(AdjudicationBatchFailure):
        _run(service, material, extraction)
    raw = db_path.read_bytes()
    assert secret.encode("utf-8") not in raw
    assert path.encode("utf-8") not in raw
    assert b".secrets" not in raw
    record = service.history("m1")[0]
    assert secret not in record.reason
    assert path not in record.reason


def test_adjudication_prompt_enforces_the_single_decision_json_shape():
    material, extraction = _fixture()
    router = _CaptureRouter()
    service = AdjudicationService(router)
    result = _run(service, material, extraction)
    assert result.extraction is extraction
    assert len(router.prompts) == 1
    prompt = router.prompts[0]
    # The prompt shows one exact single-decision JSON object ...
    assert (
        '{"candidate_kind": "node", "candidate_id": "node-1", '
        '"decision": "accepted", '
        '"reason": "short safe justification"}'
    ) in prompt
    # ... and explicitly forbids any other field on a decision object.
    assert "Do NOT add any other field" in prompt
    assert "exactly" in prompt
    assert '"decision"' in prompt
    assert "accepted, revised, rejected, preserve_conflict" in prompt
