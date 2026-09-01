"""Offline contract tests for the PRISM evolution analyzer (FR-4.1 - FR-4.8)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from prism.analyzer import (
    AnalyzerService,
    ChangeReason,
    ComparisonChange,
    EvidenceGap,
    EvolutionAnalysis,
    EvolutionComparison,
    OpenQuestion,
    TimelineStage,
    TurningPoint,
)
from prism.graph import GraphTimeline, TimelineEntry


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
D = timedelta(days=1)
LATE = NOW + 10 * D
CASE = "case-policy"


def run(coro):
    return asyncio.run(coro)


class FakeGraphService:
    """In-memory graph service honoring the [valid_at, invalid_at) contract."""

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.timeline_calls = []

    async def timeline(self, case_id, as_of):
        self.timeline_calls.append((case_id, as_of))
        matched = [
            entry
            for entry in self.entries
            if entry.case_id == case_id
            and entry.valid_at <= as_of
            and (entry.invalid_at is None or as_of < entry.invalid_at)
        ]
        matched.reverse()  # deliberately unsorted to prove the analyzer sorts
        return GraphTimeline(case_id, as_of, tuple(matched))


def make_entry(
    key,
    kind,
    valid_at,
    *,
    invalid_at=None,
    summary=None,
    source_ids=("m-1",),
    stance=None,
    confidence=None,
    provenance_type=None,
    payload=None,
    case_id=CASE,
    reference_time=None,
):
    body = json.dumps(
        payload or {"episode_key": key, "kind": kind},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return TimelineEntry(
        episode_key=key,
        case_id=case_id,
        kind=kind,
        summary=summary or f"{kind} {key}",
        reference_time=reference_time or valid_at,
        valid_at=valid_at,
        invalid_at=invalid_at,
        source_ids=tuple(source_ids),
        confidence=confidence,
        provenance_type=provenance_type,
        stance=stance,
        payload=body,
    )


def node_payload(node_type, summary):
    return {"kind": "evolution_node", "node_type": node_type, "summary": summary}


def case_payload(case_type="policy"):
    return {
        "kind": "evolution_case",
        "case_type": case_type,
        "canonical_name": "Housing policy 2026",
        "status": "active",
        "node_ids": [],
    }


def claim_payload(claim_id, actor, proposition, stance, revised_by=None):
    return {
        "kind": "claim",
        "claim_id": claim_id,
        "actor": actor,
        "proposition": proposition,
        "stance": stance,
        "based_on": [],
        "revised_by": revised_by,
    }


def fact_payload(subject, predicate, obj):
    return {
        "kind": "temporal_fact",
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "observed_at": (NOW + 2 * D).isoformat(),
    }


def material_payload():
    return {
        "kind": "material_provenance",
        "material_id": "m-pub-1",
        "title": "Official notice",
        "source": "example.gov",
    }


def rich_entries():
    return [
        make_entry(
            "case-1",
            "evolution_case",
            NOW,
            source_ids=(),
            payload=case_payload(),
        ),
        make_entry(
            "claim-support",
            "claim",
            NOW + D,
            source_ids=("m-claim-1",),
            stance="support",
            summary="The policy improves disclosure.",
            payload=claim_payload(
                "claim-support", "Analyst A", "The policy improves disclosure.", "support"
            ),
        ),
        make_entry(
            "node-proposal",
            "evolution_node",
            NOW + D,
            source_ids=("m-prop",),
            summary="Proposal floated.",
            payload=node_payload("proposal", "Proposal floated."),
        ),
        make_entry(
            "node-publish",
            "evolution_node",
            NOW + 2 * D,
            source_ids=("m-pub-1", "m-pub-2"),
            summary="Policy published.",
            payload=node_payload("publication", "Policy published."),
        ),
        make_entry(
            "mat-1",
            "material_provenance",
            NOW + 2 * D,
            source_ids=("m-pub-1",),
            summary="Official notice",
            payload=material_payload(),
        ),
        make_entry(
            "fact-current",
            "temporal_fact",
            NOW + 2 * D,
            source_ids=("m-fact-2",),
            confidence=0.42,
            provenance_type="model_inference",
            summary="Agency requires Updated disclosure",
            payload=fact_payload("Agency", "requires", "Updated disclosure"),
        ),
        make_entry(
            "fact-old",
            "temporal_fact",
            NOW + 2 * D,
            invalid_at=NOW + 4 * D,
            source_ids=("m-fact",),
            confidence=0.9,
            provenance_type="explicit",
            summary="Agency required Disclosure",
            payload=fact_payload("Agency", "required", "Disclosure"),
        ),
        make_entry(
            "node-interpret",
            "evolution_node",
            NOW + 3 * D,
            source_ids=(),
            summary="Reading of the policy.",
            payload=node_payload("interpretation", "Reading of the policy."),
        ),
        make_entry(
            "claim-uncertain",
            "claim",
            NOW + 4 * D,
            source_ids=("m-claim-2",),
            stance="uncertain",
            summary="Effect on prices is unclear.",
            payload=claim_payload(
                "claim-uncertain", "Analyst B", "Effect on prices is unclear.", "uncertain"
            ),
        ),
        make_entry(
            "claim-revised",
            "claim",
            NOW + 5 * D,
            source_ids=("m-claim-3",),
            stance="oppose",
            summary="Costs outweigh benefits.",
            payload=claim_payload(
                "claim-revised",
                "Analyst C",
                "Costs outweigh benefits.",
                "oppose",
                revised_by="claim-final",
            ),
        ),
        make_entry(
            "node-revision",
            "evolution_node",
            NOW + 5 * D,
            source_ids=("m-rev",),
            summary="Scope narrowed.",
            payload=node_payload("revision", "Scope narrowed."),
        ),
        make_entry(
            "node-open",
            "evolution_node",
            NOW + 6 * D,
            source_ids=("m-open",),
            summary="Enforcement date unresolved.",
            payload=node_payload("open_question", "Enforcement date unresolved."),
        ),
    ]


class MismatchedAsOfGraphService:
    async def timeline(self, case_id, as_of):
        return GraphTimeline(case_id, as_of + D, ())


def test_analyze_rejects_timeline_returned_for_a_different_as_of():
    with pytest.raises(ValueError, match="as_of"):
        run(AnalyzerService(MismatchedAsOfGraphService()).analyze(CASE, NOW))


def test_analyze_returns_stable_layered_stages_with_preserved_provenance():
    analysis = run(AnalyzerService(FakeGraphService(rich_entries())).analyze(CASE, LATE))

    assert isinstance(analysis, EvolutionAnalysis)
    assert analysis.case_id == CASE
    assert analysis.as_of == LATE
    assert analysis.case_type == "policy"
    assert [stage.episode_key for stage in analysis.stages] == [
        "case-1",
        "claim-support",
        "node-proposal",
        "node-publish",
        "mat-1",
        "fact-current",
        "node-interpret",
        "claim-uncertain",
        "claim-revised",
        "node-revision",
        "node-open",
    ]
    assert [stage.layer for stage in analysis.stages] == [
        "fact",
        "interpretation",
        "fact",
        "fact",
        "provenance",
        "fact",
        "fact",
        "interpretation",
        "interpretation",
        "fact",
        "fact",
    ]
    assert all(isinstance(stage, TimelineStage) for stage in analysis.stages)
    assert analysis.stages[0].node_type is None
    assert analysis.stages[2].node_type == "proposal"
    assert (analysis.stages[1].kind, analysis.stages[1].stance) == ("claim", "support")
    fact_stage = analysis.stages[5]
    assert (fact_stage.confidence, fact_stage.provenance_type) == (
        0.42,
        "model_inference",
    )
    assert analysis.stages[3].source_ids == ("m-pub-1", "m-pub-2")

    repeat = run(AnalyzerService(FakeGraphService(rich_entries())).analyze(CASE, LATE))
    assert repeat == analysis

    with pytest.raises(FrozenInstanceError):
        analysis.stages = ()


def test_analyze_derives_turning_points_reasons_gaps_and_questions():
    analysis = run(AnalyzerService(FakeGraphService(rich_entries())).analyze(CASE, LATE))

    assert analysis.turning_points == (
        TurningPoint(
            "node-publish",
            "publication",
            NOW + 2 * D,
            "Policy published.",
            ("m-pub-1", "m-pub-2"),
        ),
        TurningPoint(
            "node-revision", "revision", NOW + 5 * D, "Scope narrowed.", ("m-rev",)
        ),
    )
    assert analysis.change_reasons == (
        ChangeReason(
            "claim-revised",
            "claim_revised",
            "interpretation_change",
            NOW + 5 * D,
            "Claim claim-revised was revised by claim-final.",
            ("m-claim-3",),
        ),
        ChangeReason(
            "node-revision",
            "node:revision",
            "fact_change",
            NOW + 5 * D,
            "Scope narrowed.",
            ("m-rev",),
        ),
    )
    assert len(analysis.evidence_gaps) == 1
    gap = analysis.evidence_gaps[0]
    assert isinstance(gap, EvidenceGap)
    assert gap.gap_type == "unattributed_entry"
    assert gap.episode_key == "node-interpret"
    assert "node-interpret" in gap.detail
    assert analysis.open_questions == (
        OpenQuestion(
            "claim-uncertain",
            "uncertain_claim",
            "Effect on prices is unclear.",
            "Analyst B",
            NOW + 4 * D,
            ("m-claim-2",),
        ),
        OpenQuestion(
            "node-open",
            "open_question_node",
            "Enforcement date unresolved.",
            None,
            NOW + 6 * D,
            ("m-open",),
        ),
    )


def test_analyze_marks_fact_supersession_and_treats_invalid_at_as_exclusive():
    entries = [
        make_entry(
            "case-1", "evolution_case", NOW, source_ids=(), payload=case_payload()
        ),
        make_entry(
            "fact-old",
            "temporal_fact",
            NOW + 2 * D,
            invalid_at=NOW + 4 * D,
            source_ids=("m-fact",),
            confidence=0.9,
            provenance_type="explicit",
            summary="Agency required Disclosure",
            payload=fact_payload("Agency", "required", "Disclosure"),
        ),
    ]
    service = AnalyzerService(FakeGraphService(entries))

    during = run(service.analyze(CASE, NOW + 3 * D))
    assert [stage.episode_key for stage in during.stages] == ["case-1", "fact-old"]
    assert during.turning_points == (
        TurningPoint(
            "fact-old",
            "fact_superseded",
            NOW + 4 * D,
            "Agency required Disclosure",
            ("m-fact",),
        ),
    )
    (reason,) = during.change_reasons
    assert (reason.reason_type, reason.nature, reason.at) == (
        "fact_superseded",
        "fact_change",
        NOW + 4 * D,
    )
    assert reason.summary.startswith("Agency required Disclosure")
    assert "ceased to be valid" in reason.summary

    at_invalidation = run(service.analyze(CASE, NOW + 4 * D))
    assert "fact-old" not in [
        stage.episode_key for stage in at_invalidation.stages
    ]


def test_analyze_reports_explicit_gap_for_empty_timeline_instead_of_fabricating():
    analysis = run(AnalyzerService(FakeGraphService()).analyze("case-missing", LATE))

    assert analysis.stages == ()
    assert analysis.turning_points == ()
    assert analysis.change_reasons == ()
    assert analysis.open_questions == ()
    assert analysis.case_type is None
    assert len(analysis.evidence_gaps) == 1
    gap = analysis.evidence_gaps[0]
    assert gap.gap_type == "empty_timeline"
    assert gap.episode_key is None
    assert "case-missing" in gap.detail


def test_analyze_flags_missing_case_definition():
    entries = [
        make_entry(
            "fact-current",
            "temporal_fact",
            NOW + 2 * D,
            source_ids=("m-fact-2",),
            summary="Agency requires Disclosure",
            payload=fact_payload("Agency", "requires", "Disclosure"),
        )
    ]
    analysis = run(AnalyzerService(FakeGraphService(entries)).analyze(CASE, LATE))

    assert len(analysis.stages) == 1
    assert [gap.gap_type for gap in analysis.evidence_gaps] == [
        "missing_case_definition"
    ]


def test_analyze_without_as_of_uses_injected_clock():
    fake = FakeGraphService(rich_entries())
    analysis = run(AnalyzerService(fake, clock=lambda: LATE).analyze(CASE))

    assert fake.timeline_calls == [(CASE, LATE)]
    assert analysis.as_of == LATE

    default = run(AnalyzerService(FakeGraphService(rich_entries())).analyze(CASE))
    assert default.as_of.tzinfo is not None


def test_analyze_rejects_naive_clock():
    service = AnalyzerService(FakeGraphService(), clock=lambda: datetime(2026, 8, 11))
    with pytest.raises(RuntimeError, match="timezone-aware"):
        run(service.analyze(CASE))


def test_constructor_validates_dependencies():
    with pytest.raises(ValueError, match="graph_service is required"):
        AnalyzerService(None)
    with pytest.raises(TypeError, match="graph_service must provide timeline"):
        AnalyzerService(object())
    with pytest.raises(TypeError, match="clock must be callable"):
        AnalyzerService(FakeGraphService(), clock="nope")


def test_analyze_rejects_invalid_inputs_without_calling_the_graph():
    fake = FakeGraphService(rich_entries())
    service = AnalyzerService(fake)

    with pytest.raises(ValueError, match="case_id"):
        run(service.analyze("   ", LATE))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(service.analyze(CASE, datetime(2026, 8, 11)))
    with pytest.raises(TypeError, match="kinds"):
        run(service.analyze(CASE, LATE, kinds="claim"))
    with pytest.raises(ValueError, match="kinds must not be empty"):
        run(service.analyze(CASE, LATE, kinds=()))
    with pytest.raises(ValueError, match="kinds"):
        run(service.analyze(CASE, LATE, kinds=("rumor",)))
    assert fake.timeline_calls == []


def test_kind_filter_restricts_the_analysis_view():
    analysis = run(
        AnalyzerService(FakeGraphService(rich_entries())).analyze(
            CASE, LATE, kinds=("claim",)
        )
    )

    assert [stage.episode_key for stage in analysis.stages] == [
        "claim-support",
        "claim-uncertain",
        "claim-revised",
    ]
    assert all(stage.layer == "interpretation" for stage in analysis.stages)
    assert analysis.turning_points == ()
    assert analysis.change_reasons == (
        ChangeReason(
            "claim-revised",
            "claim_revised",
            "interpretation_change",
            NOW + 5 * D,
            "Claim claim-revised was revised by claim-final.",
            ("m-claim-3",),
        ),
    )
    assert analysis.open_questions == (
        OpenQuestion(
            "claim-uncertain",
            "uncertain_claim",
            "Effect on prices is unclear.",
            "Analyst B",
            NOW + 4 * D,
            ("m-claim-2",),
        ),
    )
    assert analysis.evidence_gaps == ()


def test_compare_identifies_added_removed_and_unchanged_with_layers():
    fake = FakeGraphService(rich_entries())
    comparison = run(AnalyzerService(fake).compare(CASE, NOW + 3 * D, NOW + 6 * D))

    assert isinstance(comparison, EvolutionComparison)
    assert (comparison.case_id, comparison.earlier, comparison.later) == (
        CASE,
        NOW + 3 * D,
        NOW + 6 * D,
    )
    assert [change.episode_key for change in comparison.added] == [
        "claim-uncertain",
        "claim-revised",
        "node-revision",
        "node-open",
    ]
    assert [change.layer for change in comparison.added] == [
        "interpretation",
        "interpretation",
        "fact",
        "fact",
    ]
    assert [change.episode_key for change in comparison.removed] == ["fact-old"]
    assert comparison.removed[0].kind == "temporal_fact"
    assert comparison.removed[0].source_ids == ("m-fact",)
    assert [change.episode_key for change in comparison.unchanged] == [
        "case-1",
        "claim-support",
        "node-proposal",
        "node-publish",
        "mat-1",
        "fact-current",
        "node-interpret",
    ]
    assert fake.timeline_calls == [(CASE, NOW + 3 * D), (CASE, NOW + 6 * D)]

    with pytest.raises(FrozenInstanceError):
        comparison.added = ()


def test_compare_half_open_boundaries_at_exact_instants():
    entries = [
        make_entry(
            "edge-added",
            "evolution_node",
            NOW + 6 * D,
            source_ids=("m-a",),
            summary="Node appears exactly at later.",
            payload=node_payload("publication", "Node appears exactly at later."),
        ),
        make_entry(
            "edge-removed",
            "temporal_fact",
            NOW + D,
            invalid_at=NOW + 6 * D,
            source_ids=("m-r",),
            summary="Fact expires exactly at later.",
            payload=fact_payload("Agency", "required", "Disclosure"),
        ),
        make_entry(
            "edge-unchanged",
            "evolution_node",
            NOW + 3 * D,
            source_ids=("m-u",),
            summary="Node appears exactly at earlier.",
            payload=node_payload("draft", "Node appears exactly at earlier."),
        ),
    ]
    comparison = run(
        AnalyzerService(FakeGraphService(entries)).compare(
            CASE, NOW + 3 * D, NOW + 6 * D
        )
    )

    assert comparison.added == (
        ComparisonChange(
            "edge-added",
            "evolution_node",
            "fact",
            "Node appears exactly at later.",
            NOW + 6 * D,
            None,
            ("m-a",),
        ),
    )
    assert comparison.removed == (
        ComparisonChange(
            "edge-removed",
            "temporal_fact",
            "fact",
            "Fact expires exactly at later.",
            NOW + D,
            NOW + 6 * D,
            ("m-r",),
        ),
    )
    assert comparison.unchanged == (
        ComparisonChange(
            "edge-unchanged",
            "evolution_node",
            "fact",
            "Node appears exactly at earlier.",
            NOW + 3 * D,
            None,
            ("m-u",),
        ),
    )


def test_compare_rejects_invalid_inputs_and_handles_unknown_cases():
    service = AnalyzerService(FakeGraphService(rich_entries()))

    with pytest.raises(ValueError, match="case_id"):
        run(service.compare("   ", NOW + 3 * D, NOW + 6 * D))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(service.compare(CASE, datetime(2026, 8, 4), NOW + 6 * D))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(service.compare(CASE, NOW + 3 * D, datetime(2026, 8, 7)))
    with pytest.raises(ValueError, match="later must not be earlier than earlier"):
        run(service.compare(CASE, NOW + 6 * D, NOW + 3 * D))

    empty = run(
        AnalyzerService(FakeGraphService()).compare(
            "case-missing", NOW + 3 * D, NOW + 6 * D
        )
    )
    assert empty.added == ()
    assert empty.removed == ()
    assert empty.unchanged == ()


def test_analyze_rejects_broken_graph_service_results():
    class BadTypeService:
        async def timeline(self, case_id, as_of):
            return object()

    class WrongCaseService:
        async def timeline(self, case_id, as_of):
            return GraphTimeline("other-case", as_of, ())

    with pytest.raises(TypeError, match="must return a GraphTimeline"):
        run(AnalyzerService(BadTypeService()).analyze(CASE, LATE))
    with pytest.raises(ValueError, match="does not match"):
        run(AnalyzerService(WrongCaseService()).analyze(CASE, LATE))

    rumor = make_entry("rumor-1", "rumor", NOW)
    with pytest.raises(ValueError, match="unknown timeline entry kind"):
        run(AnalyzerService(FakeGraphService([rumor])).analyze(CASE, LATE))
