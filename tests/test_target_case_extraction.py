"""Explicit target-case context for strict extraction (TDD acceptance).

A caller may declare the one ``EvolutionCase`` a material belongs to before
extraction.  The LLM then only extracts content and anchors it to that case:
the returned top-level case must carry the declared identity verbatim, and
``case: null`` / case-id drift / identity-field drift are never silently
rewritten.  Without a target case the old case-less behavior is preserved
(candidates are retained at material scope as ``awaiting_case_binding``).

These tests never touch real materials or a live LLM.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from prism.domain import EvolutionCase, Material
from prism.extraction import ExtractionError, ExtractionResult, ExtractionService


UTC = timezone.utc
PUBLISHED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)

REVIEW_BODY = (
    "A 2025 cohort study reported that the revised rule doubled uptake.\n"
    "\n"
    "The cited literature reported both doubled uptake and no effect.\n"
    "\n"
    "In this review, the authors conclude that the evidence remains mixed."
)


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


def target_case(**overrides) -> EvolutionCase:
    """The caller-declared evolution case used throughout these tests."""
    values = dict(
        case_id="case-disclosure",
        case_type="academic_discourse",
        canonical_name="Disclosure rule evidence",
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        status="mixed",
    )
    values.update(overrides)
    return EvolutionCase(
        case_id=values["case_id"],
        case_type=values["case_type"],
        canonical_name=values["canonical_name"],
        start_at=values["start_at"],
        status=values["status"],
        node_ids=(),
    )


def material(**overrides) -> Material:
    values = dict(
        id="material-evolution",
        title="Evidence synthesis on the disclosure rule",
        source="example.test",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        type="academic",
        content=REVIEW_BODY,
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


def review_payload(**overrides) -> dict:
    """A review/synthesis payload anchored on the declared target case."""
    payload = {
        "material_role": "review",
        "case": {
            "case_id": "case-disclosure",
            "case_type": "academic_discourse",
            "canonical_name": "Disclosure rule evidence",
            "start_at": "2025-01-01T00:00:00+00:00",
            "status": "mixed",
            "node_ids": ["prior-study"],
        },
        "nodes": [
            {
                "id": "prior-study",
                "case_id": "case-disclosure",
                "node_type": "publication",
                "assertion_type": "fact",
                "happened_at": "2025-06-01T00:00:00+00:00",
                "valid_at": "2025-06-01T00:00:00+00:00",
                "observed_at": PUBLISHED.isoformat(),
                "summary": "A 2025 cohort study reported doubled uptake.",
                "source_ids": ["material-evolution"],
                "claim_ids": [],
                "provenance_type": "cited_prior_research",
                "evidence_role": "cited_prior_research",
                "evidence": evidence(
                    "A 2025 cohort study reported that the revised rule doubled uptake.",
                    1,
                ),
            }
        ],
        "temporal_facts": [
            {
                "fact_id": "prior-result",
                "subject": "Revised rule",
                "predicate": "uptake effect",
                "object": "doubled uptake",
                "assertion_type": "fact",
                "valid_at": "2025-06-01T00:00:00+00:00",
                "invalid_at": None,
                "observed_at": PUBLISHED.isoformat(),
                "source_ids": ["material-evolution"],
                "confidence": 0.9,
                "provenance_type": "cited_prior_research",
                "evidence_role": "cited_prior_research",
                "cited_source_ref": "Cohort Group (2025), doi:10.1000/example",
                "evidence": evidence(
                    "A 2025 cohort study reported that the revised rule doubled uptake.",
                    1,
                ),
            }
        ],
        "claims": [
            {
                "claim_id": "review-conclusion",
                "actor": "Review authors",
                "proposition": "The evidence remains mixed.",
                "stance": "uncertain",
                "claim_type": "interpretation",
                "stated_at": PUBLISHED.isoformat(),
                "observed_at": PUBLISHED.isoformat(),
                "based_on": ["material-evolution"],
                "revised_by": None,
                "provenance_type": "current_author_interpretation",
                "evidence_role": "current_synthesis",
                "confidence": 0.8,
                "evidence": evidence(
                    "In this review, the authors conclude that the evidence remains mixed.",
                    3,
                ),
            }
        ],
        "conflicts": [],
        "relations": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


def extract(payload, source=None, *, target=None) -> ExtractionResult:
    router = FakeRouter(payload)
    service = ExtractionService(router)
    kwargs = {"corpus_path": "corpus/2026-03/caseless-review.md"}
    if target is not None:
        kwargs["target_case"] = target
    return run(service.extract_material(source or material(), **kwargs)), router


# --------------------------------------------------------------------- success


def test_target_case_anchors_every_candidate_to_the_declared_case():
    result, router = extract(review_payload(), target=target_case())

    expected_case = EvolutionCase(
        case_id="case-disclosure",
        case_type="academic_discourse",
        canonical_name="Disclosure rule evidence",
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        status="mixed",
        node_ids=("prior-study",),
    )
    assert result.case == expected_case
    assert result.accumulation_status == "case_bound"
    assert [node.id for node in result.nodes] == ["prior-study"]
    assert result.nodes[0].evidence_role == "cited_prior_research"
    assert [fact.fact_id for fact in result.temporal_facts] == ["prior-result"]
    assert result.temporal_facts[0].evidence_role == "cited_prior_research"
    assert result.temporal_facts[0].cited_source_ref == (
        "Cohort Group (2025), doi:10.1000/example"
    )
    assert [claim.claim_id for claim in result.claims] == ["review-conclusion"]
    assert result.claims[0].evidence_role == "current_synthesis"
    unresolved = next(
        gap
        for gap in result.evidence_gaps
        if gap.gap_type == "unresolved_cited_source"
    )
    assert unresolved.item_id == "prior-result"
    assert len(result.evidence_matches) == 3

    prompt = router.calls[0][1]
    assert "DECLARED TARGET CASE" in prompt
    assert "case_id: 'case-disclosure'" in prompt
    assert "case_type: 'academic_discourse'" in prompt
    assert "canonical_name: 'Disclosure rule evidence'" in prompt
    assert "start_at: 2025-01-01T00:00:00+00:00" in prompt
    assert "status: 'mixed'" in prompt


def test_target_case_prompt_keeps_review_prior_results_as_second_hand_evidence():
    _, router = extract(review_payload(), target=target_case())
    prompt = router.calls[0][1]
    assert "second-hand evidence" in prompt
    assert "supersedes, revises, or contradicts" in prompt
    assert "instead of omitting them" in prompt
    # The target block replaces the case_tags rule: the declared case is the
    # binding authority, never the frontmatter tags.
    assert "when tags exist" not in prompt

    # Without a target the block is absent and the old case-less wording
    # (tags rule included) is preserved verbatim.
    _, plain_router = extract(review_payload())
    plain_prompt = plain_router.calls[0][1]
    assert "DECLARED TARGET CASE" not in plain_prompt
    assert "when tags exist" in plain_prompt
    assert "second-hand evidence" in plain_prompt


# --------------------------------------------------------------------- refusal


def test_target_case_null_with_retained_candidates_is_rejected():
    payload = review_payload()
    payload["case"] = None
    with pytest.raises(ExtractionError) as info:
        extract(payload, target=target_case())
    message = str(info.value)
    assert "case: null" in message
    assert "case-disclosure" in message


def test_target_case_drifted_case_id_is_rejected():
    payload = review_payload()
    payload["case"]["case_id"] = "case-invented-elsewhere"
    with pytest.raises(ExtractionError) as info:
        extract(payload, target=target_case())
    message = str(info.value)
    assert "drifted from the declared target case" in message
    assert "case_id" in message
    assert "case-disclosure" in message


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("case_type", "policy"),
        ("canonical_name", "Some other discourse"),
        ("start_at", "2026-01-01T00:00:00+00:00"),
        ("status", "active"),
    ],
)
def test_target_case_drifted_identity_fields_are_rejected(field, drifted_value):
    payload = review_payload()
    payload["case"] = {
        "case_id": "case-disclosure",
        "case_type": "academic_discourse",
        "canonical_name": "Disclosure rule evidence",
        "start_at": "2025-01-01T00:00:00+00:00",
        "status": "mixed",
        "node_ids": [],
    }
    payload["nodes"] = []
    payload["temporal_facts"] = []
    payload["claims"] = []
    payload["case"][field] = drifted_value
    with pytest.raises(ExtractionError) as info:
        extract(payload, target=target_case())
    message = str(info.value)
    assert "drifted from the declared target case" in message
    assert field in message


def test_target_case_unusable_case_object_is_rejected():
    payload = review_payload()
    payload["case"]["case_id"] = ""
    with pytest.raises(ExtractionError) as info:
        extract(payload, target=target_case())
    message = str(info.value)
    assert "unusable case object" in message
    assert "case_id was empty" in message


def test_target_case_rejects_a_non_evolution_case_target():
    router = FakeRouter(review_payload())
    service = ExtractionService(router)
    with pytest.raises(TypeError) as info:
        run(
            service.extract_material(
                material(),
                corpus_path="corpus/2026-03/review.md",
                target_case="case-disclosure",  # type: ignore[arg-type]
            )
        )
    assert "EvolutionCase" in str(info.value)


# ------------------------------------------------- no-substantive dispositions


def test_target_case_empty_completion_skips_with_target_audit():
    payload = {
        "material_role": "news_report",
        "case": None,
        "nodes": [],
        "temporal_facts": [],
        "claims": [],
        "conflicts": [],
        "relations": [],
        "warnings": [],
    }
    result, _ = extract(payload, target=target_case())

    assert result.case is None
    assert result.accumulation_status == "no_substantive_evidence"
    assert any(
        gap.gap_type == "no_substantive_evolution" for gap in result.evidence_gaps
    )
    assert any("case-disclosure" in warning for warning in result.warnings)


def test_target_case_publication_only_review_stays_non_substantive():
    payload = review_payload()
    payload["temporal_facts"] = []
    payload["claims"] = []
    payload["nodes"] = [
        {
            "id": "review-publication",
            "case_id": "case-disclosure",
            "node_type": "publication",
            "assertion_type": "fact",
            "happened_at": PUBLISHED.isoformat(),
            "valid_at": PUBLISHED.isoformat(),
            "observed_at": PUBLISHED.isoformat(),
            "summary": "The review published its synthesis.",
            "source_ids": ["material-evolution"],
            "claim_ids": [],
            "provenance_type": "material_publication",
            "evidence_role": "publication_event",
            "evidence": evidence(
                "In this review, the authors conclude that the evidence remains mixed.",
                3,
            ),
        }
    ]
    payload["case"]["node_ids"] = ["review-publication"]

    result, _ = extract(payload, target=target_case())

    # Publication metadata never becomes substantive evolution, even when the
    # caller declared the target case for this material.
    assert result.case is None
    assert result.nodes == ()
    assert any(
        gap.gap_type == "no_substantive_evolution" for gap in result.evidence_gaps
    )
    assert any("publication-only" in warning for warning in result.warnings)


def test_target_case_context_only_candidate_stays_a_gap():
    payload = review_payload()
    payload["nodes"] = []
    payload["claims"] = []
    payload["case"]["node_ids"] = []
    payload["temporal_facts"] = [
        {
            "fact_id": "background",
            "subject": "Disclosure rules",
            "predicate": "historical_context",
            "object": "long-standing practice",
            "assertion_type": "fact",
            "valid_at": "2025-01-15T00:00:00+00:00",
            "invalid_at": None,
            "observed_at": PUBLISHED.isoformat(),
            "source_ids": ["material-evolution"],
            "confidence": 0.5,
            "provenance_type": "context_only",
            "evidence_role": "context_only",
            "evidence": evidence(
                "The cited literature reported both doubled uptake and no effect.",
                2,
            ),
        }
    ]

    result, _ = extract(payload, target=target_case())

    assert result.case is None
    assert result.temporal_facts == ()
    gap = next(gap for gap in result.evidence_gaps if gap.gap_type == "review_context")
    assert "context_only" in gap.detail


# ------------------------------------------------- old no-target behavior


def test_without_target_case_null_retention_is_unchanged():
    payload = review_payload()
    payload["case"] = None

    result, router = extract(payload)

    assert result.case is None
    assert [node.id for node in result.nodes] == ["prior-study"]
    assert result.accumulation_status == "awaiting_case_binding"
    gap = next(
        gap for gap in result.evidence_gaps if gap.gap_type == "missing_case_context"
    )
    assert gap.item_kind is None and gap.item_id is None
    assert "retained" in gap.detail
    assert "DECLARED TARGET CASE" not in router.calls[0][1]


def test_target_case_is_authoritative_when_material_tags_exclude_it():
    source = material(case_tags=("case-other",))
    result, router = extract(review_payload(), source, target=target_case())

    assert result.case is not None
    assert result.case.case_id == "case-disclosure"
    assert [node.id for node in result.nodes] == ["prior-study"]
    assert any("case_tags" in warning and "case-disclosure" in warning
               for warning in result.warnings)
    assert "when tags exist" not in router.calls[0][1]


# ---------------------------------------------- domain/target construction


def test_target_case_fields_are_frozen_aware_and_tuple_normalized():
    positional = EvolutionCase(
        "case-disclosure",
        "academic_discourse",
        "Disclosure rule evidence",
        datetime(2025, 1, 1, tzinfo=UTC),
        "mixed",
        ["a", "b"],
    )
    assert positional.node_ids == ("a", "b")
    with pytest.raises(FrozenInstanceError):
        positional.status = "active"  # type: ignore[misc]
    with pytest.raises(ValueError) as info:
        EvolutionCase(
            "case-disclosure",
            "academic_discourse",
            "Disclosure rule evidence",
            datetime(2025, 1, 1),  # naive
            "mixed",
        )
    assert "timezone-aware" in str(info.value)


def test_target_case_node_ids_shrink_to_the_actual_legal_candidates():
    """case.node_ids is rebuilt from accepted nodes, never the model's list."""
    payload = review_payload()
    second_node = dict(payload["nodes"][0])
    second_node.update(
        {
            "id": "prior-study-broken",
            "summary": "A broken quote claim.",
            "evidence": evidence("words absent from the material", 1),
        }
    )
    payload["nodes"].append(second_node)
    payload["case"]["node_ids"] = ["prior-study", "prior-study-broken"]

    result, _ = extract(payload, target=target_case())

    assert [node.id for node in result.nodes] == ["prior-study"]
    assert result.case.node_ids == ("prior-study",)
    assert any(
        gap.gap_type == "evidence_location_failed" for gap in result.evidence_gaps
    )


def test_target_case_review_cited_facts_enter_the_graph_with_roles():
    """Cited prior research under a target case reaches the graph with
    evidence_role/cited_source_ref intact (secondary evidence, not omitted)."""
    from prism.graph import GraphEpisode, GraphService

    class Backend:
        def __init__(self) -> None:
            self.episodes: dict[str, GraphEpisode] = {}

        async def add_episode(self, episode: GraphEpisode) -> bool:
            if episode.episode_key in self.episodes:
                return False
            self.episodes[episode.episode_key] = episode
            return True

        async def search(self, query: str) -> tuple[GraphEpisode, ...]:
            return tuple(self.episodes.values())

    result, _ = extract(review_payload(), target=target_case())
    backend = Backend()
    graph = GraphService(backend)  # type: ignore[arg-type]
    write = run(
        graph.add_case(
            result.case,
            nodes=result.nodes,
            facts=result.temporal_facts,
            claims=result.claims,
            materials=(material(),),
        )
    )
    assert write.episodes
    assert any(
        '"evidence_role":"cited_prior_research"' in episode.episode_body
        for episode in write.episodes
    )
    timeline = run(graph.timeline("case-disclosure", FETCHED))
    prior_fact_entry = next(
        entry for entry in timeline.entries if entry.kind == "temporal_fact"
    )
    assert prior_fact_entry.evidence_role == "cited_prior_research"
    assert prior_fact_entry.cited_source_ref == (
        "Cohort Group (2025), doi:10.1000/example"
    )


def test_old_positional_call_shapes_stay_compatible():
    # extract_material keeps accepting the material positionally with only
    # the optional corpus_path keyword; no target_case means old behavior.
    result, _ = extract(
        {
            "material_role": "news_report",
            "case": None,
            "nodes": [],
            "temporal_facts": [],
            "claims": [],
            "conflicts": [],
            "relations": [],
            "warnings": [],
        }
    )
    assert isinstance(result, ExtractionResult)
    assert result.case is None

    # The legacy non-strict extract() entry point is untouched.
    legacy_payload = {
        "case": None,
        "nodes": [],
        "temporal_facts": [],
        "claims": [],
        "warnings": [],
    }
    router = FakeRouter(legacy_payload)
    legacy = run(ExtractionService(router).extract(material()))
    assert legacy.case is None
