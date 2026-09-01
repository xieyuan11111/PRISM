"""Public API/CLI integration contracts for research execution."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from io import StringIO
from pathlib import Path

from prism.api import PrismAPI
from prism.api.fetching import SourceFetchReport, SourceItemReport
from prism.cli import main
from prism.config import PrismConfig, SourceConfig
from prism.domain import Material
from prism.research import (
    ResearchExecutionReport,
    ResearchPlan,
    ResearchPlanner,
    ResearchExecutor,
)
from prism.sources import SourceItem
from prism.store import IndexEntry

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def make_material() -> Material:
    return Material(
        id="material-1",
        title="Policy notice",
        source="example.gov",
        published_at=NOW,
        fetched_at=NOW,
        type="policy",
        content="The policy changes implementation requirements.",
        original_format="md",
        case_tags=("case-1",),
        url="https://example.gov/policy",
    )


class FakeStore:
    def __init__(self, material: Material):
        self.entry = IndexEntry(
            source_id=material.id,
            title=material.title,
            source=material.source,
            published_at=material.published_at,
            fetched_at=material.fetched_at,
            type=material.type,
            content=material.content,
            path="corpus/policy.md",
            content_hash="hash",
            case_tags=material.case_tags,
            original_format=material.original_format,
            raw_path=material.raw_path,
            url=material.url,
        )

    def get(self, source_id):
        return self.entry if source_id == self.entry.source_id else None

    def index_file(self, path):
        return type("Outcome", (), {"status": "unchanged"})()

    def search(self, criteria, *, limit, offset):
        return []


class FakeIngestion:
    def ingest(self, path, metadata=None):
        raise AssertionError("not used")


class FakeGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("not used")

    async def add_case(self, case, **kwargs):
        raise AssertionError("not used")


class FakeEvents:
    async def publish(self, event):
        raise AssertionError("not used")


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.queries = []

    async def search(self, query, *, timeout=10.0):
        self.queries.append(query)
        return (
            SourceItem(
                title="Evidence",
                source="example.gov",
                fetched_at=NOW,
                link="https://example.gov/evidence",
                content="discovery only",
            ),
        )


class FakeIntake:
    def __init__(self):
        self.calls = []

    async def fetch_source(self, url, *, kind="page", process=True):
        self.calls.append((url, kind, process))
        item = SourceItemReport(
            title="Evidence",
            source="example.gov",
            link=url,
            material_id="material-2",
            spool_path=Path("raw/spool/a.md"),
            raw_path=Path("raw/a.md"),
            corpus_path=Path("corpus/a.md"),
        )
        return SourceFetchReport(url, NOW, (item,))


def test_api_plans_research_from_indexed_material_and_preserves_material_frontier():
    material = make_material()
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))),
        clock=lambda: NOW,
    )
    api = PrismAPI(
        FakeIngestion(), FakeStore(material), FakeGraph(), FakeEvents(),
        research_planner=planner,
    )

    plan = run(api.plan_research_by_id(material.id))

    assert isinstance(plan, ResearchPlan)
    assert plan.source_id == material.id
    assert plan.frontier_at == material.fetched_at
    assert plan.queries


def test_api_executes_plan_through_injected_provider_and_authoritative_intake():
    material = make_material()
    config = PrismConfig(sources=SourceConfig(("example.gov",)))
    planner = ResearchPlanner(config, clock=lambda: NOW)
    provider = FakeProvider()
    intake = FakeIntake()
    api = PrismAPI(
        FakeIngestion(), FakeStore(material), FakeGraph(), FakeEvents(),
        research_planner=planner,
        search_provider=provider,
        research_intake=intake,
    )
    plan = run(api.plan_research_by_id(material.id))
    report = run(api.execute_research(plan, process=False))

    assert isinstance(report, ResearchExecutionReport)
    assert report.source_id == material.id
    assert report.process is False
    assert provider.queries
    assert intake.calls
    assert all(kind == "page" and process is False for _, kind, process in intake.calls)


def test_api_rejects_search_without_authoritative_intake():
    material = make_material()
    planner = ResearchPlanner(
        PrismConfig(sources=SourceConfig(("example.gov",))), clock=lambda: NOW
    )
    api = PrismAPI(
        FakeIngestion(), FakeStore(material), FakeGraph(), FakeEvents(),
        research_planner=planner,
        search_provider=FakeProvider(),
    )
    plan = run(api.plan_research_by_id(material.id))

    try:
        run(api.execute_research(plan))
    except ValueError as error:
        assert "source_service or research_intake" in str(error)
    else:
        raise AssertionError("missing authoritative intake was not rejected")


def test_cli_discover_and_research_delegate_to_public_api():
    class FakeAPI:
        def __init__(self):
            self.calls = []
            self.plan = {"source_id": "material-1", "queries": []}
            self.result = {"source_id": "material-1", "material_ids": ["material-2"]}

        async def plan_research_by_id(self, source_id):
            self.calls.append(("plan", source_id))
            return self.plan

        async def execute_research(self, plan, *, process=True):
            self.calls.append(("execute", plan, process))
            return self.result

    api = FakeAPI()
    stdout, stderr = StringIO(), StringIO()
    status = run(main(["discover", "material-1"], api=api, stdout=stdout, stderr=stderr))
    assert status == 0
    assert json.loads(stdout.getvalue()) == api.plan
    assert stderr.getvalue() == ""

    stdout, stderr = StringIO(), StringIO()
    status = run(
        main(["research", "material-1", "--no-process"], api=api, stdout=stdout, stderr=stderr)
    )
    assert status == 0
    assert json.loads(stdout.getvalue()) == api.result
    assert api.calls[-2:] == [
        ("plan", "material-1"),
        ("execute", api.plan, False),
    ]
