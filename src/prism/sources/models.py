"""Data contracts for the PRISM sources collection layer.

This module defines the shapes every caller touches: ``SourceItem`` (one
collected public item, ready for :class:`~prism.ingestion.IngestionService`),
``FetchResult``/``FetchBatch`` (outcomes of one or many fetches), the
``SourceFetcher`` pluggable-parse protocol, and the classified failure
taxonomy (``FailureKind``/``SourceFetchError``/``FetchFailure``) required by
FR-1.6 so HTTP status, timeout, parse, and policy failures stay
distinguishable.  Nothing here performs I/O.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Protocol

from .urls import normalize_url


class FailureKind(StrEnum):
    """Why a fetch failed; each value maps to one distinct failure mode."""

    BLOCKED = "blocked"          # URL violates the whitelist/SSRF policy
    HTTP_STATUS = "http_status"  # non-2xx HTTP response
    TIMEOUT = "timeout"          # transport timed out
    PARSE = "parse"              # payload could not be parsed
    TRANSPORT = "transport"      # network-level or contract error


class SourceFetchError(Exception):
    """A classified fetch failure carrying machine-readable context."""

    def __init__(self, kind: FailureKind, url: str, detail: str) -> None:
        if not isinstance(kind, FailureKind):
            raise TypeError("kind must be a FailureKind")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError("detail must be a non-empty string")
        super().__init__(f"{kind.value} error fetching {url}: {detail}")
        self.kind = kind
        self.url = url
        self.detail = detail

    def as_failure(self) -> "FetchFailure":
        return FetchFailure(url=self.url, kind=self.kind, detail=self.detail)


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """One recorded fetch failure inside a batch (FR-1.6 failure trail)."""

    url: str
    kind: FailureKind
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")
        if not isinstance(self.kind, FailureKind):
            raise TypeError("kind must be a FailureKind")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("detail must be a non-empty string")


class SourceFetcher(Protocol):
    """A pluggable strategy that turns one fetched payload into items.

    Implementations parse only — the ``SourceService`` owns transport,
    whitelist enforcement, and deduplication.  Register instances via
    ``SourceService(fetchers=[...])`` and select them with
    ``fetch(url, kind=<fetcher>.kind)``; new source types require no core
    changes (open-source plugin design, REQUIREMENTS §13.3).
    """

    kind: str

    def parse(
        self, body: str, *, url: str, fetched_at: datetime
    ) -> tuple["SourceItem", ...]: ...


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _optional_text(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")
    return value if value.strip() else None


def _text_tuple(name: str, values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        normalized = tuple(values)
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    for value in normalized:
        _require_text(name, value)
    return normalized


@dataclass(frozen=True, slots=True)
class SourceItem:
    """One collected public item, ready to be handed to IngestionService.

    ``fetched_at`` is always timezone-aware.  ``published_at`` is the value
    observed in the source material or ``None`` when the source carried no
    (trustworthy) date — unknown publish times are never invented, and
    :meth:`to_ingestion_metadata` falls back to the fetch time.  ``link`` is a
    reference for provenance, not a fetch target: any future fetch must pass
    whitelist validation again.
    """

    title: str
    source: str
    fetched_at: datetime
    link: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    content: str | None = None
    type: str = "news"
    case_tags: tuple[str, ...] = ()
    retrieval_level: str | None = None
    access_level: str | None = None
    doi: str | None = None
    authors: tuple[str, ...] = ()
    container_title: str | None = None

    def __post_init__(self) -> None:
        for name in ("title", "source", "type"):
            _require_text(name, getattr(self, name))
        for name in ("link", "summary", "content"):
            object.__setattr__(self, name, _optional_text(name, getattr(self, name)))
        _require_aware_datetime("fetched_at", self.fetched_at)
        if self.published_at is not None:
            _require_aware_datetime("published_at", self.published_at)
            if self.published_at > self.fetched_at:
                raise ValueError("published_at must not be later than fetched_at")
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))
        for name in ("retrieval_level", "access_level", "doi", "container_title"):
            value = getattr(self, name)
            if value is not None:
                _require_text(name, value)
        if self.access_level is not None and self.access_level not in {
            "fulltext", "abstract_only", "metadata_only", "blocked"
        }:
            raise ValueError("access_level must be fulltext, abstract_only, metadata_only, or blocked")
        object.__setattr__(self, "authors", _text_tuple("authors", self.authors))

    @property
    def dedup_key(self) -> str:
        """Stable identity: normalized link first, content hash as fallback."""
        if self.link:
            return "link:" + normalize_url(self.link)
        payload = " ".join((self.content or self.summary or "").split())
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return "content:" + digest

    def to_ingestion_metadata(self) -> dict[str, Any]:
        """Metadata mapping accepted by ``IngestionService.ingest(path, ...)``.

        Write ``content`` to a Markdown file and pass that path together with
        this mapping; the sources layer never writes the corpus itself.
        """
        metadata: dict[str, Any] = {
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at or self.fetched_at,
            "fetched_at": self.fetched_at,
            "type": self.type,
            "case_tags": list(self.case_tags),
            "url": self.link,
        }
        for name in ("retrieval_level", "access_level", "doi"):
            value = getattr(self, name)
            if value is not None:
                metadata[name] = value
        if self.authors:
            metadata["authors"] = list(self.authors)
        if self.container_title is not None:
            metadata["container_title"] = self.container_title
        return metadata


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The outcome of fetching one source URL."""

    url: str
    fetched_at: datetime
    items: tuple[SourceItem, ...] = ()
    duplicate_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("url", self.url)
        _require_aware_datetime("fetched_at", self.fetched_at)
        object.__setattr__(self, "items", tuple(self.items))
        for item in self.items:
            if not isinstance(item, SourceItem):
                raise TypeError("items must contain only SourceItem objects")
        object.__setattr__(self, "duplicate_keys", tuple(self.duplicate_keys))
        for key in self.duplicate_keys:
            _require_text("duplicate key", key)
