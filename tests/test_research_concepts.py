"""Offline TDD coverage for concept-level research planning."""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from prism.config import PrismConfig, SourceConfig
from prism.domain import Material
from prism.research import (
    ResearchConcept,
    ResearchPlan,
    ResearchPlanError,
    ResearchPlanner,
    ResearchWindow,
    SearchQuery,
    SourceCandidate,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 1, tzinfo=UTC)
WINDOW = ResearchWindow("current", datetime(2026, 8, 1, tzinfo=UTC), NOW, "current")
CANDIDATE = SourceCandidate("example.gov", ("news",), 1, "source")


def make_material(**overrides):
    values = {
        "id": "m-1",
        "title": "Data retention and privacy policy",
        "source": "example.gov",
        "published_at": datetime(2026, 1, 1, tzinfo=UTC),
        "fetched_at": NOW,
        "type": "policy",
        "content": "The agency changed data retention and privacy controls.",
    }
    values.update(overrides)
    return Material(**values)


def make_plan(**overrides):
    values = {
        "source_id": "m-1",
        "anchor_at": NOW,
        "frontier_at": NOW,
        "planned_at": NOW,
        "origin": "fallback",
        "windows": (WINDOW,),
        "candidates": (CANDIDATE,),
        "queries": (),
    }
    values.update(overrides)
    return ResearchPlan(**values)


@pytest.mark.parametrize("target_results", [10, 20])
def test_research_concept_accepts_target_result_boundaries(target_results):
    concept = ResearchConcept("retention", "Data retention", "retention rules", (), (), target_results)
    assert concept.target_results == target_results
    assert not hasattr(concept, "__dict__")


@pytest.mark.parametrize("target_results", [9, 21])
def test_research_concept_rejects_target_results_outside_range(target_results):
    with pytest.raises(ValueError, match="target_results"):
        ResearchConcept("retention", "Data retention", "retention rules", (), (), target_results)


def test_plan_rejects_dangling_concept_reference_and_out_of_range_limit():
    concept = ResearchConcept("retention", "Data retention", "retention rules", (), (), 10)
    with pytest.raises(ValueError, match="declared concept"):
        make_plan(
            concepts=(concept,),
            queries=(SearchQuery("retention", WINDOW, ("news",), ("example.gov",), "why", concept_id="missing"),),
        )
    with pytest.raises(ValueError, match="result_limit"):
        make_plan(
            concepts=(concept,),
            queries=(
                SearchQuery(
                    "retention", WINDOW, ("news",), ("example.gov",), "why",
                    concept_id="retention", result_limit=11,
                ),
            ),
        )
    with pytest.raises(ValueError, match="result_limit"):
        SearchQuery("retention", WINDOW, ("news",), ("example.gov",), "why", concept_id="retention", result_limit=21)


def test_fallback_extracts_deduplicated_concepts_and_binds_each_query():
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))), clock=lambda: NOW
    )
    plan = asyncio.run(
        planner.plan(make_material(), core_claims=("Data retention controls changed.",))
    )
    assert plan.concepts
    assert all(concept.target_results == 10 for concept in plan.concepts)
    assert {query.concept_id for query in plan.queries} == {
        concept.concept_id for concept in plan.concepts
    }
    assert all(
        any(query.concept_id == concept.concept_id for query in plan.queries)
        for concept in plan.concepts
    )


class FakeRouter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        return type("Completion", (), {"text": json.dumps(self.payload)})()


def llm_payload():
    return {
        "concepts": [
            {
                "concept_id": "retention",
                "label": "Data retention",
                "description": "Rules governing how long data is kept.",
                "aliases": ["retention period"],
                "source_ids": ["m-1"],
                "target_results": 20,
            }
        ],
        "windows": [
            {
                "phase": "current",
                "start_at": "2026-08-01T00:00:00Z",
                "end_at": "2026-09-01T00:00:00Z",
                "focus": "current",
            }
        ],
        "candidates": [
            {"domain": "example.gov", "source_types": ["news"], "priority": 1, "reason": "source"}
        ],
        "queries": [
            {
                "query": "data retention latest",
                "phase": "current",
                "source_domains": ["example.gov"],
                "source_types": ["news"],
                "reason": "find evidence",
                "concept_id": "retention",
                "result_limit": 20,
            }
        ],
    }


def test_fallback_concept_ids_are_ascii_stable_and_bounded():
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))), clock=lambda: NOW
    )
    plan = asyncio.run(planner.plan(make_material()))

    assert plan.concepts
    assert all(concept.concept_id.startswith("concept_") for concept in plan.concepts)
    assert all(concept.concept_id[8:].isalnum() for concept in plan.concepts)
    assert len(plan.concepts) <= 50
def test_llm_concepts_are_parsed_and_queries_are_bound_to_them():
    router = FakeRouter(llm_payload())
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))), router=router, clock=lambda: NOW
    )
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.concepts[0].concept_id == "retention"
    assert plan.queries[0].concept_id == "retention"
    assert plan.queries[0].result_limit == 20
    assert "every searchable concept" in router.calls[0][1].lower()


def test_old_llm_payload_without_concepts_still_loads_and_serializes():
    payload = llm_payload()
    payload.pop("concepts")
    for query in payload["queries"]:
        query.pop("concept_id")
        query.pop("result_limit")
    router = FakeRouter(payload)
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))), router=router, clock=lambda: NOW
    )
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.concepts == ()
    assert plan.queries[0].concept_id is None
    restored = asdict(plan)
    assert restored["concepts"] == ()
    assert restored["queries"][0]["result_limit"] == 10
