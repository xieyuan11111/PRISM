"""Focused contracts for the latest confirmed real provider synthesis shapes.

The real debate provider again answers the synthesis phase with the canonical
six top-level sections, but the *item* shapes of the non-empty sections drift
in three confirmed ways (metadata only; no material was read):

  * consensus items name the point text ``statement`` instead of ``text``,
    keep ``evidence_ids`` and attach an overall ``confidence`` value;
  * key_evidence items name the rationale ``text`` instead of ``rationale``
    and keep ``evidence_ids``;
  * falsification_conditions items keep ``condition`` with the canonical
    ``evidence_ids`` field (already supported).

``statement`` extends the confirmed point-text alias family that already
accepts the canonical ``text`` plus ``summary``/``finding`` in the
consensus / disagreements / sources_of_disagreement sections.  Exactly one
text naming is still required per item: an item mixing canonical and alias
names (or two aliases) is ambiguous and rejected, never guessed.  ``summary``
and ``finding`` stay confined to that family, ``question`` to
unresolved_questions and ``condition`` to falsification_conditions.

``confidence`` is confirmed ignored metadata only, like statement
``reasoning`` and key_evidence ``implication``: it is audited with a
field-level warning that names the item index and field but never the value,
and its value never enters DebateSynthesis, later prompts, the ledger
(result or rounds), the serialized result or the report.  In particular it
never substitutes for a citation and never influences classification or
facts.  It is tolerated only on the consensus / disagreements /
sources_of_disagreement point items that carried it; unresolved_questions,
falsification_conditions and key_evidence items still reject it.

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

CONSENSUS_STATEMENT = "Provider consensus statement on the record."
DISAGREEMENT_STATEMENT = "Provider disagreement statement on execution speed."
SOURCE_STATEMENT = "Provider source-of-disagreement statement."
KEY_EVIDENCE_TEXT = "The publication directly records the change."
FALSIFICATION_CONDITION = "A contradicting implementation record would falsify it."
CONFIDENCE_MARKER = "provider-confidence-body-that-must-never-survive"


def latest_provider_synthesis(**overrides) -> dict:
    """Synthesis shaped like the confirmed real provider response.

    Only the three confirmed non-empty sections carry items: consensus uses
    ``statement`` with ``confidence``, key_evidence uses ``text`` with
    ``evidence_ids``, falsification_conditions uses ``condition`` with the
    canonical ``evidence_ids``.  The other sections are empty arrays.
    """
    payload = {
        "consensus": [
            {
                "statement": CONSENSUS_STATEMENT,
                "evidence_ids": ["node-1"],
                "confidence": 0.87,
            }
        ],
        "disagreements": [],
        "sources_of_disagreement": [],
        "key_evidence": [
            {"text": KEY_EVIDENCE_TEXT, "evidence_ids": ["node-1"]}
        ],
        "unresolved_questions": [],
        "falsification_conditions": [
            {"condition": FALSIFICATION_CONDITION, "evidence_ids": ["node-1"]}
        ],
    }
    payload.update(overrides)
    return payload


def run_debate(tmp_path, synthesis_text, question="Q-synthesis-statement"):
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


def test_latest_real_provider_synthesis_shape_completes(tmp_path):
    result, _ = run_debate(tmp_path, json.dumps(latest_provider_synthesis()))

    assert result.status == "completed"
    assert result.fallback_reason is None
    assert result.errors == ()
    assert all(item.status == "available" for item in result.results)

    synthesis = result.synthesis
    assert synthesis is not None
    # statement maps onto the canonical point text with citations intact and
    # confidence changes nothing about the structured fact or its evidence.
    assert synthesis.consensus[0].text == CONSENSUS_STATEMENT
    assert synthesis.consensus[0].evidence_ids == ("node-1",)
    assert synthesis.disagreements == ()
    assert synthesis.sources_of_disagreement == ()
    assert synthesis.unresolved_questions == ()
    # key_evidence.text maps onto the canonical rationale.
    assert synthesis.key_evidence[0].evidence_id == "node-1"
    assert synthesis.key_evidence[0].rationale == KEY_EVIDENCE_TEXT
    # The confirmed falsification shape keeps condition + canonical evidence.
    assert synthesis.falsification_conditions[0].text == FALSIFICATION_CONDITION
    assert synthesis.falsification_conditions[0].evidence_ids == ("node-1",)

    # confidence is audited by field name and item index only.
    assert len(result.warnings) == 1
    joined = "\n".join(result.warnings)
    assert "synthesis.consensus[0]" in joined
    assert "'confidence'" in joined
    assert "0.87" not in joined
    assert CONFIDENCE_MARKER not in joined

    # The canonical serialization never carries the ignored key or value.
    serialized = json.dumps(result_to_dict(result))
    assert '"confidence"' not in serialized
    assert CONFIDENCE_MARKER not in serialized
    assert CONSENSUS_STATEMENT in serialized
    assert KEY_EVIDENCE_TEXT in serialized


def test_confidence_body_never_leaks_into_ledger_or_report(tmp_path):
    result, _ = run_debate(
        tmp_path,
        json.dumps(
            latest_provider_synthesis(
                consensus=[
                    {
                        "statement": CONSENSUS_STATEMENT,
                        "evidence_ids": ["node-1"],
                        "confidence": CONFIDENCE_MARKER,
                    }
                ]
            )
        ),
    )
    assert result.status == "completed"
    assert result.errors == ()
    assert result.synthesis is not None
    assert result.synthesis.consensus[0].text == CONSENSUS_STATEMENT
    joined = "\n".join(result.warnings)
    assert CONFIDENCE_MARKER not in joined

    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert CONFIDENCE_MARKER not in entry.rounds_json
    assert CONFIDENCE_MARKER not in entry.result_json
    assert '"confidence"' not in entry.rounds_json
    assert '"confidence"' not in entry.result_json
    assert CONSENSUS_STATEMENT in entry.result_json

    doc = fixtures.run(
        fixtures.ReportService().report(fixtures.analysis(), debate_result=result)
    )
    assert CONFIDENCE_MARKER not in doc.markdown
    assert "## Debate Interpretation" in doc.markdown
    # The mapped canonical text is interpretation prose, not a structured
    # fact: it never appears in the structured Timeline Stages body.
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert CONFIDENCE_MARKER not in structured_body
    assert CONSENSUS_STATEMENT not in structured_body
    assert KEY_EVIDENCE_TEXT not in structured_body


def test_ledger_replay_preserves_statement_shape_and_warnings(tmp_path):
    payload = json.dumps(latest_provider_synthesis())
    result, _ = run_debate(tmp_path, payload)
    assert result.status == "completed"

    replayed, router = run_debate(tmp_path, payload)
    assert replayed.replayed is True
    assert router.calls == []
    assert replayed == replace(result, replayed=True)
    assert replayed.synthesis == result.synthesis
    assert replayed.warnings == result.warnings


@pytest.mark.parametrize(
    "section, wanted_text",
    [
        ("consensus", CONSENSUS_STATEMENT),
        ("disagreements", DISAGREEMENT_STATEMENT),
        ("sources_of_disagreement", SOURCE_STATEMENT),
    ],
)
def test_statement_alias_maps_in_each_point_family_section(
    tmp_path, section, wanted_text
):
    payload = json.dumps(
        {
            **fixtures.synthesis_payload(),
            section: [
                {
                    "statement": wanted_text,
                    "evidence_ids": ["node-1"],
                    "confidence": 0.9,
                }
            ],
        }
    )
    result, _ = run_debate(tmp_path, payload)
    assert result.status == "completed"
    assert result.errors == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert getattr(synthesis, section)[0].text == wanted_text
    assert getattr(synthesis, section)[0].evidence_ids == ("node-1",)
    joined = "\n".join(result.warnings)
    assert f"synthesis.{section}[0]" in joined
    assert "'confidence'" in joined
    assert "0.9" not in joined


def test_canonical_synthesis_schema_is_unchanged_and_clean(tmp_path):
    result, _ = run_debate(tmp_path, json.dumps(fixtures.synthesis_payload()))
    assert result.status == "completed"
    assert result.errors == ()
    assert result.warnings == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert synthesis.consensus[0].text == (
        "The recorded evidence says the scope narrowed."
    )
    assert synthesis.key_evidence[0].rationale == (
        "The publication directly records the change."
    )
    assert synthesis.falsification_conditions[0].evidence_ids == ("node-1",)
    serialized = json.dumps(result_to_dict(result))
    assert '"confidence"' not in serialized


@pytest.mark.parametrize(
    "section, item",
    [
        # Mixing canonical text and the statement alias is ambiguous.
        (
            "consensus",
            {
                "text": "a",
                "statement": "b",
                "evidence_ids": ["node-1"],
                "confidence": 0.9,
            },
        ),
        # Mixing two aliases is ambiguous too.
        (
            "consensus",
            {
                "finding": "a",
                "statement": "b",
                "evidence_ids": ["node-1"],
                "confidence": 0.9,
            },
        ),
        # confidence never substitutes for the citation field.
        ("consensus", {"statement": "a", "confidence": 0.9}),
        # confidence never substitutes for an actual allowed citation.
        (
            "consensus",
            {"statement": "a", "evidence_ids": [], "confidence": 0.9},
        ),
        # Unknown citations stay rejected even with statement + confidence.
        (
            "consensus",
            {
                "statement": "a",
                "evidence_ids": ["ghost"],
                "confidence": 0.9,
            },
        ),
        # Empty or blank alias text stays invalid.
        (
            "consensus",
            {"statement": "   ", "evidence_ids": ["node-1"], "confidence": 0.9},
        ),
        # Unknown fields stay rejected inside statement-shaped items.
        (
            "consensus",
            {
                "statement": "a",
                "evidence_ids": ["node-1"],
                "confidence": 0.9,
                "bogus_field": 1,
            },
        ),
        # statement is a point-family alias only: it is unknown in the
        # question family.
        (
            "unresolved_questions",
            {"statement": "a", "evidence_ids": ["node-1"]},
        ),
        # statement is unknown in the condition family.
        (
            "falsification_conditions",
            {"statement": "a", "evidence_ids": ["node-1"]},
        ),
        # confidence is tolerated only on the point-family sections; a
        # falsification item carrying it is not a confirmed shape.
        (
            "falsification_conditions",
            {
                "condition": "a",
                "evidence_ids": ["node-1"],
                "confidence": 0.9,
            },
        ),
        # implication stays confined to key_evidence items.
        (
            "consensus",
            {
                "statement": "a",
                "evidence_ids": ["node-1"],
                "confidence": 0.9,
                "implication": "explained",
            },
        ),
        # key_evidence: confidence cannot substitute for the citation.
        ("key_evidence", {"text": "a", "confidence": 0.9}),
        # key_evidence: confidence is not a tolerated extra field at all.
        (
            "key_evidence",
            {"text": "a", "evidence_id": "node-1", "confidence": 0.9},
        ),
        # key_evidence: mixing canonical and alias rationale names is
        # ambiguous.
        (
            "key_evidence",
            {"text": "a", "rationale": "b", "evidence_ids": ["node-1"]},
        ),
        # key_evidence: empty citation list stays invalid.
        ("key_evidence", {"text": "a", "evidence_ids": []}),
        # key_evidence: unknown citations stay invalid.
        ("key_evidence", {"text": "a", "evidence_ids": ["ghost"]}),
        # key_evidence: exactly one allowed id, never two.
        (
            "key_evidence",
            {"text": "a", "evidence_ids": ["node-1", "fact-1"]},
        ),
        # Non-object items stay invalid.
        ("consensus", "just-a-string"),
    ],
)
def test_invalid_statement_shaped_items_degrade_the_whole_synthesis(
    tmp_path, section, item
):
    payload = latest_provider_synthesis()
    payload[section] = [item]
    result, _ = run_debate(tmp_path, json.dumps(payload))
    _assert_conservative_degradation(result)
    joined = "\n".join(result.warnings)
    assert CONFIDENCE_MARKER not in joined
    serialized = json.dumps(result_to_dict(result))
    assert CONFIDENCE_MARKER not in serialized
    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert CONFIDENCE_MARKER not in entry.rounds_json
    assert CONFIDENCE_MARKER not in entry.result_json


def test_statement_shape_rejects_non_array_sections(tmp_path):
    payload = latest_provider_synthesis()
    payload["consensus"] = "not-an-array"
    result, _ = run_debate(tmp_path, json.dumps(payload))
    _assert_conservative_degradation(result)


def test_statement_shape_rejects_duplicate_keys_and_nan(tmp_path):
    raw = json.dumps(latest_provider_synthesis())
    cases = [
        # Duplicate JSON key in the payload (json.dumps separates keys and
        # values with spaces, so the injected duplicate must match).
        raw.replace(
            '"consensus": [', '"consensus": [], "consensus": [', 1
        ),
        # NaN is not a valid JSON value for the alias text field.
        raw.replace(f'"{CONSENSUS_STATEMENT}"', "NaN"),
        # A NaN confidence value is not valid JSON either.
        raw.replace("0.87", "NaN"),
    ]
    for index, text in enumerate(cases):
        # A distinct question per case keeps the ledger replay path from
        # masking the later cases with the first case's recorded result.
        result, _ = run_debate(tmp_path, text, question=f"Q-strict-json-{index}")
        _assert_conservative_degradation(result)
        assert CONFIDENCE_MARKER not in json.dumps(result_to_dict(result))
