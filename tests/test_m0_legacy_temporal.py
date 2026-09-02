"""Offline temporal-semantics regressions for legacy merged-case imports.

These tests reproduce, with synthetic fixtures, the temporal problems found
when real-case acceptance ran the M0 loop over a legacy merged bundle:

* legacy nodes carry no ``observed_at`` and legacy claims carry none either,
  so both leaked into states that predate the materials reporting them;
* legacy facts recorded an ``observed_at`` earlier than the publication time
  of their bound materials (inconsistent fact/material availability);
* multi-source entries were treated as observed at their earliest bound
  material instead of the latest necessary observation time.

No real acceptance material is copied into this repository; everything below
is constructed inline.  The verification chain is: legacy loader -> graph
episodes -> analyzer/API state -> report -> CLI, all offline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from prism.analyzer import AnalyzerService
from prism.api import PrismAPI
from prism.cases import LegacyCaseLoader
from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.graph import GraphService
from prism.report import ReportService

UTC = timezone.utc
CASE_ID = "late-source-case"
EARLY = datetime(2026, 8, 20, tzinfo=UTC)  # the middle cutoff with late backfill
LATER = datetime(2026, 8, 30, tzinfo=UTC)  # after every material is available


def run(coro):
    return asyncio.run(coro)


def material_payload(source_id, day):
    return {
        "id": source_id,
        "title": f"Material {source_id}",
        "source": "example.gov",
        "published_at": datetime(2026, 8, day, tzinfo=UTC).isoformat(),
        "fetched_at": datetime(2026, 8, day, 6, 0, tzinfo=UTC).isoformat(),
        "type": "policy",
        "content": f"Recorded evidence body of {source_id}.",
        "original_format": "md",
        "ocr": False,
        "case_tags": [CASE_ID],
    }


def legacy_bundle_payload():
    """A v1-style merged bundle: nodes/claims without observed_at or evidence,
    facts whose recorded observed_at can predate their bound materials."""
    return {
        "case": {
            "case_id": CASE_ID,
            "case_type": "policy",
            "canonical_name": "Late-source policy evolution",
            "start_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
            "status": "implemented",
            "node_ids": ["node-a", "node-b", "node-late", "node-unattributed"],
        },
        "materials": [
            material_payload("mat-a", 5),
            material_payload("mat-b", 10),
            material_payload("mat-c", 25),
        ],
        "nodes": [
            {
                # Observed by a same-day material: visible before the cutoff.
                "id": "node-a",
                "case_id": CASE_ID,
                "node_type": "publication",
                "happened_at": datetime(2026, 8, 5, 9, 0, tzinfo=UTC).isoformat(),
                "summary": "Policy published early.",
                "source_ids": ["mat-a"],
            },
            {
                "id": "node-b",
                "case_id": CASE_ID,
                "node_type": "implementation",
                "happened_at": datetime(2026, 8, 10, 9, 0, tzinfo=UTC).isoformat(),
                "summary": "Policy implemented.",
                "source_ids": ["mat-b"],
            },
            {
                # Happened before the cutoff but reported ONLY by a material
                # published after it: must not appear in the cutoff state.
                "id": "node-late",
                "case_id": CASE_ID,
                "node_type": "revision",
                "happened_at": datetime(2026, 8, 12, 9, 0, tzinfo=UTC).isoformat(),
                "summary": "Early revision reported only by a late material.",
                "source_ids": ["mat-c"],
            },
            {
                # No sources at all: observation time is undetermined; the
                # loader must not fabricate one.
                "id": "node-unattributed",
                "case_id": CASE_ID,
                "node_type": "response",
                "happened_at": datetime(2026, 8, 7, tzinfo=UTC).isoformat(),
                "summary": "Unattributed market response.",
                "source_ids": [],
            },
        ],
        "temporal_facts": [
            {
                "subject": "Policy",
                "predicate": "had_state",
                "object": "early-active",
                "valid_at": datetime(2026, 8, 3, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 8, 5, tzinfo=UTC).isoformat(),
                "source_ids": ["mat-a"],
                "confidence": 0.9,
                "provenance_type": "explicit",
            },
            {
                # Recorded observed_at (08-04) predates the only bound
                # material (published 08-25): inconsistent availability that
                # must be bounded to the material, not trusted.
                "subject": "Policy",
                "predicate": "had_state",
                "object": "inconsistent-early",
                "valid_at": datetime(2026, 8, 4, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 8, 4, tzinfo=UTC).isoformat(),
                "source_ids": ["mat-c"],
                "confidence": 0.8,
                "provenance_type": "reliable_transcription",
            },
            {
                # Multi-source, all bound materials available before cutoff.
                "subject": "Policy",
                "predicate": "had_state",
                "object": "multi-ok",
                "valid_at": datetime(2026, 8, 6, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 8, 10, tzinfo=UTC).isoformat(),
                "source_ids": ["mat-a", "mat-b"],
                "confidence": 0.85,
                "provenance_type": "explicit",
            },
            {
                # Multi-source with one late material: the latest necessary
                # observation time (mat-c, 08-25) must win, never the earliest
                # (mat-a, 08-05) nor the recorded 08-10.
                "subject": "Policy",
                "predicate": "had_state",
                "object": "multi-late",
                "valid_at": datetime(2026, 8, 7, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 8, 10, tzinfo=UTC).isoformat(),
                "source_ids": ["mat-a", "mat-c"],
                "confidence": 0.7,
                "provenance_type": "model_inference",
            },
            {
                # Valid only after the cutoff: the valid-interval filter must
                # exclude it regardless of observation time.
                "subject": "Policy",
                "predicate": "had_state",
                "object": "future-valid",
                "valid_at": datetime(2026, 8, 26, tzinfo=UTC).isoformat(),
                "observed_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
                "source_ids": ["mat-b"],
                "confidence": 0.6,
                "provenance_type": "model_inference",
            },
            {
                # No observed_at and no bound material: cannot be represented
                # without fabricating a time; must be skipped with an issue.
                "subject": "Policy",
                "predicate": "had_state",
                "object": "no-time",
                "valid_at": datetime(2026, 8, 8, tzinfo=UTC).isoformat(),
                "source_ids": [],
                "confidence": 0.5,
                "provenance_type": "explicit",
            },
        ],
        "claims": [
            {
                "claim_id": "claim-early",
                "actor": "Analyst",
                "proposition": "Early support for the mechanism.",
                "stance": "support",
                "stated_at": datetime(2026, 8, 6, tzinfo=UTC).isoformat(),
                "based_on": ["mat-b"],
            },
            {
                # Stated before the cutoff but recorded only by the late
                # material: a prediction that must not backfill 08-20.
                "claim_id": "claim-late-prediction",
                "actor": "Analyst",
                "proposition": "A prediction recorded only by the late material.",
                "stance": "conditional",
                "stated_at": datetime(2026, 8, 8, tzinfo=UTC).isoformat(),
                "based_on": ["mat-c"],
            },
            {
                "claim_id": "claim-unbound",
                "actor": "Analyst",
                "proposition": "An unattributed remark.",
                "stance": "uncertain",
                "stated_at": datetime(2026, 8, 9, tzinfo=UTC).isoformat(),
                "based_on": [],
            },
        ],
        "warnings": ["Legacy import fixture: temporal fields were not recorded."],
    }


class FakeBackend:
    def __init__(self):
        self.episodes = {}

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        return tuple(self.episodes.values())


class FakeStore:
    """Evidence-store stand-in: state queries never touch the corpus."""

    def index_file(self, path):
        raise AssertionError("indexing is not part of this test")

    def search(self, criteria, *, limit, offset):
        raise AssertionError("search is not part of this test")


class FakeBus:
    async def publish(self, event):
        raise AssertionError("events are not part of this test")


class UnusedIngestion:
    def ingest(self, path, metadata=None):
        raise AssertionError("ingestion is not part of this test")


def load_bundle():
    result = LegacyCaseLoader().load(legacy_bundle_payload())
    return result


def graph_for(bundle):
    return GraphService(FakeBackend())


def api_for(graph):
    analyzer = AnalyzerService(graph)
    return PrismAPI(
        UnusedIngestion(),
        FakeStore(),
        graph,
        FakeBus(),
        analyzer_service=analyzer,
        report_service=ReportService(),
    )


def seeded_graph(bundle):
    graph = graph_for(bundle)
    run(
        graph.add_case(
            bundle.case,
            nodes=bundle.nodes,
            facts=bundle.temporal_facts,
            claims=bundle.claims,
            materials=bundle.materials,
        )
    )
    return graph


# ---------------------------------------------------------------- loader


def test_legacy_loader_derives_observation_times_without_fabricating():
    result = load_bundle()
    bundle = result.bundle

    assert bundle.case.case_id == CASE_ID
    assert [material.id for material in bundle.materials] == [
        "mat-a",
        "mat-b",
        "mat-c",
    ]
    assert bundle.warnings

    # Loader-derived observation boundaries: late sources pin their records
    # to the late material publication date.
    by_id = {node.id: node for node in bundle.nodes}
    assert by_id["node-a"].observed_at == datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    assert by_id["node-late"].observed_at == datetime(2026, 8, 25, tzinfo=UTC)
    # No material and no recorded time: never fabricated.
    assert by_id["node-unattributed"].observed_at is None

    facts_by_key = {
        f"{fact.subject}:{fact.predicate}:{fact.object}": fact
        for fact in bundle.temporal_facts
    }
    assert facts_by_key["Policy:had_state:inconsistent-early"].observed_at == datetime(
        2026, 8, 25, tzinfo=UTC
    )
    # Multi-source: the latest necessary observation time wins, not the
    # earliest bound material (mat-a, 08-05) and not the recorded 08-10.
    assert facts_by_key["Policy:had_state:multi-late"].observed_at == datetime(
        2026, 8, 25, tzinfo=UTC
    )
    # A fact with neither observed_at nor bound material cannot be
    # represented without fabrication: it is skipped, never guessed.
    assert "Policy:had_state:no-time" not in facts_by_key

    claims_by_id = {claim.claim_id: claim for claim in bundle.claims}
    assert claims_by_id["claim-early"].observed_at == datetime(
        2026, 8, 10, tzinfo=UTC
    )
    assert claims_by_id["claim-late-prediction"].observed_at == datetime(
        2026, 8, 25, tzinfo=UTC
    )
    assert claims_by_id["claim-unbound"].observed_at is None


def test_legacy_loader_reports_every_conservative_decision_as_an_issue():
    result = load_bundle()
    codes = [issue.code for issue in result.issues]
    assert codes.count("observed_at_derived") == 5  # node-a/b/late + 2 claims
    assert codes.count("observed_at_bounded_later") == 2
    assert codes.count("observed_at_undetermined") == 3

    bounded = [issue for issue in result.issues if issue.code == "observed_at_bounded_later"]
    assert any("inconsistent-early" in issue.record_id for issue in bounded)
    assert any(
        issue.record_id == "Policy:had_state:multi-late" for issue in bounded
    )
    undetermined = [
        issue for issue in result.issues if issue.code == "observed_at_undetermined"
    ]
    assert any(issue.record_id == "node-unattributed" for issue in undetermined)
    assert any(issue.record_id == "claim-unbound" for issue in undetermined)
    skipped = [issue for issue in undetermined if issue.record_kind == "temporal_fact"]
    assert len(skipped) == 1 and "no-time" in skipped[0].record_id
    assert "fabricat" in skipped[0].detail

    # Issues are deterministic and reusable across input forms.
    as_text = LegacyCaseLoader().load(json.dumps(legacy_bundle_payload()))
    assert as_text.issues == result.issues
    assert as_text.bundle == result.bundle


# ------------------------------------------------------------ cutoff state


def expected_early_gap_types():
    # Analyzer gaps sort by (gap_type, episode_key); episode keys are UUIDs so
    # only the per-type sequence is asserted: five located-but-unlocated
    # entries (missing_evidence_location) then two unattributed entries.
    return ["missing_evidence_location"] * 5 + ["unattributed_entry"] * 2


def test_state_at_middle_cutoff_excludes_every_late_source_backfill():
    result = load_bundle()
    graph = seeded_graph(result.bundle)
    state = run(api_for(graph).query_case_state(CASE_ID, EARLY))

    assert [stage.summary for stage in state.nodes] == [
        "Policy published early.",
        "Unattributed market response.",
        "Policy implemented.",
    ]
    assert [stage.summary for stage in state.facts] == [
        "Policy had_state early-active",
        "Policy had_state multi-ok",
    ]
    assert [stage.summary for stage in state.interpretations] == [
        "Early support for the mechanism.",
        "An unattributed remark.",
    ]
    # The late-material records are nowhere in the middle-cutoff state:
    # node-late, the inconsistent-early and multi-late facts, the future-valid
    # fact, and the late prediction claim.
    assert all(stage.reference_time <= EARLY for stage in (*state.nodes, *state.facts, *state.interpretations))
    assert [gap.gap_type for gap in state.evidence_gaps] == expected_early_gap_types()


def test_later_state_gains_every_record_once_its_materials_are_available():
    result = load_bundle()
    graph = seeded_graph(result.bundle)
    state = run(api_for(graph).query_case_state(CASE_ID, LATER))

    assert {stage.summary for stage in state.nodes} == {
        "Policy published early.",
        "Policy implemented.",
        "Early revision reported only by a late material.",
        "Unattributed market response.",
    }
    assert {stage.summary for stage in state.facts} == {
        "Policy had_state early-active",
        "Policy had_state inconsistent-early",
        "Policy had_state multi-ok",
        "Policy had_state multi-late",
        "Policy had_state future-valid",
    }
    assert {stage.summary for stage in state.interpretations} == {
        "Early support for the mechanism.",
        "A prediction recorded only by the late material.",
        "An unattributed remark.",
    }


def test_graph_flooring_alone_blocks_inconsistent_fact_observation():
    """Fresh-path guard: even without the loader, submitting a fact whose
    recorded observed_at predates its bound material keeps the fact out of
    earlier states — the graph never trusts an observation before the
    material existed."""
    material = Material(
        id="mat-late-only",
        title="Late material",
        source="example.gov",
        published_at=datetime(2026, 8, 25, tzinfo=UTC),
        fetched_at=datetime(2026, 8, 26, tzinfo=UTC),
        type="policy",
        content="Recorded evidence body.",
    )
    case = EvolutionCase(
        "floor-case", "policy", "Floor case", datetime(2026, 8, 1, tzinfo=UTC), "active"
    )
    fact = TemporalFact(
        "Policy",
        "had_state",
        "early-claim",
        datetime(2026, 8, 4, tzinfo=UTC),  # valid early
        None,
        datetime(2026, 8, 4, tzinfo=UTC),  # recorded observed too early
        ("mat-late-only",),
        0.9,
        "explicit",
    )
    claim = Claim(
        "claim-floor",
        "Analyst",
        "A claim only the late material records.",
        "support",
        datetime(2026, 8, 6, tzinfo=UTC),
        ("mat-late-only",),
    )
    graph = GraphService(FakeBackend())
    run(graph.add_case(case, facts=(fact,), claims=(claim,), materials=(material,)))

    early_state = run(api_for(graph).query_case_state("floor-case", EARLY))
    assert early_state.facts == ()
    assert early_state.interpretations == ()

    late_state = run(api_for(graph).query_case_state("floor-case", LATER))
    assert [stage.summary for stage in late_state.facts] == [
        "Policy had_state early-claim"
    ]
    assert [stage.summary for stage in late_state.interpretations] == [
        "A claim only the late material records."
    ]


def test_claim_episode_serializes_observed_at_and_keeps_stated_at_validity():
    material = Material(
        id="mat-c",
        title="Late material",
        source="example.gov",
        published_at=datetime(2026, 8, 25, tzinfo=UTC),
        fetched_at=datetime(2026, 8, 26, tzinfo=UTC),
        type="policy",
        content="Recorded evidence body.",
    )
    case = EvolutionCase(
        "episode-case",
        "policy",
        "Episode case",
        datetime(2026, 8, 1, tzinfo=UTC),
        "active",
    )
    claim = Claim(
        "claim-episode",
        "Analyst",
        "An episode serialization claim.",
        "conditional",
        datetime(2026, 8, 8, tzinfo=UTC),  # stated before the material
        ("mat-c",),
    )
    graph = GraphService(FakeBackend())
    result = run(graph.add_case(case, claims=(claim,), materials=(material,)))

    episodes = {episode.kind: episode for episode in result.episodes}
    payload = json.loads(episodes["claim"].episode_body)
    assert payload["observed_at"] == datetime(2026, 8, 25, tzinfo=UTC).isoformat()
    assert payload["valid_at"] == datetime(2026, 8, 8, tzinfo=UTC).isoformat()
    assert episodes["claim"].reference_time == datetime(2026, 8, 25, tzinfo=UTC)
    assert episodes["claim"].valid_at == datetime(2026, 8, 8, tzinfo=UTC)


# ------------------------------------------------- report and API/CLI


def test_report_does_not_inflate_nodes_and_states_real_counts():
    result = load_bundle()
    graph = seeded_graph(result.bundle)
    report = run(api_for(graph).report_case(CASE_ID, EARLY, use_llm=False))

    visible_nodes = [stage for stage in report.stages if stage.kind == "evolution_node"]
    material_rows = [
        stage for stage in report.stages if stage.kind == "material_provenance"
    ]
    # Material provenance rows exist but are never counted as evolution nodes;
    # the late material (mat-c, published 08-25) is not even a visible row at
    # the 08-20 cutoff.
    assert len(material_rows) == 2
    assert {row.source_ids[0] for row in material_rows} == {"mat-a", "mat-b"}
    assert len(visible_nodes) == 3
    assert sorted(stage.node_type for stage in visible_nodes) == [
        "implementation",
        "publication",
        "response",
    ]
    # The report never mentions the excluded late-source records.
    assert "reported only by a late material" not in report.summary.summary
    assert "recorded 3 evolution node(s)" in report.summary.summary
    assert (
        "(node types: 1 implementation, 1 publication, 1 response)"
        in report.summary.summary
    )
    assert "2 fact(s) and 2 claim(s)" in report.summary.summary
    assert "7 evidence gap(s)" in report.summary.summary
    # Every gap is visible in the markdown, and citation-free rows exist.
    for gap_type in expected_early_gap_types():
        assert f"**{gap_type}**" in report.markdown


def test_cli_state_command_returns_only_pre_cutoff_entries():
    result = load_bundle()
    graph = seeded_graph(result.bundle)
    api = api_for(graph)

    from io import StringIO

    from prism.cli import main

    stdout = StringIO()
    stderr = StringIO()
    status = run(
        main(
            ["state", CASE_ID, "--cutoff-at", EARLY.isoformat()],
            api=api,
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert status == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    node_summaries = [stage["summary"] for stage in payload["nodes"]]
    assert node_summaries == [
        "Policy published early.",
        "Unattributed market response.",
        "Policy implemented.",
    ]
    assert "reported only by a late material" not in stdout.getvalue()
    assert "A prediction recorded only by the late material." not in stdout.getvalue()
    gaps = [gap["gap_type"] for gap in payload["evidence_gaps"]]
    assert gaps == expected_early_gap_types()
