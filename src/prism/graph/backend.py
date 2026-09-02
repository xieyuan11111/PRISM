"""Graph backend protocol and the PRISM-only Graphiti adapter.

Phase A (Graphiti/Neo4j spike) design notes
-------------------------------------------
- Importing ``prism.graph`` never imports ``graphiti_core`` or ``neo4j`` and
  builds no client.  The real client is only ever constructed by an explicit
  factory path in the composition root after ``graphiti.enabled=true``.
- Every episode is written under an explicit ``group_id``.  On
  graphiti-core 0.29.3 with Neo4j a group IS a database: ``add_episode``
  clones the driver to ``database=group_id`` whenever an explicit group_id
  differs from the connected database, so PRISM's config requires
  ``group_id == database`` for enabled configs.  Neo4j Community serves a
  single built-in database (``neo4j`` on the PRISM-owned container), so live
  Phase B data lives in that one database/group; isolation between PRISM
  environments comes from dedicated instances (own Neo4j home, service, data
  volume and ports), never from two group names on one Community instance.
- The group filtering below is therefore a DEFENSIVE adapter contract for
  future multi-database/Enterprise servers (where each group is a real
  database) and for group-blind clients: it is NOT a Community live
  acceptance claim.
- graphiti-core 0.29.3 ``add_episode`` uuid semantics (live-probe verified):
  ``uuid=None`` (the default; PRISM omits the argument) CREATES a new episode
  under a Graphiti-assigned uuid, while an explicit uuid performs a
  ``get_by_uuid`` lookup first and raises ``NodeNotFoundError`` when nothing
  exists under it.  The adapter therefore never passes the PRISM
  ``episode_key`` as a uuid.  The PRISM key lives in the episode body
  (injected defensively when a caller's body lacks it), and the
  Graphiti-assigned uuid returned by the client is recorded BOTH in an
  in-process, auditable ``episode_key -> graphiti uuid`` cache AND - when a
  registry is injected - in the persistent registry - never treated as a
  PRISM key.  The live probe also showed the real 0.29.3
  ``add_episode`` return has NO top-level uuid: the created episode (whose
  uuid is a ``uuid.UUID``) is nested under an ``episodes`` collection, so
  the adapter negotiates the result's shape (top-level ``uuid``, a single
  ``episode`` carrier, or the first entry of an ``episodes`` collection)
  and records NOTHING when no uuid is extractable - never a fabricated
  value, never the PRISM key itself.
- Idempotency across process restarts is provided by an injected registry
  (write-before existence lookup by PRISM key), never by the in-process
  cache.  The composition root always injects the project-owned, SQLite-backed
  :class:`~prism.graph.registry.SQLiteEpisodeRegistry` on the real-Graphiti
  path (see runtime/composition.py), which persists the PRISM key, the real
  Graphiti-assigned uuid captured from the add result, the group/database the
  episode was written under and the canonical episode body.  A registry-less
  backend exists only for offline adapter tests and caller-injected custom
  registries: without a registry a restarted process cannot prevent a
  duplicate write (each write creates a fresh Graphiti uuid) and cannot
  attribute body-less search results - that is NOT the live Phase B path, and
  search still rebuilds PRISM episodes from stored bodies when it can, so
  timelines stay stable even then.  The ``_episodes`` dict below is a
  per-process local cache only and is NOT presented as persistent storage
  anywhere.
- ``search`` passes only group parameters the injected client actually
  declares: graphiti-core 0.29.3's ``search`` takes ``group_ids`` (a plural
  list), so the adapter negotiates ``group_ids``, then a singular
  ``group_id``, then calls with no group argument at all.  It maps ONLY
  episodes that can be positively attributed to PRISM: through this
  process's graphiti-uuid audit cache, through the persistent registry's
  group-scoped reverse lookup of a referenced uuid (the restart path), by
  registry knowledge of a referenced PRISM key, or through an episode body
  carrying PRISM's own schema marker.  Anything else is skipped, never
  guessed at.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

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
    the same code paths offline, and the project-owned
    :class:`~prism.graph.registry.SQLiteEpisodeRegistry` is the persistent
    implementation the composition root injects on the enabled path.
    """

    def get(self, episode_key: str) -> GraphEpisode | None:
        """Return the stored episode for ``episode_key``, or None."""
        ...

    def put(
        self,
        episode: GraphEpisode,
        *,
        group_id: str = "",
        graphiti_uuid: str | None = None,
    ) -> None:
        """Persist ``episode`` under its episode_key.

        ``group_id`` is the group/database boundary the episode was written
        under.  ``graphiti_uuid`` is the REAL Graphiti-assigned uuid the add
        returned (or None when the client result carried no usable uuid):
        a registry implementation must never treat the PRISM key as a
        Graphiti uuid, and the adapter never passes one.
        """
        ...

    def get_by_graphiti_uuid(
        self, graphiti_uuid: str, *, group_id: str = ""
    ) -> GraphEpisode | None:
        """Return the episode stored under the real Graphiti uuid, or None.

        Group-scoped: only knowledge recorded under ``group_id`` may be
        returned, so search attribution can never cross a group/database
        boundary even when a shared registry is consulted after a restart.
        """
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
    - on graphiti-core 0.29.3 with Neo4j that group is a database, so the
    adapter's group id always equals the connected database name (PRISM's
    config enforces ``group_id == database``).  First creation never passes a
    uuid: 0.29.3 treats ``uuid=None`` as "create" and an explicit uuid as a
    ``get_by_uuid`` lookup that raises ``NodeNotFoundError`` on a miss, so
    the PRISM ``episode_key`` travels in the episode body and the returned
    Graphiti uuid is captured by negotiating the add result's shape (the
    real 0.29.3 return nests it under ``episodes``; a shape without a
    usable uuid records nothing).  The captured uuid is cached in-process
    as audit knowledge AND persisted through the injected registry together
    with the PRISM key, group and canonical body - the composition root
    always injects the SQLite-backed registry on the live path, so a
    restarted process short-circuits duplicate writes and attributes
    body-less search results through it.  Search results are
    mapped back to PRISM payloads only when they can be positively
    attributed (graphiti-uuid audit cache, the persistent registry's
    group-scoped reverse lookup, registry knowledge of a referenced PRISM
    key, or a PRISM schema marker in the returned body), and
    results tagged with another group are skipped as a defensive contract
    for group-aware/multi-database servers - it is not a Community isolation
    mechanism (see the module notes).  Provenance and uncertainty are
    preserved without guessing at a live graph schema.
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
        # Auditable, per-process record of the Graphiti-assigned uuid of each
        # PRISM episode this process created.  Also NOT persistent, and never
        # a PRISM identity: it only lets this process attribute body-less
        # search results and lets operators audit which server uuid each
        # episode_key landed under.
        self._graphiti_uuids: dict[str, str] = {}
        self._episode_key_by_graphiti_uuid: dict[str, str] = {}
        self._closed = False

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def graphiti_uuids(self) -> Mapping[str, str]:
        """Read-only audit view: PRISM episode_key -> Graphiti-assigned uuid."""
        return dict(self._graphiti_uuids)

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

    @staticmethod
    def _usable_graphiti_uuid(value: Any) -> str | None:
        """Coerce a plausible server-assigned uuid to text, or None.

        Real graphiti-core nodes carry ``uuid.UUID`` instances and simpler
        injected clients return plain strings; anything else (None, numbers,
        arbitrary objects whose repr is not an identity) is not an uuid, and
        the adapter never fabricates one from it.
        """
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, str) and value:
            return value
        return None

    def _graphiti_uuid_from_result(self, result: Any) -> str | None:
        """Extract the created episode's Graphiti uuid from an add result.

        The live probe showed the real graphiti-core 0.29.3 ``add_episode``
        return carries no top-level uuid: the created episode (with its
        ``uuid.UUID``) is nested under an ``episodes`` collection.  The
        adapter therefore negotiates shapes instead of assuming one - a
        top-level ``uuid`` attribute/key, then a single ``episode`` carrier,
        then the first entry of an ``episodes`` collection (add_episode
        creates exactly one episode, so only that first entry is the
        created one) - and records nothing when none carries a usable uuid.
        """
        candidates: list[Any] = [self._result_attr(result, "uuid")]
        single = self._result_attr(result, "episode")
        if single is not None:
            candidates.append(self._result_attr(single, "uuid"))
        collection = self._result_attr(result, "episodes")
        first: Any = None
        if isinstance(collection, str):
            first = collection
        elif collection:
            try:
                first = next(iter(collection))
            except TypeError:
                first = None
        if first is not None:
            if isinstance(first, str):
                candidates.append(first)
            else:
                candidates.append(self._result_attr(first, "uuid"))
        for candidate in candidates:
            usable = self._usable_graphiti_uuid(candidate)
            if usable is not None:
                return usable
        return None

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

    def _write_body(self, episode: GraphEpisode) -> str:
        """The body to store, guaranteed to carry the PRISM episode_key.

        The episode_key in the body is the only durable link between a stored
        Graphiti episode and PRISM (the Graphiti uuid is assigned by the
        server and never doubles as a PRISM key), so a JSON body lacking the
        key gets it injected before the write.  Non-JSON bodies pass through
        unchanged; they simply cannot be rebuilt by ``search`` later.
        """
        try:
            payload = json.loads(episode.episode_body)
        except (TypeError, ValueError):
            return episode.episode_body
        if not isinstance(payload, Mapping) or "episode_key" in payload:
            return episode.episode_body
        payload["episode_key"] = episode.episode_key
        return canonical_json(payload)

    def _record_graphiti_uuid(self, episode_key: str, graphiti_uuid: str) -> None:
        """Cache the server-assigned uuid as audit knowledge, nothing more."""
        self._graphiti_uuids[episode_key] = graphiti_uuid
        self._episode_key_by_graphiti_uuid[graphiti_uuid] = episode_key

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
            "episode_body": self._write_body(episode),
            "source": self._json_episode_type(),
            "source_description": f"PRISM {episode.kind} episode",
            "reference_time": episode.reference_time,
        }
        if self._method_accepts_kwarg(self._client.add_episode, "group_id"):
            arguments["group_id"] = self._group_id
        # Deliberately no uuid argument: graphiti-core 0.29.3 treats uuid=None
        # as "create a new episode", while an explicit uuid performs a
        # get_by_uuid lookup that raises NodeNotFoundError for an episode that
        # does not exist yet - so passing the PRISM episode_key here would
        # break every first write.
        result = await self._client.add_episode(**arguments)
        self._episodes[episode.episode_key] = episode
        graphiti_uuid = self._graphiti_uuid_from_result(result)
        # A Graphiti uuid is server-assigned and never the PRISM key: a
        # client echoing the key back must not launder it into the cache or
        # the persistent registry (the row then records no uuid at all).
        if graphiti_uuid is not None and graphiti_uuid != episode.episode_key:
            self._record_graphiti_uuid(episode.episode_key, graphiti_uuid)
        else:
            graphiti_uuid = None
        if self._registry is not None:
            # The persistent registry records the episode under its PRISM
            # key together with the REAL Graphiti uuid the write returned
            # (None when no usable uuid was extractable - never a fabricated
            # value, never the PRISM key itself) and the group/database the
            # write landed under.  That row is what makes a restarted
            # process idempotent and lets it attribute body-less search
            # results.
            self._registry.put(
                episode,
                group_id=self._group_id,
                graphiti_uuid=graphiti_uuid,
            )
        return True

    def _search_group_arguments(self) -> dict[str, Any]:
        """Group scoping the injected client's ``search`` actually declares.

        graphiti-core 0.29.3 declares ``group_ids`` (a plural list), not a
        singular ``group_id``; alternate clients may declare either.  Only a
        parameter the client really declares is ever passed, and attribution
        never relies on it (episode bodies, the PRISM schema marker and group
        metadata filter defensively regardless).
        """
        method = self._client.search
        if self._method_accepts_kwarg(method, "group_ids"):
            return {"group_ids": [self._group_id]}
        if self._method_accepts_kwarg(method, "group_id"):
            return {"group_id": self._group_id}
        return {}

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        raw_results = await self._client.search(
            query, **self._search_group_arguments()
        )
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
        for reference in self._episode_references(result):
            episode = self._episode_for_reference(reference)
            if episode is not None:
                return episode
        return _episode_from_body(self._result_attr(result, "episode_body"))

    def _episode_for_reference(self, reference: str) -> GraphEpisode | None:
        """Map one Graphiti uuid/PRISM-key reference to a known episode.

        A reference is normally the Graphiti-assigned uuid.  Resolution
        order: this process's audit cache, then the persistent registry's
        group-scoped reverse lookup (a restarted process has no in-process
        cache, so the durable row is what attributes the uuid), then the raw
        reference as a PRISM episode key (covers stores that key episodes by
        deterministic keys instead of server-assigned ones).
        """
        episode_key = self._episode_key_by_graphiti_uuid.get(reference)
        if episode_key is not None and episode_key != reference:
            episode = self._known_episode(episode_key)
            if episode is not None:
                return episode
        if self._registry is not None:
            episode = self._registry.get_by_graphiti_uuid(
                reference, group_id=self._group_id
            )
            if episode is not None:
                return episode
        return self._known_episode(reference)

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
