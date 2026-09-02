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


def build_graphiti_client(config: GraphitiConfig) -> Any:
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
    happens first so missing env vars fail before any import.
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
        # PHASE B VERIFY: confirm whether construction performs eager network
        # I/O.  The keyword surface itself matches graphiti-core 0.29.3.
        return Graphiti(uri=uri, user=username, password=password)
    except TypeError as error:
        raise RuntimeError(
            "could not construct the Graphiti client with the installed "
            "graphiti-core API; expected the 0.29.3-compatible "
            "Graphiti(uri=..., user=..., password=...) signature "
            f"for uri {uri!r}"
        ) from error
