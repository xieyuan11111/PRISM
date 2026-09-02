"""Offline temporal-semantics tests through GraphitiBackend (Phase A spike).

The same service+adapter code paths Phase B will run against a real Graphiti
database are exercised against a fake graph store: double-write idempotency,
restart behavior, revision/invalid_at half-open validity, conflicting facts
coexisting, and two different historical cutoffs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from prism.domain import EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.graph import GraphService
from prism.graph.backend import GraphitiBackend

from graphiti_fakes import FakeGraphitiClient, FakeRegistry

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def make_backend(client, *, group_id="neo4j", registry=None) -> GraphitiBackend:
    return GraphitiBackend(
        client,
        group_id=group_id,
        episode_type_json="json",
        registry=registry,
    )


def material(material_id: str, published_at: datetime) -> Material:
    return Material(
        id=material_id,
        title=f"Material {material_id}",
        source="example.gov",
        published_at=published_at,
        fetched_at=published_at + timedelta(hours=1),
        type="policy",
        content="body",
        original_format="md",
    )


def test_double_write_of_the_same_case_is_idempotent_through_the_adapter():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    service = GraphService(backend)
    case = EvolutionCase(
        "case-housing", "policy", "Housing policy", T0, "active", ["node-1"]
    )
    node = EvolutionNode(
        "node-1",
        "case-housing",
        "publication",
        T0 + timedelta(hours=1),
        "Published.",
        ("material-a",),
    )
    mat = material("material-a", T0 - timedelta(days=1))

    first = run(service.add_case(case, nodes=[node], materials=[mat]))
    second = run(service.add_case(case, nodes=[node], materials=[mat]))

    assert len(first.added_keys) == 3
    assert second.added_keys == ()
    assert set(second.skipped_keys) == {
        episode.episode_key for episode in second.episodes
    }
    # The adapter's write-before cache means the graph client only saw the
    # first submission's writes.
    assert len(client.add_calls) == 3


def test_restart_simulation_returns_identical_timeline_after_reconnect():
    # Process 1 writes through a backend and a registry.
    client = FakeGraphitiClient()
    registry = FakeRegistry()
    first_backend = make_backend(client, registry=registry)
    first_service = GraphService(first_backend)
    case = EvolutionCase(
        "case-housing", "policy", "Housing policy", T0, "active", ["node-1"]
    )
    node = EvolutionNode(
        "node-1",
        "case-housing",
        "publication",
        T0 + timedelta(hours=1),
        "Published.",
        ("material-a",),
    )
    run(first_service.add_case(case, nodes=[node]))

    # Process 2: fresh backend, fresh cache, same graph store and registry.
    second_backend = make_backend(client, registry=registry)
    second_service = GraphService(second_backend)
    before = run(first_service.timeline("case-housing", T0 + timedelta(days=1)))
    after = run(second_service.timeline("case-housing", T0 + timedelta(days=1)))

    assert [entry.episode_key for entry in after.entries] == [
        entry.episode_key for entry in before.entries
    ]
    assert {entry.kind for entry in after.entries} == {"evolution_case", "evolution_node"}
    # A duplicate write from the restarted process stays a no-op.
    duplicate = run(second_service.add_case(case, nodes=[node]))
    assert duplicate.added_keys == ()


def test_revision_invalidates_earlier_fact_at_two_cutoffs():
    client = FakeGraphitiClient()
    service = GraphService(make_backend(client))
    case = EvolutionCase("case-rates", "policy", "Rate policy", T0, "active", ())
    v1_until = T0 + timedelta(days=10)
    v2_from = T0 + timedelta(days=10)
    v1 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        T0,
        v1_until,
        T0,
        ("material-a",),
        0.9,
        "document",
    )
    v2 = TemporalFact(
        "Agency",
        "sets",
        "Rate=3%",
        v2_from,
        None,
        T0 + timedelta(days=9),
        ("material-b",),
        0.9,
        "document",
    )
    run(
        service.add_case(
            case,
            facts=[v1, v2],
            materials=[
                material("material-a", T0 - timedelta(days=1)),
                material("material-b", T0 + timedelta(days=9)),
            ],
        )
    )

    early = run(service.timeline("case-rates", T0 + timedelta(days=5)))
    late = run(service.timeline("case-rates", T0 + timedelta(days=20)))

    early_facts = [entry for entry in early.entries if entry.kind == "temporal_fact"]
    late_facts = [entry for entry in late.entries if entry.kind == "temporal_fact"]
    assert len(early_facts) == 1
    assert early_facts[0].source_ids == ("material-a",)
    assert early_facts[0].invalid_at == v1_until
    assert len(late_facts) == 1
    assert late_facts[0].source_ids == ("material-b",)
    assert late_facts[0].invalid_at is None
    assert early_facts[0].episode_key != late_facts[0].episode_key


def test_conflicting_facts_coexist_at_the_same_cutoff_without_merging():
    client = FakeGraphitiClient()
    service = GraphService(make_backend(client))
    case = EvolutionCase("case-conflict", "public_issue", "Disputed topic", T0, "active", ())
    fact_one = TemporalFact(
        "Agency",
        "requires",
        "Disclosure",
        T0,
        None,
        T0,
        ("material-a",),
        0.8,
        "document",
    )
    fact_two = TemporalFact(
        "Agency",
        "requires",
        "Disclosure",
        T0,
        None,
        T0,
        ("material-b",),
        0.4,
        "model_inference",
    )
    run(
        service.add_case(
            case,
            facts=[fact_one, fact_two],
            materials=[
                material("material-a", T0 - timedelta(days=1)),
                material("material-b", T0 - timedelta(days=1)),
            ],
        )
    )

    timeline = run(service.timeline("case-conflict", T0 + timedelta(days=1)))

    facts = [entry for entry in timeline.entries if entry.kind == "temporal_fact"]
    assert len(facts) == 2
    assert {entry.source_ids for entry in facts} == {
        ("material-a",),
        ("material-b",),
    }
    assert {entry.provenance_type for entry in facts} == {"document", "model_inference"}
    assert {entry.confidence for entry in facts} == {0.8, 0.4}
    # Both survive with distinct keys: no silent overwrite of the conflict.
    assert len({entry.episode_key for entry in facts}) == 2


def test_two_cutoffs_see_different_historical_states_with_material_boundary():
    import json

    client = FakeGraphitiClient()
    service = GraphService(make_backend(client))
    case = EvolutionCase("case-state", "policy", "State policy", T0, "active", ())
    # The second fact is only ever observed through a later material, so an
    # earlier cutoff must not see it even though its validity started before.
    fact_early = TemporalFact(
        "Agency",
        "state",
        "draft",
        T0,
        T0 + timedelta(days=3),
        T0,
        ("material-a",),
        0.9,
        "document",
    )
    fact_late_observed = TemporalFact(
        "Agency",
        "state",
        "final",
        T0 - timedelta(days=1),  # validity predates observation
        None,
        T0 + timedelta(days=5),
        ("material-b",),
        0.9,
        "document",
    )
    run(
        service.add_case(
            case,
            facts=[fact_early, fact_late_observed],
            materials=[
                material("material-a", T0 - timedelta(days=1)),
                material("material-b", T0 + timedelta(days=5)),
            ],
        )
    )

    early = run(service.timeline("case-state", T0 + timedelta(days=2)))
    later = run(service.timeline("case-state", T0 + timedelta(days=10)))

    def fact_objects(timeline):
        return {
            json.loads(entry.payload).get("object")
            for entry in timeline.entries
            if entry.kind == "temporal_fact"
        }

    # Early cutoff: only the draft was observed; the "final" fact's sole
    # material was not yet published, so it cannot leak into the past.
    assert fact_objects(early) == {"draft"}
    # Later cutoff: the draft expired and the final state is what is known.
    assert fact_objects(later) == {"final"}
