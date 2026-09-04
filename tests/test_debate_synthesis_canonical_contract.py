"""Canonical synthesis contracts beyond the confirmed provider aliases."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_debate as fixtures
from test_debate_synthesis_shapes import alias_synthesis_payload


def run_debate(tmp_path: Path, synthesis_text: str, question: str):
    router = fixtures.ScriptedRouter(synthesis=synthesis_text)
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, question, fixtures.AS_OF
        )
    )
    return result, router


def assert_conservative_degradation(result):
    assert result.status == "degraded"
    assert result.fallback_reason == (
        "synthesis invalid; deterministic conservative summary used"
    )
    assert result.errors[-1].phase == "synthesis"
    assert result.errors[-1].error_code == "invalid_output"
    assert result.synthesis is not None
    assert result.synthesis.consensus == ()
    assert result.synthesis.disagreements == ()
    assert result.synthesis.sources_of_disagreement == ()
    assert result.synthesis.falsification_conditions == ()
    assert len(result.synthesis.key_evidence) == 1
    assert len(result.synthesis.unresolved_questions) == 1
    assert all(item.status == "available" for item in result.results)


@pytest.mark.parametrize(
    "section, item",
    [
        ("consensus", {"text": "x", "evidence_ids": ["node-1", "node-1"]}),
        ("consensus", {"finding": "x", "evidence_ids": ["node-1", "node-1"]}),
        (
            "unresolved_questions",
            {"question": "x", "relevant_evidence_ids": ["node-1", "node-1"]},
        ),
        (
            "falsification_conditions",
            {"condition": "x", "relevant_evidence_ids": ["node-1", "node-1"]},
        ),
    ],
)
def test_duplicate_citations_degrade_the_whole_synthesis(
    tmp_path, section, item
):
    payload = alias_synthesis_payload()
    payload[section] = [item]
    result, _ = run_debate(
        tmp_path,
        json.dumps(payload),
        question=f"Q-duplicate-{section}-{next(iter(item))}",
    )

    assert_conservative_degradation(result)


def test_synthesis_prompt_requires_canonical_schema(tmp_path):
    router = fixtures.PromptCapture()
    fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-canonical-prompt", fixtures.AS_OF
        )
    )

    prompt = router.prompts[-1]
    assert "Return exactly those six top-level fields." in prompt
    assert "items must contain exactly text and evidence_ids" in prompt
    assert (
        "key_evidence items must contain exactly evidence_id and rationale"
        in prompt
    )
    assert (
        "Do not use summary, finding, statement, question, condition, "
        "relevant_evidence_ids, implication, confidence, or status fields."
        in prompt
    )
    assert "Evidence id arrays must not repeat an id." in prompt
