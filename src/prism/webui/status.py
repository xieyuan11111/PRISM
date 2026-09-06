"""Small, honest status projections shared by the optional WebUI pages."""

from __future__ import annotations

import re
from typing import Any

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def safe_error_text(operation: str, error: BaseException) -> str:
    """Return a short error label without rendering provider/error details."""
    if not isinstance(operation, str) or not operation.strip():
        operation = "operation"
    return f"{operation.strip()} failed ({type(error).__name__})"


def safe_identifier(value: object) -> str | None:
    """Return a short opaque identifier suitable for a UI message."""
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def pipeline_ui_status(value: object) -> str:
    """Map a pipeline status to a user-facing lifecycle status."""
    return {
        "pending": "loading",
        "running": "loading",
        "completed": "success",
        "failed": "failure",
    }.get(value, "ready")


def lifecycle_ui_status(value: object) -> str:
    """Map a material lifecycle outcome to a user-facing status.

    ``committed`` is the only state that maps to success (the pipeline
    machinery finished); ``pending`` stays loading, ``failed`` stays
    failure and anything unrecognized — including ``unknown`` — stays
    unknown, never a success (WB-3.4/WB-3.6).
    """
    return {
        "committed": "success",
        "failed": "failure",
        "pending": "loading",
    }.get(value, "unknown")


def outcome_status(result: object) -> dict[str, Any]:
    """Project available quality layers without inventing semantic verdicts."""
    pipeline = getattr(result, "pipeline", None)
    pipeline_status = getattr(pipeline, "status", None)
    mechanism = getattr(result, "mechanism_status", None)
    if mechanism is None:
        mechanism = {
            "completed": "pass",
            "failed": "fail",
        }.get(pipeline_status, "unknown")
    semantic = getattr(result, "semantic_status", None) or "unknown"
    gap_count = getattr(result, "evidence_gap_count", None)
    if isinstance(gap_count, bool) or not isinstance(gap_count, int):
        gap_count = None
    gap_summary = (
        f"{gap_count} evidence gap(s)"
        if gap_count is not None
        else "not provided"
    )
    return {
        "ui_status": pipeline_ui_status(pipeline_status),
        "mechanism_status": str(mechanism),
        "semantic_status": str(semantic),
        "evidence_gap_count": gap_count,
        "evidence_gap_summary": gap_summary,
    }