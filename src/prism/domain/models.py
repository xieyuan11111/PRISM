"""Dependency-free domain dataclasses for PRISM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


CASE_TYPES = frozenset({"policy", "academic_discourse", "public_issue"})
NODE_TYPES = frozenset(
    {
        "proposal",
        "draft",
        "publication",
        "interpretation",
        "implementation",
        "response",
        "revision",
        "reversal",
        "replacement",
        "expiry",
        "debate",
        "consensus",
        "open_question",
    }
)
CLAIM_STANCES = frozenset({"support", "oppose", "conditional", "uncertain"})
ORIGINAL_FORMATS = frozenset({"md", "pdf", "html"})


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _require_text(name, value)


def _require_choice(name: str, value: str, choices: frozenset[str]) -> None:
    _require_text(name, value)
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {allowed}")


def _require_aware_datetime(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


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


def _require_confidence(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("confidence must be a number")
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")


@dataclass(frozen=True, slots=True)
class Material:
    """A normalized source document available for analysis."""

    id: str
    title: str
    source: str
    published_at: datetime
    fetched_at: datetime
    type: str
    content: str
    original_format: str | None = None
    ocr: bool = False
    extracted_via: str | None = None
    raw_path: str | None = None
    case_tags: tuple[str, ...] = ()
    url: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "title", "source", "type", "content"):
            _require_text(name, getattr(self, name))
        _require_aware_datetime("published_at", self.published_at)
        _require_aware_datetime("fetched_at", self.fetched_at)
        if self.fetched_at < self.published_at:
            raise ValueError("fetched_at must not be earlier than published_at")
        if self.original_format is not None:
            _require_choice(
                "original_format", self.original_format, ORIGINAL_FORMATS
            )
        if not isinstance(self.ocr, bool):
            raise TypeError("ocr must be a bool")
        for name in ("extracted_via", "raw_path", "url"):
            _validate_optional_text(name, getattr(self, name))
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))


@dataclass(frozen=True, slots=True)
class EvolutionCase:
    """A developing subject whose changes are tracked over time."""

    case_id: str
    case_type: str
    canonical_name: str
    start_at: datetime
    status: str
    node_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id)
        _require_choice("case_type", self.case_type, CASE_TYPES)
        _require_text("canonical_name", self.canonical_name)
        _require_aware_datetime("start_at", self.start_at)
        _require_text("status", self.status)
        object.__setattr__(self, "node_ids", _text_tuple("node_ids", self.node_ids))


@dataclass(frozen=True, slots=True)
class EvolutionNode:
    """A time-addressable stage or change point in an evolution case."""

    id: str
    case_id: str
    node_type: str
    happened_at: datetime
    summary: str
    source_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("id", self.id)
        _require_text("case_id", self.case_id)
        _require_choice("node_type", self.node_type, NODE_TYPES)
        _require_aware_datetime("happened_at", self.happened_at)
        _require_text("summary", self.summary)
        object.__setattr__(
            self, "source_ids", _text_tuple("source_ids", self.source_ids)
        )
        object.__setattr__(
            self, "claim_ids", _text_tuple("claim_ids", self.claim_ids)
        )


@dataclass(frozen=True, slots=True)
class TemporalFact:
    """A fact whose validity is bounded in time and backed by sources."""

    subject: str
    predicate: str
    object: str
    valid_at: datetime
    invalid_at: datetime | None
    observed_at: datetime
    source_ids: tuple[str, ...]
    confidence: float
    provenance_type: str

    def __post_init__(self) -> None:
        for name in ("subject", "predicate", "object", "provenance_type"):
            _require_text(name, getattr(self, name))
        _require_aware_datetime("valid_at", self.valid_at)
        if self.invalid_at is not None:
            _require_aware_datetime("invalid_at", self.invalid_at)
            if self.invalid_at < self.valid_at:
                raise ValueError("invalid_at must not be earlier than valid_at")
        _require_aware_datetime("observed_at", self.observed_at)
        object.__setattr__(
            self, "source_ids", _text_tuple("source_ids", self.source_ids)
        )
        _require_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class Claim:
    """A source-backed position stated by an actor at a specific time."""

    claim_id: str
    actor: str
    proposition: str
    stance: str
    stated_at: datetime
    based_on: tuple[str, ...] = ()
    revised_by: str | None = None

    def __post_init__(self) -> None:
        for name in ("claim_id", "actor", "proposition"):
            _require_text(name, getattr(self, name))
        _require_choice("stance", self.stance, CLAIM_STANCES)
        _require_aware_datetime("stated_at", self.stated_at)
        object.__setattr__(self, "based_on", _text_tuple("based_on", self.based_on))
        _validate_optional_text("revised_by", self.revised_by)
