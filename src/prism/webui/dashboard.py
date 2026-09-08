"""Dependency-free controller/view-model seam for the unified workbench home.

The dashboard is the WebUI's new ``/`` entry: five Chinese navigation cards
over the existing English routes (the case home itself now lives at
``/cases``), honest aggregate statistics and two "recent activity" panels.
Every read goes through the injected async facade's EXISTING read-only
operations — ``PrismAPI.case_overviews()``, ``PrismAPI.material_journeys()``
and ``PrismAPI.report_versions()`` — never the store, the graph or an LLM,
and no new facade method is introduced (H-6).

Honesty invariants (H-4/WB-3.6): a successful empty read is a count of 0 /
an empty list; a facade failure is an explicit per-section error state whose
counts stay ``None`` — never a fabricated 0 — and a facade without the
operation is an explicit unavailable state, so empty, failed, unavailable
and loaded are four distinct states.  The material badge is the shared
``lifecycle_ui_status`` mapping, so ``pending``/``unknown`` (and any
unrecognized lifecycle) never render as success.  Missing row fields
project as ``None`` and display as 未知.  Everything here is NiceGUI-free;
the page seam receives an injected ``ui`` module so tests run offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .journey import material_row
from .reports import CASE_HOME_ROUTE, report_detail_url, report_row
from .status import safe_error_text

#: The page title of the unified dashboard home.
DEFAULT_DASHBOARD_TITLE = "PRISM 统合工作台"

#: How many recent rows each activity panel shows by default; a
#: presentation cap only — the facade still decides membership and order.
RECENT_LIMIT = 5

#: Displayed for a row field the ledger did not record (H-4: missing is
#: its own state, never a borrowed value or a success).
UNKNOWN_TEXT = "未知"

_SECTION_OK = "ok"
_SECTION_ERROR = "error"
_SECTION_UNAVAILABLE = "unavailable"

_MIN_INSTANT = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class NavCard:
    """One dashboard entry card: a Chinese title/description over an
    existing English route.  Frozen because navigation is static config."""

    route: str
    title: str
    description: str


#: The five workbench entries, in reading order (URL 路由保持英文).
NAV_CARDS: tuple[NavCard, ...] = (
    NavCard(
        route=CASE_HOME_ROUTE,
        title="案例主页",
        description="选择案例,查看历史快照、时间线与证据",
    ),
    NavCard(
        route="/materials",
        title="材料上传",
        description="上传新材料,跟踪七步处理旅程与状态",
    ),
    NavCard(
        route="/reports",
        title="报告中心",
        description="浏览不可变报告版本,回溯证据并导出 PDF",
    ),
    NavCard(
        route="/evidence",
        title="证据检索",
        description="按查询与阶段/类型条件检索证据条目",
    ),
    NavCard(
        route="/debate",
        title="辩论剧场",
        description="围绕案例快照开展多视角辩论与追问",
    ),
)


class DashboardFacade(Protocol):
    """The existing facade reads the dashboard aggregates (no new method)."""

    async def case_overviews(self, **filters: object) -> object: ...

    async def material_journeys(
        self, *, case_id: str | None = None, status: str | None = None
    ) -> object: ...

    async def report_versions(
        self, case_id: str | None = None, *, as_of: datetime | None = None
    ) -> object: ...


def display_or_unknown(value: object) -> str:
    """Render one row field, showing 未知 when nothing was recorded."""
    text = str(value) if value is not None else ""
    return text if text.strip() else UNKNOWN_TEXT


def _validated_limit(limit: Any) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")
    return limit


def _material_activity_row(view: object) -> dict[str, Any]:
    """The dashboard's recent-material row: the shared journey ``material_row``
    projection reduced to the four displayed fields (badge included)."""
    row = material_row(view)
    return {
        "material_id": row["material_id"],
        # display_name already falls back to the material id (WB-2.1).
        "display_name": row["display_name"],
        "ui_status": row["ui_status"],
        "occurred_at": row["occurred_at"],
    }


def _report_activity_row(version: object) -> dict[str, Any]:
    """The dashboard's recent-report row, with the Phase C deep link."""
    row = report_row(version)
    return {
        "version_id": row["version_id"],
        "trigger": row["trigger"],
        "created_at": row["created_at"],
        "detail_url": report_detail_url(row["version_id"]),
    }


class DashboardController:
    """View-model adapter aggregating the facade's read-only ledgers.

    :meth:`load` performs the three unfiltered reads independently and
    captures each read's outcome in its own section — a failure in one
    ledger degrades exactly that section to an explicit error state and
    never fabricates numbers, an empty ledger stays a genuine 0, and a
    facade without an operation reports that capability as unavailable.
    """

    def __init__(self, api: DashboardFacade) -> None:
        if not callable(getattr(api, "case_overviews", None)):
            raise TypeError("api must provide case_overviews()")
        self._api = api
        journeys = getattr(api, "material_journeys", None)
        self._material_journeys = journeys if callable(journeys) else None
        report_versions = getattr(api, "report_versions", None)
        self._report_versions = (
            report_versions if callable(report_versions) else None
        )

    @property
    def materials_available(self) -> bool:
        """Whether the facade provides the read-only material-run listing."""
        return self._material_journeys is not None

    @property
    def reports_available(self) -> bool:
        """Whether the facade provides the read-only report-version listing."""
        return self._report_versions is not None

    async def load(
        self, *, limit: int | None = RECENT_LIMIT
    ) -> dict[str, Any]:
        """Load every dashboard section as JSON-safe view data.

        ``limit`` caps the two recent-activity lists (a presentation cap
        over the facade's own order) and is validated before any read.
        """
        row_limit = _validated_limit(limit)
        return {
            "cases": await self._load_cases(),
            "materials": await self._load_materials(row_limit),
            "reports": await self._load_reports(row_limit),
        }

    async def _load_cases(self) -> dict[str, Any]:
        try:
            overviews = tuple(await self._api.case_overviews())
        except Exception as error:
            return self._error_section(
                "加载案例统计", error, "total", "active"
            )
        # "active" is the ledger's exact status value, the same match the
        # case filter uses — anything else is simply not counted active.
        active = sum(
            1 for item in overviews
            if getattr(item, "status", None) == "active"
        )
        return {
            "state": _SECTION_OK,
            "error": None,
            "total": len(overviews),
            "active": active,
        }

    async def _load_materials(self, limit: int | None) -> dict[str, Any]:
        if self._material_journeys is None:
            return self._unavailable_section("total", "failed", "recent")
        try:
            views = tuple(await self._material_journeys())
        except Exception as error:
            return self._error_section(
                "加载材料统计", error, "total", "failed", "recent"
            )
        rows = [_material_activity_row(view) for view in views]
        # The facade already orders most recent outcome first; the limit is
        # a presentation cap over that order and never re-sorts.
        recent = rows if limit is None else rows[:limit]
        return {
            "state": _SECTION_OK,
            "error": None,
            "total": len(rows),
            "failed": sum(
                1 for view in views
                if getattr(view, "lifecycle_status", None) == "failed"
            ),
            "recent": recent,
        }

    async def _load_reports(self, limit: int | None) -> dict[str, Any]:
        if self._report_versions is None:
            return self._unavailable_section("count", "recent")
        try:
            versions = tuple(await self._report_versions())
        except Exception as error:
            return self._error_section(
                "加载报告统计", error, "count", "recent"
            )

        def _created(index: int) -> datetime:
            value = getattr(versions[index], "created_at", None)
            return value if value is not None else _MIN_INSTANT

        # Creation order comes oldest first (like report_versions()); the
        # dashboard shows newest first — a presentation-layer sort over
        # immutable saved versions, exactly like the case-home linkage.
        order = sorted(
            range(len(versions)),
            key=lambda index: (_created(index), index),
            reverse=True,
        )
        picked = order if limit is None else order[:limit]
        return {
            "state": _SECTION_OK,
            "error": None,
            "count": len(versions),
            "recent": [_report_activity_row(versions[i]) for i in picked],
        }

    @staticmethod
    def _error_section(
        operation: str, error: BaseException, *fields: str
    ) -> dict[str, Any]:
        """An explicit failure state: counts stay None, never a fake 0."""
        section: dict[str, Any] = {
            "state": _SECTION_ERROR,
            "error": safe_error_text(operation, error),
        }
        for name in fields:
            section[name] = [] if name == "recent" else None
        return section

    @staticmethod
    def _unavailable_section(*fields: str) -> dict[str, Any]:
        """A capability-missing state: every payload field is absent data."""
        section: dict[str, Any] = {"state": _SECTION_UNAVAILABLE, "error": None}
        for name in fields:
            if name == "recent":
                section[name] = []
            else:
                section[name] = None
        return section


_RECENT_MATERIAL_COLUMNS = [
    {"name": "material_id", "label": "材料", "field": "material_id",
     "align": "left", "sortable": True},
    {"name": "display_name", "label": "标题", "field": "display_name",
     "align": "left"},
    {"name": "ui_status", "label": "状态", "field": "ui_status",
     "align": "left"},
    {"name": "occurred_at", "label": "最近结果", "field": "occurred_at",
     "align": "left"},
]

_RECENT_REPORT_COLUMNS = [
    {"name": "version_id", "label": "版本", "field": "version_id",
     "align": "left", "sortable": True},
    {"name": "trigger", "label": "触发", "field": "trigger",
     "align": "left", "sortable": True},
    {"name": "created_at", "label": "创建时间", "field": "created_at",
     "align": "left", "sortable": True},
]


def _stat_text(name: str, section: dict[str, Any]) -> str:
    """One stat card's label text: the real numbers, or the honest state."""
    if section["state"] == _SECTION_OK:
        if "active" in section:
            return f"{name}总数 {section['total']},活跃 {section['active']}"
        if "failed" in section:
            return f"{name}总数 {section['total']},失败 {section['failed']}"
        return f"{name} {section['count']}"
    if section["state"] == _SECTION_UNAVAILABLE:
        return f"{name}统计不可用 — 未提供{name}读取"
    return f"{name}统计加载失败 — {section['error']}"


def _activity_markdown(
    section: dict[str, Any],
    *,
    loaded: str,
    empty: str,
    unavailable: str,
    failed: str,
) -> str:
    """One activity panel's state line; failure is never the empty state."""
    if section["state"] == _SECTION_ERROR:
        return f"**失败** — {failed}:{section['error']}"
    if section["state"] == _SECTION_UNAVAILABLE:
        return unavailable
    rows = section["recent"]
    if not rows:
        return empty
    return loaded.format(count=len(rows))


def build_dashboard_page(
    controller: DashboardController,
    ui: Any,
    *,
    route: str = "/",
    title: str = DEFAULT_DASHBOARD_TITLE,
) -> Any:
    """Register the unified dashboard ``route`` on the given ``ui`` module.

    The ``ui`` module is injected so the page construction stays a seam
    testable without NiceGUI installed; every read goes through the
    controller and each section renders its own honest state.
    """

    @ui.page(route)
    async def dashboard() -> None:
        message = ui.label("统合工作台就绪。")
        ui.label(title).classes("text-h5")

        with ui.card().classes("w-full"):
            ui.label("工作台入口").classes("text-bold")
            with ui.row():
                for card in NAV_CARDS:
                    with ui.card():
                        ui.label(card.title).classes("text-bold")
                        ui.label(card.description)
                        ui.link("进入", card.route)

        with ui.card().classes("w-full"):
            ui.label("概览统计").classes("text-bold")
            with ui.row():
                cases_label = ui.label("案例统计未加载")
                materials_label = ui.label("材料统计未加载")
                reports_label = ui.label("报告统计未加载")

        with ui.card().classes("w-full"):
            ui.label("最近材料运行").classes("text-bold")
            materials_table = ui.table(
                columns=_RECENT_MATERIAL_COLUMNS, rows=[]
            )
            materials_md = ui.markdown("_尚未加载材料运行_")

        with ui.card().classes("w-full"):
            ui.label("最近报告版本").classes("text-bold")
            reports_table = ui.table(
                columns=_RECENT_REPORT_COLUMNS, rows=[]
            )
            reports_md = ui.markdown("_尚未加载报告版本_")

        def _report(text: str) -> None:
            message.text = text
            message.update()

        async def _refresh(event: Any = None) -> None:
            try:
                view = await controller.load()
            except Exception as error:
                _report(safe_error_text("加载概览", error))
                return
            cases, materials, reports = (
                view["cases"], view["materials"], view["reports"]
            )
            cases_label.text = _stat_text("案例", cases)
            materials_label.text = _stat_text("材料", materials)
            reports_label.text = _stat_text("报告版本", reports)
            for element in (cases_label, materials_label, reports_label):
                element.update()
            materials_table.rows = [
                dict(row, occurred_at=display_or_unknown(
                    row["occurred_at"]
                ))
                for row in materials["recent"]
            ]
            materials_table.update()
            materials_md.content = _activity_markdown(
                materials,
                loaded="_{count} 条近期材料运行,最新在前_",
                empty="_最近没有材料运行_",
                unavailable="_材料读取不可用,无近期材料运行_",
                failed="无法加载材料运行",
            )
            materials_md.update()
            reports_table.rows = [
                dict(
                    row,
                    trigger=display_or_unknown(row["trigger"]),
                    created_at=display_or_unknown(row["created_at"]),
                )
                for row in reports["recent"]
            ]
            reports_table.update()
            reports_md.content = _activity_markdown(
                reports,
                loaded="_{count} 个报告版本,最新在前_",
                empty="_最近没有报告版本_",
                unavailable="_报告读取不可用,无近期报告版本_",
                failed="无法加载报告版本",
            )
            reports_md.update()
            failed = sum(
                1
                for section in (cases, materials, reports)
                if section["state"] == _SECTION_ERROR
            )
            _report(
                "概览已加载"
                if failed == 0
                else f"概览已加载,{failed} 个部分读取失败"
            )

        ui.button("刷新概览", on_click=_refresh)
        await _refresh()

    return dashboard


__all__ = [
    "DEFAULT_DASHBOARD_TITLE",
    "DashboardController",
    "DashboardFacade",
    "NAV_CARDS",
    "NavCard",
    "RECENT_LIMIT",
    "UNKNOWN_TEXT",
    "build_dashboard_page",
    "display_or_unknown",
]
