"""M3 WebUI slice: the NiceGUI case home over the existing PrismAPI facade.

This module covers the dependency-free controller/view-model seam
(:class:`prism.webui.CaseHomeController`) — case-overview loading with
search/type/status/unresolved filters, explicit case selection, timezone-aware
``as_of`` validation and delegation to ``PrismAPI.query_historical_snapshot``
— plus the JSON-safe view contract, the lazy-NiceGUI app factory seam, the
``python -m prism.webui`` entry defaults (loopback only, no auto-open
browser), and the page-builder seam exercised through a recording ``ui``
stand-in.

Everything is offline: a synthetic facade over real ``CaseOverview`` /
``HistoricalCaseState`` objects, no NiceGUI import, no runtime, no network,
no LLM.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import inspect
import json
import sys
from types import SimpleNamespace

import pytest

from prism.analyzer import ENTRY_KINDS, STAGES
from prism.analyzer import EvidenceGap
from prism.analyzer import HistoricalCaseState as State
from prism.analyzer import TimelineStage
from prism.cases.overview import CaseOverview
from prism.domain import EvidenceLocator

UTC = timezone.utc
OBSERVED = datetime(2026, 2, 1, tzinfo=UTC)
T_SNAP = datetime(2026, 2, 2, tzinfo=UTC)
CASE = "case-rates"
OTHER = "case-housing"
EVIDENCE_PATH = "corpus/2026-01/example.gov/material-a.md"


def run(coro):
    return asyncio.run(coro)


def _overview(case_id, *, case_type="policy", status="active", name=None,
              gaps=False, conflicts=False, materials=2):
    return CaseOverview(
        case_id=case_id,
        case_type=case_type,
        name=name or case_id.replace("-", " ").title(),
        status=status,
        material_count=materials,
        earliest_observed_at=OBSERVED,
        latest_observed_at=OBSERVED,
        latest_node_at=OBSERVED,
        last_updated_at=OBSERVED + timedelta(hours=1),
        has_unresolved_gaps=gaps,
        has_unresolved_conflicts=conflicts,
    )


def _stage(episode_key, kind, *, summary, stance=None, node_type=None,
           valid_at=OBSERVED, invalid_at=None, source_ids=("material-a",),
           evidence=()):
    layer = {
        "evolution_node": "fact",
        "temporal_fact": "fact",
        "temporal_relation": "fact",
        "claim": "interpretation",
    }[kind]
    return TimelineStage(
        episode_key=episode_key,
        kind=kind,
        layer=layer,
        summary=summary,
        valid_at=valid_at,
        invalid_at=invalid_at,
        reference_time=valid_at,
        source_ids=tuple(source_ids),
        node_type=node_type,
        stance=stance,
        evidence=tuple(evidence),
    )


def _snapshot_state():
    locator = EvidenceLocator(
        "material-a", EVIDENCE_PATH, paragraph=1, page=3,
        quote="The policy was published."
    )
    return State(
        case_id=CASE,
        cutoff_at=T_SNAP,
        case_type="policy",
        status="active",
        nodes=(
            _stage("node-pub", "evolution_node", summary="Policy published.",
                   node_type="publication", evidence=(locator,)),
        ),
        facts=(
            _stage("fact-2", "temporal_fact",
                   summary="Agency set Rate=2%", valid_at=OBSERVED),
        ),
        interpretations=(
            _stage("claim-yes", "claim", summary="The rate change helps.",
                   stance="support"),
        ),
        evidence_gaps=(
            EvidenceGap(
                "missing_primary_source",
                "no primary source text",
                episode_key="node-pub",
                source_ids=("material-a",),
            ),
        ),
        invalidated_facts=(
            _stage("fact-1", "temporal_fact",
                   summary="Agency set Rate=3%", valid_at=OBSERVED,
                   invalid_at=T_SNAP - timedelta(days=1)),
        ),
        relations=(
            _stage("rel-1", "temporal_relation",
                   summary="fact-2 supersedes fact-1"),
        ),
    )


class FakeFacade:
    """Synthetic PrismAPI stand-in recording every call it receives."""

    def __init__(self, overviews=(), snapshot=None):
        self._overviews = {item.case_id: item for item in overviews}
        self._snapshot = snapshot
        self.overview_calls = []
        self.case_calls = []
        self.snapshot_calls = []

    async def case_overviews(self, *, case_type=None, status=None,
                             unresolved_only=False, order="case_id",
                             reverse=False):
        self.overview_calls.append(
            dict(case_type=case_type, status=status,
                 unresolved_only=unresolved_only)
        )
        items = [
            item for item in self._overviews.values()
            if (case_type is None or item.case_type == case_type)
            and (status is None or item.status == status)
            and (not unresolved_only
                 or item.has_unresolved_gaps or item.has_unresolved_conflicts)
        ]
        return tuple(sorted(items, key=lambda item: item.case_id, reverse=reverse))

    async def case_overview(self, case_id):
        self.case_calls.append(case_id)
        try:
            return self._overviews[case_id]
        except KeyError:
            raise LookupError(f"no accumulated evolution case {case_id!r}") from None

    async def query_historical_snapshot(self, case_id, as_of, *, stage=None,
                                        kinds=None):
        self.snapshot_calls.append((case_id, as_of, stage, kinds))
        if self._snapshot is None:
            return State(case_id=case_id, cutoff_at=as_of, case_type=None,
                         status=None, nodes=(), facts=(), interpretations=(),
                         evidence_gaps=())
        return self._snapshot


def controller_facade(controller):
    return controller._api


def _controller(overviews=(), snapshot=None):
    from prism.webui import CaseHomeController

    return CaseHomeController(FakeFacade(overviews, snapshot))


# ------------------------------------------------------------- controller seam


def test_controller_rejects_a_facade_without_the_case_home_operations():
    from prism.webui import CaseHomeController

    class Empty:
        pass

    with pytest.raises(TypeError, match="case_overviews"):
        CaseHomeController(Empty())
    with pytest.raises(TypeError, match="query_historical_snapshot"):
        CaseHomeController(
            SimpleNamespace(
                case_overviews=lambda **kw: None,
                case_overview=lambda case_id: None,
            )
        )


def test_load_cases_forwards_type_status_and_unresolved_filters():
    controller = _controller([_overview(CASE, gaps=True)])

    view = run(controller.load_cases(
        case_type="policy", status="active", unresolved_only=True
    ))

    assert controller_facade(controller).overview_calls == [
        {"case_type": "policy", "status": "active", "unresolved_only": True}
    ]
    assert view["count"] == 1
    assert view["cases"][0]["case_id"] == CASE


def test_load_cases_without_filters_passes_neutral_arguments():
    controller = _controller([_overview(CASE)])

    run(controller.load_cases())

    assert controller_facade(controller).overview_calls == [
        {"case_type": None, "status": None, "unresolved_only": False}
    ]


def test_load_cases_search_filters_client_side_over_id_and_name():
    controller = _controller([
        _overview(CASE, name="Rate Policy 2026"),
        _overview(OTHER, case_type="public_issue", name="Housing Fund 2026"),
    ])

    by_name = run(controller.load_cases(search="housing"))
    assert [item["case_id"] for item in by_name["cases"]] == [OTHER]
    by_id = run(controller.load_cases(search="RATES"))
    assert [item["case_id"] for item in by_id["cases"]] == [CASE]
    none = run(controller.load_cases(search="quantum"))
    assert none["cases"] == []
    # The text search is never pushed to the facade as a case filter.
    calls = controller_facade(controller).overview_calls
    assert all(
        call == {"case_type": None, "status": None, "unresolved_only": False}
        for call in calls
    )


def test_case_view_is_json_safe_and_retains_overview_fields():
    controller = _controller(
        [_overview(CASE, name="Rate Policy 2026", gaps=True, materials=3)]
    )

    view = run(controller.load_cases(search="rates"))
    (case,) = view["cases"]

    assert case["case_id"] == CASE
    assert case["case_type"] == "policy"
    assert case["name"] == "Rate Policy 2026"
    assert case["status"] == "active"
    assert case["material_count"] == 3
    assert case["latest_observed_at"] == OBSERVED.isoformat()
    assert case["last_updated_at"] == (OBSERVED + timedelta(hours=1)).isoformat()
    assert case["has_unresolved_gaps"] is True
    assert case["unresolved"] is True
    assert json.loads(json.dumps(view)) == view


def test_select_case_returns_the_validated_view_and_remembers_it():
    controller = _controller([_overview(CASE)])

    view = run(controller.select_case(CASE))

    assert view["case_id"] == CASE
    assert controller.selected_case_id == CASE


def test_select_case_raises_explicitly_for_an_unknown_case():
    controller = _controller([_overview(CASE)])

    with pytest.raises(LookupError, match="case-unknown"):
        run(controller.select_case("case-unknown"))
    assert controller.selected_case_id is None


def test_load_snapshot_parses_iso_as_of_and_delegates_exactly_once():
    controller = _controller(
        [_overview(CASE)], snapshot=_snapshot_state()
    )

    view = run(controller.load_snapshot(
        CASE, "2026-02-02T00:00:00+00:00",
        stage="publication", kinds=("evolution_node",),
    ))

    facade = controller_facade(controller)
    assert facade.snapshot_calls == [
        (CASE, T_SNAP, "publication", ("evolution_node",))
    ]
    assert view["case"]["case_id"] == CASE
    assert view["snapshot"]["case_id"] == CASE
    assert view["snapshot"]["cutoff_at"] == T_SNAP.isoformat()


def test_load_snapshot_accepts_an_aware_datetime_directly():
    controller = _controller([_overview(CASE)])

    run(controller.load_snapshot(CASE, T_SNAP))

    assert controller_facade(controller).snapshot_calls == [
        (CASE, T_SNAP, None, None)
    ]


def test_snapshot_view_keeps_every_layer_and_locator_json_safe():
    controller = _controller([_overview(CASE)], snapshot=_snapshot_state())

    view = run(controller.load_snapshot(CASE, T_SNAP))
    snapshot = view["snapshot"]

    assert snapshot["case_type"] == "policy"
    assert snapshot["status"] == "active"
    (node,) = snapshot["nodes"]
    assert node["episode_key"] == "node-pub"
    assert node["node_type"] == "publication"
    assert node["source_ids"] == ["material-a"]
    (locator,) = node["evidence"]
    assert locator == {
        "source_id": "material-a",
        "corpus_path": EVIDENCE_PATH,
        "paragraph": 1,
        "page": 3,
        "quote": "The policy was published.",
    }
    assert [item["episode_key"] for item in snapshot["facts"]] == ["fact-2"]
    assert [item["episode_key"] for item in snapshot["invalidated_facts"]] == ["fact-1"]
    assert snapshot["invalidated_facts"][0]["invalid_at"] is not None
    assert snapshot["interpretations"][0]["stance"] == "support"
    assert snapshot["relations"][0]["episode_key"] == "rel-1"
    (gap,) = snapshot["evidence_gaps"]
    assert gap["gap_type"] == "missing_primary_source"
    assert gap["source_ids"] == ["material-a"]
    assert json.loads(json.dumps(view)) == view


def test_load_snapshot_rejects_naive_as_of_before_any_facade_call():
    controller = _controller([_overview(CASE)])

    with pytest.raises(ValueError, match="timezone-aware"):
        run(controller.load_snapshot(CASE, datetime(2026, 2, 2)))
    with pytest.raises(ValueError, match="timezone-aware"):
        run(controller.load_snapshot(CASE, "2026-02-02T00:00:00"))
    facade = controller_facade(controller)
    assert facade.case_calls == []
    assert facade.snapshot_calls == []


def test_load_snapshot_rejects_an_invalid_stage_before_any_facade_call():
    controller = _controller([_overview(CASE)])

    with pytest.raises(ValueError, match="stage"):
        run(controller.load_snapshot(CASE, T_SNAP, stage="rumor"))
    facade = controller_facade(controller)
    assert facade.case_calls == []
    assert facade.snapshot_calls == []


def test_load_snapshot_rejects_an_unknown_kind_before_any_facade_call():
    controller = _controller([_overview(CASE)])

    with pytest.raises(ValueError, match="kind"):
        run(controller.load_snapshot(CASE, T_SNAP, kinds=("rumour",)))
    facade = controller_facade(controller)
    assert facade.case_calls == []
    assert facade.snapshot_calls == []


def test_load_snapshot_requires_a_case_id():
    controller = _controller([_overview(CASE)])

    with pytest.raises(ValueError, match="case_id"):
        run(controller.load_snapshot("  ", T_SNAP))
    assert controller_facade(controller).snapshot_calls == []


def test_load_snapshot_unknown_case_is_explicit_and_never_snapshots():
    controller = _controller([_overview(CASE)])

    with pytest.raises(LookupError, match="case-unknown"):
        run(controller.load_snapshot("case-unknown", T_SNAP))
    facade = controller_facade(controller)
    assert facade.case_calls == ["case-unknown"]
    assert facade.snapshot_calls == []


# -------------------------------------------------------------- parse_as_of


def test_parse_as_of_accepts_iso_strings_and_aware_datetimes_only():
    from prism.webui import parse_as_of

    assert parse_as_of("2026-02-02T00:00:00+00:00") == T_SNAP
    assert parse_as_of(T_SNAP) is T_SNAP
    assert parse_as_of(" 2026-02-02T05:30:00+05:30 ").utcoffset() == timedelta(
        hours=5, minutes=30
    )
    with pytest.raises(ValueError, match="as_of"):
        parse_as_of("")
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_as_of("yesterday")
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_as_of("2026-02-02")
    with pytest.raises(TypeError):
        parse_as_of(20260202)


# ------------------------------------------------- package import boundaries


def test_importing_the_webui_package_never_imports_nicegui():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "nicegui" or name.startswith("nicegui.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        import prism.webui
        import prism.webui.app
        import prism.webui.controller
        import prism.webui.server

        assert callable(prism.webui.create_app)
        assert callable(prism.webui.CaseHomeController)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)


def test_module_entry_is_present_and_import_safe():
    spec = importlib.util.find_spec("prism.webui.__main__")
    assert spec is not None
    module = importlib.import_module("prism.webui.__main__")
    assert callable(module.main)


@pytest.mark.skipif(
    importlib.util.find_spec("nicegui") is not None,
    reason="NiceGUI is installed; the missing-dependency error cannot occur",
)
def test_create_app_raises_a_clear_install_error_without_nicegui():
    from prism.webui import WebUIUnavailableError, create_app

    with pytest.raises(WebUIUnavailableError, match="nicegui"):
        create_app(FakeFacade([_overview(CASE)]))


@pytest.mark.skipif(
    importlib.util.find_spec("nicegui") is not None,
    reason="NiceGUI is installed; the missing-dependency error cannot occur",
)
def test_server_main_reports_missing_nicegui_without_starting_anything(
    capsys,
):
    from prism.webui.server import main

    assert main([]) == 1
    captured = capsys.readouterr()
    assert "nicegui" in captured.err.lower()
    assert "webui" in captured.err.lower()


# ----------------------------------------------------------- server defaults


def test_server_defaults_bind_the_loopback_and_never_open_a_browser():
    from prism.webui import DEFAULT_HOST, DEFAULT_TITLE
    from prism.webui.server import build_arg_parser, run

    assert DEFAULT_HOST == "127.0.0.1"
    parameters = inspect.signature(run).parameters
    assert parameters["host"].default == "127.0.0.1"
    assert parameters["show"].default is False
    args = build_arg_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.title == DEFAULT_TITLE


def test_run_refuses_non_loopback_hosts_before_importing_nicegui():
    from prism.webui.server import run

    with pytest.raises(ValueError, match="loopback"):
        run(None, host="0.0.0.0")


def test_lazy_api_delegates_to_the_started_runtime_only():
    from prism.webui.server import _LazyAPI

    holder = {}
    proxy = _LazyAPI(holder)
    with pytest.raises(RuntimeError, match="not started"):
        run(proxy.case_overviews())

    holder["runtime"] = SimpleNamespace(api=FakeFacade([_overview(CASE)]))
    view = run(proxy.case_overviews())
    assert [item.case_id for item in view] == [CASE]


# ---------------------------------------------------------- fake-ui page seam


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
        self.figure = args[0] if name == "plotly" and args else None
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

    def on(self, event, handler):
        self.events = getattr(self, "events", {})
        self.events[event] = handler
        return self

    def update_figure(self, figure):
        self.figure = figure
        self.update()


class _FakeUI:
    """Recording stand-in for the NiceGUI ``ui`` module surface."""

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
    raise AssertionError(f"no {name} element matching label={label!r} text={text!r}")


def _labels(ui):
    return [element for element in ui.elements if element.name == "label"]


def _build_page(controller):
    from prism.webui.app import build_case_home_page

    ui = _FakeUI()
    build_case_home_page(controller, ui)
    page = ui.pages["/"]
    page()
    return ui


def test_page_seam_lists_controls_and_stage_kind_vocabularies():
    ui = _build_page(_controller([_overview(CASE)]))

    for label in ("Search", "Type", "Status", "as of"):
        assert _element(ui, "input", label=label) is not None
    assert _element(ui, "switch", text="Unresolved") is not None
    stage_options = _element(ui, "select", label="Stage").kwargs["options"]
    assert set(stage_options) == {""} | set(STAGES)
    kind_options = _element(ui, "select", label="Kind").kwargs["options"]
    assert set(kind_options) == {""} | set(ENTRY_KINDS)
    _element(ui, "button", text="Refresh cases")
    _element(ui, "button", text="Load snapshot")
    table = _element(ui, "table")
    assert table.rows == []
    assert {column["field"] for column in table.kwargs["columns"]} >= {
        "case_id", "case_type", "status", "material_count"
    }
    for title in ("Case state", "Timeline", "Evidence"):
        _element(ui, "expansion", text=title)


def test_page_seam_refresh_select_and_load_snapshot_through_the_controller(
    monkeypatch,
):
    from prism.webui import app

    monkeypatch.setattr(app, "build_timeline_figure", lambda rows: {"rows": rows})
    controller = _controller(
        [
            _overview(CASE, name="Rate Policy 2026"),
            _overview(OTHER, name="Housing Fund 2026"),
        ],
        snapshot=_snapshot_state(),
    )
    facade = controller_facade(controller)
    ui = _build_page(controller)

    # Refresh: the table is filled from the controller's JSON-safe view (the
    # facade returns its stable case_id order).
    run(_element(ui, "button", text="Refresh cases").kwargs["on_click"](None))
    table = _element(ui, "table")
    assert [row["case_id"] for row in table.rows] == [OTHER, CASE]

    # Search narrows the rows client-side.
    search = _element(ui, "input", label="Search")
    search.value = "housing"
    run(_element(ui, "button", text="Refresh cases").kwargs["on_click"](None))
    assert [row["case_id"] for row in table.rows] == [OTHER]
    search.value = ""

    # Selecting a table row delegates to select_case.
    run(_element(ui, "table").kwargs["on_select"](
        SimpleNamespace(args=[{"case_id": CASE}])
    ))
    assert controller.selected_case_id == CASE
    assert any("selected" in label.text for label in _labels(ui))

    # Loading a snapshot fills the state/timeline/evidence panels.
    _element(ui, "input", label="as of").value = "2026-02-02T00:00:00+00:00"
    _element(ui, "select", label="Stage").value = "publication"
    run(_element(ui, "button", text="Load snapshot").kwargs["on_click"](None))

    assert facade.snapshot_calls == [(CASE, T_SNAP, "publication", None)]
    markdowns = [element for element in ui.elements if element.name == "markdown"]
    state_md, timeline_md, _timeline_detail_md, evidence_md = markdowns
    assert CASE in state_md.content
    assert "node-pub" in timeline_md.content
    assert "Policy published." in timeline_md.content
    assert EVIDENCE_PATH in evidence_md.content
    assert "material-a" in evidence_md.content


def test_page_seam_reports_explicit_errors_and_calls_nothing_wrong():
    controller = _controller([_overview(CASE)])
    facade = controller_facade(controller)
    ui = _build_page(controller)

    # No case selected yet: an explicit message, no facade call.
    run(_element(ui, "button", text="Load snapshot").kwargs["on_click"](None))
    assert facade.snapshot_calls == []
    assert any("select a case" in label.text for label in _labels(ui))

    # A naive as_of surfaces the controller's explicit error.
    run(_element(ui, "table").kwargs["on_select"](
        SimpleNamespace(args=[{"case_id": CASE}])
    ))
    _element(ui, "input", label="as of").value = "2026-02-02T00:00:00"
    run(_element(ui, "button", text="Load snapshot").kwargs["on_click"](None))
    assert facade.snapshot_calls == []
    assert any("load snapshot failed (ValueError)" == label.text for label in _labels(ui))
