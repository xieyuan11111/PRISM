"""Strict, source-bound structured extraction from normalized materials."""

from .profiles import (
    BASELINE_PROMPT_PROFILE,
    KNOWN_PROMPT_PROFILES,
    PROTOCOL_V1_PROFILE,
    build_profiled_prompt,
    normalize_prompt_profile,
)
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
    "BASELINE_PROMPT_PROFILE",
    "GAP_PAYLOAD_EVIDENCE_FIELDS",
    "GAP_PAYLOAD_FIELDS",
    "ExtractionConflict",
    "ExtractionError",
    "ExtractionEvidenceGap",
    "ExtractionEvidenceMatch",
    "ExtractionResult",
    "ExtractionService",
    "KNOWN_PROMPT_PROFILES",
    "MATERIAL_ROLES",
    "PROTOCOL_V1_PROFILE",
    "build_profiled_prompt",
    "normalize_prompt_profile",
]
