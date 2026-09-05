"""Provider evidence-shape compatibility for strict extraction.

Real providers of narrow policy cases sometimes return ``evidence`` as a
single object or a bare quote string instead of the required JSON array.
Exactly those two shapes may be normalized — audited with a warning and
re-validated by the same source/quote resolver as list items — while every
other non-array shape (evidence maps, reasoning objects, empty values,
blank strings) stays fail-closed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from prism.domain import Material
from prism.extraction import ExtractionService


UTC = timezone.utc
PUBLISHED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
BODY = (
    "On 2026-01-10, the ministry proposed the disclosure rule.\n"
    "\n"
    "On 2026-02-15, the ministry implemented the disclosure rule.\n"
    "\n"
    "Analysts said the rule may expand next year.\n"
    "\n"
    "The ministry said adoption increased; researchers said adoption decreased."
)
PROPOSAL_QUOTE = "On 2026-01-10, the ministry proposed the disclosure rule."
IMPLEMENTATION_QUOTE = "On 2026-02-15, the ministry implemented the disclosure rule."
FORECAST_QUOTE = "Analysts said the rule may expand next year."
CORPUS_PATH = "corpus/2026-03/disclosure.md"


class FakeRouter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return type("Completion", (), {"text": text})()


def run(coro):
    return asyncio.run(coro)


def material(**overrides):
    values = dict(
        id="material-evolution",
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


def evidence(quote, paragraph):
    return [
        {
            "source_id": "material-evolution",
            "quote": quote,
            "paragraph": paragraph,
            "page": None,
        }
    ]


def payload():
    return {
        "material_role": "policy_source",
        "case": {
            "case_id": "case-disclosure",
            "case_type": "policy",
            "canonical_name": "Disclosure rule",
            "start_at": "2026-01-10T00:00:00+00:00",
            "status": "implemented",
            "node_ids": ["proposal", "implementation"],
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
                "source_ids": ["material-evolution"],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": evidence(PROPOSAL_QUOTE, 1),
            },
            {
                "id": "implementation",
                "case_id": "case-disclosure",
                "node_type": "implementation",
                "assertion_type": "fact",
                "happened_at": "2026-02-15T00:00:00+00:00",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "The ministry implemented the rule.",
                "source_ids": ["material-evolution"],
                "claim_ids": [],
                "provenance_type": "source_explicit",
                "evidence": evidence(IMPLEMENTATION_QUOTE, 2),
            },
        ],
        "temporal_facts": [
            {
                "subject": "Disclosure rule",
                "predicate": "implementation_status",
                "object": "implemented",
                "assertion_type": "fact",
                "valid_at": "2026-02-15T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": PUBLISHED.isoformat(),
                "source_ids": ["material-evolution"],
                "confidence": 0.97,
                "provenance_type": "source_explicit",
                "evidence": evidence(IMPLEMENTATION_QUOTE, 2),
            }
        ],
        "claims": [
            {
                "claim_id": "forecast",
                "actor": "Analysts",
                "proposition": "The rule may expand next year.",
                "stance": "uncertain",
                "claim_type": "prediction",
                "stated_at": PUBLISHED.isoformat(),
                "observed_at": PUBLISHED.isoformat(),
                "based_on": ["material-evolution"],
                "revised_by": None,
                "provenance_type": "reported",
                "confidence": 0.72,
                "evidence": evidence(FORECAST_QUOTE, 3),
            }
        ],
        "conflicts": [],
        "warnings": [],
    }


def extract(mutated):
    router = FakeRouter(mutated)
    service = ExtractionService(router)
    result = run(service.extract_material(material(), corpus_path=CORPUS_PATH))
    return result


# --- Narrow, auditable normalizations --------------------------------------


def test_single_evidence_object_is_normalized_to_a_single_element_array():
    shape = payload()
    shape["nodes"][1]["evidence"] = {
        "source_id": "material-evolution",
        "quote": IMPLEMENTATION_QUOTE,
        "paragraph": 2,
        "page": None,
    }

    result = extract(shape)

    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    locator = result.nodes[1].evidence[0]
    assert locator.quote == IMPLEMENTATION_QUOTE
    assert locator.paragraph == 2
    assert locator.source_id == "material-evolution"
    assert locator.corpus_path == CORPUS_PATH
    assert any(
        "evidence_single_object_normalized" in warning
        and "nodes[1].evidence" in warning
        for warning in result.warnings
    )
    assert any(
        match.path == "nodes[1].evidence[0]" and match.match_type == "exact"
        for match in result.evidence_matches
    )


def test_single_evidence_object_with_only_quote_binds_to_current_material():
    shape = payload()
    shape["temporal_facts"][0]["evidence"] = {"quote": IMPLEMENTATION_QUOTE}

    result = extract(shape)

    assert result.temporal_facts[0].object == "implemented"
    locator = result.temporal_facts[0].evidence[0]
    assert locator.quote == IMPLEMENTATION_QUOTE
    assert locator.source_id == "material-evolution"
    assert locator.paragraph == 2
    assert any(
        "evidence_single_object_normalized" in warning
        and "temporal_facts[0].evidence" in warning
        for warning in result.warnings
    )


def test_bare_quote_string_evidence_is_normalized_when_verbatim():
    shape = payload()
    shape["claims"][0]["evidence"] = FORECAST_QUOTE

    result = extract(shape)

    assert result.claims[0].claim_id == "forecast"
    locator = result.claims[0].evidence[0]
    assert locator.quote == FORECAST_QUOTE
    assert locator.source_id == "material-evolution"
    assert locator.paragraph == 3
    assert any(
        "evidence_single_string_normalized" in warning
        and "claims[0].evidence" in warning
        for warning in result.warnings
    )


def test_bare_string_evidence_without_verbatim_match_is_rejected():
    shape = payload()
    shape["claims"][0]["evidence"] = "The analysts expect the rule to grow."

    result = extract(shape)

    assert result.claims == ()
    assert [node.id for node in result.nodes] == ["proposal", "implementation"]
    assert result.temporal_facts[0].object == "implemented"
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "forecast")
    assert gap.gap_type == "evidence_location_failed"
    assert "quote" in gap.detail


# --- Everything else stays fail-closed --------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        # An evidence map keyed by candidate id is not one locator.
        {"proposal": {"quote": PROPOSAL_QUOTE}},
        # Reasoning fields never ride along with a quote.
        {"quote": PROPOSAL_QUOTE, "reasoning": "the proposal paragraph"},
        # An object without a quote has nothing that can be resolved.
        {"source_id": "material-evolution", "paragraph": 1, "page": None},
        {},
        None,
        42,
        True,
    ],
)
def test_other_non_array_evidence_shapes_fail_closed(shape):
    mutated = payload()
    mutated["nodes"][0]["evidence"] = shape

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "candidate_validation_failed"
    assert "must be a JSON array" in gap.detail


def test_blank_string_evidence_fails_closed():
    mutated = payload()
    mutated["nodes"][0]["evidence"] = "   "

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "candidate_validation_failed"


def test_single_object_with_foreign_source_id_is_rejected():
    mutated = payload()
    mutated["nodes"][0]["evidence"] = {
        "source_id": "material-other",
        "quote": PROPOSAL_QUOTE,
        "paragraph": 1,
        "page": None,
    }

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "evidence_location_failed"
    assert "candidate source array" in gap.detail


def test_single_object_with_unverifiable_quote_is_rejected():
    mutated = payload()
    mutated["nodes"][0]["evidence"] = {
        "source_id": "material-evolution",
        "quote": "words absent from the material",
        "paragraph": 1,
        "page": None,
    }

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "evidence_location_failed"
    assert "quote" in gap.detail


def test_list_evidence_stays_strict_for_non_object_items():
    mutated = payload()
    mutated["nodes"][0]["evidence"] = [PROPOSAL_QUOTE]

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "candidate_validation_failed"
    assert "must be a JSON object" in gap.detail


def test_empty_evidence_list_still_fails_closed():
    mutated = payload()
    mutated["nodes"][0]["evidence"] = []

    result = extract(mutated)

    assert [node.id for node in result.nodes] == ["implementation"]
    gap = next(gap for gap in result.evidence_gaps if gap.item_id == "proposal")
    assert gap.gap_type == "candidate_validation_failed"
    assert "must not be empty" in gap.detail
