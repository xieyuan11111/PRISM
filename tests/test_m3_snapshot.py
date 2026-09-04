"""M3 vertical slice: GTI-backed historical snapshots, stage filtering, compare.

This module covers the formal historical-snapshot contract (effective nodes,
facts, claims, relations, facts invalidated by the cutoff, and evidence gaps
at ``as_of``, with a fail-closed knowledge boundary so future
reference/publication evidence never enters a historical state), the
deterministic stage filter over recorded ``node_type``/``claim`` ``stance``
markers, two-instant comparison through ``AnalyzerService.compare``, the
``PrismAPI`` historical facade methods and the ``prism snapshot`` /
``prism compare`` CLI commands.

Everything is offline: synthetic graph backends, no network, no LLM, no
external services.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from prism.analyzer import (
    AnalyzerService,
    EvolutionComparison,
    HistoricalCaseState,
    STAGES,
    TimelineStage,
)
from prism.api import PrismAPI
from prism.cli import build_parser, handle_compare, handle_snapshot, main
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
    TemporalRelation,
)
from prism.graph import GraphService, GraphTimeline, TimelineEntry
from prism.graph.backend import GraphitiBackend

from graphiti_fakes import FakeGraphitiClient, FakeRegistry

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)
D = timedelta(days=1)
H = timedelta(hours=1)
CASE = "case-rates"

# Scenario instants (2026):
#   material-a published 01-10, material-b 02-10, material-c 03-01.
#   node-publish happened/observed 01-15 (only material-a knows it).
#   fact v1 effective 01-16 .. 02-20 (material-a), fact v2 from 02-20
#   (material-b), supersedes relation recorded 02-21.
#   node-retrospective happened 01-25 but is reported ONLY by material-c
#   (published 03-01); claim-future stated 01-22 but recorded only by
#   material-c.  Both must stay out of any state before 03-01.
MAT_A = BASE + 9 * D
MAT_B = BASE + 40 * D
MAT_C = BASE + 59 * D
NODE_PUBLISH_AT = BASE + 14 * D
V1_FROM = BASE + 15 * D
V1_UNTIL = BASE + 50 * D
V2_FROM = BASE + 50 * D
V2_OBSERVED = BASE + 51 * D
CLAIM_SUPPORT_AT = BASE + 19 * D
RETRO_HAPPENED = BASE + 24 * D
CLAIM_FUTURE_STATED = BASE + 21 * D
EARLY = BASE + 32 * D  # 2026-02-02: only material-a knowledge is available
LATER = BASE + 70 * D  # 2026-03-12: everything is known

MATERIAL_A = "material-a"
MATERIAL_B = "material-b"
MATERIAL_C = "material-c"


def run(coro):
    return asyncio.run(coro)


def loc(source_id: str, quote: str) -> EvidenceLocator:
    return EvidenceLocator(
        source_id,
        f"corpus/2026-01/example.gov/{source_id}.md",
        paragraph=1,
        quote=quote,
    )


# ---------------------------------------------------------------- analyzer fakes


class FakeBackend:
    """In-memory GraphBackend with no database, network, or LLM."""

    def __init__(self):
        self.episodes = {}
        self.search_calls = []

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        self.search_calls.append(query)
        return tuple(self.episodes.values())


class ContractReader:
    """An analyzer graph reader honoring the GraphTimeline contract.

    Mirrors GraphService.timeline semantics for direct TimelineEntry input:
    only entries known (``reference_time``) and effective (half-open
    ``[valid_at, invalid_at)``) at the cutoff are listed, and entries whose
    ``invalid_at`` is at or before the cutoff move to ``invalidated_entries``.
    """

    def __init__(self, entries=()):
        self.entries = tuple(entries)
        self.timeline_calls = []

    async def timeline(self, case_id, as_of):
        self.timeline_calls.append((case_id, as_of))
        eligible = [
            entry
            for entry in self.entries
            if entry.case_id == case_id
            and entry.reference_time <= as_of
            and entry.valid_at <= as_of
        ]
        active = []
        invalidated = []
        for entry in eligible:
            if entry.invalid_at is not None and entry.invalid_at <= as_of:
                invalidated.append(entry)
            else:
                active.append(entry)
        active.sort(
            key=lambda entry: (
                entry.valid_at, entry.reference_time, entry.kind, entry.episode_key,
            )
        )
        invalidated.sort(
            key=lambda entry: (
                entry.invalid_at or entry.valid_at,
                entry.reference_time,
                entry.kind,
                entry.episode_key,
            )
        )
        return GraphTimeline(case_id, as_of, tuple(active), tuple(invalidated))


def make_entry(
    key,
    kind,
    valid_at,
    *,
    reference_time=None,
    invalid_at=None,
    summary=None,
    source_ids=None,
    stance=None,
    node_type=None,
    evidence=(),
    payload=None,
    case_id=CASE,
):
    body = {"episode_key": key, "kind": kind}
    if node_type is not None:
        body["node_type"] = node_type
    if stance is not None:
        body["stance"] = stance
    if payload:
        body.update(payload)
    serialized = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    evidence = tuple(evidence)
    if source_ids is None:
        source_ids = (
            tuple(dict.fromkeys(item.source_id for item in evidence)) or ("m-1",)
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
        confidence=None,
        provenance_type=None,
        stance=stance,
        payload=serialized,
        evidence=evidence,
    )


def case_entry(status="active", at=BASE):
    return make_entry(
        "case-1",
        "evolution_case",
        at,
        source_ids=(),
        summary="Rate policy 2026",
        payload={
            "kind": "evolution_case",
            "case_type": "policy",
            "canonical_name": "Rate policy 2026",
            "status": status,
            "node_ids": [],
        },
    )


def staged_entries():
    """One entry per bucket/layer for snapshot projection tests."""
    return [
        case_entry(),
        make_entry(
            "node-pub",
            "evolution_node",
            BASE + 5 * D,
            node_type="publication",
            summary="Policy published.",
            evidence=(loc(MATERIAL_A, "The policy was published."),),
        ),
        make_entry(
            "node-rev",
            "evolution_node",
            BASE + 8 * D,
            node_type="revision",
            summary="Scope narrowed.",
            evidence=(loc(MATERIAL_A, "The scope was narrowed."),),
        ),
        make_entry(
            "node-con",
            "evolution_node",
            BASE + 9 * D,
            node_type="consensus",
            summary="A consensus formed.",
            evidence=(loc(MATERIAL_A, "A consensus formed."),),
        ),
        make_entry(
            "claim-yes",
            "claim",
            BASE + 6 * D,
            stance="support",
            summary="The rate change helps.",
            evidence=(loc(MATERIAL_A, "The rate change helps."),),
            payload={"kind": "claim", "claim_id": "claim-yes", "claim_type": "interpretation"},
        ),
        make_entry(
            "claim-no",
            "claim",
            BASE + 7 * D,
            stance="oppose",
            summary="The rate change hurts.",
            evidence=(loc(MATERIAL_A, "The rate change hurts."),),
            payload={"kind": "claim", "claim_id": "claim-no", "claim_type": "interpretation"},
        ),
        make_entry(
            "fact-1",
            "temporal_fact",
            BASE + 5 * D,
            summary="Agency set Rate=3%",
            payload={
                "kind": "temporal_fact",
                "subject": "Agency",
                "predicate": "set",
                "object": "Rate=3%",
            },
        ),
        make_entry(
            "rel-1",
            "temporal_relation",
            BASE + 5 * D,
            summary="fact-new supersedes fact-old",
            payload={
                "kind": "temporal_relation",
                "relation_id": "rel-1",
                "relation_type": "supersedes",
                "source_ref": "fact-new",
                "target_ref": "fact-old",
            },
        ),
    ]


# ------------------------------------------------------------- analyzer snapshot


def test_stage_vocabulary_is_the_recorded_node_and_stance_markers():
    assert "publication" in STAGES
    assert "consensus" in STAGES
    assert "support" in STAGES
    assert "oppose" in STAGES
    assert "rumor" not in STAGES
    assert isinstance(STAGES, frozenset)


def test_snapshot_projects_effective_state_with_layers_and_preserved_evidence():
    service = AnalyzerService(ContractReader(staged_entries()))

    snapshot = run(service.snapshot(CASE, BASE + 20 * D))

    assert isinstance(snapshot, HistoricalCaseState)
    assert (snapshot.case_id, snapshot.cutoff_at) == (CASE, BASE + 20 * D)
    assert snapshot.case_type == "policy"
    assert snapshot.status == "active"
    assert [stage.episode_key for stage in snapshot.nodes] == [
        "node-pub", "node-rev", "node-con",
    ]
    assert all(stage.layer == "fact" for stage in snapshot.nodes)
    assert [stage.episode_key for stage in snapshot.facts] == ["fact-1"]
    assert [stage.episode_key for stage in snapshot.interpretations] == [
        "claim-yes", "claim-no",
    ]
    assert all(stage.layer == "interpretation" for stage in snapshot.interpretations)
    assert [stage.episode_key for stage in snapshot.relations] == ["rel-1"]
    assert snapshot.invalidated_facts == ()
    assert snapshot.evidence_gaps == ()
    # Evidence and sources survive the projection verbatim.
    claim = snapshot.interpretations[0]
    assert claim.source_ids == (MATERIAL_A,)
    assert claim.evidence == (loc(MATERIAL_A, "The rate change helps."),)
    assert claim.node_type is None

    repeat = run(service.snapshot(CASE, BASE + 20 * D))
    assert repeat == snapshot


def test_snapshot_rejects_naive_cutoff_and_invalid_stage_before_fetching():
    reader = ContractReader(staged_entries())
    service = AnalyzerService(reader)

    with pytest.raises(ValueError, match="timezone-aware"):
        run(service.snapshot(CASE, datetime(2026, 2, 1)))
    with pytest.raises(ValueError, match="stage"):
        run(service.snapshot(CASE, BASE + 20 * D, stage="rumor"))
    with pytest.raises(ValueError, match="stage"):
        run(service.snapshot(CASE, BASE + 20 * D, stage=""))
    assert reader.timeline_calls == []


def test_snapshot_stage_filter_selects_only_recorded_markers():
    service = AnalyzerService(ContractReader(staged_entries()))

    publication = run(service.snapshot(CASE, BASE + 20 * D, stage="publication"))
    assert [stage.episode_key for stage in publication.nodes] == ["node-pub"]
    assert publication.nodes[0].node_type == "publication"
    assert publication.nodes[0].layer == "fact"
    assert publication.facts == ()
    assert publication.interpretations == ()
    assert publication.relations == ()

    support = run(service.snapshot(CASE, BASE + 20 * D, stage="support"))
    assert [stage.episode_key for stage in support.interpretations] == ["claim-yes"]
    assert support.interpretations[0].layer == "interpretation"
    assert support.nodes == ()
    assert support.facts == ()

    oppose = run(service.snapshot(CASE, BASE + 20 * D, stage="oppose"))
    assert [stage.episode_key for stage in oppose.interpretations] == ["claim-no"]

    consensus = run(service.snapshot(CASE, BASE + 20 * D, stage="consensus"))
    assert [stage.episode_key for stage in consensus.nodes] == ["node-con"]

    revision = run(service.snapshot(CASE, BASE + 20 * D, stage="revision"))
    assert [stage.episode_key for stage in revision.nodes] == ["node-rev"]


def test_snapshot_stage_filter_keeps_facts_and_interpretations_separated():
    """A node stage never returns claim entries and a stance stage never
    returns node or fact entries; layers are re-derived from the entry kind."""
    service = AnalyzerService(ContractReader(staged_entries()))

    publication = run(service.snapshot(CASE, BASE + 20 * D, stage="publication"))
    assert not any(stage.kind == "claim" for stage in publication.nodes)
    assert not any(stage.kind == "temporal_fact" for stage in publication.nodes)
    support = run(service.snapshot(CASE, BASE + 20 * D, stage="support"))
    assert not any(stage.kind == "evolution_node" for stage in support.interpretations)
    assert {stage.layer for stage in support.interpretations} == {"interpretation"}
    assert support.interpretations[0].evidence == (
        loc(MATERIAL_A, "The rate change helps."),
    )


def test_snapshot_stage_without_members_keeps_case_metadata_and_no_fake_gap():
    entries = [
        case_entry(),
        make_entry(
            "node-pub",
            "evolution_node",
            BASE + 5 * D,
            node_type="publication",
            summary="Policy published.",
            evidence=(loc(MATERIAL_A, "The policy was published."),),
        ),
    ]
    service = AnalyzerService(ContractReader(entries))

    snapshot = run(service.snapshot(CASE, BASE + 20 * D, stage="consensus"))

    assert snapshot.nodes == ()
    assert snapshot.facts == ()
    assert snapshot.interpretations == ()
    # The case itself is still described by the snapshot; an absent stage is
    # not reported as an empty timeline.
    assert snapshot.case_type == "policy"
    assert snapshot.status == "active"
    assert snapshot.evidence_gaps == ()


def test_snapshot_stage_combines_with_the_existing_kind_filter():
    service = AnalyzerService(ContractReader(staged_entries()))

    snapshot = run(
        service.snapshot(
            CASE, BASE + 20 * D, stage="support", kinds=("claim",)
        )
    )

    assert [stage.episode_key for stage in snapshot.interpretations] == ["claim-yes"]
    assert snapshot.nodes == ()
    assert snapshot.facts == ()
    assert snapshot.evidence_gaps == ()


def test_snapshot_includes_invalidated_facts_after_their_invalid_at():
    entries = [
        case_entry(),
        make_entry(
            "fact-old",
            "temporal_fact",
            V1_FROM,
            invalid_at=V1_UNTIL,
            summary="Agency set Rate=3%",
            payload={
                "kind": "temporal_fact",
                "subject": "Agency",
                "predicate": "set",
                "object": "Rate=3%",
            },
        ),
    ]
    service = AnalyzerService(ContractReader(entries))

    during = run(service.snapshot(CASE, EARLY))
    assert [stage.episode_key for stage in during.facts] == ["fact-old"]
    assert during.invalidated_facts == ()

    after = run(service.snapshot(CASE, LATER))
    assert after.facts == ()
    assert [stage.episode_key for stage in after.invalidated_facts] == ["fact-old"]
    (invalidated,) = after.invalidated_facts
    assert invalidated.kind == "temporal_fact"
    assert invalidated.invalid_at == V1_UNTIL
    assert invalidated.layer == "fact"


# ------------------------------------------------- snapshot knowledge boundary


class LeakyFutureReader:
    """A reader that lets an entry known only after the cutoff into entries."""

    async def timeline(self, case_id, as_of):
        leaked = make_entry(
            "leak",
            "evolution_node",
            BASE + 5 * D,
            reference_time=BASE + 40 * D,
            node_type="publication",
            summary="Published later.",
        )
        return GraphTimeline(case_id, as_of, (leaked,))


class LeakyWindowReader:
    """A reader listing an entry as effective outside its validity window."""

    async def timeline(self, case_id, as_of):
        future = make_entry(
            "future",
            "evolution_node",
            BASE + 40 * D,
            reference_time=BASE + 5 * D,
            node_type="publication",
            summary="Not yet valid.",
        )
        expired = make_entry(
            "expired",
            "temporal_fact",
            BASE + 5 * D,
            invalid_at=BASE + 10 * D,
            summary="Already expired.",
        )
        return GraphTimeline(case_id, as_of, (future, expired))


class LeakyInvalidatedReader:
    """A reader listing a still-valid entry as invalidated."""

    async def timeline(self, case_id, as_of):
        still_valid = make_entry(
            "still-valid",
            "temporal_fact",
            BASE + 5 * D,
            invalid_at=None,
            summary="Still effective.",
        )
        future_known = make_entry(
            "future-known",
            "temporal_fact",
            BASE + 5 * D,
            invalid_at=BASE + 10 * D,
            reference_time=BASE + 40 * D,
            summary="Invalidated but only known later.",
        )
        return GraphTimeline(case_id, as_of, (), (still_valid, future_known))


def test_snapshot_fails_closed_when_reader_leaks_future_knowledge():
    with pytest.raises(ValueError, match="after as_of"):
        run(AnalyzerService(LeakyFutureReader()).snapshot(CASE, EARLY))


def test_snapshot_fails_closed_when_reader_lists_ineffective_entries():
    with pytest.raises(ValueError, match=r"valid_at"):
        run(AnalyzerService(LeakyWindowReader()).snapshot(CASE, EARLY))


def test_snapshot_fails_closed_when_reader_misplaces_invalidated_entries():
    with pytest.raises(ValueError, match="invalidated"):
        run(AnalyzerService(LeakyInvalidatedReader()).snapshot(CASE, EARLY))


# ------------------------------------------------- full-stack GTI-shaped path


def rates_material(material_id: str, published_at: datetime) -> Material:
    return Material(
        id=material_id,
        title=f"Material {material_id}",
        source="example.gov",
        published_at=published_at,
        fetched_at=published_at + H,
        type="policy",
        content=f"Body of {material_id}.",
        original_format="md",
    )


def rates_case() -> EvolutionCase:
    return EvolutionCase(
        case_id=CASE,
        case_type="policy",
        canonical_name="Rate policy 2026",
        start_at=BASE,
        status="active",
    )


def add_rates_case(service: GraphService) -> EvolutionCase:
    """Write the full M3 scenario through one GraphService."""
    case = rates_case()
    publication = EvolutionNode(
        id="node-publish",
        case_id=case.case_id,
        node_type="publication",
        happened_at=NODE_PUBLISH_AT,
        summary="The rate policy was published.",
        source_ids=(MATERIAL_A,),
        evidence=(loc(MATERIAL_A, "The rate policy was published."),),
        provenance_type="source_explicit",
    )
    retrospective = EvolutionNode(
        id="node-retrospective",
        case_id=case.case_id,
        node_type="response",
        happened_at=RETRO_HAPPENED,
        summary="An early market response reported only by a later material.",
        source_ids=(MATERIAL_C,),
        evidence=(loc(MATERIAL_C, "An early market response."),),
        provenance_type="source_explicit",
    )
    v1 = TemporalFact(
        "Agency",
        "set",
        "Rate=3%",
        V1_FROM,
        V1_UNTIL,
        V1_FROM,
        (MATERIAL_A,),
        0.95,
        "source_explicit",
        (loc(MATERIAL_A, "The rate is set at 3%."),),
        fact_id="fact-rates-v1",
    )
    v2 = TemporalFact(
        "Agency",
        "set",
        "Rate=3.5%",
        V2_FROM,
        None,
        V2_OBSERVED,
        (MATERIAL_B,),
        0.95,
        "source_explicit",
        (loc(MATERIAL_B, "The rate is set at 3.5%."),),
        fact_id="fact-rates-v2",
    )
    support = Claim(
        claim_id="claim-support",
        actor="Analyst A",
        proposition="The rate change will help homeowners.",
        stance="support",
        stated_at=CLAIM_SUPPORT_AT,
        based_on=(MATERIAL_A,),
        evidence=(loc(MATERIAL_A, "The rate change will help homeowners."),),
        observed_at=CLAIM_SUPPORT_AT,
    )
    future_claim = Claim(
        claim_id="claim-future",
        actor="Analyst B",
        proposition="The market impact remains unclear.",
        stance="uncertain",
        stated_at=CLAIM_FUTURE_STATED,
        based_on=(MATERIAL_C,),
        evidence=(loc(MATERIAL_C, "The market impact remains unclear."),),
    )
    supersession = TemporalRelation(
        relation_id="rel-rates",
        relation_type="supersedes",
        source_ref="fact-rates-v2",
        target_ref="fact-rates-v1",
        valid_at=V2_FROM,
        invalid_at=None,
        observed_at=V2_OBSERVED,
        source_ids=(MATERIAL_B,),
        evidence=(loc(MATERIAL_B, "The new rate supersedes the old rate."),),
        provenance_type="source_explicit",
    )
    write = run(
        service.add_case(
            case,
            nodes=(publication, retrospective),
            facts=(v1, v2),
            claims=(support, future_claim),
            relations=(supersession,),
            materials=(
                rates_material(MATERIAL_A, MAT_A),
                rates_material(MATERIAL_B, MAT_B),
                rates_material(MATERIAL_C, MAT_C),
            ),
        )
    )
    assert write.added_keys
    return case


def test_historical_snapshot_never_leaks_future_reference_evidence():
    """End-to-end: an earlier cutoff sees only material-a knowledge; facts,
    claims and nodes recorded only by later materials stay out of it."""
    graph = GraphService(FakeBackend())
    case = add_rates_case(graph)
    service = AnalyzerService(graph)

    early = run(service.snapshot(case.case_id, EARLY))

    assert [stage.node_type for stage in early.nodes] == ["publication"]
    assert {stage.summary for stage in early.facts} == {"Agency set Rate=3%"}
    assert [stage.summary for stage in early.interpretations] == [
        "The rate change will help homeowners."
    ]
    assert early.relations == ()
    assert early.invalidated_facts == ()
    assert early.evidence_gaps == ()
    # Evidence stays bound to the historical facts it came from.
    (fact,) = early.facts
    assert fact.source_ids == (MATERIAL_A,)
    assert fact.evidence == (loc(MATERIAL_A, "The rate is set at 3%."),)


def test_historical_snapshot_later_cutoff_sees_invalidated_facts_and_new_entries():
    graph = GraphService(FakeBackend())
    case = add_rates_case(graph)
    service = AnalyzerService(graph)

    later = run(service.snapshot(case.case_id, LATER))

    assert {stage.node_type for stage in later.nodes} == {"publication", "response"}
    assert {stage.summary for stage in later.facts} == {"Agency set Rate=3.5%"}
    assert {stage.summary for stage in later.interpretations} == {
        "The rate change will help homeowners.",
        "The market impact remains unclear.",
    }
    assert len(later.relations) == 1
    (relation,) = later.relations
    assert relation.relation_type == "supersedes"
    assert relation.source_ref == "fact-rates-v2"
    assert relation.target_ref == "fact-rates-v1"
    assert relation.evidence == (loc(MATERIAL_B, "The new rate supersedes the old rate."),)
    # The superseded fact is no longer effective but stays auditable with its
    # original sources and evidence.
    assert len(later.invalidated_facts) == 1
    (old,) = later.invalidated_facts
    assert old.kind == "temporal_fact"
    assert old.invalid_at == V1_UNTIL
    assert old.source_ids == (MATERIAL_A,)
    assert old.evidence == (loc(MATERIAL_A, "The rate is set at 3%."),)
    assert later.evidence_gaps == ()


def test_compare_reports_added_removed_unchanged_between_two_instants():
    graph = GraphService(FakeBackend())
    case = add_rates_case(graph)
    service = AnalyzerService(graph)

    comparison = run(service.compare(case.case_id, EARLY, LATER))

    assert isinstance(comparison, EvolutionComparison)
    assert (comparison.case_id, comparison.earlier, comparison.later) == (
        CASE,
        EARLY,
        LATER,
    )
    removed = {change.summary for change in comparison.removed}
    assert removed == {"Agency set Rate=3%"}
    assert comparison.removed[0].layer == "fact"
    added = {change.summary for change in comparison.added}
    assert {"Agency set Rate=3.5%", "The market impact remains unclear."} <= added
    assert "The rate policy was published." in {
        change.summary for change in comparison.unchanged
    }
    assert "The rate change will help homeowners." in {
        change.summary for change in comparison.unchanged
    }
    # Invalid comparisons are refused before any graph read.
    reader = ContractReader(())
    service_refused = AnalyzerService(reader)
    with pytest.raises(ValueError, match="later must not be earlier"):
        run(service_refused.compare(CASE, LATER, EARLY))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(service_refused.compare(CASE, datetime(2026, 2, 1), LATER))
    assert reader.timeline_calls == []


def test_snapshot_stage_filter_over_real_graph_service():
    graph = GraphService(FakeBackend())
    case = add_rates_case(graph)
    service = AnalyzerService(graph)

    publication = run(service.snapshot(case.case_id, LATER, stage="publication"))
    assert [stage.node_type for stage in publication.nodes] == ["publication"]
    assert publication.nodes[0].evidence
    assert publication.interpretations == ()

    response = run(service.snapshot(case.case_id, LATER, stage="response"))
    assert [stage.node_type for stage in response.nodes] == ["response"]

    uncertain = run(service.snapshot(case.case_id, LATER, stage="uncertain"))
    assert [stage.summary for stage in uncertain.interpretations] == [
        "The market impact remains unclear."
    ]

    consensus = run(service.snapshot(case.case_id, LATER, stage="consensus"))
    assert consensus.nodes == ()
    assert consensus.case_type == "policy"
    assert consensus.evidence_gaps == ()


def test_snapshot_and_compare_survive_adapter_restart_readback():
    """Registry-based restart readback: a fresh backend over the same store
    and durable registry rebuilds identical snapshots and comparisons."""
    client = FakeGraphitiClient()
    registry = FakeRegistry()
    first = GraphService(
        GraphitiBackend(
            client, group_id="neo4j", episode_type_json="json", registry=registry
        )
    )
    case = add_rates_case(first)

    second = GraphService(
        GraphitiBackend(
            client, group_id="neo4j", episode_type_json="json", registry=registry
        )
    )
    first_analysis = AnalyzerService(first)
    second_analysis = AnalyzerService(second)

    first_snapshot = run(first_analysis.snapshot(case.case_id, LATER))
    second_snapshot = run(second_analysis.snapshot(case.case_id, LATER))
    assert second_snapshot == first_snapshot
    assert len(second_snapshot.invalidated_facts) == 1

    first_comparison = run(first_analysis.compare(case.case_id, EARLY, LATER))
    second_comparison = run(second_analysis.compare(case.case_id, EARLY, LATER))
    assert second_comparison == first_comparison
    assert {change.summary for change in first_comparison.removed} == {
        "Agency set Rate=3%"
    }


# --------------------------------------------------------------------- facade


class FakeIngestionService:
    def __init__(self):
        self.calls = []

    def ingest(self, path, metadata=None):
        self.calls.append((path, metadata))
        raise AssertionError("facade history tests never ingest")


class FakeEvidenceStore:
    def __init__(self):
        self.calls = []

    def index_file(self, path):
        self.calls.append(path)
        raise AssertionError("facade history tests never index")

    def search(self, criteria, *, limit=50, offset=0):
        raise AssertionError("facade history tests never search")


class FakeGraphService:
    def __init__(self):
        self.timeline_result = GraphTimeline("case-1", EARLY, ())
        self.timeline_calls = []
        self.add_case_calls = []

    async def timeline(self, case_id, as_of):
        self.timeline_calls.append((case_id, as_of))
        return self.timeline_result

    async def add_case(self, case, **bundle):
        self.add_case_calls.append((case, bundle))
        raise AssertionError("facade history tests never write")


class FakeEventBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class FakeAnalyzer:
    def __init__(self):
        self.state_result = None
        self.snapshot_result = None
        self.compare_result = None
        self.calls = []

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append(("analyze", case_id, as_of, kinds))
        raise AssertionError("facade history tests never analyze")

    async def state(self, case_id, cutoff_at):
        self.calls.append(("state", case_id, cutoff_at))
        return self.state_result

    async def snapshot(self, case_id, as_of, *, stage=None, kinds=None):
        self.calls.append(("snapshot", case_id, as_of, stage, kinds))
        return self.snapshot_result

    async def compare(self, case_id, earlier, later, *, kinds=None):
        self.calls.append(("compare", case_id, earlier, later, kinds))
        return self.compare_result


def facade(analyzer: FakeAnalyzer | None = None) -> PrismAPI:
    return PrismAPI(
        FakeIngestionService(),
        FakeEvidenceStore(),
        FakeGraphService(),
        FakeEventBus(),
        analyzer_service=analyzer,
    )


def test_facade_query_historical_snapshot_delegates_stage_and_kinds():
    analyzer = FakeAnalyzer()
    snapshot = HistoricalCaseState(
        case_id=CASE,
        cutoff_at=EARLY,
        case_type="policy",
        status="active",
        nodes=(),
        facts=(),
        interpretations=(),
        evidence_gaps=(),
    )
    analyzer.snapshot_result = snapshot
    api = facade(analyzer)

    result = run(
        api.query_historical_snapshot(
            CASE, EARLY, stage="publication", kinds=("claim",)
        )
    )

    assert result is snapshot
    assert analyzer.calls == [
        ("snapshot", CASE, EARLY, "publication", ("claim",))
    ]


def test_facade_compare_case_history_delegates_to_analyzer_compare():
    analyzer = FakeAnalyzer()
    comparison = EvolutionComparison(CASE, EARLY, LATER, (), (), ())
    analyzer.compare_result = comparison
    api = facade(analyzer)

    result = run(api.compare_case_history(CASE, EARLY, LATER))

    assert result is comparison
    assert analyzer.calls == [("compare", CASE, EARLY, LATER, None)]


def test_facade_history_entry_points_stay_backward_compatible():
    analyzer = FakeAnalyzer()
    analyzer.state_result = HistoricalCaseState(
        case_id=CASE,
        cutoff_at=EARLY,
        case_type="policy",
        status="active",
        nodes=(),
        facts=(),
        interpretations=(),
        evidence_gaps=(),
    )
    api = facade(analyzer)

    state = run(api.query_case_state(CASE, EARLY))
    assert state is analyzer.state_result
    assert analyzer.calls == [("state", CASE, EARLY)]


def test_facade_history_methods_require_the_analyzer():
    api = facade(analyzer=None)

    with pytest.raises(ValueError, match="analyzer_service is required"):
        run(api.query_historical_snapshot(CASE, EARLY))
    with pytest.raises(ValueError, match="analyzer_service is required"):
        run(api.compare_case_history(CASE, EARLY, LATER))


def test_facade_history_methods_reject_analyzers_without_the_operation():
    class AnalyzeOnly:
        async def analyze(self, case_id, as_of=None, *, kinds=None):
            raise AssertionError("must not be called")

    api = facade(AnalyzeOnly())
    with pytest.raises(TypeError, match="must provide snapshot"):
        run(api.query_historical_snapshot(CASE, EARLY))


# ------------------------------------------------------------------------ CLI


class CliAPI:
    def __init__(self):
        self.calls = []
        self.snapshot_result = None
        self.compare_result = None

    async def query_historical_snapshot(self, case_id, as_of, *, stage=None, kinds=None):
        self.calls.append(
            ("query_historical_snapshot", (case_id, as_of), {"stage": stage, "kinds": kinds})
        )
        return self.snapshot_result

    async def compare_case_history(self, case_id, earlier, later, *, kinds=None):
        self.calls.append(
            ("compare_case_history", (case_id, earlier, later), {"kinds": kinds})
        )
        return self.compare_result


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


def test_cli_parser_exposes_snapshot_and_compare_commands():
    parser = build_parser()

    snapshot = parser.parse_args(
        [
            "snapshot",
            CASE,
            "--as-of",
            "2026-02-02T00:00:00+00:00",
            "--stage",
            "publication",
            "--kind",
            "evolution_node",
            "--kind",
            "claim",
        ]
    )
    assert snapshot.handler is handle_snapshot
    assert snapshot.stage == "publication"
    assert snapshot.kind == ["evolution_node", "claim"]

    comparison = parser.parse_args(
        [
            "compare",
            CASE,
            "--earlier",
            "2026-02-02T00:00:00+00:00",
            "--later",
            "2026-03-12T00:00:00+00:00",
        ]
    )
    assert comparison.handler is handle_compare
    assert comparison.earlier == EARLY
    assert comparison.later == LATER


def test_cli_snapshot_delegates_as_of_stage_and_kinds():
    api = CliAPI()
    status, stdout, stderr = run_cli(
        [
            "snapshot",
            CASE,
            "--as-of",
            "2026-02-02T00:00:00+00:00",
            "--stage",
            "publication",
            "--kind",
            "evolution_node",
            "--kind",
            "claim",
        ],
        api,
    )

    assert status == 0
    assert stderr == ""
    assert api.calls == [
        (
            "query_historical_snapshot",
            (CASE, EARLY),
            {"stage": "publication", "kinds": ("evolution_node", "claim")},
        )
    ]


def test_cli_snapshot_without_stage_passes_explicit_none():
    api = CliAPI()
    status, _, stderr = run_cli(
        ["snapshot", CASE, "--as-of", "2026-02-02T00:00:00+00:00"], api
    )

    assert status == 0 and stderr == ""
    assert api.calls == [
        ("query_historical_snapshot", (CASE, EARLY), {"stage": None, "kinds": None})
    ]


def test_cli_snapshot_rejects_naive_or_absent_as_of_without_calling_api():
    api = CliAPI()

    status, _, stderr = run_cli(
        ["snapshot", CASE, "--as-of", "2026-02-02T00:00:00"], api
    )
    assert status == 2
    assert "timezone-aware" in stderr
    assert api.calls == []

    status, _, stderr = run_cli(["snapshot", CASE], api)
    assert status == 2
    assert "as-of" in stderr
    assert api.calls == []


def test_cli_snapshot_rejects_unknown_stage_or_kind_without_calling_api():
    api = CliAPI()

    status, _, stderr = run_cli(
        ["snapshot", CASE, "--as-of", "2026-02-02T00:00:00+00:00", "--stage", "rumor"],
        api,
    )
    assert status == 2
    assert "rumor" in stderr
    assert api.calls == []

    status, _, stderr = run_cli(
        [
            "snapshot",
            CASE,
            "--as-of",
            "2026-02-02T00:00:00+00:00",
            "--kind",
            "rumor",
        ],
        api,
    )
    assert status == 2
    assert "rumor" in stderr
    assert api.calls == []


def test_cli_snapshot_prints_the_formal_state_json():
    stage = TimelineStage(
        episode_key="node-publish",
        kind="evolution_node",
        layer="fact",
        summary="The rate policy was published.",
        valid_at=NODE_PUBLISH_AT,
        invalid_at=None,
        reference_time=NODE_PUBLISH_AT,
        source_ids=(MATERIAL_A,),
        node_type="publication",
        evidence=(loc(MATERIAL_A, "The rate policy was published."),),
    )
    api = CliAPI()
    api.snapshot_result = HistoricalCaseState(
        case_id=CASE,
        cutoff_at=LATER,
        case_type="policy",
        status="active",
        nodes=(stage,),
        facts=(),
        interpretations=(),
        evidence_gaps=(),
    )

    status, stdout, stderr = run_cli(
        ["snapshot", CASE, "--as-of", "2026-03-12T00:00:00+00:00"], api
    )

    assert status == 0 and stderr == ""
    payload = json.loads(stdout)
    assert payload["case_id"] == CASE
    assert payload["cutoff_at"] == "2026-03-12T00:00:00+00:00"
    assert payload["nodes"][0]["node_type"] == "publication"
    assert payload["nodes"][0]["evidence"][0]["source_id"] == MATERIAL_A
    assert payload["nodes"][0]["evidence"][0]["quote"] == (
        "The rate policy was published."
    )


def test_cli_compare_delegates_earlier_later_and_optional_kinds():
    api = CliAPI()
    api.compare_result = {
        "case_id": CASE,
        "earlier": EARLY,
        "later": LATER,
        "added": [],
        "removed": [],
        "unchanged": [],
    }

    status, stdout, stderr = run_cli(
        [
            "compare",
            CASE,
            "--earlier",
            "2026-02-02T00:00:00+00:00",
            "--later",
            "2026-03-12T00:00:00+00:00",
            "--kind",
            "claim",
            "--kind",
            "temporal_fact",
        ],
        api,
    )

    assert status == 0 and stderr == ""
    assert api.calls == [
        (
            "compare_case_history",
            (CASE, EARLY, LATER),
            {"kinds": ("claim", "temporal_fact")},
        )
    ]
    assert json.loads(stdout)["case_id"] == CASE


def test_cli_compare_rejects_naive_timestamps_without_calling_api():
    api = CliAPI()

    status, _, stderr = run_cli(
        [
            "compare",
            CASE,
            "--earlier",
            "2026-02-02T00:00:00",
            "--later",
            "2026-03-12T00:00:00+00:00",
        ],
        api,
    )
    assert status == 2
    assert "timezone-aware" in stderr
    assert api.calls == []


def test_cli_compare_surfaces_reversed_instants_as_a_facade_error():
    class StrictAPI(CliAPI):
        async def compare_case_history(self, case_id, earlier, later, *, kinds=None):
            self.calls.append(
                ("compare_case_history", (case_id, earlier, later), {"kinds": kinds})
            )
            raise ValueError("later must not be earlier than earlier")

    api = StrictAPI()
    status, stdout, stderr = run_cli(
        [
            "compare",
            CASE,
            "--earlier",
            "2026-03-12T00:00:00+00:00",
            "--later",
            "2026-02-02T00:00:00+00:00",
        ],
        api,
    )

    assert status == 1
    assert stdout == ""
    assert "later must not be earlier" in stderr
    assert api.calls == [
        (
            "compare_case_history",
            (CASE, LATER, EARLY),
            {"kinds": None},
        )
    ]
