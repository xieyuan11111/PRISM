"""Lazy Graphiti/Neo4j client construction for the opt-in spike path.

Importing this module never imports ``graphiti_core`` or ``neo4j``: every
Graphiti/Neo4j import happens inside the functions below, which the
composition root calls only after ``graphiti.enabled=true`` and only when the
optional dependencies are installed (or a factory was injected).  The offline
default runtime never reaches this module's import sites.
"""

from __future__ import annotations

import os
from typing import Any

from prism.config import GraphitiConfig

#: Neo4j's standard initial administrative user.  The Phase A deployment
#: template creates its own container with this same user, and PRISM's config
#: defaults to it whenever ``username_env`` is left empty.
NEO4J_DEFAULT_USER = "neo4j"

#: Environment variable name that must hold the password whenever the real
#: client is built.  It is a NAME here; the value is only ever read from the
#: process environment at connection time.
DEFAULT_PASSWORD_ENV = "PRISM_GRAPHITI_PASSWORD"

#: Graphiti's default search window is too small for a timeline, where one
#: case expands into a case episode plus facts, relations, nodes and materials.
#: graphiti-core 0.29.3 ``search`` defaults ``num_results`` to 10 (its
#: ``DEFAULT_SEARCH_LIMIT``) over ALL entity edges in the group, so with other
#: cases' edges in the same Community database a 10-result window can silently
#: truncate one case's episodes.  Keep the compatibility policy beside
#: real-client construction: the backend still negotiates group scoping, while
#: this adapter makes its otherwise implicit Graphiti search window explicit
#: and large enough for the live boundary.  This is a bounded spike safeguard,
#: not pagination: a case (or cumulative group) beyond 100 entity edges still
#: needs real pagination.
GRAPHITI_SEARCH_NUM_RESULTS = 100


class _GraphitiSearchWindow:
    """Delegate a Graphiti client while widening omitted search windows."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def search(
        self,
        query: str,
        center_node_uuid: str | None = None,
        group_ids: list[str] | None = None,
        num_results: int | None = None,
        search_filter: Any | None = None,
        driver: Any | None = None,
    ) -> Any:
        """Use an explicit timeline-sized window unless a caller supplies one."""
        return await self._client.search(
            query,
            center_node_uuid=center_node_uuid,
            group_ids=group_ids,
            num_results=(
                GRAPHITI_SEARCH_NUM_RESULTS
                if num_results is None
                else num_results
            ),
            search_filter=search_filter,
            driver=driver,
        )


def _resolve_credentials(config: GraphitiConfig) -> tuple[str, str]:
    """Resolve credentials from the configured environment variable names.

    Called before any Graphiti/Neo4j import so a missing credential fails
    fast and clearly, without ever touching the optional dependencies.
    """
    if config.username_env:
        username = os.environ.get(config.username_env, "")
        if not username:
            raise RuntimeError(
                f"graphiti.enabled requires environment variable "
                f"{config.username_env!r} to be set (the config only stores the "
                "variable name, never the value)"
            )
    else:
        username = NEO4J_DEFAULT_USER
    password = os.environ.get(config.password_env, "")
    if not password:
        raise RuntimeError(
            f"graphiti.enabled requires environment variable "
            f"{config.password_env!r} to be set (the config only stores the "
            "variable name, never the value)"
        )
    return username, password


def resolve_episode_type_json() -> Any:
    """Return graphiti's ``EpisodeType.json`` when importable.

    Falls back to the plain string ``"json"`` only when graphiti-core is not
    installed, which happens exclusively on the injected-fake-factory path;
    the real client path always runs with graphiti-core present.
    """
    try:
        # Deliberately lazy: importing prism.graph never imports Graphiti.
        from graphiti_core.nodes import EpisodeType  # type: ignore[import-not-found]
    except ImportError:
        return "json"
    return EpisodeType.json


def build_graphiti_client(
    config: GraphitiConfig,
    *,
    llm_client: Any | None = None,
    embedder: Any | None = None,
    cross_encoder: Any | None = None,
) -> Any:
    """Build the real Graphiti client for a live Phase B spike run.

    Graphiti 0.29.3 accepts ``uri``, ``user`` and ``password``; neither that
    constructor nor this function consumes ``config.database``.  The database
    is selected later by graphiti itself: ``add_episode`` treats an explicit
    ``group_id`` as the Neo4j database and clones the driver to
    ``database=group_id`` whenever it differs from the connected database.
    That is why ``GraphitiConfig`` requires ``database == group_id`` for
    every enabled config: on the PRISM-owned Community container both are the
    single built-in database ``neo4j``, and no other group name could resolve
    to a database the single-database edition serves.  Credential resolution
    happens first so missing env vars fail before any import.  The keyword-only
    client hooks let an explicit ``graphiti_client_factory`` replace Graphiti's
    provider defaults while leaving normal production construction unchanged.
    """
    username, password = _resolve_credentials(config)
    uri = config.effective_uri
    try:
        from graphiti_core import Graphiti  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "graphiti-core is not installed; install the optional extra with "
            "'pip install -e \".[graphiti]\"' or inject graphiti_client_factory"
        ) from error

    try:
        # PHASE B VERIFY result (live spike, graphiti-core 0.29.3): Graphiti
        # construction performs no eager DATABASE I/O (the Neo4j driver is
        # lazy; the first query happens on add_episode/search) and the keyword
        # surface below matches 0.29.3.  The one eager network behavior is
        # Graphiti's own anonymous-usage telemetry event fired during
        # construction when its telemetry is enabled (its default outside
        # pytest).  PRISM opts out by default so a PRISM process never phones
        # home on the operator's behalf; an operator who explicitly exports
        # GRAPHITI_TELEMETRY_ENABLED=true keeps telemetry on.
        os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
        client_kwargs = {
            "uri": uri,
            "user": username,
            "password": password,
        }
        if llm_client is not None:
            client_kwargs["llm_client"] = llm_client
        if embedder is not None:
            client_kwargs["embedder"] = embedder
        if cross_encoder is not None:
            client_kwargs["cross_encoder"] = cross_encoder
        client = Graphiti(**client_kwargs)
        # Test doubles used by the offline constructor-contract tests may only
        # model construction.  A real Graphiti client always has search and is
        # adapted so GraphitiBackend cannot silently inherit a 10-result
        # provider default for a multi-episode timeline.
        if callable(getattr(client, "search", None)):
            return _GraphitiSearchWindow(client)
        return client
    except TypeError as error:
        raise RuntimeError(
            "could not construct the Graphiti client with the installed "
            "graphiti-core API; expected the 0.29.3-compatible "
            "Graphiti(uri=..., user=..., password=...) signature "
            f"for uri {uri!r}"
        ) from error
