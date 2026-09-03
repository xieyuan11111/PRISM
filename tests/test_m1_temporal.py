"""Offline acceptance tests for the M1 temporal evolution slice."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from prism.analyzer import AnalyzerService
from prism.api import PrismAPI
from prism.cli import main as cli_main
from prism.domain import (
    Claim,
    EvidenceLocator,
    EvolutionCase,
    Material,
    TemporalFact,
    TemporalRelation,
)
from prism.extraction import ExtractionConflict, ExtractionResult, ExtractionService
from prism.graph import GraphEpisode, GraphService
from prism.report import ReportService


UTC = timezone.utc
T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
T3 = datetime(2026, 3, 1, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


class MemoryBackend:
    def __init__(self, episodes=None):
        self.episodes: dict[str, GraphEpisode] = dict(episodes or {})

    async def add_episode(self, episode):
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query):
        return tuple(self.episodes.values())


def locator(source_id: str, quote: str) -> EvidenceLocator:
    return EvidenceLocator(
        source_id,
        f"corpus/{source_id}.md",
        paragraph=1,
        quote=quote,
    )


def material(source_id: str, at: datetime, text: str) -> Material:
    return Material(
        source_id,
        source_id,
        "example.test",
        at,
        at,
        "policy",
        text,
        case_tags=("m1-case",),
        access_level="fulltext",
    )


def fact(
    fact_id: str,
    value: str,
    *,
    valid_at: datetime,
    observed_at: datetime,
    source_id: str,
    invalid_at: datetime | None = None,
    confidence: float = 1.0,
) -> TemporalFact:
    quote = f"status is {value}"
    return TemporalFact(
        "Policy",
        "status",
        value,
        valid_at,
        invalid_at,
        observed_at,
        (source_id,),
        confidence,
        "source_explicit",
        (locator(source_id, quote),),
        fact_id=fact_id,
    )


def relation(
    relation_id: str,
    relation_type: str,
    source_ref: str,
    target_ref: str,
    *,
    at: datetime = T2,
    source_id: str = "revision",
    with_evidence: bool = True,
) -> TemporalRelation:
    evidence = (
        locator(source_id, f"{source_ref} {relation_type} {target_ref}"),
    ) if with_evidence else ()
    return TemporalRelation(
        relation_id,
        relation_type,
        source_ref,
        target_ref,
        at,
        None,
        at,
        (source_id,),
        evidence,
        1.0,
        "source_explicit",
    )


def case_bundle(*, include_trigger: bool = True):
    case = EvolutionCase("m1-case", "policy", "M1 policy", T1, "active")
    old = fact(
        "fact-old", "draft", valid_at=T1, invalid_at=T2,
        observed_at=T1, source_id="original",
    )
    replacement = fact(
        "fact-new", "revised", valid_at=T2, observed_at=T2,
        source_id="revision",
    )
    conflict_a = fact(
        "fact-conflict-a", "effective", valid_at=T2, observed_at=T2,
        source_id="source-a", confidence=0.8,
    )
    conflict_b = fact(
        "fact-conflict-b", "suspended", valid_at=T2, observed_at=T2,
        source_id="source-b", confidence=0.6,
    )
    relations = [
        relation("rel-supersedes", "supersedes", "fact-new", "fact-old"),
        relation(
            "rel-contradicts", "contradicts", "fact-conflict-a",
            "fact-conflict-b", source_id="source-a",
        ),
    ]
    if include_trigger:
        relations.append(
            relation(
                "rel-trigger", "triggered_by", "fact-new", "event-review"
            )
        )
    return case, (old, replacement, conflict_a, conflict_b), tuple(relations)


def test_temporal_relation_is_frozen_slotted_aware_and_tuple_normalized():
    item = TemporalRelation(
        "rel", "revises", "claim-new", "claim-old", T2, None, T2,
        ["revision"], [locator("revision", "new revises old")], 0.9,
        "source_explicit",
    )

    assert item.source_ids == ("revision",)
    assert isinstance(item.evidence, tuple)
    assert not hasattr(item, "__dict__")
    with pytest.raises(FrozenInstanceError):
        item.relation_type = "contradicts"
    with pytest.raises(ValueError, match="timezone-aware"):
        TemporalRelation(
            "bad", "revises", "a", "b", datetime(2026, 1, 1), None,
            T2, ("revision",), (), 1.0, "source_explicit",
        )


def test_old_fact_constructor_remains_compatible():
    old = TemporalFact(
        "Policy", "status", "draft", T1, None, T1,
        ("original",), 1.0, "source_explicit",
    )
    assert old.fact_id is None


def test_two_cutoffs_hide_but_retain_old_fact_and_keep_conflicts_separate():
    backend = MemoryBackend()
    graph = GraphService(backend)
    case, facts, relations = case_bundle()
    run(graph.add_case(case, facts=facts, relations=relations))

    before = run(graph.timeline(case.case_id, T2 - timedelta(seconds=1)))
    after = run(graph.timeline(case.case_id, T3))

    before_payloads = [json.loads(item.payload) for item in before.entries]
    after_payloads = [json.loads(item.payload) for item in after.entries]
    invalidated = [json.loads(item.payload) for item in after.invalidated_entries]
    assert {item.get("fact_id") for item in before_payloads} >= {"fact-old"}
    assert "fact-old" not in {item.get("fact_id") for item in after_payloads}
    assert "fact-old" in {item.get("fact_id") for item in invalidated}
    assert {item.get("fact_id") for item in after_payloads} >= {
        "fact-new", "fact-conflict-a", "fact-conflict-b"
    }
    conflict_facts = [
        item for item in after.entries
        if json.loads(item.payload).get("fact_id", "").startswith("fact-conflict")
    ]
    assert {(item.source_ids, item.confidence) for item in conflict_facts} == {
        (("source-a",), 0.8), (("source-b",), 0.6)
    }
    assert all(item.evidence for item in conflict_facts)


def test_later_fact_version_invalidates_the_prior_open_version():
    backend = MemoryBackend()
    graph = GraphService(backend)
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    original = fact(
        "fact-old", "draft", valid_at=T1, observed_at=T1, source_id="original"
    )
    corrected = fact(
        "fact-old", "draft", valid_at=T1, invalid_at=T2,
        observed_at=T2, source_id="revision",
    )
    run(graph.add_case(case, facts=(original,)))
    run(graph.add_case(case, facts=(corrected,)))

    before = run(graph.timeline(case.case_id, T2 - timedelta(seconds=1)))
    after = run(graph.timeline(case.case_id, T3))
    assert any(json.loads(item.payload).get("fact_id") == "fact-old" for item in before.entries)
    assert not any(json.loads(item.payload).get("fact_id") == "fact-old" for item in after.entries)
    assert [json.loads(item.payload).get("fact_id") for item in after.invalidated_entries] == [
        "fact-old"
    ]


def test_revised_by_projects_a_revision_relation_but_not_a_cause():
    graph = GraphService(MemoryBackend())
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    old = Claim(
        "claim-old", "Agency", "Old interpretation", "support", T1,
        ("revision",), "claim-new",
        (locator("revision", "New interpretation revises old"),),
        T2,
    )
    new = Claim(
        "claim-new", "Agency", "New interpretation", "conditional", T2,
        ("revision",), None,
        (locator("revision", "New interpretation revises old"),),
        T2,
    )
    run(graph.add_case(case, claims=(old, new)))

    analysis = run(AnalyzerService(graph).analyze(case.case_id, T3))
    revisions = [
        stage for stage in analysis.stages if stage.relation_type == "revises"
    ]
    assert [(stage.source_ref, stage.target_ref) for stage in revisions] == [
        ("claim-new", "claim-old")
    ]
    assert analysis.change_reasons == ()
    assert any(
        question.origin == "unconfirmed_change_cause"
        for question in analysis.open_questions
    )


def test_strict_extraction_accepts_only_source_bound_temporal_relations():
    body = "A review explicitly triggered the revised status."
    source = material("revision", T2, body)
    payload = {
        "case": {
            "case_id": "m1-case", "case_type": "policy",
            "canonical_name": "M1", "start_at": T1.isoformat(),
            "status": "active", "node_ids": [], "status_at": T2.isoformat(),
            "status_observed_at": T2.isoformat(),
        },
        "nodes": [], "temporal_facts": [], "claims": [], "conflicts": [],
        "relations": [{
            "relation_id": "rel-trigger", "relation_type": "triggered_by",
            "source_ref": "fact-new", "target_ref": "event-review",
            "valid_at": T2.isoformat(), "invalid_at": None,
            "observed_at": T2.isoformat(), "source_ids": ["revision"],
            "evidence": [{
                "source_id": "revision", "quote": body, "paragraph": 1,
                "page": None,
            }],
            "confidence": 1.0, "provenance_type": "source_explicit",
        }],
        "warnings": [],
    }

    class Router:
        async def complete(self, role, prompt):
            return type("Completion", (), {"text": json.dumps(payload)})()

    result = run(
        ExtractionService(Router()).extract_material(
            source, corpus_path="corpus/revision.md"
        )
    )
    assert result.relations[0].evidence[0].quote == body
    assert result.relations[0].observed_at == T2


def test_only_evidenced_triggered_by_becomes_a_change_reason():
    for include_trigger in (False, True):
        graph = GraphService(MemoryBackend())
        case, facts, relations = case_bundle(include_trigger=include_trigger)
        run(graph.add_case(case, facts=facts, relations=relations))
        analysis = run(AnalyzerService(graph).analyze(case.case_id, T3))
        if include_trigger:
            assert [reason.reason_type for reason in analysis.change_reasons] == [
                "triggered_by"
            ]
            assert analysis.change_reasons[0].evidence
            assert not any(
                question.origin == "unconfirmed_change_cause"
                and "fact-new" in question.question
                for question in analysis.open_questions
            )
        else:
            assert analysis.change_reasons == ()
            assert any(
                question.origin == "unconfirmed_change_cause"
                and "fact-new" in question.question
                for question in analysis.open_questions
            )


def test_triggered_by_without_evidence_is_not_presented_as_causality():
    graph = GraphService(MemoryBackend())
    case, facts, relations = case_bundle(include_trigger=False)
    unevidenced = relation(
        "rel-trigger", "triggered_by", "fact-new", "event-review",
        with_evidence=False,
    )
    run(graph.add_case(case, facts=facts, relations=(*relations, unevidenced)))

    analysis = run(AnalyzerService(graph).analyze(case.case_id, T3))
    assert analysis.change_reasons == ()
    assert any(
        question.origin == "unconfirmed_change_cause"
        and "fact-new" in question.question
        for question in analysis.open_questions
    )


def test_compare_reports_old_fact_removed_and_replacement_added():
    graph = GraphService(MemoryBackend())
    case, facts, relations = case_bundle()
    run(graph.add_case(case, facts=facts, relations=relations))

    comparison = run(
        AnalyzerService(graph).compare(
            case.case_id, T2 - timedelta(seconds=1), T3
        )
    )
    assert "Policy status draft" in {item.summary for item in comparison.removed}
    assert {
        "Policy status revised", "Policy status effective",
        "Policy status suspended",
    }.issubset({item.summary for item in comparison.added})


def test_extraction_conflict_becomes_a_traceable_contradicts_episode():
    source = material("revision", T2, "status is revised and status is suspended")
    conflict = ExtractionConflict(
        "conflict-1", "Policy", "status", ("revised", "suspended"),
        (source.id,),
        (locator(source.id, "status is revised and status is suspended"),),
        valid_at=T2,
        observed_at=T2,
        confidence=0.7,
    )
    graph = GraphService(MemoryBackend())
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    run(graph.add_case(case, conflicts=(conflict,), materials=(source,)))

    timeline = run(graph.timeline(case.case_id, T3))
    contradiction = next(
        item
        for item in timeline.entries
        if json.loads(item.payload).get("relation_type") == "contradicts"
    )
    assert contradiction.source_ids == ("revision",)
    assert contradiction.confidence == 0.7
    assert contradiction.evidence == conflict.evidence


def test_pipeline_graph_contract_carries_relations_conflicts_and_evidence(tmp_path):
    class CapturingGraph:
        def __init__(self):
            self.kwargs = None

        async def add_case(self, case, **kwargs):
            from prism.graph import GraphWriteResult
            self.kwargs = kwargs
            return GraphWriteResult((), (), ())

    from prism.cases import CaseBundleMerger
    from prism.cases.ledger import CaseExtractionLedger
    from prism.cases.service import CaseService
    from prism.config import PathConfig

    source = material("revision", T2, "new contradicts old")
    conflict = ExtractionConflict(
        "conflict-1", "Policy", "status", ("new", "old"),
        (source.id,), (locator(source.id, "new contradicts old"),),
        valid_at=T2, observed_at=T2,
    )
    change = relation("rel", "revises", "fact-new", "fact-old")
    extraction = ExtractionResult(
        case=EvolutionCase("m1-case", "policy", "M1", T1, "active"),
        conflicts=(conflict,),
        relations=(change,),
    )
    paths = PathConfig(
        data_dir=tmp_path / "data",
        corpus_dir=tmp_path / "corpus",
        raw_dir=tmp_path / "raw",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
    )
    graph = CapturingGraph()
    ledger = CaseExtractionLedger(paths)
    service = CaseService(
        ledger=ledger, merger=CaseBundleMerger(), graph_service=graph
    )
    try:
        run(service.record_extraction(source, extraction))
    finally:
        ledger.close()
    assert graph.kwargs["relations"] == (change,)
    assert graph.kwargs["conflicts"] == (conflict,)
    assert graph.kwargs["conflicts"][0].evidence == conflict.evidence


def test_report_and_cli_surface_invalidated_revision_conflict_and_evidence():
    backend = MemoryBackend()
    graph = GraphService(backend)
    case, facts, relations = case_bundle()
    run(graph.add_case(case, facts=facts, relations=relations))
    analyzer = AnalyzerService(graph)
    analysis = run(analyzer.analyze(case.case_id, T3))
    report = run(ReportService().report(analysis))

    assert "## Invalidated Facts" in report.markdown
    assert "fact-old" in report.markdown
    assert "supersedes" in report.markdown
    assert "contradicts" in report.markdown
    assert "event-review" in report.markdown
    assert "corpus/revision.md" in report.markdown

    class Unused:
        def ingest(self, path, metadata=None):
            raise AssertionError

    class Store:
        def index_file(self, path):
            raise AssertionError

        def search(self, criteria, *, limit, offset):
            raise AssertionError

    class Bus:
        async def publish(self, event):
            raise AssertionError

    api = PrismAPI(
        Unused(), Store(), graph, Bus(), analyzer_service=analyzer,
        report_service=ReportService(),
    )
    stdout, stderr = StringIO(), StringIO()
    status = run(
        cli_main(
            ["timeline", case.case_id, "--as-of", T3.isoformat()],
            api=api, stdout=stdout, stderr=stderr,
        )
    )
    assert status == 0 and stderr.getvalue() == ""
    cli_value = json.loads(stdout.getvalue())
    api_value = run(api.build_timeline(case.case_id, T3))
    assert cli_value["invalidated_entries"][0]["episode_key"] == (
        api_value.invalidated_entries[0].episode_key
    )
    assert any(
        json.loads(item["payload"]).get("relation_type") == "contradicts"
        for item in cli_value["entries"]
    )


def test_restart_and_repeated_write_are_idempotent_for_relation_episodes():
    backend = MemoryBackend()
    case, facts, relations = case_bundle()
    first = run(GraphService(backend).add_case(case, facts=facts, relations=relations))
    restarted = run(
        GraphService(backend).add_case(case, facts=facts, relations=relations)
    )
    assert first.added_keys
    assert restarted.added_keys == ()
    assert set(restarted.skipped_keys) == set(first.added_keys)


def test_invalid_at_cutoff_is_exclusive_and_observation_bound_is_inclusive():
    """The effective interval is [valid_at, invalid_at).

    At ``as_of == invalid_at`` the fact is known (observed) but no longer
    effective: it belongs to invalidated_entries, never entries.  At
    ``as_of == observed_at`` an entry becomes known.
    """
    backend = MemoryBackend()
    graph = GraphService(backend)
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    closing = fact(
        "fact-old", "draft", valid_at=T1, invalid_at=T2,
        observed_at=T2, source_id="revision",
    )
    run(graph.add_case(case, facts=(closing,)))

    at_t2 = run(graph.timeline(case.case_id, T2))
    assert not any(
        json.loads(item.payload).get("fact_id") == "fact-old"
        for item in at_t2.entries
    )
    assert [json.loads(item.payload).get("fact_id")
            for item in at_t2.invalidated_entries] == ["fact-old"]

    at_t1 = run(graph.timeline(case.case_id, T1))
    assert not any(item.kind == "temporal_fact" for item in at_t1.entries)
    assert not any(item.kind == "temporal_fact" for item in at_t1.invalidated_entries)
    observed = run(graph.timeline(case.case_id, T2 + timedelta(seconds=1)))
    assert [json.loads(item.payload).get("fact_id")
            for item in observed.invalidated_entries] == ["fact-old"]


def test_material_publication_bounds_relation_and_fact_visibility():
    """A valid-but-unobserved record stays hidden until its bound material
    exists: reference/valid/invalid/observed are all part of the filter."""
    backend = MemoryBackend()
    graph = GraphService(backend)
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    source = material("revision", T2, "status is revised")
    backdated = fact(
        "fact-new", "revised", valid_at=T1, observed_at=T1,
        source_id="revision",
    )
    supersedes = relation(
        "rel-supersedes", "supersedes", "fact-new", "fact-old",
        at=T1, source_id="revision",
    )
    run(graph.add_case(
        case, facts=(backdated,), relations=(supersedes,), materials=(source,)
    ))

    before = run(graph.timeline(case.case_id, T2 - timedelta(seconds=1)))
    assert not any(
        item.kind in {"temporal_fact", "temporal_relation"}
        for item in before.entries
    )
    after = run(graph.timeline(case.case_id, T3))
    assert {item.kind for item in after.entries} >= {
        "temporal_fact", "temporal_relation"
    }


def test_conflict_without_observation_bound_is_refused_loudly():
    """A conflict with neither observed_at nor bound materials cannot be
    placed on the timeline; it must fail loudly, never be guessed."""
    backend = MemoryBackend()
    graph = GraphService(backend)
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    conflict = ExtractionConflict(
        "conflict-1", "Policy", "status", ("revised", "suspended"),
        ("revision",),
        (locator("revision", "status is revised and status is suspended"),),
    )
    with pytest.raises(ValueError, match="observed_at or bound material"):
        run(graph.add_case(case, conflicts=(conflict,)))


def test_claim_revision_relation_materializes_when_the_revising_claim_arrives():
    """Accumulation replays the full claim set, so a revision relation that
    could not exist in the first single-claim write appears once the revising
    claim is present — without rewriting or duplicating earlier episodes."""
    backend = MemoryBackend()
    graph = GraphService(backend)
    case = EvolutionCase("m1-case", "policy", "M1", T1, "active")
    old = Claim(
        "claim-old", "Agency", "Old interpretation", "support", T1,
        ("revision",), "claim-new",
        (locator("revision", "Old interpretation"),), T1,
    )
    new = Claim(
        "claim-new", "Agency", "New interpretation", "conditional", T2,
        ("revision",), None,
        (locator("revision", "New interpretation revises old"),), T2,
    )
    run(graph.add_case(case, claims=(old,)))
    assert not any(
        json.loads(item.payload).get("relation_type") == "revises"
        for item in run(graph.timeline(case.case_id, T3)).entries
    )
    replayed = run(graph.add_case(case, claims=(old, new)))
    relation_episodes = [
        episode for episode in replayed.episodes
        if json.loads(episode.episode_body).get("relation_type") == "revises"
    ]
    assert relation_episodes
    assert relation_episodes[0].episode_key in replayed.added_keys
    stages = [
        json.loads(item.payload)
        for item in run(graph.timeline(case.case_id, T3)).entries
        if json.loads(item.payload).get("relation_type") == "revises"
    ]
    assert [(item["source_ref"], item["target_ref"]) for item in stages] == [
        ("claim-new", "claim-old")
    ]


def test_timeline_cli_json_never_exposes_private_paths_or_keys():
    """The CLI timeline JSON carries only portable fields: raw_path, urls and
    absolute host paths never leave the service layer."""
    backend = MemoryBackend()
    graph = GraphService(backend)
    case, facts, relations = case_bundle()
    private = material(
        "revision", T2, "status is revised",
    )
    private = Material(
        private.id, private.title, private.source, private.published_at,
        private.fetched_at, private.type, private.content,
        case_tags=private.case_tags, access_level=private.access_level,
        raw_path="C:/private/raw/revision.pdf",
        url="https://private.example.test/revision",
    )
    run(graph.add_case(case, facts=facts, relations=relations, materials=(private,)))

    class Unused:
        def ingest(self, path, metadata=None):
            raise AssertionError

    class Store:
        def index_file(self, path):
            raise AssertionError

        def search(self, criteria, *, limit, offset):
            raise AssertionError

    class Bus:
        async def publish(self, event):
            raise AssertionError

    api = PrismAPI(
        Unused(), Store(), graph, Bus(),
        analyzer_service=AnalyzerService(graph),
        report_service=ReportService(),
    )
    stdout, stderr = StringIO(), StringIO()
    status = run(
        cli_main(
            ["timeline", case.case_id, "--as-of", T3.isoformat()],
            api=api, stdout=stdout, stderr=stderr,
        )
    )
    assert status == 0 and stderr.getvalue() == ""
    text = stdout.getvalue()
    for forbidden in ("raw_path", "private.example.test", "C:/private", "url"):
        assert forbidden not in text
    assert "invalidated_entries" in text
