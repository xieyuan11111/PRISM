"""TDD acceptance for the experimental split-v1 extraction structure."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.domain import EvolutionCase, Material
from prism.extraction import ExtractionError, ExtractionResult, ExtractionService
from prism.extraction.split import SplitExtractionError, SplitExtractionService

UTC = timezone.utc
PUBLISHED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
MATERIAL_ID = "material-split"
CASE_ID = "case-split"

BODY = """On 2026-01-10, the ministry proposed the disclosure rule.

On 2026-02-15, the ministry implemented the disclosure rule.

Analysts said the rule may expand next year.

The implementation supersedes the proposal.
"""


class FakeRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, role: str, prompt: str):
        self.calls.append((role, prompt))
        if not self.responses:
            raise AssertionError("unexpected router completion")
        response = self.responses.pop(0)
        text = response if isinstance(response, str) else json.dumps(response)
        return type("Completion", (), {"text": text})()


def run(coro):
    return asyncio.run(coro)


def material() -> Material:
    return Material(
        id=MATERIAL_ID,
        title="Split extraction chronology",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content=BODY,
        original_format="md",
        case_tags=(CASE_ID,),
    )


def target_case() -> EvolutionCase:
    return EvolutionCase(
        case_id=CASE_ID,
        case_type="policy",
        canonical_name="Disclosure rule",
        start_at=datetime(2026, 1, 10, tzinfo=UTC),
        status="implemented",
    )


def evidence(quote: str, paragraph: int) -> list[dict]:
    return [
        {
            "source_id": MATERIAL_ID,
            "quote": quote,
            "paragraph": paragraph,
            "page": None,
        }
    ]


def stage_a_payload() -> dict:
    return {
        "material_role": "policy_source",
        "case": {
            "case_id": CASE_ID,
            "case_type": "policy",
            "canonical_name": "Disclosure rule",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "implemented",
            "node_ids": ["node-policy"],
        },
        "nodes": [
            {
                "id": "node-policy",
                "case_id": CASE_ID,
                "node_type": "proposal",
                "assertion_type": "fact",
                "happened_at": "2026-01-10T00:00:00+00:00",
                "valid_at": "2026-01-10T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "The ministry proposed the disclosure rule.",
                "source_ids": [MATERIAL_ID],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": evidence(
                    "On 2026-01-10, the ministry proposed the disclosure rule.", 1
                ),
            }
        ],
        "temporal_facts": [
            {
                "fact_id": "fact-implementation",
                "subject": "Disclosure rule",
                "predicate": "implementation_status",
                "object": "implemented",
                "assertion_type": "fact",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": PUBLISHED.isoformat(),
                "source_ids": [MATERIAL_ID],
                "confidence": 0.97,
                "provenance_type": "source_explicit",
                "evidence": evidence(
                    "On 2026-02-15, the ministry implemented the disclosure rule.", 2
                ),
            }
        ],
        "warnings": ["stage-a warning"],
    }


def stage_b_payload(*, relation_target: str = "fact-implementation") -> dict:
    return {
        "claims": [
            {
                "claim_id": "claim-forecast",
                "actor": "Analysts",
                "proposition": "The rule may expand next year.",
                "stance": "uncertain",
                "claim_type": "prediction",
                "stated_at": PUBLISHED.isoformat(),
                "observed_at": PUBLISHED.isoformat(),
                "based_on": [MATERIAL_ID],
                "revised_by": None,
                "provenance_type": "reported",
                "confidence": 0.72,
                "evidence": evidence("Analysts said the rule may expand next year.", 3),
            }
        ],
        "conflicts": [],
        "relations": [
            {
                "relation_id": "relation-supersedes",
                "relation_type": "supersedes",
                "source_ref": "node-policy",
                "target_ref": relation_target,
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": PUBLISHED.isoformat(),
                "source_ids": [MATERIAL_ID],
                "evidence": evidence(
                    "The implementation supersedes the proposal.", 4
                ),
                "confidence": 0.95,
                "provenance_type": "source_explicit",
            }
        ],
        "warnings": ["stage-b warning"],
    }


def split_service(responses) -> tuple[SplitExtractionService, FakeRouter]:
    router = FakeRouter(responses)
    return (
        SplitExtractionService(router),
        router,
    )


def extract_split(responses) -> tuple[ExtractionResult, FakeRouter]:
    service, router = split_service(responses)
    result = run(
        service.extract_material_split(
            material(),
            corpus_path="corpus/2026-03/split.md",
            target_case=target_case(),
        )
    )
    return result, router


def test_stage_a_runs_before_stage_b_and_combines_validated_results():
    result, router = extract_split([stage_a_payload(), stage_b_payload()])

    assert [node.id for node in result.nodes] == ["node-policy"]
    assert [fact.fact_id for fact in result.temporal_facts] == ["fact-implementation"]
    assert [claim.claim_id for claim in result.claims] == ["claim-forecast"]
    assert [relation.relation_id for relation in result.relations] == [
        "relation-supersedes"
    ]
    assert result.relations[0].source_ref == "node-policy"
    assert result.relations[0].target_ref == "fact-implementation"
    assert result.evidence_gaps == ()
    assert result.evidence_matches
    assert "stage-a warning" in result.warnings
    assert "stage-b warning" in result.warnings
    assert result.accumulation_status == "case_bound"

    assert [role for role, _ in router.calls] == ["extract", "extract"]
    stage_a_prompt = router.calls[0][1]
    stage_b_prompt = router.calls[1][1]
    assert "SPLIT-V1 STAGE A" in stage_a_prompt
    assert "material_role, case, nodes, temporal_facts, warnings" in stage_a_prompt
    assert "claims, conflicts, and relations are forbidden" in stage_a_prompt
    assert "DECLARED TARGET CASE" in stage_a_prompt
    assert "SPLIT-V1 STAGE B" in stage_b_prompt
    assert "claims, conflicts, relations, warnings" in stage_b_prompt
    assert "node-policy" in stage_b_prompt
    assert "fact-implementation" in stage_b_prompt
    assert "Do not return nodes or temporal_facts" in stage_b_prompt


def test_stage_a_failure_never_calls_stage_b():
    service, router = split_service(['{"material_role":"policy_source"'])

    with pytest.raises(SplitExtractionError) as info:
        run(
            service.extract_material_split(
                material(),
                corpus_path="corpus/2026-03/split.md",
                target_case=target_case(),
            )
        )

    assert info.value.stage == "stage_a"
    assert isinstance(info.value, ExtractionError)
    assert len(router.calls) == 1


def test_stage_b_failure_preserves_stage_a_and_adds_structured_gap():
    result, router = extract_split(
        [stage_a_payload(), '{"claims":[{"claim_id":"SECRET-BAD-CLAIM"}']
    )

    assert [node.id for node in result.nodes] == ["node-policy"]
    assert [fact.fact_id for fact in result.temporal_facts] == ["fact-implementation"]
    assert result.claims == ()
    assert result.relations == ()
    assert [gap.gap_type for gap in result.evidence_gaps] == ["stage_b_failure"]
    assert "SECRET-BAD-CLAIM" not in result.evidence_gaps[0].detail
    assert result.evidence_gaps[0].source_ids == (MATERIAL_ID,)
    assert result.accumulation_status == "case_bound"
    assert len(router.calls) == 2


def test_stage_b_relation_must_reference_stage_a_accepted_ids():
    result, _ = extract_split(
        [stage_a_payload(), stage_b_payload(relation_target="not-from-stage-a")]
    )

    assert [node.id for node in result.nodes] == ["node-policy"]
    assert [fact.fact_id for fact in result.temporal_facts] == ["fact-implementation"]
    assert result.relations == ()
    assert [gap.gap_type for gap in result.evidence_gaps] == ["stage_b_failure"]


@pytest.mark.parametrize(
    ("stage_a", "stage_b"),
    [
        (
            {**stage_a_payload(), "claims": []},
            stage_b_payload(),
        ),
        (
            {"material_role": "policy_source"},
            stage_b_payload(),
        ),
        (
            stage_a_payload(),
            {**stage_b_payload(), "nodes": []},
        ),
        (
            stage_a_payload(),
            {"claims": []},
        ),
    ],
)
def test_complete_and_partial_stage_envelopes_fail_closed(stage_a, stage_b):
    if set(stage_a) != {"material_role", "case", "nodes", "temporal_facts", "warnings"}:
        service, router = split_service([stage_a])
        with pytest.raises(SplitExtractionError) as info:
            run(
                service.extract_material_split(
                    material(),
                    corpus_path="corpus/2026-03/split.md",
                    target_case=target_case(),
                )
            )
        assert info.value.stage == "stage_a"
        assert len(router.calls) == 1
        return

    result, _ = extract_split([stage_a, stage_b])
    assert [gap.gap_type for gap in result.evidence_gaps] == ["stage_b_failure"]
    assert result.claims == ()
    assert result.relations == ()


def test_default_extraction_service_remains_single_stage_and_byte_identical():
    full_payload = {
        **stage_a_payload(),
        "claims": stage_b_payload()["claims"],
        "conflicts": [],
        "relations": [],
        "warnings": [],
    }
    router = FakeRouter([full_payload])
    service = ExtractionService(router)
    source = material()

    result = run(
        service.extract_material(
            source, corpus_path="corpus/2026-03/split.md", target_case=target_case()
        )
    )

    assert isinstance(result, ExtractionResult)
    assert [node.id for node in result.nodes] == ["node-policy"]
    assert [fact.fact_id for fact in result.temporal_facts] == ["fact-implementation"]
    assert len(router.calls) == 1
    assert "SPLIT-V1" not in router.calls[0][1]
    assert router.calls[0][1] == ExtractionService._evolution_prompt(
        source, target_case()
    )
