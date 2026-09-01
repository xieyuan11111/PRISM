"""Deterministic Markdown evolution reports with traceable summaries (FR-6)."""

from .models import (
    SUMMARY_ORIGINS,
    SUMMARY_ORIGIN_FALLBACK,
    SUMMARY_ORIGIN_LLM,
    ReportCitation,
    ReportDocument,
    ReportSummary,
)
from .service import SUMMARIZE_REPORT_ROLE, ReportService

__all__ = [
    "SUMMARIZE_REPORT_ROLE",
    "SUMMARY_ORIGINS",
    "SUMMARY_ORIGIN_FALLBACK",
    "SUMMARY_ORIGIN_LLM",
    "ReportCitation",
    "ReportDocument",
    "ReportService",
    "ReportSummary",
]
