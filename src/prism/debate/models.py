"""Immutable contracts for automatic multi-perspective debate (FR-5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


STATEMENT_CLASSIFICATIONS = frozenset(
    {
        "fact",
        "interpretation",
        "value_judgment",
        "prediction",
        "unresolved",
    }
)

PERSPECTIVE_STATUSES = frozenset({"available", "unavailable"})

DEBATE_STATUSES = frozenset(
    {
        "completed",
        "completed_with_unavailable_perspectives",
        "degraded",
        "no_conclusion",
    }
)


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _id_tuple(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{name} must be an iterable of strings, not a string")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{name} must be an iterable of strings") from error
    for value in normalized:
        _text(name, value)
    return normalized


def _optional_text(name: str, value: str | None) -> None:
    if value is not None:
        _text(name, value)


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class PerspectiveProfile:
    """One observation position; it never prescribes a conclusion."""

    id: str
    label: str
    description: str
    preset_conclusion: bool = False

    def __post_init__(self) -> None:
        for name in ("id", "label", "description"):
            _text(name, getattr(self, name))
        if not isinstance(self.preset_conclusion, bool):
            raise TypeError("preset_conclusion must be a bool")
        if self.preset_conclusion:
            raise ValueError("perspective profiles must not preset conclusions")


@dataclass(frozen=True, slots=True)
class DebateStatement:
    id: str
    classification: str
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("id", "text"):
            _text(name, getattr(self, name))
        if self.classification not in STATEMENT_CLASSIFICATIONS:
            allowed = ", ".join(sorted(STATEMENT_CLASSIFICATIONS))
            raise ValueError(f"classification must be one of: {allowed}")
        object.__setattr__(
            self, "evidence_ids", _id_tuple("evidence_ids", self.evidence_ids)
        )
        # Unresolved statements are the only statements that may lack a
        # citation. This is stricter than the minimum fact/causal rule and
        # keeps model explanations from being promoted without evidence.
        if self.classification != "unresolved" and not self.evidence_ids:
            raise ValueError(
                f"statement {self.id!r} requires at least one evidence id"
            )


@dataclass(frozen=True, slots=True)
class IndependentInterpretation:
    profile_id: str
    statements: tuple[DebateStatement, ...]

    def __post_init__(self) -> None:
        _text("profile_id", self.profile_id)
        object.__setattr__(
            self,
            "statements",
            tuple(
                item if isinstance(item, DebateStatement) else DebateStatement(**item)
                for item in self.statements
            ),
        )


@dataclass(frozen=True, slots=True)
class CrossChallenge:
    challenge_id: str
    target_profile_id: str
    target_statement_id: str
    challenge: str
    reply: str
    withdrawn: bool
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("challenge_id", "target_profile_id", "target_statement_id", "challenge", "reply"):
            _text(name, getattr(self, name))
        if not isinstance(self.withdrawn, bool):
            raise TypeError("withdrawn must be a bool")
        object.__setattr__(
            self, "evidence_ids", _id_tuple("evidence_ids", self.evidence_ids)
        )


@dataclass(frozen=True, slots=True)
class CrossExamination:
    profile_id: str
    challenges: tuple[CrossChallenge, ...]

    def __post_init__(self) -> None:
        _text("profile_id", self.profile_id)
        object.__setattr__(
            self,
            "challenges",
            tuple(
                item if isinstance(item, CrossChallenge) else CrossChallenge(**item)
                for item in self.challenges
            ),
        )


@dataclass(frozen=True, slots=True)
class SynthesisPoint:
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text("text", self.text)
        object.__setattr__(
            self, "evidence_ids", _id_tuple("evidence_ids", self.evidence_ids)
        )
        if not self.evidence_ids:
            raise ValueError("synthesis points require at least one evidence id")


@dataclass(frozen=True, slots=True)
class KeyEvidence:
    evidence_id: str
    rationale: str

    def __post_init__(self) -> None:
        _text("evidence_id", self.evidence_id)
        _text("rationale", self.rationale)


@dataclass(frozen=True, slots=True)
class DebateSynthesis:
    consensus: tuple[SynthesisPoint, ...]
    disagreements: tuple[SynthesisPoint, ...]
    sources_of_disagreement: tuple[SynthesisPoint, ...]
    key_evidence: tuple[KeyEvidence, ...]
    unresolved_questions: tuple[SynthesisPoint, ...]
    falsification_conditions: tuple[SynthesisPoint, ...]

    def __post_init__(self) -> None:
        for name in (
            "consensus",
            "disagreements",
            "sources_of_disagreement",
            "unresolved_questions",
            "falsification_conditions",
        ):
            object.__setattr__(
                self,
                name,
                tuple(
                    item if isinstance(item, SynthesisPoint) else SynthesisPoint(**item)
                    for item in getattr(self, name)
                ),
            )
        object.__setattr__(
            self,
            "key_evidence",
            tuple(
                item if isinstance(item, KeyEvidence) else KeyEvidence(**item)
                for item in self.key_evidence
            ),
        )


@dataclass(frozen=True, slots=True)
class DebateFailure:
    profile_id: str | None
    phase: str
    error_code: str
    message: str

    def __post_init__(self) -> None:
        _optional_text("profile_id", self.profile_id)
        for name in ("phase", "error_code", "message"):
            _text(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class PerspectiveResult:
    profile_id: str
    status: str
    interpretation: IndependentInterpretation | None = None
    cross_examination: CrossExamination | None = None
    failure: DebateFailure | None = None

    def __post_init__(self) -> None:
        _text("profile_id", self.profile_id)
        if self.status not in PERSPECTIVE_STATUSES:
            allowed = ", ".join(sorted(PERSPECTIVE_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        if self.status == "available" and self.interpretation is None:
            raise ValueError("available perspectives require an interpretation")
        if self.status == "unavailable" and self.failure is None:
            raise ValueError("unavailable perspectives require a failure")


@dataclass(frozen=True, slots=True)
class DebateResult:
    case_id: str
    question: str
    as_of: datetime
    profiles: tuple[str, ...]
    results: tuple[PerspectiveResult, ...]
    synthesis: DebateSynthesis | None
    status: str
    fallback_reason: str | None
    evidence_bundle_hash: str
    errors: tuple[DebateFailure, ...] = ()
    completed_at: datetime | None = None
    replayed: bool = False
    automatic_adjudication: bool = True

    def __post_init__(self) -> None:
        _text("case_id", self.case_id)
        _text("question", self.question)
        _aware("as_of", self.as_of)
        if self.completed_at is not None:
            _aware("completed_at", self.completed_at)
        object.__setattr__(self, "profiles", _id_tuple("profiles", self.profiles))
        object.__setattr__(
            self,
            "results",
            tuple(
                item if isinstance(item, PerspectiveResult) else PerspectiveResult(**item)
                for item in self.results
            ),
        )
        if self.synthesis is not None and not isinstance(self.synthesis, DebateSynthesis):
            raise TypeError("synthesis must be a DebateSynthesis")
        if self.status not in DEBATE_STATUSES:
            allowed = ", ".join(sorted(DEBATE_STATUSES))
            raise ValueError(f"status must be one of: {allowed}")
        _optional_text("fallback_reason", self.fallback_reason)
        _text("evidence_bundle_hash", self.evidence_bundle_hash)
        object.__setattr__(
            self,
            "errors",
            tuple(
                item if isinstance(item, DebateFailure) else DebateFailure(**item)
                for item in self.errors
            ),
        )
        if not isinstance(self.replayed, bool) or not isinstance(
            self.automatic_adjudication, bool
        ):
            raise TypeError("replayed and automatic_adjudication must be bools")
        if self.status == "no_conclusion":
            if self.synthesis is not None or not self.fallback_reason:
                raise ValueError("no_conclusion requires no synthesis and a reason")
        elif self.synthesis is None:
            raise ValueError(f"{self.status} requires a synthesis")


def _datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(_text("datetime", value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return parsed


def _statement_dict(item: DebateStatement) -> dict[str, Any]:
    return {
        "id": item.id,
        "classification": item.classification,
        "text": item.text,
        "evidence_ids": list(item.evidence_ids),
    }


def _synthesis_point_dict(item: SynthesisPoint) -> dict[str, Any]:
    return {"text": item.text, "evidence_ids": list(item.evidence_ids)}


def result_to_dict(result: DebateResult) -> dict[str, Any]:
    """Serialize a debate result to JSON-safe, stable primitives."""

    synthesis: dict[str, Any] | None = None
    if result.synthesis is not None:
        synthesis = {
            "consensus": [
                _synthesis_point_dict(item) for item in result.synthesis.consensus
            ],
            "disagreements": [
                _synthesis_point_dict(item) for item in result.synthesis.disagreements
            ],
            "sources_of_disagreement": [
                _synthesis_point_dict(item)
                for item in result.synthesis.sources_of_disagreement
            ],
            "key_evidence": [
                {"evidence_id": item.evidence_id, "rationale": item.rationale}
                for item in result.synthesis.key_evidence
            ],
            "unresolved_questions": [
                _synthesis_point_dict(item)
                for item in result.synthesis.unresolved_questions
            ],
            "falsification_conditions": [
                _synthesis_point_dict(item)
                for item in result.synthesis.falsification_conditions
            ],
        }

    return {
        "case_id": result.case_id,
        "question": result.question,
        "as_of": result.as_of.isoformat(),
        "profiles": list(result.profiles),
        "results": [
            {
                "profile_id": item.profile_id,
                "status": item.status,
                "interpretation": None
                if item.interpretation is None
                else {
                    "profile_id": item.interpretation.profile_id,
                    "statements": [
                        _statement_dict(statement)
                        for statement in item.interpretation.statements
                    ],
                },
                "cross_examination": None
                if item.cross_examination is None
                else {
                    "profile_id": item.cross_examination.profile_id,
                    "challenges": [
                        {
                            "challenge_id": challenge.challenge_id,
                            "target_profile_id": challenge.target_profile_id,
                            "target_statement_id": challenge.target_statement_id,
                            "challenge": challenge.challenge,
                            "reply": challenge.reply,
                            "withdrawn": challenge.withdrawn,
                            "evidence_ids": list(challenge.evidence_ids),
                        }
                        for challenge in item.cross_examination.challenges
                    ],
                },
                "failure": None
                if item.failure is None
                else {
                    "profile_id": item.failure.profile_id,
                    "phase": item.failure.phase,
                    "error_code": item.failure.error_code,
                    "message": item.failure.message,
                },
            }
            for item in result.results
        ],
        "synthesis": synthesis,
        "status": result.status,
        "fallback_reason": result.fallback_reason,
        "evidence_bundle_hash": result.evidence_bundle_hash,
        "errors": [
            {
                "profile_id": item.profile_id,
                "phase": item.phase,
                "error_code": item.error_code,
                "message": item.message,
            }
            for item in result.errors
        ],
        "completed_at": None
        if result.completed_at is None
        else result.completed_at.isoformat(),
        "replayed": result.replayed,
        "automatic_adjudication": result.automatic_adjudication,
    }


def result_from_dict(data: dict[str, Any]) -> DebateResult:
    """Reconstruct a ledger result without accepting untrusted extra fields."""

    required = {
        "case_id",
        "question",
        "as_of",
        "profiles",
        "results",
        "synthesis",
        "status",
        "fallback_reason",
        "evidence_bundle_hash",
        "errors",
        "completed_at",
        "replayed",
        "automatic_adjudication",
    }
    if set(data) != required:
        missing = sorted(required - set(data))
        extra = sorted(set(data) - required)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unknown: " + ", ".join(extra))
        raise ValueError("invalid debate result fields (" + "; ".join(details) + ")")

    results: list[PerspectiveResult] = []
    for raw in data["results"]:
        failure = None
        if raw["failure"] is not None:
            failure = DebateFailure(**raw["failure"])
        interpretation = None
        if raw["interpretation"] is not None:
            interpretation = IndependentInterpretation(
                profile_id=raw["interpretation"]["profile_id"],
                statements=tuple(
                    DebateStatement(**statement)
                    for statement in raw["interpretation"]["statements"]
                ),
            )
        cross = None
        if raw["cross_examination"] is not None:
            cross = CrossExamination(
                profile_id=raw["cross_examination"]["profile_id"],
                challenges=tuple(
                    CrossChallenge(**challenge)
                    for challenge in raw["cross_examination"]["challenges"]
                ),
            )
        results.append(
            PerspectiveResult(
                profile_id=raw["profile_id"],
                status=raw["status"],
                interpretation=interpretation,
                cross_examination=cross,
                failure=failure,
            )
        )

    synthesis = None
    if data["synthesis"] is not None:
        raw_synthesis = data["synthesis"]
        synthesis = DebateSynthesis(
            consensus=tuple(
                SynthesisPoint(**item) for item in raw_synthesis["consensus"]
            ),
            disagreements=tuple(
                SynthesisPoint(**item) for item in raw_synthesis["disagreements"]
            ),
            sources_of_disagreement=tuple(
                SynthesisPoint(**item)
                for item in raw_synthesis["sources_of_disagreement"]
            ),
            key_evidence=tuple(
                KeyEvidence(**item) for item in raw_synthesis["key_evidence"]
            ),
            unresolved_questions=tuple(
                SynthesisPoint(**item)
                for item in raw_synthesis["unresolved_questions"]
            ),
            falsification_conditions=tuple(
                SynthesisPoint(**item)
                for item in raw_synthesis["falsification_conditions"]
            ),
        )

    return DebateResult(
        case_id=data["case_id"],
        question=data["question"],
        as_of=_datetime(data["as_of"]),
        profiles=tuple(data["profiles"]),
        results=tuple(results),
        synthesis=synthesis,
        status=data["status"],
        fallback_reason=data["fallback_reason"],
        evidence_bundle_hash=data["evidence_bundle_hash"],
        errors=tuple(DebateFailure(**item) for item in data["errors"]),
        completed_at=None if data["completed_at"] is None else _datetime(data["completed_at"]),
        replayed=data["replayed"],
        automatic_adjudication=data["automatic_adjudication"],
    )
