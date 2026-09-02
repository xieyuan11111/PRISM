"""Offline adapter tests for the PRISM-only GraphitiBackend (Phase A spike).

These tests never import graphiti-core or neo4j and never touch a network.
A fake graph store stands in for a real Graphiti database, and a fake or
SQLite-backed registry stands in for durable PRISM-side episode knowledge, so
idempotency and restart behavior are exercised with the same code paths Phase
B will run live (the composition root injects the project-owned SQLite
registry on the real-Graphiti path).

Real graphiti-core 0.29.3 semantics these tests pin down (live-probe
verified): ``add_episode(uuid=None)`` CREATES an episode under a
server-assigned uuid, while an explicit uuid performs a ``get_by_uuid``
lookup that raises ``NodeNotFoundError`` on a miss - so the adapter never
passes the PRISM episode_key as a uuid, keeps the key in the episode body,
and records the returned Graphiti uuid in an in-process auditable cache
AND - when a registry is injected - in the persistent registry (which is
what a restarted process consults).  ``search`` takes ``group_ids``
(plural), so the adapter negotiates the parameters each client really
declares.  The real 0.29.3 ``add_episode`` return nests the created episode
(whose ``uuid`` is a ``uuid.UUID``) under an ``episodes`` collection with no
top-level ``uuid``, so the uuid capture below negotiates the result's shape
instead of assuming one, and records nothing when no uuid is extractable.

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
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.config import PathConfig
from prism.graph import GraphEpisode, SQLiteEpisodeRegistry
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
    KeyEchoingResultClient,
    MappingResultClient,
    NestedMappingResultClient,
    NestedResultClient,
    NodeNotFoundError,
    NoCloseClient,
    PlainQuerySearchClient,
    RaisingSecretiveClient,
    SecretiveReprClient,
    StoredEpisode,
    SyncCloseClient,
    UuidlessResultClient,
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
    include_key=True,
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
    }
    if include_key:
        payload["episode_key"] = key
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


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "output",
        raw_dir=Path("raw"),
        corpus_dir=Path("corpus"),
    )


def test_first_add_creates_without_uuid_and_is_in_process_idempotent():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True
    assert run(backend.add_episode(episode)) is False
    assert len(client.add_calls) == 1
    call = client.add_calls[0]
    # First creation must take the uuid=None path: an explicit uuid makes real
    # graphiti-core 0.29.3 call get_by_uuid first and raise NodeNotFoundError
    # for an episode that does not exist yet.
    assert call["uuid"] is None
    assert call["group_id"] == "neo4j"
    assert call["name"] == episode.name
    assert call["episode_body"] == episode.episode_body
    assert call["source"] == "json"
    assert call["source_description"] == "PRISM claim episode"
    assert call["reference_time"] == episode.reference_time

    # The stored node is keyed by the server-assigned Graphiti uuid, never by
    # the PRISM episode_key; the audit cache records exactly that mapping.
    stored = next(iter(client.store.episodes.values()))
    assert stored.uuid != episode.episode_key
    assert backend.graphiti_uuids == {episode.episode_key: stored.uuid}


def test_registry_supplies_write_before_existence_lookup_across_restart():
    # Process 1 writes and persists its episode knowledge in the registry.
    client = FakeGraphitiClient()
    registry = FakeRegistry()
    first = make_backend(client, registry=registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    assert registry.get(episode.episode_key) is episode

    # Process 2 (fresh backend, same registry, same graph) must not re-write:
    # the write-before existence lookup short-circuits before the client call,
    # so no second Graphiti node is ever created.
    second = make_backend(client, registry=registry)
    assert run(second.add_episode(episode)) is False
    assert len(client.add_calls) == 1
    assert len(client.store.episodes) == 1


def test_without_registry_a_fresh_backend_rewrites_after_restart():
    # Honest boundary: without a registry there is no write-before lookup a
    # restarted process can trust, so the client sees the write again - and
    # because creation goes through uuid=None, Graphiti assigns the second
    # write its own distinct uuid (a real duplicate node).  PRISM timelines
    # stay stable anyway: both bodies carry the same PRISM episode_key and
    # search dedups by it.
    client = FakeGraphitiClient()
    first = make_backend(client)
    second = make_backend(client)
    episode = make_episode()

    assert run(first.add_episode(episode)) is True
    assert run(second.add_episode(episode)) is True
    assert len(client.add_calls) == 2
    assert len({stored.uuid for stored in client.store.episodes.values()}) == 2


def test_write_body_injects_the_prism_episode_key_when_missing():
    # The body is the only durable PRISM-side identity of a stored episode
    # (the Graphiti uuid is server business), so a JSON body lacking the key
    # gets it injected before the write.
    client = FakeGraphitiClient()
    backend = make_backend(client)
    episode = make_episode(include_key=False)

    assert run(backend.add_episode(episode)) is True
    stored = next(iter(client.store.episodes.values()))
    payload = json.loads(stored.episode_body)
    assert payload["episode_key"] == episode.episode_key

    # Restart: a fresh backend rebuilds the episode from the stored body.
    restarted = make_backend(client)
    results = run(restarted.search("prism query"))
    assert [item.episode_key for item in results] == [episode.episode_key]


def test_search_after_restart_maps_episodes_from_graph_bodies():
    # No registry: a fresh backend maps search results whose episode bodies
    # carry the PRISM schema, exactly like a real Graphiti restart would.
    client = FakeGraphitiClient()
    first = make_backend(client)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True

    restarted = make_backend(client)
    results = run(restarted.search("prism query"))

    assert len(results) == 1
    mapped = results[0]
    assert mapped.episode_key == episode.episode_key
    assert mapped.case_id == episode.case_id
    assert mapped.kind == episode.kind
    assert mapped.episode_body == episode.episode_body
    assert mapped.reference_time == episode.reference_time
    assert mapped.source_ids == episode.source_ids


def test_search_negotiates_the_real_graphiti_signature():
    # The shared fake declares the real 0.29.3 surface: group_ids (plural
    # list).  The adapter must pass exactly that - never an invented singular
    # group_id - and nothing else beyond the query.
    client = FakeGraphitiClient()
    backend = make_backend(client)

    run(backend.search("prism query"))

    assert client.search_calls == [("prism query", ["neo4j"], 5)]


def test_search_calls_bare_signature_clients_without_group_arguments():
    # A client whose search accepts only the query is called with only the
    # query: the adapter never invents unsupported keyword arguments.
    client = PlainQuerySearchClient()
    backend = make_backend(client)
    episode = make_episode()
    run(backend.add_episode(episode))

    results = run(backend.search("anything"))

    assert client.search_calls == [("anything",)]
    assert [item.episode_key for item in results] == [episode.episode_key]


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


def test_search_skips_unknown_graphiti_uuids_without_bodies():
    client = FakeGraphitiClient(with_body=False)
    backend = make_backend(client)
    episode = make_episode()
    run(backend.add_episode(episode))
    # The store gains an episode this backend never wrote (its Graphiti uuid
    # is unknown here) and the client returns no body: nothing to positively
    # map -> skipped.
    client.store.episodes["22222222-3333-4444-5555-666666666666"] = StoredEpisode(
        make_episode("22222222-3333-4444-5555-666666666666", case_id="case-y"), "neo4j"
    )

    results = run(backend.search("anything"))

    assert [episode.episode_key for episode in results] == [episode.episode_key]


def test_search_maps_bodyless_results_through_the_graphiti_uuid_cache():
    # Same process: the backend knows which Graphiti uuid each PRISM episode
    # got (audit cache), so a body-less result referencing that uuid still
    # maps back to the full episode.
    client = FakeGraphitiClient(with_body=False)
    backend = make_backend(client)
    episode = make_episode()
    run(backend.add_episode(episode))
    stored = next(iter(client.store.episodes.values()))

    results = run(backend.search("query"))

    assert results == (episode,)
    assert backend.graphiti_uuids[episode.episode_key] == stored.uuid


def test_graphiti_uuid_cache_is_a_read_only_audit_view():
    client = FakeGraphitiClient()
    backend = make_backend(client)
    episode = make_episode()
    run(backend.add_episode(episode))
    stored = next(iter(client.store.episodes.values()))

    view = backend.graphiti_uuids
    assert view == {episode.episode_key: stored.uuid}
    view[episode.episode_key] = "tampered"
    assert backend.graphiti_uuids[episode.episode_key] == stored.uuid


def test_add_captures_the_graphiti_uuid_from_the_real_nested_result_shape():
    # Live-probe regression: the real graphiti-core 0.29.3 ``add_episode``
    # return carries NO top-level uuid - the created episode node (with a
    # ``uuid.UUID`` uuid) is nested under ``episodes``.  The adapter used to
    # record nothing there, so ``graphiti_uuids`` stayed empty and body-less
    # readback could not attribute anything.
    client = NestedResultClient(with_body=False)
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True

    stored = next(iter(client.store.episodes.values()))
    assert backend.graphiti_uuids == {episode.episode_key: stored.uuid}
    assert stored.uuid != episode.episode_key
    # The captured uuid is what makes same-process, body-less readback work.
    assert run(backend.search("query")) == (episode,)


def test_add_captures_the_graphiti_uuid_from_a_mapping_result():
    client = MappingResultClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True

    stored = next(iter(client.store.episodes.values()))
    assert backend.graphiti_uuids == {episode.episode_key: stored.uuid}


def test_add_captures_the_graphiti_uuid_from_a_nested_mapping_result():
    client = NestedMappingResultClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True

    stored = next(iter(client.store.episodes.values()))
    assert backend.graphiti_uuids == {episode.episode_key: stored.uuid}


def test_add_without_a_returned_uuid_records_nothing_and_keeps_body_rebuild():
    client = UuidlessResultClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True
    assert client.add_calls[0]["uuid"] is None
    # No uuid anywhere in the result: nothing is recorded - never the PRISM
    # key, never a fabricated value.
    assert backend.graphiti_uuids == {}
    assert episode.episode_key not in backend.graphiti_uuids.values()
    # The write stays usable without a cached uuid: a restarted backend
    # still maps the episode from its PRISM schema body.
    restarted = make_backend(client)
    assert [item.episode_key for item in run(restarted.search("query"))] == [
        episode.episode_key
    ]


def test_prism_episode_key_is_never_treated_as_the_graphiti_uuid():
    client = KeyEchoingResultClient()
    backend = make_backend(client)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True
    # The write never sends the PRISM key as a uuid...
    assert client.add_calls[0]["uuid"] is None
    # ...and a client echoing the key back does not turn it into a Graphiti
    # uuid: the two identities stay separate in the audit cache.
    assert backend.graphiti_uuids == {}


def test_restart_rebuilds_from_bodies_and_registry_stays_prism_key_idempotent():
    client = NestedResultClient()
    registry = FakeRegistry()
    first = make_backend(client, registry=registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    stored = next(iter(client.store.episodes.values()))
    assert first.graphiti_uuids == {episode.episode_key: stored.uuid}

    # Restart: the uuid audit cache is per-process, so the fresh backend
    # starts with an empty one (nothing fabricated) and reads back through
    # the PRISM schema bodies.
    restarted = make_backend(client, registry=registry)
    assert restarted.graphiti_uuids == {}
    results = run(restarted.search("query"))
    assert [item.episode_key for item in results] == [episode.episode_key]
    # Idempotency across the restart is by PRISM key in the registry, never
    # by the server-assigned Graphiti uuid.
    assert run(restarted.add_episode(episode)) is False
    assert len(client.add_calls) == 1


def test_backend_errors_never_embed_client_credentials(monkeypatch):
    secret = SecretiveReprClient.SECRET
    client = SecretiveReprClient()

    with pytest.raises(TypeError, match="group_id"):
        GraphitiBackend(client)
    with pytest.raises(ValueError, match="group_id") as exc:
        GraphitiBackend(client, group_id=" ")
    assert secret not in str(exc.value)

    # The lazy graphiti-core import failure must stay credential-free even
    # though the injected client's repr carries a secret URI; forcing the
    # ImportError keeps this honest on machines where the optional extra IS
    # installed.
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", None)
    backend = GraphitiBackend(client, group_id="neo4j")
    with pytest.raises(RuntimeError, match="graphiti-core") as exc:
        run(backend.add_episode(make_episode()))
    assert secret not in str(exc.value)
    assert client.add_calls == []

    # A client-side failure propagates as the client's own error text: the
    # adapter never wraps it with anything that could embed the client
    # (whose repr holds the secret).
    raising_backend = make_backend(RaisingSecretiveClient())
    with pytest.raises(RuntimeError, match="connection failed") as exc:
        run(raising_backend.add_episode(make_episode()))
    assert str(exc.value) == "connection failed"


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
    assert group_a.search_calls[-1] == ("query", ["tenant-a"], 5)
    assert group_b.search_calls[-1] == ("query", ["tenant-b"], 5)


def test_search_passes_through_graph_episode_results_directly():
    class SingularGroupClient(FakeGraphitiClient):
        # Models alternate/older clients that declare a singular group_id:
        # the negotiation falls back to that spelling for them.
        def __init__(self) -> None:
            super().__init__()
            self.search_results = []

        async def search(self, query: str, group_id: str | None = None):
            self.search_calls.append((query, group_id))
            return self.search_results

    passthrough = SingularGroupClient()
    episode = make_episode()
    passthrough.search_results = [episode]
    backend = make_backend(passthrough)
    assert run(backend.search("case query")) == (episode,)
    assert passthrough.search_calls == [("case query", "neo4j")]


def test_search_maps_registry_episodes_whose_references_are_prism_keys():
    client = FakeGraphitiClient(with_body=False)
    registry = FakeRegistry()
    backend = make_backend(client, registry=registry)
    episode = make_episode()
    registry.put(episode)
    # Models a store whose episode references ARE deterministic PRISM keys
    # (PRISM's own 0.29.3 writes carry server-assigned Graphiti uuids
    # instead); the registry still attributes such a result positively, by
    # PRISM key, without any body.
    client.store.episodes[episode.episode_key] = StoredEpisode(
        episode, "neo4j", uuid=episode.episode_key
    )

    results = run(backend.search("query"))

    assert results == (episode,)


def test_fake_client_models_real_uuid_lookup_semantics():
    # Fake fidelity: an explicit unknown uuid is a get_by_uuid miss
    # (NodeNotFoundError), and uuid=None creations are never deduplicated by
    # the client itself.  This is exactly why the adapter must not pass the
    # PRISM episode_key as a uuid and needs its own idempotency layers.
    client = FakeGraphitiClient()
    episode = make_episode()
    with pytest.raises(NodeNotFoundError):
        run(
            client.add_episode(
                name=episode.name,
                episode_body=episode.episode_body,
                source="json",
                source_description="PRISM claim episode",
                reference_time=NOW,
                group_id="neo4j",
                uuid=episode.episode_key,
            )
        )

    def create():
        return run(
            client.add_episode(
                name=episode.name,
                episode_body=episode.episode_body,
                source="json",
                source_description="PRISM claim episode",
                reference_time=NOW,
                group_id="neo4j",
                uuid=None,
            )
        )

    first = create()
    second = create()
    assert first.uuid != second.uuid
    assert len(client.store.episodes) == 2


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


# ---------------------------------------------------------------------------
# Persistent SQLite registry (Phase B): real-nested-uuid capture, restart
# idempotency and body-less EntityEdge attribution across process restarts.
# ---------------------------------------------------------------------------


def test_sqlite_registry_persists_the_real_nested_uuid_across_restart(tmp_path):
    # The real graphiti-core 0.29.3 add result nests the created episode
    # (uuid.UUID) under ``episodes``; the adapter captures it and the SQLite
    # registry persists it, so a RESTARTED backend (empty in-process caches,
    # fresh registry object on the same database file) can still attribute
    # body-less EntityEdge search results by uuid.
    client = NestedResultClient(with_body=False)
    paths = make_paths(tmp_path)

    first_registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    first = make_backend(client, registry=first_registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    stored = next(iter(client.store.episodes.values()))
    assert stored.uuid != episode.episode_key
    assert first.graphiti_uuids == {episode.episode_key: stored.uuid}
    first_registry.close()

    # Restart: nothing is carried over in memory - not the audit cache, not
    # the registry object.  Only the SQLite file persists.
    restarted_registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    restarted = make_backend(client, registry=restarted_registry)
    try:
        assert restarted.graphiti_uuids == {}
        results = run(restarted.search("prism query"))
        assert results == (episode,)
        # The restarted process also short-circuits the duplicate write
        # before the client call: no second Graphiti node is created.
        assert run(restarted.add_episode(episode)) is False
        assert len(client.add_calls) == 1
        assert len(client.store.episodes) == 1
    finally:
        restarted_registry.close()


def test_sqlite_registry_persists_identity_across_separate_graph_clients(tmp_path):
    # A harder restart: even the Graphiti client/store is a fresh object (a
    # new process cannot share the old driver), so the only durable state is
    # the SQLite registry plus the episode bodies in the graph itself.  With
    # body-less search results, the registry uuid mapping is what attributes
    # the restarted search.
    paths = make_paths(tmp_path)
    first_client = NestedResultClient(with_body=False)
    first_registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    first = make_backend(first_client, registry=first_registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    uuid = next(iter(first_client.store.episodes.values())).uuid
    first_registry.close()

    second_client = FakeGraphStore()
    second_client.episodes[uuid] = StoredEpisode(episode, "neo4j", uuid=uuid)
    second_registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    second = make_backend(
        FakeGraphitiClient(second_client, with_body=False),
        registry=second_registry,
    )
    try:
        results = run(second.search("prism query"))
        assert results == (episode,)
    finally:
        second_registry.close()


def test_sqlite_registry_reverse_lookup_stays_within_the_backend_group(tmp_path):
    # Defensive group boundary through the persistent registry: a body-less
    # result may reference a uuid that IS recorded - but in another group's
    # registry row.  The reverse lookup is group-scoped, so the adapter must
    # not attribute that result to a PRISM episode of its own group.
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    client = FakeGraphitiClient(with_body=False)
    backend = make_backend(client, registry=registry)
    ours = make_episode()
    run(backend.add_episode(ours))
    foreign = make_episode(
        "33333333-4444-5555-6666-777777777777", case_id="case-other"
    )
    foreign_uuid = "88888888-9999-aaaa-bbbb-cccccccccccc"
    registry.put(foreign, group_id="tenant-b", graphiti_uuid=foreign_uuid)
    # The graph itself returns a body-less result for that uuid tagged with
    # OUR group label: the adapter's group-mismatch filter passes it, but the
    # group-scoped reverse lookup still refuses the foreign row.
    client.store.episodes[foreign_uuid] = StoredEpisode(
        foreign, "neo4j", uuid=foreign_uuid
    )

    try:
        results = run(backend.search("anything"))
        assert [item.episode_key for item in results] == [ours.episode_key]
    finally:
        registry.close()


def test_sqlite_registry_never_persists_the_prism_key_as_a_graphiti_uuid(
    tmp_path,
):
    # A client echoing the PRISM key back as the "uuid" must not launder it
    # into the persistent mapping: the row records NO uuid (nothing was ever
    # fabricated), so a restarted process still cannot reverse-lookup it.
    client = KeyEchoingResultClient()
    paths = make_paths(tmp_path)
    registry = SQLiteEpisodeRegistry(paths, database="neo4j")
    backend = make_backend(client, registry=registry)
    episode = make_episode()

    assert run(backend.add_episode(episode)) is True
    assert backend.graphiti_uuids == {}
    try:
        with sqlite3.connect(registry.db_path) as conn:
            row = conn.execute(
                "SELECT graphiti_uuid FROM graphiti_episode_registry"
                " WHERE episode_key = ?",
                (episode.episode_key,),
            ).fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        registry.close()


def test_fake_registry_records_the_graphiti_uuid_for_bodyless_restart_attribution():
    # Same restart scenario through the shared FakeRegistry: the fake must
    # model the persistent mapping (uuid recorded at write time), so a fresh
    # backend with an empty in-process cache attributes body-less results.
    client = FakeGraphitiClient(with_body=False)
    registry = FakeRegistry()
    first = make_backend(client, registry=registry)
    episode = make_episode()
    assert run(first.add_episode(episode)) is True
    stored = next(iter(client.store.episodes.values()))

    restarted = make_backend(client, registry=registry)
    assert restarted.graphiti_uuids == {}
    assert run(restarted.search("query")) == (episode,)
    assert run(restarted.add_episode(episode)) is False
    assert len(client.add_calls) == 1
