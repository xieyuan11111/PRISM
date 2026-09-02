"""Strict, source-bound structured extraction from normalized materials."""

from .service import (
    ExtractionConflict,
    ExtractionError,
    ExtractionEvidenceGap,
    ExtractionResult,
    ExtractionService,
)

__all__ = [
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionResult",
    "ExtractionService",
]
