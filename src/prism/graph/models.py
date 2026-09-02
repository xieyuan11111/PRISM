"""Immutable contracts for PRISM graph episodes and historical timelines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from prism.domain import EvidenceLocator


def require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def require_interval(valid_at: datetime, invalid_at: datetime | None) -> None:
    require_aware("valid_at", valid_at)
    if invalid_at is not None:
        require_aware("invalid_at", invalid_at)
        if invalid_at < valid_at:
            raise ValueError("invalid_at must not be earlier than valid_at")


def source_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("source_ids must be an iterable of strings, not a string")
    result = tuple(values)
    for value in result:
        require_text("source_ids", value)
    return result


def evidence_tuple(values: object) -> tuple[EvidenceLocator, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("evidence must be an iterable of EvidenceLocator objects")
    result = tuple(values)  # type: ignore[arg-type]
    if any(not isinstance(value, EvidenceLocator) for value in result):
        raise TypeError("evidence must contain only EvidenceLocator objects")
    return result


@dataclass(frozen=True, slots=True)
class GraphEpisode:
    """One explicit, deterministic ingestion unit for a graph backend."""

    episode_key: str
    name: str
    case_id: str
    kind: str
    episode_body: str
    reference_time: datetime
    valid_at: datetime
    invalid_at: datetime | None
    source_ids: tuple[str, ...]
    confidence: float | None = None
    provenance_type: str | None = None
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        for name in ("episode_key", "name", "case_id", "kind", "episode_body"):
            require_text(name, getattr(self, name))
        require_aware("reference_time", self.reference_time)
        require_interval(self.valid_at, self.invalid_at)
        object.__setattr__(self, "source_ids", source_tuple(self.source_ids))
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise TypeError("confidence must be a number")
            if not 0.0 <= self.confidence <= 1.0:
                raise ValueError("confidence must be between 0.0 and 1.0")
        if self.provenance_type is not None:
            require_text("provenance_type", self.provenance_type)
        object.__setattr__(self, "evidence", evidence_tuple(self.evidence))
        if not {item.source_id for item in self.evidence}.issubset(set(self.source_ids)):
            raise ValueError("evidence source_id must be present in source_ids")


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """A graph episode that was valid at the requested historical instant."""

    episode_key: str
    case_id: str
    kind: str
    summary: str
    reference_time: datetime
    valid_at: datetime
    invalid_at: datetime | None
    source_ids: tuple[str, ...]
    confidence: float | None
    provenance_type: str | None
    stance: str | None
    payload: str
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        for name in ("episode_key", "case_id", "kind", "summary", "payload"):
            require_text(name, getattr(self, name))
        require_aware("reference_time", self.reference_time)
        require_interval(self.valid_at, self.invalid_at)
        object.__setattr__(self, "source_ids", source_tuple(self.source_ids))
        if self.stance is not None:
            require_text("stance", self.stance)
        object.__setattr__(self, "evidence", evidence_tuple(self.evidence))
        if not {item.source_id for item in self.evidence}.issubset(set(self.source_ids)):
            raise ValueError("evidence source_id must be present in source_ids")


@dataclass(frozen=True, slots=True)
class GraphTimeline:
    """The valid graph state for one case at one timezone-aware instant."""

    case_id: str
    as_of: datetime
    entries: tuple[TimelineEntry, ...]

    def __post_init__(self) -> None:
        require_text("case_id", self.case_id)
        require_aware("as_of", self.as_of)
        object.__setattr__(self, "entries", tuple(self.entries))


@dataclass(frozen=True, slots=True)
class GraphWriteResult:
    """Result of incrementally submitting a case's graph episodes."""

    episodes: tuple[GraphEpisode, ...]
    added_keys: tuple[str, ...]
    skipped_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "episodes", tuple(self.episodes))
        object.__setattr__(self, "added_keys", tuple(self.added_keys))
        object.__setattr__(self, "skipped_keys", tuple(self.skipped_keys))
