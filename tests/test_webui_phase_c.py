"""WebUI workbench Phase C: case-home linkage and bidirectional deep links.

Phase C wires the already-delivered read-only seams together without any new
fact source (H-6): the case home shows the selected case's recent material
runs (read through ``PrismAPI.material_journeys``, the durable outcome ledger,
with a lifecycle status filter — cross-session and including failures) and its
recent report versions (read through ``PrismAPI.report_versions``, newest
first, each row deep-linking to ``/reports/{version_id}``); the journey view
carries a report deep link; and the report detail page links back to the case
home.  The honesty invariants carry over unchanged (H-4): pending, partial,
unknown and failure never render as success; an empty list and a facade
failure are distinct states; missing fields project as unknown.  Everything is
offline through synthetic facades and a recording ``ui`` stand-in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from prism.analyzer import HistoricalCaseState as State
from prism.api.facade import MaterialJourneyView
from prism.cases.overview import CaseOverview
from prism.pipeline.outcomes import PipelineRunAudit
from prism.pipeline.service import PipelineFailure
from prism.report.ledger import ReportVersion

UTC = timezone.utc
OBSERVED = datetime(2026, 2, 1, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
T3 = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
CASE = "case-rates"
OTHER = "case-housing"


def run(coro):
    return asyncio.run(coro)


def _overview(case_id=CASE, *, materials=2):
    return CaseOverview(
        case_id=case_id,
        case_type="policy",
        name=f"Case {case_id}",
        status="active",
        material_count=materials,
        earliest_observed_at=OBSERVED,
        latest_observed_at=OBSERVED,
        latest_node_at=OBSERVED,
        last_updated_at=OBSERVED,
        has_unresolved_gaps=False,
        has_unresolved_conflicts=False,
    )


def _journey_view(
    material_id,
    *,
    lifecycle="committed",
    case_id=CASE,
    occurred=T2,
    report_version_id=None,
    display="Policy update",
):
    failure = None
    if lifecycle == "failed":
        failure = PipelineFailure(
            material_id=material_id,
            stage="extract",
            error_type="RuntimeError",
            message="extractor exploded",
            failed_at=occurred,
        )
    return MaterialJourneyView(
        material_id=material_id,
        display_name=display,
        source_format="md",
        case_id=case_id,
        raw_path=f"raw/{material_id}.md",
        corpus_path=f"corpus/doc-{material_id}.md",
        content_hash="0" * 64,
        fetched_at=occurred,
        occurred_at=occurred,
        lifecycle_status=lifecycle,
        run=None,
        run_audit=PipelineRunAudit(
            material_id,
            "failed" if lifecycle == "failed" else "completed",
            stages=(),
            finished_at=occurred,
        ),
        failure=failure,
        report_version_id=report_version_id,
    )


def _version(version_id, *, created_at=T2, case_id=CASE,
             trigger="material_added"):
    return ReportVersion(
        version_id=version_id,
        case_id=case_id,
        as_of=created_at,
        created_at=created_at,
        input_hash="i" * 64,
        markdown_hash="m" * 64,
        summary_origin="deterministic",
        debate_input_hash=None,
        markdown="# Report\n\nBody.",
        parent_version_id=None,
        trigger=trigger,
    )


class FakeLinkedFacade:
    """Case-home facade with the Phase C journey/report read capabilities."""

    def __init__(self, *, overviews=(), journeys=(), versions=(),
                 journeys_error=None, versions_error=None):
        self._overviews = {item.case_id: item for item in overviews}
        self._journeys = tuple(journeys)
        self._versions = tuple(versions)
        self._journeys_error = journeys_error
        self._versions_error = versions_error
        self.overview_calls = []
        self.journeys_calls = []
        self.versions_calls = []

    async def case_overviews(self, **filters):
        return tuple(self._overviews.values())

    async def case_overview(self, case_id):
        self.overview_calls.append(case_id)
        try:
            return self._overviews[case_id]
        except KeyError:
            raise LookupError(
                f"no accumulated evolution case {case_id!r}"
            ) from None

    async def query_historical_snapshot(
        self, case_id, as_of, *, stage=None, kinds=None
    ):
        return State(
            case_id=case_id, cutoff_at=as_of, case_type=None, status=None,
            nodes=(), facts=(), interpretations=(), evidence_gaps=(),
        )

    async def material_journeys(self, *, case_id=None, status=None):
        self.journeys_calls.append({"case_id": case_id, "status": status})
        if self._journeys_error is not None:
            raise self._journeys_error
        views = self._journeys
        if case_id is not None:
            views = [view for view in views if view.case_id == case_id]
        if status is not None:
            views = [
                view for view in views
                if view.lifecycle_status == status
            ]
        return tuple(views)

    async def report_versions(self, case_id=None, *, as_of=None):
        self.versions_calls.append(case_id)
        if self._versions_error is not None:
            raise self._versions_error
        versions = self._versions
        if case_id is not None:
            versions = [
                version for version in versions
                if version.case_id == case_id
            ]
        return tuple(versions)


class CaseOnlyFacade:
    """The historical case-home facade shape (no journey/report reads)."""

    async def case_overviews(self, **filters):
        return ()

    async def case_overview(self, case_id):
        raise LookupError(case_id)

    async def query_historical_snapshot(
        self, case_id, as_of, *, stage=None, kinds=None
    ):
        raise AssertionError("not called in these tests")


def _controller(facade):
    from prism.webui.controller import CaseHomeController

    return CaseHomeController(facade)


# ------------------------------------------------- capability gates


def test_controller_reports_the_phase_c_linkage_capabilities():
    controller = _controller(CaseOnlyFacade())

    assert controller.materials_available is False
    assert controller.reports_available is False

    linked = _controller(
        FakeLinkedFacade(overviews=[_overview()], journeys=(), versions=())
    )
    assert linked.materials_available is True
    assert linked.reports_available is True


def test_a_journeys_only_facade_enables_only_the_materials_panel():
    class JourneysOnly(CaseOnlyFacade):
        async def material_journeys(self, *, case_id=None, status=None):
            return ()

    controller = _controller(JourneysOnly())

    assert controller.materials_available is True
    assert controller.reports_available is False


# --------------------------------------------- load_case_materials


def test_load_case_materials_projects_recent_rows_with_honest_badges():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys=(
            _journey_view("mat-new", occurred=T3, report_version_id="rv-2"),
            _journey_view("mat-broken", lifecycle="failed", occurred=T2),
            _journey_view("mat-flight", lifecycle="pending", occurred=T1),
            _journey_view("mat-stale", lifecycle="unknown", occurred=T1),
            _journey_view("mat-other", case_id=OTHER, occurred=T3),
        ),
    )
    controller = _controller(facade)

    payload = run(controller.load_case_materials(CASE))

    # Only the selected case's runs, most recent first (facade order).
    assert [row["material_id"] for row in payload["materials"]] == [
        "mat-new", "mat-broken", "mat-flight", "mat-stale",
    ]
    assert payload["count"] == 4
    assert payload["case_id"] == CASE
    by_id = {row["material_id"]: row for row in payload["materials"]}
    # The required row fields (WB-2.1 shape).
    for row in payload["materials"]:
        assert set(row) >= {
            "material_id", "display_name", "lifecycle_status",
            "ui_status", "occurred_at",
        }
    assert by_id["mat-new"]["display_name"] == "Policy update"
    assert by_id["mat-new"]["occurred_at"] == T3.isoformat()
    # lifecycle_ui_status badges: only committed is success (H-4).
    assert by_id["mat-new"]["ui_status"] == "成功"
    assert by_id["mat-broken"]["ui_status"] == "失败"
    assert by_id["mat-broken"]["failed_stage"] == "extract"
    assert by_id["mat-broken"]["error_type"] == "RuntimeError"
    assert by_id["mat-flight"]["ui_status"] == "加载中"
    assert by_id["mat-stale"]["ui_status"] == "未知"
    assert facade.journeys_calls == [{"case_id": CASE, "status": None}]


def test_load_case_materials_filters_by_status_and_limits_rows():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys=(
            _journey_view("mat-new", occurred=T3),
            _journey_view("mat-broken", lifecycle="failed", occurred=T2),
            _journey_view("mat-flight", lifecycle="pending", occurred=T1),
        ),
    )
    controller = _controller(facade)

    payload = run(
        controller.load_case_materials(CASE, status="failed", limit=1)
    )

    assert [row["material_id"] for row in payload["materials"]] == [
        "mat-broken"
    ]
    assert facade.journeys_calls[-1] == {"case_id": CASE, "status": "failed"}


def test_load_case_materials_validates_inputs_before_any_read():
    facade = FakeLinkedFacade(overviews=[_overview(CASE)])
    controller = _controller(facade)

    with pytest.raises(ValueError):
        run(controller.load_case_materials("  "))
    with pytest.raises(ValueError):
        run(controller.load_case_materials(CASE, status="green"))
    with pytest.raises(ValueError):
        run(controller.load_case_materials(CASE, limit=0))
    with pytest.raises(TypeError):
        run(controller.load_case_materials(CASE, status=7))
    assert facade.journeys_calls == []
    assert facade.overview_calls == []


def test_load_case_materials_gates_on_capability_and_case_existence():
    with pytest.raises(RuntimeError, match="material_journeys"):
        run(_controller(CaseOnlyFacade()).load_case_materials(CASE))

    facade = FakeLinkedFacade(overviews=[_overview(CASE)])
    controller = _controller(facade)
    with pytest.raises(LookupError):
        run(controller.load_case_materials("case-ghost"))
    assert facade.journeys_calls == []


def test_load_case_materials_failure_propagates_and_empty_is_its_own_state():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys_error=RuntimeError("ledger locked"),
    )
    controller = _controller(facade)

    with pytest.raises(RuntimeError, match="ledger locked"):
        run(controller.load_case_materials(CASE))

    empty = run(_controller(
        FakeLinkedFacade(overviews=[_overview(CASE)], journeys=())
    ).load_case_materials(CASE))
    assert empty["materials"] == []
    assert empty["count"] == 0


# ---------------------------------------------- load_case_reports


def test_load_case_reports_is_newest_first_with_deep_links():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        # Ledger creation order (oldest first), like report_versions().
        versions=(
            _version("rv-1", created_at=T1),
            _version("rv-2", created_at=T3),
            _version("rv-3", created_at=T2),
            _version("rv-other", created_at=T3, case_id=OTHER),
        ),
    )
    controller = _controller(facade)

    payload = run(controller.load_case_reports(CASE))

    assert payload["case_id"] == CASE
    assert [row["version_id"] for row in payload["reports"]] == [
        "rv-2", "rv-3", "rv-1",
    ]
    assert payload["count"] == 3
    by_id = {row["version_id"]: row for row in payload["reports"]}
    assert by_id["rv-2"]["trigger"] == "material_added"
    assert by_id["rv-2"]["created_at"] == T3.isoformat()
    assert by_id["rv-2"]["detail_url"] == "/reports/rv-2"
    assert facade.versions_calls == [CASE]


def test_load_case_reports_limits_validates_and_distinguishes_states():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        versions=(
            _version("rv-1", created_at=T1),
            _version("rv-2", created_at=T2),
        ),
    )
    controller = _controller(facade)

    limited = run(controller.load_case_reports(CASE, limit=1))
    assert [row["version_id"] for row in limited["reports"]] == ["rv-2"]

    with pytest.raises(ValueError):
        run(controller.load_case_reports("  "))
    with pytest.raises(ValueError):
        run(controller.load_case_reports(CASE, limit=-1))
    with pytest.raises(RuntimeError, match="report_versions"):
        run(_controller(CaseOnlyFacade()).load_case_reports(CASE))
    with pytest.raises(LookupError):
        run(controller.load_case_reports("case-ghost"))

    failing = FakeLinkedFacade(
        overviews=[_overview(CASE)], versions_error=RuntimeError("no ledger")
    )
    with pytest.raises(RuntimeError, match="no ledger"):
        run(_controller(failing).load_case_reports(CASE))

    empty = run(_controller(
        FakeLinkedFacade(overviews=[_overview(CASE)], versions=())
    ).load_case_reports(CASE))
    assert empty["reports"] == []
    assert empty["count"] == 0


# ------------------------------------------------ deep-link projections


def test_report_detail_url_builds_safe_links_only():
    from prism.webui.reports import report_detail_url

    assert report_detail_url("rv-1") == "/reports/rv-1"
    assert report_detail_url(" report-9._- ") == "/reports/report-9._-"
    for bad in ("", "  ", "a/b", "../etc", "sp ace"):
        with pytest.raises(ValueError):
            report_detail_url(bad)
    with pytest.raises(TypeError):
        report_detail_url(None)


def test_journey_view_carries_the_report_deep_link():
    from prism.webui.journey import (
        journey_markdown,
        journey_view_data,
        material_row,
    )

    linked = _journey_view("mat-1", report_version_id="rv-9")
    data = journey_view_data(linked)
    assert data["report_version_id"] == "rv-9"
    assert data["report_url"] == "/reports/rv-9"
    assert "[打开报告](/reports/rv-9)" in journey_markdown(data)
    row = material_row(linked)
    assert row["report_version_id"] == "rv-9"
    assert row["report_url"] == "/reports/rv-9"

    unlinked = journey_view_data(_journey_view("mat-2"))
    assert unlinked["report_url"] is None
    assert "/reports/" not in journey_markdown(unlinked)
    assert material_row(_journey_view("mat-2"))["report_url"] is None


def test_report_detail_view_links_back_to_the_case_home():
    from prism.webui.reports import report_detail_view

    view = report_detail_view(_version("rv-1"))

    assert view["case_home_url"] == "/cases"


def test_report_detail_page_renders_a_case_home_link():
    from prism.webui.reports import (
        ReportCenterController,
        build_report_pages,
    )

    class FakeReportsFacade:
        async def report_versions(self, case_id=None, *, as_of=None):
            return (_version("rv-1"),)

        async def report_version(self, version_id):
            return _version("rv-1")

        async def export_report_pdf(self, version_id, output_path):
            raise AssertionError("not exercised")

    ui = _FakeUI()
    build_report_pages(ReportCenterController(FakeReportsFacade()), ui)
    run(ui.pages["/reports/{version_id}"]("rv-1"))

    links = [
        element for element in ui.elements if element.name == "link"
    ]
    back = [
        element for element in links
        if "案例主页" in str(element.args[0] if element.args else "")
    ]
    assert back, "expected a back-to-case-home link on the detail page"
    assert back[0].args[-1] == "/cases"


# ------------------------------------------------- fake-ui page seam


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


def _table_by_field(ui, field):
    for element in ui.elements:
        if element.name != "table":
            continue
        columns = element.kwargs.get("columns") or ()
        if any(column.get("field") == field for column in columns):
            return element
    raise AssertionError(f"no table with a {field!r} column")


def _labels(ui):
    return [element for element in ui.elements if element.name == "label"]


def _build_case_home(controller):
    from prism.webui.app import build_case_home_page

    ui = _FakeUI()
    build_case_home_page(controller, ui)
    ui.pages["/cases"]()
    return ui


def _select_case(ui, case_id=CASE):
    run(
        _table_by_field(ui, "case_id").kwargs["on_select"](
            SimpleNamespace(args=[{"case_id": case_id}])
        )
    )


# ------------------------------------------------- case-home linkage


def test_case_home_without_linkage_capabilities_keeps_the_legacy_page():
    ui = _build_case_home(_controller(CaseOnlyFacade()))

    label_texts = [label.text for label in _labels(ui)]
    assert not any(
        "选中案例的材料" in text for text in label_texts
    )
    assert not any(
        "选中案例的报告版本" in text for text in label_texts
    )
    with pytest.raises(AssertionError):
        _element(ui, "button", text="刷新案例材料")
    with pytest.raises(AssertionError):
        _element(ui, "button", text="刷新案例报告")
    markdowns = [
        element for element in ui.elements if element.name == "markdown"
    ]
    assert len(markdowns) == 4  # state, timeline, detail, evidence only


def test_case_home_selection_loads_materials_and_reports():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys=(
            _journey_view("mat-new", occurred=T3, report_version_id="rv-2"),
            _journey_view("mat-broken", lifecycle="failed", occurred=T2),
        ),
        versions=(
            _version("rv-1", created_at=T1),
            _version("rv-2", created_at=T3),
        ),
    )
    controller = _controller(facade)
    ui = _build_case_home(controller)

    _select_case(ui)

    assert facade.journeys_calls == [{"case_id": CASE, "status": None}]
    materials_table = _table_by_field(ui, "material_id")
    assert [row["material_id"] for row in materials_table.rows] == [
        "mat-new", "mat-broken",
    ]
    statuses = {row["material_id"]: row["ui_status"]
               for row in materials_table.rows}
    assert statuses["mat-new"] == "成功"
    assert statuses["mat-broken"] == "失败"

    assert facade.versions_calls == [CASE]
    reports_table = _table_by_field(ui, "version_id")
    assert [row["version_id"] for row in reports_table.rows] == [
        "rv-2", "rv-1",
    ]
    assert reports_table.rows[0]["detail_url"] == "/reports/rv-2"

    # Each panel reports its own state (the shared message label keeps only
    # the latest handler's text, so the per-panel markdowns are asserted).
    contents = " ".join(
        str(element.content) for element in ui.elements
        if element.name == "markdown"
    )
    assert "2 条近期材料运行" in contents
    assert "2 个报告版本,最新在前" in contents


def test_case_home_materials_status_filter_reaches_the_facade():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys=(
            _journey_view("mat-new", occurred=T3),
            _journey_view("mat-broken", lifecycle="failed", occurred=T2),
        ),
        versions=(_version("rv-1"),),
    )
    ui = _build_case_home(_controller(facade))

    _select_case(ui)
    status_select = _element(
        ui, "select", label="材料状态筛选"
    )
    status_select.value = "failed"
    run(
        _element(ui, "button", text="刷新案例材料").kwargs[
            "on_click"
        ](None)
    )

    assert facade.journeys_calls[-1] == {"case_id": CASE, "status": "failed"}
    materials_table = _table_by_field(ui, "material_id")
    assert [row["material_id"] for row in materials_table.rows] == [
        "mat-broken"
    ]


def test_case_home_distinguishes_empty_panels_from_load_failures():
    failing = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys_error=RuntimeError("ledger locked"),
        versions_error=RuntimeError("no ledger"),
    )
    ui = _build_case_home(_controller(failing))

    _select_case(ui)

    # Each panel carries its own failure state; neither failure is rendered
    # as the "no ... recorded" empty state.
    contents = " ".join(
        str(element.content) for element in ui.elements
        if element.name == "markdown"
    )
    assert "无法加载材料运行" in contents
    assert "无法加载报告版本" in contents
    assert "该案例暂无材料运行" not in contents
    assert "该案例暂无报告版本" not in contents

    empty = _build_case_home(_controller(
        FakeLinkedFacade(overviews=[_overview(CASE)])
    ))
    _select_case(empty)
    empty_contents = " ".join(
        str(item.content)
        for item in empty.elements if item.name == "markdown"
    )
    assert "该案例暂无材料运行" in empty_contents
    assert "该案例暂无报告版本" in empty_contents


def test_case_home_report_row_selection_opens_the_detail_route():
    facade = FakeLinkedFacade(
        overviews=[_overview(CASE)],
        journeys=(),
        versions=(
            _version("rv-1", created_at=T1),
            _version("rv-2", created_at=T3),
        ),
    )
    ui = _build_case_home(_controller(facade))

    _select_case(ui)
    reports_table = _table_by_field(ui, "version_id")
    run(
        reports_table.kwargs["on_select"](
            SimpleNamespace(args=[dict(reports_table.rows[0])])
        )
    )

    texts = " ".join(label.text for label in _labels(ui))
    assert "打开 /reports/rv-2" in texts
