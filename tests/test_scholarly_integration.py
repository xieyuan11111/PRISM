"""Tests for scholarly abstract fallback through the public API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from prism.api import PrismAPI
from prism.api.fetching import SourceFetchReport
from prism.domain import Material
from prism.ingestion import IngestionResult
from prism.sources import FailureKind, SourceFetchError, SourceItem
from prism.store import IndexEntry

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
DOI_URL = "https://doi.org/10.1016/j.biortech.2025.133768"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/40212345/"
NATURE_URL = "https://www.nature.com/articles/s41586-024-99999-9"


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


class FakePubMedScholarly:
    def __init__(self):
        self.calls = []

    async def fetch(self, value):
        self.calls.append(value)
        return SourceItem(
            title="A PubMed work",
            source="academic",
            fetched_at=NOW,
            link=PUBMED_URL,
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            summary="A public PubMed abstract.",
            content=None,
            type="academic",
            access_level="abstract_only",
            retrieval_level="abstract_only",
            pmid="40212345",
            pmcid="PMC8880123",
            authors=("Lovelace A",),
            container_title="Journal of Public Evidence",
        )


def test_fetch_source_falls_back_for_pubmed_pmid_url(tmp_path):
    scholarly = FakePubMedScholarly()
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

    report = asyncio.run(api.fetch_source(PUBMED_URL, kind="page", process=False))

    assert scholarly.calls == [PUBMED_URL]
    assert report.items[0].access_level == "abstract_only"
    assert ingestion.calls[0][1]["pmid"] == "40212345"
    assert ingestion.calls[0][1]["pmcid"] == "PMC8880123"


def test_fetch_source_does_not_fallback_for_pmcid_free_publisher_url(tmp_path):
    scholarly = FakeScholarly()
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path,
        scholarly_metadata_client=scholarly,
    )

    try:
        asyncio.run(api.fetch_source(NATURE_URL, process=False))
    except SourceFetchError as error:
        assert error.kind is FailureKind.HTTP_STATUS
    else:
        raise AssertionError("identifier-less URL unexpectedly used scholarly fallback")
    assert scholarly.calls == []


class FakeTitleScholarly:
    def __init__(self):
        self.calls = []

    async def fetch(self, value):
        raise AssertionError("fetch() must not be called for title resolution")

    async def fetch_by_title(self, title, *, link=None):
        self.calls.append((title, link))
        return SourceItem(
            title=title,
            source="academic",
            fetched_at=NOW,
            link=link or "https://doi.org/10.1038/s41586-024-99999-9",
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            summary="A public abstract matched by strict title comparison.",
            content=None,
            type="academic",
            access_level="abstract_only",
            retrieval_level="abstract_only",
            doi="10.1038/s41586-024-99999-9",
            authors=("Ada Lovelace",),
            container_title="Nature",
        )


def test_fetch_scholarly_by_title_routes_through_intake(tmp_path):
    scholarly = FakeTitleScholarly()
    ingestion = FakeIngestion()
    api = PrismAPI(
        ingestion,
        FakeStore(),
        FakeGraph(),
        FakeEvents(),
        source_raw_dir=tmp_path,
        scholarly_metadata_client=scholarly,
    )

    report = asyncio.run(
        api.fetch_scholarly_by_title(
            "A Quantitative Study of Public Evidence",
            link=NATURE_URL,
            process=False,
        )
    )

    assert scholarly.calls == [("A Quantitative Study of Public Evidence", NATURE_URL)]
    assert isinstance(report, SourceFetchReport)
    assert report.url == NATURE_URL
    assert report.items[0].access_level == "abstract_only"
    assert ingestion.calls[0][1]["doi"] == "10.1038/s41586-024-99999-9"


def test_fetch_scholarly_by_title_requires_scholarly_client(tmp_path):
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        source_raw_dir=tmp_path,
    )
    with pytest.raises(ValueError):
        asyncio.run(api.fetch_scholarly_by_title("Some title", process=False))


def test_fetch_scholarly_by_title_requires_raw_dir_with_pipeline():
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        scholarly_metadata_client=FakeTitleScholarly(),
    )
    with pytest.raises(ValueError):
        asyncio.run(api.fetch_scholarly_by_title("Some title", process=False))


def test_fetch_source_does_not_fallback_for_pubmed_lookalike_urls(tmp_path):
    """Embedded PMIDs/PMCIDs in foreign URLs never trigger the fallback.

    Regression for the review finding: identifier extraction used to match
    ``pubmed.ncbi.nlm.nih.gov/...`` text anywhere inside a URL, so a
    malicious page URL whose path, query, or userinfo embedded a genuine
    PubMed/PMC string would route to the scholarly fallback.
    """
    scholarly = FakeScholarly()
    api = PrismAPI(
        FakeIngestion(), FakeStore(), FakeGraph(), FakeEvents(),
        source_service=FakeSource(),
        source_raw_dir=tmp_path,
        scholarly_metadata_client=scholarly,
    )

    malicious_urls = (
        "https://evil.example/pubmed.ncbi.nlm.nih.gov/40212345",
        "https://evil.example/redirect?next=https://pubmed.ncbi.nlm.nih.gov/40212345",
        "https://evil.example/?u=https%3A%2F%2Fpmc.ncbi.nlm.nih.gov%2Farticles%2FPMC8880123",
        "https://pubmed.ncbi.nlm.nih.gov@evil.example/40212345",
        "https://evil.example@pmc.ncbi.nlm.nih.gov/articles/PMC8880123",
    )
    for url in malicious_urls:
        try:
            asyncio.run(api.fetch_source(url, process=False))
        except SourceFetchError as error:
            assert error.kind is FailureKind.HTTP_STATUS
        else:
            raise AssertionError(f"scholarly fallback fired for lookalike URL: {url}")
    assert scholarly.calls == []
