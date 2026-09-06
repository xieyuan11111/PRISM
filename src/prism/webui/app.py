"""Optional NiceGUI shell: the PRISM case home and historical timeline view.

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
from .status import safe_error_text, safe_identifier

DEFAULT_TITLE = "PRISM Case Home"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

NICEGUI_MISSING_MESSAGE = (
    "the PRISM WebUI requires the optional nicegui dependency; install it "
    'with: pip install ".[webui]"'
)
PLOTLY_MISSING_MESSAGE = (
    "timeline rendering requires the optional plotly dependency; install it "
    'with: pip install ".[webui]"'
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
    {"name": "case_id", "label": "Case", "field": "case_id",
     "align": "left", "sortable": True},
    {"name": "name", "label": "Name", "field": "name", "align": "left"},
    {"name": "case_type", "label": "Type", "field": "case_type",
     "align": "left", "sortable": True},
    {"name": "status", "label": "Status", "field": "status",
     "align": "left", "sortable": True},
    {"name": "material_count", "label": "Materials", "field": "material_count",
     "align": "right", "sortable": True},
    {"name": "latest_observed_at", "label": "Latest observed",
     "field": "latest_observed_at", "align": "left"},
    {"name": "unresolved", "label": "Unresolved", "field": "unresolved",
     "align": "left"},
]

_TIMELINE_SECTIONS = (
    ("Nodes", "nodes"),
    ("Effective facts", "facts"),
    ("Invalidated facts", "invalidated_facts"),
    ("Interpretations", "interpretations"),
    ("Relations", "relations"),
)

_EVIDENCE_BUCKETS = (
    "nodes",
    "facts",
    "invalidated_facts",
    "interpretations",
    "relations",
)


def _stage_line(entry: dict[str, Any]) -> str:
    window = entry["valid_at"] + (
        f" \u2192 {entry['invalid_at']}" if entry["invalid_at"] else ""
    )
    sources = ", ".join(entry["source_ids"]) or "_no sources_"
    marker = entry.get("node_type") or entry.get("stance") or entry["kind"]
    return (
        f"- `{entry['episode_key']}` [{marker}] {entry['summary']} "
        f"({window}; {sources})"
    )


def _state_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        f"**Case** {snapshot['case_id']} \u2014 type "
        f"{snapshot.get('case_type') or '?'}, status "
        f"{snapshot.get('status') or '?'}",
        f"**As of** {snapshot['cutoff_at']}",
    ]
    for label, key in _TIMELINE_SECTIONS + (("Evidence gaps", "evidence_gaps"),):
        lines.append(f"- {label}: {len(snapshot.get(key) or ())}")
    return "\n".join(lines)


def _timeline_markdown(snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    for heading, key in _TIMELINE_SECTIONS + (("Evidence gaps", "evidence_gaps"),):
        entries = snapshot.get(key) or ()
        lines.append(f"### {heading} ({len(entries)})")
        if not entries:
            lines.append("_none_")
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
                    where.append(f"paragraph {locator['paragraph']}")
                if locator.get("page") is not None:
                    where.append(f"page {locator['page']}")
                at = f" ({'; '.join(where)})" if where else ""
                quote = f': "{locator["quote"]}"' if locator.get("quote") else ""
                lines.append(
                    f"- `{entry['episode_key']}` \u2014 {locator['source_id']} "
                    f"\u2014 {locator['corpus_path']}{at}{quote}"
                )
    if not lines:
        lines.append("_no evidence locators in this snapshot_")
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
        status = "invalidated" if invalidated else "effective"
        layer = str(row.get("layer") or "unknown")
        kind = str(row.get("kind") or "unknown")
        legend_group = f"{layer}:{status}"
        sources = ", ".join(str(item) for item in row.get("source_ids") or ())
        hover = "<br>".join((
            f"Episode: {escape(str(row.get('episode_key') or ''))}",
            f"Kind/layer: {escape(kind)} / {escape(layer)}",
            f"Status: {status}",
            f"Summary: {escape(str(row.get('summary') or ''))}",
            f"Valid at: {escape(str(row.get('valid_at') or ''))}",
            f"Invalid at: {escape(str(row.get('invalid_at') or '-'))}",
            f"Source ids: {escape(sources or '-')}",
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
        title="Historical snapshot timeline",
        xaxis_title="Valid at",
        yaxis_title="Layer / kind",
        hovermode="closest",
        legend_title_text="Layer and status",
        margin={"l": 80, "r": 30, "t": 60, "b": 60},
    )
    return figure


def _detail_markdown(entry: dict[str, Any]) -> str:
    """Render full point metadata and portable evidence locators."""
    status = "INVALIDATED" if entry.get("invalidated") else "EFFECTIVE"
    sources = ", ".join(entry.get("source_ids") or ()) or "_none_"
    lines = [
        f"### `{entry['episode_key']}` — {status}",
        f"- Kind/layer: `{entry['kind']}` / `{entry['layer']}`",
        f"- Summary: {entry['summary']}",
        f"- Valid at: {entry['valid_at']}",
        f"- Invalid at: {entry.get('invalid_at') or '_not invalidated_'}",
        f"- Source ids: {sources}",
    ]
    core_fields = {
        "episode_key", "kind", "layer", "summary", "valid_at", "invalid_at",
        "source_ids", "evidence", "invalidated",
    }
    for name in sorted(set(entry) - core_fields):
        value = entry[name]
        if value is not None:
            lines.append(f"- {name}: {value}")
    lines.append("#### Evidence locators")
    evidence = entry.get("evidence") or ()
    if not evidence:
        lines.append("_no evidence locators on this entry_")
    for locator in evidence:
        where = []
        if locator.get("paragraph") is not None:
            where.append(f"paragraph {locator['paragraph']}")
        if locator.get("page") is not None:
            where.append(f"page {locator['page']}")
        location = f" ({'; '.join(where)})" if where else ""
        lines.append(
            f"- `{locator['source_id']}` — {locator['corpus_path']}{location}"
        )
        lines.append(f"  - Quote: {locator.get('quote') or '_none_'}")
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
    return {"": "all stages"} | {stage: stage for stage in sorted(STAGES)}


def _kind_options() -> dict[str, str]:
    return {"": "all kinds"} | {kind: kind for kind in sorted(ENTRY_KINDS)}


def _facade_supports(api: object, *operations: str) -> bool:
    """Whether the injected facade provides every named operation."""
    return all(callable(getattr(api, name, None)) for name in operations)


def _default_upload_staging_root() -> Path:
    """The controlled staging default: ``<PRISM_HOME>/staging/uploads``."""
    from prism.config import PathConfig

    return PathConfig.prism_home() / "staging" / "uploads"


def build_case_home_page(
    controller: CaseHomeController, ui: Any, *, title: str = DEFAULT_TITLE
) -> Any:
    """Register the ``/`` case-home page on the given ``ui`` module.

    The ``ui`` module is injected so the page construction — the controls,
    panels and their handlers — is a seam testable without NiceGUI installed;
    every handler delegates to the controller and reports explicit errors in
    the message label instead of swallowing them.
    """
    @ui.page("/")
    def case_home() -> None:
        timeline_plot: Any | None = None
        message = ui.label("Load cases to begin.")

        with ui.card().classes("w-full"):
            ui.label("Case filters").classes("text-bold")
            with ui.row():
                search = ui.input(label="Search", placeholder="case id or name")
                type_input = ui.input(label="Type (exact)", placeholder="policy")
                status_input = ui.input(label="Status (exact)", placeholder="active")
                unresolved = ui.switch("Unresolved only", value=False)
            with ui.row():
                as_of_input = ui.input(
                    label="as of (ISO 8601, timezone-aware)",
                    placeholder="2026-02-02T00:00:00+00:00",
                )
                stage_select = ui.select(
                    options=_stage_options(), value="", label="Stage"
                )
                kind_select = ui.select(
                    options=_kind_options(), value="", label="Kind"
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
                _report(safe_error_text("load cases", error))
                return
            cases_table.rows = view["cases"]
            cases_table.update()
            _report(f"{view['count']} case(s) loaded")

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
                _report(safe_error_text("select case", error))
                return
            _report(
                f"selected {view['case_id']} \u2014 {view['name']} "
                f"({view['status']})"
            )

        async def _load_snapshot(event: Any = None) -> None:
            nonlocal timeline_plot
            if controller.selected_case_id is None:
                _report("select a case in the table first")
                return
            try:
                view = await controller.load_snapshot(
                    controller.selected_case_id,
                    as_of_input.value or "",
                    stage=stage_select.value or None,
                    kinds=(kind_select.value,) if kind_select.value else None,
                )
            except Exception as error:
                _report(safe_error_text("load snapshot", error))
                return
            snapshot = view["snapshot"]
            try:
                figure = build_timeline_figure(view["timeline"])
            except Exception as error:
                _report(safe_error_text("render timeline", error))
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
                f"snapshot at {snapshot['cutoff_at']}: "
                f"{len(snapshot['nodes'])} node(s), "
                f"{len(snapshot['facts'])} effective fact(s), "
                f"{len(snapshot['invalidated_facts'])} invalidated fact(s)"
            )

        async def _on_timeline_click(event: Any) -> None:
            episode_key = ""
            try:
                episode_key = _clicked_episode_key(event)
                detail = controller.select_timeline_point(episode_key)
            except Exception as error:
                safe_key = safe_identifier(episode_key)
                _report(
                    f"unknown timeline point: {safe_key}"
                    if safe_key is not None
                    else safe_error_text("select timeline point", error)
                )
                return
            timeline_detail_md.content = _detail_markdown(detail)
            timeline_detail_md.update()
            _report(f"selected timeline point {episode_key}")

        with ui.card().classes("w-full"):
            cases_table = ui.table(
                columns=_CASE_COLUMNS,
                rows=[],
                selection="single",
                on_select=_on_row_selected,
            )
            with ui.row():
                ui.button("Refresh cases", on_click=_refresh_cases)
                ui.button("Load snapshot", on_click=_load_snapshot)

        with ui.card().classes("w-full"):
            with ui.expansion("Case state"):
                state_md = ui.markdown("_no snapshot loaded_")
            with ui.expansion("Timeline"):
                timeline_plot_container = ui.column().classes("w-full")
                timeline_md = ui.markdown("_no snapshot loaded_")
                timeline_detail_md = ui.markdown("_Select a timeline point for details._")
            with ui.expansion("Evidence"):
                evidence_md = ui.markdown("_no snapshot loaded_")

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
    build_case_home_page(controller, ui, title=title)
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
    from nicegui import app as nicegui_app

    return nicegui_app


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TITLE",
    "NICEGUI_MISSING_MESSAGE",
    "PLOTLY_MISSING_MESSAGE",
    "CaseHomeController",
    "WebUIUnavailableError",
    "build_case_home_page",
    "build_timeline_figure",
    "create_app",
]
