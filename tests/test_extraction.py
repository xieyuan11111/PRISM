import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.extraction import (
    ExtractionError,
    ExtractionEvidenceMatch,
    ExtractionResult,
    ExtractionService,
)
from prism.llm import Completion


PUBLISHED = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def make_material(**overrides):
    values = {
        "id": "material-1",
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


def valid_payload():
    return {
        "case": {
            "case_id": "case-1",
            "case_type": "policy",
            "canonical_name": "Revised policy",
            "start_at": "2026-08-30T08:00:00Z",
            "status": "active",
            "node_ids": ["node-1"],
        },
        "nodes": [
            {
                "id": "node-1",
                "case_id": "case-1",
                "node_type": "publication",
                "happened_at": "2026-08-31T09:30:00+00:00",
                "summary": "The revised policy was published.",
                "claim_ids": ["claim-1"],
            }
        ],
        "temporal_facts": [
            {
                "subject": "Agency",
                "predicate": "published",
                "object": "Revised policy",
                "valid_at": "2026-08-31T09:30:00Z",
                "invalid_at": None,
                "observed_at": "2026-08-31T10:00:00Z",
                "source_ids": [],
                "confidence": 0.82,
                "provenance_type": "explicit",
            }
        ],
        "claims": [
            {
                "claim_id": "claim-1",
                "actor": "Agency",
                "proposition": "The revision improves clarity.",
                "stance": "support",
                "stated_at": "2026-08-31T09:45:00Z",
                "revised_by": None,
            }
        ],
        "warnings": ["The publication time is stated only to the minute."],
    }


class FakeRouter:
    def __init__(self, payload, *, api_secret="router-api-secret"):
        self.payload = payload
        self.api_secret = api_secret
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return Completion(text=text, provider=self.api_secret, model=self.api_secret)


def run_extract(payload, material=None):
    router = FakeRouter(payload)
    result = asyncio.run(ExtractionService(router).extract(material or make_material()))
    return result, router


def test_extract_calls_extract_role_and_maps_domain_objects_without_secret_leakage():
    result, router = run_extract(valid_payload())

    assert isinstance(result, ExtractionResult)
    assert isinstance(result.case, EvolutionCase)
    assert isinstance(result.nodes[0], EvolutionNode)
    assert isinstance(result.temporal_facts[0], TemporalFact)
    assert isinstance(result.claims[0], Claim)
    assert result.nodes[0].source_ids == ("material-1",)
    assert result.temporal_facts[0].source_ids == ("material-1",)
    assert result.claims[0].based_on == ("material-1",)
    assert result.warnings == (
        "The publication time is stated only to the minute.",
    )
    assert router.calls[0][0] == "extract"
    assert "material-1" in router.calls[0][1]
    assert make_material().content in router.calls[0][1]
    assert router.api_secret not in repr(result)

    with pytest.raises(FrozenInstanceError):
        result.warnings = ()


def test_explicit_evidence_and_uncertainty_are_preserved():
    payload = valid_payload()
    payload["nodes"][0]["source_ids"] = ["source-a", "source-b"]
    payload["temporal_facts"][0]["source_ids"] = ["source-a"]
    payload["temporal_facts"][0]["confidence"] = 0.31
    payload["temporal_facts"][0]["provenance_type"] = "inferred"
    payload["claims"][0]["based_on"] = ["source-b"]

    result, _ = run_extract(payload)

    assert result.nodes[0].source_ids == ("source-a", "source-b")
    assert result.temporal_facts[0].source_ids == ("source-a",)
    assert result.temporal_facts[0].confidence == 0.31
    assert result.temporal_facts[0].provenance_type == "inferred"
    assert result.claims[0].based_on == ("source-b",)


def test_optional_case_and_empty_collections_are_supported():
    result, _ = run_extract(
        {
            "case": None,
            "nodes": [],
            "temporal_facts": [],
            "claims": [],
            "warnings": ["No structured assertions were supported by the material."],
        }
    )

    assert result == ExtractionResult(
        case=None,
        nodes=(),
        temporal_facts=(),
        claims=(),
        warnings=("No structured assertions were supported by the material.",),
    )


def test_json_fenced_and_bare_noise_repairs_are_audited():
    fenced = f"```json\n{json.dumps(valid_payload())}\n```"
    result, _ = run_extract(fenced)
    assert result.case.case_id == "case-1"
    assert any(
        "JSON syntax repair" in warning and "code fence" in warning
        for warning in result.warnings
    )

    result, _ = run_extract(
        f"Here is the result:\n{json.dumps(valid_payload())}\nEnd result."
    )
    assert result.case.case_id == "case-1"
    assert any(
        "JSON syntax repair" in warning and "surrounding" in warning
        for warning in result.warnings
    )


def test_only_structural_trailing_commas_are_repaired_and_audited():
    payload = valid_payload()
    payload["warnings"].append("A literal comma before a brace stays: ,}")
    encoded = json.dumps(payload)
    completion = encoded[:-1] + ",}"

    result, _ = run_extract(completion)

    assert result.case.case_id == "case-1"
    assert "A literal comma before a brace stays: ,}" in result.warnings
    assert any(
        "JSON syntax repair" in warning and "trailing comma" in warning
        for warning in result.warnings
    )


@pytest.mark.parametrize(
    "completion",
    [
        # Two adjacent objects are not a unique recoverable envelope.  In
        # particular, the parser must not silently keep one and drop the other.
        '{"case":null}{"case":null}',
        # A malformed nested member is intermediate content, not surrounding
        # noise and not a structural trailing comma.
        '{"case":null,"nodes":[],"temporal_facts":[],"claims":[],"warnings":[] '
        '{"nested":true}}',
        # Missing quotes require semantic guessing and are never repaired.
        '{"case":null,"nodes":[],"temporal_facts":[],"claims":[],warnings:[]}',
    ],
)
def test_non_unique_or_non_local_json_damage_is_rejected(completion):
    with pytest.raises(ExtractionError, match="valid JSON"):
        run_extract(completion)


@pytest.mark.parametrize(
    "completion",
    [
        '{"case":null,"case":null,"nodes":[],"temporal_facts":[],"claims":[],"warnings":[],}',
        '{"case":null,"nodes":[],"temporal_facts":[],"claims":[],"warnings":[],"score":NaN,}',
        '{"case":null,"nodes":[],"temporal_facts":[],"claims":[],"warnings":[],"score":Infinity,}',
    ],
)
def test_trailing_comma_repair_cannot_bypass_duplicate_or_nonfinite_rejection(
    completion,
):
    with pytest.raises(ExtractionError, match="valid JSON") as caught:
        run_extract(completion)
    assert "JSON syntax repair" in str(caught.value)


def test_noise_repair_cannot_bypass_unknown_secret_field_rejection():
    payload = valid_payload()
    payload["api_key"] = "must-not-pass"

    with pytest.raises(ExtractionError, match="unexpected field"):
        run_extract(f"result follows\n{json.dumps(payload)}\nresult ends")


@pytest.mark.parametrize(
    ("completion", "message"),
    [
        ("not json", "valid JSON"),
        ("[]", "JSON object"),
        ('{"case": null}', "missing required field"),
    ],
)
def test_malformed_or_incomplete_completions_fail_without_partial_results(
    completion, message
):
    with pytest.raises(ExtractionError, match=message):
        run_extract(completion)


@pytest.mark.parametrize(
    "path",
    [
        ("case", "canonical_name"),
        ("nodes", 0, "summary"),
        ("temporal_facts", 0, "confidence"),
        ("claims", 0, "actor"),
    ],
)
def test_missing_required_nested_fields_are_clear(path):
    payload = valid_payload()
    parent = payload
    for part in path[:-1]:
        parent = parent[part]
    parent.pop(path[-1])

    with pytest.raises(ExtractionError, match="missing required field"):
        run_extract(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"api_key": "untrusted"}),
        lambda payload: payload["nodes"][0].update({"explanation": "untrusted"}),
        lambda payload: payload["claims"][0].update({"secret": "untrusted"}),
    ],
)
def test_extra_untrusted_fields_are_rejected(mutate):
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ExtractionError, match="unexpected field"):
        run_extract(payload)


def test_declared_node_and_claim_references_must_exist():
    payload = valid_payload()
    payload["case"]["node_ids"] = ["missing-node"]

    with pytest.raises(ExtractionError, match="node_ids"):
        run_extract(payload)

    payload = valid_payload()
    payload["nodes"][0]["claim_ids"] = ["missing-claim"]

    with pytest.raises(ExtractionError, match="claim_ids"):
        run_extract(payload)


def test_wrong_case_id_is_rejected():
    payload = valid_payload()
    payload["nodes"][0]["case_id"] = "case-other"

    with pytest.raises(ExtractionError, match="case_id.*case-1"):
        run_extract(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["nodes"][0].update(
            {"happened_at": "2026-09-01T00:00:00Z"}
        ),
        lambda payload: payload["temporal_facts"][0].update(
            {"observed_at": "2026-08-31T09:00:00Z"}
        ),
        lambda payload: payload["temporal_facts"][0].update(
            {"invalid_at": "2026-08-30T00:00:00Z"}
        ),
        lambda payload: payload["case"].update(
            {"start_at": "2026-08-31T09:31:00Z"}
        ),
        lambda payload: payload["case"].update(
            {"status_at": "2026-09-01T00:00:00Z"}
        ),
        lambda payload: payload["case"].update(
            {"status_observed_at": "2026-09-01T00:00:00Z"}
        ),
    ],
)
def test_future_and_invalid_time_ordering_are_rejected(mutate):
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ExtractionError, match="time|earlier|later|future"):
        run_extract(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"nodes": {}}),
        lambda payload: payload["nodes"][0].update(
            {"happened_at": "2026-08-31T09:30:00"}
        ),
        lambda payload: payload["temporal_facts"][0].update({"confidence": True}),
        lambda payload: payload["claims"][0].update({"stance": "enthusiastic"}),
        lambda payload: payload["warnings"].append({"message": "not text"}),
    ],
)
def test_unsupported_types_enums_and_naive_timestamps_are_rejected(mutate):
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(ExtractionError):
        run_extract(payload)


# --- Real-LLM output compatibility (narrow recovery, audited) -------------


def test_missing_warnings_and_scalar_based_on_are_recovered_with_audit():
    payload = valid_payload()
    del payload["warnings"]
    payload["claims"][0]["based_on"] = "material-1"

    result, _ = run_extract(payload)

    assert result.claims[0].based_on == ("material-1",)
    assert any("warnings" in warning for warning in result.warnings)
    assert any(
        "based_on" in warning and "normalized" in warning
        for warning in result.warnings
    )


def test_legacy_top_level_evidence_field_is_ignored_with_warning():
    payload = valid_payload()
    payload["evidence"] = [
        {
            "source_id": "material-1",
            "quote": "The agency published the revised policy.",
            "paragraph": 1,
            "page": None,
        }
    ]

    result, _ = run_extract(payload)

    assert result.nodes[0].source_ids == ("material-1",)
    assert any("top-level evidence" in warning for warning in result.warnings)


def test_legacy_unusable_case_id_records_gap_and_keeps_tag_bound_nodes():
    payload = valid_payload()
    payload["case"]["case_id"] = ""

    result, _ = run_extract(payload)

    assert result.case is None
    assert result.nodes[0].id == "node-1"
    assert any(gap.gap_type == "unusable_case" for gap in result.evidence_gaps)


def test_evidence_match_records_are_validated_and_default_empty():
    assert ExtractionResult().evidence_matches == ()

    with pytest.raises(ValueError, match="match_type"):
        ExtractionEvidenceMatch("nodes[0].evidence[0]", "material-1", "semantic")
    with pytest.raises(ValueError, match="paragraph"):
        ExtractionEvidenceMatch(
            "nodes[0].evidence[0]", "material-1", "exact", paragraph=0
        )
    with pytest.raises(ValueError, match="path"):
        ExtractionEvidenceMatch(" ", "material-1", "exact")
