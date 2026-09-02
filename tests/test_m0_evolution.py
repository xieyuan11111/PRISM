"""Offline acceptance tests for the M0 single-case longitudinal loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from prism.analyzer import (
    GAP_MISSING_PRIMARY_SOURCE,
    GAP_UNVERIFIED_PREDICTION,
    AnalyzerService,
    EvidenceGap,
)
from prism.api import PrismAPI
from prism.config import PathConfig
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    EvolutionNode,
    Material,
    TemporalFact,
)
from prism.extraction import ExtractionService
from prism.graph import GraphEpisode, GraphService
from prism.report import ReportService
from prism.store import EvidenceStore


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 25, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


class FakeBackend:
    def __init__(self):
        self.episodes: dict[str, GraphEpisode] = {}

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        return tuple(self.episodes.values())


class UnusedIngestion:
    def ingest(self, path, metadata=None):
        raise AssertionError("ingestion is not part of this test")


class FakeBus:
    async def publish(self, event):
        raise AssertionError("events are not part of this test")


def api_for(store, graph):
    analyzer = AnalyzerService(graph)
    return PrismAPI(
        UnusedIngestion(),
        store,
        graph,
        FakeBus(),
        analyzer_service=analyzer,
        report_service=ReportService(),
    )


class StaticRouter:
    """Router replaying one canned completion, for extraction-path tests."""

    def __init__(self, text):
        self.text = text

    async def complete(self, role, prompt):
        return type("_Completion", (), {"text": self.text})()


def extract_case_payload(payload, material):
    router = StaticRouter(json.dumps(payload))
    return run(ExtractionService(router).extract(material))


def write_material(path, source_id, text, published_at):
    path.write_text(
        "\n".join(
            [
                "---",
                f'source_id: "{source_id}"',
                f'title: "Evidence {source_id}"',
                'source: "example.gov"',
                f"published_at: {published_at.isoformat()}",
                f"fetched_at: {published_at.isoformat()}",
                'type: "policy"',
                'case_tags: ["m0-case"]',
                'original_format: "md"',
                "ocr: false",
                'extracted_via: "direct"',
                "---",
                "",
                text,
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_case(tmp_path):
    paths = PathConfig(
        data_dir=tmp_path / "data",
        corpus_dir=tmp_path / "corpus",
        raw_dir=tmp_path / "raw",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
    )
    paths.corpus_dir.mkdir(parents=True)
    store = EvidenceStore(paths)
    locators = []
    for index in range(1, 8):
        source_id = f"source-{index}"
        text = f"Recorded evidence for substantive change {index}."
        path = paths.corpus_dir / f"source-{index}.md"
        write_material(path, source_id, text, START + timedelta(days=index))
        store.index_file(path)
        locators.append(store.locate(source_id, quote=f"substantive change {index}"))

    nodes = tuple(
        EvolutionNode(
            id=f"node-{index}",
            case_id="m0-case",
            node_type=(
                "proposal",
                "implementation",
                "response",
                "revision",
                "interpretation",
            )[index - 1],
            happened_at=START + timedelta(days=index),
            summary=f"Substantive evolution node {index}.",
            source_ids=(f"source-{index}",),
            valid_at=START + timedelta(days=index),
            observed_at=START + timedelta(days=index),
            evidence=(locators[index - 1],),
        )
        for index in range(1, 6)
    )
    scheduled_future = EvolutionNode(
        "node-future-effective",
        "m0-case",
        "implementation",
        CUTOFF + timedelta(days=10),
        "A known announcement has not yet become effective.",
        ("source-6",),
        (),
        CUTOFF + timedelta(days=10),
        CUTOFF - timedelta(days=1),
        (locators[5],),
    )
    retrospectively_observed = TemporalFact(
        "Policy",
        "had_state",
        "retrospective-only",
        START,
        None,
        CUTOFF + timedelta(days=1),
        ("source-7",),
        0.9,
        "explicit",
        (locators[6],),
    )
    current_fact = TemporalFact(
        "Policy",
        "state",
        "active",
        START + timedelta(days=3),
        None,
        START + timedelta(days=3),
        ("source-3",),
        1.0,
        "source_explicit",
        (locators[2],),
    )
    interpretation = Claim(
        "claim-1",
        "Analyst",
        "The mechanism may stabilize the market.",
        "conditional",
        START + timedelta(days=5),
        ("source-5",),
        None,
        (locators[4],),
    )
    case = EvolutionCase(
        "m0-case",
        "policy",
        "M0 policy evolution",
        START,
        "active",
        tuple(node.id for node in (*nodes, scheduled_future)),
    )
    return (
        store,
        case,
        nodes,
        scheduled_future,
        current_fact,
        retrospectively_observed,
        interpretation,
    )


def test_cutoff_state_excludes_future_validity_and_future_observations(tmp_path):
    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled),
            facts=(current, retrospective),
            claims=(claim,),
        )
    )

    state = run(api_for(store, graph).query_case_state(case.case_id, CUTOFF))

    assert state.status == "active"
    assert len(state.nodes) == 5
    assert {stage.summary for stage in state.nodes} == {
        f"Substantive evolution node {index}." for index in range(1, 6)
    }
    assert [fact.summary for fact in state.facts] == ["Policy state active"]
    assert len(state.interpretations) == 1
    assert state.evidence_gaps == ()
    assert all(stage.reference_time <= CUTOFF for stage in (*state.nodes, *state.facts))
    store.close()


def test_five_nodes_have_portable_source_locations_and_report_citations(tmp_path):
    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled),
            facts=(current, retrospective),
            claims=(claim,),
        )
    )
    report = run(api_for(store, graph).report_case(case.case_id, CUTOFF, use_llm=False))

    visible_nodes = [stage for stage in report.stages if stage.kind == "evolution_node"]
    assert len(visible_nodes) == 5
    assert all(stage.source_ids and stage.evidence for stage in visible_nodes)
    assert all(
        not item.corpus_path.startswith(("/", "\\"))
        for stage in visible_nodes
        for item in stage.evidence
    )
    for stage in visible_nodes:
        for locator in stage.evidence:
            assert locator.corpus_path in report.markdown
            assert locator.quote in report.markdown
            assert locator.source_id in {citation.source_id for citation in report.citations}
    assert "Source excerpt:" in report.markdown
    assert "`fact`" in report.markdown and "`interpretation`" in report.markdown
    assert "No recorded change reasons; no causal chain is asserted." in report.markdown
    store.close()


def test_empty_case_is_an_explicit_evidence_gap():
    state = run(
        AnalyzerService(GraphService(FakeBackend())).state(
            "empty-case", datetime(2026, 9, 1, tzinfo=UTC)
        )
    )

    assert state.nodes == ()
    assert state.facts == ()
    assert [gap.gap_type for gap in state.evidence_gaps] == ["empty_timeline"]


def test_source_id_without_original_location_is_an_explicit_gap():
    graph = GraphService(FakeBackend())
    case = EvolutionCase("gap-case", "policy", "Gap case", START, "open")
    node = EvolutionNode(
        "unlocated-node",
        case.case_id,
        "proposal",
        START,
        "A source id alone cannot locate original text.",
        ("source-only",),
    )
    run(graph.add_case(case, nodes=(node,)))

    state = run(AnalyzerService(graph).state(case.case_id, CUTOFF))

    assert [gap.gap_type for gap in state.evidence_gaps] == [
        "missing_evidence_location"
    ]


def test_future_case_status_is_not_backfilled_into_historical_state():
    graph = GraphService(FakeBackend())
    case = EvolutionCase(
        "status-case",
        "policy",
        "Status case",
        START,
        "implemented",
        status_at=CUTOFF + timedelta(days=1),
        status_observed_at=CUTOFF + timedelta(days=1),
    )
    run(graph.add_case(case))

    state = run(AnalyzerService(graph).state(case.case_id, CUTOFF))

    assert state.case_type == "policy"
    assert state.status is None


def test_new_node_times_and_evidence_contracts_remain_strict_and_immutable():
    locator = EvidenceLocator("source", "corpus/source.md", paragraph=2, quote="text")
    node = EvolutionNode(
        "node",
        "case",
        "proposal",
        START,
        "Proposal",
        ("source",),
        evidence=[locator],
    )

    assert node.evidence == (locator,)
    with pytest.raises(ValueError, match="timezone-aware"):
        EvolutionNode(
            "node",
            "case",
            "proposal",
            START,
            "Proposal",
            ("source",),
            valid_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="project-relative"):
        EvidenceLocator("source", "C:/private/source.md", paragraph=1)


def test_cutoff_state_excludes_retrospectively_observed_nodes(tmp_path):
    """A node that happened before the cutoff but was only reported by a
    material published after the cutoff must not appear in the earlier state."""
    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    retrospective_node = EvolutionNode(
        "node-retrospective",
        case.case_id,
        "response",
        START + timedelta(days=2),
        "An early market response reported only by a later material.",
        ("source-7",),
        (),
        START + timedelta(days=2),
        CUTOFF + timedelta(days=2),
    )
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled, retrospective_node),
            facts=(current, retrospective),
            claims=(claim,),
        )
    )

    state = run(api_for(store, graph).query_case_state(case.case_id, CUTOFF))

    assert {stage.summary for stage in state.nodes} == {
        f"Substantive evolution node {index}." for index in range(1, 6)
    }
    store.close()


def test_cutoff_state_excludes_claims_stated_after_cutoff(tmp_path):
    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    future_claim = Claim(
        "claim-future",
        "Analyst",
        "A prediction made after the cutoff.",
        "conditional",
        CUTOFF + timedelta(days=3),
        ("source-7",),
    )
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled),
            facts=(current, retrospective),
            claims=(claim, future_claim),
        )
    )

    state = run(api_for(store, graph).query_case_state(case.case_id, CUTOFF))

    assert [stage.summary for stage in state.interpretations] == [
        "The mechanism may stabilize the market."
    ]
    store.close()


def test_cutoff_state_does_not_backfill_an_unobserved_prediction_gap(tmp_path):
    """A fully located interpretation is reported, not relabeled as a gap."""
    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled),
            facts=(current, retrospective),
            claims=(claim,),
        )
    )

    state = run(api_for(store, graph).query_case_state(case.case_id, CUTOFF))

    assert [stage.stance for stage in state.interpretations] == ["conditional"]
    assert state.evidence_gaps == ()
    store.close()


def test_locator_requires_a_real_anchor_and_rejects_drive_qualified_paths():
    with pytest.raises(ValueError, match="project-relative"):
        EvidenceLocator("source", "C:relative/source.md")
    with pytest.raises(ValueError, match="cannot locate evidence"):
        EvidenceLocator("source", "corpus/source.md")
    locator = EvidenceLocator("source", "corpus/source.md", quote="text")
    assert locator.corpus_path == "corpus/source.md"
    with pytest.raises(FrozenInstanceError):
        locator.quote = "other"


def test_reserved_audit_gaps_are_legal_but_never_auto_fabricated(tmp_path):
    """missing_primary_source / unverified_prediction are recorded-audit gap
    types: constructing one is legal, but the deterministic analyzer must not
    derive them from a fully located case."""
    gap = EvidenceGap(
        GAP_UNVERIFIED_PREDICTION,
        "prediction recorded without official confirmation",
    )
    assert gap.gap_type == GAP_UNVERIFIED_PREDICTION
    assert EvidenceGap(GAP_MISSING_PRIMARY_SOURCE, "primary text absent").gap_type

    store, case, nodes, scheduled, current, retrospective, claim = build_case(tmp_path)
    graph = GraphService(FakeBackend())
    run(
        graph.add_case(
            case,
            nodes=(*nodes, scheduled),
            facts=(current, retrospective),
            claims=(claim,),
        )
    )
    state = run(api_for(store, graph).query_case_state(case.case_id, CUTOFF))

    assert state.evidence_gaps == ()
    store.close()


def test_locator_refuses_an_excerpt_absent_from_the_corpus(tmp_path):
    store, *_ = build_case(tmp_path)

    with pytest.raises(LookupError, match="quote was not found"):
        store.locate("source-1", quote="invented evidence")
    store.close()


def make_status_material(source_id, *, published_at, fetched_at):
    return Material(
        id=source_id,
        title=source_id,
        source="example.gov",
        published_at=published_at,
        fetched_at=fetched_at,
        type="policy",
        content="Recorded evidence.",
        original_format="md",
        case_tags=("status-case",),
    )


def status_case_payload(status="implemented", *, status_at=None, status_observed_at=None):
    case = {
        "case_id": "status-case",
        "case_type": "policy",
        "canonical_name": "Status case",
        "start_at": START.isoformat(),
        "status": status,
        "node_ids": [],
    }
    if status_at is not None:
        case["status_at"] = status_at.isoformat()
    if status_observed_at is not None:
        case["status_observed_at"] = status_observed_at.isoformat()
    return {
        "case": case,
        "nodes": [],
        "temporal_facts": [],
        "claims": [],
        "warnings": [],
    }


def test_extracted_case_payload_future_status_is_none_at_cutoff():
    """ZCode High regression: a future status carried by an extraction/authored
    case payload (``status_at``/``status_observed_at`` after the cutoff) must
    never be backfilled into the historical state: the cutoff query returns
    ``None`` until both status times have passed."""
    effective = CUTOFF + timedelta(days=1)
    observed = CUTOFF + timedelta(days=1, hours=6)
    fetched = CUTOFF + timedelta(days=2)
    material = make_status_material(
        "source-ext-future", published_at=observed, fetched_at=fetched
    )
    extraction = extract_case_payload(
        status_case_payload(
            status_at=effective,
            status_observed_at=observed,
        ),
        material,
    )

    assert extraction.case is not None
    assert extraction.case.status_at == effective
    assert extraction.case.status_observed_at == observed

    graph = GraphService(FakeBackend())
    run(graph.add_case(extraction.case))

    state = run(AnalyzerService(graph).state("status-case", CUTOFF))

    assert state.case_type == "policy"
    assert state.status is None

    later = run(
        AnalyzerService(graph).state(
            "status-case", CUTOFF + timedelta(days=3)
        )
    )
    assert later.status == "implemented"


def test_extracted_case_status_without_times_is_bounded_by_material_publication():
    """A case payload without explicit status times is only observable from
    the material that asserted it: the extraction path must bind
    ``status_observed_at`` to the material publication date so a status known
    only from a later material cannot pollute earlier cutoff states."""
    published = CUTOFF + timedelta(days=1)
    fetched = CUTOFF + timedelta(days=2)
    material = make_status_material(
        "source-ext-late", published_at=published, fetched_at=fetched
    )
    extraction = extract_case_payload(
        status_case_payload(status="active"),
        material,
    )

    assert extraction.case is not None
    assert extraction.case.status_at is None
    assert extraction.case.status_observed_at == published

    graph = GraphService(FakeBackend())
    run(graph.add_case(extraction.case))

    state = run(AnalyzerService(graph).state("status-case", CUTOFF))

    assert state.status is None

    later = run(
        AnalyzerService(graph).state(
            "status-case", CUTOFF + timedelta(days=3)
        )
    )
    assert later.status == "active"


def test_locate_never_silently_ignores_quote_when_paragraph_is_given(tmp_path):
    """ZCode L-1 regression: locate() given a paragraph must still honor the
    quote.  A quote outside the given paragraph fails explicitly (naming the
    paragraph) instead of silently anchoring the stale paragraph, and a
    matching quote keeps the caller's paragraph anchor with the quote inside
    the returned excerpt."""
    store, *_ = build_case(tmp_path)
    source_id = "source-multi"
    path = store.paths.corpus_dir / "source-multi.md"
    write_material(
        path,
        source_id,
        "First paragraph about the initial proposal.\n"
        "Second paragraph about the later revision.",
        START + timedelta(days=1),
    )
    store.index_file(path)

    located = store.locate(
        source_id, paragraph=2, quote="later revision"
    )
    assert located.paragraph == 2
    assert "later revision" in located.quote

    quote_anchor = store.locate(source_id, quote="initial proposal")
    assert quote_anchor.paragraph == 1

    with pytest.raises(LookupError, match="paragraph 2"):
        store.locate(source_id, paragraph=2, quote="initial proposal")
    store.close()
