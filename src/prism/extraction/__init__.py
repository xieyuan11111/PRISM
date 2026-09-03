"""Strict, source-bound structured extraction from normalized materials."""

from .service import (
    ExtractionConflict,
    ExtractionError,
    ExtractionEvidenceGap,
    ExtractionEvidenceMatch,
    ExtractionResult,
    ExtractionService,
    MATERIAL_ROLES,
)

__all__ = [
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionEvidenceMatch",
    "ExtractionResult",
    "ExtractionService",
    "MATERIAL_ROLES",
]
