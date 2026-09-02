"""Immutable report contracts for PRISM case evolution reports (FR-6.1-6.5).

Every contract below is frozen and slotted.  A report document carries only
data derived from a :class:`~prism.analyzer.EvolutionAnalysis` plus, for the
executive summary, strictly validated model output whose ``episode_key`` and
``source_id`` references must already exist in that analysis — so the report
layer can restate recorded evidence but never invent it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from prism.analyzer import (
    ChangeReason,
    EvidenceGap,
    OpenQuestion,
    TimelineStage,
    TurningPoint,
)
from prism.domain import EvidenceLocator

SUMMARY_ORIGIN_LLM = "llm"
SUMMARY_ORIGIN_FALLBACK = "fallback"
SUMMARY_ORIGINS = frozenset({SUMMARY_ORIGIN_LLM, SUMMARY_ORIGIN_FALLBACK})


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _require_text(name, value)


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _text_tuple(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    for value in normalized:
        _require_text(name, value)
    return normalized


def _typed_tuple(name: str, values: object, expected: type) -> tuple:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{name} must be a tuple or list")
    normalized = tuple(values)
    if any(not isinstance(item, expected) for item in normalized):
        raise TypeError(f"{name} must contain only {expected.__name__} objects")
    return normalized


@dataclass(frozen=True, slots=True)
class ReportCitation:
    """One source referenced by the report, with the episodes that cite it."""

    source_id: str
    episode_keys: tuple[str, ...] = ()
    evidence: tuple[EvidenceLocator, ...] = ()

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        object.__setattr__(
            self, "episode_keys", _text_tuple("episode_keys", self.episode_keys)
        )
        object.__setattr__(
            self,
            "evidence",
            _typed_tuple("evidence", self.evidence, EvidenceLocator),
        )
        if any(item.source_id != self.source_id for item in self.evidence):
            raise ValueError("citation evidence must match citation source_id")


@dataclass(frozen=True, slots=True)
class ReportSummary:
    """The executive summary of one report and its full provenance.

    ``origin`` records whether the text was distilled by the
    ``summarize_report`` LLM role or composed by the deterministic fallback,
    so a model summary can never be presented as, or mistaken for, recorded
    evidence.  ``citations`` bound every summary judgment back to source ids
    and episode keys that exist in the analyzed evidence (FR-6.5).
    """

    summary: str
    key_findings: tuple[str, ...] = ()
    turning_points: tuple[str, ...] = ()
    causal_chain: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    citations: tuple[ReportCitation, ...] = ()
    origin: str = SUMMARY_ORIGIN_FALLBACK

    def __post_init__(self) -> None:
        _require_text("summary", self.summary)
        object.__setattr__(
            self, "key_findings", _text_tuple("key_findings", self.key_findings)
        )
        object.__setattr__(
            self, "turning_points", _text_tuple("turning_points", self.turning_points)
        )
        object.__setattr__(
            self, "causal_chain", _text_tuple("causal_chain", self.causal_chain)
        )
        object.__setattr__(
            self, "uncertainties", _text_tuple("uncertainties", self.uncertainties)
        )
        object.__setattr__(
            self, "citations", _typed_tuple("citations", self.citations, ReportCitation)
        )
        if self.origin not in SUMMARY_ORIGINS:
            allowed = ", ".join(sorted(SUMMARY_ORIGINS))
            raise ValueError(f"origin must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class ReportDocument:
    """A complete, rendered evolution report for one case at one instant.

    The structured sections are the analysis itself, restated verbatim; the
    rendered ``markdown`` always keeps them alongside the summary, so summary
    text can complement but never overwrite recorded facts (FR-6.2).
    """

    case_id: str
    as_of: datetime
    case_type: str | None
    summary: ReportSummary
    stages: tuple[TimelineStage, ...]
    turning_points: tuple[TurningPoint, ...]
    change_reasons: tuple[ChangeReason, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    open_questions: tuple[OpenQuestion, ...]
    citations: tuple[ReportCitation, ...]
    markdown: str
    case_status: str | None = None

    def __post_init__(self) -> None:
        _require_text("case_id", self.case_id)
        _require_aware("as_of", self.as_of)
        _optional_text("case_type", self.case_type)
        _optional_text("case_status", self.case_status)
        if not isinstance(self.summary, ReportSummary):
            raise TypeError("summary must be a ReportSummary")
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
            "citations",
            _typed_tuple("citations", self.citations, ReportCitation),
        )
        _require_text("markdown", self.markdown)
