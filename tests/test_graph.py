"""Offline contract tests for the PRISM temporal graph module (module 4)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prism.domain import Claim, EvolutionCase, EvolutionNode, Material, TemporalFact
from prism.graph import (
    GraphEpisode,
    GraphService,
    GraphTimeline,
    GraphitiBackend,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class FakeBackend:
    """In-memory backend with no database, network, or LLM."""

    def __init__(self):
        self.episodes: dict[str, GraphEpisode] = {}
        self.add_calls: list[GraphEpisode] = []
        self.search_calls: list[str] = []

    async def add_episode(self, episode: GraphEpisode) -> bool:
        self.add_calls.append(episode)
        if episode.episode_key in self.episodes:
            return False
        self.episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str):
        self.search_calls.append(query)
        return tuple(self.episodes.values())


class FakeGraphitiClient:
    def __init__(self):
        self.add_calls = []
        self.search_calls = []
        self.search_results = []

    async def add_episode(self, **kwargs):
        self.add_calls.append(kwargs)

    async def search(self, query):
        self.search_calls.append(query)
        return self.search_results


def fixtures():
    case = EvolutionCase(
        "case-housing", "policy", "Housing policy", NOW, "active", ["node-publish"]
    )
    node = EvolutionNode(
        "node-publish",
        case.case_id,
        "publication",
        NOW + timedelta(hours=1),
        "The policy was published.",
        ["material-a", "material-b"],
        ["claim-a"],
    )
    fact = TemporalFact(
        "Agency",
        "requires",
        "Disclosure",
        NOW + timedelta(hours=2),
        NOW + timedelta(days=2),
        NOW + timedelta(hours=3),
        ["material-a", "material-b"],
        0.61,
        "model_inference",
    )
    claim = Claim(
        "claim-a",
        "Researcher",
        "The policy may improve disclosure.",
        "uncertain",
        NOW + timedelta(hours=4),
        ["material-a", "material-b"],
        "claim-b",
    )
    material = Material(
        id="material-a",
        title="Official notice",
        source="example.gov",
        published_at=NOW - timedelta(hours=1),
        fetched_at=NOW,
        type="policy",
        content="TOP-SECRET raw document body",
        original_format="html",
        raw_path="C:/private/alice/raw.html",
        url="https://example.gov/notice?token=super-secret",
    )
    return case, node, fact, claim, material


def test_service_maps_every_domain_object_to_explicit_safe_episode_payloads():
    backend = FakeBackend()
    service = GraphService(backend)
    case, node, fact, claim, material = fixtures()

    result = run(
        service.add_case(
            case,
            nodes=[node],
            facts=[fact],
            claims=[claim],
            materials=[material],
        )
    )

    assert len(result.episodes) == 5
    assert len(result.added_keys) == 5
    payloads = {json.loads(episode.episode_body)["kind"]: json.loads(episode.episode_body) for episode in result.episodes}
    assert set(payloads) == {
        "evolution_case",
        "evolution_node",
        "temporal_fact",
        "claim",
        "material_provenance",
    }
    assert payloads["evolution_node"]["source_ids"] == ["material-a", "material-b"]
    assert payloads["temporal_fact"]["source_ids"] == ["material-a", "material-b"]
    assert payloads["temporal_fact"]["confidence"] == 0.61
    assert payloads["temporal_fact"]["provenance_type"] == "model_inference"
    assert payloads["claim"]["stance"] == "uncertain"
    assert payloads["claim"]["revised_by"] == "claim-b"
    serialized = "\n".join(episode.episode_body for episode in result.episodes)
    assert "TOP-SECRET" not in serialized
    assert "super-secret" not in serialized
    assert "C:/private/alice" not in serialized


def test_episode_keys_are_stable_and_backend_makes_duplicate_writes_noops():
    backend = FakeBackend()
    service = GraphService(backend)
    case, node, fact, claim, material = fixtures()

    first = run(service.add_case(case, nodes=[node], facts=[fact], claims=[claim], materials=[material]))
    second = run(service.add_case(case, nodes=[node], facts=[fact], claims=[claim], materials=[material]))

    assert [episode.episode_key for episode in first.episodes] == [
        episode.episode_key for episode in second.episodes
    ]
    assert len(first.added_keys) == 5
    assert second.added_keys == ()
    assert second.skipped_keys == tuple(episode.episode_key for episode in second.episodes)


def test_as_of_timeline_uses_half_open_validity_and_keeps_provenance():
    backend = FakeBackend()
    service = GraphService(backend)
    case, node, fact, claim, material = fixtures()
    run(service.add_case(case, nodes=[node], facts=[fact], claims=[claim], materials=[material]))

    before = run(service.timeline(case.case_id, NOW + timedelta(hours=1, minutes=30)))
    during = run(service.timeline(case.case_id, NOW + timedelta(days=1)))
    at_invalidation = run(service.timeline(case.case_id, NOW + timedelta(days=2)))

    assert isinstance(during, GraphTimeline)
    assert all(entry.case_id == case.case_id for entry in during.entries)
    assert [entry.valid_at for entry in during.entries] == sorted(
        entry.valid_at for entry in during.entries
    )
    assert not any(entry.kind == "temporal_fact" for entry in before.entries)
    fact_entry = next(entry for entry in during.entries if entry.kind == "temporal_fact")
    assert fact_entry.source_ids == ("material-a", "material-b")
    assert fact_entry.confidence == 0.61
    assert fact_entry.provenance_type == "model_inference"
    assert not any(entry.kind == "temporal_fact" for entry in at_invalidation.entries)
    assert backend.search_calls[-1] == "PRISM timeline for case_id=case-housing"


def test_timeline_rejects_naive_as_of_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        run(GraphService(FakeBackend()).timeline("case-housing", datetime(2026, 8, 31)))


def test_graph_episode_rejects_naive_or_reversed_validity():
    with pytest.raises(ValueError, match="timezone-aware"):
        GraphEpisode(
            "key", "name", "case", "claim", "{}", datetime(2026, 8, 31), NOW, None, ()
        )
    with pytest.raises(ValueError, match="invalid_at"):
        GraphEpisode(
            "key", "name", "case", "claim", "{}", NOW, NOW, NOW - timedelta(seconds=1), ()
        )


def test_graphiti_adapter_calls_only_documented_add_and_search_methods():
    client = FakeGraphitiClient()
    adapter = GraphitiBackend(client, episode_type_json="json")
    episode = GraphEpisode(
        episode_key="4d8fe701-5578-5ca3-a436-1f24d29c6300",
        name="prism:case:claim:claim-a",
        case_id="case",
        kind="claim",
        episode_body='{"kind":"claim"}',
        reference_time=NOW,
        valid_at=NOW,
        invalid_at=None,
        source_ids=("material-a",),
    )

    assert run(adapter.add_episode(episode)) is True
    assert run(adapter.add_episode(episode)) is False
    assert len(client.add_calls) == 1
    assert client.add_calls[0] == {
        "name": episode.name,
        "episode_body": episode.episode_body,
        "source": "json",
        "source_description": "PRISM claim episode",
        "reference_time": NOW,
        "uuid": episode.episode_key,
    }

    client.search_results = [SimpleNamespace(episodes=[episode.episode_key])]
    assert run(adapter.search("case query")) == (episode,)
    assert client.search_calls == ["case query"]


def test_graphiti_adapter_does_not_require_graphiti_core_when_type_is_injected(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("graphiti_core"):
            raise AssertionError("graphiti-core was imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    adapter = GraphitiBackend(FakeGraphitiClient(), episode_type_json="json")
    episode = GraphEpisode("key", "name", "case", "claim", "{}", NOW, NOW, None, ())
    assert run(adapter.add_episode(episode)) is True


def test_graphiti_adapter_accepts_uuid_objects_from_search_results():
    client = FakeGraphitiClient()
    adapter = GraphitiBackend(client, episode_type_json="json")
    episode = GraphEpisode("key", "name", "case", "claim", "{}", NOW, NOW, None, ())
    run(adapter.add_episode(episode))

    client.search_results = [SimpleNamespace(uuid=episode.episode_key)]

    assert run(adapter.search("case query")) == (episode,)
