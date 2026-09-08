"""WebUI 统合工作台主页:controller/view-model 与页面接缝(离线)。

The unified dashboard home is the new ``/`` entry: five Chinese navigation
cards over the existing English routes, honest aggregate stats (case
total/active, material total/failed, report-version count) read ONLY
through the facade's existing read-only operations (``case_overviews``,
``material_journeys``, ``report_versions`` — no new facade method, no
SQLite/Graphiti/LLM access), and two "recent activity" panels (latest five
material runs and report versions).  The honesty invariants carry over
(H-4/WB-3.6): pending/partial/unknown/failure never render as success; an
empty read (count 0, empty list) and a read failure (explicit error state,
never a fabricated number) and a missing capability (explicit unavailable
state) are three distinct states; missing row fields project as ``None``
and display as 未知.  Everything runs offline through synthetic facades
and a recording ``ui`` stand-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
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
T4 = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
T5 = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
T6 = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def _overview(case_id="case-rates", *, status="active", materials=2):
    return CaseOverview(
        case_id=case_id,
        case_type="policy",
        name=f"Case {case_id}",
        status=status,
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
    case_id="case-rates",
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


def _version(version_id, *, created_at=T2, case_id="case-rates",
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


def _sparse_version(version_id):
    """A duck-typed report version with missing optional display fields.

    ``ReportVersion`` itself always records a trigger and an aware
    ``created_at``; the sparse stand-in exercises the projection's honesty
    when a foreign or degraded source leaves them absent.
    """
    return SimpleNamespace(
        version_id=version_id,
        case_id="case-rates",
        as_of=None,
        created_at=None,
        input_hash="i" * 64,
        markdown_hash="m" * 64,
        summary_origin="deterministic",
        parent_version_id=None,
        trigger=None,
    )


class FakeDashboardFacade:
    """The facade shape the dashboard reads, with per-read failure switches."""

    def __init__(self, *, overviews=(), journeys=(), versions=(),
                 overviews_error=None, journeys_error=None,
                 versions_error=None):
        self._overviews = tuple(overviews)
        self._journeys = tuple(journeys)
        self._versions = tuple(versions)
        self._overviews_error = overviews_error
        self._journeys_error = journeys_error
        self._versions_error = versions_error
        self.overview_calls = []
        self.journeys_calls = []
        self.versions_calls = []

    async def case_overviews(self, **filters):
        self.overview_calls.append(dict(filters))
        if self._overviews_error is not None:
            raise self._overviews_error
        return self._overviews

    async def material_journeys(self, *, case_id=None, status=None):
        self.journeys_calls.append({"case_id": case_id, "status": status})
        if self._journeys_error is not None:
            raise self._journeys_error
        views = self._journeys
        if case_id is not None:
            views = tuple(
                view for view in views if view.case_id == case_id
            )
        if status is not None:
            views = tuple(
                view for view in views
                if view.lifecycle_status == status
            )
        return views

    async def report_versions(self, case_id=None, *, as_of=None):
        self.versions_calls.append(case_id)
        if self._versions_error is not None:
            raise self._versions_error
        versions = self._versions
        if case_id is not None:
            versions = tuple(
                version for version in versions
                if version.case_id == case_id
            )
        return versions


class CaseOnlyFacade:
    """The historical case-home facade shape (no journey/report reads)."""

    async def case_overviews(self, **filters):
        return ()

    async def case_overview(self, case_id):
        raise LookupError(case_id)

    async def query_historical_snapshot(
        self, case_id, as_of, *, stage=None, kinds=None
    ):
        return State(
            case_id=case_id, cutoff_at=as_of, case_type=None, status=None,
            nodes=(), facts=(), interpretations=(), evidence_gaps=(),
        )


def _controller(facade):
    from prism.webui.dashboard import DashboardController

    return DashboardController(facade)


# ---------------------------------------------------- navigation cards


def test_nav_cards_are_a_frozen_tuple_of_five_chinese_entries():
    from prism.webui.dashboard import NAV_CARDS, NavCard

    assert isinstance(NAV_CARDS, tuple)
    assert len(NAV_CARDS) == 5
    # URL 路由保持英文;标题与说明为中文。
    assert {card.route for card in NAV_CARDS} == {
        "/cases", "/materials", "/reports", "/evidence", "/debate",
    }
    assert {card.title for card in NAV_CARDS} == {
        "案例主页", "材料上传", "报告中心", "证据检索", "辩论剧场",
    }
    for card in NAV_CARDS:
        assert isinstance(card, NavCard)
        assert card.description and card.description.strip()
    with pytest.raises(FrozenInstanceError):
        NAV_CARDS[0].title = "篡改"


# ------------------------------------------------- capability gates


def test_controller_requires_case_overviews_and_detects_capabilities():
    class Bare:
        pass

    with pytest.raises(TypeError, match="case_overviews"):
        _controller(Bare())

    controller = _controller(CaseOnlyFacade())
    assert controller.materials_available is False
    assert controller.reports_available is False

    full = _controller(FakeDashboardFacade())
    assert full.materials_available is True
    assert full.reports_available is True


# ----------------------------------------------------- stat sections


def test_load_aggregates_stats_from_the_three_unfiltered_reads():
    facade = FakeDashboardFacade(
        overviews=(
            _overview("case-rates", status="active"),
            _overview("case-housing", status="active"),
            _overview("case-old", status="closed"),
        ),
        journeys=(
            _journey_view("mat-1"),
            _journey_view("mat-2", lifecycle="failed"),
            _journey_view("mat-3", lifecycle="pending"),
        ),
        versions=(_version("rv-1"), _version("rv-2")),
    )

    view = run(_controller(facade).load())

    assert view["cases"] == {
        "state": "ok", "error": None, "total": 3, "active": 2,
    }
    assert view["materials"]["state"] == "ok"
    assert view["materials"]["total"] == 3
    assert view["materials"]["failed"] == 1
    assert view["reports"]["state"] == "ok"
    assert view["reports"]["error"] is None
    assert view["reports"]["count"] == 2
    # 只读、无过滤的 facade 调用(跨案例聚合)。
    assert facade.overview_calls == [{}]
    assert facade.journeys_calls == [{"case_id": None, "status": None}]
    assert facade.versions_calls == [None]


def test_load_counts_zero_for_empty_reads_and_keeps_zero_an_own_state():
    view = run(_controller(FakeDashboardFacade()).load())

    assert view["cases"] == {
        "state": "ok", "error": None, "total": 0, "active": 0,
    }
    assert view["materials"]["state"] == "ok"
    assert view["materials"]["total"] == 0
    assert view["materials"]["failed"] == 0
    assert view["materials"]["recent"] == []
    assert view["reports"]["state"] == "ok"
    assert view["reports"]["error"] is None
    assert view["reports"]["count"] == 0
    assert view["reports"]["recent"] == []


def test_load_reports_read_failures_as_explicit_error_states():
    facade = FakeDashboardFacade(
        overviews_error=RuntimeError("overview store locked"),
        journeys=(
            _journey_view("mat-1"),
            _journey_view("mat-2", lifecycle="failed"),
        ),
        versions=(_version("rv-1"),),
    )

    view = run(_controller(facade).load())

    # 失败的段落是显式错误状态:计数为 None(绝不伪造 0),列表为空。
    assert view["cases"]["state"] == "error"
    assert view["cases"]["total"] is None
    assert view["cases"]["active"] is None
    assert "加载案例统计 failed (RuntimeError)" == view["cases"]["error"]
    # 其余读取不受影响。
    assert view["materials"]["state"] == "ok"
    assert view["materials"]["total"] == 2
    assert view["materials"]["failed"] == 1
    assert view["reports"]["state"] == "ok"
    assert view["reports"]["count"] == 1

    both = run(_controller(FakeDashboardFacade(
        journeys_error=RuntimeError("ledger locked"),
        versions_error=OSError("no ledger"),
    )).load())
    assert both["materials"]["state"] == "error"
    assert both["materials"]["total"] is None
    assert both["materials"]["failed"] is None
    assert both["materials"]["recent"] == []
    assert "加载材料统计 failed (RuntimeError)" == both["materials"]["error"]
    assert both["reports"]["state"] == "error"
    assert both["reports"]["count"] is None
    assert both["reports"]["recent"] == []
    assert "加载报告统计 failed (OSError)" == both["reports"]["error"]


def test_load_marks_missing_capabilities_explicitly_unavailable():
    view = run(_controller(CaseOnlyFacade()).load())

    assert view["cases"]["state"] == "ok"
    assert view["cases"]["total"] == 0
    # 能力缺失不是空列表,也不是错误:显式“不可用”。
    assert view["materials"]["state"] == "unavailable"
    assert view["materials"]["error"] is None
    assert view["materials"]["total"] is None
    assert view["materials"]["recent"] == []
    assert view["reports"]["state"] == "unavailable"
    assert view["reports"]["error"] is None
    assert view["reports"]["count"] is None
    assert view["reports"]["recent"] == []


def test_load_validates_limit_before_any_read():
    facade = FakeDashboardFacade()
    controller = _controller(facade)

    for bad in (0, -1, "5", 1.5):
        with pytest.raises((ValueError, TypeError)):
            run(controller.load(limit=bad))
    with pytest.raises(ValueError):
        run(controller.load(limit=True))
    assert facade.overview_calls == []
    assert facade.journeys_calls == []
    assert facade.versions_calls == []


# ------------------------------------------------- recent activity


def test_recent_materials_are_limited_newest_first_with_honest_badges():
    facade = FakeDashboardFacade(
        journeys=(
            # facade 契约:最近的结果在前。
            _journey_view("mat-newest", occurred=T6),
            _journey_view("mat-broken", lifecycle="failed", occurred=T5),
            _journey_view("mat-flight", lifecycle="pending", occurred=T4),
            _journey_view("mat-stale", lifecycle="unknown", occurred=T3),
            _journey_view("mat-5", occurred=T2),
            _journey_view("mat-6", occurred=T1),
        ),
    )

    view = run(_controller(facade).load())

    recent = view["materials"]["recent"]
    assert [row["material_id"] for row in recent] == [
        "mat-newest", "mat-broken", "mat-flight", "mat-stale", "mat-5",
    ]
    assert view["materials"]["total"] == 6
    by_id = {row["material_id"]: row for row in recent}
    for row in recent:
        assert set(row) == {
            "material_id", "display_name", "ui_status", "occurred_at",
        }
    assert by_id["mat-newest"]["display_name"] == "Policy update"
    assert by_id["mat-newest"]["occurred_at"] == T6.isoformat()
    # lifecycle_ui_status 徽章:只有 committed 是成功(H-4)。
    assert by_id["mat-newest"]["ui_status"] == "成功"
    assert by_id["mat-broken"]["ui_status"] == "失败"
    assert by_id["mat-flight"]["ui_status"] == "加载中"
    assert by_id["mat-stale"]["ui_status"] == "未知"


def test_recent_reports_are_newest_first_limited_and_deep_linked():
    facade = FakeDashboardFacade(
        # 账本创建顺序(最早在前),同 report_versions()。
        versions=(
            _version("rv-1", created_at=T1, trigger="initial"),
            _version("rv-2", created_at=T6),
            _version("rv-3", created_at=T5),
            _version("rv-4", created_at=T4),
            _version("rv-5", created_at=T3),
            _version("rv-6", created_at=T2),
        ),
    )

    view = run(_controller(facade).load())

    recent = view["reports"]["recent"]
    assert [row["version_id"] for row in recent] == [
        "rv-2", "rv-3", "rv-4", "rv-5", "rv-6",
    ]
    assert view["reports"]["count"] == 6
    by_id = {row["version_id"]: row for row in recent}
    for row in recent:
        assert set(row) == {
            "version_id", "trigger", "created_at", "detail_url",
        }
    assert "rv-1" not in by_id
    assert by_id["rv-2"]["trigger"] == "material_added"
    assert by_id["rv-2"]["created_at"] == T6.isoformat()
    assert by_id["rv-2"]["detail_url"] == "/reports/rv-2"


def test_missing_recent_fields_project_none_and_display_unknown():
    from prism.webui.dashboard import display_or_unknown

    facade = FakeDashboardFacade(
        journeys=(
            _journey_view("mat-ghost", display=None, occurred=None),
        ),
        versions=(_sparse_version("rv-x"),),
    )

    view = run(_controller(facade).load())

    material = view["materials"]["recent"][0]
    assert material["display_name"] == "mat-ghost"  # material_row 回退
    assert material["occurred_at"] is None
    report = view["reports"]["recent"][0]
    assert report["trigger"] is None
    assert report["created_at"] is None
    # 显示层约定:缺失字段显示“未知”。
    assert display_or_unknown(None) == "未知"
    assert display_or_unknown("") == "未知"
    assert display_or_unknown(T6.isoformat()) == T6.isoformat()


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


def _labels(ui):
    return [element for element in ui.elements if element.name == "label"]


def _links(ui):
    return [element for element in ui.elements if element.name == "link"]


def _table_by_field(ui, field):
    for element in ui.elements:
        if element.name != "table":
            continue
        columns = element.kwargs.get("columns") or ()
        if any(column.get("field") == field for column in columns):
            return element
    raise AssertionError(f"no table with a {field!r} column")


def _build_dashboard(controller):
    from prism.webui.dashboard import build_dashboard_page

    ui = _FakeUI()
    build_dashboard_page(controller, ui)
    run(ui.pages["/"]())
    return ui


def test_dashboard_page_renders_cards_stats_and_recent_activity():
    facade = FakeDashboardFacade(
        overviews=(
            _overview("case-rates"),
            _overview("case-old", status="closed"),
        ),
        journeys=(
            _journey_view("mat-newest", occurred=T6),
            _journey_view("mat-broken", lifecycle="failed", occurred=T5),
        ),
        versions=(_version("rv-1", created_at=T1, trigger="initial"),),
    )
    ui = _build_dashboard(_controller(facade))

    # 五张导航卡片:中文标题/说明 + 英文路由链接。
    links = _links(ui)
    by_target = {link.args[-1]: link for link in links}
    for route in ("/cases", "/materials", "/reports", "/evidence", "/debate"):
        assert route in by_target
    texts = " ".join(label.text for label in _labels(ui))
    for title in ("案例主页", "材料上传", "报告中心", "证据检索", "辩论剧场"):
        assert title in texts

    # 概览统计标签(数字来自 facade 只读数据)。
    assert "案例总数 2" in texts
    assert "活跃 1" in texts
    assert "材料总数 2" in texts
    assert "失败 1" in texts
    assert "报告版本 1" in texts

    # 最近动态:材料表(徽章列)与报告表。
    materials_table = _table_by_field(ui, "material_id")
    assert [row["material_id"] for row in materials_table.rows] == [
        "mat-newest", "mat-broken",
    ]
    statuses = {row["material_id"]: row["ui_status"]
                for row in materials_table.rows}
    assert statuses["mat-newest"] == "成功"
    assert statuses["mat-broken"] == "失败"
    reports_table = _table_by_field(ui, "version_id")
    assert [row["version_id"] for row in reports_table.rows] == ["rv-1"]
    assert reports_table.rows[0]["trigger"] == "initial"

    # 刷新入口存在。
    refresh = [
        element for element in ui.elements
        if element.name == "button" and "刷新" in element.text
    ]
    assert refresh


def test_dashboard_page_separates_empty_error_and_unavailable_states():
    failing = FakeDashboardFacade(
        overviews_error=RuntimeError("locked"),
        journeys_error=RuntimeError("ledger locked"),
        versions_error=OSError("no ledger"),
    )
    ui = _build_dashboard(_controller(failing))

    texts = " ".join(
        str(element.text) + str(element.content)
        for element in ui.elements
        if element.name in ("label", "markdown")
    )
    assert "加载失败" in texts
    assert "无法加载材料运行" in texts
    assert "无法加载报告版本" in texts
    # 失败绝不显示为 0 或“暂无”。
    assert "案例总数 0" not in texts
    assert "最近没有材料运行" not in texts
    assert "最近没有报告版本" not in texts

    unavailable = _build_dashboard(_controller(CaseOnlyFacade()))
    unavailable_texts = " ".join(
        str(element.text) + str(element.content)
        for element in unavailable.elements
        if element.name in ("label", "markdown")
    )
    assert "不可用" in unavailable_texts

    empty = _build_dashboard(_controller(FakeDashboardFacade()))
    empty_texts = " ".join(
        str(element.text) + str(element.content)
        for element in empty.elements
        if element.name in ("label", "markdown")
    )
    assert "案例总数 0" in empty_texts
    assert "材料总数 0" in empty_texts
    assert "报告版本 0" in empty_texts
    assert "最近没有材料运行" in empty_texts
    assert "最近没有报告版本" in empty_texts
    assert "加载失败" not in empty_texts


def test_dashboard_page_renders_unknown_for_missing_row_fields():
    facade = FakeDashboardFacade(
        journeys=(_journey_view("mat-ghost", display=None, occurred=None),),
        versions=(_sparse_version("rv-x"),),
    )
    ui = _build_dashboard(_controller(facade))

    materials_table = _table_by_field(ui, "material_id")
    assert materials_table.rows[0]["occurred_at"] == "未知"
    assert materials_table.rows[0]["display_name"] == "mat-ghost"
    reports_table = _table_by_field(ui, "version_id")
    assert reports_table.rows[0]["trigger"] == "未知"
    assert reports_table.rows[0]["created_at"] == "未知"


# ------------------------------------------------- app wiring


def test_create_app_registers_the_dashboard_at_root_and_case_home_at_cases(
    monkeypatch,
):
    import sys
    from types import ModuleType

    from prism.webui import app

    class _RecordingUI:
        def __init__(self):
            self.routes = []

        def page(self, route):
            def register(fn):
                self.routes.append(route)
                return fn

            return register

    ui = _RecordingUI()
    monkeypatch.setattr(app, "_nicegui", lambda: ui)
    monkeypatch.setattr(
        app, "_plotly_graph_objects", lambda: object()
    )
    fake_nicegui = ModuleType("nicegui")
    fake_nicegui.app = SimpleNamespace(name="nicegui-app")
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)

    app.create_app(CaseOnlyFacade())

    # 统合主页占用 /;案例主页迁至 /cases;能力门未开时其余路由不注册。
    assert "/" in ui.routes
    assert "/cases" in ui.routes
    for route in ("/materials", "/reports", "/evidence", "/debate"):
        assert route not in ui.routes


def test_report_detail_links_back_to_the_relocated_case_home():
    from prism.webui.reports import CASE_HOME_ROUTE, report_detail_view

    assert CASE_HOME_ROUTE == "/cases"
    assert report_detail_view(_version("rv-1"))["case_home_url"] == "/cases"
