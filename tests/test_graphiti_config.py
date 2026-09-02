"""Offline contract tests for the opt-in Graphiti/Neo4j configuration.

Every Graphiti connection is explicitly opt-in (``enabled`` defaults to
false).  The config stores environment-variable NAMES (never values), rejects
URIs that embed credentials, and keeps old config files loadable.
"""

from __future__ import annotations

import json

import pytest

from prism.config import GraphitiConfig, PrismConfig


def test_graphiti_is_disabled_by_default():
    config = PrismConfig()

    assert config.graphiti.enabled is False
    assert config.graphiti.uri == ""
    assert config.graphiti.database == ""
    assert config.graphiti.group_id == ""
    assert config.graphiti.username_env == ""
    assert config.graphiti.password_env == "PRISM_GRAPHITI_PASSWORD"
    assert config.graphiti.timeout == 30.0


def test_graphiti_config_accepts_plain_bolt_and_http_uris():
    bolt = GraphitiConfig(uri="bolt://prism-graphiti-spike:7688")
    http = GraphitiConfig(uri="http://127.0.0.1:7475")

    assert bolt.uri == "bolt://prism-graphiti-spike:7688"
    assert http.uri == "http://127.0.0.1:7475"


def test_portless_uri_stays_parseable_while_disabled_but_never_connectable():
    # A disabled config may stay parseable (it never builds a client), but the
    # connectable URI must never be synthesized with a default port (7687/7474)
    # that could silently reach a default local Neo4j instance.
    bolt = GraphitiConfig(uri="bolt://prism.local")
    http = GraphitiConfig(uri="http://prism.local")
    assert bolt.uri == "bolt://prism.local"
    assert http.uri == "http://prism.local"
    with pytest.raises(ValueError, match="port"):
        _ = bolt.effective_uri
    with pytest.raises(ValueError, match="port"):
        _ = http.effective_uri


def test_effective_uri_never_applies_a_default_port_for_connection():
    # Connections always state an explicit non-default port: neither a missing
    # port nor the standard listener port numbers 7474/7687 may ever be used
    # to connect, under ANY scheme (bolt://host:7474 and http://host:7687 are
    # cross-scheme mistakes that still touch a default local Neo4j's ports).
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="bolt://prism.local").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="http://prism.local:7474").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="bolt+s://prism.local:7687").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="bolt://prism.local:7474").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="http://prism.local:7687").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="https://prism.local:7687").effective_uri
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri="neo4j+s://prism.local:7474").effective_uri
    assert GraphitiConfig(uri="http://prism.local:7475").effective_uri == (
        "http://prism.local:7475"
    )
    assert GraphitiConfig(uri="bolt://prism.local:7688").effective_uri == (
        "bolt://prism.local:7688"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://neo4j:secret@prism.local:7688",
        "bolt://user@prism.local",
        "http://alice@127.0.0.1:7475",
        "bolt://@prism.local",
    ],
)
def test_uri_rejects_embedded_credentials(uri):
    with pytest.raises(ValueError, match="credential"):
        GraphitiConfig(uri=uri)


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://",
        "bolt://:7688",
        "http://",
        "http://:7475",
    ],
)
def test_uri_rejects_empty_host(uri):
    with pytest.raises(ValueError, match="host"):
        GraphitiConfig(uri=uri)


@pytest.mark.parametrize("uri", ["ftp://prism.local", "mongodb://prism.local", "prism.local:7688"])
def test_uri_rejects_unknown_scheme_or_missing_scheme(uri):
    with pytest.raises(ValueError, match="scheme"):
        GraphitiConfig(uri=uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://prism.local",
    ],
)
def test_uri_requires_explicit_port_when_no_documented_default_exists(uri):
    # https has no documented default port, so a portless https URI is refused
    # at parse time; connection URIs always state an explicit port.
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri=uri)


def test_neo4j_family_and_https_with_explicit_ports_are_accepted():
    assert (
        GraphitiConfig(uri="neo4j+s://prism.local:7688").effective_uri
        == "neo4j+s://prism.local:7688"
    )
    assert GraphitiConfig(uri="https://prism.local:7473").effective_uri == (
        "https://prism.local:7473"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://prism.local",
        "bolt+s://prism.local",
        "neo4j://prism.local",
        "http://prism.local",
        "https://prism.local",
    ],
)
def test_enabled_path_rejects_uri_without_an_explicit_port(uri):
    # Isolation: an enabled config must never fall back to the standard local
    # Neo4j ports 7687/7474, so a portless URI is refused before any client
    # could be built against a default local instance.
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(enabled=True, uri=uri, database="neo4j", group_id="neo4j")


def test_disabled_config_parses_standard_ports_but_never_connects_to_them():
    # A disabled config only stores the URI, so 7474/7687 - even under a
    # mismatched scheme - still parse while disabled; every connection gate
    # (enabled=True and effective_uri) refuses both port numbers regardless
    # of scheme.
    bolt = GraphitiConfig(uri="bolt://prism.local:7474")
    http = GraphitiConfig(uri="http://prism.local:7687")
    assert bolt.uri == "bolt://prism.local:7474"
    assert http.uri == "http://prism.local:7687"
    with pytest.raises(ValueError, match="port"):
        _ = bolt.effective_uri
    with pytest.raises(ValueError, match="port"):
        _ = http.effective_uri


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://prism.local:7687",
        "bolt+s://prism.local:7687",
        "neo4j://prism.local:7687",
        "http://prism.local:7474",
    ],
)
def test_enabled_path_rejects_standard_default_ports(uri):
    # Even an explicit 7474/7687 targets a default local Neo4j, not the
    # PRISM-owned container; the enabled path must refuse it.
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(enabled=True, uri=uri, database="neo4j", group_id="neo4j")


@pytest.mark.parametrize(
    "uri",
    [
        # Cross-scheme mistakes: the standard listener port NUMBERS 7474/7687
        # are refused under every scheme, not only the scheme they natively
        # belong to - a default local Neo4j owns both port numbers.
        "bolt://prism.local:7474",
        "bolt+s://prism.local:7474",
        "neo4j://prism.local:7474",
        "http://prism.local:7687",
        "https://prism.local:7474",
        "https://prism.local:7687",
        "bolt+ssc://prism.local:7687",
    ],
)
def test_enabled_path_rejects_standard_ports_under_any_scheme(uri):
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(enabled=True, uri=uri, database="neo4j", group_id="neo4j")


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://prism.local:7688",
        "bolt+s://prism.local:7688",
        "neo4j://prism.local:7688",
        "http://prism.local:7475",
    ],
)
def test_enabled_path_accepts_explicit_non_default_ports(uri):
    config = GraphitiConfig(enabled=True, uri=uri, database="neo4j", group_id="neo4j")
    assert config.effective_uri == uri


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://prism.local:0",
        "bolt://prism.local:70000",
        "bolt://prism.local:abc",
        "http://prism.local:999999999999999",
    ],
)
def test_uri_rejects_out_of_range_or_non_numeric_ports(uri):
    with pytest.raises(ValueError, match="port"):
        GraphitiConfig(uri=uri)


@pytest.mark.parametrize(
    "uri",
    [
        "bolt://prism.local/db",
        "bolt://prism.local:7688?ssl=false",
        "http://prism.local:7475/#fragment",
    ],
)
def test_uri_rejects_paths_queries_and_fragments(uri):
    with pytest.raises(ValueError, match="database"):
        GraphitiConfig(uri=uri)


def test_enabled_requires_uri_database_and_equal_group_id():
    with pytest.raises(ValueError, match="uri"):
        GraphitiConfig(enabled=True, database="neo4j", group_id="neo4j")
    with pytest.raises(ValueError, match="database"):
        GraphitiConfig(enabled=True, uri="bolt://prism.local:7688", group_id="neo4j")
    with pytest.raises(ValueError, match="group_id"):
        GraphitiConfig(enabled=True, uri="bolt://prism.local:7688", database="neo4j")
    GraphitiConfig(
        enabled=True,
        uri="bolt://prism.local:7688",
        database="neo4j",
        group_id="neo4j",
    )


def test_enabled_rejects_group_id_differing_from_database():
    # graphiti-core 0.29.3 realises a Neo4j group as a database: add_episode
    # clones the driver to database=group_id whenever an explicit group_id
    # differs from the connected database, and Neo4j Community serves only its
    # single built-in database.  An enabled config with database="neo4j" and a
    # different group id (the pre-fix "prism-spike" example) would target a
    # database the edition does not serve, so it is rejected offline before
    # any client could be built.
    with pytest.raises(ValueError, match="group_id == graphiti.database"):
        GraphitiConfig(
            enabled=True,
            uri="bolt://prism.local:7688",
            database="neo4j",
            group_id="prism-spike",
        )
    with pytest.raises(ValueError, match="group_id == graphiti.database"):
        GraphitiConfig(
            enabled=True,
            uri="bolt://prism.local:7688",
            database="prism-spike",
            group_id="neo4j",
        )
    # Equal values are accepted; the concrete name is the server's to serve
    # (the PRISM-owned Community container serves the built-in "neo4j").
    config = GraphitiConfig(
        enabled=True,
        uri="bolt://prism.local:7688",
        database="neo4j",
        group_id="neo4j",
    )
    assert config.database == config.group_id == "neo4j"


def test_config_fields_validate_types_and_shapes():
    with pytest.raises(TypeError, match="enabled"):
        GraphitiConfig(enabled=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="password_env"):
        GraphitiConfig(password_env="")
    with pytest.raises(ValueError, match="password_env"):
        GraphitiConfig(password_env="   ")
    with pytest.raises(TypeError, match="timeout"):
        GraphitiConfig(timeout=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout"):
        GraphitiConfig(timeout=0)
    with pytest.raises(ValueError, match="database"):
        GraphitiConfig(database=" ")
    with pytest.raises(ValueError, match="database"):
        GraphitiConfig(database="prism spike")


def test_env_fields_name_environment_variables_never_values():
    config = GraphitiConfig(
        enabled=True,
        uri="bolt://prism.local:7688",
        database="neo4j",
        group_id="neo4j",
        username_env="PRISM_GRAPHITI_USERNAME",
        password_env="PRISM_GRAPHITI_PASSWORD",
    )

    # The config carries the variable NAMES; it has no field that could hold a
    # secret value at all (frozen, slots), so serialization cannot leak one.
    assert config.username_env == "PRISM_GRAPHITI_USERNAME"
    assert config.password_env == "PRISM_GRAPHITI_PASSWORD"
    assert not hasattr(config, "password")
    assert not hasattr(config, "username")


def test_graphiti_round_trips_through_portable_json(tmp_path):
    config = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri="bolt://prism-graphiti-spike:7688",
            database="neo4j",
            group_id="neo4j",
            username_env="PRISM_GRAPHITI_USERNAME",
            password_env="PRISM_GRAPHITI_PASSWORD",
            timeout=7.5,
        )
    )
    path = tmp_path / "config.json"

    config.save(path)

    loaded = PrismConfig.load(path)
    assert loaded == config
    assert loaded.graphiti.database == "neo4j"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["graphiti"]["enabled"] is True
    assert raw["graphiti"]["password_env"] == "PRISM_GRAPHITI_PASSWORD"
    assert "PRISM_GRAPHITI_PASSWORD" in path.read_text(encoding="utf-8")


def test_old_config_without_graphiti_section_stays_compatible(tmp_path):
    legacy = {
        "paths": {"data_dir": "data"},
        "sources": {"whitelist": ["example.gov"]},
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = PrismConfig.load(path)

    assert loaded.graphiti == GraphitiConfig()
    assert loaded.graphiti.enabled is False
    assert loaded.sources.whitelist == ("example.gov",)


def test_config_rejects_unknown_graphiti_keys():
    data = PrismConfig().to_dict()
    data["graphiti"] = {"enabled": False, "stray": 1}

    with pytest.raises(ValueError, match="stray"):
        PrismConfig.from_dict(data)


def test_graphiti_to_dict_never_contains_password_values():
    data = PrismConfig(
        graphiti=GraphitiConfig(
            enabled=True,
            uri="bolt://prism.local:7688",
            database="neo4j",
            group_id="neo4j",
            username_env="PRISM_GRAPHITI_USERNAME",
            password_env="PRISM_GRAPHITI_PASSWORD",
        )
    ).to_dict()

    serialized = json.dumps(data)
    assert "super-secret-password" not in serialized
    assert data["graphiti"]["password_env"] == "PRISM_GRAPHITI_PASSWORD"
