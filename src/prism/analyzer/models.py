"""Immutable result contracts for PRISM evolution analysis (FR-4).

Every derived statement keeps the ``episode_key`` and ``source_ids`` of the
timeline entry it was derived from.  Entries are classified into layers so
recorded facts can never be presented as, or merged with, interpretations:
``claim`` entries are interpretations, ``material_provenance`` entries are
provenance metadata, and everything else is factual — with each entry's own
``provenance_type``/``confidence`` retained so inferred facts stay visibly
inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from prism.domain import EvidenceLocator

FACT_LAYER = "fact"
INTERPRETATION_LAYER = "interpretation"
PROVENANCE_LAYER = "provenance"
LAYERS = frozenset({FACT_LAYER, INTERPRETATION_LAYER, PROVENANCE_LAYER})

ENTRY_KINDS = frozenset(
    {
        "evolution_case",
        "evolution_node",
        "temporal_fact",
        "claim",
        "temporal_relation",
        "material_provenance",
    }
)

KIND_LAYERS = {
    "evolution_case": FACT_LAYER,
    "evolution_node": FACT_LAYER,
    "temporal_fact": FACT_LAYER,
    "claim": INTERPRETATION_LAYER,
    "temporal_relation": FACT_LAYER,
    "material_provenance": PROVENANCE_LAYER,
}

# Kinds whose entries assert substantive content and therefore require sources.
SUBSTANTIVE_KINDS = frozenset(
    {"evolution_node", "temporal_fact", "claim", "temporal_relation"}
)

# Node types that mark a pivot in the policy chain (FR-4.5) or the academic
# discourse chain (FR-4.6).
TURNING_NODE_TYPES = frozenset(
    {
        "publication",
        "implementation",
        "revision",
        "reversal",
        "replacement",
        "expiry",
        "debate",
        "consensus",
    }
)

# Node types that record an explicit change or its trigger (FR-3.8).
REASON_NODE_TYPES = frozenset({"revision", "reversal", "replacement", "response"})

FACT_CHANGE = "fact_change"
INTERPRETATION_CHANGE = "interpretation_change"
CHANGE_NATURES = frozenset({FACT_CHANGE, INTERPRETATION_CHANGE})

FACT_SUPERSEDED = "fact_superseded"
CLAIM_REVISED = "claim_revised"

GAP_EMPTY_TIMELINE = "empty_timeline"
GAP_MISSING_CASE_DEFINITION = "missing_case_definition"
GAP_UNATTRIBUTED_ENTRY = "unattributed_entry"
GAP_MISSING_EVIDENCE_LOCATION = "missing_evidence_location"
# The two types below are part of the public gap taxonomy but are NOT derived
# by the deterministic analyzer: deciding that a stage lacks its primary
# source text, or that a prediction has no official confirmation, requires
# knowledge the timeline record does not carry (e.g. what else exists in the
# corpus).  They are reserved for caller-recorded case audits; the analyzer
# never fabricates them from well-evidenced entries.
GAP_MISSING_PRIMARY_SOURCE = "missing_primary_source"
GAP_UNVERIFIED_PREDICTION = "unverified_prediction"
GAP_TYPES = frozenset(
    {
        GAP_EMPTY_TIMELINE,
        GAP_MISSING_CASE_DEFINITION,
        GAP_UNATTRIBUTED_ENTRY,
        GAP_MISSING_EVIDENCE_LOCATION,
        GAP_MISSING_PRIMARY_SOURCE,
        GAP_UNVERIFIED_PREDICTION,
    }
)

ORIGIN_OPEN_QUESTION_NODE = "open_question_node"
ORIGIN_UNCERTAIN_CLAIM = "uncertain_claim"
ORIGIN_UNCONFIRMED_CHANGE_CAUSE = "unconfirmed_change_cause"
QUESTION_ORIGINS = frozenset(
    {
        ORIGIN_OPEN_QUESTION_NODE,
        ORIGIN_UNCERTAIN_CLAIM,
        ORIGIN_UNCONFIRMED_CHANGE_CAUSE,
    }
)


def require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def layer_for_kind(kind: str) -> str:
    """Map a timeline entry kind to its fact/interpretation/provenance layer."""
    require_text("kind", kind)
    try:
        return KIND_LAYERS[kind]
    except KeyError:
        allowed = ", ".join(sorted(ENTRY_KINDS))
        raise ValueError(
            f"unknown timeline entry kind {kind!r}; must be one of: {allowed}"
        ) from None


def _optional_text(name: str, value: str | None) -> None:
    if value is not None:
        require_text(name, value)


def _require_interval(valid_at: datetime, invalid_at: datetime | None) -> None:
    require_aware("valid_at", valid_at)
    if invalid_at is not None:
        require_aware("invalid_at", invalid_at)
        if invalid_at < valid_at:
            raise ValueError("invalid_at must not be earlier than valid_at")


def _source_tuple(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    result = tuple(values)  # type: ignore[arg-type]
    for value in result:
        require_text(name, value)
    return result


def _optional_confidence(name: str, value: float | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")


def _typed_tuple(name: str, values: object, expected: type) -> tuple:
    result = tuple(values)  # type: ignore[arg-type]
    for item in result:
        if not isinstance(item, expected):
            raise TypeError(f"{name} must contain only {expected.__name__} objects")
    return result


@dataclass(frozen=True, slots=True)
class TimelineStage:
    """One timeline entry projected onto the analysis view.

    The layer is derived from the kind and re-validated here so an
    interpretation can never be constructed disguised as a fact.
    """

    episode_key: str
    kind: str
    layer: str
    summary: str
    valid_at: datetime
    invalid_at: datetime | None
    reference_time: datetime
    source_ids: tuple[str, ...]
    node_type: str | None = None
    confidence: float | None = None
    provenance_type: str | None = None
    stance: str | None = None
    happened_at: datetime | None = None
    evidence: tuple[EvidenceLocator, ...] = ()
    claim_type: str | None = None
    relation_type: str | None = None
    source_ref: str | None = None
    target_ref: str | None = None
    record_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("episode_key", "kind", "layer", "summary"):
            require_text(name, getattr(self, name))
        if self.layer != layer_for_kind(self.kind):
            raise ValueError(f"layer {self.layer!r} does not match kind {self.kind!r}")
        _require_interval(self.valid_at, self.invalid_at)
        require_aware("reference_time", self.reference_time)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )
        for name in (
            "node_type", "provenance_type", "stance", "claim_type",
            "relation_type", "source_ref", "target_ref", "record_id",
        ):
            _optional_text(name, getattr(self, name))
        if self.happened_at is not None:
            require_aware("happened_at", self.happened_at)
        _optional_confidence("confidence", self.confidence)
        object.__setattr__(
            self, "evidence", _typed_tuple("evidence", self.evidence, EvidenceLocator)
        )


@dataclass(frozen=True, slots=True)
class TurningPoint:
    """A chain-defining pivot: a turning node type or a fact supersession."""

    episode_key: str
    category: str
    at: datetime
    summary: str
    source_ids: tuple[str, ...]
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        for name in ("episode_key", "category", "summary"):
            require_text(name, getattr(self, name))
        require_aware("at", self.at)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )
        object.__setattr__(
            self, "evidence", _typed_tuple("evidence", self.evidence, EvidenceLocator)
        )


@dataclass(frozen=True, slots=True)
class ChangeReason:
    """An evidence-backed reason a change was recorded, never an invention."""

    episode_key: str
    reason_type: str
    nature: str
    at: datetime
    summary: str
    source_ids: tuple[str, ...]
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        for name in ("episode_key", "reason_type", "summary"):
            require_text(name, getattr(self, name))
        if self.nature not in CHANGE_NATURES:
            allowed = ", ".join(sorted(CHANGE_NATURES))
            raise ValueError(f"nature must be one of: {allowed}")
        require_aware("at", self.at)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )
        object.__setattr__(
            self, "evidence", _typed_tuple("evidence", self.evidence, EvidenceLocator)
        )


@dataclass(frozen=True, slots=True)
class EvidenceGap:
    """An explicit statement that evidence is missing, not a fabricated fill."""

    gap_type: str
    detail: str
    episode_key: str | None = None
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text("gap_type", self.gap_type)
        if self.gap_type not in GAP_TYPES:
            allowed = ", ".join(sorted(GAP_TYPES))
            raise ValueError(f"gap_type must be one of: {allowed}")
        require_text("detail", self.detail)
        _optional_text("episode_key", self.episode_key)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """An unresolved question raised by recorded evidence."""

    episode_key: str
    origin: str
    question: str
    raised_by: str | None
    at: datetime
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("episode_key", "question"):
            require_text(name, getattr(self, name))
        if self.origin not in QUESTION_ORIGINS:
            allowed = ", ".join(sorted(QUESTION_ORIGINS))
            raise ValueError(f"origin must be one of: {allowed}")
        _optional_text("raised_by", self.raised_by)
        require_aware("at", self.at)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )


@dataclass(frozen=True, slots=True)
class EvolutionAnalysis:
    """The stable, fully-derived analysis of one case at one instant."""

    case_id: str
    as_of: datetime
    case_type: str | None
    stages: tuple[TimelineStage, ...]
    turning_points: tuple[TurningPoint, ...]
    change_reasons: tuple[ChangeReason, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    open_questions: tuple[OpenQuestion, ...]
    case_status: str | None = None
    invalidated_stages: tuple[TimelineStage, ...] = ()

    def __post_init__(self) -> None:
        require_text("case_id", self.case_id)
        require_aware("as_of", self.as_of)
        _optional_text("case_type", self.case_type)
        _optional_text("case_status", self.case_status)
        object.__setattr__(
            self, "stages", _typed_tuple("stages", self.stages, TimelineStage)
        )
        object.__setattr__(
            self,
            "turning_points",
            _typed_tuple("turning_points", self.turning_points, TurningPoint),
        )
        object.__setattr__(
            self,
            "change_reasons",
            _typed_tuple("change_reasons", self.change_reasons, ChangeReason),
        )
        object.__setattr__(
            self,
            "evidence_gaps",
            _typed_tuple("evidence_gaps", self.evidence_gaps, EvidenceGap),
        )
        object.__setattr__(
            self,
            "open_questions",
            _typed_tuple("open_questions", self.open_questions, OpenQuestion),
        )
        object.__setattr__(
            self,
            "invalidated_stages",
            _typed_tuple(
                "invalidated_stages", self.invalidated_stages, TimelineStage
            ),
        )


@dataclass(frozen=True, slots=True)
class ComparisonChange:
    """One entry whose effectiveness differed between two instants."""

    episode_key: str
    kind: str
    layer: str
    summary: str
    valid_at: datetime
    invalid_at: datetime | None
    source_ids: tuple[str, ...]
    confidence: float | None = None
    provenance_type: str | None = None
    stance: str | None = None
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        for name in ("episode_key", "kind", "layer", "summary"):
            require_text(name, getattr(self, name))
        if self.layer != layer_for_kind(self.kind):
            raise ValueError(f"layer {self.layer!r} does not match kind {self.kind!r}")
        _require_interval(self.valid_at, self.invalid_at)
        object.__setattr__(
            self, "source_ids", _source_tuple("source_ids", self.source_ids)
        )
        _optional_confidence("confidence", self.confidence)
        _optional_text("provenance_type", self.provenance_type)
        _optional_text("stance", self.stance)
        object.__setattr__(
            self, "evidence", _typed_tuple("evidence", self.evidence, EvidenceLocator)
        )


@dataclass(frozen=True, slots=True)
class HistoricalCaseState:
    """Auditable state containing only knowledge available at a cutoff."""

    case_id: str
    cutoff_at: datetime
    case_type: str | None
    status: str | None
    nodes: tuple[TimelineStage, ...]
    facts: tuple[TimelineStage, ...]
    interpretations: tuple[TimelineStage, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    invalidated_facts: tuple[TimelineStage, ...] = ()
    relations: tuple[TimelineStage, ...] = ()

    def __post_init__(self) -> None:
        require_text("case_id", self.case_id)
        require_aware("cutoff_at", self.cutoff_at)
        _optional_text("case_type", self.case_type)
        _optional_text("status", self.status)
        for name in ("nodes", "facts", "interpretations"):
            object.__setattr__(
                self, name, _typed_tuple(name, getattr(self, name), TimelineStage)
            )
        object.__setattr__(
            self,
            "evidence_gaps",
            _typed_tuple("evidence_gaps", self.evidence_gaps, EvidenceGap),
        )
        for name in ("invalidated_facts", "relations"):
            object.__setattr__(
                self, name, _typed_tuple(name, getattr(self, name), TimelineStage)
            )


@dataclass(frozen=True, slots=True)
class EvolutionComparison:
    """Effective-entry differences between two historical instants (FR-4.3/4.4)."""

    case_id: str
    earlier: datetime
    later: datetime
    added: tuple[ComparisonChange, ...]
    removed: tuple[ComparisonChange, ...]
    unchanged: tuple[ComparisonChange, ...]

    def __post_init__(self) -> None:
        require_text("case_id", self.case_id)
        require_aware("earlier", self.earlier)
        require_aware("later", self.later)
        if self.later < self.earlier:
            raise ValueError("later must not be earlier than earlier")
        for name in ("added", "removed", "unchanged"):
            object.__setattr__(
                self, name, _typed_tuple(name, getattr(self, name), ComparisonChange)
            )
