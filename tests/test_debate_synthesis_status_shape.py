"""Focused contracts for the last confirmed real provider synthesis metadata.

The real debate provider answers the synthesis phase with the canonical six
top-level sections, but in this case only ``unresolved_questions`` is
non-empty; its items carry the confirmed shape ``question`` + canonical
``evidence_ids`` plus an extra ``status`` field (e.g. ``"unanswered"``).

``status`` is the last confirmed ignored metadata, like statement
``reasoning``, key_evidence ``implication`` and point-family ``confidence``:
it is tolerated only on unresolved_questions items, its value must be a
non-empty string (any other value invalidates the item), and it is audited
with a field-level warning that names the item index and the field but never
the value.  The value never enters SynthesisPoint, DebateSynthesis, later
prompts, the ledger (result or rounds), the serialized result or the report,
and it never substitutes for the question text or for a citation.  It is not
tolerated on any other section and not at the synthesis top level; the
canonical item schema stays authoritative and unchanged.

Everything else stays strict: unknown fields, mixed text or evidence naming,
empty or unknown citations, empty text, non-arrays, duplicate keys and NaN
still invalidate the whole synthesis and trigger the deterministic
conservative fallback.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_debate as fixtures

from prism.debate import DebateLedger, result_to_dict

UNRESOLVED_QUESTION = "Provider open question on enforcement effects."
STATUS_MARKER = "provider-status-body-that-must-never-become-a-fact"
SECOND_QUESTION = "Provider second open question on retroactive reach."


def real_provider_synthesis(**overrides) -> dict:
    """Synthesis shaped like the confirmed real provider response.

    Only unresolved_questions is non-empty in this case; its items use the
    confirmed ``question`` + canonical ``evidence_ids`` shape and carry an
    ignored ``status`` metadata field.  All other sections are empty arrays.
    """
    payload = {
        "consensus": [],
        "disagreements": [],
        "sources_of_disagreement": [],
        "key_evidence": [],
        "unresolved_questions": [
            {
                "question": UNRESOLVED_QUESTION,
                "evidence_ids": ["node-1"],
                "status": "unanswered",
            }
        ],
        "falsification_conditions": [],
    }
    payload.update(overrides)
    return payload


def run_debate(tmp_path, synthesis_text, question="Q-synthesis-status"):
    router = fixtures.ScriptedRouter(synthesis=synthesis_text)
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, question, fixtures.AS_OF
        )
    )
    return result, router


def _assert_conservative_degradation(result):
    """The whole synthesis must fall back, never keep partially parsed points."""
    assert result.status == "degraded"
    assert result.fallback_reason == (
        "synthesis invalid; deterministic conservative summary used"
    )
    assert result.synthesis is not None
    assert result.synthesis.consensus == ()
    assert result.synthesis.disagreements == ()
    assert result.synthesis.sources_of_disagreement == ()
    assert result.synthesis.falsification_conditions == ()
    assert len(result.synthesis.key_evidence) == 1
    assert len(result.synthesis.unresolved_questions) == 1
    assert result.synthesis.unresolved_questions[0].evidence_ids == (
        result.synthesis.key_evidence[0].evidence_id,
    )
    assert all(
        point.evidence_ids for point in result.synthesis.unresolved_questions
    )
    assert result.errors
    assert result.errors[-1].phase == "synthesis"
    assert result.errors[-1].error_code == "invalid_output"
    assert all(item.status == "available" for item in result.results)


def test_real_provider_unresolved_status_shape_completes(tmp_path):
    result, _ = run_debate(tmp_path, json.dumps(real_provider_synthesis()))

    assert result.status == "completed"
    assert result.fallback_reason is None
    assert result.errors == ()
    assert all(item.status == "available" for item in result.results)

    synthesis = result.synthesis
    assert synthesis is not None
    # Only the unresolved question survives from the model output; the other
    # sections are legitimately empty and nothing is fabricated for them.
    assert synthesis.consensus == ()
    assert synthesis.disagreements == ()
    assert synthesis.sources_of_disagreement == ()
    assert synthesis.key_evidence == ()
    assert synthesis.falsification_conditions == ()
    assert len(synthesis.unresolved_questions) == 1
    # question maps onto the canonical point text with citations intact and
    # status changes nothing about the structured fact or its evidence.
    assert synthesis.unresolved_questions[0].text == UNRESOLVED_QUESTION
    assert synthesis.unresolved_questions[0].evidence_ids == ("node-1",)

    # status is audited by field name and item index only; the value never
    # survives in the warning.
    assert len(result.warnings) == 1
    joined = "\n".join(result.warnings)
    assert "synthesis.unresolved_questions[0]" in joined
    assert "'status'" in joined
    assert "unanswered" not in joined
    assert STATUS_MARKER not in joined

    # The canonical serialization never carries the ignored key or value and
    # the mapped question stays canonical text + evidence_ids only.
    serialized = json.dumps(result_to_dict(result))
    assert STATUS_MARKER not in serialized
    raw_synthesis = json.loads(serialized)["synthesis"]
    assert raw_synthesis["unresolved_questions"] == [
        {"text": UNRESOLVED_QUESTION, "evidence_ids": ["node-1"]}
    ]
    assert UNRESOLVED_QUESTION in serialized


def test_status_body_never_leaks_into_ledger_or_report(tmp_path):
    payload = real_provider_synthesis(
        unresolved_questions=[
            {
                "question": UNRESOLVED_QUESTION,
                "evidence_ids": ["node-1"],
                "status": STATUS_MARKER,
            }
        ]
    )
    result, _ = run_debate(tmp_path, json.dumps(payload))
    assert result.status == "completed"
    assert result.errors == ()
    assert result.synthesis is not None
    assert result.synthesis.unresolved_questions[0].text == UNRESOLVED_QUESTION
    joined = "\n".join(result.warnings)
    assert "synthesis.unresolved_questions[0]" in joined
    assert "'status'" in joined
    assert STATUS_MARKER not in joined

    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert STATUS_MARKER not in entry.rounds_json
    assert STATUS_MARKER not in entry.result_json
    # No synthesis item anywhere in the ledger carries a status key.
    rounds = json.loads(entry.rounds_json)
    recorded = [
        round_output["output"]
        for round_output in rounds
        if round_output["phase"] == "synthesis"
    ]
    assert len(recorded) == 1
    synthesis_docs = [recorded[0], json.loads(entry.result_json)["synthesis"]]
    for synthesis in synthesis_docs:
        for items in (
            synthesis["consensus"],
            synthesis["disagreements"],
            synthesis["sources_of_disagreement"],
            synthesis["unresolved_questions"],
            synthesis["falsification_conditions"],
        ):
            assert all(set(item) == {"text", "evidence_ids"} for item in items)
    assert UNRESOLVED_QUESTION in entry.result_json

    doc = fixtures.run(
        fixtures.ReportService().report(fixtures.analysis(), debate_result=result)
    )
    assert STATUS_MARKER not in doc.markdown
    assert "## Debate Interpretation" in doc.markdown
    # The mapped canonical question is interpretation prose, not a structured
    # fact: it never appears in the structured Timeline Stages body.
    assert UNRESOLVED_QUESTION in doc.markdown
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert STATUS_MARKER not in structured_body
    assert UNRESOLVED_QUESTION not in structured_body


def test_ledger_replay_preserves_status_shape_and_warnings(tmp_path):
    payload = json.dumps(real_provider_synthesis())
    result, _ = run_debate(tmp_path, payload)
    assert result.status == "completed"

    replayed, router = run_debate(tmp_path, payload)
    assert replayed.replayed is True
    assert router.calls == []
    assert replayed == replace(result, replayed=True)
    assert replayed.synthesis == result.synthesis
    assert replayed.warnings == result.warnings


@pytest.mark.parametrize(
    "item",
    [
        # The confirmed real provider item shape.
        {
            "question": UNRESOLVED_QUESTION,
            "evidence_ids": ["node-1"],
            "status": "unanswered",
        },
        # Canonical text naming stays authoritative beside the metadata.
        {
            "text": UNRESOLVED_QUESTION,
            "evidence_ids": ["node-1"],
            "status": "unanswered",
        },
        # The confirmed alias citation field also stays compatible.
        {
            "question": UNRESOLVED_QUESTION,
            "relevant_evidence_ids": ["node-1"],
            "status": "unanswered",
        },
    ],
)
def test_status_is_orthogonal_to_the_confirmed_text_and_citation_shapes(
    tmp_path, item
):
    result, _ = run_debate(
        tmp_path,
        json.dumps(
            real_provider_synthesis(unresolved_questions=[dict(item)])
        ),
        question=f"Q-status-combo-{item.get('text') is not None}-"
        f"{'relevant_evidence_ids' in item}",
    )
    assert result.status == "completed"
    assert result.errors == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert synthesis.unresolved_questions[0].text == UNRESOLVED_QUESTION
    assert synthesis.unresolved_questions[0].evidence_ids == ("node-1",)
    joined = "\n".join(result.warnings)
    assert "synthesis.unresolved_questions[0]" in joined
    assert "'status'" in joined


def test_status_warnings_name_each_item_index(tmp_path):
    result, _ = run_debate(
        tmp_path,
        json.dumps(
            real_provider_synthesis(
                unresolved_questions=[
                    {
                        "question": UNRESOLVED_QUESTION,
                        "evidence_ids": ["node-1"],
                        "status": "unanswered",
                    },
                    {
                        "question": SECOND_QUESTION,
                        "evidence_ids": ["node-1"],
                        "status": "answered_later",
                    },
                ]
            )
        ),
    )
    assert result.status == "completed"
    assert result.errors == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert [item.text for item in synthesis.unresolved_questions] == [
        UNRESOLVED_QUESTION,
        SECOND_QUESTION,
    ]
    joined = "\n".join(result.warnings)
    assert joined.count("'status'") == 2
    assert "synthesis.unresolved_questions[0]" in joined
    assert "synthesis.unresolved_questions[1]" in joined
    assert "unanswered" not in joined
    assert "answered_later" not in joined


@pytest.mark.parametrize(
    "status_value",
    [
        "",
        "   ",
        1,
        0.5,
        True,
        None,
        [],
        {},
        {"value": "unanswered"},
    ],
)
def test_status_value_must_be_a_non_empty_string(tmp_path, status_value):
    result, _ = run_debate(
        tmp_path,
        json.dumps(
            real_provider_synthesis(
                unresolved_questions=[
                    {
                        "question": UNRESOLVED_QUESTION,
                        "evidence_ids": ["node-1"],
                        "status": status_value,
                    }
                ]
            )
        ),
        question=f"Q-status-value-{type(status_value).__name__}-"
        f"{str(status_value)[:12]}",
    )
    _assert_conservative_degradation(result)


@pytest.mark.parametrize(
    "section, item",
    [
        # status is not a confirmed field on any point-family section.
        (
            "consensus",
            {"text": "a", "evidence_ids": ["node-1"], "status": "open"},
        ),
        (
            "disagreements",
            {"text": "a", "evidence_ids": ["node-1"], "status": "open"},
        ),
        (
            "sources_of_disagreement",
            {"text": "a", "evidence_ids": ["node-1"], "status": "open"},
        ),
        # status is not a confirmed field on key_evidence items.
        (
            "key_evidence",
            {
                "evidence_id": "node-1",
                "rationale": "a",
                "status": "open",
            },
        ),
        # status is not a confirmed field on the condition family either.
        (
            "falsification_conditions",
            {"condition": "a", "evidence_ids": ["node-1"], "status": "open"},
        ),
        # status does not substitute for the question text.
        (
            "unresolved_questions",
            {"evidence_ids": ["node-1"], "status": "open"},
        ),
        # status does not substitute for a citation.
        (
            "unresolved_questions",
            {"question": "a", "status": "open"},
        ),
        # Unknown fields beside the confirmed status stay rejected.
        (
            "unresolved_questions",
            {
                "question": "a",
                "evidence_ids": ["node-1"],
                "status": "open",
                "bogus_field": 1,
            },
        ),
        # Empty or blank question text stays invalid with status present.
        (
            "unresolved_questions",
            {
                "question": "   ",
                "evidence_ids": ["node-1"],
                "status": "open",
            },
        ),
        # Empty citation arrays stay invalid with status present.
        (
            "unresolved_questions",
            {
                "question": "a",
                "evidence_ids": [],
                "status": "open",
            },
        ),
        # Unknown citations stay invalid with status present.
        (
            "unresolved_questions",
            {
                "question": "a",
                "evidence_ids": ["ghost"],
                "status": "open",
            },
        ),
    ],
)
def test_status_is_confined_to_unresolved_questions_and_never_substitutes(
    tmp_path, section, item
):
    payload = real_provider_synthesis()
    payload[section] = [item]
    result, _ = run_debate(
        tmp_path, json.dumps(payload), question=f"Q-status-confine-{section}"
    )
    _assert_conservative_degradation(result)
    serialized = json.dumps(result_to_dict(result))
    assert STATUS_MARKER not in serialized
    assert "unanswered" not in serialized


def test_canonical_synthesis_schema_is_unchanged_and_clean(tmp_path):
    result, _ = run_debate(
        tmp_path, json.dumps(fixtures.synthesis_payload())
    )
    assert result.status == "completed"
    assert result.errors == ()
    assert result.warnings == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert synthesis.unresolved_questions[0].text == (
        "No recorded evidence measures the enforcement effect."
    )
    assert synthesis.unresolved_questions[0].evidence_ids == ("node-1",)
    serialized = json.dumps(result_to_dict(result))
    assert "'status'" not in serialized


def test_status_shape_rejects_top_level_status_and_non_array_sections(tmp_path):
    for override in (
        {"status": "open"},
        {"consensus": "not-an-array"},
        {"unresolved_questions": "not-an-array"},
    ):
        payload = real_provider_synthesis()
        payload.update(override)
        result, _ = run_debate(tmp_path, json.dumps(payload))
        _assert_conservative_degradation(result)


def test_status_shape_rejects_duplicate_keys_and_nan(tmp_path):
    raw = json.dumps(real_provider_synthesis())
    cases = [
        # Duplicate JSON key in the payload (json.dumps separates keys and
        # values with spaces, so the injected duplicate must match).
        raw.replace(
            '"consensus": [],', '"consensus": [], "consensus": [],', 1
        ),
        # NaN is not a valid JSON value for the question text.
        raw.replace(f'"{UNRESOLVED_QUESTION}"', "NaN"),
        # A NaN status value is not valid JSON either.
        raw.replace('"unanswered"', "NaN"),
    ]
    for index, text in enumerate(cases):
        # A distinct question per case keeps the ledger replay path from
        # masking the later cases with the first case's recorded result.
        result, _ = run_debate(
            tmp_path, text, question=f"Q-status-strict-json-{index}"
        )
        _assert_conservative_degradation(result)
        assert STATUS_MARKER not in json.dumps(result_to_dict(result))
