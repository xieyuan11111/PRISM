"""Data types for the PRISM SQLite/FTS5 text evidence index (module 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_text(name: str, value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


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
class IndexEntry:
    """A document row in the evidence index."""

    source_id: str
    title: str
    source: str
    published_at: datetime
    fetched_at: datetime
    type: str
    content: str
    path: str
    content_hash: str
    case_tags: tuple[str, ...] = ()
    original_format: str | None = None
    ocr: bool = False
    extracted_via: str | None = None
    raw_path: str | None = None
    url: str | None = None
    retrieval_level: str | None = None
    access_level: str | None = None
    doi: str | None = None
    authors: tuple[str, ...] = ()
    container_title: str | None = None
    pmid: str | None = None
    pmcid: str | None = None

    def __post_init__(self) -> None:
        _require_aware("published_at", self.published_at)
        _require_aware("fetched_at", self.fetched_at)
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))
        object.__setattr__(self, "authors", _text_tuple("authors", self.authors))


@dataclass(frozen=True, slots=True)
class IndexOutcome:
    """Result of indexing one file: the entry and what the upsert did."""

    entry: IndexEntry
    status: str  # "indexed" | "updated" | "unchanged"


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One full-text search result with a snippet of the original body."""

    source_id: str
    title: str
    path: str
    snippet: str
    source: str
    type: str
    published_at: datetime
    case_tags: tuple[str, ...] = ()
    url: str | None = None
    raw_path: str | None = None
    retrieval_level: str | None = None
    access_level: str | None = None
    doi: str | None = None
    authors: tuple[str, ...] = ()
    container_title: str | None = None
    pmid: str | None = None
    pmcid: str | None = None

    def __post_init__(self) -> None:
        _require_aware("published_at", self.published_at)
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))
        object.__setattr__(self, "authors", _text_tuple("authors", self.authors))


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Summary of a corpus directory scan."""

    total: int
    indexed: int
    updated: int
    unchanged: int
    failed: int
    errors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """Full-text query plus optional metadata/time-range filters."""

    query: str | None = None
    case_tag: str | None = None
    source: str | None = None
    type: str | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("query", "case_tag", "source", "type"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_text(name, value))
        for name in ("published_after", "published_before"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(name, value)
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after > self.published_before
        ):
            raise ValueError(
                "published_after must not be later than published_before"
            )
