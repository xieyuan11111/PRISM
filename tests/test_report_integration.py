"""Focused integration tests wiring the report layer through facade and CLI.

``PrismAPI.report_case`` must chain the injected ``AnalyzerService`` and
``ReportService`` without reimplementing either, the ``prism report`` shell
must only delegate to that facade method, and the whole path stays offline:
fakes stand in for the analyzer/report services and the injected API, and the
LLM is only ever reachable through an explicitly configured router that
``--no-llm`` bypasses entirely.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO
import json

import pytest

from prism.analyzer import EvolutionAnalysis, TimelineStage, TurningPoint
from prism.api import PrismAPI
from prism.cli import build_parser, handle_report, main
from prism.report import (
    SUMMARY_ORIGIN_FALLBACK,
    ReportDocument,
    ReportService,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CASE_ID = "housing-policy-2026"
AWARE_TIMESTAMP = "2026-09-01T09:00:00+00:00"


def make_analysis() -> EvolutionAnalysis:
    """One small, real analysis so real report rendering stays exercised."""

    case_stage = TimelineStage(
        episode_key="case-housing-2026",
        kind="evolution_case",
        layer="fact",
        summary="Case tracks the 2026 housing policy revision.",
        valid_at=datetime(2026, 8, 1, tzinfo=UTC),
        invalid_at=None,
        reference_time=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        source_ids=("material-case",),
    )
    publication = TimelineStage(
        episode_key="node-publication",
        kind="evolution_node",
        layer="fact",
        summary="The revised housing policy was published.",
        valid_at=datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
        invalid_at=None,
        reference_time=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        source_ids=("material-1",),
        node_type="publication",
    )
    turning = TurningPoint(
        "node-publication",
        "publication",
        datetime(2026, 8, 31, 9, 30, tzinfo=UTC),
        "The revised housing policy was published.",
        ("material-1",),
    )
    return EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type="policy",
        stages=(case_stage, publication),
        turning_points=(turning,),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
    )


def render_offline_document(analysis: EvolutionAnalysis) -> ReportDocument:
    return asyncio.run(ReportService().report(analysis))


class FakeAnalyzer:
    def __init__(self, analysis: EvolutionAnalysis, journal: list[str]) -> None:
        self.analysis = analysis
        self.journal = journal
        self.calls: list[tuple[object, object]] = []

    async def analyze(self, case_id, as_of=None, *, kinds=None):
        self.calls.append((case_id, as_of))
        self.journal.append("analyze")
        return self.analysis


class FakeReportService:
    def __init__(self, document: ReportDocument, journal: list[str]) -> None:
        self.document = document
        self.journal = journal
        self.calls: list[object] = []

    async def report(self, analysis):
        self.calls.append(analysis)
        self.journal.append("report")
        return self.document


class StubIngestion:
    def ingest(self, path, metadata=None):
        return object()


class StubStore:
    def index_file(self, path):
        return object()

    def search(self, criteria, *, limit=50, offset=0):
        return []


class StubGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("timeline is not part of the report path")

    async def add_case(self, case, **bundle):
        raise AssertionError("add_case is not part of the report path")


class StubBus:
    async def publish(self, event):
        raise AssertionError("publish is not part of the report path")


def make_api(**optional) -> PrismAPI:
    return PrismAPI(StubIngestion(), StubStore(), StubGraph(), StubBus(), **optional)


def run_cli(argv, api):
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(main(argv, api=api, stdout=stdout, stderr=stderr))
    return status, stdout.getvalue(), stderr.getvalue()


class FakeReportAPI:
    """Injected CLI facade whose report_case returns a real document."""

    def __init__(self, document: ReportDocument) -> None:
        self.document = document
        self.calls: list[tuple[object, object, object]] = []

    async def report_case(self, case_id, as_of=None, use_llm=True):
        self.calls.append((case_id, as_of, use_llm))
        return self.document


def test_report_case_chains_analyzer_then_report_and_preserves_document():
    journal: list[str] = []
    analysis = make_analysis()
    document = render_offline_document(analysis)
    analyzer = FakeAnalyzer(analysis, journal)
    reports = FakeReportService(document, journal)
    api = make_api(analyzer_service=analyzer, report_service=reports)

    result = asyncio.run(api.report_case(CASE_ID, AS_OF))

    assert result is document
    assert journal == ["analyze", "report"]
    assert analyzer.calls == [(CASE_ID, AS_OF)]
    assert reports.calls == [analysis]
    assert reports.calls[0] is analysis


def test_report_case_defaults_pass_case_id_and_none_as_of():
    journal: list[str] = []
    analysis = make_analysis()
    analyzer = FakeAnalyzer(analysis, journal)
    reports = FakeReportService(render_offline_document(analysis), journal)
    api = make_api(analyzer_service=analyzer, report_service=reports)

    assert asyncio.run(api.report_case(CASE_ID)) is reports.document
    assert analyzer.calls == [(CASE_ID, None)]


def test_report_case_reports_missing_optional_dependencies_clearly():
    with pytest.raises(ValueError, match="analyzer_service is required"):
        asyncio.run(make_api().report_case(CASE_ID))

    analysis = make_analysis()
    api = make_api(analyzer_service=FakeAnalyzer(analysis, []))
    with pytest.raises(ValueError, match="report_service is required"):
        asyncio.run(api.report_case(CASE_ID, AS_OF))


def test_invalid_optional_report_dependencies_are_rejected_up_front():
    with pytest.raises(TypeError, match="analyzer_service must provide analyze"):
        make_api(analyzer_service=object())
    with pytest.raises(TypeError, match="report_service must provide report"):
        make_api(report_service=object())


def test_report_case_use_llm_false_bypasses_the_configured_report_service():
    journal: list[str] = []
    analysis = make_analysis()
    analyzer = FakeAnalyzer(analysis, journal)
    configured = FakeReportService(render_offline_document(analysis), journal)
    api = make_api(analyzer_service=analyzer, report_service=configured)

    first = asyncio.run(api.report_case(CASE_ID, AS_OF, use_llm=False))
    second = asyncio.run(api.report_case(CASE_ID, AS_OF, use_llm=False))

    assert journal == ["analyze", "analyze"]
    assert configured.calls == []
    assert isinstance(first, ReportDocument)
    assert first == second
    assert first.case_id == CASE_ID
    assert first.summary.origin == SUMMARY_ORIGIN_FALLBACK
    assert first.markdown.startswith(f"# Evolution Report: {CASE_ID}")
    assert first.citations
    assert {"material-case", "material-1"} <= {
        citation.source_id for citation in first.citations
    }


def test_legacy_positional_construction_still_works_and_reports_still_optional():
    api = PrismAPI(StubIngestion(), StubStore(), StubGraph(), StubBus())

    assert asyncio.run(api.search("anything")) == []
    with pytest.raises(ValueError, match="analyzer_service is required"):
        asyncio.run(api.report_case(CASE_ID))


def test_build_parser_exposes_report_subcommand_and_public_handler():
    parser = build_parser()

    args = parser.parse_args(["report", CASE_ID, "--as-of", AWARE_TIMESTAMP])
    assert args.handler is handle_report
    assert args.case_id == CASE_ID
    assert args.as_of == AS_OF
    assert args.no_llm is False

    defaults = parser.parse_args(["report", CASE_ID])
    assert defaults.as_of is None
    assert defaults.no_llm is False

    offline = parser.parse_args(["report", CASE_ID, "--no-llm"])
    assert offline.no_llm is True


def test_cli_report_delegates_to_facade_and_prints_deterministic_json():
    api = FakeReportAPI(render_offline_document(make_analysis()))
    argv = ["report", CASE_ID, "--as-of", AWARE_TIMESTAMP]

    status, stdout, stderr = run_cli(argv, api)
    _, stdout_again, _ = run_cli(argv, api)

    assert status == 0
    assert stderr == ""
    assert api.calls == [(CASE_ID, AS_OF, True), (CASE_ID, AS_OF, True)]
    payload = json.loads(stdout)
    assert {"markdown", "summary", "citations"} <= set(payload)
    assert payload["case_id"] == CASE_ID
    assert payload["as_of"] == AWARE_TIMESTAMP
    assert payload["summary"]["origin"] == SUMMARY_ORIGIN_FALLBACK
    assert payload["markdown"].startswith(f"# Evolution Report: {CASE_ID}")
    assert payload["citations"]
    assert {"source_id", "episode_keys"} <= set(payload["citations"][0])
    assert stdout_again == stdout


def test_cli_report_without_as_of_delegates_none_and_no_llm_disables_llm():
    api = FakeReportAPI(render_offline_document(make_analysis()))

    status, stdout, stderr = run_cli(["report", CASE_ID], api)
    assert status == 0
    assert api.calls == [(CASE_ID, None, True)]

    status, stdout, stderr = run_cli(["report", CASE_ID, "--no-llm"], api)
    assert status == 0
    assert stderr == ""
    assert api.calls[-1] == (CASE_ID, None, False)
    assert {"markdown", "summary", "citations"} <= set(json.loads(stdout))


def test_cli_report_naive_timestamp_is_a_usage_error_before_calling_api():
    api = FakeReportAPI(render_offline_document(make_analysis()))

    status, stdout, stderr = run_cli(
        ["report", CASE_ID, "--as-of", "2026-09-01T09:00:00"], api
    )

    assert status == 2
    assert stdout == ""
    assert "timezone-aware" in stderr
    assert api.calls == []


def test_cli_report_missing_dependency_is_a_clean_runtime_error():
    class MissingReportAPI:
        async def report_case(self, case_id, as_of=None, use_llm=True):
            raise ValueError("analyzer_service is required for report_case()")

    status, stdout, stderr = run_cli(["report", CASE_ID], MissingReportAPI())

    assert status == 1
    assert stdout == ""
    error = json.loads(stderr)["error"]
    assert error["type"] == "ValueError"
    assert "analyzer_service is required" in error["message"]


def test_cli_default_runtime_stays_offline_and_renders_fallback_report(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRISM_HOME", str(tmp_path / "cli-report-home"))
    stdout = StringIO()
    stderr = StringIO()

    status = asyncio.run(
        main(["report", CASE_ID, "--no-llm"], stdout=stdout, stderr=stderr)
    )

    assert status == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["case_id"] == CASE_ID
    assert payload["summary"]["origin"] == SUMMARY_ORIGIN_FALLBACK
    assert "The recorded timeline is empty." in payload["markdown"]
