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

from typing import Any

from prism.analyzer import ENTRY_KINDS, STAGES

from .controller import CaseHomeController, PrismFacade

DEFAULT_TITLE = "PRISM Case Home"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

NICEGUI_MISSING_MESSAGE = (
    "the PRISM WebUI requires the optional nicegui dependency; install it "
    'with: pip install ".[webui]"'
)


class WebUIUnavailableError(RuntimeError):
    """The optional NiceGUI dependency is not installed."""


def _nicegui() -> Any:
    """Import the NiceGUI ``ui`` module lazily, or fail with a clear error."""
    try:
        from nicegui import ui
    except ImportError as error:
        raise WebUIUnavailableError(NICEGUI_MISSING_MESSAGE) from error
    return ui


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


def _stage_options() -> dict[str, str]:
    return {"": "all stages"} | {stage: stage for stage in sorted(STAGES)}


def _kind_options() -> dict[str, str]:
    return {"": "all kinds"} | {kind: kind for kind in sorted(ENTRY_KINDS)}


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
        message = ui.label("Load cases to begin.")

        with ui.card().classes("w-full"):
            ui.label("Case filters").classes("text-bold")
            with ui.row():
                search = ui.input(label="Search", placeholder="case id or name")
                type_input = ui.input(label="Type (exact)", placeholder="policy")
                status_input = ui.input(label="Status (exact)", placeholder="active")
                unresolved = ui.switch(text="Unresolved only", value=False)
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
                _report(f"error loading cases: {error}")
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
                _report(f"error selecting case: {error}")
                return
            _report(
                f"selected {view['case_id']} \u2014 {view['name']} "
                f"({view['status']})"
            )

        async def _load_snapshot(event: Any = None) -> None:
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
                _report(f"error loading snapshot: {error}")
                return
            snapshot = view["snapshot"]
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
                timeline_md = ui.markdown("_no snapshot loaded_")
            with ui.expansion("Evidence"):
                evidence_md = ui.markdown("_no snapshot loaded_")

    return case_home


def create_app(api: PrismFacade, *, title: str = DEFAULT_TITLE) -> Any:
    """Build the case-home NiceGUI pages over ``api`` without serving them.

    Raises :class:`WebUIUnavailableError` with install instructions when the
    optional NiceGUI dependency is missing; NiceGUI is never imported at
    module scope.
    """
    try:
        ui = _nicegui()
    except ImportError as error:
        raise WebUIUnavailableError(NICEGUI_MISSING_MESSAGE) from error
    controller = CaseHomeController(api)
    build_case_home_page(controller, ui, title=title)
    from nicegui import app as nicegui_app

    return nicegui_app


def run(
    api: PrismFacade, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> None:
    """Serve the case home on loopback without opening a browser."""
    if host != DEFAULT_HOST:
        raise ValueError("PRISM WebUI only binds loopback by default")
    create_app(api)
    ui = _nicegui()
    ui.run(host=host, port=port, show=False, reload=False)


def main() -> None:
    """Start the optional WebUI with the normal PRISM runtime."""
    import asyncio
    from prism.runtime import create_runtime

    runtime = asyncio.run(create_runtime())
    try:
        run(runtime.api)
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            close_result = close()
            if hasattr(close_result, "__await__"):
                asyncio.run(close_result)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TITLE",
    "NICEGUI_MISSING_MESSAGE",
    "CaseHomeController",
    "WebUIUnavailableError",
    "build_case_home_page",
    "create_app",
    "main",
    "run",
]
