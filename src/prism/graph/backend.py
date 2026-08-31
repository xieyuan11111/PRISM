"""Graph backend protocol and the dependency-optional Graphiti adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .models import GraphEpisode


@runtime_checkable
class GraphBackend(Protocol):
    """Minimal backend surface needed by :class:`GraphService`."""

    async def add_episode(self, episode: GraphEpisode) -> bool:
        """Add an episode, returning false when its key already exists."""
        ...

    async def search(self, query: str) -> Sequence[GraphEpisode]:
        """Return explicit PRISM episodes relevant to a search query."""
        ...


class GraphitiBackend:
    """Adapt an injected Graphiti client without a hard Graphiti dependency.

    Graphiti's documented ``add_episode`` and ``search`` methods are the only
    client operations used. A small write-through registry maps the episode
    UUID references returned by Graphiti search results back to PRISM's exact
    payloads, preserving provenance and uncertainty without guessing at a
    live graph schema.
    """

    def __init__(self, client: Any, *, episode_type_json: Any | None = None):
        if client is None:
            raise ValueError("client is required")
        self._client = client
        self._injected_episode_type = episode_type_json
        self._episodes: dict[str, GraphEpisode] = {}

    def _json_episode_type(self) -> Any:
        if self._injected_episode_type is not None:
            return self._injected_episode_type
        try:
            # Deliberately lazy: importing prism.graph never imports Graphiti.
            from graphiti_core.nodes import EpisodeType
        except ImportError as error:
            raise RuntimeError(
                "graphiti-core is required unless episode_type_json is injected"
            ) from error
        return EpisodeType.json

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self._episodes:
            return False
        await self._client.add_episode(
            name=episode.name,
            episode_body=episode.episode_body,
            source=self._json_episode_type(),
            source_description=f"PRISM {episode.kind} episode",
            reference_time=episode.reference_time,
            uuid=episode.episode_key,
        )
        self._episodes[episode.episode_key] = episode
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        raw_results = await self._client.search(query)
        found: dict[str, GraphEpisode] = {}
        for result in raw_results or ():
            if isinstance(result, GraphEpisode):
                found[result.episode_key] = result
                continue
            for episode_key in self._episode_references(result):
                episode = self._episodes.get(episode_key)
                if episode is not None:
                    found[episode_key] = episode
        return tuple(found.values())

    @staticmethod
    def _episode_references(result: Any) -> tuple[str, ...]:
        if isinstance(result, Mapping):
            values = result.get("episodes", ())
            if not values and result.get("uuid") is not None:
                values = (result["uuid"],)
        else:
            values = getattr(result, "episodes", ())
            if not values and getattr(result, "uuid", None) is not None:
                values = (result.uuid,)
        if values is None:
            return ()
        if isinstance(values, str):
            return (values,)
        references: list[str] = []
        for value in values:
            if isinstance(value, str):
                references.append(value)
            elif isinstance(value, Mapping) and value.get("uuid") is not None:
                references.append(str(value["uuid"]))
            elif getattr(value, "uuid", None) is not None:
                references.append(str(value.uuid))
            else:
                references.append(str(value))
        return tuple(references)
