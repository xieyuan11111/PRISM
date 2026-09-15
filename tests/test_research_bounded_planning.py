"""Real-run regressions for bounded, auditable research planning.

Covers the HN-AD review findings recorded in
``docs/case-driven-auto-research-loop.md`` §12.1/§12.2: an ~80KB seed
material must never be pasted whole into the ``source_selector`` prompt
(the completion truncated at ~32K chars and silently degraded to a
policy-flavored fallback), concept/query counts must be deterministically
bounded, truncation must stay auditable instead of masquerading as a normal
plan, and an ``academic_discourse`` case must fall back to academic
field/term-level queries rather than proposal/implementation policy
templates.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from prism.config import PrismConfig, SourceConfig
from prism.domain import EvolutionCase, Material
from prism.extraction import ExtractionResult
from prism.research import ResearchPlanner

UTC = timezone.utc
PLANNED = datetime(2026, 9, 1, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2024, 1, 10, tzinfo=UTC)
WHITELIST = ("europepmc.org", "pubmed.ncbi.nlm.nih.gov")
REVIEW_BODY = (
    "Heterotrophic nitrification and aerobic denitrification (HN-AD) review. "
    + "Stress-tolerant strains under low temperature, high salt and heavy "
    "metal conditions were surveyed. " * 900
)  # ~80KB of review prose, like the real seed material


def make_material(**overrides):
    values = {
        "id": "mat_review",
        "title": "Stress-tolerant HN-AD strains: a review",
        "source": "europepmc.org",
        "published_at": PUBLISHED,
        "fetched_at": FETCHED,
        "type": "academic",
        "content": REVIEW_BODY,
    }
    values.update(overrides)
    return Material(**values)


def make_extraction(case_type="academic_discourse"):
    case = EvolutionCase(
        case_id="stress-tolerant-hnad-strains-2026",
        case_type=case_type,
        canonical_name="Stress-tolerant HN-AD strains",
        start_at=PUBLISHED,
        status="active",
    )
    return ExtractionResult(case=case)


def make_planner(router=None):
    return ResearchPlanner(
        PrismConfig(sources=SourceConfig(WHITELIST)),
        router=router,
        clock=lambda: PLANNED,
    )


class FakeRouter:
    def __init__(self, text=""):
        self._text = text
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        return type("Completion", (), {"text": self._text})()


class ScriptedRouter:
    """Returns the given completion texts in order, one per call."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        return type("Completion", (), {"text": self._texts.pop(0)})()


class ExplodingRouter:
    def __init__(self, error):
        self._error = error
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        raise self._error


def llm_payload(concept_count, queries_per_concept):
    """A structurally valid source_selector payload over the whitelist."""
    concepts = [
        {
            "concept_id": f"c{index}",
            "label": f"concept {index}",
            "description": "d",
            "aliases": [],
            "source_ids": ["mat_review"],
            "target_results": 10,
        }
        for index in range(concept_count)
    ]
    queries = [
        {
            "query": f'"concept {index}" variant {variant}',
            "phase": "current",
            "concept_id": f"c{index}",
            "result_limit": 10,
            "source_domains": ["europepmc.org"],
            "source_types": ["academic_paper"],
            "reason": "r",
        }
        for index in range(concept_count)
        for variant in range(queries_per_concept)
    ]
    return {
        "concepts": concepts,
        "windows": [
            {
                "phase": "current",
                "start_at": "2026-08-01T00:00:00Z",
                "end_at": "2026-08-30T00:00:00Z",
                "focus": "current status",
            }
        ],
        "candidates": [
            {
                "domain": "europepmc.org",
                "source_types": ["academic_paper"],
                "priority": 1,
                "reason": "whitelisted",
            }
        ],
        "queries": queries,
    }


# --- A: bounded source_selector input ---------------------------------------


def test_prompt_never_embeds_the_full_material_body():
    router = FakeRouter("{}")
    plan = asyncio.run(make_planner(router).plan(make_material()))
    assert plan.origin == "fallback"  # empty payload is not a plan; bounded anyway

    (role, prompt) = router.calls[0]
    assert role == "source_selector"
    assert len(REVIEW_BODY) > 60_000
    # The ~80KB body must not be pasted whole into the prompt: the real run
    # truncated the completion at ~32K chars because the prompt demanded a
    # concept for everything in the full text.
    assert REVIEW_BODY not in prompt
    assert len(prompt) < 20_000
    # The bounded excerpt is marked as such and still carries the identity.
    assert "MATERIAL EXCERPT" in prompt
    assert make_material().title in prompt
    assert "mat_review" in prompt


def test_prompt_caps_concepts_and_requires_term_level_queries():
    router = FakeRouter("{}")
    asyncio.run(make_planner(router).plan(make_material()))
    prompt = router.calls[0][1].lower()
    # Single-completion output budget, stated in the prompt itself: the
    # real run asked for up to 50 concepts + one query each and the
    # completion truncated mid-JSON at ~29K chars.  The budget is
    # decoupled from the plan-level hard caps (50 concepts / 60 queries),
    # which stay enforced by ResearchPlan.
    assert "every searchable concept" not in prompt
    assert "at most 10 concepts" in prompt
    assert "at most 15 queries" in prompt
    assert "at most 50" not in prompt
    assert "at most 60" not in prompt
    # Field/term-level academic query discipline (docs §12.2): quoted exact
    # phrases plus the source's field syntax, never a bare natural-language
    # sentence.
    assert "title_abs" in prompt
    assert "quoted" in prompt or "quote exact" in prompt
    assert "and" in prompt


def test_truncated_completion_stays_auditable_and_is_not_claimed_as_llm_plan():
    truncated = '{"windows": [{"phase": "current"'  # unterminated JSON, like a cut-off completion
    router = FakeRouter(truncated)
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "fallback"
    joined = "\n".join(plan.warnings)
    # The rejection detail survives verbatim for audit...
    assert "source_selector output rejected" in joined
    assert "not valid JSON" in joined
    # ...and the plan honestly says the LLM plan failed and may be retried,
    # instead of presenting the fallback as a routine plan.
    assert "retryable" in joined or "retried" in joined


def test_oversized_llm_concept_list_falls_back_with_warning():
    concept = {
        "concept_id": "c",
        "label": "concept",
        "description": "d",
        "aliases": [],
        "source_ids": ["mat_review"],
        "target_results": 10,
    }
    payload = {
        "concepts": [
            {**concept, "concept_id": f"c{index}", "label": f"concept {index}"}
            for index in range(60)
        ],
    }
    router = FakeRouter(json.dumps(payload))
    plan = asyncio.run(make_planner(router).plan(make_material()))
    assert plan.origin == "fallback"
    assert plan.warnings


# --- B: single-completion output budget (real-run ~29K-char truncation) -----


def test_over_budget_llm_output_is_truncated_deterministically_with_warnings():
    # 20 concepts with one query each: within the plan-level hard caps
    # (50/60) but over the 10/15 single-completion budget.  The plan must
    # survive, cut to the budget, with every cut auditable.
    router = FakeRouter(json.dumps(llm_payload(20, 1)))
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "llm"
    assert len(plan.concepts) == 10
    assert len(plan.queries) == 10
    joined = "\n".join(plan.warnings)
    assert "concepts truncated from 20 to 10" in joined
    assert "10 query(ies) dropped" in joined
    # Self-consistency per ResearchPlan rules: every kept concept still has
    # at least one query.
    for concept in plan.concepts:
        assert any(query.concept_id == concept.concept_id for query in plan.queries)


def test_over_budget_query_count_truncates_and_drops_orphan_concepts():
    # 10 concepts x 5 queries = 50 queries: within the hard caps but over
    # the budget.  Queries cut to 15, then concepts left without a query
    # are dropped — also audited, never silent.
    router = FakeRouter(json.dumps(llm_payload(10, 5)))
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "llm"
    assert len(plan.queries) == 15
    assert len(plan.concepts) == 3
    joined = "\n".join(plan.warnings)
    assert "queries truncated from 50 to 15" in joined
    assert "concept(s) dropped because no query survived truncation" in joined
    queried = {query.concept_id for query in plan.queries}
    assert {concept.concept_id for concept in plan.concepts} == queried


def test_invalid_json_retries_once_with_smaller_budget():
    truncated = '{"windows": [{"phase": "current"'
    router = ScriptedRouter([truncated, json.dumps(llm_payload(2, 1))])
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "llm"
    assert plan.warnings == ()
    assert len(router.calls) == 2
    first_prompt = router.calls[0][1].lower()
    retry_prompt = router.calls[1][1].lower()
    assert "at most 10 concepts" in first_prompt
    assert "at most 15 queries" in first_prompt
    assert "at most 5 concepts" in retry_prompt
    assert "at most 7 queries" in retry_prompt
    assert "at most 10 concepts" not in retry_prompt


def test_retry_also_failing_falls_back_with_retryable_warning():
    truncated = '{"windows": [{"phase": "current"'
    router = ScriptedRouter([truncated, truncated])
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "fallback"
    assert len(router.calls) == 2  # exactly one bounded retry, never more
    joined = "\n".join(plan.warnings)
    assert "source_selector output rejected" in joined
    assert "not valid JSON" in joined
    assert "smaller-budget retry" in joined
    assert "retryable" in joined


def test_router_level_failure_does_not_burn_the_retry():
    router = ExplodingRouter(RuntimeError("provider offline"))
    plan = asyncio.run(make_planner(router).plan(make_material()))

    assert plan.origin == "fallback"
    assert len(router.calls) == 1
    assert any("RuntimeError" in warning for warning in plan.warnings)


# --- A: academic_discourse fallback is academic, not policy ------------------


def test_academic_discourse_fallback_never_uses_policy_templates():
    plan = asyncio.run(make_planner().plan(make_material(), make_extraction()))

    phases = tuple(window.phase for window in plan.windows)
    assert "proposal" not in phases
    assert "implementation" not in phases
    assert "revision" not in phases
    assert "publication" in phases
    assert phases[-1] == "current"

    for query in plan.queries:
        # The policy-template filler terms must not appear ...
        lowered = query.query.lower()
        for junk in ("proposal draft", "implementation rollout", "revision amendment"):
            assert junk not in lowered
        # ... and every query is a bounded term-level phrase, not a sentence.
        assert len(query.query) < 200
    assert plan.queries


def test_academic_discourse_fallback_queries_are_term_level_phrases():
    material = make_material(
        content="Study of heterotrophic nitrification under low temperature.\n"
        "## Aerobic denitrification\nStrain W30 removed ammonia at 15 C."
    )
    plan = asyncio.run(make_planner().plan(material, make_extraction()))

    labels = {concept.label for concept in plan.concepts}
    assert labels
    query_texts = {query.query for query in plan.queries}
    # Every concept is queried as a quoted exact phrase so academic fielded
    # backends can bind the terms instead of free-text matching.
    for label in labels:
        assert f'"{label}"' in query_texts
    for query in plan.queries:
        assert set(query.source_types) <= {"academic_paper", "academic_discussion"}


def test_academic_material_type_fallback_is_academic_without_extraction():
    plan = asyncio.run(make_planner().plan(make_material()))
    phases = tuple(window.phase for window in plan.windows)
    assert "proposal" not in phases and "implementation" not in phases
    assert all(
        set(query.source_types) <= {"academic_paper", "academic_discussion"}
        for query in plan.queries
    )


def test_academic_review_material_type_without_case_is_academic():
    # Real-run finding: the HN-AD seed material carries type
    # "academic_review" and extraction produced no case (case_type None),
    # which used to fall into the policy proposal/implementation template.
    material = make_material(type="academic_review")
    plan = asyncio.run(make_planner().plan(material))

    assert plan.origin == "fallback"
    phases = tuple(window.phase for window in plan.windows)
    for banned in ("proposal", "implementation", "revision"):
        assert banned not in phases
    assert "publication" in phases
    assert phases[-1] == "current"
    assert plan.queries
    for query in plan.queries:
        lowered = query.query.lower()
        for junk in ("proposal draft", "implementation rollout", "revision amendment"):
            assert junk not in lowered
        assert set(query.source_types) <= {"academic_paper", "academic_discussion"}


def test_academic_prefixed_material_types_are_academic_without_case():
    for material_type in ("academic_article", "academic_journal", "academic_paper"):
        material = make_material(type=material_type)
        plan = asyncio.run(make_planner().plan(material))
        phases = tuple(window.phase for window in plan.windows)
        assert "proposal" not in phases and "implementation" not in phases, material_type


def test_news_material_without_case_keeps_policy_template():
    material = make_material(type="news")
    plan = asyncio.run(make_planner().plan(material))

    phases = tuple(window.phase for window in plan.windows)
    assert "proposal" in phases and "implementation" in phases
    assert any("implementation rollout" in query.query.lower() for query in plan.queries)


def test_policy_fallback_keeps_the_policy_template():
    material = make_material(type="policy")
    plan = asyncio.run(
        make_planner().plan(material, make_extraction(case_type="policy"))
    )
    phases = tuple(window.phase for window in plan.windows)
    assert "proposal" in phases and "implementation" in phases


# --- A: deterministic caps on fallback concepts and queries ------------------


def test_fallback_concepts_and_queries_are_deterministically_bounded():
    headings = "\n".join(
        f"## Concept heading number {index}" for index in range(40)
    )
    material = make_material(content=headings)
    plan = asyncio.run(make_planner().plan(material, make_extraction()))

    assert len(plan.concepts) <= 50
    assert len(plan.concepts) <= 12  # fallback ceiling stays small on purpose
    assert len(plan.queries) <= 60
    assert any("truncated" in warning for warning in plan.warnings)

    replay = asyncio.run(make_planner().plan(material, make_extraction()))
    assert replay == plan


def test_fallback_windows_are_contiguous_within_the_frontier():
    plan = asyncio.run(make_planner().plan(make_material(), make_extraction()))
    for left, right in zip(plan.windows, plan.windows[1:]):
        assert left.end_at == right.start_at
    for window in plan.windows:
        assert window.end_at <= FETCHED + timedelta(seconds=1)
        assert window.start_at < window.end_at
