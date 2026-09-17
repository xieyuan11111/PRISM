from __future__ import annotations

import asyncio
from collections import Counter
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
        "中文代码",
    ):
        assert "".join(expected.split()) in normalized_text
    # The timeline table now keeps every column inside the printable page
    # (the no-shrink fix), so its CJK summary cell legitimately wraps and
    # pypdf interleaves the wrapped lines with neighbouring cells.  The
    # guarantee that matters is character-complete, extractable CJK: every
    # character of the summary must reach the text layer, and the exporter's
    # own read-back validation (which gates the longest Han run) passed when
    # the export succeeded above.
    summary = "政策已发布，时间线包含中文文本。"
    for character, needed in Counter(summary).items():
        assert normalized_text.count(character) >= needed, character

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
    edge_executable()
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
    edge_executable()
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
    edge_executable()
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


# --- Chinese (zh-CN) report PDFs ------------------------------------------------


ZH_CASE_ID = "case-pdf-zh"


def make_zh_analysis() -> EvolutionAnalysis:
    stage = TimelineStage(
        episode_key="node-w30",
        kind="evolution_node",
        layer="fact",
        summary="W30 全文证据已入图。",
        valid_at=datetime(2026, 8, 20, tzinfo=UTC),
        invalid_at=None,
        reference_time=datetime(2026, 8, 20, tzinfo=UTC),
        source_ids=("mat-w30",),
        node_type="publication",
    )
    return EvolutionAnalysis(
        case_id=ZH_CASE_ID,
        as_of=AS_OF,
        case_type="academic_discourse",
        stages=(stage,),
        turning_points=(),
        change_reasons=(),
        evidence_gaps=(),
        open_questions=(),
    )


def make_zh_document() -> ReportDocument:
    return asyncio.run(
        ReportService().report(make_zh_analysis(), language="zh-CN")
    )


ZH_AS_OF_TEXT = "2026-09-01T00:00:00+00:00"


def make_zh_markdown() -> str:
    return (
        f"# 演变报告：{ZH_CASE_ID}\n\n"
        f"- 案例 ID: {ZH_CASE_ID}\n"
        f"- 截至时间: {ZH_AS_OF_TEXT}\n\n"
        "## 执行摘要\n\n"
        "该案例记录了 W30 的机制证据。\n\n"
        "## 时间线阶段\n\n"
        "| 事件键 | 类型 | 层 |\n| --- | --- | --- |\n"
        "| `node-w30` | evolution_node | fact |\n\n"
        "## 引用与证据定位\n\n"
        "- `mat-w30` — 引用它的 episode：`node-w30`\n"
    )


def test_render_report_html_localizes_title_and_lang_for_chinese() -> None:
    rendered = render_report_html(make_zh_markdown(), ZH_CASE_ID, language="zh-CN")
    assert '<html lang="zh-CN">' in rendered
    assert f"<title>演变报告：{ZH_CASE_ID}</title>" in rendered

    # The default stays the English document shape.
    english = render_report_html(
        "# Evolution Report: case-en\n", "case-en"
    )
    assert '<html lang="en">' in english
    assert "<title>Evolution Report: case-en</title>" in english


def test_validate_pdf_text_accepts_a_chinese_report() -> None:
    from prism.report.pdf import _validate_pdf_text

    markdown = make_zh_markdown()
    pdf_text = (
        f"演变报告：{ZH_CASE_ID} 案例 ID: {ZH_CASE_ID} "
        f"截至时间: {ZH_AS_OF_TEXT} 执行摘要 时间线阶段 引用与证据定位 "
        "该案例记录了 W30 的机制证据。"
    )
    _validate_pdf_text(
        "".join(pdf_text.split()), markdown, ZH_CASE_ID, AS_OF, language="zh-CN"
    )

    # A Chinese document missing a required Chinese section fails closed.
    truncated = pdf_text.replace("时间线阶段", "").replace("引用与证据定位", "")
    with pytest.raises(ReportPdfValidationError):
        _validate_pdf_text(
            "".join(truncated.split()), markdown, ZH_CASE_ID, AS_OF, language="zh-CN"
        )


def test_validate_pdf_text_rejects_chinese_markdown_under_english_groups() -> None:
    """Language is explicit: the English section gate must not silently pass
    a Chinese report (the pre-localization failure mode)."""
    from prism.report.pdf import _validate_pdf_text

    markdown = make_zh_markdown()
    with pytest.raises(ReportPdfValidationError):
        _validate_pdf_text(
            "".join(make_zh_markdown().split()),
            markdown,
            ZH_CASE_ID,
            AS_OF,
            language="en",
        )


def test_chinese_version_export_passes_readback_validation(tmp_path: Path) -> None:
    """Acceptance: a saved zh-CN version exports to PDF whose read-back text
    carries the Chinese title, Chinese sections and the W30 CJK body."""
    executable = edge_executable()
    paths = make_paths(tmp_path)
    ledger = ReportVersionLedger(paths)
    try:
        analysis = make_zh_analysis()
        document = make_zh_document()
        version = ledger.save(document, analysis, trigger="initial")
        assert version.language == "zh-CN"

        exporter = ReportPdfExporter(paths, renderer=EdgePdfRenderer(executable))
        result = exporter.export_version(version, "reports/case-pdf-zh.pdf")

        page_count, text = read_pdf(result.path)
        normalized = "".join(text.split())
        assert page_count >= 1
        for expected in (
            f"演变报告：{ZH_CASE_ID}",
            "截至时间",
            "执行摘要",
            "时间线阶段",
            "引用与证据定位",
            "W30全文证据已入图",
        ):
            assert "".join(expected.split()) in normalized

        reader = PdfReader(result.path)
        assert reader.metadata.get("/Title") == f"演变报告：{ZH_CASE_ID}"
    finally:
        ledger.close()


def test_wide_uuid_table_does_not_shrink_the_declared_font_sizes(tmp_path: Path) -> None:
    """Regression for the 2026-09-16 global ~67% PDF shrink (real export
    hnad-zh-20260916.pdf rendered every body char at 6.7pt instead of the
    declared 10pt).  The trigger was a 12-column table whose unbreakable UUID
    cells exceeded the printable width, so Chromium's print-to-pdf scaled the
    whole document down to fit.  The stylesheet must keep every table, code
    span, pre block, and image inside the page width so the declared sizes
    render as declared."""
    pdfplumber = pytest.importorskip("pdfplumber")
    from prism.report.pdf import (
        PDF_BODY_FONT_PT,
        PDF_TABLE_FONT_PT,
        render_report_html,
    )

    executable = edge_executable()
    columns = (
        "事件键", "类型", "层", "发生时间", "生效自", "生效至",
        "观察时间", "证据角色", "引用来源", "来源属性", "摘要", "来源材料",
    )
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = []
    for index in range(3):
        cells = (
            f"c5570b3a-bbea-52e7-886d-fefe3fe3988{index}",
            "事实", "事实层", "2026-08-30", "2026-08-30", "至今",
            "2026-09-01", "直接证据",
            f"mat_394b3e4d9e6c0ef9d3ec8ce{index}",
            "fulltext_academic_primary_source_record",
            "摘要文本在宽表格单元格里持续换行也必须保持声明字号。" * 2,
            f"710f7a28-9cd8-7125-b501-3cfd0000000{index}",
        )
        rows.append("| " + " | ".join(cells) + " |")
    body_paragraphs = "\n\n".join(
        f"第{index}段：政策与学术证据在时间线上持续演化，"
        "正文文本必须始终以样式表声明的字号渲染，不得整页缩放。"
        for index in range(40)
    )
    markdown = (
        f"# Evolution Report: {CASE_ID}\n\n"
        "- As of: 2026-09-01T00:00:00+00:00\n\n"
        "## Executive Summary\n\n"
        + body_paragraphs
        + "\n\n## Timeline Stages\n\n"
        + "\n".join([header, separator, *rows])
        + "\n\n## Citations\n\n- mat-0000\n"
    )

    html_document = render_report_html(markdown, CASE_ID, "en")
    output = tmp_path / "wide-table.pdf"
    EdgePdfRenderer(executable).render(html_document, output)

    with pdfplumber.open(output) as pdf:
        sizes = [
            round(char["size"], 1)
            for page in pdf.pages
            for char in page.chars
        ]
    assert sizes, "PDF must contain extractable positioned characters"
    dominant = Counter(sizes).most_common(1)[0][0]
    # The whole-document shrink manifested as the dominant size collapsing to
    # ~0.67x the declared body size; assert against the declared constant.
    assert abs(dominant - PDF_BODY_FONT_PT) <= 0.05, (
        f"dominant font size {dominant}pt != declared body {PDF_BODY_FONT_PT}pt; "
        "the document was globally scaled"
    )
    # Nothing may render below the smallest declared size (the table's): a
    # partial shrink of any element class fails here too.
    smallest = min(sizes)
    assert smallest >= PDF_TABLE_FONT_PT - 0.05, (
        f"smallest font size {smallest}pt < declared table {PDF_TABLE_FONT_PT}pt"
    )
