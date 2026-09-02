"""Shared offline fakes for the Phase A Graphiti spike tests.

A fake graph store stands in for a real Graphiti database and a fake registry
stands in for durable PRISM-side uuid knowledge.  Nothing here imports
graphiti-core or neo4j and nothing touches a network.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from prism.graph import GraphEpisode


class StoredEpisode:
    """What a real Graphiti database keeps about one of our episodes."""

    def __init__(self, episode: GraphEpisode, group_id: str) -> None:
        self.uuid = episode.episode_key
        self.name = episode.name
        self.episode_body = episode.episode_body
        self.group_id = group_id


class FakeGraphStore:
    def __init__(self) -> None:
        self.episodes: dict[str, StoredEpisode] = {}


class FakeGraphitiClient:
    """A fake Graphiti client backed by a shared store.

    ``group_aware`` clients filter ``search`` by group like a real Graphiti
    deployment is expected to; ``group_blind`` clients return everything and
    force the backend to filter.  ``with_body`` controls whether search
    results carry ``episode_body`` (real Graphiti search results do).
    """

    def __init__(
        self,
        store: FakeGraphStore | None = None,
        *,
        group_aware: bool = True,
        with_body: bool = True,
    ) -> None:
        self.store = store or FakeGraphStore()
        self.group_aware = group_aware
        self.with_body = with_body
        self.add_calls: list[dict] = []
        self.search_calls: list[tuple] = []
        self.closed = False

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source: str,
        source_description: str,
        reference_time: datetime,
        uuid: str,
        group_id: str | None = None,
    ) -> None:
        self.add_calls.append(
            {
                "name": name,
                "episode_body": episode_body,
                "source": source,
                "source_description": source_description,
                "reference_time": reference_time,
                "uuid": uuid,
                "group_id": group_id,
            }
        )
        if uuid not in self.store.episodes:
            self.store.episodes[uuid] = StoredEpisode(
                SimpleNamespace(
                    episode_key=uuid,
                    name=name,
                    episode_body=episode_body,
                ),
                group_id or "",
            )

    async def search(self, query: str, group_id: str | None = None) -> list:
        self.search_calls.append((query, group_id))
        results = []
        for stored in self.store.episodes.values():
            if self.group_aware and group_id is not None and stored.group_id != group_id:
                continue
            if self.with_body:
                results.append(stored)
            else:
                results.append(
                    SimpleNamespace(uuid=stored.uuid, group_id=stored.group_id)
                )
        return results

    async def aclose(self) -> None:
        self.closed = True


class SyncCloseClient(FakeGraphitiClient):
    def close(self) -> None:
        self.closed = True


class NoCloseClient(FakeGraphitiClient):
    pass


class FakeRegistry:
    """Durable PRISM-side uuid knowledge (persistent across backend instances)."""

    def __init__(self) -> None:
        self.data: dict[str, GraphEpisode] = {}

    def get(self, episode_key: str) -> GraphEpisode | None:
        return self.data.get(episode_key)

    def put(self, episode: GraphEpisode) -> None:
        self.data[episode.episode_key] = episode
