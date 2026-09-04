"""Focused contracts for real provider statement shapes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_debate as fixtures

from prism.debate import ACADEMIC_PROFILES, result_to_dict


LATEST_SHAPE = {
    "statements": [
        {
            "classification": "fact",
            "evidence_ids": ["node-1"],
            "statement": "The publication records the change.",
        },
        {
            "classification": "unresolved",
            "evidence_ids": [],
            "statement": "Enforcement effects remain unknown.",
        },
    ]
}


class LatestShapeRouter(fixtures.ScriptedRouter):
    def __init__(self):
        super().__init__(independent=json.dumps(LATEST_SHAPE))
        self.cross_payloads = []

    async def complete(self, role, prompt):
        if fixtures.phase(prompt) == "cross_examination":
            payload = fixtures.cross(fixtures.perspective(prompt))
            payload["challenges"][0]["target_profile_id"] = "industry_execution"
            payload["challenges"][0]["target_statement_id"] = (
                "industry_execution:independent:0"
            )
            return fixtures.Completion(
                text=json.dumps(payload),
                provider="offline",
                model="test",
            )
        return await super().complete(role, prompt)


class SyntheticTargetRouter(fixtures.PromptCapture):
    """Serve id-less drifted statements and aim cross at their synthetic ids.

    Statements without an id are targeted internally as
    ``profile_id:independent:index``, so the default cross fixture (which
    aims at the explicit ``industry-fact`` id) must be retargeted the same
    way LatestShapeRouter does.
    """

    def __init__(self, independent):
        super().__init__(independent=independent)

    async def complete(self, role, prompt):
        if fixtures.phase(prompt) == "cross_examination":
            payload = fixtures.cross(fixtures.perspective(prompt))
            payload["challenges"][0]["target_profile_id"] = "industry_execution"
            payload["challenges"][0]["target_statement_id"] = (
                "industry_execution:independent:0"
            )
            return fixtures.Completion(
                text=json.dumps(payload),
                provider="offline",
                model="test",
            )
        return await super().complete(role, prompt)


def _assert_provider_shape_completes(tmp_path, payload, marker="classifications"):
    router = SyntheticTargetRouter(json.dumps(payload))
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, f"Q-{marker}", fixtures.AS_OF
        )
    )

    assert result.status == "completed"
    assert all(item.status == "available" for item in result.results)
    for perspective in result.results:
        assert perspective.interpretation.statements[0].classification == "fact"
    assert result.warnings == ()
    serialized = json.dumps(result_to_dict(result))
    assert "The publication records the change." in serialized
    return result


def _assert_independent_shapes_rejected(tmp_path, cases, marker="classifications"):
    for index, payload in enumerate(cases):
        router = fixtures.ScriptedRouter(independent=json.dumps(payload))
        result = fixtures.run(
            fixtures.service(tmp_path, router).debate(
                fixtures.CASE_ID, f"Q-{marker}-reject-{index}", fixtures.AS_OF
            )
        )

        assert result.status in {"degraded", "no_conclusion"}
        assert all(item.status == "unavailable" for item in result.results)
        assert all(
            item.failure.error_code == "invalid_output"
            for item in result.results
            if item.failure is not None
        )


def test_latest_real_provider_statement_string_parses_with_synthetic_id(tmp_path):
    router = LatestShapeRouter()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-latest-shape", fixtures.AS_OF
        )
    )

    assert result.status == "completed"
    assert all(item.status == "available" for item in result.results)
    first = result.results[0].interpretation.statements
    assert [statement.id for statement in first] == [
        "institutional_regulatory:independent:0",
        "institutional_regulatory:independent:1",
    ]
    assert [statement.text for statement in first] == [
        "The publication records the change.",
        "Enforcement effects remain unknown.",
    ]
    assert [statement.evidence_ids for statement in first] == [("node-1",), ()]

    serialized = json.dumps(result_to_dict(result))
    assert "institutional_regulatory:independent:0" in serialized
    assert "The publication records the change." in serialized
    assert result.errors == ()
    assert result.warnings == ()

    replay = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-latest-shape", fixtures.AS_OF
        )
    )
    assert replay.replayed is True
    assert replay == fixtures.replace(result, replayed=True)


def test_independent_prompt_requires_canonical_classification_shape(tmp_path):
    router = fixtures.PromptCapture()
    fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-prompt-shape", fixtures.AS_OF
        )
    )

    prompt = router.prompts[0]
    assert "classification must be a single string" in prompt
    assert "or a JSON array containing exactly one string" in prompt
    assert "Do not use a classifications object" in prompt
    assert "do not output both text and statement" in prompt


@pytest.mark.parametrize(
    "classifications",
    [
        "fact",
        ["fact"],
        {"type": "fact", "confidence": 0.9},
        {"classification": "fact", "confidence": 0.9},
        {"category": "fact", "confidence": 0.9},
        {"fact": {"confidence": 0.9, "reason": "recorded"}},
    ],
)
def test_confirmed_classifications_shapes_are_mapped(tmp_path, classifications):
    payload = {
        "statements": [
            {
                "classifications": classifications,
                "evidence_ids": ["node-1"],
                "statement": "The publication records the change.",
            }
        ]
    }
    _assert_provider_shape_completes(
        tmp_path, payload, marker=json.dumps(classifications, sort_keys=True)
    )


def test_ambiguous_or_unknown_classifications_are_rejected(tmp_path):
    valid_statement = {
        "classifications": ["fact"],
        "evidence_ids": ["node-1"],
        "statement": "The publication records the change.",
    }
    cases = [
        {
            "statements": [
                {**valid_statement, "classifications": ["fact", "interpretation"]}
            ]
        },
        {"statements": [{**valid_statement, "classifications": ["ghost"]}]},
        {"statements": [{**valid_statement, "classifications": ["fact", 1]}]},
        {
            "statements": [
                {**valid_statement, "classifications": {"confidence": 0.9}}
            ]
        },
        {
            "statements": [
                {
                    **valid_statement,
                    "classifications": {
                        "fact": "supported",
                        "interpretation": "possible",
                    },
                }
            ]
        },
        {
            "statements": [
                {
                    **valid_statement,
                    "classifications": {
                        "confidence": 0.9,
                        "evidence_ids": ["node-1"],
                    },
                }
            ]
        },
        {
            "statements": [
                {**valid_statement, "classifications": [{"type": "fact"}]}
            ]
        },
        {
            "statements": [
                {
                    **valid_statement,
                    "classification": "fact",
                    "classifications": ["fact"],
                }
            ]
        },
    ]
    _assert_independent_shapes_rejected(tmp_path, cases)


def test_academic_discourse_default_profiles_are_used():
    assert tuple(profile.id for profile in ACADEMIC_PROFILES) == (
        "experimental_methods",
        "mechanism_explanation",
        "evidence_quality",
        "research_history",
    )

    from prism.debate import DebateService

    service = DebateService(
        fixtures.CutoffAnalyzer("academic_discourse"), None, ledger=None
    )
    from prism.debate.service import _selected_profiles

    assert [profile.id for profile in _selected_profiles((), "academic_discourse", None)] == [
        "experimental_methods",
        "mechanism_explanation",
        "evidence_quality",
        "research_history",
    ]


def test_statement_alias_remains_strict(tmp_path):
    cases = [
        {"statements": ["The publication records the change."]},
        {"statements": [[]]},
        {"statements": [""]},
        {"statements": [{"classification": "fact", "evidence_ids": ["node-1"]}]},
        {
            "statements": [
                {
                    "classification": "fact",
                    "evidence_ids": ["node-1"],
                    "reasoning": "ignored explanation",
                }
            ]
        },
        {
            "statements": [
                {
                    "classification": "fact",
                    "evidence_ids": ["node-1"],
                    "statement": "valid",
                    "text": "conflicting",
                }
            ]
        },
    ]
    _assert_independent_shapes_rejected(tmp_path, cases, marker="strict-alias")


def test_reasoning_is_never_leaked_by_provider_classification_shapes(tmp_path):
    marker = "classification-reasoning-that-must-not-leak"
    payload = {
        "statements": [
            {
                "classifications": {
                    "type": "fact",
                    "reason": marker,
                    "confidence": 0.9,
                },
                "evidence_ids": ["node-1"],
                "statement": "The publication records the change.",
                "reasoning": marker,
            }
        ]
    }
    router = SyntheticTargetRouter(json.dumps(payload))
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-reasoning-leak", fixtures.AS_OF
        )
    )

    assert result.status == "completed"
    serialized = json.dumps(result_to_dict(result))
    assert marker not in serialized
    assert all(marker not in prompt for prompt in router.prompts)
    assert result.warnings


# --- Real-provider cross-examination shapes (M2 fix) -----------------------
#
# A real debate provider answers the cross_examination phase with three
# confirmed non-canonical shapes:
#   A) {"challenges": [{"challenged_id", "challenged_text" | "grounds"}],
#       "reply": string}
#   B) {"answer": string, "challenges": []}
#   C) {"response": string}
# The canonical contract (challenge_id/target_profile_id/target_statement_id/
# challenge/reply/withdrawn/evidence_ids on every item, no top-level text
# fields) stays strictly unchanged.  Drifted output is only ever mapped
# through challenged_id against known independent statement ids - never by
# guessing a target - and top-level reply/answer/response text is cross-phase
# metadata only: it never becomes a statement fact or an evidence citation,
# and ignored content never leaves the audit trail as a warning body.

CROSS_REPLY = "the cross-phase reply metadata"
CROSS_ANSWER = "cross-answer-metadata-that-must-not-leak"
CROSS_RESPONSE = "cross-response-metadata-that-must-not-leak"
CROSS_CHALLENGE = "the cross challenge argument"


def _drifted_cross(cross_payload):
    """Return a router serving canonical independents plus one cross shape."""

    class DriftedCrossRouter(fixtures.PromptCapture):
        def __init__(self):
            super().__init__()
            self.prompts = []

        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            if fixtures.phase(prompt) == "cross_examination":
                payload = cross_payload(fixtures.perspective(prompt))
                return fixtures.Completion(
                    text=json.dumps(payload), provider="offline", model="test"
                )
            return await super().complete(role, prompt)

    return DriftedCrossRouter


def _shape_a(profile):
    return {
        "reply": CROSS_REPLY,
        "challenges": [
            {
                "challenged_id": "industry-fact",
                "challenged_text": f"{profile} {CROSS_CHALLENGE}",
            }
        ],
    }


def _assert_cross_result(result, router, *, empty=False):
    assert result.status == "completed"
    assert all(item.status == "available" for item in result.results)
    assert result.errors == ()
    for perspective in result.results:
        assert perspective.cross_examination is not None
        assert (perspective.cross_examination.challenges == ()) is empty
    return result


def test_drifted_cross_challenged_text_maps_with_shared_top_level_reply(tmp_path):
    router = _drifted_cross(_shape_a)()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-shape-a", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    first = result.results[0].cross_examination.challenges[0]
    assert first.target_profile_id == "industry_execution"
    assert first.target_statement_id == "industry-fact"
    assert first.challenge == (
        "institutional_regulatory " + CROSS_CHALLENGE
    )
    assert first.reply == CROSS_REPLY
    assert first.withdrawn is False
    assert first.evidence_ids == ()
    assert first.challenge_id == "institutional_regulatory:cross:0"
    # The structural deviation is audited without carrying any content.
    assert result.warnings
    joined = "\n".join(result.warnings)
    assert "reply" in joined
    assert CROSS_CHALLENGE not in joined
    assert CROSS_REPLY not in joined

    # Ledger replay for the identical input preserves the audited warnings.
    from dataclasses import replace as dataclass_replace

    replay = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-shape-a", fixtures.AS_OF
        )
    )
    assert replay.replayed is True
    assert replay == dataclass_replace(result, replayed=True)
    assert replay.warnings == result.warnings


def test_drifted_cross_grounds_shape_maps_to_canonical(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": CROSS_REPLY,
            "challenges": [
                {"challenged_id": "industry-fact", "grounds": "grounds argument"}
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-grounds", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    challenge = result.results[0].cross_examination.challenges[0]
    assert challenge.challenge == "grounds argument"
    assert challenge.reply == CROSS_REPLY
    assert challenge.target_profile_id == "industry_execution"
    assert challenge.withdrawn is False
    assert challenge.evidence_ids == ()


def test_drifted_cross_single_top_level_answer_serves_as_item_reply(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "answer": CROSS_ANSWER,
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "challenged_text": "challenge without own reply",
                }
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-answer-reply", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    challenge = result.results[0].cross_examination.challenges[0]
    assert challenge.reply == CROSS_ANSWER
    # The top-level answer is cross-phase reply metadata only: it never turns
    # into a statement or a citation anywhere else.
    serialized = json.dumps(result_to_dict(result))
    assert CROSS_ANSWER in serialized  # canonical reply field of the challenge
    assert result.warnings


def test_drifted_cross_without_any_reply_drops_item_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "challenges": [
                {"challenged_id": "industry-fact", "grounds": "no reply anywhere"}
            ]
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-no-reply", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    assert any("dropped" in warning for warning in result.warnings)
    assert "no reply anywhere" not in "\n".join(result.warnings)


def test_drifted_cross_missing_challenged_id_drops_item_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": CROSS_REPLY,
            "challenges": [
                {"challenged_text": "challenge without any target reference"}
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-no-target", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    assert any("dropped" in warning for warning in result.warnings)
    joined = "\n".join(result.warnings)
    assert "challenge without any target reference" not in joined
    assert CROSS_REPLY not in joined


def test_drifted_cross_unknown_challenged_id_drops_item_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": CROSS_REPLY,
            "challenges": [
                {"challenged_id": "ghost-statement", "grounds": "unknown target"}
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-ghost-target", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    assert any("dropped" in warning for warning in result.warnings)


def test_drifted_cross_ambiguous_challenged_id_drops_item_with_warning(tmp_path):
    class AmbiguousTargetRouter(fixtures.PromptCapture):
        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            current = fixtures.perspective(prompt)
            if fixtures.phase(prompt) == "independent":
                if current in ("institutional_regulatory", "industry_execution"):
                    payload = {
                        "statements": [
                            {
                                "id": "shared-fact",
                                "classification": "fact",
                                "text": f"{current} records the publication.",
                                "evidence_ids": ["node-1"],
                            }
                        ]
                    }
                else:
                    payload = fixtures.independent(current)
            elif fixtures.phase(prompt) == "cross_examination":
                payload = {
                    "reply": CROSS_REPLY,
                    "challenges": [
                        {
                            "challenged_id": "shared-fact",
                            "challenged_text": "challenge to an ambiguous target",
                        }
                    ],
                }
            else:
                payload = fixtures.synthesis_payload()
            return fixtures.Completion(
                text=json.dumps(payload), provider="offline", model="test"
            )

    router = AmbiguousTargetRouter()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-ambiguous", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    assert any("dropped" in warning for warning in result.warnings)
    joined = "\n".join(result.warnings)
    assert "challenge to an ambiguous target" not in joined


def test_drifted_cross_both_challenge_text_fields_drop_item_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": CROSS_REPLY,
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "challenged_text": "one text",
                    "grounds": "another text",
                }
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-both-texts", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    assert any("dropped" in warning for warning in result.warnings)
    joined = "\n".join(result.warnings)
    assert "one text" not in joined
    assert "another text" not in joined


def test_drifted_cross_missing_evidence_ids_stays_unresolved_metadata(tmp_path):
    router = _drifted_cross(_shape_a)()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-no-evidence", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    assert all(
        challenge.evidence_ids == ()
        for perspective in result.results
        for challenge in perspective.cross_examination.challenges
    )


def test_drifted_cross_explicit_evidence_ids_are_validated(tmp_path):
    valid = _drifted_cross(
        lambda profile: {
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "recorded",
                    "reply": "recorded reply",
                    "evidence_ids": ["node-1"],
                }
            ]
        }
    )()
    ok = fixtures.run(
        fixtures.service(tmp_path, valid).debate(
            fixtures.CASE_ID, "Q-cross-evidence-valid", fixtures.AS_OF
        )
    )
    _assert_cross_result(ok, valid)
    assert ok.results[0].cross_examination.challenges[0].evidence_ids == ("node-1",)

    invalid = _drifted_cross(
        lambda profile: {
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "recorded",
                    "reply": "recorded reply",
                    "evidence_ids": ["ghost"],
                }
            ]
        }
    )()
    bad = fixtures.run(
        fixtures.service(tmp_path, invalid).debate(
            fixtures.CASE_ID, "Q-cross-evidence-ghost", fixtures.AS_OF
        )
    )
    assert bad.status == "completed_with_unavailable_perspectives"
    assert all(item.status == "unavailable" for item in bad.results)
    assert all(
        item.failure.error_code == "invalid_output"
        and item.failure.phase == "cross_examination"
        for item in bad.results
        if item.failure is not None
    )


def test_cross_answer_only_output_is_empty_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {"answer": CROSS_ANSWER, "challenges": []}
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-answer-only", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    joined = "\n".join(result.warnings)
    assert "answer" in joined
    assert CROSS_ANSWER not in joined
    # The ignored answer never reaches any later prompt, the serialized
    # result, or the ledger.
    assert all(CROSS_ANSWER not in prompt for prompt in router.prompts)
    assert CROSS_ANSWER not in json.dumps(result_to_dict(result))

    from prism.debate import DebateLedger

    entry = DebateLedger(tmp_path / "index.db").entries(fixtures.CASE_ID)[0]
    assert CROSS_ANSWER not in entry.rounds_json
    assert CROSS_ANSWER not in entry.result_json


def test_cross_response_only_output_is_empty_with_warning(tmp_path):
    router = _drifted_cross(
        lambda profile: {"response": CROSS_RESPONSE}
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-response-only", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    joined = "\n".join(result.warnings)
    assert "response" in joined
    assert CROSS_RESPONSE not in joined
    assert all(CROSS_RESPONSE not in prompt for prompt in router.prompts)
    assert CROSS_RESPONSE not in json.dumps(result_to_dict(result))

    from prism.debate import DebateLedger

    entry = DebateLedger(tmp_path / "index.db").entries(fixtures.CASE_ID)[0]
    assert CROSS_RESPONSE not in entry.rounds_json
    assert CROSS_RESPONSE not in entry.result_json


def test_cross_top_level_metadata_with_canonical_items_is_ignored_with_warning(
    tmp_path,
):
    router = _drifted_cross(
        lambda profile: {**fixtures.cross(profile), "answer": CROSS_ANSWER}
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-canonical-plus-answer", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    canonical = result.results[0].cross_examination.challenges[0]
    assert canonical.challenge_id == "institutional_regulatory-challenge"
    assert canonical.reply == "The publication record remains supported."
    assert result.warnings
    assert CROSS_ANSWER not in json.dumps(result_to_dict(result))
    assert CROSS_ANSWER not in "\n".join(result.warnings)


def test_cross_empty_top_level_metadata_is_an_audited_no_op(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": "",
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "empty reply must not fail the phase",
                }
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-empty-meta", fixtures.AS_OF
        )
    )

    # The empty top-level reply is unusable as reply metadata, so the item is
    # conservatively dropped; the perspective must not fail as a whole.
    _assert_cross_result(result, router, empty=True)
    assert result.warnings
    joined = "\n".join(result.warnings)
    assert "reply" in joined
    assert "empty reply must not fail the phase" not in joined


def test_cross_non_string_top_level_metadata_is_still_invalid(tmp_path):
    router = _drifted_cross(
        lambda profile: {"response": 42, "challenges": []}
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-nonstring-meta", fixtures.AS_OF
        )
    )

    assert result.status == "completed_with_unavailable_perspectives"
    assert all(item.status == "unavailable" for item in result.results)
    assert all(
        item.failure.error_code == "invalid_output"
        and item.failure.phase == "cross_examination"
        for item in result.results
        if item.failure is not None
    )


def test_cross_unknown_top_level_field_is_still_rejected(tmp_path):
    router = _drifted_cross(
        lambda profile: {"challenges": [], "bogus_top_level": 1}
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-unknown-top", fixtures.AS_OF
        )
    )

    assert result.status == "completed_with_unavailable_perspectives"
    assert all(item.status == "unavailable" for item in result.results)
    assert all(
        item.failure.error_code == "invalid_output"
        and item.failure.phase == "cross_examination"
        for item in result.results
        if item.failure is not None
    )


def test_cross_drifted_unknown_item_field_is_still_rejected(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "reply": CROSS_REPLY,
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "recorded",
                    "reply": "reply",
                    "bogus_item_field": True,
                }
            ],
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-unknown-item", fixtures.AS_OF
        )
    )

    assert result.status == "completed_with_unavailable_perspectives"
    assert all(item.status == "unavailable" for item in result.results)
    assert all(
        item.failure.error_code == "invalid_output"
        for item in result.results
        if item.failure is not None
    )


def test_cross_drifted_non_boolean_withdrawn_is_still_rejected(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "recorded",
                    "reply": "reply",
                    "withdrawn": "yes",
                }
            ]
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-bad-withdrawn", fixtures.AS_OF
        )
    )

    assert result.status == "completed_with_unavailable_perspectives"
    assert all(item.status == "unavailable" for item in result.results)
    assert all(
        item.failure.error_code == "invalid_output"
        for item in result.results
        if item.failure is not None
    )


def test_drifted_cross_explicit_withdrawn_true_is_recorded(tmp_path):
    router = _drifted_cross(
        lambda profile: {
            "challenges": [
                {
                    "challenged_id": "industry-fact",
                    "grounds": "now retracted",
                    "reply": "retraction accepted",
                    "withdrawn": True,
                }
            ]
        }
    )()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-withdrawn", fixtures.AS_OF
        )
    )

    _assert_cross_result(result, router)
    challenge = result.results[0].cross_examination.challenges[0]
    assert challenge.withdrawn is True


def test_cross_strict_canonical_schema_is_unchanged(tmp_path):
    canonical = fixtures.cross("institutional_regulatory")
    missing_reply_item = {
        key: value
        for key, value in canonical["challenges"][0].items()
        if key != "reply"
    }
    invalid_payloads = [
        # Missing one required canonical item field.
        {"challenges": [missing_reply_item]},
        # Unknown item field.
        {
            "challenges": [
                {**canonical["challenges"][0], "bogus_item_field": True}
            ]
        },
        # Empty object is not a cross output.
        {},
        # challenges must be an array.
        {"challenges": "not-an-array"},
        # Unknown top-level field without any tolerated metadata.
        {**canonical, "bogus_top_level": 1},
    ]
    for index, payload in enumerate(invalid_payloads):
        router = fixtures.ScriptedRouter(cross=json.dumps(payload))
        result = fixtures.run(
            fixtures.service(tmp_path, router).debate(
                fixtures.CASE_ID, f"Q-cross-strict-{index}", fixtures.AS_OF
            )
        )
        assert result.status == "completed_with_unavailable_perspectives"
        assert all(item.status == "unavailable" for item in result.results)
        assert all(
            item.failure.error_code == "invalid_output"
            and item.failure.phase == "cross_examination"
            for item in result.results
            if item.failure is not None
        )


def test_cross_drift_invalid_output_is_isolated_per_perspective(tmp_path):
    class OneDriftedInvalidRouter(fixtures.PromptCapture):
        async def complete(self, role, prompt):
            self.prompts.append(prompt)
            current = fixtures.perspective(prompt)
            if (
                fixtures.phase(prompt) == "cross_examination"
                and current == "institutional_regulatory"
            ):
                return fixtures.Completion(
                    text='{"challenges": [], "bogus": 1}',
                    provider="offline",
                    model="test",
                )
            return await super().complete(role, prompt)

    router = OneDriftedInvalidRouter()
    result = fixtures.run(
        fixtures.service(tmp_path, router).debate(
            fixtures.CASE_ID, "Q-cross-isolation", fixtures.AS_OF
        )
    )

    assert result.status == "completed_with_unavailable_perspectives"
    failed = result.results[0]
    assert failed.status == "unavailable"
    assert failed.failure is not None
    assert failed.failure.phase == "cross_examination"
    assert failed.failure.error_code == "invalid_output"
    assert [item.status for item in result.results[1:]] == ["available"] * 3
    assert len(result.errors) == 1
    assert all(
        item.cross_examination is not None for item in result.results[1:]
    )
