"""Dependency-free Debate Theater controller and optional NiceGUI page."""
from __future__ import annotations
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol
from .controller import parse_as_of

class DebateFacade(Protocol):
    async def debate_case(self, case_id: str, question: str, as_of: datetime, perspectives: Iterable[str] | None = None) -> object: ...
    async def follow_up_debate(self, parent_run_id: str, question: str, perspective: str) -> object: ...

def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, (tuple, list, set, frozenset)): return [_json_safe(item) for item in value]
    if isinstance(value, dict): return {str(k): _json_safe(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"): return {name: _json_safe(getattr(value, name)) for name in value.__dataclass_fields__}
    return value

class DebateTheaterController:
    def __init__(self, api: DebateFacade) -> None:
        for name in ("debate_case", "follow_up_debate"):
            if not callable(getattr(api, name, None)): raise TypeError(f"api must provide {name}()")
        self._api = api

    async def run_debate(self, case_id: str, question: str, as_of: datetime | str, perspectives: Iterable[str] | None = None) -> dict[str, Any]:
        if not isinstance(case_id, str) or not case_id.strip(): raise ValueError("case_id must be a non-empty string")
        if not isinstance(question, str) or not question.strip(): raise ValueError("question must be a non-empty string")
        instant = parse_as_of(as_of)
        selected = None if perspectives is None else tuple(perspectives)
        if selected is not None and any(not isinstance(item, str) or not item.strip() for item in selected): raise ValueError("perspectives must contain non-empty strings")
        return _json_safe(await self._api.debate_case(case_id.strip(), question.strip(), instant, selected))

    async def run_follow_up(self, parent_run_id: str, question: str, perspective: str) -> dict[str, Any]:
        for name, value in (("parent_run_id", parent_run_id), ("question", question), ("perspective", perspective)):
            if not isinstance(value, str) or not value.strip(): raise ValueError(f"{name} must be a non-empty string")
        return _json_safe(await self._api.follow_up_debate(parent_run_id.strip(), question.strip(), perspective.strip()))

def build_debate_theater_page(controller: DebateTheaterController, ui: Any, *, route: str = "/debate") -> Any:
    @ui.page(route)
    def debate_page() -> None:
        status = ui.label("Ready")
        case_id = ui.input(label="Case ID"); as_of = ui.input(label="As of (timezone-aware ISO 8601)")
        question = ui.input(label="Question"); perspectives = ui.input(label="Perspectives (comma-separated)")
        parent = ui.input(label="Parent run ID"); follow_question = ui.input(label="Follow-up question"); follow_perspective = ui.input(label="Follow-up perspective")
        output = ui.json({}) if hasattr(ui, "json") else ui.label("{}")
        async def run_handler(event: Any = None) -> None:
            try:
                view = await controller.run_debate(case_id.value or "", question.value or "", as_of.value or "", tuple(x.strip() for x in (perspectives.value or "").split(",") if x.strip()))
                if hasattr(output, "value"): output.value = view
                status.text = f"debate: {view.get('status', 'completed')}"
            except Exception as error: status.text = f"error: {error}"
            status.update()
        async def follow_handler(event: Any = None) -> None:
            try:
                view = await controller.run_follow_up(parent.value or "", follow_question.value or "", follow_perspective.value or "")
                if hasattr(output, "value"): output.value = view
                status.text = f"follow-up: {view.get('status', 'completed')}"
            except Exception as error: status.text = f"error: {error}"
            status.update()
        ui.button("Run debate", on_click=run_handler); ui.button("Ask follow-up", on_click=follow_handler)
    return debate_page

__all__ = ["DebateTheaterController", "DebateFacade", "build_debate_theater_page"]
