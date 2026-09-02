"""Shared offline fakes for the Phase A Graphiti spike tests.

A fake graph store stands in for a real Graphiti database and a fake registry
stands in for durable PRISM-side episode knowledge.  Nothing here imports
graphiti-core or neo4j and nothing touches a network.

The fake client models the graphiti-core 0.29.3 semantics the live probe
verified: ``add_episode(uuid=None)`` CREATES an episode with a
server-assigned uuid, while an explicit ``uuid`` performs a ``get_by_uuid``
lookup first and raises ``NodeNotFoundError`` when nothing exists under it;
``search`` takes ``group_ids`` (a plural list), not a singular ``group_id``.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from prism.graph import GraphEpisode


class NodeNotFoundError(Exception):
    """Stands in for graphiti-core's error for an unknown node uuid."""


class StoredEpisode:
    """What a real Graphiti database keeps about one of our episodes.

    ``uuid`` is the SERVER-assigned Graphiti uuid (a fresh uuid4 by default,
    exactly like a real ``uuid=None`` creation).  It is never the PRISM
    episode_key; the PRISM key lives inside ``episode_body``.
    """

    def __init__(self, episode: GraphEpisode, group_id: str, uuid: str | None = None) -> None:
        self.uuid = uuid if uuid is not None else str(uuid4())
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
    results carry ``episode_body`` (real Graphiti search results do not - it
    returns edges with episode uuid references).
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
        group_id: str | None = None,
        uuid: str | None = None,
    ) -> StoredEpisode:
        self.add_calls.append(
            {
                "name": name,
                "episode_body": episode_body,
                "source": source,
                "source_description": source_description,
                "reference_time": reference_time,
                "group_id": group_id,
                "uuid": uuid,
            }
        )
        if uuid is not None:
            # Real 0.29.3: an explicit uuid means "look it up first"; a miss
            # is a NodeNotFoundError, never a silent create.
            stored = self.store.episodes.get(uuid)
            if stored is None:
                raise NodeNotFoundError(f"no episode node with uuid {uuid!r}")
            return stored
        stored = StoredEpisode(
            SimpleNamespace(name=name, episode_body=episode_body),
            group_id or "",
        )
        self.store.episodes[stored.uuid] = stored
        return stored

    async def search(
        self, query: str, group_ids: list[str] | None = None, num_episodes: int = 5
    ) -> list:
        # Real 0.29.3 surface: group_ids (plural list); no singular group_id.
        self.search_calls.append((query, group_ids, num_episodes))
        results = []
        for stored in self.store.episodes.values():
            if self.group_aware and group_ids and stored.group_id not in group_ids:
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


class PlainQuerySearchClient(FakeGraphitiClient):
    """A client whose ``search`` accepts nothing but the query itself."""

    async def search(self, query: str) -> list:
        self.search_calls.append((query,))
        return [
            stored if self.with_body else SimpleNamespace(
                uuid=stored.uuid, group_id=stored.group_id
            )
            for stored in self.store.episodes.values()
        ]


class SyncCloseClient(FakeGraphitiClient):
    def close(self) -> None:
        self.closed = True


class NoCloseClient(FakeGraphitiClient):
    pass


class NestedResultClient(FakeGraphitiClient):
    """``add_episode`` returns the real graphiti-core 0.29.3 live-probe shape.

    The created episode is NOT the return value itself: it sits (as a node
    object whose ``uuid`` is a ``uuid.UUID``, not a str) under an
    ``episodes`` collection on the result, and the result carries no
    top-level ``uuid``.  The default fake's bare-``StoredEpisode`` return
    could not express this, which is exactly how the adapter's uuid capture
    slipped through offline tests while failing the live probe.
    """

    async def add_episode(self, **kwargs) -> SimpleNamespace:
        stored = await super().add_episode(**kwargs)
        return SimpleNamespace(episodes=[SimpleNamespace(uuid=UUID(stored.uuid))])


class MappingResultClient(FakeGraphitiClient):
    """``add_episode`` returns a plain mapping carrying the created uuid."""

    async def add_episode(self, **kwargs) -> dict:
        stored = await super().add_episode(**kwargs)
        return {"uuid": stored.uuid}


class NestedMappingResultClient(FakeGraphitiClient):
    """``add_episode`` returns a mapping with the created uuid nested in
    ``episodes`` (the mapping spelling of the real 0.29.3 shape)."""

    async def add_episode(self, **kwargs) -> dict:
        stored = await super().add_episode(**kwargs)
        return {"episodes": [{"uuid": stored.uuid}]}


class UuidlessResultClient(FakeGraphitiClient):
    """``add_episode`` returns a result carrying no uuid anywhere."""

    async def add_episode(self, **kwargs) -> SimpleNamespace:
        await super().add_episode(**kwargs)
        return SimpleNamespace(episodes=[])


class KeyEchoingResultClient(FakeGraphitiClient):
    """``add_episode`` echoes the PRISM episode_key back as the "uuid".

    Models a client that derives an identifier from the body: the adapter
    must neither send the key nor accept it as a Graphiti uuid.
    """

    async def add_episode(self, **kwargs) -> SimpleNamespace:
        await super().add_episode(**kwargs)
        payload = json.loads(kwargs["episode_body"])
        return SimpleNamespace(uuid=payload["episode_key"])


class SecretiveReprClient(FakeGraphitiClient):
    """A client whose repr embeds credentials (a URI with a password)."""

    SECRET = "hunter2-graphiti-password"

    def __repr__(self) -> str:
        return f"Graphiti(uri='bolt://neo4j:{self.SECRET}@127.0.0.1:7688')"


class RaisingSecretiveClient(SecretiveReprClient):
    """Also fails on ``add_episode`` with a credential-free error."""

    async def add_episode(self, **kwargs) -> None:
        raise RuntimeError("connection failed")


class FakeRegistry:
    """Durable PRISM-side episode knowledge (persistent across backend instances).

    Models the extended Phase B protocol: rows are keyed by PRISM
    ``episode_key``, may carry the real Graphiti-assigned uuid captured from
    an add result (plus the group it was recorded under), and support a
    group-scoped reverse lookup by that uuid - exactly the surface the
    SQLite-backed registry provides for real restarts.
    """

    def __init__(self) -> None:
        self.data: dict[str, GraphEpisode] = {}
        self._group_of: dict[str, str] = {}
        self._uuid_of: dict[str, str] = {}

    def get(self, episode_key: str) -> GraphEpisode | None:
        return self.data.get(episode_key)

    def put(
        self,
        episode: GraphEpisode,
        *,
        group_id: str = "",
        graphiti_uuid: str | None = None,
    ) -> None:
        self.data[episode.episode_key] = episode
        self._group_of[episode.episode_key] = group_id
        if graphiti_uuid is None:
            self._uuid_of.pop(episode.episode_key, None)
        else:
            self._uuid_of[episode.episode_key] = graphiti_uuid

    def get_by_graphiti_uuid(
        self, graphiti_uuid: str, *, group_id: str = ""
    ) -> GraphEpisode | None:
        for episode_key, uuid in self._uuid_of.items():
            if uuid == graphiti_uuid and self._group_of.get(episode_key, "") == group_id:
                return self.data.get(episode_key)
        return None
