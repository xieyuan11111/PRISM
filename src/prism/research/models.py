"""Immutable research-planning contracts for PRISM evidence discovery.

Every contract below is frozen and slotted, all collections are tuples, and
``ResearchPlan`` normalizes its members into a canonical deterministic order
while enforcing plan-level integrity: each query must use one of the declared
windows and only candidate-backed source domains.  Nothing here performs I/O
or talks to an LLM; whitelist *enforcement* happens in the planner, which is
the only component that owns a :class:`~prism.config.PrismConfig`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

RESEARCH_PHASES = frozenset(
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
        "current",
    }
)

SOURCE_TYPES = frozenset(
    {
        "policy_document",
        "official_statement",
        "news",
        "academic_paper",
        "academic_discussion",
        "data_or_statistics",
        "public_discussion",
    }
)

PLAN_ORIGIN_LLM = "llm"
PLAN_ORIGIN_FALLBACK = "fallback"
PLAN_ORIGINS = frozenset({PLAN_ORIGIN_LLM, PLAN_ORIGIN_FALLBACK})

PRIORITY_MIN = 1
PRIORITY_MAX = 5

_DOMAIN_SHAPE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


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


def _normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("source domains must be strings")
    domain = value.strip().lower().rstrip(".")
    if not domain or "." not in domain or not _DOMAIN_SHAPE.fullmatch(domain):
        raise ValueError(f"invalid source domain: {value!r}")
    return domain


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


def _vocabulary_tuple(
    name: str, values: Iterable[str], allowed: frozenset[str]
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        items = tuple(values)
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    for item in items:
        _require_text(name, item)
        if item not in allowed:
            permitted = ", ".join(sorted(allowed))
            raise ValueError(f"{name} values must come from: {permitted}")
    if not items:
        raise ValueError(f"{name} must not be empty")
    return tuple(sorted(set(items)))


def _domain_tuple(name: str, values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        items = tuple(values)
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    if not items:
        raise ValueError(f"{name} must not be empty")
    try:
        domains = tuple(_normalize_domain(item) for item in items)
    except ValueError as error:
        raise ValueError(f"invalid {name} entry: {error}") from error
    return tuple(sorted(set(domains)))


def _typed_tuple(name: str, values: object, expected: type) -> tuple:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{name} must be a tuple or list")
    normalized = tuple(values)
    if any(not isinstance(item, expected) for item in normalized):
        raise TypeError(f"{name} must contain only {expected.__name__} objects")
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchWindow:
    """One interpretable phase slice of the discovery timeline."""

    phase: str
    start_at: datetime
    end_at: datetime
    focus: str

    def __post_init__(self) -> None:
        _require_choice("phase", self.phase, RESEARCH_PHASES)
        _require_aware_datetime("start_at", self.start_at)
        _require_aware_datetime("end_at", self.end_at)
        if self.end_at <= self.start_at:
            raise ValueError(
                "start_at must be earlier than end_at (reverse or empty window)"
            )
        _require_text("focus", self.focus)


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """One whitelisted source suggested for collection, with an audit reason."""

    domain: str
    source_types: tuple[str, ...]
    priority: int
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.domain, str):
            raise TypeError("domain must be a string")
        object.__setattr__(self, "domain", _normalize_domain(self.domain))
        object.__setattr__(
            self,
            "source_types",
            _vocabulary_tuple("source_types", self.source_types, SOURCE_TYPES),
        )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError("priority must be an integer")
        if not PRIORITY_MIN <= self.priority <= PRIORITY_MAX:
            raise ValueError(
                f"priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}"
            )
        _require_text("reason", self.reason)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One executable retrieval instruction bound to a research window."""

    query: str
    window: ResearchWindow
    source_types: tuple[str, ...]
    source_domains: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError("query must be a non-empty string")
        object.__setattr__(self, "query", self.query.strip())
        if not isinstance(self.window, ResearchWindow):
            raise TypeError("window must be a ResearchWindow")
        object.__setattr__(
            self,
            "source_types",
            _vocabulary_tuple("source_types", self.source_types, SOURCE_TYPES),
        )
        object.__setattr__(
            self, "source_domains", _domain_tuple("source_domains", self.source_domains)
        )
        _require_text("reason", self.reason)


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """A deterministic, auditable retrieval plan for one material.

    Members are normalized into canonical order: windows by
    ``(start_at, phase)``, candidates by ``(priority, domain)``, queries by
    ``(window.start_at, window.phase, query)``.  Integrity is enforced here
    so no caller can assemble a plan whose queries drift outside the
    declared windows or reference sources the plan never vetted.
    """

    source_id: str
    anchor_at: datetime
    frontier_at: datetime
    planned_at: datetime
    origin: str
    case_tags: tuple[str, ...] = ()
    core_claims: tuple[str, ...] = ()
    evidence_boundaries: tuple[str, ...] = ()
    windows: tuple[ResearchWindow, ...] = ()
    candidates: tuple[SourceCandidate, ...] = ()
    queries: tuple[SearchQuery, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        _require_aware_datetime("anchor_at", self.anchor_at)
        _require_aware_datetime("frontier_at", self.frontier_at)
        _require_aware_datetime("planned_at", self.planned_at)
        _require_choice("origin", self.origin, PLAN_ORIGINS)
        object.__setattr__(self, "case_tags", _text_tuple("case_tags", self.case_tags))
        object.__setattr__(
            self, "core_claims", _text_tuple("core_claims", self.core_claims)
        )
        object.__setattr__(
            self,
            "evidence_boundaries",
            _text_tuple("evidence_boundaries", self.evidence_boundaries),
        )

        windows = _typed_tuple("windows", self.windows, ResearchWindow)
        phases = [item.phase for item in windows]
        if len(set(phases)) != len(phases):
            raise ValueError("windows must not repeat a phase")
        windows = tuple(sorted(windows, key=lambda item: (item.start_at, item.phase)))

        candidates = _typed_tuple("candidates", self.candidates, SourceCandidate)
        domains = [item.domain for item in candidates]
        if len(set(domains)) != len(domains):
            raise ValueError("candidates must not repeat a domain")
        candidates = tuple(
            sorted(candidates, key=lambda item: (item.priority, item.domain))
        )

        candidate_domains = {item.domain for item in candidates}
        queries = _typed_tuple("queries", self.queries, SearchQuery)
        seen: set[tuple[str, str]] = set()
        for item in queries:
            if item.window not in windows:
                raise ValueError(
                    f"query {item.query!r} must use one of the declared windows"
                )
            unknown = set(item.source_domains) - candidate_domains
            if unknown:
                raise ValueError(
                    f"query {item.query!r} references domains not among the plan "
                    f"candidates: {', '.join(sorted(unknown))}"
                )
            key = (item.window.phase, item.query)
            if key in seen:
                raise ValueError(
                    "queries must not contain duplicate (window, query) pairs"
                )
            seen.add(key)
        queries = tuple(
            sorted(
                queries,
                key=lambda item: (item.window.start_at, item.window.phase, item.query),
            )
        )

        object.__setattr__(self, "windows", windows)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "queries", queries)
        object.__setattr__(self, "warnings", _text_tuple("warnings", self.warnings))
