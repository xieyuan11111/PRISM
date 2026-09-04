from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import StringIO
from pathlib import Path
import subprocess
import tempfile

import pytest

pytest.importorskip("pypdf")
from pypdf import PdfReader

from prism.analyzer import EvolutionAnalysis, TimelineStage
from prism.api import PrismAPI
from prism.cli import build_parser, handle_report_version, main
from prism.config import PathConfig
from prism.report import ReportDocument, ReportService
from prism.report.ledger import ReportVersionLedger
from prism.report.pdf import (
    EdgePdfRenderer,
    ReportPdfExporter,
    ReportPdfPathError,
    ReportPdfRendererError,
    ReportPdfValidationError,
    render_report_html,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
CASE_ID = "case-pdf"
OUTPUT_RELATIVE = "reports/case-pdf.pdf"


def make_paths(tmp_path: Path) -> PathConfig:
    return PathConfig(data_dir=tmp_path / "data").resolve(tmp_path)


def make_analysis() -> EvolutionAnalysis:
    stage = TimelineStage(
        episode_key="case-pdf",
        kind="evolution_case",
        layer="fact",
        summary="政策已发布，时间线包含中文文本。",
        valid_at=datetime(2026, 8, 30, tzinfo=UTC),
        invalid_at=None,
        reference_time=datetime(2026, 8, 30, tzinfo=UTC),
        source_ids=("mat-pdf",),
    )
    return EvolutionAnalysis(
        case_id=CASE_ID,
        as_of=AS_OF,
        case_type="policy",
        stages=(stage,),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
    )


def make_document() -> ReportDocument:
    document = asyncio.run(ReportService().report(make_analysis()))
    extra = (
        "\n## Debate Interpretation\n\n"
        "- 解释：政策与时间线存在中文证据。\n\n"
        "| 指标 | 数值 |\n"
        "| --- | --- |\n"
        "| 发布 | 已完成 |\n\n"
        "```python\n"
        "print('中文代码')\n"
        "```\n"
        "- Raw HTML probe: <script>alert('secret')</script>\n"
    )
    return replace(document, markdown=document.markdown + extra)


def read_pdf(path: Path) -> tuple[int, str]:
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return len(reader.pages), text


def edge_executable() -> Path | None:
    pytest.importorskip("markdown")
    from prism.report.pdf import find_chromium_executable

    executable = find_chromium_executable()
    if executable is None:
        pytest.skip("no Edge or compatible Chromium renderer found")
    return executable


class CapturingRenderer:
    def __init__(self) -> None:
        self.html: str | None = None

    def render(self, html: str, output_path: Path) -> None:
        self.html = html
        raise RuntimeError("stop after HTML capture")


class MustNotRender:
    def render(self, html: str, output_path: Path) -> None:
        raise AssertionError("renderer must not be called")


def test_renderer_seam_is_injectable_and_failure_creates_no_pdf(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    renderer = CapturingRenderer()
    exporter = ReportPdfExporter(paths, renderer=renderer)
    document = make_document()

    with pytest.raises(RuntimeError, match="stop after HTML capture"):
        exporter.export_document(document, OUTPUT_RELATIVE)

    assert renderer.html is not None
    assert "<script>alert(" not in renderer.html
    assert "&lt;script&gt;alert(" in renderer.html
    assert "<h1>" in renderer.html
    assert "<table>" in renderer.html
    assert "<pre><code" in renderer.html
    assert "政策已发布，时间线包含中文文本。" in renderer.html
    assert "file:///" not in renderer.html
    assert not (paths.output_dir / "reports").exists()


def test_missing_renderer_fails_clearly_and_creates_no_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prism.report import pdf as pdf_module

    paths = make_paths(tmp_path)
    monkeypatch.setattr(pdf_module, "find_chromium_executable", lambda: None)
    exporter = ReportPdfExporter(paths)

    with pytest.raises(
        ReportPdfRendererError,
        match="no PDF renderer.*PRISM_PDF_RENDERER.*Microsoft Edge or a compatible Chromium",
    ):
        exporter.export_document(make_document(), OUTPUT_RELATIVE)

    assert not (paths.output_dir / "reports").exists()


@pytest.mark.parametrize(
    "output_path",
    ["../outside.pdf", "nested/../../outside.pdf", "not-pdf.txt", "."],
)
def test_export_rejects_unsafe_output_paths_before_rendering(
    tmp_path: Path, output_path: str
) -> None:
    paths = make_paths(tmp_path)
    exporter = ReportPdfExporter(paths, renderer=MustNotRender())

    with pytest.raises((ReportPdfPathError, ValueError)):
        exporter.export_document(make_document(), output_path)

    with pytest.raises((ReportPdfPathError, ValueError)):
        exporter.export_document(
            make_document(), str(tmp_path / "absolute.pdf")
        )


def test_edge_export_validates_content_metadata_and_paths(tmp_path: Path) -> None:
    executable = edge_executable()
    paths = make_paths(tmp_path)
    exporter = ReportPdfExporter(paths, renderer=EdgePdfRenderer(executable))
    document = make_document()

    result = exporter.export_pdf(document, OUTPUT_RELATIVE)

    assert result.path == paths.output_dir / OUTPUT_RELATIVE
    assert result.path.is_file()
    assert result.version_id is None
    assert result.case_id == CASE_ID
    assert result.as_of == AS_OF
    assert result.markdown_hash == hashlib.sha256(
        document.markdown.encode("utf-8")
    ).hexdigest()
    assert result.pdf_hash == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert result.page_count >= 1

    page_count, text = read_pdf(result.path)
    assert page_count == result.page_count
    normalized_text = "".join(text.split())
    for expected in (
        f"Evolution Report: {CASE_ID}",
        "As of:",
        "Executive Summary",
        "Debate Interpretation",
        "Timeline Stages",
        "Citations",
        "政策已发布，时间线包含中文文本。",
        "中文代码",
    ):
        assert "".join(expected.split()) in normalized_text

    payload = result.path.read_bytes()
    reader = PdfReader(result.path)
    assert reader.metadata is not None
    assert reader.metadata.get("/Producer") == "PRISM"
    assert reader.metadata.get("/Creator") == "PRISM"
    assert reader.metadata.get("/PRISMMarkdownHash") == result.markdown_hash
    assert b"file:///" not in payload.lower()
    for private_path in (tmp_path, Path(tempfile.gettempdir())):
        assert str(private_path).encode("utf-8") not in payload
        assert private_path.as_posix().encode("utf-8") not in payload


def test_report_version_export_is_idempotent_and_refuses_different_content(
    tmp_path: Path,
) -> None:
    executable = edge_executable()
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        analysis = make_analysis()
        document = make_document()
        version = ledger.save(document, analysis, trigger="initial")
        before = ledger.get(version.version_id)
        versions_before = ledger.versions()
        exporter = ReportPdfExporter(
            paths, renderer=EdgePdfRenderer(executable)
        )

        first = exporter.export_version(version, OUTPUT_RELATIVE)
        marker = first.path.stat().st_mtime_ns
        second = exporter.export_version(version, OUTPUT_RELATIVE)

        assert second.path == first.path
        assert second.version_id == version.version_id
        assert second.markdown_hash == version.markdown_hash
        assert second.pdf_hash == first.pdf_hash
        assert second.page_count == first.page_count
        assert first.path.stat().st_mtime_ns == marker
        assert ledger.get(version.version_id) == before
        assert ledger.versions() == versions_before

        first.path.write_bytes(b"not a pdf")
        with pytest.raises(FileExistsError, match="refusing to overwrite"):
            exporter.export_version(version, OUTPUT_RELATIVE)
        assert first.path.read_bytes() == b"not a pdf"
    finally:
        ledger.close()


def test_report_service_and_ledger_delegate_to_pdf_exporter(tmp_path: Path) -> None:
    executable = edge_executable()
    paths = make_paths(tmp_path)
    document = make_document()
    analysis = make_analysis()

    service = ReportService(paths=paths)
    service_result = service.export_pdf(document, OUTPUT_RELATIVE)
    assert service_result.path == paths.output_dir / OUTPUT_RELATIVE

    ledger = ReportVersionLedger(paths)
    try:
        version = ledger.save(document, analysis, trigger="initial")
        result = ledger.export_pdf(version.version_id, "reports/version.pdf")
        assert result.version_id == version.version_id
        assert result.path == paths.output_dir / "reports" / "version.pdf"
        assert result.path.is_file()
    finally:
        ledger.close()


class DummyIngestion:
    def ingest(self, path, metadata=None):
        return None


class DummyStore:
    def index_file(self, path):
        return None

    def search(self, criteria, *, limit, offset):
        return ()


class DummyGraph:
    async def timeline(self, case_id, as_of):
        return None

    async def add_case(self, case, **bundle):
        return None


class DummyBus:
    async def publish(self, event):
        return None


def test_api_export_report_pdf_delegates_to_version_ledger(tmp_path: Path) -> None:
    executable = edge_executable()
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        document = make_document()
        version = ledger.save(document, make_analysis(), trigger="initial")
        api = PrismAPI(
            DummyIngestion(),
            DummyStore(),
            DummyGraph(),
            DummyBus(),
            report_version_service=ledger,
        )

        result = asyncio.run(api.export_report_pdf(version.version_id, "reports/api.pdf"))

        assert result.version_id == version.version_id
        assert result.path == paths.output_dir / "reports" / "api.pdf"
        assert result.path.is_file()
    finally:
        ledger.close()


def test_cli_report_version_pdf_option_delegates_to_api() -> None:
    class PDFCLIAPI:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

        async def export_report_pdf(self, version_id, output_path):
            self.calls.append(
                ("export_report_pdf", (version_id, output_path), {})
            )
            return {
                "path": Path("output/reports/cli.pdf"),
                "version_id": version_id,
                "markdown_hash": "a" * 64,
                "pdf_hash": "b" * 64,
                "page_count": 1,
            }

    args = build_parser().parse_args(
        ["report-version", "rv_cli", "--pdf", "reports/cli.pdf"]
    )
    assert args.handler is handle_report_version

    api = PDFCLIAPI()
    stdout = StringIO()
    stderr = StringIO()
    status = asyncio.run(
        main(
            ["report-version", "rv_cli", "--pdf", "reports/cli.pdf"],
            api=api,
            stdout=stdout,
            stderr=stderr,
        )
    )

    assert status == 0
    assert stderr.getvalue() == ""
    assert api.calls == [("export_report_pdf", ("rv_cli", "reports/cli.pdf"), {})]
    assert '"version_id":"rv_cli"' in stdout.getvalue()


def test_rendered_html_forbids_external_resource_loading() -> None:
    rendered = render_report_html(
        "# 标题\n\n"
        "![probe](file:///C:/Users/secret/shot.png)\n"
        "![beacon](http://127.0.0.1:9/px.png)\n",
        "case-csp",
    )

    assert 'http-equiv="Content-Security-Policy"' in rendered
    assert "default-src 'none'" in rendered
    assert "style-src 'unsafe-inline'" in rendered
    # Markdown image syntax still parses into an <img> tag; the policy is
    # what forbids headless Chromium from fetching the referenced resource.
    assert "<img" in rendered


def test_edge_renderer_prints_with_an_isolated_ephemeral_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prism.report import pdf as pdf_module

    fake_browser = tmp_path / "fake-chromium.exe"
    fake_browser.write_bytes(b"")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        for flag in command:
            if flag.startswith("--print-to-pdf="):
                Path(flag.split("=", 1)[1]).write_bytes(b"%PDF-fake")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
    output = tmp_path / "out" / "report.pdf"
    EdgePdfRenderer(fake_browser).render("<p>x</p>", output)

    assert output.read_bytes() == b"%PDF-fake"
    command = commands[0]
    assert "--no-pdf-header-footer" in command
    user_data_dirs = [
        flag.split("=", 1)[1]
        for flag in command
        if flag.startswith("--user-data-dir=")
    ]
    assert len(user_data_dirs) == 1
    assert "prism-report-pdf-" in str(Path(user_data_dirs[0]))


def test_validate_pdf_text_accepts_a_non_utc_cutoff_after_ledger_reload() -> None:
    from prism.report.pdf import _validate_pdf_text

    markdown = (
        "# Evolution Report: case-tz\n\n"
        "- As of: 2026-09-01T00:00:00+08:00\n\n"
        "## Executive Summary\n\n政策已发布。\n"
    )
    # ReportVersionLedger persists as_of UTC-normalized, so a reopened
    # ledger hands the exporter the UTC instant while the stored Markdown
    # keeps the writer's original +08:00 spelling of the same instant.
    reloaded_as_of = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    pdf_text = (
        "Evolution Report: case-tz\n"
        "As of: 2026-09-01T00:00:00+08:00\n"
        "Executive Summary Timeline Stages Citations 政策已发布"
    )

    _validate_pdf_text(
        "".join(pdf_text.split()), markdown, "case-tz", reloaded_as_of
    )


def test_validate_pdf_text_rejects_a_markdown_as_of_from_a_different_instant() -> None:
    from prism.report.pdf import _validate_pdf_text

    markdown = (
        "# Evolution Report: case-tz\n\n"
        "- As of: 2020-01-01T00:00:00+00:00\n\n"
        "## Executive Summary\n\n摘要\n"
    )
    as_of = datetime(2026, 9, 1, tzinfo=UTC)
    pdf_text = (
        "Evolution Report: case-tz\n"
        "As of: 2020-01-01T00:00:00+00:00\n"
        "Executive Summary Timeline Stages Citations 摘要"
    )

    with pytest.raises(ReportPdfValidationError):
        _validate_pdf_text("".join(pdf_text.split()), markdown, "case-tz", as_of)


def test_validate_pdf_text_requires_cjk_text_when_markdown_has_cjk() -> None:
    from prism.report.pdf import _validate_pdf_text

    markdown = (
        "# Evolution Report: case-cjk\n\n"
        f"- As of: {AS_OF.isoformat()}\n\n"
        "## Executive Summary\n\n政策已发布，时间线包含中文文本。\n"
    )
    latin_only = (
        f"Evolution Report: case-cjk\nAs of: {AS_OF.isoformat()}\n"
        "Executive Summary Timeline Stages Citations"
    )
    complete = latin_only + "\n政策已发布，时间线包含中文文本。"

    with pytest.raises(
        ReportPdfValidationError, match="failed text validation"
    ):
        _validate_pdf_text(
            "".join(latin_only.split()), markdown, "case-cjk", AS_OF
        )

    _validate_pdf_text("".join(complete.split()), markdown, "case-cjk", AS_OF)


def test_reopened_ledger_exports_the_saved_version_association(
    tmp_path: Path,
) -> None:
    executable = edge_executable()
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        version = ledger.save(make_document(), make_analysis(), trigger="initial")
    finally:
        ledger.close()

    reopened = ReportVersionLedger(paths)
    try:
        result = reopened.export_pdf(version.version_id, OUTPUT_RELATIVE)
    finally:
        reopened.close()

    assert result.version_id == version.version_id
    assert result.markdown_hash == version.markdown_hash
    assert result.path == paths.output_dir / OUTPUT_RELATIVE
    assert result.path.is_file()
