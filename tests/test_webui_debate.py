from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from prism.webui.debate import DebateTheaterController, build_debate_theater_page


def run(coro):
    return asyncio.run(coro)


class FakeFacade:
    def __init__(self):
        self.debate_calls = []
        self.follow_calls = []

    async def debate_case(self, case_id, question, as_of, perspectives):
        self.debate_calls.append((case_id, question, as_of, tuple(perspectives)))
        return {"case_id": case_id, "question": question, "as_of": as_of, "status": "no_conclusion", "results": [], "synthesis": None, "errors": [], "warnings": [], "fallback_reason": "provider failure"}

    async def follow_up_debate(self, parent_run_id, question, perspective):
        self.follow_calls.append((parent_run_id, question, perspective))
        return {"parent_run_id": parent_run_id, "question": question, "perspective_id": perspective, "status": "failed", "errors": [], "warnings": []}


def test_debate_controller_delegates_and_projects_json_safe_result():
    facade = FakeFacade()
    controller = DebateTheaterController(facade)
    view = run(controller.run_debate(
        "case-1", "Why?", "2026-09-01T00:00:00+00:00", ["institutional_regulatory"]
    ))
    assert facade.debate_calls == [("case-1", "Why?", datetime(2026, 9, 1, tzinfo=timezone.utc), ("institutional_regulatory",))]
    assert view["status"] == "no_conclusion"
    assert view["as_of"] == "2026-09-01T00:00:00+00:00"
    assert view["fallback_reason"] == "provider failure"


def test_debate_controller_requires_aware_as_of_and_nonempty_inputs():
    controller = DebateTheaterController(FakeFacade())
    with pytest.raises(ValueError, match="timezone-aware"):
        run(controller.run_debate("case", "Q", "2026-09-01T00:00:00", []))
    with pytest.raises(ValueError, match="case_id"):
        run(controller.run_debate("", "Q", datetime.now(timezone.utc), []))


def test_follow_up_passes_parent_run_id_without_rerunning_debate():
    facade = FakeFacade()
    controller = DebateTheaterController(facade)
    view = run(controller.run_follow_up("parent-123", "Why now?", "institutional_regulatory"))
    assert facade.follow_calls == [("parent-123", "Why now?", "institutional_regulatory")]
    assert facade.debate_calls == []
    assert view["parent_run_id"] == "parent-123"


def test_page_builder_uses_injected_ui_and_registers_debate_handlers():
    class Element:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs, self.value = args, kwargs, kwargs.get("value")
            self.text = args[0] if args else ""
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def classes(self, *args, **kwargs): return self
        def update(self): return None
    class UI:
        def __init__(self): self.pages = {}; self.elements = []
        def page(self, route):
            return lambda fn: self.pages.setdefault(route, fn) or fn
        def __getattr__(self, name):
            def make(*args, **kwargs):
                e = Element(*args, **kwargs); e.name = name; self.elements.append(e); return e
            return make
    ui = UI(); build_debate_theater_page(DebateTheaterController(FakeFacade()), ui)
    assert "/debate" in ui.pages
    ui.pages["/debate"]()
    assert any(e.name == "button" for e in ui.elements)
