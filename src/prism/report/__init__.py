"""Deterministic Markdown evolution reports with traceable summaries (FR-6)."""

from .ledger import ReportVersion, ReportVersionLedger
from .pdf import (
    EdgePdfRenderer,
    PdfRenderer,
    ReportPdfConflictError,
    ReportPdfExportResult,
    ReportPdfExporter,
    ReportPdfPathError,
    ReportPdfRendererError,
    ReportPdfValidationError,
    find_chromium_executable,
    render_report_html,
)
from .models import (
    REPORT_LANGUAGES,
    REPORT_LANGUAGE_EN,
    REPORT_LANGUAGE_ZH_CN,
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
    "REPORT_LANGUAGES",
    "REPORT_LANGUAGE_EN",
    "REPORT_LANGUAGE_ZH_CN",
    "ReportCitation",
    "ReportDocument",
    "ReportService",
    "ReportSummary",
    "ReportVersion",
    "ReportVersionLedger",
    "EdgePdfRenderer",
    "PdfRenderer",
    "ReportPdfConflictError",
    "ReportPdfExportResult",
    "ReportPdfExporter",
    "ReportPdfPathError",
    "ReportPdfRendererError",
    "ReportPdfValidationError",
    "find_chromium_executable",
    "render_report_html",
]
