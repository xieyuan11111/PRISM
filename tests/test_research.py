"""Tests for the PRISM Evidence Discovery / Research Planner module."""

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from prism.config import PrismConfig, SourceConfig
from prism.domain import Claim, EvolutionCase, Material
from prism.extraction import ExtractionResult
from prism.llm import Completion
from prism.research import (
    PLAN_ORIGINS,
    PLAN_ORIGIN_FALLBACK,
    PLAN_ORIGIN_LLM,
    RESEARCH_PHASES,
    SOURCE_TYPES,
    SOURCE_SELECTOR_ROLE,
    ResearchPlan,
    ResearchPlanError,
    ResearchPlanner,
    ResearchWindow,
    SearchProvider,
    SearchQuery,
    SourceCandidate,
)

UTC = timezone.utc
PUBLISHED = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)
FETCHED = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
PLANNED = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
CASE_START = datetime(2025, 3, 1, 9, 0, tzinfo=UTC)
WHITELIST = ("gov.example", "news.example", "papers.example")

FALLBACK_PHASES = ("proposal", "publication", "implementation", "revision", "current")


def make_material(**overrides):
    values = {
        "id": "material-7",
        "title": "Data retention policy update",
        "source": "gov.example",
        "published_at": PUBLISHED,
        "fetched_at": FETCHED,
        "type": "policy",
        "content": "The agency revised the data retention policy.",
        "case_tags": ("case-42",),
    }
    values.update(overrides)
    return Material(**values)


def make_extraction(**overrides):
    values = {
        "case": EvolutionCase(
            case_id="case-42",
            case_type="policy",
            canonical_name="Data retention policy",
            start_at=CASE_START,
            status="active",
        ),
        "claims": (
            Claim(
                claim_id="claim-1",
                actor="Agency",
                proposition="Retention windows are extended to five years.",
                stance="support",
                stated_at=PUBLISHED,
            ),
        ),
        "warnings": ("Evidence ends at the publication date.",),
    }
    values.update(overrides)
    return ExtractionResult(**values)


def make_config(whitelist=WHITELIST):
    return PrismConfig(sources=SourceConfig(whitelist=whitelist))


def fixed_clock():
    return lambda: PLANNED


def llm_payload():
    return {
        "windows": [
            {
                "phase": "current",
                "start_at": "2026-06-01T00:00:00Z",
                "end_at": "2026-08-31T00:00:00Z",
                "focus": "Track the most recent developments.",
            },
            {
                "phase": "proposal",
                "start_at": "2025-01-01T00:00:00+00:00",
                "end_at": "2025-03-01T00:00:00+00:00",
                "focus": "Locate the precursor proposals.",
            },
        ],
        "candidates": [
            {
                "domain": "news.example",
                "source_types": ["news"],
                "priority": 2,
                "reason": "Mainstream coverage of the policy.",
            },
            {
                "domain": "gov.example",
                "source_types": ["policy_document"],
                "priority": 1,
                "reason": "Primary policy record.",
            },
        ],
        "queries": [
            {
                "query": "data retention policy latest status",
                "phase": "current",
                "source_domains": ["news.example", "gov.example"],
                "source_types": ["news", "official_statement"],
                "reason": "Catch the latest public statements.",
            },
            {
                "query": "data retention policy proposal",
                "phase": "proposal",
                "source_domains": ["gov.example"],
                "source_types": ["policy_document"],
                "reason": "Locate the original proposal text.",
            },
        ],
    }


class FakeRouter:
    def __init__(self, payload=None, *, text=None, error=None):
        if payload is not None and text is not None:
            raise ValueError("pass payload or text, not both")
        self._payload = payload
        self._text = text
        self._error = error
        self.calls = []

    async def complete(self, role, prompt):
        self.calls.append((role, prompt))
        if self._error is not None:
            raise self._error
        text = self._text if self._text is not None else json.dumps(self._payload)
        return Completion(text=text, provider="fake", model="fake-model")


def window(phase="publication", start=None, end=None, focus="Find the record."):
    return ResearchWindow(
        phase=phase,
        start_at=start or datetime(2025, 6, 1, tzinfo=UTC),
        end_at=end or datetime(2025, 9, 1, tzinfo=UTC),
        focus=focus,
    )


def candidate(domain="gov.example", source_types=("policy_document",), priority=1):
    return SourceCandidate(
        domain=domain,
        source_types=source_types,
        priority=priority,
        reason="Primary policy record.",
    )


def query(
    text="data retention policy",
    win=None,
    source_domains=("gov.example",),
    source_types=("policy_document",),
    reason="Locate the original text.",
):
    return SearchQuery(
        query=text,
        window=win or window(),
        source_domains=source_domains,
        source_types=source_types,
        reason=reason,
    )


def make_plan(**overrides):
    win = window()
    values = {
        "source_id": "material-7",
        "anchor_at": PUBLISHED,
        "frontier_at": FETCHED,
        "planned_at": PLANNED,
        "origin": PLAN_ORIGIN_FALLBACK,
        "case_tags": ("case-42",),
        "core_claims": ("Retention windows are extended to five years.",),
        "evidence_boundaries": ("Evidence ends at the publication date.",),
        "windows": (win,),
        "candidates": (candidate(),),
        "queries": (query(win=win),),
        "warnings": (),
    }
    values.update(overrides)
    return ResearchPlan(**values)


# --------------------------------------------------------------------------
# ResearchWindow contract
# --------------------------------------------------------------------------


def test_window_is_frozen_and_slotted():
    win = window()
    with pytest.raises(FrozenInstanceError):
        win.phase = "revision"
    assert not hasattr(win, "__dict__")


def test_window_accepts_known_phases_and_rejects_unknown_phase():
    for phase in ("proposal", "publication", "implementation", "revision",
                  "reversal", "replacement", "current"):
        assert window(phase=phase).phase == phase
    with pytest.raises(ValueError, match="phase"):
        window(phase="wild-guess")
    assert RESEARCH_PHASES <= {
        "proposal", "draft", "publication", "interpretation", "implementation",
        "response", "revision", "reversal", "replacement", "expiry", "debate",
        "consensus", "open_question", "current",
    }


def test_window_rejects_naive_datetimes():
    naive = datetime(2025, 6, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        window(start=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        window(end=naive)


def test_window_rejects_reverse_and_empty_windows():
    start = datetime(2025, 6, 1, tzinfo=UTC)
    later = datetime(2025, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="start_at must be earlier than end_at"):
        window(start=later, end=start)
    with pytest.raises(ValueError, match="start_at must be earlier than end_at"):
        window(start=start, end=start)


def test_window_requires_non_empty_focus_and_datetime_types():
    with pytest.raises(ValueError, match="focus"):
        window(focus="   ")
    with pytest.raises(TypeError, match="datetime"):
        window(start="2025-06-01")


# --------------------------------------------------------------------------
# SourceCandidate contract
# --------------------------------------------------------------------------


def test_candidate_normalizes_domain_and_source_types():
    cand = SourceCandidate(
        domain="Gov.Example.",
        source_types=["news", "policy_document", "news"],
        priority=2,
        reason="Primary record.",
    )
    assert cand.domain == "gov.example"
    assert cand.source_types == ("news", "policy_document")
    with pytest.raises(FrozenInstanceError):
        cand.priority = 1
    assert not hasattr(cand, "__dict__")


def test_candidate_rejects_bad_domains():
    with pytest.raises(ValueError, match="domain"):
        SourceCandidate(domain="  ", source_types=("news",), priority=1, reason="r")
    with pytest.raises(ValueError, match="domain"):
        SourceCandidate(
            domain="gov.example/path", source_types=("news",), priority=1, reason="r"
        )


def test_candidate_rejects_empty_or_unknown_source_types():
    with pytest.raises(ValueError, match="source_types"):
        SourceCandidate(domain="gov.example", source_types=(), priority=1, reason="r")
    with pytest.raises(ValueError, match="source_types"):
        SourceCandidate(
            domain="gov.example", source_types=("gossip",), priority=1, reason="r"
        )
    assert "policy_document" in SOURCE_TYPES
    assert "academic_paper" in SOURCE_TYPES


@pytest.mark.parametrize("priority", [0, 6, -1, 3.5, True, "2"])
def test_candidate_rejects_invalid_priority(priority):
    with pytest.raises((ValueError, TypeError)):
        SourceCandidate(
            domain="gov.example", source_types=("news",), priority=priority, reason="r"
        )


def test_candidate_requires_reason():
    with pytest.raises(ValueError, match="reason"):
        SourceCandidate(domain="gov.example", source_types=("news",), priority=1, reason="")


# --------------------------------------------------------------------------
# SearchQuery contract
# --------------------------------------------------------------------------


def test_query_is_frozen_and_normalizes_collections():
    win = window()
    q = SearchQuery(
        query="  policy status  ",
        window=win,
        source_domains=["news.example", "gov.example", "news.example"],
        source_types=["news", "policy_document"],
        reason="Why not.",
    )
    assert q.query == "policy status"
    assert q.source_domains == ("gov.example", "news.example")
    assert q.source_types == ("news", "policy_document")
    with pytest.raises(FrozenInstanceError):
        q.query = "other"
    assert not hasattr(q, "__dict__")


def test_query_rejects_empty_query_reason_and_collections():
    win = window()
    with pytest.raises(ValueError, match="query"):
        query(text="   ")
    with pytest.raises(ValueError, match="reason"):
        query(reason="  ")
    with pytest.raises(ValueError, match="source_domains"):
        query(source_domains=())
    with pytest.raises(ValueError, match="source_types"):
        query(source_types=())


def test_query_rejects_malformed_domains_and_windows():
    with pytest.raises(ValueError, match="source_domains"):
        query(source_domains=("gov example",))
    with pytest.raises(TypeError, match="window"):
        SearchQuery(
            query="policy",
            window="publication",
            source_domains=("gov.example",),
            source_types=("news",),
            reason="r",
        )


# --------------------------------------------------------------------------
# ResearchPlan contract
# --------------------------------------------------------------------------


def test_plan_preserves_material_context_and_is_frozen():
    plan = make_plan()
    assert plan.source_id == "material-7"
    assert plan.case_tags == ("case-42",)
    assert plan.core_claims == ("Retention windows are extended to five years.",)
    assert plan.evidence_boundaries == ("Evidence ends at the publication date.",)
    assert isinstance(plan.case_tags, tuple)
    with pytest.raises(FrozenInstanceError):
        plan.origin = PLAN_ORIGIN_LLM
    assert not hasattr(plan, "__dict__")


def test_plan_rejects_unknown_origin_naive_times_and_bad_text():
    with pytest.raises(ValueError, match="origin"):
        make_plan(origin="guesswork")
    assert PLAN_ORIGINS == frozenset({PLAN_ORIGIN_LLM, PLAN_ORIGIN_FALLBACK})
    with pytest.raises(ValueError, match="timezone-aware"):
        make_plan(anchor_at=datetime(2025, 6, 1))
    with pytest.raises(ValueError, match="source_id"):
        make_plan(source_id=" ")
    with pytest.raises(ValueError, match="case_tags"):
        make_plan(case_tags=(" ",))
    with pytest.raises(ValueError, match="core_claims"):
        make_plan(core_claims=("",))


def test_plan_orders_windows_candidates_and_queries_deterministically():
    early = window(phase="proposal", start=datetime(2024, 6, 1, tzinfo=UTC),
                   end=datetime(2024, 9, 1, tzinfo=UTC))
    late = window(phase="current", start=datetime(2026, 6, 1, tzinfo=UTC),
                  end=datetime(2026, 8, 31, tzinfo=UTC))
    q_early = query(text="alpha proposal", win=early)
    q_late = query(text="zulu status", win=late)
    plan = make_plan(
        windows=[late, early],
        candidates=[candidate("news.example", priority=2), candidate("gov.example", priority=1)],
        queries=[q_late, q_early],
    )
    assert plan.windows == (early, late)
    assert [c.domain for c in plan.candidates] == ["gov.example", "news.example"]
    assert plan.queries == (q_early, q_late)


def test_plan_rejects_queries_outside_declared_windows_or_candidates():
    orphan_window = window(phase="revision", start=datetime(2025, 10, 1, tzinfo=UTC),
                           end=datetime(2025, 12, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="window"):
        make_plan(queries=(query(win=orphan_window),))
    with pytest.raises(ValueError, match="not among the plan candidates"):
        make_plan(queries=(query(source_domains=("papers.example",),),))


def test_plan_rejects_duplicates_and_wrong_member_types():
    with pytest.raises(ValueError, match="phase"):
        make_plan(windows=(window(), window(phase="publication")))
    with pytest.raises(ValueError, match="domain"):
        make_plan(candidates=(candidate(), candidate()))
    with pytest.raises(ValueError, match="duplicate"):
        make_plan(queries=(query(), query()))
    with pytest.raises(TypeError, match="ResearchWindow"):
        make_plan(windows=("publication",))
    with pytest.raises(TypeError, match="SourceCandidate"):
        make_plan(candidates=("gov.example",))
    with pytest.raises(TypeError, match="SearchQuery"):
        make_plan(queries=("find it",))
    with pytest.raises(ValueError, match="warnings"):
        make_plan(warnings=("",))


# --------------------------------------------------------------------------
# ResearchPlanner construction and input validation
# --------------------------------------------------------------------------


def test_planner_rejects_bad_constructor_arguments():
    with pytest.raises(TypeError, match="config"):
        ResearchPlanner({"sources": WHITELIST})
    with pytest.raises(TypeError, match="router"):
        ResearchPlanner(make_config(), router=object())
    with pytest.raises(TypeError, match="clock"):
        ResearchPlanner(make_config(), clock="now")


def test_planner_rejects_bad_plan_inputs():
    planner = ResearchPlanner(make_config(), clock=fixed_clock())
    with pytest.raises(TypeError, match="material"):
        asyncio.run(planner.plan("material-7"))
    with pytest.raises(TypeError, match="extraction"):
        asyncio.run(planner.plan(make_material(), extraction={"case": None}))
    with pytest.raises(ValueError, match="core_claims"):
        asyncio.run(planner.plan(make_material(), core_claims=(" ",)))
    with pytest.raises(ValueError, match="evidence_boundaries"):
        asyncio.run(
            planner.plan(make_material(), evidence_boundaries=("unsupported", ""))
        )


# --------------------------------------------------------------------------
# Deterministic fallback (no router) — FR-1.17
# --------------------------------------------------------------------------


def test_fallback_plan_without_router_is_complete_and_deterministic():
    planner = ResearchPlanner(make_config(), clock=fixed_clock())
    plan_a = asyncio.run(planner.plan(make_material(), make_extraction()))
    plan_b = asyncio.run(planner.plan(make_material(), make_extraction()))
    assert plan_a == plan_b
    assert plan_a.origin == PLAN_ORIGIN_FALLBACK
    assert plan_a.planned_at == PLANNED
    assert plan_a.source_id == "material-7"
    assert plan_a.case_tags == ("case-42",)
    assert plan_a.core_claims == ("Retention windows are extended to five years.",)
    assert plan_a.evidence_boundaries == ("Evidence ends at the publication date.",)
    assert plan_a.anchor_at == CASE_START
    assert plan_a.frontier_at == FETCHED


def test_fallback_windows_are_phased_sorted_and_contiguous():
    planner = ResearchPlanner(make_config(), clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material(), make_extraction()))
    phases = tuple(w.phase for w in plan.windows)
    assert phases == FALLBACK_PHASES
    first = plan.windows[0]
    assert first.start_at == CASE_START - timedelta(days=365)
    assert first.end_at == CASE_START
    last = plan.windows[-1]
    assert last.phase == "current"
    assert last.end_at == FETCHED
    for left, right in zip(plan.windows, plan.windows[1:]):
        assert left.end_at == right.start_at
    assert all(w.focus.strip() for w in plan.windows)


def test_fallback_uses_whitelist_candidates_and_one_query_per_window():
    planner = ResearchPlanner(make_config(), clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert [c.domain for c in plan.candidates] == list(sorted(WHITELIST))
    assert all(c.priority == 1 for c in plan.candidates)
    assert all(c.reason.strip() for c in plan.candidates)
    assert len(plan.queries) == len(plan.windows)
    domains = {c.domain for c in plan.candidates}
    for q in plan.queries:
        assert q.query.strip()
        assert q.reason.strip()
        assert q.source_domains
        assert set(q.source_domains) <= domains
        assert q.source_types
        assert set(q.source_types) <= SOURCE_TYPES
        assert q.window in plan.windows
    # Without an extraction the anchor is the publication time.
    assert plan.anchor_at == PUBLISHED
    assert plan.core_claims == ()
    assert plan.evidence_boundaries == ()


def test_fallback_with_empty_whitelist_warns_and_plans_no_queries():
    planner = ResearchPlanner(make_config(whitelist=()), clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.origin == PLAN_ORIGIN_FALLBACK
    assert plan.candidates == ()
    assert plan.queries == ()
    assert plan.windows
    assert any("whitelist" in warning for warning in plan.warnings)


def test_explicit_claims_and_boundaries_win_over_extraction():
    planner = ResearchPlanner(make_config(), clock=fixed_clock())
    plan = asyncio.run(
        planner.plan(
            make_material(),
            make_extraction(),
            core_claims=["Retention periods doubled."],
            evidence_boundaries=["No post-2026 evidence."],
        )
    )
    assert plan.core_claims == ("Retention periods doubled.",)
    assert plan.evidence_boundaries == ("No post-2026 evidence.",)


# --------------------------------------------------------------------------
# LLM source selection (source_selector) — FR-1.14 ~ FR-1.16
# --------------------------------------------------------------------------


def test_valid_llm_payload_produces_llm_plan():
    router = FakeRouter(llm_payload())
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material(), make_extraction()))
    assert plan.origin == PLAN_ORIGIN_LLM
    assert plan.warnings == ()
    assert [w.phase for w in plan.windows] == ["proposal", "current"]
    assert [c.domain for c in plan.candidates] == ["gov.example", "news.example"]
    assert [q.query for q in plan.queries] == [
        "data retention policy proposal",
        "data retention policy latest status",
    ]
    assert plan.queries[1].source_domains == ("gov.example", "news.example")
    assert plan.queries[1].window is plan.windows[1]
    role, prompt = router.calls[0]
    assert role == SOURCE_SELECTOR_ROLE
    assert "gov.example" in prompt and "news.example" in prompt
    assert "papers.example" in prompt
    assert "material-7" in prompt
    assert "Data retention policy" in prompt


def test_llm_window_must_not_extend_beyond_material_frontier():
    payload = llm_payload()
    payload["windows"][0]["start_at"] = "2026-08-30T00:00:00Z"
    payload["windows"][0]["end_at"] = "2026-09-01T00:00:00Z"
    router = FakeRouter(payload)
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())

    plan = asyncio.run(planner.plan(make_material()))

    assert plan.origin == PLAN_ORIGIN_FALLBACK
    assert any("horizon" in warning or "frontier" in warning for warning in plan.warnings)


def test_llm_payload_with_fenced_json_is_accepted():
    router = FakeRouter(text="```json\n" + json.dumps(llm_payload()) + "\n```")
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.origin == PLAN_ORIGIN_LLM
    assert len(plan.queries) == 2


def _fallback_after_bad_payload(mutate):
    payload = llm_payload()
    mutate(payload)
    router = FakeRouter(payload)
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.origin == PLAN_ORIGIN_FALLBACK
    assert plan.warnings
    assert [c.domain for c in plan.candidates] == list(sorted(WHITELIST))
    return plan


def test_llm_failure_routes_fall_back_with_warning():
    router = FakeRouter(error=RuntimeError("provider offline"))
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.origin == PLAN_ORIGIN_FALLBACK
    assert any("RuntimeError" in w for w in plan.warnings)
    assert len(router.calls) == 1


def test_non_json_completion_falls_back():
    router = FakeRouter(text="I think you should search the web.")
    planner = ResearchPlanner(make_config(), router=router, clock=fixed_clock())
    plan = asyncio.run(planner.plan(make_material()))
    assert plan.origin == PLAN_ORIGIN_FALLBACK
    assert plan.warnings


def test_invented_domain_outside_whitelist_is_rejected():
    def mutate(payload):
        payload["candidates"][0]["domain"] = "totally-invented.example"

    _fallback_after_bad_payload(mutate)


def test_query_domain_missing_from_candidates_is_rejected():
    def mutate(payload):
        payload["queries"][1]["source_domains"] = ["papers.example"]

    _fallback_after_bad_payload(mutate)


def test_unknown_phase_and_dangling_query_phase_are_rejected():
    def mutate_phase(payload):
        payload["windows"][0]["phase"] = "hibernation"

    def mutate_dangling(payload):
        payload["queries"][0]["phase"] = "revision"

    _fallback_after_bad_payload(mutate_phase)
    _fallback_after_bad_payload(mutate_dangling)


def test_reverse_window_and_future_window_are_rejected():
    def mutate_reverse(payload):
        payload["windows"][0]["start_at"] = "2026-07-01T00:00:00Z"
        payload["windows"][0]["end_at"] = "2026-06-01T00:00:00Z"

    def mutate_future(payload):
        payload["windows"][0]["end_at"] = "2030-01-01T00:00:00Z"

    _fallback_after_bad_payload(mutate_reverse)
    _fallback_after_bad_payload(mutate_future)


def test_naive_timestamp_in_payload_is_rejected():
    def mutate(payload):
        payload["windows"][0]["start_at"] = "2026-06-01T00:00:00"

    _fallback_after_bad_payload(mutate)


def test_empty_query_and_empty_reason_are_rejected():
    def mutate_query(payload):
        payload["queries"][0]["query"] = "   "

    def mutate_reason(payload):
        payload["candidates"][0]["reason"] = ""

    _fallback_after_bad_payload(mutate_query)
    _fallback_after_bad_payload(mutate_reason)


def test_extra_or_missing_fields_are_rejected():
    def mutate_extra(payload):
        payload["queries"][0]["url"] = "https://invented.example/search"

    def mutate_missing(payload):
        del payload["windows"][0]["focus"]

    def mutate_top(payload):
        payload["notes"] = ["extra"]

    _fallback_after_bad_payload(mutate_extra)
    _fallback_after_bad_payload(mutate_missing)
    _fallback_after_bad_payload(mutate_top)


def test_bad_priority_source_type_and_duplicate_window_phase_are_rejected():
    def mutate_priority(payload):
        payload["candidates"][0]["priority"] = 9

    def mutate_type(payload):
        payload["candidates"][0]["source_types"] = ["gossip"]

    def mutate_duplicate(payload):
        payload["windows"][1]["phase"] = "current"

    _fallback_after_bad_payload(mutate_priority)
    _fallback_after_bad_payload(mutate_type)
    _fallback_after_bad_payload(mutate_duplicate)


def test_research_plan_error_is_value_error():
    assert issubclass(ResearchPlanError, ValueError)


# --------------------------------------------------------------------------
# SearchProvider seam (future adapter boundary — no network here)
# --------------------------------------------------------------------------


def test_search_provider_protocol_is_a_structural_seam():
    class DummyProvider:
        name = "dummy"

        async def search(self, query, *, timeout=10.0):
            return ()

    assert isinstance(DummyProvider(), SearchProvider)
