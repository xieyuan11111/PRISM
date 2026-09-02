"""Offline adapter tests for the PRISM-only GraphitiBackend (Phase A spike).

These tests never import graphiti-core or neo4j and never touch a network.
A fake graph store stands in for a real Graphiti database, and a fake registry
stands in for durable PRISM-side uuid knowledge, so idempotency and restart
behavior are exercised with the same code paths Phase B will run live.

Scope note: the group-scoped tests below (``test_group_isolation_...``,
``test_search_filters_results_whose_group_mismatches_...``) are PURE ADAPTER
CONTRACT tests over the fake store.  They are NOT Community live acceptance:
graphiti-core 0.29.3 realises a Neo4j group as a database, Neo4j Community
serves one built-in database, and live Phase B uses group_id == database ==
"neo4j" on a PRISM-dedicated instance (see docs/graphiti-spike-plan.md).
Group labels in these tests are opaque partition strings, not Community
config values.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from prism.graph import GraphEpisode
from prism.graph.backend import (
    EPISODE_SCHEMA,
    GraphEpisodeRegistry,
    GraphitiBackend,
    canonical_json,
)
from graphiti_fakes import (
    FakeGraphitiClient,
    FakeGraphStore,
    FakeRegistry,
    NoCloseClient,
    StoredEpisode,
    SyncCloseClient,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def make_episode(
    key="4d8fe701-5578-5ca3-a436-1f24d29c6300",
    *,
    case_id="case-a",
    kind="claim",
    invalid_at=None,
    source_ids=("material-a",),
    evidence=(),
    schema=EPISODE_SCHEMA,
) -> GraphEpisode:
    payload = {
        "schema": schema,
        "case_id": case_id,
        "kind": kind,
        "reference_time": NOW.isoformat(),
        "valid_at": NOW.isoformat(),
        "invalid_at": invalid_at.isoformat() if invalid_at else None,
        "source_ids": list(source_ids),
        "evidence": [
            {
                "source_id": item.source_id,
                "corpus_path": item.corpus_path,
                "paragraph": item.paragraph,
                "page": item.page,
                "quote": item.quote,
            }
            for item in evidence
        ],
        "episode_key": key,
    }
    body = canonical_json(payload)
    return GraphEpisode(
        episode_key=key,
        name=f"prism:{case_id}:{kind}:{key[:12]}",
        case_id=case_id,
        kind=kind,
        episode_body=body,
        reference_time=NOW,
        valid_at=NOW,
        invalid_at=invalid_at,
        source_ids=source_ids,
        evidence=evidence,
    )


def test_backend_requires_an_explicit_group_id():
    client = FakeGraphitiClient()
    with pytest.raises(TypeError, match="group_id"):
        GraphitiBackend(client)
    with pytest.raises(ValueError, match="group_id"):
        GraphitiBackend(client, group_id="")
    with pytest.raises(ValueError, match="group_id"):
        GraphitiBackend(client, group_id="   ")
    GraphitiBackend(client, group_id="neo4j")


def test_backend_requires_a_client():
    with pytest.raises(ValueError, match="client"):
        GraphitiBackend(None, group_id="neo4j")  # type: ignore[arg-type]


def make_backend(client, *, group_id="neo4j", registry=None) -> GraphitiBackend:
    return GraphitiBackend(
        client,
        group_id=group_id,
        episode_type_json="json",
        registry=registry,
    )


def test_add_episode_passes_group_id_and_is_in_process_idempotent():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True
    assert run(backend.add_episode(episode)) is False
    assert len(client.add_calls) == 1
    assert client.add_calls[0]["uuid"] == episode.episode_key
    assert client.add_calls[0]["group_id"] == "neo4j"
    assert client.add_calls[0]["name"] == episode.name
    assert client.add_calls[0]["episode_body"] == episode.episode_body
    assert client.add_calls[0]["source"] == "json"
    assert client.add_calls[0]["source_description"] == "PRISM claim episode"
    assert client.add_calls[0]["reference_time"] == episode.reference_time


def test_registry_supplies_write_before_existence_lookup_across_restart():
    # Process 1 writes and persists its uuid knowledge in the registry.
    client = FakeGraphitiClient()
    registry = FakeRegistry()
    first = make_backend(client, registry=registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    assert registry.get(episode.episode_key) is episode

    # Process 2 (fresh backend, same registry, same graph) must not re-write:
    # the write-before existence lookup short-circuits before the client call.
    second = make_backend(client, registry=registry)
    assert run(second.add_episode(episode)) is False
    assert len(client.add_calls) == 1


def test_without_registry_a_fresh_backend_rewrites_after_restart():
    # Honest boundary: the in-process cache is not persistent.  Without a
    # registry the adapter cannot claim cross-process idempotency, so the
    # client sees the write again after a restart.
    client = FakeGraphitiClient()
    first = make_backend(client)
    second = make_backend(client)
    episode = make_episode()

    assert run(first.add_episode(episode)) is True
    assert run(second.add_episode(episode)) is True
    assert len(client.add_calls) == 2


def test_search_after_restart_maps_episodes_from_graph_bodies():
    # No registry: a fresh backend maps search results whose episode bodies
    # carry the PRISM schema, exactly like a real Graphiti restart would.
    client = FakeGraphitiClient()
    first = make_backend(client)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True

    restarted = make_backend(client)
    results = run(restarted.search("prism query"))

    assert client.search_calls == [("prism query", "neo4j")]
    assert len(results) == 1
    mapped = results[0]
    assert mapped.episode_key == episode.episode_key
    assert mapped.case_id == episode.case_id
    assert mapped.kind == episode.kind
    assert mapped.episode_body == episode.episode_body
    assert mapped.reference_time == episode.reference_time
    assert mapped.source_ids == episode.source_ids


def test_search_only_maps_prism_schema_episodes():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    ours = make_episode()
    run(backend.add_episode(ours))
    foreign = make_episode(
        "11111111-2222-3333-4444-555555555555",
        case_id="case-x",
        schema="some.other.episode.v1",
    )
    client.store.episodes[foreign.episode_key] = StoredEpisode(foreign, "other-group")

    results = run(backend.search("anything"))

    assert [episode.episode_key for episode in results] == [ours.episode_key]


def test_search_skips_unknown_uuids_without_bodies():
    client = FakeGraphitiClient(with_body=False)
    backend = make_backend(client)
    episode = make_episode()
    run(backend.add_episode(episode))
    # The store gains an episode this backend never wrote and whose body the
    # client does not return: nothing to positively map -> skipped.
    client.store.episodes["22222222-3333-4444-5555-666666666666"] = StoredEpisode(
        make_episode("22222222-3333-4444-5555-666666666666", case_id="case-y"), "neo4j"
    )

    results = run(backend.search("anything"))

    assert [episode.episode_key for episode in results] == [episode.episode_key]


def test_search_filters_results_whose_group_mismatches_even_when_client_is_group_blind():
    # Pure adapter defense contract: when a client returns results from more
    # than one group (a group-blind client, or a future multi-database
    # server), the adapter still skips results tagged with another group.
    # NOT a Community live acceptance - a Community instance has one database
    # (see the module docstring).
    client = FakeGraphitiClient(group_aware=False)
    backend = make_backend(client)
    ours = make_episode()
    theirs = make_episode("33333333-4444-5555-6666-777777777777", case_id="case-other")
    run(backend.add_episode(ours))
    client.store.episodes[theirs.episode_key] = StoredEpisode(theirs, "another-group")

    results = run(backend.search("anything"))

    assert [episode.episode_key for episode in results] == [ours.episode_key]


def test_group_isolation_between_two_backends_shared_graph():
    # Pure adapter contract test over a fake store shared by two group-scoped
    # backends ("tenant-a"/"tenant-b" are opaque partition labels).
    # NOT a Community live acceptance: graphiti-core 0.29.3 realises a Neo4j
    # group as a database, Neo4j Community serves one built-in database, and
    # live Phase B isolation is the PRISM-dedicated instance + schema-marker
    # gating (see the module docstring and docs/graphiti-spike-plan.md).
    store = FakeGraphStore()
    group_a = FakeGraphitiClient(store)
    group_b = FakeGraphitiClient(store)
    backend_a = make_backend(group_a, group_id="tenant-a")
    backend_b = make_backend(group_b, group_id="tenant-b")
    episode_a = make_episode("aaaaaaaa-1111-2222-3333-444444444444", case_id="case-a")
    episode_b = make_episode("bbbbbbbb-1111-2222-3333-444444444444", case_id="case-b")

    assert run(backend_a.add_episode(episode_a)) is True
    assert run(backend_b.add_episode(episode_b)) is True

    results_a = run(backend_a.search("query"))
    results_b = run(backend_b.search("query"))

    assert [episode.episode_key for episode in results_a] == [episode_a.episode_key]
    assert [episode.episode_key for episode in results_b] == [episode_b.episode_key]
    assert group_a.search_calls[-1] == ("query", "tenant-a")
    assert group_b.search_calls[-1] == ("query", "tenant-b")


def test_search_passes_through_graph_episode_results_directly():
    class PassthroughClient(FakeGraphitiClient):
        def __init__(self) -> None:
            super().__init__()
            self.search_results = []

        async def search(self, query: str, group_id: str | None = None):
            return self.search_results

    passthrough = PassthroughClient()
    episode = make_episode()
    passthrough.search_results = [episode]
    backend = make_backend(passthrough)
    assert run(backend.search("case query")) == (episode,)


def test_search_maps_registry_episodes_without_bodies():
    client = FakeGraphitiClient(with_body=False)
    registry = FakeRegistry()
    backend = make_backend(client, registry=registry)
    episode = make_episode()
    registry.put(episode)
    # The graph store holds the episode too, but the client returns no body;
    # only the registry's uuid knowledge maps the result back.
    client.store.episodes[episode.episode_key] = StoredEpisode(episode, "neo4j")

    results = run(backend.search("query"))

    assert results == (episode,)


def test_registry_interface_is_runtime_checkable():
    assert isinstance(FakeRegistry(), GraphEpisodeRegistry)


def test_close_is_idempotent_and_handles_async_and_sync_clients():
    async_client = FakeGraphitiClient()
    async_backend = make_backend(async_client)
    run(async_backend.close())
    run(async_backend.close())
    assert async_client.closed is True

    sync_client = SyncCloseClient()
    sync_backend = make_backend(sync_client)
    run(sync_backend.close())
    assert sync_client.closed is True

    bare_client = NoCloseClient()
    bare_backend = make_backend(bare_client)
    run(bare_backend.close())  # must not raise


def test_close_after_writes_does_not_raise():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    run(backend.add_episode(make_episode()))
    run(backend.close())
    assert client.closed is True
