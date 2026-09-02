"""Offline contract tests for the lazy real Graphiti client builder.

graphiti-core 0.29.3's ``Graphiti`` constructor surface is
``Graphiti(uri=..., user=..., password=...)``: it accepts no ``neo4j_*``
keywords and does not consume PRISM's ``config.database`` metadata.  These
tests pin that contract offline with a fake ``graphiti_core`` module, and
pin the ordering guarantees: credentials resolve before any graphiti-core
import, missing dependencies fail with the explicit install hint, and the
TypeError wrap never echoes the password.
"""

from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest

from prism.config import GraphitiConfig
from prism.graph.graphiti_client import build_graphiti_client

URI = "bolt://prism-graphiti-spike:7688"
USERNAME = "prism-user"
PASSWORD = "test-only-password"
USERNAME_ENV = "PRISM_GRAPHITI_USERNAME"
PASSWORD_ENV = "PRISM_GRAPHITI_PASSWORD"


def config(**overrides: object) -> GraphitiConfig:
    values: dict[str, object] = {
        "uri": URI,
        "password_env": PASSWORD_ENV,
    }
    values.update(overrides)
    return GraphitiConfig(**values)  # type: ignore[arg-type]


def install_fake_graphiti(monkeypatch, graphiti: object) -> None:
    """Serve a fake ``graphiti_core`` module whose ``Graphiti`` is ``graphiti``."""
    graphiti_core = ModuleType("graphiti_core")
    graphiti_core.Graphiti = graphiti  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "graphiti_core", graphiti_core)


def test_build_graphiti_client_uses_graphiti_0293_constructor_keywords(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, USERNAME)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    calls: list[dict[str, object]] = []
    client = object()

    def fake_graphiti(**kwargs):
        calls.append(kwargs)
        return client

    install_fake_graphiti(monkeypatch, fake_graphiti)

    assert (
        build_graphiti_client(
            config(database="neo4j", username_env=USERNAME_ENV)
        )
        is client
    )
    assert calls == [
        {"uri": URI, "user": USERNAME, "password": PASSWORD}
    ]


def test_build_graphiti_client_never_forwards_database_or_neo4j_kwargs(monkeypatch):
    """Even a non-default PRISM database must stay metadata: 0.29.3 accepts
    no database argument, and no ``neo4j_*`` keyword may reach it."""
    monkeypatch.setenv(USERNAME_ENV, USERNAME)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    calls: list[dict[str, object]] = []

    def fake_graphiti(**kwargs):
        calls.append(kwargs)
        return object()

    install_fake_graphiti(monkeypatch, fake_graphiti)

    build_graphiti_client(config(database="prism-metadata-db"))

    assert len(calls) == 1
    assert list(calls[0]) == ["uri", "user", "password"]
    assert "database" not in calls[0]
    assert not any(key.startswith("neo4j_") for key in calls[0])


def test_build_graphiti_client_typeerror_wrap_does_not_leak_password(monkeypatch):
    monkeypatch.setenv(USERNAME_ENV, USERNAME)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)

    def incompatible_graphiti(**kwargs):
        raise TypeError(
            "Graphiti.__init__() got an unexpected keyword argument "
            "'neo4j_database'"
        )

    install_fake_graphiti(monkeypatch, incompatible_graphiti)

    with pytest.raises(RuntimeError) as excinfo:
        build_graphiti_client(config(database="neo4j"))

    message = str(excinfo.value)
    assert PASSWORD not in message
    assert URI in message
    assert "0.29.3" in message
    assert "uri" in message and "user" in message and "password" in message
    # The original TypeError stays reachable as the cause for diagnosis.
    assert isinstance(excinfo.value.__cause__, TypeError)


def test_build_graphiti_client_missing_password_fails_before_any_import(monkeypatch):
    """Credentials resolve before the graphiti-core import site: a missing
    password must fail without ever importing the optional dependency."""
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("graphiti_core"):
            raise AssertionError("graphiti-core was imported before env checks")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError) as excinfo:
        build_graphiti_client(config())

    message = str(excinfo.value)
    # The error names the environment variable, never a stored credential.
    assert PASSWORD_ENV in message


def test_build_graphiti_client_missing_dependency_fails_with_explicit_hint(
    monkeypatch,
):
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "graphiti_core" or name.startswith("graphiti_core."):
            raise ImportError("No module named 'graphiti_core'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError) as excinfo:
        build_graphiti_client(config())

    message = str(excinfo.value)
    assert "graphiti-core is not installed" in message
    assert ".[graphiti]" in message
    assert "graphiti_client_factory" in message
    assert PASSWORD not in message
    assert isinstance(excinfo.value.__cause__, ImportError)
