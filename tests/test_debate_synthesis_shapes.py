"""Focused contracts for real provider synthesis shapes (M2 fix).

A real debate provider answers the synthesis phase with the canonical six
sections but drifts the *item* field names in four confirmed, non-critical
ways:

  * consensus / disagreements / sources_of_disagreement items name the point
    ``finding`` instead of ``text`` (their citations still use
    ``evidence_ids``);
  * key_evidence items name the rationale ``finding`` and attach an
    ``implication`` explanation field;
  * unresolved_questions items use ``question`` and ``relevant_evidence_ids``;
  * falsification_conditions items use ``condition`` and
    ``relevant_evidence_ids``.

The canonical item schema stays authoritative: every item must follow exactly
one naming scheme, the confirmed aliases only ever land in the canonical
structured fields, and anything else (unknown fields, mixed canonical+alias
items, unknown or missing citations, empty text, non-arrays, duplicate keys,
NaN) still invalidates the whole synthesis and triggers the existing
deterministic conservative fallback.  ``implication`` is explanation metadata
only: it is ignored with a field-level warning that names the field and index
but never carries the model's implication text, and the text never enters
structured facts, the ledger, the serialized result or the report.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_debate as fixtures

from prism.debate import DebateLedger, result_to_dict

IMPLICATION_MARKER = "provider-implication-text-that-must-never-become-a-fact"
REASONING_MARKER = fixtures.REASONING_MARKER

CONSENSUS_FINDING = "Provider consensus finding on the record."
DISAGREEMENT_FINDING = "Provider disagreement finding on execution speed."
SOURCE_FINDING = "Provider source-of-disagreement finding."
KEY_EVIDENCE_FINDING = "Provider key-evidence rationale on the publication."
UNRESOLVED_QUESTION = "Provider open question on enforcement effects."
FALSIFICATION_CONDITION = "Provider falsification condition on implementation."


def alias_synthesis_payload(**overrides):
    """All four confirmed real-provider alias shapes in one payload."""
    payload = {
        "consensus": [
            {"finding": CONSENSUS_FINDING, "evidence_ids": ["node-1"]}
        ],
        "disagreements": [
            {"finding": DISAGREEMENT_FINDING, "evidence_ids": ["node-1"]}
        ],
        "sources_of_disagreement": [
            {"finding": SOURCE_FINDING, "evidence_ids": ["node-1"]}
        ],
        "key_evidence": [
            {
                "evidence_id": "node-1",
                "finding": KEY_EVIDENCE_FINDING,
                "implication": IMPLICATION_MARKER,
            }
        ],
        "unresolved_questions": [
            {
                "question": UNRESOLVED_QUESTION,
                "relevant_evidence_ids": ["node-1"],
            }
        ],
        "falsification_conditions": [
            {
                "condition": FALSIFICATION_CONDITION,
                "relevant_evidence_ids": ["node-1"],
            }
        ],
    }
    payload.update(overrides)
    return payload


def run_debate(tmp_path, synthesis_text, question="Q-synthesis-shape"):
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


def test_real_provider_alias_synthesis_parses_to_completed(tmp_path):
    result, router = run_debate(
        tmp_path, json.dumps(alias_synthesis_payload())
    )

    assert result.status == "completed"
    assert result.fallback_reason is None
    assert result.errors == ()
    assert all(item.status == "available" for item in result.results)

    synthesis = result.synthesis
    assert synthesis is not None
    # Alias text lands in the canonical text field with citations intact.
    assert synthesis.consensus[0].text == CONSENSUS_FINDING
    assert synthesis.consensus[0].evidence_ids == ("node-1",)
    assert synthesis.disagreements[0].text == DISAGREEMENT_FINDING
    assert synthesis.sources_of_disagreement[0].text == SOURCE_FINDING
    assert synthesis.unresolved_questions[0].text == UNRESOLVED_QUESTION
    assert synthesis.unresolved_questions[0].evidence_ids == ("node-1",)
    assert synthesis.falsification_conditions[0].text == FALSIFICATION_CONDITION
    assert synthesis.falsification_conditions[0].evidence_ids == ("node-1",)
    # key_evidence.finding maps to the canonical rationale field.
    assert synthesis.key_evidence[0].evidence_id == "node-1"
    assert synthesis.key_evidence[0].rationale == KEY_EVIDENCE_FINDING

    # The implication field is explanation metadata: it is audited by field
    # name and index only, and its body never survives anywhere.
    assert len(result.warnings) == 1
    joined = "\n".join(result.warnings)
    assert "synthesis.key_evidence[0]" in joined
    assert "'implication'" in joined
    assert IMPLICATION_MARKER not in joined

    serialized = json.dumps(result_to_dict(result))
    assert IMPLICATION_MARKER not in serialized
    assert KEY_EVIDENCE_FINDING in serialized
    assert CONSENSUS_FINDING in serialized

    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert IMPLICATION_MARKER not in entry.rounds_json
    assert IMPLICATION_MARKER not in entry.result_json
    assert KEY_EVIDENCE_FINDING in entry.result_json

    # Ledger replay preserves the same canonical result and its warnings.
    from dataclasses import replace as dataclass_replace

    replayed = fixtures.run(
        fixtures.service(tmp_path, fixtures.ScriptedRouter(
            synthesis=json.dumps(alias_synthesis_payload())
        )).debate(fixtures.CASE_ID, "Q-synthesis-shape", fixtures.AS_OF)
    )
    assert replayed.replayed is True
    assert replayed == dataclass_replace(result, replayed=True)
    assert replayed.warnings == result.warnings


def test_report_never_shows_implication_metadata(tmp_path):
    result, _ = run_debate(tmp_path, json.dumps(alias_synthesis_payload()))
    assert result.status == "completed"

    doc = fixtures.run(
        fixtures.ReportService().report(fixtures.analysis(), debate_result=result)
    )
    assert IMPLICATION_MARKER not in doc.markdown
    assert "## Debate Interpretation" in doc.markdown
    # The mapped canonical rationale is interpretation, not a structured fact.
    assert KEY_EVIDENCE_FINDING in doc.markdown
    structured_body = doc.markdown.split("## Timeline Stages", 1)[1]
    assert IMPLICATION_MARKER not in structured_body
    assert KEY_EVIDENCE_FINDING not in structured_body


def test_each_real_alias_form_maps_inside_an_otherwise_canonical_synthesis(
    tmp_path,
):
    canonical = fixtures.synthesis_payload()
    cases = [
        (
            "consensus",
            {
                "consensus": [
                    {"finding": CONSENSUS_FINDING, "evidence_ids": ["node-1"]}
                ]
            },
            ("consensus", CONSENSUS_FINDING),
            False,
        ),
        (
            "disagreements",
            {
                "disagreements": [
                    {
                        "finding": DISAGREEMENT_FINDING,
                        "evidence_ids": ["node-1"],
                    }
                ]
            },
            ("disagreements", DISAGREEMENT_FINDING),
            False,
        ),
        (
            "sources_of_disagreement",
            {
                "sources_of_disagreement": [
                    {"finding": SOURCE_FINDING, "evidence_ids": ["node-1"]}
                ]
            },
            ("sources_of_disagreement", SOURCE_FINDING),
            False,
        ),
        (
            "key_evidence",
            {
                "key_evidence": [
                    {
                        "evidence_id": "node-1",
                        "finding": KEY_EVIDENCE_FINDING,
                        "implication": IMPLICATION_MARKER,
                    }
                ]
            },
            ("key_evidence", KEY_EVIDENCE_FINDING),
            True,
        ),
        (
            "unresolved_questions",
            {
                "unresolved_questions": [
                    {
                        "question": UNRESOLVED_QUESTION,
                        "relevant_evidence_ids": ["node-1"],
                    }
                ]
            },
            ("unresolved_questions", UNRESOLVED_QUESTION),
            False,
        ),
        (
            "falsification_conditions",
            {
                "falsification_conditions": [
                    {
                        "condition": FALSIFICATION_CONDITION,
                        "relevant_evidence_ids": ["node-1"],
                    }
                ]
            },
            ("falsification_conditions", FALSIFICATION_CONDITION),
            False,
        ),
    ]
    for index, (section, override, (wanted_section, wanted_text), warns) in enumerate(
        cases
    ):
        payload = json.dumps({**canonical, **override})
        result, _ = run_debate(
            tmp_path, payload, question=f"Q-alias-{section}-{index}"
        )
        assert result.status == "completed"
        assert result.errors == ()
        synthesis = result.synthesis
        assert synthesis is not None
        if wanted_section == "key_evidence":
            assert synthesis.key_evidence[0].rationale == wanted_text
            assert synthesis.key_evidence[0].evidence_id == "node-1"
        else:
            assert getattr(synthesis, wanted_section)[0].text == wanted_text
            assert getattr(synthesis, wanted_section)[0].evidence_ids == (
                "node-1",
            )
        joined = "\n".join(result.warnings)
        if warns:
            assert "'implication'" in joined
            assert IMPLICATION_MARKER not in joined
        else:
            assert result.warnings == ()


def test_canonical_synthesis_schema_is_unchanged_and_clean(tmp_path):
    payload = fixtures.synthesis_payload()
    result, _ = run_debate(
        tmp_path, json.dumps(payload), question="Q-canonical-still"
    )
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
    assert synthesis.unresolved_questions[0].evidence_ids == ("node-1",)


@pytest.mark.parametrize(
    "section, item",
    [
        # Unknown fields stay rejected inside alias items.
        (
            "consensus",
            {"finding": "x", "evidence_ids": ["node-1"], "bogus_field": 1},
        ),
        # Mixed canonical text and alias text in one item is ambiguous.
        (
            "consensus",
            {"text": "x", "finding": "y", "evidence_ids": ["node-1"]},
        ),
        # Mixed canonical and alias citation names are ambiguous.
        (
            "unresolved_questions",
            {
                "question": "x",
                "evidence_ids": ["node-1"],
                "relevant_evidence_ids": ["node-1"],
            },
        ),
        # A point item with no text of either scheme is incomplete.
        ("consensus", {"evidence_ids": ["node-1"]}),
        # Alias text fields are section-specific: finding is unknown here.
        (
            "unresolved_questions",
            {"finding": "x", "evidence_ids": ["node-1"]},
        ),
        # implication is tolerated only on key_evidence items; a point item
        # carrying it is not a confirmed shape.
        (
            "consensus",
            {
                "finding": "x",
                "evidence_ids": ["node-1"],
                "implication": IMPLICATION_MARKER,
            },
        ),
        # Non-object items stay invalid.
        ("consensus", "just-a-string"),
    ],
)
def test_invalid_alias_points_degrade_the_whole_synthesis(tmp_path, section, item):
    payload = alias_synthesis_payload()
    payload[section] = [item]
    result, _ = run_debate(tmp_path, json.dumps(payload))
    _assert_conservative_degradation(result)
    # The rejected model content never leaks into the audit or the result.
    serialized = json.dumps(result_to_dict(result))
    assert IMPLICATION_MARKER not in serialized
    assert IMPLICATION_MARKER not in "\n".join(result.warnings)
    ledger = DebateLedger(tmp_path / "index.db")
    entry = ledger.entries(fixtures.CASE_ID)[0]
    assert IMPLICATION_MARKER not in entry.rounds_json
    assert IMPLICATION_MARKER not in entry.result_json


@pytest.mark.parametrize(
    "section, item",
    [
        # Missing citations entirely.
        ("consensus", {"finding": "x"}),
        # Empty citation arrays leave the point without evidence.
        ("consensus", {"finding": "x", "evidence_ids": []}),
        # Unknown citations still reject alias items.
        ("consensus", {"finding": "x", "evidence_ids": ["ghost"]}),
        # Empty or blank text stays invalid.
        ("consensus", {"finding": "   ", "evidence_ids": ["node-1"]}),
        # Missing alias citations in unresolved_questions.
        (
            "unresolved_questions",
            {"question": "x", "evidence_ids": []},
        ),
        # Unknown citations through the alias citation field.
        (
            "falsification_conditions",
            {"condition": "x", "relevant_evidence_ids": ["ghost"]},
        ),
        # key_evidence requires exactly one rationale scheme and a known id.
        ("key_evidence", {"evidence_id": "ghost", "finding": "x"}),
        ("key_evidence", {"evidence_id": "node-1"}),
        (
            "key_evidence",
            {"evidence_id": "node-1", "rationale": "a", "finding": "b"},
        ),
        (
            "key_evidence",
            {"evidence_id": "node-1", "finding": "x", "bogus_field": 1},
        ),
        # Alias evidence fields are section-specific.
        (
            "consensus",
            {"finding": "x", "relevant_evidence_ids": ["node-1"]},
        ),
    ],
)
def test_alias_forms_still_require_allowed_citations_and_text(
    tmp_path, section, item
):
    payload = alias_synthesis_payload()
    payload[section] = [item]
    result, _ = run_debate(tmp_path, json.dumps(payload))
    _assert_conservative_degradation(result)


def test_alias_synthesis_non_array_sections_are_rejected(tmp_path):
    for section in (
        "consensus",
        "disagreements",
        "sources_of_disagreement",
        "key_evidence",
        "unresolved_questions",
        "falsification_conditions",
    ):
        payload = alias_synthesis_payload()
        payload[section] = "not-an-array"
        result, _ = run_debate(
            tmp_path, json.dumps(payload), question=f"Q-nonarray-{section}"
        )
        _assert_conservative_degradation(result)


def test_alias_synthesis_duplicate_keys_and_nan_still_reject(tmp_path):
    raw = json.dumps(alias_synthesis_payload())
    cases = [
        # Duplicate JSON key in the alias payload (json.dumps separates keys
        # and values with spaces, so the injected duplicate must match).
        raw.replace(
            '"consensus": [', '"consensus": [], "consensus": [', 1
        ),
        # NaN is not a valid JSON value for an alias text field: replace the
        # quoted alias text with the bare constant so the loader rejects it.
        raw.replace(f'"{CONSENSUS_FINDING}"', "NaN"),
    ]
    for index, text in enumerate(cases):
        # A distinct question per case keeps the ledger replay path from
        # masking the second case with the first case's recorded result.
        result, _ = run_debate(
            tmp_path, text, question=f"Q-strict-json-{index}"
        )
        _assert_conservative_degradation(result)
        assert IMPLICATION_MARKER not in json.dumps(result_to_dict(result))


def test_academic_discourse_uses_academic_profiles_with_real_synthesis(tmp_path):
    class AcademicRouter(fixtures.PromptCapture):
        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            current = fixtures.perspective(prompt)
            if fixtures.phase(prompt) == "independent":
                payload = fixtures.independent(current)
            elif fixtures.phase(prompt) == "cross_examination":
                payload = fixtures.cross(current)
                payload["challenges"][0]["target_profile_id"] = "evidence_quality"
                payload["challenges"][0]["target_statement_id"] = "evidence-fact"
            else:
                payload = alias_synthesis_payload()
            return fixtures.Completion(
                text=json.dumps(payload), provider="offline", model="test"
            )

    from prism.debate import ACADEMIC_PROFILES

    academic_ids = tuple(profile.id for profile in ACADEMIC_PROFILES)
    analyzer = fixtures.FakeAnalyzer(fixtures.analysis("academic_discourse"))
    router = AcademicRouter()
    result = fixtures.run(
        fixtures.service(tmp_path, router, analyzer).debate(
            fixtures.CASE_ID, "Q-academic-alias-synthesis", fixtures.AS_OF
        )
    )

    assert result.profiles == academic_ids
    assert result.status == "completed"
    assert result.errors == ()
    assert result.synthesis is not None
    assert result.synthesis.consensus[0].text == CONSENSUS_FINDING
    assert result.synthesis.key_evidence[0].rationale == KEY_EVIDENCE_FINDING
    assert IMPLICATION_MARKER not in "\n".join(result.warnings)
    assert IMPLICATION_MARKER not in json.dumps(result_to_dict(result))
