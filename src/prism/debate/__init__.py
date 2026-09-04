"""Automatic multi-perspective debate (FR-5)."""

from .ledger import DebateAuditEntry, DebateLedger
from .models import (
    CrossChallenge,
    CrossExamination,
    DebateFailure,
    DebateResult,
    FollowUpResult,
    DebateStatement,
    DebateSynthesis,
    IndependentInterpretation,
    KeyEvidence,
    PerspectiveProfile,
    PerspectiveResult,
    SynthesisPoint,
    result_from_dict,
    result_to_dict,
)
from .profiles import ACADEMIC_PROFILES, DEFAULT_PROFILES
from .service import DebateService

__all__ = [
    "ACADEMIC_PROFILES",
    "CrossChallenge",
    "CrossExamination",
    "DEFAULT_PROFILES",
    "DebateAuditEntry",
    "DebateFailure",
    "DebateLedger",
    "DebateResult",
    "FollowUpResult",
    "DebateService",
    "DebateStatement",
    "DebateSynthesis",
    "IndependentInterpretation",
    "KeyEvidence",
    "PerspectiveProfile",
    "PerspectiveResult",
    "SynthesisPoint",
    "result_from_dict",
    "result_to_dict",
]
