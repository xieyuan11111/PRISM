"""Dependency-free PRISM configuration and JSON serialization."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_DOMAIN_PATTERN = re.compile(
    r"(?=^.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


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

    def __post_init__(self) -> None:
        _require_text("model", self.model)
        if self.api_key_env is not None:
            _require_text("api_key_env", self.api_key_env)
        if self.base_url is not None:
            _require_text("base_url", self.base_url)


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
class PrismConfig:
    """Root configuration object for the PRISM foundation module."""

    paths: PathConfig = field(default_factory=PathConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.paths, PathConfig):
            raise TypeError("paths must be a PathConfig")
        if not isinstance(self.llm, LLMConfig):
            raise TypeError("llm must be an LLMConfig")
        if not isinstance(self.sources, SourceConfig):
            raise TypeError("sources must be a SourceConfig")

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
                    }
                    for name, provider in self.llm.providers.items()
                },
                "task_roles": dict(self.llm.task_roles),
            },
            "sources": {"whitelist": list(self.sources.whitelist)},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PrismConfig:
        data = _require_mapping("config", raw)
        _reject_unknown(data, {"paths", "llm", "sources"}, "config")

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
                provider_data, {"model", "api_key_env", "base_url"}, "provider"
            )
            providers[name] = LLMProviderConfig(**provider_data)
        task_roles = _require_mapping(
            "llm.task_roles", llm_data.get("task_roles", {})
        )
        llm = LLMConfig(providers=providers, task_roles=task_roles)

        sources_data = _require_mapping("sources", data.get("sources", {}))
        _reject_unknown(sources_data, {"whitelist"}, "sources")
        sources = SourceConfig(whitelist=sources_data.get("whitelist", ()))

        return cls(paths=paths, llm=llm, sources=sources)

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
