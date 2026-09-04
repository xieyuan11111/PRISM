"""Focused Plotly timeline and point-detail contract for the M3 WebUI."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import test_webui_case_home as case_home


def run(coro):
    return asyncio.run(coro)


def _loaded_controller():
    controller = case_home._controller(
        [case_home._overview(case_home.CASE)], snapshot=case_home._snapshot_state()
    )
    view = run(controller.load_snapshot(case_home.CASE, case_home.T_SNAP))
    return controller, case_home.controller_facade(controller), view


def test_controller_timeline_rows_are_json_safe_complete_and_deterministic():
    controller, facade, view = _loaded_controller()

    rows = controller.timeline_rows()

    assert rows == view["timeline"]
    assert [row["episode_key"] for row in rows] == [
        "claim-yes", "fact-1", "fact-2", "node-pub", "rel-1"
    ]
    assert all(
        set(row) >= {
            "episode_key", "kind", "layer", "summary", "valid_at",
            "invalid_at", "source_ids", "evidence", "invalidated",
        }
        for row in rows
    )
    invalidated = next(row for row in rows if row["episode_key"] == "fact-1")
    assert invalidated["invalidated"] is True
    effective = next(row for row in rows if row["episode_key"] == "fact-2")
    assert effective["invalidated"] is False
    node = next(row for row in rows if row["episode_key"] == "node-pub")
    assert node["source_ids"] == ["material-a"]
    assert node["evidence"] == [{
        "source_id": "material-a",
        "corpus_path": case_home.EVIDENCE_PATH,
        "paragraph": 1,
        "page": 3,
        "quote": "The policy was published.",
    }]
    assert facade.snapshot_calls == [(case_home.CASE, case_home.T_SNAP, None, None)]


def test_controller_selects_detail_from_loaded_snapshot_without_another_api_call():
    controller, facade, _ = _loaded_controller()
    calls = (list(facade.case_calls), list(facade.snapshot_calls))

    detail = controller.select_timeline_point("node-pub")

    assert detail["episode_key"] == "node-pub"
    assert detail["summary"] == "Policy published."
    assert detail["evidence"][0]["corpus_path"] == case_home.EVIDENCE_PATH
    assert detail["evidence"][0]["paragraph"] == 1
    assert detail["evidence"][0]["page"] == 3
    assert detail["evidence"][0]["quote"] == "The policy was published."
    assert (facade.case_calls, facade.snapshot_calls) == calls


def test_controller_rejects_unknown_point_without_any_unrelated_api_call():
    controller, facade, _ = _loaded_controller()
    calls = (list(facade.case_calls), list(facade.snapshot_calls))

    with pytest.raises(LookupError, match="unknown-point"):
        controller.select_timeline_point("unknown-point")
    with pytest.raises(ValueError, match="episode_key"):
        controller.select_timeline_point("  ")

    assert (facade.case_calls, facade.snapshot_calls) == calls


class _Scatter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Figure:
    def __init__(self):
        self.data = []
        self.layout = {}

    def add_trace(self, trace):
        self.data.append(trace)

    def update_layout(self, **kwargs):
        self.layout.update(kwargs)


def test_plotly_figure_has_one_deterministic_clickable_point_per_row(monkeypatch):
    from prism.webui import app

    _, _, view = _loaded_controller()
    monkeypatch.setattr(
        app, "_plotly_graph_objects",
        lambda: SimpleNamespace(Figure=_Figure, Scatter=_Scatter),
    )

    figure = app.build_timeline_figure(list(reversed(view["timeline"])))

    assert isinstance(figure, _Figure)
    assert len(figure.data) == len(view["timeline"])
    keys = [trace.kwargs["customdata"][0] for trace in figure.data]
    assert keys == ["claim-yes", "fact-1", "fact-2", "node-pub", "rel-1"]
    assert all(len(trace.kwargs["x"]) == 1 for trace in figure.data)
    assert all(len(trace.kwargs["y"]) == 1 for trace in figure.data)
    for trace in figure.data:
        hover = trace.kwargs["text"][0]
        key = trace.kwargs["customdata"][0]
        row = next(row for row in view["timeline"] if row["episode_key"] == key)
        assert key in hover
        assert row["summary"] in hover
        assert row["valid_at"] in hover
        assert "material-a" in hover
        assert row["kind"] in hover
    old = next(t for t in figure.data if t.kwargs["customdata"] == ["fact-1"])
    current = next(t for t in figure.data if t.kwargs["customdata"] == ["fact-2"])
    assert old.kwargs["marker"]["symbol"] != current.kwargs["marker"]["symbol"]
    assert "invalidated" in old.kwargs["text"][0].lower()
    assert "effective" in current.kwargs["text"][0].lower()


def test_plotly_missing_is_clear_only_when_figure_rendering_is_requested(monkeypatch):
    from prism.webui import CaseHomeController, WebUIUnavailableError
    from prism.webui import app

    # Controller construction and snapshot shaping have no Plotly dependency.
    controller = CaseHomeController(
        case_home.FakeFacade([case_home._overview(case_home.CASE)])
    )
    assert controller.timeline_rows() == []

    def missing():
        raise WebUIUnavailableError("plotly optional dependency is missing")

    monkeypatch.setattr(app, "_plotly_graph_objects", missing)
    with pytest.raises(WebUIUnavailableError, match="plotly"):
        app.build_timeline_figure([])


def test_app_factory_reports_missing_plotly_but_import_and_controller_do_not(
    monkeypatch,
):
    from prism.webui import WebUIUnavailableError
    from prism.webui import app

    monkeypatch.setattr(app, "_nicegui", lambda: SimpleNamespace())

    def missing():
        raise WebUIUnavailableError("plotly is required for timeline rendering")

    monkeypatch.setattr(app, "_plotly_graph_objects", missing)
    with pytest.raises(WebUIUnavailableError, match="plotly"):
        app.create_app(case_home.FakeFacade([case_home._overview(case_home.CASE)]))


def test_page_click_renders_same_snapshot_detail_and_unknown_id_is_explicit(
    monkeypatch,
):
    from prism.webui import app

    controller = case_home._controller(
        [case_home._overview(case_home.CASE)], snapshot=case_home._snapshot_state()
    )
    facade = case_home.controller_facade(controller)
    monkeypatch.setattr(app, "build_timeline_figure", lambda rows: {"rows": rows})
    ui = case_home._build_page(controller)
    run(case_home._element(ui, "table").kwargs["on_select"](
        SimpleNamespace(args=[{"case_id": case_home.CASE}])
    ))
    case_home._element(ui, "input", label="as of").value = (
        "2026-02-02T00:00:00+00:00"
    )
    run(case_home._element(ui, "button", text="Load snapshot").kwargs["on_click"]())

    plot = case_home._element(ui, "plotly")
    calls = (list(facade.case_calls), list(facade.snapshot_calls))
    run(plot.events["plotly_click"](SimpleNamespace(
        args={"points": [{"customdata": "node-pub"}]}
    )))
    detail = case_home._element(ui, "markdown", text="Select a timeline point")
    assert "node-pub" in detail.content
    assert "Policy published." in detail.content
    assert case_home.EVIDENCE_PATH in detail.content
    assert "paragraph 1" in detail.content
    assert "page 3" in detail.content
    assert "The policy was published." in detail.content

    run(plot.events["plotly_click"](SimpleNamespace(
        args={"points": [{"customdata": "unknown-point"}]}
    )))
    assert any("unknown-point" in label.text for label in case_home._labels(ui))
    assert (facade.case_calls, facade.snapshot_calls) == calls
