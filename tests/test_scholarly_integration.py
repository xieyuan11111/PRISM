"""Tests for scholarly abstract fallback through the public API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from prism.api import PrismAPI
from prism.api.fetching import SourceFetchReport
from prism.domain import Material
from prism.ingestion import IngestionResult
from prism.sources import FailureKind, SourceFetchError, SourceItem
from prism.store import IndexEntry

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
DOI_URL = "https://doi.org/10.1016/j.biortech.2025.133768"


class FakeSource:
    async def fetch(self, url, *, kind="auto"):
        raise SourceFetchError(FailureKind.HTTP_STATUS, url, "HTTP 403")


class FakeScholarly:
    def __init__(self):
        self.calls = []

    async def fetch(self, value):
        self.calls.append(value)
        return SourceItem(
            title="A scholarly work",
            source="academic",
            fetched_at=NOW,
            link="https://doi.org/10.1016/j.biortech.2025.133768",
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            summary="This is a public abstract.",
            content=None,
            type="academic",
            access_level="abstract_only",
            retrieval_level="abstract_only",
            doi="10.1016/j.biortech.2025.133768",
            authors=("Ada Lovelace",),
            container_title="Journal of Evidence",
        )


class FakeIngestion:
    def __init__(self):
        self.calls = []
        self.result = IngestionResult(
            Material(
                id="mat-abstract",
                title="A scholarly work",
                source="academic",
                published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                fetched_at=NOW,
                type="academic",
                content="This is a public abstract.",
                original_format="md",
                access_level="abstract_only",
                retrieval_level="abstract_only",
            ),
            Path("raw/mat-abstract.md"),
            Path("corpus/mat-abstract.md"),
            False,
            "direct",
        )

    def ingest(self, path, metadata=None):
        self.calls.append((path, metadata))
        return self.result


class FakeStore:
    def index_file(self, path):
        return type("Outcome", (), {"status": "indexed"})()

    def search(self, criteria, *, limit, offset):
        return []

    def get(self, source_id):
        return IndexEntry(
            source_id=source_id,
            title="A scholarly work",
            source="academic",
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            fetched_at=NOW,
            type="academic",
            content="This is a public abstract.",
            path="corpus/mat-abstract.md",
            content_hash="hash",
            url="https://doi.org/10.1016/j.biortech.2025.133768",
            retrieval_level="abstract_only",
            access_level="abstract_only",
            doi="10.1016/j.biortech.2025.133768",
            authors=("Ada Lovelace",),
            container_title="Journal of Evidence",
        )


class FakePlanner:
    def __init__(self):
        self.materials = []

    async def plan(self, material, extraction=None, *, core_claims=(), evidence_boundaries=()):
        self.materials.append(material)
        return "plan"


class FakeGraph:
    async def timeline(self, case_id, as_of):
        raise AssertionError("not used")

    async def add_case(self, case, **kwargs):
        raise AssertionError("not used")


class FakeEvents:
    async def publish(self, event):
        pass


def test_fetch_source_falls_back_to_public_academic_abstract(tmp_path):
    scholarly = FakeScholarly()
    ingestion = FakeIngestion()
    api = PrismAPI(
        ingestion,
        FakeStore(),
        FakeGraph(),
        FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path,
        scholarly_metadata_client=scholarly,
    )

    report = asyncio.run(api.fetch_source(DOI_URL, kind="page", process=False))

    assert scholarly.calls == [DOI_URL]
    assert isinstance(report, SourceFetchReport)
    assert report.items[0].material_id == "mat-abstract"
    # The intake report preserves the evidence level so callers can see the
    # fallback item was an abstract, never full text.
    assert report.items[0].access_level == "abstract_only"
    assert ingestion.calls[0][1]["access_level"] == "abstract_only"
    assert ingestion.calls[0][1]["retrieval_level"] == "abstract_only"
    assert ingestion.calls[0][1]["doi"] == "10.1016/j.biortech.2025.133768"
    assert ingestion.calls[0][1]["authors"] == ["Ada Lovelace"]
    assert ingestion.calls[0][1]["container_title"] == "Journal of Evidence"


def test_fetch_source_does_not_fallback_for_non_doi_url(tmp_path):
    scholarly = FakeScholarly()
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path,
        scholarly_metadata_client=scholarly,
    )

    try:
        asyncio.run(api.fetch_source("https://example.gov/article", process=False))
    except SourceFetchError as error:
        assert error.kind is FailureKind.HTTP_STATUS
    else:
        raise AssertionError("non-DOI source unexpectedly used scholarly fallback")
    assert scholarly.calls == []


def test_plan_research_by_id_threads_levels_and_doi_into_material():
    planner = FakePlanner()
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        research_planner=planner,
    )

    plan = asyncio.run(api.plan_research_by_id("mat-abstract"))

    assert plan == "plan"
    material = planner.materials[0]
    assert material.access_level == "abstract_only"
    assert material.retrieval_level == "abstract_only"
    assert material.doi == "10.1016/j.biortech.2025.133768"
    assert material.authors == ("Ada Lovelace",)
    assert material.container_title == "Journal of Evidence"
