"""Strict, source-bound structured extraction from normalized materials."""

from .service import (
    ACCUMULATION_STATUSES,
    GAP_PAYLOAD_EVIDENCE_FIELDS,
    GAP_PAYLOAD_FIELDS,
    ExtractionConflict,
    ExtractionError,
    ExtractionEvidenceGap,
    ExtractionEvidenceMatch,
    ExtractionResult,
    ExtractionService,
    MATERIAL_ROLES,
)

__all__ = [
    "ACCUMULATION_STATUSES",
    "GAP_PAYLOAD_EVIDENCE_FIELDS",
    "GAP_PAYLOAD_FIELDS",
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionEvidenceMatch",
    "ExtractionResult",
    "ExtractionService",
    "MATERIAL_ROLES",
]
