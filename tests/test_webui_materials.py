"""M3 WebUI slice: the material-entry intake over the existing facade.

This module covers the dependency-free controller/view-model seam
(:class:`prism.webui.materials.MaterialEntryController`) — explicit MD/PDF
paths appended to an explicitly declared target case through
``PrismAPI.add_material``, never guessing a case, never writing the corpus
directly, never calling an LLM unless the caller opts in — plus the
JSON-safe outcome view (pipeline status and stage audit, report version,
debate-link prior/current evidence hashes with the stale flag) and the
lazy-NiceGUI page-builder seam exercised through a recording ``ui``
stand-in.  Failures propagate: the controller never returns a fake success.

Everything is offline: a synthetic facade over real ``ProcessMaterialResult``
objects, no NiceGUI import, no pipeline runtime, no LLM, no network.  The
material fixture uses its own ``tempfile`` directory because this sandboxed
environment denies access to pytest's ``tmp_path`` root.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.api.facade import MaterialDebateLink, ProcessMaterialResult
from prism.pipeline import PipelineError
from prism.pipeline.service import PipelineRun, PipelineStage


UTC = timezone.utc
AS_OF = datetime(2026, 9, 1, tzinfo=UTC)
STARTED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 1, 12, 0, 5, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def material_file():
    directory = Path(tempfile.mkdtemp(prefix="prism-webui-materials-"))
    try:
        material = directory / "note.md"
        material.write_text(
            "# Material\n\nThe agency revised the policy.", encoding="utf-8"
        )
        yield material
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _pipeline_run(material_id="mat-1", status="completed"):
    return PipelineRun(
        material_id=material_id,
        status=status,
        stages=(
            PipelineStage("index", "skipped", None,
                          detail="already indexed in this process"),
            PipelineStage("extract", "skipped", None,
                          detail="idempotent replay"),
        ),
        started_at=STARTED,
        finished_at=FINISHED,
    )


def _report_version():
    return SimpleNamespace(
        version_id="rv-1",
        case_id="case-b",
        as_of=AS_OF,
        created_at=FINISHED,
        input_hash="a" * 64,
        markdown_hash="c" * 64,
        summary_origin="deterministic",
        debate_input_hash=None,
        markdown="# Report\n\nbody text that must not flood the status view",
        parent_version_id=None,
        trigger="material_added",
    )


def _debate_link(stale=True):
    return MaterialDebateLink(
        parent_run_id="run-1",
        case_id="case-b",
        as_of=AS_OF,
        prior_evidence_bundle_hash="a" * 64,
        current_evidence_bundle_hash="b" * 64,
        affected=True,
        stale=stale,
    )


def _result(*, with_link=True, replayed=False):
    return ProcessMaterialResult(
        material_id="mat-1",
        pipeline=_pipeline_run(),
        case_id="case-b",
        case_outcome=None,
        warnings=("warning: evidence gap recorded",),
        replayed=replayed,
        report_version=_report_version(),
        debate_link=_debate_link() if with_link else None,
    )


class FakeMaterialFacade:
    """Synthetic PrismAPI stand-in recording every append it receives."""

    def __init__(self, result=None, error=None):
        self._result = result if result is not None else _result()
        self._error = error
        self.calls = []

    async def add_material(self, source, target_case, metadata=None, *,
                           as_of=None, use_llm=True,
                           parent_debate_run_id=None):
        self.calls.append(
            dict(source=source, target_case=target_case, metadata=metadata,
                 as_of=as_of, use_llm=use_llm,
                 parent_debate_run_id=parent_debate_run_id)
        )
        if self._error is not None:
            raise self._error
        return self._result


def _controller(material, *, facade=None) -> object:
    from prism.webui.materials import MaterialEntryController

    controller = MaterialEntryController(
        facade if facade is not None else FakeMaterialFacade()
    )
    controller._test_material = material
    return controller


def _material_path(controller) -> str:
    return str(controller._test_material)


# ------------------------------------------------------------- controller seam


def test_controller_rejects_a_facade_without_add_material():
    from prism.webui.materials import MaterialEntryController

    class Empty:
        pass

    with pytest.raises(TypeError, match="add_material"):
        MaterialEntryController(Empty())


def test_submit_requires_an_explicit_md_or_pdf_path(material_file):
    controller = _controller(material_file)
    facade = controller._api

    with pytest.raises(ValueError, match="path"):
        run(controller.submit("", "case-b"))
    with pytest.raises(TypeError, match="path"):
        run(controller.submit(42, "case-b"))
    text = material_file.parent / "note.txt"
    text.write_text("plain text", encoding="utf-8")
    with pytest.raises(ValueError, match="\\.md|\\.pdf"):
        run(controller.submit(str(text), "case-b"))
    with pytest.raises(FileNotFoundError, match="missing\\.md"):
        run(controller.submit(
            str(material_file.parent / "missing.md"), "case-b"
        ))
    assert facade.calls == []


def test_submit_requires_an_explicit_target_case(material_file):
    controller = _controller(material_file)
    facade = controller._api

    with pytest.raises(ValueError, match="target_case"):
        run(controller.submit(_material_path(controller), "  "))
    with pytest.raises(ValueError, match="target_case"):
        run(controller.submit(_material_path(controller), None))
    assert facade.calls == []


def test_submit_delegates_to_add_material_exactly(material_file):
    facade = FakeMaterialFacade()
    controller = _controller(material_file, facade=facade)
    path = _material_path(controller)

    view = run(controller.submit(
        path, "case-b",
        metadata={"origin": "user-provided"},
        as_of="2026-09-01T00:00:00+00:00",
        parent_debate_run_id="run-1",
    ))

    assert facade.calls == [
        dict(source=Path(path), target_case="case-b",
             metadata={"origin": "user-provided"}, as_of=AS_OF,
             use_llm=False, parent_debate_run_id="run-1")
    ]
    assert view["material_id"] == "mat-1"


def test_submit_defaults_to_no_llm_and_no_parent(material_file):
    facade = FakeMaterialFacade()
    controller = _controller(material_file, facade=facade)

    run(controller.submit(_material_path(controller), "case-b"))

    (call,) = facade.calls
    assert call["use_llm"] is False
    assert call["as_of"] is None
    assert call["parent_debate_run_id"] is None
    assert call["metadata"] is None


def test_submit_forwards_an_llm_opt_in_explicitly(material_file):
    facade = FakeMaterialFacade()
    controller = _controller(material_file, facade=facade)

    run(controller.submit(_material_path(controller), "case-b", use_llm=True))

    assert facade.calls[0]["use_llm"] is True


def test_submit_rejects_invalid_optional_arguments_before_any_call(
    material_file,
):
    controller = _controller(material_file)
    facade = controller._api
    path = _material_path(controller)

    with pytest.raises(ValueError, match="as_of"):
        run(controller.submit(path, "case-b", as_of=datetime(2026, 9, 1)))
    with pytest.raises(ValueError, match="as_of"):
        run(controller.submit(path, "case-b", as_of="2026-09-01"))
    with pytest.raises(TypeError, match="as_of"):
        run(controller.submit(path, "case-b", as_of=20260901))
    with pytest.raises(TypeError, match="use_llm"):
        run(controller.submit(path, "case-b", use_llm="no"))
    with pytest.raises(ValueError, match="parent_debate_run_id"):
        run(controller.submit(path, "case-b", parent_debate_run_id="  "))
    with pytest.raises(TypeError, match="metadata"):
        run(controller.submit(path, "case-b", metadata=["not", "a", "dict"]))
    assert facade.calls == []


def test_view_is_json_safe_and_reports_the_full_outcome(material_file):
    controller = _controller(material_file)

    view = run(controller.submit(_material_path(controller), "case-b"))

    assert view["status"] == "completed"
    assert view["case_id"] == "case-b"
    assert view["warnings"] == ["warning: evidence gap recorded"]
    assert view["replayed"] is False
    assert json.loads(json.dumps(view)) == view

    pipeline = view["pipeline"]
    assert pipeline["material_id"] == "mat-1"
    assert pipeline["status"] == "completed"
    assert pipeline["started_at"] == STARTED.isoformat()
    assert pipeline["finished_at"] == FINISHED.isoformat()
    assert [stage["name"] for stage in pipeline["stages"]] == [
        "index", "extract"
    ]
    assert all("result" not in stage for stage in pipeline["stages"])

    version = view["report_version"]
    assert version["version_id"] == "rv-1"
    assert version["case_id"] == "case-b"
    assert version["trigger"] == "material_added"
    assert version["as_of"] == AS_OF.isoformat()
    assert "markdown" not in version

    link = view["debate_link"]
    assert link["parent_run_id"] == "run-1"
    assert link["prior_evidence_bundle_hash"] == "a" * 64
    assert link["current_evidence_bundle_hash"] == "b" * 64
    assert link["affected"] is True
    assert link["stale"] is True
    assert link["as_of"] == AS_OF.isoformat()


def test_view_without_a_debate_link_reports_none_truthfully(material_file):
    facade = FakeMaterialFacade(result=_result(with_link=False))
    controller = _controller(material_file, facade=facade)

    view = run(controller.submit(_material_path(controller), "case-b"))

    assert view["debate_link"] is None


def test_a_replayed_run_is_reported_as_such(material_file):
    facade = FakeMaterialFacade(result=_result(replayed=True))
    controller = _controller(material_file, facade=facade)

    view = run(controller.submit(_material_path(controller), "case-b"))

    assert view["replayed"] is True


def test_a_facade_failure_propagates_and_never_returns_success(material_file):
    error = PipelineError(
        "graph stage failed", stage="graph", material_id="mat-1"
    )
    facade = FakeMaterialFacade(error=error)
    controller = _controller(material_file, facade=facade)

    with pytest.raises(PipelineError, match="graph stage failed"):
        run(controller.submit(_material_path(controller), "case-b"))
    assert len(facade.calls) == 1


# ------------------------------------------------------ import / page seam


def test_importing_the_module_never_imports_nicegui():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "nicegui" or name.startswith("nicegui.")
    }
    for name in saved:
        del sys.modules[name]
    try:
        from prism.webui import materials

        assert callable(materials.MaterialEntryController)
        assert callable(materials.build_material_entry_page)
        assert not any(
            name == "nicegui" or name.startswith("nicegui.")
            for name in sys.modules
        )
    finally:
        sys.modules.update(saved)


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

    def on(self, event, handler):
        self.events = getattr(self, "events", {})
        self.events[event] = handler
        return self


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
    from prism.webui.materials import build_material_entry_page

    ui = _FakeUI()
    build_material_entry_page(controller, ui)
    page = ui.pages["/materials"]
    page()
    return ui


def test_page_seam_lists_the_explicit_intake_controls(material_file):
    ui = _build_page(_controller(material_file))

    for label in ("Path", "Target case", "as of",
                  "Parent debate run (optional)"):
        assert _element(ui, "input", label=label) is not None
    assert _element(ui, "switch", text="LLM").kwargs["value"] is False
    _element(ui, "button", text="Append material")
    status = _element(ui, "markdown")
    assert "No material" in status.content


def test_page_seam_appends_through_the_controller_and_shows_the_outcome(
    material_file,
):
    facade = FakeMaterialFacade()
    controller = _controller(material_file, facade=facade)
    ui = _build_page(controller)

    _element(ui, "input", label="Path").value = _material_path(
        controller
    )
    _element(ui, "input", label="Target case").value = "case-b"
    _element(ui, "input", label="Parent debate run").value = "run-1"
    run(_element(ui, "button", text="Append material").kwargs["on_click"](None))

    (call,) = facade.calls
    assert call["target_case"] == "case-b"
    assert call["parent_debate_run_id"] == "run-1"
    assert call["use_llm"] is False

    status = _element(ui, "markdown")
    assert "mat-1" in status.content
    assert "completed" in status.content
    assert "rv-1" in status.content
    assert "run-1" in status.content
    assert "a" * 64 in status.content
    assert "stale" in status.content


def test_page_seam_reports_validation_errors_without_any_append(
    material_file,
):
    facade = FakeMaterialFacade()
    controller = _controller(material_file, facade=facade)
    ui = _build_page(controller)

    _element(ui, "input", label="Target case").value = "case-b"
    run(_element(ui, "button", text="Append material").kwargs["on_click"](None))

    assert facade.calls == []
    assert any("path" in label.text for label in _labels(ui))

    _element(ui, "input", label="Path").value = _material_path(
        controller
    )
    _element(ui, "input", label="Target case").value = " "
    run(_element(ui, "button", text="Append material").kwargs["on_click"](None))

    assert facade.calls == []
    assert any("target_case" in label.text for label in _labels(ui))


def test_page_seam_reports_pipeline_failures_and_never_claims_success(
    material_file,
):
    error = PipelineError(
        "graph stage failed", stage="graph", material_id="mat-1"
    )
    facade = FakeMaterialFacade(error=error)
    controller = _controller(material_file, facade=facade)
    ui = _build_page(controller)

    _element(ui, "input", label="Path").value = _material_path(
        controller
    )
    _element(ui, "input", label="Target case").value = "case-b"
    run(_element(ui, "button", text="Append material").kwargs["on_click"](None))

    assert any("failed" in label.text for label in _labels(ui))
    status = _element(ui, "markdown")
    assert "No material" in status.content
