"""Dependency-free PRISM configuration and JSON serialization."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


_DOMAIN_PATTERN = re.compile(
    r"(?=^.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

# Phase A Graphiti/Neo4j URI policy.  Neo4j's standard listener ports are
# 7474 (HTTP) and 7687 (Bolt and the neo4j routing family) - precisely the
# ports a default local Neo4j occupies.  PRISM NEVER applies a default port
# when connecting, and every connection path (an ``enabled`` config and
# ``effective_uri``) refuses EITHER standard port NUMBER under ANY scheme:
# bolt://host:7474 and http://host:7687 are cross-scheme mistakes that would
# still touch the port numbers a default local instance owns, so they are
# rejected exactly like their scheme-native forms.  A portless or
# standard-port URI can therefore never silently reach a pre-existing
# default local instance instead of the PRISM-owned container.  A scheme
# with a documented standard port may omit the port while disabled (its URI
# is only stored, never connected).  https has NO documented default here on
# purpose: Neo4j's own https listener default (7473) is outside the two
# reserved values, so an https URI must state an explicit port rather than
# receive a guessed one.  Verify during Phase B before widening this set.
_GRAPHITI_SCHEMES = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "http", "https", "neo4j", "neo4j+s", "neo4j+ssc"}
)
# Schemes whose standard Neo4j listener port is documented for Phase A (bolt
# family: 7687, http: 7474).  Only membership matters: a portless URI of
# such a scheme may be stored while disabled - it is never connectable.
# https is absent because 7473 is not a documented Phase A default, so an
# https URI must state its port even at parse time.
_GRAPHITI_DOCUMENTED_DEFAULT_SCHEMES = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc", "http"}
)
# Standard listener port numbers of a default local Neo4j: 7474 (HTTP) and
# 7687 (Bolt and the neo4j routing family).  Every connection path refuses
# these numbers under ANY scheme, because a default local instance owns both
# port numbers no matter which listener natively uses them.  Do not widen:
# 7473 (Neo4j's https listener) is deliberately outside Phase A's reserved
# set.
_GRAPHITI_RESERVED_PORTS = frozenset({7474, 7687})


def _graphiti_uri_parts(uri: str) -> tuple[str, str, int | None]:
    """Validate a Graphiti URI and return ``(scheme, host, port)``.

    Credentials embedded in the URI are rejected outright; PRISM reads them
    from environment variables named by config instead.  Host, port, path and
    scheme rules are enforced here so a typo is caught at config load time,
    offline, before any service is touched.
    """
    try:
        parts = urlsplit(uri)
    except ValueError as error:
        raise ValueError(f"graphiti.uri is not parseable: {uri!r}") from error
    scheme = parts.scheme.lower()
    if not scheme:
        raise ValueError(
            f"graphiti.uri must include a scheme such as bolt:// or http://: {uri!r}"
        )
    if scheme not in _GRAPHITI_SCHEMES:
        allowed = ", ".join(sorted(_GRAPHITI_SCHEMES))
        raise ValueError(f"graphiti.uri scheme {scheme!r} is not supported; use one of: {allowed}")
    if parts.username not in (None, "") or parts.password not in (None, ""):
        raise ValueError(
            "graphiti.uri must not embed credentials; configure "
            "graphiti.username_env/graphiti.password_env instead"
        )
    if "@" in parts.netloc:
        raise ValueError(
            "graphiti.uri must not embed credentials; configure "
            "graphiti.username_env/graphiti.password_env instead"
        )
    host = parts.hostname
    if host is None or not host:
        raise ValueError(f"graphiti.uri host must not be empty: {uri!r}")
    if any(character.isspace() for character in host):
        raise ValueError(f"graphiti.uri host must not contain whitespace: {uri!r}")
    if parts.path not in ("", "/"):
        raise ValueError(
            "graphiti.uri must not include a path; put the database name in "
            "graphiti.database instead"
        )
    if parts.query or parts.fragment:
        raise ValueError(
            "graphiti.uri must not include a query or fragment; put the database "
            "name in graphiti.database instead"
        )
    port: int | None = None
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError(f"graphiti.uri has an invalid port: {uri!r}") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"graphiti.uri port must be between 1 and 65535: {uri!r}")
    if port is None and scheme not in _GRAPHITI_DOCUMENTED_DEFAULT_SCHEMES:
        raise ValueError(
            f"graphiti.uri scheme {scheme!r} has no Phase A default port; "
            "include an explicit port such as bolt://host:7688"
        )
    return scheme, host, port


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_mapping(name: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return value


def _reject_unknown(
    data: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown {context} keys: {', '.join(unknown)}")


@dataclass(frozen=True, slots=True)
class PathConfig:
    """Filesystem paths, relative to PRISM_HOME unless absolute."""

    data_dir: Path = Path("data")
    cache_dir: Path = Path("cache")
    output_dir: Path = Path("output")
    raw_dir: Path = Path("raw")
    corpus_dir: Path = Path("corpus")

    def __post_init__(self) -> None:
        for name in ("data_dir", "cache_dir", "output_dir", "raw_dir", "corpus_dir"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{name} must not be empty")
            if not isinstance(value, (str, os.PathLike)):
                raise TypeError(f"{name} must be path-like")
            path = Path(value).expanduser()
            object.__setattr__(self, name, path)

    @staticmethod
    def prism_home() -> Path:
        configured = os.environ.get("PRISM_HOME")
        if configured and configured.strip():
            return Path(configured).expanduser()
        return Path.home() / ".prism"

    def resolve(self, home: Path | None = None) -> PathConfig:
        base = (Path(home).expanduser() if home is not None else self.prism_home()).resolve()

        def resolve_one(path: Path) -> Path:
            return path.resolve() if path.is_absolute() else (base / path).resolve()

        data_dir = resolve_one(self.data_dir)
        # raw_dir/corpus_dir are storage siblings of the data directory:
        # relative values are anchored to the directory that contains data_dir
        # (PRISM_HOME itself when data_dir is relative).
        ingest_base = data_dir.parent

        def resolve_sibling(path: Path) -> Path:
            return path.resolve() if path.is_absolute() else (ingest_base / path).resolve()

        return PathConfig(
            data_dir=data_dir,
            cache_dir=resolve_one(self.cache_dir),
            output_dir=resolve_one(self.output_dir),
            raw_dir=resolve_sibling(self.raw_dir),
            corpus_dir=resolve_sibling(self.corpus_dir),
        )


@dataclass(frozen=True, slots=True)
class LLMProviderConfig:
    """A named provider's model and optional connection settings."""

    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: float = 30.0
    concurrency_limit: int = 4

    def __post_init__(self) -> None:
        _require_text("model", self.model)
        if self.api_key_env is not None:
            _require_text("api_key_env", self.api_key_env)
        if self.base_url is not None:
            _require_text("base_url", self.base_url)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        object.__setattr__(self, "timeout", float(self.timeout))
        if isinstance(self.concurrency_limit, bool) or not isinstance(self.concurrency_limit, int):
            raise TypeError("concurrency_limit must be an integer")
        if self.concurrency_limit <= 0:
            raise ValueError("concurrency_limit must be greater than zero")


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Provider definitions and task-role-to-provider mappings."""

    providers: Mapping[str, LLMProviderConfig] = field(default_factory=dict)
    task_roles: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        providers = dict(self.providers)
        task_roles = dict(self.task_roles)

        for name, provider in providers.items():
            _require_text("provider name", name)
            if not isinstance(provider, LLMProviderConfig):
                raise TypeError(f"provider {name!r} must be an LLMProviderConfig")

        for task, provider_name in task_roles.items():
            _require_text("task role", task)
            _require_text("provider name", provider_name)
            if provider_name not in providers:
                raise ValueError(
                    f"task role {task!r} references missing provider {provider_name!r}"
                )

        object.__setattr__(self, "providers", MappingProxyType(providers))
        object.__setattr__(self, "task_roles", MappingProxyType(task_roles))


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """The exact source domains PRISM is permitted to use."""

    whitelist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.whitelist, str):
            raise TypeError("whitelist must be an iterable of domains")

        normalized: list[str] = []
        seen: set[str] = set()
        for domain in self.whitelist:
            if not isinstance(domain, str):
                raise TypeError("source domain must be a string")
            domain = domain.strip().lower().rstrip(".")
            if not _DOMAIN_PATTERN.fullmatch(domain):
                raise ValueError(f"invalid source domain: {domain!r}")
            if domain not in seen:
                seen.add(domain)
                normalized.append(domain)

        object.__setattr__(self, "whitelist", tuple(normalized))

    def allows(self, domain: str) -> bool:
        return domain.strip().lower().rstrip(".") in self.whitelist


@dataclass(frozen=True, slots=True)
class FirecrawlConfig:
    """Explicit opt-in settings for the Firecrawl discovery backend."""

    enabled: bool = False
    api_key_env: str = "FIRECRAWL_API_KEY"
    base_url: str = "https://api.firecrawl.dev"
    limit: int = 10
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("firecrawl.enabled must be a bool")
        _require_text("firecrawl.api_key_env", self.api_key_env)
        _require_text("firecrawl.base_url", self.base_url)
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("firecrawl.limit must be an integer")
        if not 1 <= self.limit <= 100:
            raise ValueError("firecrawl.limit must be between 1 and 100")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("firecrawl.timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("firecrawl.timeout must be greater than zero")
        object.__setattr__(self, "timeout", float(self.timeout))


@dataclass(frozen=True, slots=True)
class GraphitiConfig:
    """Explicit opt-in connection settings for the PRISM Graphiti/Neo4j spike.

    The default configuration is fully offline: ``enabled`` defaults to false
    and no Graphiti/Neo4j dependency is imported or client built unless the
    runtime is explicitly asked to attempt it.  Credentials are never stored
    here: ``username_env``/``password_env`` name environment variables that the
    runtime resolves only at connection time.  An empty ``username_env`` means
    the Neo4j default administrative user ``neo4j`` (the deployment template's
    own container uses the same default).  An empty ``database`` means the
    server's default database; the Community template's PRISM-owned container
    serves Neo4j's single built-in database ``neo4j``, so a Phase B config
    should set ``database`` to ``neo4j`` (or leave it empty) unless a live run
    verifies the server's actual database capabilities.  ``enabled`` configs
    (and ``effective_uri``) require an explicit non-default port: the
    standard Neo4j listener port numbers 7474/7687 are refused under every
    scheme, so a portless or standard-port URI - ``bolt://host:7474`` and
    ``http://host:7687`` included - cannot silently reach a default local
    Neo4j instead of the PRISM-owned container.
    """

    enabled: bool = False
    uri: str = ""
    database: str = ""
    group_id: str = ""
    username_env: str = ""
    password_env: str = "PRISM_GRAPHITI_PASSWORD"
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("graphiti.enabled must be a bool")

        if not isinstance(self.uri, str):
            raise TypeError("graphiti.uri must be a string")
        uri = self.uri.strip()
        object.__setattr__(self, "uri", uri)

        if not isinstance(self.database, str):
            raise TypeError("graphiti.database must be a string")
        if self.database and any(character.isspace() for character in self.database):
            raise ValueError(
                "graphiti.database must be a bare database name without whitespace"
            )

        if not isinstance(self.group_id, str):
            raise TypeError("graphiti.group_id must be a string")
        group_id = self.group_id.strip()
        object.__setattr__(self, "group_id", group_id)

        if self.username_env is None:
            raise ValueError("graphiti.username_env must be a string")
        if not isinstance(self.username_env, str):
            raise TypeError("graphiti.username_env must be a string")
        object.__setattr__(self, "username_env", self.username_env.strip())

        if not isinstance(self.password_env, str):
            raise TypeError("graphiti.password_env must be a string")
        password_env = self.password_env.strip()
        object.__setattr__(self, "password_env", password_env)
        if not password_env:
            raise ValueError("graphiti.password_env must be a non-empty string")

        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise TypeError("graphiti.timeout must be a number")
        if self.timeout <= 0:
            raise ValueError("graphiti.timeout must be greater than zero")
        object.__setattr__(self, "timeout", float(self.timeout))

        if self.enabled:
            if not uri:
                raise ValueError(
                    "graphiti.enabled requires graphiti.uri (e.g. "
                    "bolt://prism-graphiti-spike:7688)"
                )
            if not group_id:
                raise ValueError(
                    "graphiti.enabled requires an explicit graphiti.group_id"
                )
        if uri:
            _, _, port = _graphiti_uri_parts(uri)
            if self.enabled:
                if port is None:
                    raise ValueError(
                        "graphiti.enabled requires an explicit port in "
                        f"graphiti.uri ({uri!r}); PRISM never applies a "
                        "default port for connections, so a portless URI "
                        "cannot be used"
                    )
                if port in _GRAPHITI_RESERVED_PORTS:
                    raise ValueError(
                        f"graphiti.uri port {port} is a standard Neo4j "
                        "default port (7474 http, 7687 bolt); graphiti.enabled "
                        "refuses these port numbers under every scheme and "
                        "must target the PRISM-owned container instead - use "
                        "an explicit non-default port (e.g. 7475 or 7688) "
                        "rather than a default local instance's ports"
                    )

    @property
    def uri_scheme(self) -> str | None:
        return None if not self.uri else _graphiti_uri_parts(self.uri)[0]

    @property
    def uri_host(self) -> str | None:
        return None if not self.uri else _graphiti_uri_parts(self.uri)[1]

    @property
    def uri_port(self) -> int | None:
        return None if not self.uri else _graphiti_uri_parts(self.uri)[2]

    @property
    def effective_uri(self) -> str:
        """The URI a real client would connect to - never a guessed default.

        PRISM never applies a default port, and no connection accepts the
        standard Neo4j listener port numbers 7474 (http) or 7687 (bolt
        family) under any scheme: an omitted port or a 7474/7687 port would
        silently reach a default local Neo4j instead of the PRISM-owned
        container, so this property raises for all of them instead of
        synthesizing a URI.  The stored ``uri`` is returned unchanged only
        once it carries an explicit non-default port.
        """
        if not self.uri:
            raise ValueError("graphiti.uri is not configured")
        _, _, port = _graphiti_uri_parts(self.uri)
        if port is None:
            raise ValueError(
                "graphiti.uri must include an explicit port for connections "
                f"({self.uri!r}); PRISM never applies a default port, so a "
                "portless URI cannot be used"
            )
        if port in _GRAPHITI_RESERVED_PORTS:
            raise ValueError(
                f"graphiti.uri port {port} is a standard Neo4j default port "
                "(7474 http, 7687 bolt); connections never target a default "
                "local instance's port numbers under any scheme - use an "
                "explicit non-default port such as 7475 or 7688"
            )
        return self.uri


@dataclass(frozen=True, slots=True)
class PrismConfig:
    """Root configuration object for the PRISM foundation module."""

    paths: PathConfig = field(default_factory=PathConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    firecrawl: FirecrawlConfig = field(default_factory=FirecrawlConfig)
    graphiti: GraphitiConfig = field(default_factory=GraphitiConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        if not isinstance(self.llm, LLMConfig):
            raise TypeError("llm must be an LLMConfig")
        if not isinstance(self.sources, SourceConfig):
            raise TypeError("sources must be a SourceConfig")
        if not isinstance(self.firecrawl, FirecrawlConfig):
            raise TypeError("firecrawl must be a FirecrawlConfig")
        if not isinstance(self.graphiti, GraphitiConfig):
            raise TypeError("graphiti must be a GraphitiConfig")

    def resolved_paths(self, home: Path | None = None) -> PathConfig:
        return self.paths.resolve(home)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": {
                "data_dir": self.paths.data_dir.as_posix(),
                "cache_dir": self.paths.cache_dir.as_posix(),
                "output_dir": self.paths.output_dir.as_posix(),
                "raw_dir": self.paths.raw_dir.as_posix(),
                "corpus_dir": self.paths.corpus_dir.as_posix(),
            },
            "llm": {
                "providers": {
                    name: {
                        "model": provider.model,
                        "api_key_env": provider.api_key_env,
                        "base_url": provider.base_url,
                        "timeout": provider.timeout,
                        "concurrency_limit": provider.concurrency_limit,
                    }
                    for name, provider in self.llm.providers.items()
                },
                "task_roles": dict(self.llm.task_roles),
            },
            "sources": {"whitelist": list(self.sources.whitelist)},
            "firecrawl": {
                "enabled": self.firecrawl.enabled,
                "api_key_env": self.firecrawl.api_key_env,
                "base_url": self.firecrawl.base_url,
                "limit": self.firecrawl.limit,
                "timeout": self.firecrawl.timeout,
            },
            "graphiti": {
                "enabled": self.graphiti.enabled,
                "uri": self.graphiti.uri,
                "database": self.graphiti.database,
                "group_id": self.graphiti.group_id,
                "username_env": self.graphiti.username_env,
                "password_env": self.graphiti.password_env,
                "timeout": self.graphiti.timeout,
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PrismConfig:
        data = _require_mapping("config", raw)
        _reject_unknown(
            data, {"paths", "llm", "sources", "firecrawl", "graphiti"}, "config"
        )

        paths_data = _require_mapping("paths", data.get("paths", {}))
        _reject_unknown(
            paths_data, {"data_dir", "cache_dir", "output_dir", "raw_dir", "corpus_dir"}, "paths"
        )
        paths = PathConfig(**paths_data)

        llm_data = _require_mapping("llm", data.get("llm", {}))
        _reject_unknown(llm_data, {"providers", "task_roles"}, "llm")
        providers_data = _require_mapping(
            "llm.providers", llm_data.get("providers", {})
        )
        providers: dict[str, LLMProviderConfig] = {}
        for name, raw_provider in providers_data.items():
            provider_data = _require_mapping(f"provider {name!r}", raw_provider)
            _reject_unknown(
                provider_data, {"model", "api_key_env", "base_url", "timeout", "concurrency_limit"}, "provider"
            )
            providers[name] = LLMProviderConfig(**provider_data)
        task_roles = _require_mapping(
            "llm.task_roles", llm_data.get("task_roles", {})
        )
        llm = LLMConfig(providers=providers, task_roles=task_roles)

        sources_data = _require_mapping("sources", data.get("sources", {}))
        _reject_unknown(sources_data, {"whitelist"}, "sources")
        sources = SourceConfig(whitelist=sources_data.get("whitelist", ()))

        firecrawl_data = _require_mapping("firecrawl", data.get("firecrawl", {}))
        _reject_unknown(
            firecrawl_data,
            {"enabled", "api_key_env", "base_url", "limit", "timeout"},
            "firecrawl",
        )
        firecrawl = FirecrawlConfig(**firecrawl_data)

        graphiti_data = _require_mapping("graphiti", data.get("graphiti", {}))
        _reject_unknown(
            graphiti_data,
            {
                "enabled",
                "uri",
                "database",
                "group_id",
                "username_env",
                "password_env",
                "timeout",
            },
            "graphiti",
        )
        graphiti = GraphitiConfig(**graphiti_data)

        return cls(
            paths=paths,
            llm=llm,
            sources=sources,
            firecrawl=firecrawl,
            graphiti=graphiti,
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> PrismConfig:
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        return cls.from_dict(raw)
