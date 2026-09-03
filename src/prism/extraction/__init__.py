"""Strict, source-bound structured extraction from normalized materials."""

from .service import (
    ExtractionConflict,
    ExtractionError,
    ExtractionEvidenceGap,
    ExtractionEvidenceMatch,
    ExtractionResult,
    ExtractionService,
)

__all__ = [
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionEvidenceMatch",
    "ExtractionResult",
    "ExtractionService",
]
