"""TDD tests for the experimental extraction prompt profiles.

The baseline strict evolution prompt is the production contract: it must
stay byte-identical when no profile (or the explicit ``baseline`` profile)
is selected.  The experimental ``protocol-v1`` profile may only be enabled
through the controlled :class:`ExtractionService` constructor parameter,
never through the default composition, and may never relax deterministic
quote/time/source/case/relation validation — it only changes the prompt.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.domain import Material
from prism.extraction import ExtractionService
from prism.extraction.profiles import (
    PROTOCOL_V1_PROFILE,
    normalize_prompt_profile,
)

UTC = timezone.utc
PUBLISHED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
BODY = """On 2026-01-10, the ministry proposed the disclosure rule.

On 2026-02-15, the ministry implemented the disclosure rule."""


class Router:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return type("Completion", (), {"text": text})()


def make_material(**overrides):
    values = dict(
        id="material-profiles",
        title="Disclosure rule chronology",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="policy",
        content=BODY,
        original_format="md",
        case_tags=("case-disclosure",),
    )
    values.update(overrides)
    return Material(**values)


def strict_payload():
    return {
        "material_role": "policy_source",
        "case": {
            "case_id": "case-disclosure",
            "case_type": "policy",
            "canonical_name": "Disclosure rule",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "proposed",
            "node_ids": ["proposal"],
        },
        "nodes": [
            {
                "id": "proposal",
                "case_id": "case-disclosure",
                "node_type": "proposal",
                "assertion_type": "fact",
                "happened_at": "2026-01-10T00:00:00+00:00",
                "valid_at": "2026-01-10T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "The ministry proposed the rule.",
                "source_ids": ["material-profiles"],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": [
                    {
                        "source_id": "material-profiles",
                        "quote": "On 2026-01-10, the ministry proposed the disclosure rule.",
                        "paragraph": 1,
                        "page": None,
                    }
                ],
            }
        ],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "relations": [],
        "warnings": [],
    }


def run(coro):
    return asyncio.run(coro)


def strict_prompt(service):
    return service._prompt(make_material(), strict=True)


# ------------------------------------------------------------- profile registry


def test_normalize_prompt_profile_accepts_none_baseline_and_protocol_v1():
    assert normalize_prompt_profile(None) is None
    assert normalize_prompt_profile("baseline") == "baseline"
    assert normalize_prompt_profile(PROTOCOL_V1_PROFILE) == PROTOCOL_V1_PROFILE


@pytest.mark.parametrize(
    "value",
    ["protocol-v2", "", "Baseline", "protocol v1", "PROTOCOL-V1", 1, b"baseline"],
)
def test_unknown_or_malformed_profile_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_prompt_profile(value)


def test_service_constructor_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unknown prompt_profile"):
        ExtractionService(Router("{}"), prompt_profile="protocol-v2")


# ------------------------------------------------------- baseline isolation


def test_default_service_prompt_is_byte_identical_to_baseline_prompt():
    service = ExtractionService(Router(strict_payload()))
    assert service._prompt_profile is None

    result = run(
        service.extract_material(
            make_material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert result.nodes[0].id == "proposal"
    sent = service._router.calls[0][1]
    assert sent == strict_prompt(service)
    assert sent == ExtractionService._evolution_prompt(make_material())


def test_explicit_baseline_profile_matches_default_prompt_bytes():
    service = ExtractionService(
        Router(strict_payload()), prompt_profile="baseline"
    )

    run(
        service.extract_material(
            make_material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    assert (
        service._router.calls[0][1] == ExtractionService._evolution_prompt(make_material())
    )


def test_legacy_extract_prompt_is_unchanged_without_profile():
    service = ExtractionService(
        Router(
            {
                "case": None,
                "nodes": [],
                "temporal_facts": [],
                "claims": [],
                "warnings": [],
            }
        )
    )

    run(service.extract(make_material()))

    role, prompt = service._router.calls[0]
    assert role == "extract"
    assert prompt == ExtractionService._prompt(make_material(), strict=False)


# ------------------------------------------------------------ protocol-v1 shape


def make_protocol_v1_service():
    service = ExtractionService(
        Router(strict_payload()), prompt_profile=PROTOCOL_V1_PROFILE
    )
    return service


def protocol_v1_prompt():
    service = make_protocol_v1_service()
    run(
        service.extract_material(
            make_material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )
    return service._router.calls[0][1]


def test_protocol_v1_prompt_prepends_self_check_before_baseline():
    prompt = protocol_v1_prompt()
    baseline = ExtractionService._evolution_prompt(make_material())

    assert prompt != baseline
    assert prompt.startswith("SILENT PRE-JSON SELF-CHECK")
    # The baseline prompt bytes survive untouched after the prepended block:
    # protocol-v1 only adds instructions, it never edits the contract.
    assert prompt.endswith(baseline)
    # The material content stays the final section of the prompt.
    assert prompt.endswith(f"{BODY}\nEND MATERIAL CONTENT")


def test_protocol_v1_self_check_covers_verbatim_paragraph_unique_quotes():
    prompt = protocol_v1_prompt()

    assert "SILENT PRE-JSON SELF-CHECK" in prompt
    assert "verbatim" in prompt
    assert "continuous" in prompt
    assert "single non-empty paragraph" in prompt
    assert "exactly that one paragraph" in prompt
    assert "drop that candidate" in prompt


def test_protocol_v1_self_check_covers_time_ordering_and_fetched_bound():
    prompt = protocol_v1_prompt()

    assert "observed_at" in prompt
    assert "valid_at" in prompt
    assert "happened_at" in prompt
    assert "stated_at" in prompt
    assert "not earlier than" in prompt
    assert "none is later than the material fetched time" in prompt
    assert FETCHED.isoformat() in prompt


def test_protocol_v1_self_check_covers_prediction_claim_only():
    prompt = protocol_v1_prompt()

    assert "claim_type prediction" in prompt
    assert "stance uncertain" in prompt
    assert "never a node" in prompt


def test_protocol_v1_self_check_covers_relation_gating_and_empty_default():
    prompt = protocol_v1_prompt()

    assert "explicitly states" in prompt
    assert "source_ref" in prompt
    assert "target_ref" in prompt
    assert "verbatim quote" in prompt
    assert "relations array must be exactly" in prompt
    assert "[]" in prompt


def test_protocol_v1_self_check_declares_no_json_output_of_itself():
    prompt = protocol_v1_prompt()

    assert "never part of the response" in prompt
    assert "no self-check results" in prompt
    assert "leaves no trace in the JSON" in prompt


def test_protocol_v1_profile_never_reaches_the_legacy_extract_prompt():
    service = make_protocol_v1_service()

    with pytest.raises(ValueError, match="strict evolution"):
        run(service.extract(make_material()))


def test_profile_changes_prompt_only_not_validation_outcomes():
    baseline = ExtractionService(Router(strict_payload()))
    profiled = make_protocol_v1_service()

    baseline_result = run(
        baseline.extract_material(
            make_material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )
    profiled_result = run(
        profiled.extract_material(
            make_material(), corpus_path="corpus/2026-03/disclosure.md"
        )
    )

    # Same deterministic parser: identical accepted candidates and gaps.
    assert [node.id for node in profiled_result.nodes] == [
        node.id for node in baseline_result.nodes
    ]
    assert profiled_result.evidence_gaps == baseline_result.evidence_gaps
    assert profiled_result.warnings == baseline_result.warnings
    assert profiled_result.relations == baseline_result.relations
