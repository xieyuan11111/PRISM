"""WebUI workbench Phase A: the create_app facade-capability split.

``create_app`` declares only the case-home :class:`PrismFacade` protocol,
but the workbench registers richer pages (debate theater, evidence
browser, material intake with upload and journey controllers).  Those page
constructions must be capability-gated: an injected facade that provides
only the case-home operations — the shape every older fake and embedder
used — must keep building the app instead of crashing with a ``TypeError``
at construction time, while a facade that provides a page's operations
gets that page registered.  These tests run entirely offline through a
recording ``ui`` stand-in and a fake ``nicegui.app`` module, so they never
require (or import) the real optional dependency.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class _RecordingUI:
    """The slice of the NiceGUI ``ui`` module create_app touches eagerly."""

    def __init__(self) -> None:
        self.routes: list[str] = []

    def page(self, route: str):
        def register(fn):
            self.routes.append(route)
            return fn

        return register


@pytest.fixture()
def app_seam(monkeypatch):
    from prism.webui import app

    ui = _RecordingUI()
    monkeypatch.setattr(app, "_nicegui", lambda: ui)
    monkeypatch.setattr(app, "_plotly_graph_objects", lambda: object())
    nicegui_app = SimpleNamespace(name="nicegui-app")
    fake_nicegui = ModuleType("nicegui")
    fake_nicegui.app = nicegui_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nicegui", fake_nicegui)
    return app, ui, nicegui_app


class CaseOnlyFacade:
    """The historical case-home facade shape (PrismFacade)."""

    async def case_overviews(self, **filters):
        return ()

    async def case_overview(self, case_id):
        raise LookupError(case_id)

    async def query_historical_snapshot(
        self, case_id, as_of, *, stage=None, kinds=None
    ):
        raise AssertionError("not called during construction")


class LegacyMaterialsFacade(CaseOnlyFacade):
    async def add_material(self, *args, **kwargs):
        raise AssertionError("not called during construction")


class FullWorkbenchFacade(LegacyMaterialsFacade):
    async def debate_case(self, *args, **kwargs): ...

    async def follow_up_debate(self, *args, **kwargs): ...

    async def search(self, query=None, **filters): ...

    async def process_material(
        self, source, metadata=None, *, target_case=None
    ): ...

    async def material_journey(self, material_id): ...

    async def material_journeys(self, *, case_id=None, status=None): ...


def test_create_app_with_a_case_only_facade_registers_only_the_case_home(
    app_seam,
):
    app, ui, nicegui_app = app_seam

    result = app.create_app(CaseOnlyFacade())

    assert result is nicegui_app
    assert ui.routes == ["/"]


def test_create_app_keeps_the_legacy_materials_page_without_journey_ops(
    app_seam,
):
    app, ui, _ = app_seam

    app.create_app(LegacyMaterialsFacade())

    assert ui.routes == ["/", "/materials"]


def test_create_app_registers_every_page_for_a_full_workbench_facade(
    app_seam,
):
    app, ui, _ = app_seam

    app.create_app(FullWorkbenchFacade())

    assert "/" in ui.routes
    assert "/debate" in ui.routes
    assert "/evidence" in ui.routes
    assert "/materials" in ui.routes


# ------------------------------------------------- staging root anchoring


@pytest.fixture()
def staging_home():
    directory = Path(tempfile.mkdtemp(prefix="prism-webui-apphome-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_the_default_upload_staging_root_lives_inside_prism_home(
    app_seam, staging_home, monkeypatch
):
    app, _, _ = app_seam
    monkeypatch.setenv("PRISM_HOME", str(staging_home))

    from prism.config import PathConfig

    assert app._default_upload_staging_root() == (
        PathConfig.prism_home() / "staging" / "uploads"
    )
    assert app._default_upload_staging_root().is_relative_to(
        PathConfig.prism_home()
    )


def test_the_server_staging_provider_anchors_inside_prism_home(
    staging_home, monkeypatch
):
    from prism.webui import server

    monkeypatch.setenv("PRISM_HOME", str(staging_home))

    root = server._upload_staging_root()

    assert root == staging_home / "staging" / "uploads"
