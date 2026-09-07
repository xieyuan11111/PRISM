"""WebUI workbench Phase B: the report center seam (WB-4).

These tests cover the dependency-free report controller and projections
(:mod:`prism.webui.reports`): the ``/reports`` list over
``PrismAPI.report_versions()`` (newest first, case/trigger filters, empty
distinct from failure), the read-only detail over ``report_version()``
(escaped Markdown rendering, lineage chain, honest quality layers that
never render unknown/partial as success), evidence backtracking (source-id
citations from the rendered Citations section, ``/evidence`` deep links and
facade-search locator lookup), and the server-controlled PDF export over
``export_report_pdf()`` (server-generated output path only, idempotent
filename, safe typed error mapping with CLI-consistent install guidance).
Everything is offline with fake facades and real ``ReportVersion`` objects —
no NiceGUI, no runtime, no browser, no LLM.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
from tempfile import mkdtemp
from types import SimpleNamespace

import pytest

from prism.report.ledger import ReportVersion
from prism.report.pdf import (
    ReportPdfConflictError,
    ReportPdfExportResult,
    ReportPdfRendererError,
    ReportPdfValidationError,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
CREATED_1 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
CREATED_2 = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
CREATED_3 = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)

MARKDOWN = """# Evolution Report: case-rates

- Case ID: case-rates
- As of: 2026-09-01T00:00:00+00:00

## Timeline Stages

| Episode | Kind | Summary | Sources |
| --- | --- | --- | --- |
| `node-1` | fact | Rate changed. | `mat-3` |

<script>alert('x')</script>

## Citations

- `mat-1` — cited by episodes: `node-1`
  - `corpus/2026-08/example.gov/mat-1.md` (paragraph 2, page 3)
    - Source excerpt: “The rate changed.”
- `mat-2` — cited by episodes: —
"""


def run(coro):
    return asyncio.run(coro)


def _version(
    version_id,
    *,
    case_id="case-rates",
    parent=None,
    trigger="initial",
    created_at=CREATED_1,
    summary_origin="fallback",
    markdown=MARKDOWN,
):
    return ReportVersion(
        version_id=version_id,
        case_id=case_id,
        as_of=AS_OF,
        created_at=created_at,
        input_hash=f"input-{version_id}",
        markdown_hash=f"markdown-{version_id}",
        summary_origin=summary_origin,
        debate_input_hash=None,
        markdown=markdown,
        parent_version_id=parent,
        trigger=trigger,
    )


# ------------------------------------------------------------ list projection


def test_report_rows_carry_full_and_short_hashes():
    import hashlib

    from prism.webui.reports import report_row

    row = report_row(_version("rv-1"))

    assert row["version_id"] == "rv-1"
    assert row["case_id"] == "case-rates"
    assert row["as_of"] == AS_OF.isoformat()
    assert row["created_at"] == CREATED_1.isoformat()
    assert row["trigger"] == "initial"
    assert row["parent_version_id"] is None
    assert row["summary_origin"] == "fallback"
    assert row["input_hash"] == "input-rv-1"
    assert row["markdown_hash"] == "markdown-rv-1"
    # Short values stay verbatim; longer ones are truncated for display
    # while the full value never enters a URL.
    assert row["input_hash_short"] == "input-rv-1"
    assert row["markdown_hash_short"] == "markdown-rv-…"

    digest = hashlib.sha256(b"x").hexdigest()
    long_version = ReportVersion(
        version_id="rv-long",
        case_id="case-rates",
        as_of=AS_OF,
        created_at=CREATED_2,
        input_hash=digest,
        markdown_hash=digest,
        summary_origin="llm",
        debate_input_hash=None,
        markdown=MARKDOWN,
        parent_version_id=None,
        trigger="rebuild",
    )
    row = report_row(long_version)
    assert row["input_hash"] == digest
    assert row["input_hash_short"] == f"{digest[:12]}…"
    assert row["markdown_hash_short"] == f"{digest[:12]}…"


def test_load_versions_lists_newest_first_and_forwards_the_case_filter():
    facade = FakeReportsFacade(
        versions=(
            _version("rv-1", created_at=CREATED_1),
            _version("rv-2", created_at=CREATED_3),
            _version("rv-3", created_at=CREATED_2),
        )
    )
    controller = _controller(facade)

    payload = run(controller.load_versions())

    assert [row["version_id"] for row in payload["versions"]] == [
        "rv-2", "rv-3", "rv-1",
    ]
    assert payload["count"] == 3
    assert payload["empty"] is False
    assert payload["filtered"] is False

    run(controller.load_versions(case_id="case-rates"))
    assert facade.versions_calls == [
        {"case_id": None, "as_of": None},
        {"case_id": "case-rates", "as_of": None},
    ]


def test_load_versions_trigger_filter_is_a_presentation_filter():
    controller = _controller(FakeReportsFacade(
        versions=(
            _version("rv-1", trigger="initial"),
            _version("rv-2", trigger="rebuild", created_at=CREATED_2),
        )
    ))

    payload = run(controller.load_versions(trigger="rebuild"))

    assert [row["version_id"] for row in payload["versions"]] == ["rv-2"]
    assert payload["count"] == 1
    assert payload["filtered"] is True


def test_load_versions_rejects_invalid_filters_before_any_facade_call():
    facade = FakeReportsFacade()
    controller = _controller(facade)

    with pytest.raises(ValueError, match="case_id"):
        run(controller.load_versions(case_id=" "))
    with pytest.raises(ValueError, match="trigger"):
        run(controller.load_versions(trigger="green"))

    assert facade.versions_calls == []


def test_an_empty_ledger_is_reported_as_empty_not_as_a_failure():
    controller = _controller(FakeReportsFacade())

    payload = run(controller.load_versions())

    assert payload["versions"] == []
    assert payload["count"] == 0
    assert payload["empty"] is True


def test_a_facade_failure_propagates_and_never_becomes_an_empty_list():
    controller = _controller(FakeReportsFacade(error=RuntimeError("ledger down")))

    with pytest.raises(RuntimeError, match="ledger down"):
        run(controller.load_versions())


# ---------------------------------------------------------- detail projection


def _fake_version(**overrides):
    fields = dict(
        version_id="rv-1",
        case_id="case-rates",
        as_of=AS_OF,
        created_at=CREATED_1,
        input_hash="input-rv-1",
        markdown_hash="markdown-rv-1",
        summary_origin="llm",
        debate_input_hash="debate-1",
        markdown=MARKDOWN,
        parent_version_id=None,
        trigger="material_added",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_detail_view_escapes_html_and_keeps_both_raw_and_rendered_bodies():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_fake_version())

    assert view["markdown"] == MARKDOWN
    # The rendered body neutralizes embedded HTML/script...
    assert "<script>" not in view["render_markdown"]
    assert "&lt;script&gt;" in view["render_markdown"]
    # ...while the raw body stays verbatim inside a read-only code fence.
    assert "```markdown\n" in view["raw_markdown_block"]
    assert "<script>alert('x')</script>" in view["raw_markdown_block"]
    assert view["summary_origin"] == "llm"
    assert view["debate_input_hash"] == "debate-1"


def test_detail_quality_layers_default_to_unknown_never_success():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_fake_version())

    # ReportVersion carries no mechanism/semantic/gap fields: they project
    # as unknown/not provided — never as pass or success (H-4).
    assert view["mechanism_status"] == "unknown"
    assert view["semantic_status"] == "unknown"
    assert view["mechanism_ui"] == "未知"
    assert view["semantic_ui"] == "未知"
    assert view["evidence_gap_count"] is None
    assert view["evidence_gap_summary"] == "未提供"
    assert view["evidence_gaps"] == []


def test_detail_quality_layers_project_real_fields_and_flag_partial():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_fake_version(
        mechanism_status="pass",
        semantic_status="partial",
        evidence_gap_count=2,
        evidence_gaps=("missing quote on node-1",),
    ))

    assert view["mechanism_status"] == "pass"
    assert view["mechanism_ui"] == "成功"
    assert view["semantic_status"] == "partial"
    # partial is a warning, never a success color or word.
    assert view["semantic_ui"] == "部分完成"
    assert view["semantic_ui"] != "成功"
    assert view["evidence_gap_count"] == 2
    assert view["evidence_gap_summary"] == "2 个证据缺口"
    assert view["evidence_gaps"] == ["missing quote on node-1"]


def test_lineage_walks_parents_from_current_to_root():
    from prism.webui.reports import lineage_rows

    v1 = _version("rv-1", created_at=CREATED_1)
    v2 = _version("rv-2", parent="rv-1", created_at=CREATED_2)
    v3 = _version("rv-3", parent="rv-2", created_at=CREATED_3)

    lineage = lineage_rows(v3, (v1, v2, v3))

    assert [row["version_id"] for row in lineage["chain"]] == [
        "rv-3", "rv-2", "rv-1",
    ]
    assert [row["current"] for row in lineage["chain"]] == [
        True, False, False,
    ]
    assert lineage["truncated"] is False


def test_lineage_marks_a_missing_parent_as_truncated():
    from prism.webui.reports import lineage_rows

    orphan = _version("rv-3", parent="rv-gone", created_at=CREATED_3)

    lineage = lineage_rows(orphan, (orphan,))

    assert [row["version_id"] for row in lineage["chain"]] == ["rv-3"]
    assert lineage["truncated"] is True


def test_lineage_survives_a_corrupt_parent_cycle():
    from prism.webui.reports import lineage_rows

    a = _version("rv-a", parent="rv-b", created_at=CREATED_1)
    b = _version("rv-b", parent="rv-a", created_at=CREATED_2)

    lineage = lineage_rows(a, (a, b))

    assert lineage["chain"][0]["version_id"] == "rv-a"
    assert lineage["truncated"] is True
    # The walk terminates instead of looping forever.
    assert len(lineage["chain"]) <= 2


def test_controller_load_version_returns_metadata_lineage_and_body():
    v1 = _version("rv-1", created_at=CREATED_1)
    v2 = _version("rv-2", parent="rv-1", created_at=CREATED_2)
    facade = FakeReportsFacade(versions=(v1, v2), by_id={"rv-2": v2})
    controller = _controller(facade)

    view = run(controller.load_version("rv-2"))

    assert view["version_id"] == "rv-2"
    assert view["parent_version_id"] == "rv-1"
    assert [row["version_id"] for row in view["lineage"]["chain"]] == [
        "rv-2", "rv-1",
    ]
    assert view["render_markdown"]
    assert facade.version_calls == ["rv-2"]


def test_controller_load_version_rejects_blank_or_path_like_ids():
    facade = FakeReportsFacade()
    controller = _controller(facade)

    with pytest.raises(ValueError):
        run(controller.load_version(""))
    with pytest.raises(ValueError, match="路径"):
        run(controller.load_version("rv-1/../../etc"))

    assert facade.version_calls == []


def test_controller_load_version_unknown_ids_raise_lookup_error():
    controller = _controller(FakeReportsFacade())

    with pytest.raises(LookupError):
        run(controller.load_version("rv-missing"))


# ------------------------------------------------------ evidence backtracking


def test_cited_source_ids_come_only_from_the_citations_section():
    from prism.webui.reports import cited_source_ids

    assert cited_source_ids(MARKDOWN) == ("mat-1", "mat-2")
    # `mat-3` appears in the timeline Sources column but is not a citation
    # entry, and documents without a Citations section yield nothing.
    assert "mat-3" not in cited_source_ids(MARKDOWN)
    assert cited_source_ids("# Evolution Report: case-rates\n") == ()


def test_evidence_query_urls_are_escaped():
    from prism.webui.reports import evidence_query_url

    assert evidence_query_url("mat-1") == "/evidence?query=mat-1"
    assert evidence_query_url("mat 1/x") == "/evidence?query=mat%201%2Fx"


def test_detail_view_flags_missing_structured_citations():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_fake_version())

    citations = view["citations"]
    # ReportVersion persists no structured citations: the view says the
    # citation structure is incomplete instead of guessing locators.
    assert citations["structured"] is False
    assert citations["source_ids"] == ["mat-1", "mat-2"]
    assert "不完整" in citations["note"]
    assert citations["query_urls"] == {
        "mat-1": "/evidence?query=mat-1",
        "mat-2": "/evidence?query=mat-2",
    }


def test_detail_view_projects_structured_citations_when_present():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_fake_version(citations=(
        SimpleNamespace(
            source_id="mat-1",
            episode_keys=("node-1",),
            evidence=(SimpleNamespace(
                source_id="mat-1",
                corpus_path="corpus/2026-08/example.gov/mat-1.md",
                paragraph=2,
                page=3,
                quote="The rate changed.",
            ),),
        ),
)))

    citations = view["citations"]
    assert citations["structured"] is True
    assert citations["source_ids"] == ["mat-1"]
    locator = citations["locators"]["mat-1"][0]
    assert locator["corpus_path"] == "corpus/2026-08/example.gov/mat-1.md"
    assert locator["paragraph"] == 2
    assert locator["page"] == 3
    assert locator["quote"] == "The rate changed."


def test_locate_source_returns_only_exact_source_id_locators():
    facade = FakeReportsFacade(search_hits=(
        SimpleNamespace(
            source_id="mat-1", title="Policy update",
            path="corpus/2026-08/example.gov/mat-1.md",
            paragraph=2, page=3, quote="The rate changed.",
        ),
        SimpleNamespace(
            source_id="mat-9", title="Unrelated",
            path="corpus/other.md", paragraph=None, page=None, quote=None,
        ),
    ))
    controller = _controller(facade)

    found = run(controller.locate_source("mat-1"))

    assert facade.search_calls == ["mat-1"]
    assert found["source_id"] == "mat-1"
    assert found["count"] == 1
    assert found["locators"][0]["corpus_path"] == (
        "corpus/2026-08/example.gov/mat-1.md"
    )
    assert found["locators"][0]["paragraph"] == 2
    assert found["locators"][0]["page"] == 3
    assert found["locators"][0]["quote"] == "The rate changed."


def test_locate_source_without_a_match_is_honestly_empty():
    controller = _controller(FakeReportsFacade(search_hits=()))

    found = run(controller.locate_source("mat-1"))

    assert found["count"] == 0
    assert found["locators"] == []


def test_locate_source_requires_the_search_capability_and_a_valid_id():
    bare = _NoSearchFacade()
    assert _controller(bare).evidence_lookup_available is False
    with pytest.raises(RuntimeError, match="search"):
        run(_controller(bare).locate_source("mat-1"))

    with_search = FakeReportsFacade(search_hits=())
    with pytest.raises(ValueError):
        run(_controller(with_search).locate_source(" "))


def test_detail_view_reports_the_evidence_lookup_capability():
    from prism.webui.reports import report_detail_view

    assert report_detail_view(_fake_version())[
        "evidence_lookup_available"
    ] is False
    assert report_detail_view(
        _fake_version(), evidence_lookup_available=True
    )["evidence_lookup_available"] is True


# ------------------------------------------------------------ PDF export


def _export_result(tmp_path, version_id="rv-1"):
    return ReportPdfExportResult(
        path=tmp_path / "output" / "reports" / "exports" / f"{version_id}.pdf",
        version_id=version_id,
        case_id="case-rates",
        as_of=AS_OF,
        markdown_hash="markdown-rv-1",
        pdf_hash="pdf-hash",
        page_count=3,
    )


def test_export_uses_a_server_generated_path_and_never_asked_the_caller():
    facade = FakeReportsFacade(export_result=_export_result(Path(".")))
    controller = _controller(facade)

    view = run(controller.export_pdf("rv-1"))

    assert facade.export_calls == [("rv-1", "reports/exports/rv-1.pdf")]
    assert view["state"] == "exported"
    assert view["version_id"] == "rv-1"
    assert view["page_count"] == 3
    assert view["markdown_hash"] == "markdown-rv-1"
    assert view["pdf_hash"] == "pdf-hash"
    assert view["output_path"] == "reports/exports/rv-1.pdf"
    assert view["filename"] == "rv-1.pdf"


def test_export_filename_is_deterministic_for_idempotent_reuse():
    facade = FakeReportsFacade(export_result=_export_result(Path(".")))
    controller = _controller(facade)

    run(controller.export_pdf("rv-1"))
    run(controller.export_pdf("rv-1"))

    assert facade.export_calls[0] == facade.export_calls[1]


@pytest.fixture()
def home_dir():
    directory = Path(mkdtemp(prefix="prism-webui-reporthome-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_export_display_path_is_project_relative(home_dir, monkeypatch):
    from prism.webui.reports import export_result_view

    monkeypatch.setenv("PRISM_HOME", str(home_dir))
    result = _export_result(home_dir)

    view = export_result_view(result, "reports/exports/rv-1.pdf")

    assert view["display_path"] == "output/reports/exports/rv-1.pdf"

    elsewhere = _export_result(Path("Z:/nowhere"))
    assert export_result_view(
        elsewhere, "reports/exports/rv-1.pdf"
    )["display_path"] == "rv-1.pdf"


def test_export_failures_map_to_safe_typed_errors_with_cli_guidance():
    facade = FakeReportsFacade(export_error=ReportPdfRendererError(
        "PDF export requires the optional 'pypdf' package; "
        "install with 'pip install -e \".[pdf]\"'"
    ))
    view = run(_controller(facade).export_pdf("rv-1"))

    assert view["state"] == "failure"
    assert view["error_type"] == "pdf_dependencies_missing"
    assert "pip install" in view["message"]

    no_chromium = run(_controller(FakeReportsFacade(
        export_error=ReportPdfRendererError(
            "no PDF renderer is available; set PRISM_PDF_RENDERER to "
            "Microsoft Edge or a compatible Chromium executable"
        )
    )).export_pdf("rv-1"))
    assert no_chromium["error_type"] == "pdf_render_failed"
    assert "PRISM_PDF_RENDERER" in no_chromium["message"]

    conflict = run(_controller(FakeReportsFacade(
        export_error=ReportPdfConflictError(
            "output path already contains different content; refusing to "
            "overwrite"
        )
    )).export_pdf("rv-1"))
    assert conflict["error_type"] == "pdf_conflict"

    invalid = run(_controller(FakeReportsFacade(
        export_error=ReportPdfValidationError(
            "rendered PDF failed text validation"
        )
    )).export_pdf("rv-1"))
    assert invalid["error_type"] == "pdf_validation_failed"


def test_export_unknown_version_maps_to_version_not_found():
    # The facade/ledger raises LookupError for an unknown version id; the
    # controller maps it to a typed failure view, never a fake export.
    view = run(_controller(FakeReportsFacade(
        export_error=LookupError("no report version 'rv-missing'")
    )).export_pdf("rv-missing"))

    assert view["state"] == "failure"
    assert view["error_type"] == "version_not_found"


def test_export_unexpected_errors_never_leak_details():
    view = run(_controller(FakeReportsFacade(
        export_error=RuntimeError("secret key=ABC at C:/Users/x/keys.env")
    )).export_pdf("rv-1"))

    assert view["state"] == "failure"
    assert view["message"] == "导出 PDF failed (RuntimeError)"
    assert "ABC" not in view["message"]
    assert "C:/" not in view["message"]
    assert "keys.env" not in view["message"]


def test_export_rejects_path_like_version_ids_before_any_facade_call():
    facade = FakeReportsFacade(export_result=_export_result(Path(".")))

    with pytest.raises(ValueError, match="路径"):
        run(_controller(facade).export_pdf("rv-1/../../etc"))
    with pytest.raises(ValueError):
        run(_controller(facade).export_pdf(" "))

    assert facade.export_calls == []


# ------------------------------------------------------------ controller seam


class FakeReportsFacade:
    def __init__(
        self,
        versions=(),
        by_id=None,
        export_result=None,
        export_error=None,
        search_hits=None,
        error=None,
    ):
        self.versions = tuple(versions)
        self.by_id = dict(by_id or {})
        self.export_result = export_result
        self.export_error = export_error
        self.search_hits = search_hits
        self.error = error
        self.versions_calls = []
        self.version_calls = []
        self.export_calls = []
        self.search_calls = []

    async def report_versions(self, case_id=None, *, as_of=None):
        self.versions_calls.append({"case_id": case_id, "as_of": as_of})
        if self.error is not None:
            raise self.error
        return tuple(
            version
            for version in self.versions
            if case_id is None or version.case_id == case_id
        )

    async def report_version(self, version_id):
        self.version_calls.append(version_id)
        if version_id not in self.by_id:
            raise LookupError(f"no report version {version_id!r}")
        return self.by_id[version_id]

    async def export_report_pdf(self, version_id, output_path):
        self.export_calls.append((version_id, output_path))
        if self.export_error is not None:
            raise self.export_error
        return self.export_result

    async def search(self, query=None, **filters):
        self.search_calls.append(query)
        return tuple(self.search_hits or ())


class _NoSearchFacade:
    """The minimal report facade shape: no evidence search capability."""

    def __init__(self, versions=(), by_id=None):
        self.versions = tuple(versions)
        self.by_id = dict(by_id or {})

    async def report_versions(self, case_id=None, *, as_of=None):
        return tuple(
            version
            for version in self.versions
            if case_id is None or version.case_id == case_id
        )

    async def report_version(self, version_id):
        if version_id not in self.by_id:
            raise LookupError(f"no report version {version_id!r}")
        return self.by_id[version_id]

    async def export_report_pdf(self, version_id, output_path):
        raise AssertionError("not called in this test")


def _controller(facade=None):
    from prism.webui.reports import ReportCenterController

    return ReportCenterController(
        facade if facade is not None else FakeReportsFacade()
    )


def test_controller_rejects_incomplete_facades():
    from prism.webui.reports import ReportCenterController

    class Empty:
        pass

    with pytest.raises(TypeError, match="report_versions"):
        ReportCenterController(Empty())


def test_controller_reports_the_evidence_lookup_capability():
    assert _controller(_NoSearchFacade()).evidence_lookup_available is False
    assert _controller(
        FakeReportsFacade(search_hits=())
    ).evidence_lookup_available is True


# ------------------------------------------------------------ fake-ui page seam


class _FakeElement:
    def __init__(self, ui, name, *args, **kwargs):
        self._ui = ui
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.value = kwargs.get("value")
        self.rows = list(kwargs.get("rows") or ())
        self.text = args[0] if args and isinstance(args[0], str) else ""
        self.content = args[0] if args and isinstance(args[0], str) else ""
        self.children = []

    def __enter__(self):
        self._ui._stack.append(self)
        return self

    def __exit__(self, *exc_info):
        self._ui._stack.pop()
        return False

    def update(self):
        self._ui.updates.append(self.name)

    def classes(self, *args, **kwargs):
        return self


class _FakeUI:
    def __init__(self):
        self.elements = []
        self.pages = {}
        self.updates = []
        self._stack = []

    def __getattr__(self, name):
        def factory(*args, **kwargs):
            element = _FakeElement(self, name, *args, **kwargs)
            if self._stack:
                self._stack[-1].children.append(element)
            self.elements.append(element)
            return element

        return factory

    def page(self, route):
        def register(fn):
            self.pages[route] = fn
            return fn

        return register


def _element(ui, name, *, label=None, text=None):
    for element in ui.elements:
        if element.name != name:
            continue
        element_label = str(element.kwargs.get("label", ""))
        if label is not None and label.lower() not in element_label.lower():
            continue
        element_text = element.args[0] if element.args else ""
        if text is not None and text.lower() not in str(element_text).lower():
            continue
        return element
    raise AssertionError(
        f"no {name} element matching label={label!r} text={text!r}"
    )


def _labels(ui):
    return [element for element in ui.elements if element.name == "label"]


def _build_pages(controller):
    from prism.webui.reports import build_report_pages

    ui = _FakeUI()
    build_report_pages(controller, ui)
    return ui


def test_list_page_lists_filters_columns_and_trigger_vocabulary():
    ui = _build_pages(_controller())

    assert "/reports" in ui.pages
    assert "/reports/{version_id}" in ui.pages
    ui.pages["/reports"]()
    _element(ui, "input", label="案例")
    trigger_options = _element(ui, "select", label="触发").kwargs["options"]
    assert set(trigger_options) == {
        "", "initial", "material_added", "rebuild", "debate_updated",
    }
    _element(ui, "button", text="刷新报告")
    columns = {
        column["field"]
        for column in _element(ui, "table").kwargs["columns"]
    }
    assert columns >= {
        "version_id", "case_id", "as_of", "created_at", "trigger",
        "parent_version_id", "summary_origin", "input_hash_short",
        "markdown_hash_short",
    }


def test_list_page_refresh_fills_rows_and_distinguishes_empty_from_failure():
    v1 = _version("rv-1", created_at=CREATED_1)
    v2 = _version("rv-2", created_at=CREATED_2)
    ui = _build_pages(_controller(FakeReportsFacade(versions=(v1, v2))))
    list_page = ui.pages["/reports"]
    list_page()

    run(_element(ui, "button", text="刷新报告").kwargs["on_click"](None))

    table = _element(ui, "table")
    assert [row["version_id"] for row in table.rows] == ["rv-2", "rv-1"]

    # Empty ledger: an explicit "no report versions" state...
    empty_ui = _build_pages(_controller(FakeReportsFacade()))
    empty_ui.pages["/reports"]()
    run(
        _element(empty_ui, "button", text="刷新报告").kwargs["on_click"](
            None
        )
    )
    status = [
        element
        for element in empty_ui.elements
        if element.name == "markdown"
    ][0]
    assert "暂无报告版本" in status.content
    assert "无法加载" not in status.content

    # ...versus a facade failure: an error state that never claims emptiness.
    failed_ui = _build_pages(
        _controller(FakeReportsFacade(error=RuntimeError("ledger down")))
    )
    failed_ui.pages["/reports"]()
    run(
        _element(failed_ui, "button", text="刷新报告").kwargs[
            "on_click"
        ](None)
    )
    failed_status = [
        element
        for element in failed_ui.elements
        if element.name == "markdown"
    ][0]
    assert "无法加载" in failed_status.content
    assert "暂无报告版本" not in failed_status.content
    assert any(
        "加载报告 failed (RuntimeError)" == label.text
        for label in _labels(failed_ui)
    )


def test_detail_page_renders_the_escaped_body_metadata_and_citations():
    v1 = _version("rv-1", created_at=CREATED_1)
    controller = _controller(FakeReportsFacade(versions=(v1,), by_id={
        "rv-1": v1,
    }))
    ui = _build_pages(controller)
    detail_page = ui.pages["/reports/{version_id}"]

    run(detail_page("rv-1"))

    body = [
        element for element in ui.elements if element.name == "markdown"
    ]
    joined = "\n".join(element.content for element in body)
    assert "&lt;script&gt;" in joined
    assert "case-rates" in joined
    assert "`rv-1`" in joined
    # Citations panel: incomplete-structure note plus deep links.
    assert "不完整" in joined
    targets = [
        element.args[1] if len(element.args) > 1 else element.kwargs.get(
            "target", ""
        )
        for element in ui.elements
        if element.name == "link"
    ]
    assert "/evidence?query=mat-1" in targets


def test_detail_page_locates_evidence_for_a_cited_source():
    v1 = _version("rv-1", created_at=CREATED_1)
    controller = _controller(FakeReportsFacade(
        versions=(v1,),
        by_id={"rv-1": v1},
        search_hits=(SimpleNamespace(
            source_id="mat-1", title="Policy update",
            path="corpus/2026-08/example.gov/mat-1.md",
            paragraph=2, page=3, quote="The rate changed.",
        ),),
    ))
    ui = _build_pages(controller)
    run(ui.pages["/reports/{version_id}"]("rv-1"))

    locate = _element(ui, "button", text="定位 mat-1")
    run(locate.kwargs["on_click"](None))

    joined = "\n".join(
        element.content
        for element in ui.elements
        if element.name == "markdown"
    )
    assert "corpus/2026-08/example.gov/mat-1.md" in joined
    assert "第 2 段" in joined
    assert "The rate changed." in joined


def test_detail_page_export_button_uses_only_the_version_id():
    v1 = _version("rv-1", created_at=CREATED_1)
    facade = FakeReportsFacade(
        versions=(v1,), by_id={"rv-1": v1},
        export_result=_export_result(Path(".")),
    )
    controller = _controller(facade)
    ui = _build_pages(controller)
    run(ui.pages["/reports/{version_id}"]("rv-1"))

    run(_element(ui, "button", text="导出 PDF").kwargs["on_click"](None))

    assert facade.export_calls == [("rv-1", "reports/exports/rv-1.pdf")]
    joined = "\n".join(
        element.content
        for element in ui.elements
        if element.name == "markdown"
    )
    assert "3 页" in joined


def test_detail_page_export_failure_shows_guidance_without_success_words():
    v1 = _version("rv-1", created_at=CREATED_1)
    controller = _controller(FakeReportsFacade(
        versions=(v1,), by_id={"rv-1": v1},
        export_error=ReportPdfRendererError(
            "PDF export requires the optional 'pypdf' package; "
            "install with 'pip install -e \".[pdf]\"'"
        ),
    ))
    ui = _build_pages(controller)
    run(ui.pages["/reports/{version_id}"]("rv-1"))

    run(_element(ui, "button", text="导出 PDF").kwargs["on_click"](None))

    export_md = [
        element
        for element in ui.elements
        if element.name == "markdown"
    ][-1]
    assert "pdf_dependencies_missing" in export_md.content
    assert "pip install" in export_md.content
    assert "success" not in export_md.content.lower()
    assert any(
        "PDF 导出失败" in label.text for label in _labels(ui)
    )


def test_detail_page_unknown_version_shows_a_failure_not_an_empty_page():
    ui = _build_pages(_controller(FakeReportsFacade()))
    page = ui.pages["/reports/{version_id}"]

    run(page("rv-missing"))

    assert any(
        "加载报告版本 failed (LookupError)" == label.text
        for label in _labels(ui)
    )


def test_detail_page_flags_partial_semantics_with_a_warning_badge():
    version = _fake_version(
        mechanism_status="pass",
        semantic_status="partial",
        evidence_gap_count=2,
        evidence_gaps=("missing quote on node-1",),
    )
    controller = _controller(FakeReportsFacade(
        versions=(version,), by_id={"rv-1": version},
    ))
    ui = _build_pages(controller)

    run(ui.pages["/reports/{version_id}"]("rv-1"))

    badges = {
        element.text: element.kwargs.get("color")
        for element in ui.elements
        if element.name == "badge"
    }
    # partial is the warning color and never the success color; pass is
    # positive; the gap count stays visible as text.
    assert badges["语义: partial"] == "warning"
    assert badges["机制: pass"] == "positive"
    quality_text = "\n".join(
        element.content for element in ui.elements
        if element.name == "markdown"
    )
    assert "2 个证据缺口" in quality_text


def test_detail_page_keeps_deep_links_without_the_search_capability():
    v1 = _version("rv-1", created_at=CREATED_1)
    controller = _controller(_NoSearchFacade(
        versions=(v1,), by_id={"rv-1": v1},
    ))
    ui = _build_pages(controller)

    run(ui.pages["/reports/{version_id}"]("rv-1"))

    targets = [
        element.args[1] if len(element.args) > 1 else element.kwargs.get(
            "target", ""
        )
        for element in ui.elements
        if element.name == "link"
    ]
    assert "/evidence?query=mat-1" in targets
    assert not any(
        element.name == "button" and "定位" in str(
            element.args[0] if element.args else ""
        )
        for element in ui.elements
    )


# ------------------------------------------------- server proxy / import seam


def test_lazy_api_proxies_the_report_operations():
    from prism.webui.server import _LazyAPI

    v1 = _version("rv-1", created_at=CREATED_1)
    facade = FakeReportsFacade(
        versions=(v1,), by_id={"rv-1": v1},
        export_result=_export_result(Path(".")),
    )
    holder = {}
    proxy = _LazyAPI(holder)

    with pytest.raises(RuntimeError, match="not started"):
        run(proxy.report_versions())

    holder["runtime"] = SimpleNamespace(api=facade)
    assert run(proxy.report_versions()) == (v1,)
    assert run(proxy.report_version("rv-1")) is v1
    run(proxy.export_report_pdf("rv-1", "reports/exports/rv-1.pdf"))
    assert facade.export_calls == [("rv-1", "reports/exports/rv-1.pdf")]


def test_importing_the_module_never_imports_nicegui():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "nicegui" or name.startswith("nicegui.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        from prism.webui import reports

        assert callable(reports.ReportCenterController)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)
