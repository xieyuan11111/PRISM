"""Strict, source-bound structured extraction from normalized materials."""

from .service import (
    ACCUMULATION_STATUSES,
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
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionEvidenceMatch",
    "ExtractionResult",
    "ExtractionService",
    "MATERIAL_ROLES",
]
