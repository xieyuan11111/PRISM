from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class AdjudicationDecision(str, Enum):
    ACCEPTED = "accepted"
    REVISED = "revised"
    REJECTED = "rejected"
    PRESERVE_CONFLICT = "preserve_conflict"
    AWAITING_CASE_BINDING = "awaiting_case_binding"
    #: Whole-batch failure outcome: the LLM adjudication batch failed as a
    #: whole (invalid completion, malformed decisions, missing role,
    #: transport failure) and no model decision was applied.  The model can
    #: never emit this value, but it is a member so every audit record —
    #: per-candidate and batch-level — carries one uniform
    #: :class:`AdjudicationDecision` whose ``.value`` consumers can rely on.
    ADJUDICATION_FAILED = "adjudication_failed"


#: Candidate kind of a batch-level (not per-candidate) audit record: the
#: whole LLM adjudication batch failed and no model decision was applied.
BATCH_CANDIDATE_KIND = "batch"

#: Stable candidate id used by batch-level audit records.
BATCH_CANDIDATE_ID = "adjudication_batch"

#: Alias of :attr:`AdjudicationDecision.ADJUDICATION_FAILED`, kept for
#: callers of the pre-enum plain-string name.  It IS the enum member, so
#: audit decisions never degrade to a bare string.
ADJUDICATION_FAILED_OUTCOME = AdjudicationDecision.ADJUDICATION_FAILED


#: Canonical, secret-free summaries for every structured batch-failure code.
#: These strings are the ONLY model-independent text a batch failure carries;
#: raw completion content never appears in a failure or its audit record.
_BATCH_FAILURE_SUMMARIES = {
    "invalid_completion": (
        "adjudication completion did not provide a text string"
    ),
    "invalid_json": "adjudication completion is not valid JSON",
    "duplicate_json_key": (
        "adjudication completion contains duplicate JSON keys"
    ),
    "invalid_json_constant": (
        "adjudication completion contains non-finite JSON constants "
        "(NaN or Infinity)"
    ),
    "invalid_payload": (
        "adjudication completion must be one JSON object containing only "
        "the decisions array"
    ),
    "invalid_decision": "every decisions entry must be a JSON object",
    "missing_decision_fields": (
        "decision object is missing one or more required fields "
        "(candidate_kind, candidate_id, decision, reason)"
    ),
    "unknown_decision_fields": (
        "decision object contains fields outside candidate_kind, "
        "candidate_id, decision, reason, revised_payload"
    ),
    "invalid_decision_kind": (
        "candidate_kind must be one of node, temporal_fact, claim, "
        "conflict, relation"
    ),
    "invalid_candidate_id": "candidate_id must be a non-empty string",
    "invalid_decision_value": (
        "decision must be one of accepted, revised, rejected, "
        "preserve_conflict, awaiting_case_binding"
    ),
    "invalid_reason": "reason must be a non-empty string",
    "invalid_revised_payload": "revised_payload must be a JSON object",
    "missing_revised_payload": (
        "a revised decision must carry a revised_payload object"
    ),
    "duplicate_decision_target": (
        "the same candidate is adjudicated more than once"
    ),
    "unknown_candidate_identity": (
        "a decision names a candidate that is not part of the verified "
        "extraction"
    ),
    "invalid_preserve_conflict": (
        "preserve_conflict decisions are valid only for conflict candidates"
    ),
    "missing_role": (
        "the adjudicate task role has no configured LLM route"
    ),
    "transport_failure": (
        "the adjudication LLM request failed at the role or transport layer"
    ),
}


class AdjudicationBatchFailure(Exception):
    """Structured, safe failure of one whole LLM adjudication batch.

    Carries only the error type (``error_code``), the offending field path
    (``field``, when one exists) and a canonical message — never the model's
    raw completion text, secrets, or absolute paths — so a fail-open caller
    can audit the failure and keep the already-verified first-layer
    extraction without ever echoing the untrusted completion.
    """

    def __init__(
        self,
        error_code: str,
        *,
        field: str | None = None,
        message: str | None = None,
    ) -> None:
        if not isinstance(error_code, str) or not error_code.strip():
            raise TypeError("error_code must be a non-empty string")
        if field is not None and (
            not isinstance(field, str) or not field.strip()
        ):
            raise TypeError("field must be a non-empty string or None")
        if message is not None and (
            not isinstance(message, str) or not message.strip()
        ):
            raise TypeError("message must be a non-empty string or None")
        if error_code not in _BATCH_FAILURE_SUMMARIES:
            raise ValueError(
                f"unknown adjudication batch failure code: {error_code!r}"
            )
        self.error_code = error_code
        self.field = field
        self.message = message or _BATCH_FAILURE_SUMMARIES[error_code]
        composed = (
            f"{error_code}"
            + (f" at {field}" if field is not None else "")
            + f": {self.message}"
        )
        super().__init__(composed)


@dataclass(frozen=True, slots=True)
class AdjudicationItem:
    candidate_kind: str
    candidate_id: str
    decision: AdjudicationDecision
    reason: str
    revised_payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("candidate_kind", "candidate_id", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        object.__setattr__(self, "decision", AdjudicationDecision(self.decision))
        if self.revised_payload is not None:
            if not isinstance(self.revised_payload, Mapping):
                raise TypeError("revised_payload must be a mapping")
            object.__setattr__(self, "revised_payload", MappingProxyType(dict(self.revised_payload)))
        if self.decision is AdjudicationDecision.REVISED and self.revised_payload is None:
            raise ValueError("revised decision requires revised_payload")


@dataclass(frozen=True, slots=True)
class AuditRecord:
    decision_id: str
    material_id: str
    candidate_kind: str
    candidate_id: str
    original_payload_hash: str
    original_payload: Mapping[str, Any]
    validation_failures: tuple[str, ...]
    decision: AdjudicationDecision
    reason: str
    revised_payload: Mapping[str, Any] | None
    model_role: str
    decided_at: datetime
    revalidation_outcome: str
    graph_episode_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("decision_id", "material_id", "candidate_kind", "candidate_id", "original_payload_hash", "reason", "model_role", "revalidation_outcome"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        # Normalize every decision — batch-level 'adjudication_failed'
        # included — to the formal enum; unknown values raise instead of
        # silently leaving a bare string behind.
        object.__setattr__(self, "decision", AdjudicationDecision(self.decision))
        if not isinstance(self.original_payload, Mapping):
            raise TypeError("original_payload must be a mapping")
        object.__setattr__(self, "original_payload", MappingProxyType(dict(self.original_payload)))
        object.__setattr__(self, "validation_failures", tuple(self.validation_failures))
        object.__setattr__(self, "graph_episode_keys", tuple(self.graph_episode_keys))
        if self.revised_payload is not None:
            if not isinstance(self.revised_payload, Mapping):
                raise TypeError("revised_payload must be a mapping")
            object.__setattr__(self, "revised_payload", MappingProxyType(dict(self.revised_payload)))
        if not isinstance(self.decided_at, datetime) or self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    extraction: Any
    audits: tuple[AuditRecord, ...] = ()
    applied: bool = False
