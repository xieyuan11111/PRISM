"""Optional NiceGUI shell: the unified dashboard home, the PRISM case home
and the workbench pages behind it.

Importing this module never imports NiceGUI: :func:`create_app` resolves the
optional dependency lazily and raises the typed
:class:`WebUIUnavailableError` with install instructions when it is missing,
so the package — and PRISM's default runtime — stay dependency-free without
the ``webui`` extra.  All data flows through
:class:`~prism.webui.controller.CaseHomeController` over the injected
PrismAPI facade: the shell adds presentation only, never temporal logic, and
the CLI and this WebUI read the same facade methods so both surfaces show the
same timelines and evidence (FR-8.10).
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from prism.analyzer import ENTRY_KINDS, STAGES

from .controller import CaseHomeController, PrismFacade
from .dashboard import DashboardController, build_dashboard_page
from .reports import CASE_HOME_ROUTE
from .status import safe_error_text, safe_identifier

DEFAULT_TITLE = "PRISM 案例主页"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

NICEGUI_MISSING_MESSAGE = (
    "PRISM WebUI 需要可选依赖 nicegui;请使用以下命令安装:"
    'pip install ".[webui]"'
)
PLOTLY_MISSING_MESSAGE = (
    "时间线渲染需要可选依赖 plotly;请使用以下命令安装:"
    'pip install ".[webui]"'
)


class WebUIUnavailableError(RuntimeError):
    """A dependency needed by the optional WebUI is not installed."""


def _nicegui() -> Any:
    """Import the NiceGUI ``ui`` module lazily, or fail with a clear error."""
    try:
        from nicegui import ui
    except ImportError as error:
        raise WebUIUnavailableError(NICEGUI_MISSING_MESSAGE) from error
    return ui


def _plotly_graph_objects() -> Any:
    """Import Plotly only when a timeline figure is actually requested."""
    try:
        from plotly import graph_objects as go
    except ImportError as error:
        raise WebUIUnavailableError(PLOTLY_MISSING_MESSAGE) from error
    return go


_CASE_COLUMNS = [
    {"name": "case_id", "label": "案例", "field": "case_id",
     "align": "left", "sortable": True},
    {"name": "name", "label": "名称", "field": "name", "align": "left"},
    {"name": "case_type", "label": "类型", "field": "case_type",
     "align": "left", "sortable": True},
    {"name": "status", "label": "状态", "field": "status",
     "align": "left", "sortable": True},
    {"name": "material_count", "label": "材料数", "field": "material_count",
     "align": "right", "sortable": True},
    {"name": "latest_observed_at", "label": "最近观测",
     "field": "latest_observed_at", "align": "left"},
    {"name": "unresolved", "label": "未解决", "field": "unresolved",
     "align": "left"},
]

_TIMELINE_SECTIONS = (
    ("节点", "nodes"),
    ("有效事实", "facts"),
    ("已失效事实", "invalidated_facts"),
    ("解释", "interpretations"),
    ("关系", "relations"),
)

_EVIDENCE_BUCKETS = (
    "nodes",
    "facts",
    "invalidated_facts",
    "interpretations",
    "relations",
)

#: Columns of the selected case's recent material runs (Phase C; the row
#: projection is the shared journey ``material_row`` with its
#: ``lifecycle_ui_status`` badge).
_CASE_MATERIAL_COLUMNS = [
    {"name": "material_id", "label": "材料", "field": "material_id",
     "align": "left", "sortable": True},
    {"name": "display_name", "label": "标题", "field": "display_name",
     "align": "left"},
    {"name": "lifecycle_status", "label": "生命周期",
     "field": "lifecycle_status", "align": "left", "sortable": True},
    {"name": "ui_status", "label": "状态", "field": "ui_status",
     "align": "left"},
    {"name": "occurred_at", "label": "最近结果", "field": "occurred_at",
     "align": "left"},
    {"name": "failed_stage", "label": "失败阶段", "field": "failed_stage",
     "align": "left"},
]

#: Columns of the selected case's report versions (Phase C).
_CASE_REPORT_COLUMNS = [
    {"name": "version_id", "label": "版本", "field": "version_id",
     "align": "left", "sortable": True},
    {"name": "trigger", "label": "触发", "field": "trigger",
     "align": "left", "sortable": True},
    {"name": "created_at", "label": "创建时间", "field": "created_at",
     "align": "left", "sortable": True},
]


def _stage_line(entry: dict[str, Any]) -> str:
    window = entry["valid_at"] + (
        f" \u2192 {entry['invalid_at']}" if entry["invalid_at"] else ""
    )
    sources = ", ".join(entry["source_ids"]) or "_无来源_"
    marker = entry.get("node_type") or entry.get("stance") or entry["kind"]
    return (
        f"- `{entry['episode_key']}` [{marker}] {entry['summary']} "
        f"({window}; {sources})"
    )


def _state_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        f"**案例** {snapshot['case_id']} \u2014 类型 "
        f"{snapshot.get('case_type') or '?'},状态 "
        f"{snapshot.get('status') or '?'}",
        f"**截止时间** {snapshot['cutoff_at']}",
    ]
    for label, key in _TIMELINE_SECTIONS + (("证据缺口", "evidence_gaps"),):
        lines.append(f"- {label}: {len(snapshot.get(key) or ())}")
    return "\n".join(lines)


def _timeline_markdown(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    for heading, key in _TIMELINE_SECTIONS + (("证据缺口", "evidence_gaps"),):
        entries = snapshot.get(key) or ()
        lines.append(f"### {heading} ({len(entries)})")
        if not entries:
            lines.append("_无_")
        for entry in entries:
            if key == "evidence_gaps":
                lines.append(
                    f"- `{entry.get('episode_key') or '-'}` "
                    f"[{entry['gap_type']}] {entry['detail']}"
                )
            else:
                lines.append(_stage_line(entry))
    return "\n".join(lines)


def _evidence_markdown(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    for bucket in _EVIDENCE_BUCKETS:
        for entry in snapshot.get(bucket) or ():
            for locator in entry.get("evidence") or ():
                where = []
                if locator.get("paragraph") is not None:
                    where.append(f"第 {locator['paragraph']} 段")
                if locator.get("page") is not None:
                    where.append(f"第 {locator['page']} 页")
                at = f" ({'; '.join(where)})" if where else ""
                quote = f': "{locator["quote"]}"' if locator.get("quote") else ""
                lines.append(
                    f"- `{entry['episode_key']}` \u2014 {locator['source_id']} "
                    f"\u2014 {locator['corpus_path']}{at}{quote}"
                )
    if not lines:
        lines.append("_该快照中没有证据定位符_")
    return "\n".join(lines)


_LAYER_COLORS = {
    "fact": "#2563eb",
    "interpretation": "#d97706",
    "provenance": "#059669",
}


def _timeline_sort_key(row: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (
        str(row.get("valid_at") or ""),
        str(row.get("episode_key") or ""),
        str(row.get("kind") or ""),
        bool(row.get("invalidated")),
    )


def build_timeline_figure(timeline_rows: list[dict[str, Any]]) -> Any:
    """Build a deterministic Plotly timeline from controller-produced rows.

    Each trace contains exactly one snapshot entry and carries its stable
    ``episode_key`` as click ``customdata``. No filtering or stage inference
    occurs here; the facade/analyzer has already decided snapshot membership.
    """
    go = _plotly_graph_objects()
    figure = go.Figure()
    legend_seen: set[str] = set()
    for row in sorted(timeline_rows, key=_timeline_sort_key):
        invalidated = bool(row.get("invalidated"))
        status = "已失效" if invalidated else "有效"
        layer = str(row.get("layer") or "unknown")
        kind = str(row.get("kind") or "unknown")
        legend_group = f"{layer}:{status}"
        sources = ", ".join(str(item) for item in row.get("source_ids") or ())
        hover = "<br>".join((
            f"事件: {escape(str(row.get('episode_key') or ''))}",
            f"类型/层级: {escape(kind)} / {escape(layer)}",
            f"状态: {status}",
            f"摘要: {escape(str(row.get('summary') or ''))}",
            f"生效时间: {escape(str(row.get('valid_at') or ''))}",
            f"失效时间: {escape(str(row.get('invalid_at') or '-'))}",
            f"来源 ID: {escape(sources or '-')}",
        ))
        figure.add_trace(go.Scatter(
            x=[row.get("valid_at")],
            y=[f"{layer} / {kind}"],
            mode="markers",
            name=f"{layer} — {status}",
            legendgroup=legend_group,
            showlegend=legend_group not in legend_seen,
            customdata=[row.get("episode_key")],
            text=[hover],
            hovertemplate="%{text}<extra></extra>",
            marker={
                "color": _LAYER_COLORS.get(layer, "#64748b"),
                "symbol": "x" if invalidated else "circle",
                "size": 13 if invalidated else 11,
                "line": {"color": "#991b1b" if invalidated else "#ffffff", "width": 2},
            },
        ))
        legend_seen.add(legend_group)
    figure.update_layout(
        title="历史快照时间线",
        xaxis_title="生效时间",
        yaxis_title="层级 / 类型",
        hovermode="closest",
        legend_title_text="层级与状态",
        margin={"l": 80, "r": 30, "t": 60, "b": 60},
    )
    return figure


def _detail_markdown(entry: dict[str, Any]) -> str:
    """Render full point metadata and portable evidence locators."""
    status = "已失效" if entry.get("invalidated") else "有效"
    sources = ", ".join(entry.get("source_ids") or ()) or "_无_"
    lines = [
        f"### `{entry['episode_key']}` — {status}",
        f"- 类型/层级: `{entry['kind']}` / `{entry['layer']}`",
        f"- 摘要: {entry['summary']}",
        f"- 生效时间: {entry['valid_at']}",
        f"- 失效时间: {entry.get('invalid_at') or '_未失效_'}",
        f"- 来源 ID: {sources}",
    ]
    core_fields = {
        "episode_key", "kind", "layer", "summary", "valid_at", "invalid_at",
        "source_ids", "evidence", "invalidated",
    }
    for name in sorted(set(entry) - core_fields):
        value = entry[name]
        if value is not None:
            lines.append(f"- {name}: {value}")
    lines.append("#### 证据定位符")
    evidence = entry.get("evidence") or ()
    if not evidence:
        lines.append("_该条目没有证据定位符_")
    for locator in evidence:
        where = []
        if locator.get("paragraph") is not None:
            where.append(f"第 {locator['paragraph']} 段")
        if locator.get("page") is not None:
            where.append(f"第 {locator['page']} 页")
        location = f" ({'; '.join(where)})" if where else ""
        lines.append(
            f"- `{locator['source_id']}` — {locator['corpus_path']}{location}"
        )
        lines.append(f"  - 引文: {locator.get('quote') or '_无_'}")
    return "\n".join(lines)


def _clicked_episode_key(event: Any) -> str:
    args = getattr(event, "args", None)
    if not isinstance(args, dict):
        raise ValueError("timeline click did not contain Plotly point data")
    points = args.get("points")
    if not isinstance(points, list) or not points or not isinstance(points[0], dict):
        raise ValueError("timeline click did not contain a Plotly point")
    customdata = points[0].get("customdata")
    if isinstance(customdata, (list, tuple)):
        customdata = customdata[0] if customdata else None
    if not isinstance(customdata, str) or not customdata.strip():
        raise ValueError("timeline point has no stable episode_key")
    return customdata.strip()


def _stage_options() -> dict[str, str]:
    return {"": "全部阶段"} | {stage: stage for stage in sorted(STAGES)}


def _material_status_options() -> dict[str, str]:
    """The lifecycle filter vocabulary, shared with the journey list page."""
    return {
        "": "全部状态",
        "committed": "committed",
        "failed": "failed",
        "pending": "processing",
        "unknown": "unknown",
    }


def _kind_options() -> dict[str, str]:
    return {"": "全部类型"} | {kind: kind for kind in sorted(ENTRY_KINDS)}


def _facade_supports(api: object, *operations: str) -> bool:
    """Whether the injected facade provides every named operation."""
    return all(callable(getattr(api, name, None)) for name in operations)


def _default_upload_staging_root() -> Path:
    """The controlled staging default: ``<PRISM_HOME>/staging/uploads``."""
    from prism.config import PathConfig

    return PathConfig.prism_home() / "staging" / "uploads"


def build_case_home_page(
    controller: CaseHomeController,
    ui: Any,
    *,
    title: str = DEFAULT_TITLE,
    route: str = CASE_HOME_ROUTE,
) -> Any:
    """Register the case-home page on the given ``ui`` module.

    The case home lives at ``/cases`` since the unified dashboard took over
    ``/``; the route stays a parameter so embedders can place it elsewhere.
    The ``ui`` module is injected so the page construction — the controls,
    panels and their handlers — is a seam testable without NiceGUI installed;
    every handler delegates to the controller and reports explicit errors in
    the message label instead of swallowing them.
    """
    @ui.page(route)
    def case_home() -> None:
        timeline_plot: Any | None = None
        message = ui.label("加载案例开始。")

        with ui.card().classes("w-full"):
            ui.label("案例筛选").classes("text-bold")
            with ui.row():
                search = ui.input(label="搜索", placeholder="案例 ID 或名称")
                type_input = ui.input(label="类型(精确)", placeholder="policy")
                status_input = ui.input(label="状态(精确)", placeholder="active")
                unresolved = ui.switch("仅看未解决", value=False)
            with ui.row():
                as_of_input = ui.input(
                    label="截止时间(ISO 8601,含时区)",
                    placeholder="2026-02-02T00:00:00+00:00",
                )
                stage_select = ui.select(
                    options=_stage_options(), value="", label="阶段"
                )
                kind_select = ui.select(
                    options=_kind_options(), value="", label="类型"
                )

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _refresh_cases(event: Any = None) -> None:
            try:
                view = await controller.load_cases(
                    search=search.value or "",
                    case_type=type_input.value or "",
                    status=status_input.value or "",
                    unresolved_only=bool(unresolved.value),
                )
            except Exception as error:
                _report(safe_error_text("加载案例", error))
                return
            cases_table.rows = view["cases"]
            cases_table.update()
            _report(f"已加载 {view['count']} 个案例")

        async def _on_row_selected(event: Any = None) -> None:
            rows = list(getattr(event, "args", None) or ())
            if not rows:
                return
            row = rows[0]
            case_id = row.get("case_id", "") if isinstance(row, dict) else ""
            if not case_id:
                return
            try:
                view = await controller.select_case(case_id)
            except Exception as error:
                _report(safe_error_text("选择案例", error))
                return
            _report(
                f"已选择 {view['case_id']} \u2014 {view['name']} "
                f"({view['status']})"
            )
            # Phase C linkage: selecting a case also loads its recent
            # material runs and report versions (each panel only when the
            # facade provides that read; a load failure is reported by the
            # panel itself, never as an empty list).
            if controller.materials_available:
                await _refresh_case_materials()
            if controller.reports_available:
                await _refresh_case_reports()

        async def _load_snapshot(event: Any = None) -> None:
            nonlocal timeline_plot
            if controller.selected_case_id is None:
                _report("请先在表格中选择案例")
                return
            try:
                view = await controller.load_snapshot(
                    controller.selected_case_id,
                    as_of_input.value or "",
                    stage=stage_select.value or None,
                    kinds=(kind_select.value,) if kind_select.value else None,
                )
            except Exception as error:
                _report(safe_error_text("加载快照", error))
                return
            snapshot = view["snapshot"]
            try:
                figure = build_timeline_figure(view["timeline"])
            except Exception as error:
                _report(safe_error_text("渲染时间线", error))
                return
            if timeline_plot is None:
                with timeline_plot_container:
                    timeline_plot = ui.plotly(figure).classes("w-full")
                    timeline_plot.on("plotly_click", _on_timeline_click)
            else:
                timeline_plot.update_figure(figure)
            state_md.content = _state_markdown(snapshot)
            timeline_md.content = _timeline_markdown(snapshot)
            evidence_md.content = _evidence_markdown(snapshot)
            for element in (state_md, timeline_md, evidence_md):
                element.update()
            _report(
                f"{snapshot['cutoff_at']} 的快照:"
                f"{len(snapshot['nodes'])} 个节点、"
                f"{len(snapshot['facts'])} 条有效事实、"
                f"{len(snapshot['invalidated_facts'])} 条已失效事实"
            )

        async def _on_timeline_click(event: Any) -> None:
            episode_key = ""
            try:
                episode_key = _clicked_episode_key(event)
                detail = controller.select_timeline_point(episode_key)
            except Exception as error:
                safe_key = safe_identifier(episode_key)
                _report(
                    f"未知时间线点: {safe_key}"
                    if safe_key is not None
                    else safe_error_text("选择时间线点", error)
                )
                return
            timeline_detail_md.content = _detail_markdown(detail)
            timeline_detail_md.update()
            _report(f"已选择时间线点 {episode_key}")

        # --------------------------------------- Phase C linkage panels
        # Both handlers and cards exist only when the controller detected
        # the matching read-only facade capability; an older case-only
        # facade keeps the exact legacy page.
        async def _refresh_case_materials(event: Any = None) -> None:
            if controller.selected_case_id is None:
                _report("请先在表格中选择案例")
                return
            status_filter = case_material_status.value or None
            try:
                payload = await controller.load_case_materials(
                    controller.selected_case_id, status=status_filter
                )
            except Exception as error:
                _report(safe_error_text("加载案例材料", error))
                case_materials_md.content = (
                    "**失败** \u2014 无法加载材料运行"
                )
                case_materials_md.update()
                return
            case_materials_table.rows = payload["materials"]
            case_materials_table.update()
            case_materials_md.content = (
                "_该案例暂无材料运行_"
                if payload["count"] == 0
                else (
                    f"_{payload['count']} 条近期材料运行 "
                    "(跨会话结果账本),最新在前_"
                )
            )
            case_materials_md.update()
            _report(
                f"已为 {payload['case_id']} 加载 "
                f"{payload['count']} 条材料运行"
            )

        async def _refresh_case_reports(event: Any = None) -> None:
            if controller.selected_case_id is None:
                _report("请先在表格中选择案例")
                return
            try:
                payload = await controller.load_case_reports(
                    controller.selected_case_id
                )
            except Exception as error:
                _report(safe_error_text("加载案例报告", error))
                case_reports_md.content = (
                    "**失败** \u2014 无法加载报告版本"
                )
                case_reports_md.update()
                return
            case_reports_table.rows = payload["reports"]
            case_reports_table.update()
            case_reports_md.content = (
                "_该案例暂无报告版本_"
                if payload["count"] == 0
                else f"_{payload['count']} 个报告版本,最新在前_"
            )
            case_reports_md.update()
            _report(
                f"已为 {payload['case_id']} 加载 "
                f"{payload['count']} 个报告版本"
            )

        async def _open_case_report(event: Any = None) -> None:
            rows = list(getattr(event, "args", None) or ())
            if not rows:
                return
            row = rows[0]
            route = row.get("detail_url", "") if isinstance(row, dict) else ""
            if not route:
                return
            navigate = getattr(ui, "navigate", None)
            opener = getattr(navigate, "to", None)
            if callable(opener):
                opener(route)
            else:
                _report(
                    f"已选择报告 {row.get('version_id')};打开 {route}"
                )

        with ui.card().classes("w-full"):
            cases_table = ui.table(
                columns=_CASE_COLUMNS,
                rows=[],
                selection="single",
                on_select=_on_row_selected,
            )
            with ui.row():
                ui.button("刷新案例", on_click=_refresh_cases)
                ui.button("加载快照", on_click=_load_snapshot)

        with ui.card().classes("w-full"):
            with ui.expansion("案例状态"):
                state_md = ui.markdown("_尚未加载快照_")
            with ui.expansion("时间线"):
                timeline_plot_container = ui.column().classes("w-full")
                timeline_md = ui.markdown("_尚未加载快照_")
                timeline_detail_md = ui.markdown("_点击时间线节点查看详情。_")
            with ui.expansion("证据"):
                evidence_md = ui.markdown("_尚未加载快照_")

        # Phase C linkage panels come after the snapshot card so the legacy
        # page (and its element order) is preserved when the capabilities
        # are absent.
        if controller.materials_available:
            with ui.card().classes("w-full"):
                ui.label(
                    "选中案例的材料(近期运行,跨会话)"
                ).classes("text-bold")
                case_material_status = ui.select(
                    options=_material_status_options(),
                    value="",
                    label="材料状态筛选(生命周期)",
                )
                with ui.row():
                    ui.button(
                        "刷新案例材料",
                        on_click=_refresh_case_materials,
                    )
                case_materials_table = ui.table(
                    columns=_CASE_MATERIAL_COLUMNS,
                    rows=[],
                )
                case_materials_md = ui.markdown(
                    "_选择案例以加载其近期材料运行_"
                )

        if controller.reports_available:
            with ui.card().classes("w-full"):
                ui.label("选中案例的报告版本").classes("text-bold")
                with ui.row():
                    ui.button(
                        "刷新案例报告", on_click=_refresh_case_reports
                    )
                case_reports_table = ui.table(
                    columns=_CASE_REPORT_COLUMNS,
                    rows=[],
                    selection="single",
                    on_select=_open_case_report,
                )
                case_reports_md = ui.markdown(
                    "_选择案例以加载其报告版本_"
                )

    return case_home


def create_app(
    api: PrismFacade,
    *,
    title: str = DEFAULT_TITLE,
    upload_staging_root: object = None,
    upload_controlled_root: object = None,
) -> Any:
    """Build the case-home NiceGUI pages over ``api`` without serving them.

    Raises :class:`WebUIUnavailableError` with install instructions when
    NiceGUI or the timeline's Plotly renderer is missing; neither optional
    dependency is imported at module scope.

    ``PrismFacade`` promises only the case-home queries, and this factory
    honours exactly that contract: the case home always registers, while
    every richer page (debate theater, evidence browser, the material
    intake with its workbench sections) registers only when the injected
    facade provides that page's operations — an older, narrower facade
    (e.g. a case-only fake) keeps building the app instead of crashing at
    construction.

    ``upload_staging_root`` (a path or a lazy provider resolving to one)
    anchors the browser-upload staging area; it defaults to
    ``<PRISM_HOME>/staging/uploads``.  The staging service validates on
    every use that this root sits inside the controlled root —
    ``PRISM_HOME`` by default, or the explicitly declared
    ``upload_controlled_root`` (a path or lazy provider) for callers whose
    staging root deliberately lives elsewhere.
    """
    ui = _nicegui()
    # This app factory requests the timeline-enabled case home. Keep the
    # dependency check here (not at import/controller construction time) so a
    # partial installation fails explicitly before any page is registered.
    _plotly_graph_objects()
    controller = CaseHomeController(api)
    build_case_home_page(controller, ui, title=title, route=CASE_HOME_ROUTE)
    # The unified dashboard always owns ``/`` (it needs only the case
    # overview read; its richer sections degrade to explicit unavailable
    # states on narrower facades).
    build_dashboard_page(DashboardController(api), ui)
    from .debate import DebateTheaterController, build_debate_theater_page
    from .evidence import EvidenceBrowserController, build_evidence_page
    from .journey import MaterialJourneyController
    from .materials import MaterialEntryController, build_material_entry_page
    from .upload import UploadController, UploadStagingService

    if _facade_supports(api, "debate_case", "follow_up_debate"):
        build_debate_theater_page(DebateTheaterController(api), ui)
    if _facade_supports(api, "search") or _facade_supports(
        api, "search_evidence"
    ):
        build_evidence_page(EvidenceBrowserController(api), ui)
    if _facade_supports(api, "add_material"):
        if upload_staging_root is None:
            upload_staging_root = _default_upload_staging_root()
        build_material_entry_page(
            MaterialEntryController(api),
            ui,
            upload_controller=UploadController(
                api,
                UploadStagingService(
                    upload_staging_root,
                    controlled_root=upload_controlled_root,
                ),
            ),
            journey_controller=(
                MaterialJourneyController(api)
                if _facade_supports(
                    api,
                    "material_journey",
                    "material_journeys",
                    "process_material",
                )
                else None
            ),
        )
    from .reports import ReportCenterController, build_report_pages

    if _facade_supports(
        api, "report_versions", "report_version", "export_report_pdf"
    ):
        build_report_pages(ReportCenterController(api), ui)
    from nicegui import app as nicegui_app

    return nicegui_app


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TITLE",
    "NICEGUI_MISSING_MESSAGE",
    "PLOTLY_MISSING_MESSAGE",
    "CaseHomeController",
    "DashboardController",
    "WebUIUnavailableError",
    "build_case_home_page",
    "build_timeline_figure",
    "create_app",
]
