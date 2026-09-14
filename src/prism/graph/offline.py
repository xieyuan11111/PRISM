"""Persistent offline graph backend for the default (no-Graphiti) runtime.

The default ``create_runtime`` composition (``graphiti.enabled == false``
and no injected ``graph_backend``) needs graph episodes to survive across
CLI processes: ``prism process`` writes a case bundle, then NEW processes
serve ``timeline``/``state``/``snapshot``/``compare``/``report`` from it.
:class:`SQLiteOfflineGraphBackend` provides that durability by reusing the
project-owned :class:`~prism.graph.registry.SQLiteEpisodeRegistry` — the
same mature SQLite serialization the live Graphiti path already depends on
— under dedicated isolation labels:

.. code-block:: text

    database = "offline"
    group_id = "offline"

so offline episodes live in the shared local database file but can never
read or pollute rows recorded under a live Graphiti group/database (and
vice versa).  This backend is deliberately NOT a Graphiti backend and never
pretends to be one: it performs no LLM/LLM-derived entity extraction, no
network I/O and no client construction, and callers can distinguish it by
type and by the registry's recorded ``database`` label.  The process-local
``OfflineGraphBackend`` in ``prism.runtime`` remains only for unit tests
and explicit injection; it is never the default anymore.
"""

from __future__ import annotations

from .models import GraphEpisode
from .registry import SQLiteEpisodeRegistry

#: Group label every offline episode is written under (write AND read scope).
OFFLINE_GROUP_ID = "offline"

#: Database label recorded on every offline registry row.
OFFLINE_DATABASE = "offline"

_REQUIRED_REGISTRY_METHODS = ("get", "put", "list_episodes")


class SQLiteOfflineGraphBackend:
    """Durable :class:`~prism.graph.backend.GraphBackend` for offline runs.

    Writes are idempotent by PRISM ``episode_key`` across process restarts
    (group-scoped existence lookup before the insert, backed by the
    persistent registry: idempotency holds within the ``offline`` group,
    while an identical key recorded under a foreign group never suppresses
    an offline write — the registry's composite ``(episode_key, group_id)``
    key keeps both rows), and reads return exactly the episodes recorded
    under the ``offline`` group, stably ordered by the registry's
    :meth:`~prism.graph.registry.SQLiteEpisodeRegistry.list_episodes`
    contract.  The injected registry is owned by the composition root,
    which closes it on runtime shutdown.
    """

    def __init__(self, registry: SQLiteEpisodeRegistry) -> None:
        for method in _REQUIRED_REGISTRY_METHODS:
            if not callable(getattr(registry, method, None)):
                raise TypeError(
                    f"registry must provide a callable {method}() "
                    "(expected SQLiteEpisodeRegistry)"
                )
        self._registry = registry

    @property
    def registry(self) -> SQLiteEpisodeRegistry:
        """The persistent registry this backend reads and writes."""
        return self._registry

    @property
    def group_id(self) -> str:
        """The group label every episode is written and read under."""
        return OFFLINE_GROUP_ID

    async def add_episode(self, episode: GraphEpisode) -> bool:
        """Store ``episode`` durably; return False when its key already exists.

        The existence lookup is group-scoped to :data:`OFFLINE_GROUP_ID`: a
        live Graphiti group sharing the same SQLite file may hold the very
        same deterministic ``episode_key``, and that foreign row must never
        make the offline write skip (nor vice versa on the Graphiti side).
        """
        if (
            self._registry.get(episode.episode_key, group_id=OFFLINE_GROUP_ID)
            is not None
        ):
            return False
        self._registry.put(episode, group_id=OFFLINE_GROUP_ID)
        return True

    async def search(self, query: str) -> tuple[GraphEpisode, ...]:
        # GraphService owns filtering and temporal evaluation; this read is
        # group-scoped so foreign rows (e.g. a live Graphiti group sharing
        # the same database file) can never leak into offline results.
        return self._registry.list_episodes(group_id=OFFLINE_GROUP_ID)


__all__ = ["OFFLINE_DATABASE", "OFFLINE_GROUP_ID", "SQLiteOfflineGraphBackend"]
