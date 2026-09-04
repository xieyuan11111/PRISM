"""Focused regression for the real provider's summary synthesis shape."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_debate as fixtures

from prism.debate import DebateLedger, result_to_dict

KEY_SUMMARY = "Provider summary must stay explanatory."
IMPLICATION_MARKER = "provider-implication-text-that-must-never-become-a-fact"


def test_real_summary_synthesis_shape_completes(tmp_path):
    payload = {
        "consensus": [
            {"summary": "The scope narrowed on the record.", "evidence_ids": ["node-1"]}
        ],
        "disagreements": [
            {
                "summary": "The perspectives differ on execution speed.",
                "evidence_ids": ["node-1"],
            }
        ],
        "sources_of_disagreement": [
            {
                "summary": "The difference concerns execution capacity.",
                "evidence_ids": ["node-1"],
            }
        ],
        "key_evidence": [
            {"summary": "The publication records the change.", "evidence_ids": ["node-1"]}
        ],
        "unresolved_questions": [
            {
                "question": "What is the enforcement effect?",
                "evidence_ids": ["node-1"],
            }
        ],
        "falsification_conditions": [
            {
                "condition": "A contradicting implementation record falsifies it.",
                "evidence_ids": ["node-1"],
            }
        ],
    }
    result = fixtures.run(
        fixtures.service(
            tmp_path,
            fixtures.ScriptedRouter(synthesis=json.dumps(payload)),
        ).debate(fixtures.CASE_ID, "Q-summary-shape", fixtures.AS_OF)
    )

    assert result.status == "completed"
    assert result.fallback_reason is None
    assert result.errors == ()
    synthesis = result.synthesis
    assert synthesis is not None
    assert synthesis.consensus[0].text == "The scope narrowed on the record."
    assert synthesis.disagreements[0].text == (
        "The perspectives differ on execution speed."
    )
    assert synthesis.sources_of_disagreement[0].text == (
        "The difference concerns execution capacity."
    )
    assert synthesis.key_evidence[0].evidence_id == "node-1"
    assert synthesis.key_evidence[0].rationale == "The publication records the change."
    assert synthesis.unresolved_questions[0].text == "What is the enforcement effect?"
    assert synthesis.falsification_conditions[0].text == (
        "A contradicting implementation record falsifies it."
    )


def summary_payload(*, implication: bool = False) -> dict:
    key_evidence = {
        "summary": KEY_SUMMARY,
        "evidence_ids": ["node-1"],
    }
    if implication:
        key_evidence["implication"] = IMPLICATION_MARKER
    return {
        "consensus": [
            {
                "summary": "The scope narrowed on the record.",
                "evidence_ids": ["node-1"],
            }
        ],
        "disagreements": [
            {
                "summary": "The perspectives differ on execution speed.",
                "evidence_ids": ["node-1"],
            }
        ],
        "sources_of_disagreement": [
            {
                "summary": "The difference concerns execution capacity.",
                "evidence_ids": ["node-1"],
            }
        ],
        "key_evidence": [key_evidence],
        "unresolved_questions": [
            {
                "question": "What is the enforcement effect?",
                "evidence_ids": ["node-1"],
            }
        ],
        "falsification_conditions": [
            {
                "condition": "A contradicting implementation record falsifies it.",
                "evidence_ids": ["node-1"],
            }
        ],
    }


def run_summary_debate(tmp_path, synthesis_text, question="Q-summary-shape"):
    return fixtures.run(
        fixtures.service(
            tmp_path,
            fixtures.ScriptedRouter(synthesis=synthesis_text),
        ).debate(fixtures.CASE_ID, question, fixtures.AS_OF)
    )


def _assert_conservative_degradation(result):
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
    assert result.errors
    assert result.errors[-1].phase == "synthesis"
    assert result.errors[-1].error_code == "invalid_output"
    assert all(item.status == "available" for item in result.results)


def test_summary_is_explanatory_and_ignored_metadata_never_leaks(tmp_path):
    result = run_summary_debate(
        tmp_path, json.dumps(summary_payload(implication=True))
    )

    assert result.status == "completed"
    assert result.errors == ()
    assert result.synthesis is not None
    assert result.synthesis.key_evidence[0].rationale == KEY_SUMMARY

    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "synthesis.key_evidence[0]" in warning
    assert "'implication'" in warning
    assert IMPLICATION_MARKER not in warning

    serialized = json.dumps(result_to_dict(result))
    assert IMPLICATION_MARKER not in serialized
    assert KEY_SUMMARY in serialized

    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert IMPLICATION_MARKER not in entry.rounds_json
    assert IMPLICATION_MARKER not in entry.result_json

    doc = fixtures.run(
        fixtures.ReportService().report(fixtures.analysis(), debate_result=result)
    )
    assert IMPLICATION_MARKER not in doc.markdown
    assert KEY_SUMMARY in doc.markdown
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert IMPLICATION_MARKER not in structured_body
    assert KEY_SUMMARY not in structured_body

    replayed = run_summary_debate(
        tmp_path,
        json.dumps(summary_payload(implication=True)),
    )
    assert replayed.replayed is True
    assert replayed == replace(result, replayed=True)
    assert replayed.evidence_bundle_hash == result.evidence_bundle_hash
    assert replayed.warnings == result.warnings


@pytest.mark.parametrize(
    "section, item",
    [
        (
            "consensus",
            {"summary": "x", "text": "y", "evidence_ids": ["node-1"]},
        ),
        (
            "consensus",
            {"summary": "x", "finding": "y", "evidence_ids": ["node-1"]},
        ),
        (
            "consensus",
            {
                "summary": "x",
                "evidence_ids": ["node-1"],
                "bogus_field": "ignored",
            },
        ),
        ("consensus", {"summary": "x"}),
        ("consensus", {"summary": "   ", "evidence_ids": ["node-1"]}),
        ("consensus", {"summary": "x", "evidence_ids": []}),
        ("consensus", {"summary": "x", "evidence_ids": ["ghost"]}),
        (
            "unresolved_questions",
            {"question": "x", "summary": "y", "evidence_ids": ["node-1"]},
        ),
        (
            "unresolved_questions",
            {
                "question": "x",
                "evidence_ids": ["node-1"],
                "relevant_evidence_ids": ["node-1"],
            },
        ),
        (
            "falsification_conditions",
            {"condition": "x", "summary": "y", "evidence_ids": ["node-1"]},
        ),
        (
            "key_evidence",
            {
                "summary": "x",
                "evidence_id": "node-1",
                "evidence_ids": ["node-1"],
            },
        ),
        (
            "key_evidence",
            {"summary": "x", "rationale": "y", "evidence_id": "node-1"},
        ),
        ("key_evidence", {"summary": "x"}),
        ("key_evidence", {"summary": "x", "evidence_ids": []}),
        ("key_evidence", {"summary": "x", "evidence_ids": ["ghost"]}),
        (
            "key_evidence",
            {"summary": "x", "evidence_ids": ["node-1", "node-1"]},
        ),
        (
            "key_evidence",
            {
                "summary": "x",
                "evidence_ids": ["node-1"],
                "bogus_field": "ignored",
            },
        ),
    ],
)

def test_summary_shape_stays_strict_and_degrades_whole_synthesis(
    tmp_path, section, item
):
    payload = summary_payload()
    payload[section] = [item]
    result = run_summary_debate(tmp_path, json.dumps(payload))

    _assert_conservative_degradation(result)
    assert IMPLICATION_MARKER not in json.dumps(result_to_dict(result))
    assert IMPLICATION_MARKER not in "\n".join(result.warnings)


def test_summary_shape_rejects_duplicate_keys_and_nan(tmp_path):
    raw = json.dumps(summary_payload())
    cases = [
        raw.replace(
            '"consensus": [', '"consensus": [], "consensus": [', 1
        ),
        raw.replace('"The scope narrowed on the record."', "NaN"),
    ]
    for index, text in enumerate(cases):
        result = run_summary_debate(
            tmp_path, text, question=f"Q-summary-strict-json-{index}"
        )
        _assert_conservative_degradation(result)
        assert IMPLICATION_MARKER not in json.dumps(result_to_dict(result))
