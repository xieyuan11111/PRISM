"""Graph backend protocol and the PRISM-only Graphiti adapter.

Phase A (Graphiti/Neo4j spike) design notes
-------------------------------------------
- Importing ``prism.graph`` never imports ``graphiti_core`` or ``neo4j`` and
  builds no client.  The real client is only ever constructed by an explicit
  factory path in the composition root after ``graphiti.enabled=true``.
- Every episode is written under an explicit ``group_id`` so PRISM data is
  scoped inside its own Graphiti database/group.
- Idempotency across process restarts is provided by an injected registry
  (write-before existence lookup), never by the in-process cache.  The
  ``_episodes`` dict below is a per-process local cache only and is NOT
  presented as persistent storage anywhere.
- ``search`` maps ONLY episodes that can be positively attributed to PRISM:
  either through the registry/cache uuid knowledge or through an episode body
  carrying PRISM's own schema marker.  Anything else is skipped, never
  guessed at.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from prism.domain import EvidenceLocator

from .models import EPISODE_SCHEMA, GraphEpisode, canonical_json


@runtime_checkable
class GraphBackend(Protocol):
    """Minimal backend surface needed by :class:`GraphService`."""

    async def add_episode(self, episode: GraphEpisode) -> bool:
        """Add an episode, returning false when its key already exists."""
        ...

    async def search(self, query: str) -> Sequence[GraphEpisode]:
        """Return explicit PRISM episodes relevant to a search query."""
        ...


@runtime_checkable
class GraphEpisodeRegistry(Protocol):
    """Durable ``episode_key`` -> episode knowledge consulted before writes.

    The adapter looks a key up in the registry BEFORE calling the Graphiti
    client (write-before existence lookup) and stores the episode there after
    a successful add.  A persistent registry therefore makes duplicate writes
    idempotent across process restarts without relying on any Graphiti
    behavior PRISM cannot verify.  Implementations are injected so the adapter
    never guesses at a live graph schema; a fake in-memory registry exercises
    the same code paths offline.
    """

    def get(self, episode_key: str) -> GraphEpisode | None:
        """Return the stored episode for ``episode_key``, or None."""
        ...

    def put(self, episode: GraphEpisode) -> None:
        """Persist ``episode`` under its episode_key."""
        ...


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expected a timezone-aware ISO-8601 string")
    return parsed


def _parse_evidence(value: object) -> tuple[EvidenceLocator, ...] | None:
    """Parse PRISM evidence locators; None means the payload is unusable."""
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    locators: list[EvidenceLocator] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        try:
            locators.append(
                EvidenceLocator(
                    source_id=str(item["source_id"]),
                    corpus_path=str(item["corpus_path"]),
                    paragraph=item.get("paragraph"),
                    page=item.get("page"),
                    quote=item.get("quote"),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    return tuple(locators)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number")
    return float(value)


def _episode_from_body(body: object) -> GraphEpisode | None:
    """Rebuild a :class:`GraphEpisode` from a PRISM episode body.

    Used when Graphiti search returns episodes this process never wrote (for
    example after a restart): the body's schema marker is the positive proof
    that the episode is PRISM's own.  Any unusable payload yields None rather
    than a guessed episode.
    """
    if not isinstance(body, str):
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema") != EPISODE_SCHEMA:
        return None
    try:
        episode_key = str(payload["episode_key"])
        case_id = str(payload["case_id"])
        kind = str(payload["kind"])
        source_ids = tuple(str(item) for item in payload["source_ids"])
        evidence = _parse_evidence(payload.get("evidence"))
        if evidence is None:
            return None
        confidence = _optional_float(payload.get("confidence"))
        provenance_type = payload.get("provenance_type")
        return GraphEpisode(
            episode_key=episode_key,
            name=f"prism:{case_id}:{kind}:{episode_key[:12]}",
            case_id=case_id,
            kind=kind,
            episode_body=canonical_json(payload),
            reference_time=_parse_datetime(payload["reference_time"]),
            valid_at=_parse_datetime(payload["valid_at"]),
            invalid_at=(
                _parse_datetime(payload["invalid_at"])
                if payload.get("invalid_at") is not None
                else None
            ),
            source_ids=source_ids,
            confidence=confidence,
            provenance_type=provenance_type,
            evidence=evidence,
        )
    except (KeyError, TypeError, ValueError):
        return None


class GraphitiBackend:
    """Adapt an injected Graphiti client without a hard Graphiti dependency.

    Graphiti's documented ``add_episode`` and ``search`` methods are the only
    client operations used.  All episodes belong to the explicit ``group_id``
    and search results are mapped back to PRISM payloads only when they can be
    positively attributed (registry/cache uuid knowledge or a PRISM schema
    marker in the returned body), preserving provenance and uncertainty
    without guessing at a live graph schema.
    """

    def __init__(
        self,
        client: Any,
        *,
        group_id: str,
        episode_type_json: Any | None = None,
        registry: GraphEpisodeRegistry | None = None,
    ):
        if client is None:
            raise ValueError("client is required")
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError(
                "group_id is required: PRISM episodes must be written and "
                "searched under an explicit Graphiti group"
            )
        self._client = client
        self._group_id = group_id.strip()
        self._injected_episode_type = episode_type_json
        self._registry = registry
        # Per-process local cache only.  It is NOT persistent: cross-process
        # idempotency comes from the injected registry, and cross-process
        # search mapping comes from PRISM episode bodies returned by Graphiti.
        self._episodes: dict[str, GraphEpisode] = {}
        self._closed = False

    @property
    def group_id(self) -> str:
        return self._group_id

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

    @staticmethod
    def _method_accepts_kwarg(method: Any, name: str) -> bool:
        """Whether a client method accepts ``name`` as a keyword argument.

        The exact Graphiti client signature is a Phase B verification point,
        so the adapter negotiates the ``group_id`` keyword instead of assuming
        it: a client that does not accept it is simply called without it.
        When the signature cannot be introspected the adapter attempts the
        keyword and lets a real mismatch surface loudly.
        """
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return True
        for parameter in parameters.values():
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if parameter.name == name and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                return True
        return False

    @staticmethod
    def _result_attr(result: Any, name: str) -> Any:
        if isinstance(result, Mapping):
            return result.get(name)
        return getattr(result, name, None)

    def _known_episode(self, episode_key: str) -> GraphEpisode | None:
        cached = self._episodes.get(episode_key)
        if cached is not None:
            return cached
        if self._registry is not None:
            return self._registry.get(episode_key)
        return None

    def _result_group_mismatch(self, result: Any) -> bool:
        group_id = self._result_attr(result, "group_id")
        if group_id is None:
            return False
        return str(group_id) != self._group_id

    async def add_episode(self, episode: GraphEpisode) -> bool:
        if episode.episode_key in self._episodes:
            return False
        if self._registry is not None:
            known = self._registry.get(episode.episode_key)
            if known is not None:
                # Write-before existence lookup: a durable registry makes this
                # idempotent even after this process restarted.
                self._episodes[episode.episode_key] = known
                return False
        arguments: dict[str, Any] = {
            "name": episode.name,
            "episode_body": episode.episode_body,
            "source": self._json_episode_type(),
            "source_description": f"PRISM {episode.kind} episode",
            "reference_time": episode.reference_time,
            "uuid": episode.episode_key,
        }
        if self._method_accepts_kwarg(self._client.add_episode, "group_id"):
            arguments["group_id"] = self._group_id
        await self._client.add_episode(**arguments)
        self._episodes[episode.episode_key] = episode
        if self._registry is not None:
            self._registry.put(episode)
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        arguments: dict[str, Any] = {}
        if self._method_accepts_kwarg(self._client.search, "group_id"):
            arguments["group_id"] = self._group_id
        raw_results = await self._client.search(query, **arguments)
        found: dict[str, GraphEpisode] = {}
        for result in raw_results or ():
            if isinstance(result, GraphEpisode):
                found[result.episode_key] = result
                continue
            if self._result_group_mismatch(result):
                continue
            episode = self._mapped_episode(result)
            if episode is not None:
                found[episode.episode_key] = episode
        return tuple(found.values())

    def _mapped_episode(self, result: Any) -> GraphEpisode | None:
        """Map one search result to a PRISM episode, or None when unsure."""
        for episode_key in self._episode_references(result):
            episode = self._known_episode(episode_key)
            if episode is not None:
                return episode
        return _episode_from_body(self._result_attr(result, "episode_body"))

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

    async def close(self) -> None:
        """Close the underlying client exactly once, if it owns resources.

        Handles both ``async aclose()`` and sync ``close()`` clients; clients
        without either are left alone.  A backend created by the composition
        root is closed by :meth:`PrismRuntime.close`; an injected client the
        caller still owns is closed only when the caller asks this backend to
        close it.
        """
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._client, "aclose", None)
        if closer is None:
            closer = getattr(self._client, "close", None)
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result
